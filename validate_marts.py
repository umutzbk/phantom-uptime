"""
Validates the analytical layer and scores the silent-failure detector.

Four groups:
  1. structure   - do the analytical tables hold together
  2. metrics     - do the two reliability numbers recompute from their own basis
  3. detector    - how well does detection recover the planted ground truth
  4. hero gate   - does the CHG-0034 narrative hold in the data

Group 3 is the one that keeps the demo honest. The transformation layer detects
silent failures from the data; the simulator's planted list is used ONLY here,
to score that detection. If detection ever reads the ground truth, this file is
the thing that would stop being meaningful.

Exit 0 if everything passes, 1 otherwise.
"""
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
DB = ROOT / "marts" / "voltway.duckdb"
GT = json.loads((ROOT / "raw" / "_manifest.json").read_text(encoding="utf-8"))["ground_truth"]

con = duckdb.connect(str(DB), read_only=True)
q = lambda sql: con.execute(sql).fetchall()
one = lambda sql: con.execute(sql).fetchone()[0]

fails = []


def check(label, ok, detail=""):
    print("  {}  {}{}".format("PASS" if ok else "FAIL", label,
                              "" if ok else "   <- {}".format(detail)))
    if not ok:
        fails.append(label)


# ---------------------------------------------------------------- 1
print("\n1. ANALYTICAL LAYER STRUCTURE")
check("every charger belongs to a known station",
      one("SELECT COUNT(*) FROM dim_charger c "
          "LEFT JOIN dim_station s USING (station_id) WHERE s.station_id IS NULL") == 0)
check("every attempt resolves to a charger and a customer",
      one("SELECT COUNT(*) FROM fact_charging_attempt a "
          "LEFT JOIN dim_charger c USING (charger_id) "
          "LEFT JOIN dim_customer u USING (customer_id) "
          "WHERE c.charger_id IS NULL OR u.customer_id IS NULL") == 0)
check("status intervals never overlap on the same charger",
      one("""SELECT COUNT(*) FROM (
               SELECT charger_id, interval_end,
                      LEAD(interval_start) OVER (PARTITION BY charger_id
                                                 ORDER BY interval_start) AS nxt
               FROM fact_status_interval)
             WHERE nxt IS NOT NULL AND nxt < interval_end""") == 0)
check("every charger has 30 days of status rollup",
      one("SELECT COUNT(*) FROM agg_charger_status_daily") == 600 * 30)
check("status minutes reconcile to the day",
      one("SELECT COUNT(*) FROM agg_charger_status_daily "
          "WHERE ABS(total_minutes - 1440) > 1.5") == 0,
      "{} days off".format(one("SELECT COUNT(*) FROM agg_charger_status_daily "
                               "WHERE ABS(total_minutes - 1440) > 1.5")))
check("uptime excludes Faulted and Unavailable only",
      one("SELECT COUNT(*) FROM fact_status_interval "
          "WHERE counts_as_uptime <> (status NOT IN ('Faulted','Unavailable'))") == 0)
check("successful attempts carry energy and revenue",
      one("SELECT COUNT(*) FROM fact_charging_attempt "
          "WHERE is_successful AND (energy_kwh <= 0 OR revenue_gbp <= 0)") == 0)
check("failed attempts carry no revenue",
      one("SELECT COUNT(*) FROM fact_charging_attempt "
          "WHERE NOT is_successful AND revenue_gbp > 0") == 0)
check("no attempt starts inside a logged fault window",
      one("""SELECT COUNT(*) FROM fact_charging_attempt a JOIN fact_fault f
               ON f.charger_id = a.charger_id
              AND a.start_ts >= f.opened_ts
              AND a.start_ts < COALESCE(f.closed_ts, TIMESTAMP '2026-09-05 00:00:00')""") == 0)
check("alerts only ever derive from logged faults",
      one("SELECT COUNT(*) FROM fact_alert a LEFT JOIN fact_fault f "
          "ON f.fault_id = a.source_fault_id WHERE f.fault_id IS NULL") == 0)

