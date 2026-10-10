"""
Validates the approved tool layer against the running Cube instance.

What this is really checking is the guardrail, not the arithmetic: that every
number the copilot can ever show came from the semantic layer, that no tool
touches raw operational data or the simulator's ground truth, and that the hero
question can be answered end to end using tool output alone.

Prerequisites:  docker compose up -d   (Cube on :4000)
Run:            python test_tools.py
"""
from __future__ import annotations

import ast
import inspect
import json
import sys
from pathlib import Path

from api import cube_client as cube
from api import tools as T
from api.tool_schemas import TOOL_SCHEMAS

ROOT = Path(__file__).parent
fails: list[str] = []


def check(label, ok, detail=""):
    print("  {}  {}{}".format("PASS" if ok else "FAIL", label,
                              "" if ok else "   <- {}".format(detail)))
    if not ok:
        fails.append(label)


# ---------------------------------------------------------------- 1
print("\n1. DEPENDENCIES")
try:
    cube.load({"measures": ["reliability_summary.reported_reliability_percent"]})
    reachable = True
    err = ""
except Exception as exc:                                       # noqa: BLE001
    reachable, err = False, str(exc)[:160]
check("Cube is reachable", reachable, err)
if not reachable:
    print("\n  Start it with: docker compose up -d\n")
    sys.exit(1)
check("Cube publishes the expected cubes",
      {c["name"] for c in cube.meta()["cubes"]} >= {
          "reliability_summary", "alert_blindspot", "reliability_daily",
          "charging_attempts", "charger_status_daily", "silent_failures",
          "alerts", "faults", "status_intervals"})

# ---------------------------------------------------------------- 2
print("\n2. GUARDRAILS")
# Scan what the code DOES, not what its comments say. A substring search trips
# over the very docstrings that promise the guardrail, which is worse than no
# check at all: it fails while the code is correct.
imports: set[str] = set()
calls: set[str] = set()
literals: list[str] = []
for _f in ("tools.py", "cube_client.py", "main.py", "tool_schemas.py"):
    _tree = ast.parse((ROOT / "api" / _f).read_text(encoding="utf-8"))
    for _n in ast.walk(_tree):
        if isinstance(_n, ast.Import):
            imports.update(a.name.split(".")[0] for a in _n.names)
        elif isinstance(_n, ast.ImportFrom) and _n.module:
            imports.add(_n.module.split(".")[0])
        elif isinstance(_n, ast.Call) and isinstance(_n.func, ast.Name):
            calls.add(_n.func.id)
        elif isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute):
            calls.add(_n.func.attr)
        elif isinstance(_n, ast.Constant) and isinstance(_n.value, str):
            literals.append(_n.value)

check("no tool opens a file",
      not ({"open", "read_text", "read_bytes"} & calls),
      sorted({"open", "read_text", "read_bytes"} & calls))
check("no tool imports a database or file reader",
      not (imports & {"duckdb", "csv", "sqlite3", "pandas", "pyarrow"}),
      sorted(imports & {"duckdb", "csv", "sqlite3", "pandas", "pyarrow"}))
check("no tool names a data file",
      not any(v.endswith((".csv", ".parquet", ".duckdb")) for v in literals))
check("no tool references the simulator ground truth",
      not any("_manifest" in v or "ground_truth" in v for v in literals))
check("no tool writes SQL",
      not any(v.lstrip().upper().startswith(("SELECT ", "WITH ")) for v in literals))
check("every schema maps to a real function",
      {s["name"] for s in TOOL_SCHEMAS} == set(T.REGISTRY))
check("every tool signature matches its schema",
      all(set(s["input_schema"].get("required", []))
          <= set(inspect.signature(T.REGISTRY[s["name"]]).parameters)
          for s in TOOL_SCHEMAS))

# ---------------------------------------------------------------- 3
print("\n3. RESPONSE SHAPE")
ENVELOPE = {"tool", "source_layer", "as_of", "period", "metrics",
            "metric_definitions", "assumptions", "evidence", "caveats"}
samples = {
    "get_reported_reliability": {},
    "get_delivered_reliability": {},
    "get_gap_anatomy": {},
    "get_alert_blindspot": {},
    "find_silent_failures": {"limit": 5},
    "get_charger_evidence": {"charger_id": "CHG-0034"},
    "estimate_commercial_impact": {"charger_id": "CHG-0034"},
    "draft_ops_action": {"charger_id": "CHG-0034"},
}
results = {}
for name, args in samples.items():
    try:
        r = T.REGISTRY[name](**args)
        results[name] = r
        ok = ENVELOPE <= set(r) and r["tool"] == name and bool(r["metrics"])
        check("{} returns the standard envelope".format(name), ok)
        json.dumps(r)
    except Exception as exc:                                   # noqa: BLE001
        check("{} returns the standard envelope".format(name), False, str(exc)[:160])

check("tools publish Cube's own metric definitions",
      any("percentage points" in d or "share of" in d.lower()
          for d in results["get_gap_anatomy"]["metric_definitions"].values()))
check("estimates are always labelled as estimates",
      all(any("prioritisation" in c for c in results[n]["caveats"])
          for n in ("get_alert_blindspot", "find_silent_failures",
                    "estimate_commercial_impact")))