# ---------------------------------------------------------------- 2
print("\n2. RELIABILITY METRICS")
rep = one("SELECT ROUND(100.0*SUM(uptime_minutes)/SUM(total_minutes),2) "
          "FROM agg_charger_status_daily")
dlv = one("SELECT ROUND(100.0*SUM(CASE WHEN is_successful THEN 1 ELSE 0 END)/COUNT(*),2) "
          "FROM fact_charging_attempt")
summ = q("SELECT reported_reliability_percent, delivered_reliability_percent, "
         "reliability_gap_points FROM mart_reliability_summary")[0]
check("reported reliability recomputes from status minutes", abs(rep - summ[0]) < 0.01)
check("delivered reliability recomputes from attempts", abs(dlv - summ[1]) < 0.01)
check("the gap is positive and material", summ[2] > 1.0, "{} points".format(summ[2]))
check("chargers spend time in Preparing without ever charging (blind spot exists)",
      one("SELECT SUM(preparing_minutes) FROM agg_charger_status_daily") > 0)
gapc = q("SELECT delivered_failure_rate_points, reported_downtime_points, "
         "reliability_gap_points FROM mart_reliability_summary")[0]
check("gap reconciles: delivered failure rate - reported downtime",
      abs((gapc[0] - gapc[1]) - gapc[2]) <= 0.02,
      "{} - {} != {}".format(*gapc))

# ---------------------------------------------------------------- 3
print("\n3. SILENT-FAILURE DETECTOR")
planted = set(GT["impaired_chargers"])
found = {r[0] for r in q("SELECT charger_id FROM mart_silent_failures")}
tp, fp, fn = found & planted, found - planted, planted - found
precision = len(tp) / len(found) if found else 0
recall = len(tp) / len(planted)
check("detector raises no false positives", not fp,
      "{} chargers flagged that were never impaired".format(len(fp)))
check("detector recovers most of the planted impairments", recall >= 0.60,
      "recall {:.0%}".format(recall))
check("none of the detected failures are already in the alert queue",
      one("SELECT alert_silent_failure_overlap FROM mart_alert_blindspot") == 0)
check("detection never reads the ground truth",
      "_manifest" not in (ROOT / "build_marts.py").read_text(encoding="utf-8")
      and "ground_truth" not in (ROOT / "build_marts.py").read_text(encoding="utf-8"))

print("     precision {:.0%}  ·  recall {:.0%}  ·  {} detected of {} planted".format(
    precision, recall, len(found), len(planted)))
for k, v in GT["by_pattern"].items():
    s = set(v)
    print("       {:<18} {}/{}".format(k, len(s & found), len(s)))

# ---------------------------------------------------------------- 4
print("\n4. HERO GATE — CHG-0034")
HERO = GT["hero"]
h = q("SELECT * FROM mart_silent_failures WHERE charger_id = '{}'".format(HERO))
cols = [d[0] for d in con.execute(
    "SELECT * FROM mart_silent_failures LIMIT 0").description]
check("hero is detected at all", bool(h))
H = dict(zip(cols, h[0], strict=True)) if h else {}

check("hero raises no fault event",
      one("SELECT COUNT(*) FROM fact_fault WHERE charger_id='{}'".format(HERO)) == 0)
check("hero raises no alert",
      one("SELECT COUNT(*) FROM fact_alert WHERE charger_id='{}'".format(HERO)) == 0)
check("hero never reached Charging in the episode",
      one("""SELECT COUNT(*) FROM fact_status_interval
             WHERE charger_id='{}' AND status='Charging'
               AND interval_start >= TIMESTAMP '2026-09-02 00:00:00'""".format(HERO)) == 0)
check("every hero status minute counted as uptime in the episode",
      one("""SELECT COUNT(*) FROM agg_charger_status_daily
             WHERE charger_id='{}' AND status_date >= DATE '2026-09-02'
               AND (offline_minutes > 0 OR faulted_minutes > 0)""".format(HERO)) == 0)
check("hero delivered zero energy", H.get("energy_kwh") == 0)
check("hero had real drivers attempt to charge", H.get("attempted_sessions", 0) >= 20)
check("hero site peers were healthy", H.get("peer_success_percent", 0) > 88,
      "{}% peer success".format(H.get("peer_success_percent")))
check("hero is still failing at the end of the period", bool(H.get("is_ongoing")))
check("hero is the first dispatch recommendation", H.get("dispatch_priority") == 1)

# ---------------------------------------------------------------- gate
b = dict(zip([d[0] for d in con.execute("SELECT * FROM mart_alert_blindspot LIMIT 0").description],
             q("SELECT * FROM mart_alert_blindspot")[0], strict=True))
print("\n" + "=" * 66)
print("  HERO GATE  ·  {} charger Midlands & North pilot".format(b["pilot_chargers"]))
print("=" * 66)
row = lambda k, v: print("  {:<30} {}".format(k, v))
row("Reported reliability", "{:.2f}%".format(summ[0]))
row("Delivered reliability", "{:.2f}%".format(summ[1]))
row("Reliability gap", "{:.2f} percentage points".format(summ[2]))
print()
row("Visible alert queue", "{} alerts across {} devices".format(
    b["visible_alert_count"], b["visible_alert_device_count"]))
row("Silent failures detected", b["silent_failure_device_count"])
row("Overlap", "{}  <- the blind spot".format(b["alert_silent_failure_overlap"]))
row("Hidden revenue at risk", "GBP {:,.0f}".format(b["hidden_revenue_at_risk_gbp"]))
row("Still failing today", one("SELECT COUNT(*) FROM mart_silent_failures WHERE is_ongoing"))
print()
row("First dispatch", "{}  {}".format(HERO, H.get("station_name")))
row("Pattern", H.get("failure_pattern"))
row("Reported uptime", "{:.2f}%".format(H.get("reported_uptime_percent", 0)))
row("Delivered success", "{:.0f}%  (site peers {:.1f}%)".format(
    H.get("delivered_success_percent", 0), H.get("peer_success_percent", 0)))
row("Attempts / successful", "{} / {}".format(
    H.get("attempted_sessions"), H.get("successful_sessions")))
row("Energy delivered", "{} kWh".format(H.get("energy_kwh")))
row("Fault alerts", 0)
row("Observed window", "{} to {}  ({} days, ongoing)".format(
    H.get("observed_start"), H.get("observed_end"), H.get("window_days")))
row("Estimated lost sessions", H.get("estimated_lost_sessions"))
row("Estimated lost kWh", H.get("estimated_lost_kwh"))
row("Revenue at risk", "GBP {:,.2f}".format(H.get("estimated_revenue_at_risk_gbp", 0)))
row("Daily loss rate", "GBP {:,.2f}/day".format(H.get("daily_loss_rate_gbp", 0)))
row("Confidence", H.get("confidence"))
row("Recommended action", H.get("recommended_action"))
print("=" * 66)
print("  Estimate for operational prioritisation, not an accounting-grade")
print("  lost-revenue calculation.")

s = q("SELECT failures_on_detected_devices, failures_baseline_diffuse, "
      "failure_points_from_detected_devices, failure_points_baseline_diffuse, "
      "attempted_sessions, delivered_failure_rate_points, reported_downtime_points, "
      "reliability_gap_points FROM mart_reliability_summary")[0]
print("\n  Delivered failure rate — {:,} failed attempts of {:,} = {:.2f}%".format(
    s[0] + s[1], s[4], s[5]))
print("    from detected silent-failure devices : {:>6,}  ({:.2f} pts)".format(s[0], s[2]))
print("    widespread baseline failures         : {:>6,}  ({:.2f} pts)".format(s[1], s[3]))
print("\n  This is the anatomy of the FAILURE RATE, not of the gap.")
print("    delivered failure rate {:.2f} - reported downtime {:.2f} "
      "= reported-vs-delivered gap {:.2f}".format(s[5], s[6], s[7]))

print("\n" + ("All checks passed.\n" if not fails else "{} CHECKS FAILED\n".format(len(fails))))
con.close()
sys.exit(1 if fails else 0)