# ---------------------------------------------------------------- 4
print("\n4. HERO QUESTION, ANSWERED FROM TOOL OUTPUT ONLY")
gap = results["get_gap_anatomy"]["metrics"]
blind = results["get_alert_blindspot"]["metrics"]
port = results["find_silent_failures"]["metrics"]["chargers"]
ev = results["get_charger_evidence"]["metrics"]
im = results["estimate_commercial_impact"]["metrics"]
act = results["draft_ops_action"]["metrics"]

check("reported reliability is above delivered",
      gap["reported_reliability_percent"] > gap["delivered_reliability_percent"])
check("gap reconciles with the failure-rate decomposition",
      abs((gap["delivered_failure_rate_points"] - gap["reported_downtime_points"])
          - gap["reliability_gap_points"]) <= 0.02)
check("failure-rate split adds up",
      abs((gap["failure_points_from_detected_devices"]
           + gap["failure_points_baseline_diffuse"])
          - gap["delivered_failure_rate_points"]) <= 0.02)
check("no detected silent failure is already in the alert queue",
      blind["alert_silent_overlap"] == 0)
check("the alert queue is not empty (the point is misdirection, not absence)",
      blind["visible_alert_count"] > 0)
check("CHG-0034 is the first dispatch recommendation",
      port and port[0]["charger_id"] == "CHG-0034"
      and port[0]["dispatch_priority"] == 1)
check("hero evidence is scoped to the failure episode",
      ev["window_scope"] == "detected failure episode")
check("hero delivered nothing during the episode",
      ev["kwh_delivered"] == 0 and ev["successful_sessions"] == 0
      and ev["attempted_sessions"] > 20)
check("hero was working earlier in the period",
      ev["full_period_context"]["successful_sessions"] > 0)
check("hero looks healthy to status reporting",
      ev["reported_uptime_percent"] >= 99.9 and ev["fault_events"] == 0
      and ev["alerts_raised"] == 0)
check("hero never reached Charging in the episode",
      not any(s["status"] == "Charging" for s in
              results["get_charger_evidence"]["evidence"]["status_timeline"]))
check("site peers were healthy over the same window",
      ev["peer_comparison"]["peer_delivered_success_percent"] > 88)
check("impact is costed and still running",
      im["this_charger"]["observed_revenue_at_risk_gbp"] > 0 and im["is_ongoing"])
check("run-rate projects only ongoing episodes",
      "still running" in im["run_rate_estimate"]["basis"])
check("the drafted action is not sent",
      act["requires_human_approval"] is True and act["sent"] is False)
check("the draft carries the evidence, not just a conclusion",
      "Reported uptime" in act["body"] and "Delivered success rate" in act["body"])

# ---------------------------------------------------------------- summary
print("\n" + "=" * 66)
print("  TOOL LAYER  ·  answering \"what is our reliability this month?\"")
print("=" * 66)
p = lambda k, v: print("  {:<34} {}".format(k, v))
p("reported reliability", "{}%".format(gap["reported_reliability_percent"]))
p("delivered reliability", "{}%".format(gap["delivered_reliability_percent"]))
p("gap", "{} points".format(gap["reliability_gap_points"]))
p("  of which actionable", "{} points".format(gap["failure_points_from_detected_devices"]))
p("  of which baseline noise", "{} points".format(gap["failure_points_baseline_diffuse"]))
print()
p("alerts in the ops queue", "{} across {} devices".format(
    blind["visible_alert_count"], blind["visible_alert_device_count"]))
p("silent failures detected", blind["silent_failure_count"])
p("overlap between the two", blind["alert_silent_overlap"])
p("still failing right now", blind["still_failing_now"])
p("hidden revenue at risk", "GBP {:,.2f}".format(blind["hidden_revenue_at_risk_gbp"]))
print()
p("first dispatch", "{}  {}".format(port[0]["charger_id"], port[0]["station_name"]))
p("  episode", "{} to {} ({} days, ongoing)".format(
    ev["detection"]["observed_start"], ev["detection"]["observed_end"],
    im["observed_window"]["days"]))
p("  attempts / successful", "{} / {}".format(
    ev["attempted_sessions"], ev["successful_sessions"]))
p("  energy delivered", "{} kWh".format(ev["kwh_delivered"]))
p("  reported uptime", "{}%".format(ev["reported_uptime_percent"]))
p("  faults / alerts", "{} / {}".format(ev["fault_events"], ev["alerts_raised"]))
p("  same-site peers", "{}% success".format(
    ev["peer_comparison"]["peer_delivered_success_percent"]))
p("  revenue at risk", "GBP {:,.2f}  ({:,.2f}/day)".format(
    im["this_charger"]["observed_revenue_at_risk_gbp"],
    im["this_charger"]["daily_loss_rate_gbp"]))
p("  drafted action", "{}  (requires approval, not sent)".format(act["channel"]))
print("=" * 66)

print("\n" + ("All checks passed.\n" if not fails
             else "{} CHECKS FAILED\n".format(len(fails))))
sys.exit(1 if fails else 0)
