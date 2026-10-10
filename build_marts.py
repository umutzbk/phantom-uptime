"""
Transformation layer: operational (OLTP-like) -> analytical (OLAP).
===================================================================
Reads raw/*.csv, writes marts/voltway.duckdb plus CSV copies in marts/.

Cube sits on the analytical layer produced here. Neither Cube nor the agent
ever reads raw/.

The important part is that silent failures are DETECTED from the data, not
read from the simulator's ground truth. The manifest's planted list is used
only by validate_marts.py to score the detector afterwards. If the detection
logic here were replaced by a lookup, the demo would be theatre.

Run:  python build_marts.py
"""

import json
from types import SimpleNamespace
from pathlib import Path

import duckdb

ROOT = Path(__file__).parent
RAW = ROOT / "raw"
MARTS = ROOT / "marts"
MARTS.mkdir(parents=True, exist_ok=True)
DB = MARTS / "voltway.duckdb"

# Detection thresholds. Deliberately explicit and few - a reviewer has to be
# able to argue with them.
MIN_ATTEMPTS = 12          # below this, a low success rate is just noise
MIN_FAILURES = 8           # a shortfall this large is not baseline variation
SUCCESS_GAP = 0.20         # how far below its site peers a charger must fall
UPTIME_FLOOR = 0.98        # only flag units that status reporting calls healthy
POWER_RATIO_FLOOR = 0.55   # delivered power vs peers, for throughput failures
MIN_LOW_POWER_DAYS = 3     # a sustained throughput shortfall, not one bad session

STEPS = []


def step(name, sql):
    STEPS.append((name, sql))


# ---------------------------------------------------------------- staging
step("stg_status_events", """
CREATE OR REPLACE TABLE stg_status_events AS
SELECT event_id, charger_id, NULLIF(connector_id, '') AS connector_id,
       CAST(event_ts AS TIMESTAMP) AS event_ts,
       status, NULLIF(error_code, '') AS error_code
FROM read_csv_auto('{raw}/raw_ocpp_status_events.csv', header=true, all_varchar=true);
""")

step("stg_attempts", """
CREATE OR REPLACE TABLE stg_attempts AS
SELECT a.transaction_id, a.charger_id, a.connector_id, a.customer_id,
       CAST(a.start_ts AS TIMESTAMP) AS start_ts,
       CAST(a.stop_ts  AS TIMESTAMP) AS stop_ts,
       (CAST(a.meter_stop_wh AS BIGINT) - CAST(a.meter_start_wh AS BIGINT))
         / 1000.0 AS energy_kwh,
       a.stop_reason, a.auth_method, a.tariff_code,
       CAST(a.unit_price_gbp_per_kwh AS DOUBLE) AS unit_price_gbp_per_kwh,
       p.status AS payment_status, NULLIF(p.decline_code, '') AS decline_code
FROM read_csv_auto('{raw}/raw_charging_attempts.csv', header=true, all_varchar=true) a
LEFT JOIN read_csv_auto('{raw}/raw_payment_transactions.csv', header=true, all_varchar=true) p
       ON p.transaction_id = a.transaction_id;
""")

# ---------------------------------------------------------------- dimensions
step("dim_station", """
CREATE OR REPLACE TABLE dim_station AS
SELECT station_id, station_name, city, region, location_type,
       partner_location_type,
       CAST(latitude AS DOUBLE) AS latitude, CAST(longitude AS DOUBLE) AS longitude,
       CAST(num_chargers AS INTEGER) AS num_chargers,
       is_24_7 = 'Y' AS is_24_7, CAST(commissioned_date AS DATE) AS commissioned_date
FROM read_csv_auto('{raw}/raw_station_inventory.csv', header=true, all_varchar=true);
""")

step("dim_charger", """
CREATE OR REPLACE TABLE dim_charger AS
SELECT c.charger_id, c.station_id, s.station_name, s.city, s.region,
       s.location_type, s.partner_location_type,
       c.model, CAST(c.max_power_kw AS INTEGER) AS max_power_kw,
       CAST(c.connector_count AS INTEGER) AS connector_count,
       CAST(c.installed_date AS DATE) AS installed_date, c.firmware_version
FROM read_csv_auto('{raw}/raw_charger_inventory.csv', header=true, all_varchar=true) c
JOIN dim_station s USING (station_id);
""")

step("dim_connector", """
CREATE OR REPLACE TABLE dim_connector AS
SELECT n.connector_id, n.charger_id, c.station_id,
       CAST(n.connector_ordinal AS INTEGER) AS connector_ordinal,
       n.connector_type, CAST(n.max_power_kw AS INTEGER) AS max_power_kw
FROM read_csv_auto('{raw}/raw_connector_inventory.csv', header=true, all_varchar=true) n
JOIN dim_charger c USING (charger_id);
""")

step("dim_customer", """
CREATE OR REPLACE TABLE dim_customer AS
SELECT customer_id, segment, home_region,
       CAST(signup_date AS DATE) AS signup_date, is_subscriber = 'Y' AS is_subscriber
FROM read_csv_auto('{raw}/raw_customer_accounts.csv', header=true, all_varchar=true);
""")

# ---------------------------------------------------------------- facts
step("fact_charging_attempt", """
CREATE OR REPLACE TABLE fact_charging_attempt AS
SELECT
  a.transaction_id AS attempt_id,
  a.charger_id, a.connector_id, a.customer_id,
  c.station_id, c.station_name, c.city, c.region, c.location_type,
  c.max_power_kw, cu.segment AS customer_segment,
  a.start_ts, a.stop_ts,
  CAST(a.start_ts AS DATE) AS attempt_date,
  EXTRACT(hour FROM a.start_ts) AS hour_of_day,
  CASE WHEN EXTRACT(dow FROM a.start_ts) IN (0, 6) THEN 'weekend' ELSE 'weekday' END AS day_type,
  DATEDIFF('minute', a.start_ts, a.stop_ts) AS duration_min,
  a.energy_kwh, a.unit_price_gbp_per_kwh, a.tariff_code, a.auth_method,
  a.stop_reason, a.payment_status, a.decline_code,
  a.energy_kwh > 0.6 AS is_successful,
  -- outcome is derived here, not carried from the source
  CASE
    WHEN a.energy_kwh > 0.6                            THEN 'success'
    WHEN a.payment_status = 'declined'                 THEN 'payment_failed'
    WHEN a.energy_kwh > 0
     AND DATEDIFF('minute', a.start_ts, a.stop_ts) < 5 THEN 'aborted_short'
    ELSE 'zero_energy'
  END AS outcome,
  CASE WHEN a.energy_kwh > 0.6
       THEN ROUND(a.energy_kwh * a.unit_price_gbp_per_kwh, 2) ELSE 0 END AS revenue_gbp,
  CASE WHEN a.energy_kwh > 0 AND DATEDIFF('minute', a.start_ts, a.stop_ts) > 0
       THEN ROUND(a.energy_kwh / (DATEDIFF('minute', a.start_ts, a.stop_ts) / 60.0), 1)
       ELSE 0 END AS avg_power_kw
FROM stg_attempts a
JOIN dim_charger c USING (charger_id)
JOIN dim_customer cu USING (customer_id);
""")

# Status intervals: OCPP reports on change, so an interval runs from one event
# to the next event on that charger.
step("fact_status_interval", """
CREATE OR REPLACE TABLE fact_status_interval AS
WITH ordered AS (
  SELECT charger_id, event_ts, status, error_code,
         LEAD(event_ts) OVER (PARTITION BY charger_id ORDER BY event_ts, event_id)
           AS next_ts
  FROM stg_status_events
)
SELECT
  o.charger_id, c.station_id, c.region, c.location_type,
  o.status, o.error_code,
  o.event_ts AS interval_start,
  COALESCE(o.next_ts, TIMESTAMP '2026-09-05 00:00:00') AS interval_end,
  DATEDIFF('second',
           o.event_ts,
           COALESCE(o.next_ts, TIMESTAMP '2026-09-05 00:00:00')) / 60.0 AS minutes,
  -- Reported uptime in this prototype is time spent in a non-fault operational
  -- status. That is the modelling choice, not a claim about how any specific
  -- regulation scores availability. The consequence is what matters: a charger
  -- cycling Available -> Preparing -> Available never leaves an operational
  -- status, so status-derived reporting sees nothing wrong with it.
  o.status NOT IN ('Faulted', 'Unavailable') AS counts_as_uptime
FROM ordered o
JOIN dim_charger c ON c.charger_id = o.charger_id
WHERE COALESCE(o.next_ts, TIMESTAMP '2026-09-05 00:00:00') > o.event_ts;
""")

step("fact_fault", """
CREATE OR REPLACE TABLE fact_fault AS
SELECT f.fault_id, f.charger_id, f.station_id,
       CAST(f.opened_ts AS TIMESTAMP) AS opened_ts,
       TRY_CAST(NULLIF(f.closed_ts, '') AS TIMESTAMP) AS closed_ts,
       f.fault_code, f.severity_raw,
       DATEDIFF('minute', CAST(f.opened_ts AS TIMESTAMP),
                COALESCE(TRY_CAST(NULLIF(f.closed_ts, '') AS TIMESTAMP),
                         TIMESTAMP '2026-09-05 00:00:00')) AS duration_min,
       f.closed_ts = '' AS is_open
FROM read_csv_auto('{raw}/raw_fault_events.csv', header=true, all_varchar=true) f;
""")

step("fact_alert", """
CREATE OR REPLACE TABLE fact_alert AS
SELECT a.alert_id, a.charger_id, a.station_id, c.station_name, c.city, c.region,
       CAST(a.created_ts AS TIMESTAMP) AS created_ts,
       a.rule_id, a.severity, a.message, a.status, a.source_fault_id
FROM read_csv_auto('{raw}/raw_alert_events.csv', header=true, all_varchar=true) a
JOIN dim_charger c USING (charger_id);
""")

# ---------------------------------------------------------------- aggregates
step("agg_charger_status_daily", """
CREATE OR REPLACE TABLE agg_charger_status_daily AS
WITH days AS (
  SELECT UNNEST(GENERATE_SERIES(DATE '2026-08-06', DATE '2026-09-04', INTERVAL 1 DAY))::DATE AS d
),
grid AS (SELECT c.charger_id, c.station_id, d.d AS status_date FROM dim_charger c CROSS JOIN days d),
sliced AS (
  SELECT g.charger_id, g.station_id, g.status_date, i.status, i.counts_as_uptime,
         DATEDIFF('second',
                  GREATEST(i.interval_start, g.status_date::TIMESTAMP),
                  LEAST(i.interval_end, g.status_date::TIMESTAMP + INTERVAL 1 DAY)) / 60.0
           AS minutes
  FROM grid g
  JOIN fact_status_interval i
    ON i.charger_id = g.charger_id
   AND i.interval_start < g.status_date::TIMESTAMP + INTERVAL 1 DAY
   AND i.interval_end   > g.status_date::TIMESTAMP
)
SELECT charger_id, station_id, status_date,
       ROUND(SUM(CASE WHEN status = 'Available' THEN minutes ELSE 0 END), 1) AS available_minutes,
       ROUND(SUM(CASE WHEN status = 'Preparing' THEN minutes ELSE 0 END), 1) AS preparing_minutes,
       ROUND(SUM(CASE WHEN status = 'Charging'  THEN minutes ELSE 0 END), 1) AS charging_minutes,
       ROUND(SUM(CASE WHEN status = 'Faulted'   THEN minutes ELSE 0 END), 1) AS faulted_minutes,
       ROUND(SUM(CASE WHEN status = 'Unavailable' THEN minutes ELSE 0 END), 1) AS offline_minutes,
       ROUND(SUM(minutes), 1) AS total_minutes,
       ROUND(SUM(CASE WHEN counts_as_uptime THEN minutes ELSE 0 END), 1) AS uptime_minutes,
       ROUND(100.0 * SUM(CASE WHEN counts_as_uptime THEN minutes ELSE 0 END)
             / NULLIF(SUM(minutes), 0), 3) AS reported_uptime_pct
FROM sliced
GROUP BY 1, 2, 3;
""")

step("agg_reliability_daily", """
CREATE OR REPLACE TABLE agg_reliability_daily AS
WITH rep AS (
  SELECT s.status_date AS reliability_date, c.station_id, c.region, c.city,
         SUM(s.uptime_minutes) AS uptime_minutes, SUM(s.total_minutes) AS total_minutes
  FROM agg_charger_status_daily s JOIN dim_charger c USING (charger_id)
  GROUP BY 1, 2, 3, 4
),
dlv AS (
  SELECT attempt_date AS reliability_date, station_id,
         COUNT(*) AS attempted_sessions,
         SUM(CASE WHEN is_successful THEN 1 ELSE 0 END) AS successful_sessions,
         SUM(CASE WHEN is_successful THEN 0 ELSE 1 END) AS failed_sessions,
         ROUND(SUM(energy_kwh), 2) AS kwh_delivered,
         SUM(CASE WHEN energy_kwh = 0 THEN 1 ELSE 0 END) AS zero_kwh_sessions
  FROM fact_charging_attempt GROUP BY 1, 2
)
SELECT r.reliability_date, r.station_id, r.region, r.city,
       r.uptime_minutes, r.total_minutes,
       ROUND(100.0 * r.uptime_minutes / NULLIF(r.total_minutes, 0), 3)
         AS reported_reliability_pct,
       COALESCE(d.attempted_sessions, 0) AS attempted_sessions,
       COALESCE(d.successful_sessions, 0) AS successful_sessions,
       COALESCE(d.failed_sessions, 0) AS failed_sessions,
       COALESCE(d.kwh_delivered, 0) AS kwh_delivered,
       COALESCE(d.zero_kwh_sessions, 0) AS zero_kwh_sessions,
       ROUND(100.0 * d.successful_sessions / NULLIF(d.attempted_sessions, 0), 3)
         AS delivered_reliability_pct,
       ROUND(100.0 * r.uptime_minutes / NULLIF(r.total_minutes, 0)
             - 100.0 * d.successful_sessions / NULLIF(d.attempted_sessions, 0), 3)
         AS reliability_gap_points
FROM rep r LEFT JOIN dlv d USING (reliability_date, station_id);
""")

# ---------------------------------------------------------------- detection
step("mart_silent_failures", """
CREATE OR REPLACE TABLE mart_silent_failures AS
-- An impairment is an EPISODE, not a month. Aggregating a three-day failure
-- over thirty days dilutes it below every threshold, and defining the episode
-- as min/max of all failed attempts stretches it across the whole month
-- because baseline failures happen everywhere. So the episode is built from
-- days where this charger clearly misbehaved, and every statistic below is
-- measured inside that window.
WITH bad_days AS (
  SELECT charger_id, attempt_date
  FROM fact_charging_attempt
  GROUP BY 1, 2
  HAVING SUM(CASE WHEN is_successful THEN 0 ELSE 1 END) >= 2
     AND SUM(CASE WHEN is_successful THEN 0 ELSE 1 END) * 1.0 / COUNT(*) >= 0.30
),
slow_days AS (
  SELECT charger_id, attempt_date
  FROM fact_charging_attempt
  WHERE is_successful
  GROUP BY 1, 2
  HAVING AVG(avg_power_kw) < 0.35 * MAX(max_power_kw)
),
episode AS (
  SELECT charger_id, MIN(attempt_date) AS episode_start, MAX(attempt_date) AS episode_end,
         COUNT(*) AS bad_day_count,
         DATEDIFF('day', MIN(attempt_date), MAX(attempt_date)) + 1 AS episode_days,
         'failure' AS episode_kind
  FROM bad_days GROUP BY 1
  UNION ALL
  SELECT charger_id, MIN(attempt_date), MAX(attempt_date), COUNT(*),
         DATEDIFF('day', MIN(attempt_date), MAX(attempt_date)) + 1, 'throughput'
  FROM slow_days GROUP BY 1
  HAVING COUNT(*) >= {min_lp_days}
),
-- one episode per charger: the more severe of the two tracks
ep AS (
  SELECT * FROM (
    SELECT *, ROW_NUMBER() OVER (
      PARTITION BY charger_id
      ORDER BY CASE WHEN episode_kind = 'failure' THEN 0 ELSE 1 END, bad_day_count DESC) AS rn
    FROM episode) WHERE rn = 1
),
in_window AS (
  SELECT f.*, ep.episode_start, ep.episode_end, ep.episode_days, ep.episode_kind
  FROM fact_charging_attempt f JOIN ep USING (charger_id)
  WHERE f.attempt_date BETWEEN ep.episode_start AND ep.episode_end
),
subject AS (
  SELECT charger_id, station_id, station_name, city, region, location_type,
         MAX(max_power_kw) AS max_power_kw,
         MAX(episode_start) AS episode_start, MAX(episode_end) AS episode_end,
         MAX(episode_days) AS episode_days, MAX(episode_kind) AS episode_kind,
         COUNT(*) AS attempted_sessions,
         SUM(CASE WHEN is_successful THEN 1 ELSE 0 END) AS successful_sessions,
         SUM(CASE WHEN is_successful THEN 0 ELSE 1 END) AS failed_sessions,
         SUM(CASE WHEN energy_kwh = 0 THEN 1 ELSE 0 END) AS zero_kwh_sessions,
         SUM(energy_kwh) AS energy_kwh,
         AVG(CASE WHEN is_successful THEN energy_kwh END) AS avg_kwh_per_session,
         AVG(CASE WHEN is_successful THEN avg_power_kw END) AS avg_power_kw,
         AVG(unit_price_gbp_per_kwh) AS avg_tariff
  FROM in_window GROUP BY 1, 2, 3, 4, 5, 6
),
-- same-site peers measured over the SAME window, so the comparison is fair
peer AS (
  SELECT s.charger_id,
         COUNT(DISTINCT f.charger_id) AS peer_chargers,
         COUNT(*) AS peer_attempts,
         SUM(CASE WHEN f.is_successful THEN 1 ELSE 0 END) AS peer_successes,
         SUM(CASE WHEN f.is_successful THEN 1 ELSE 0 END) * 1.0
           / NULLIF(COUNT(DISTINCT f.charger_id), 0) AS peer_avg_successful,
         AVG(CASE WHEN f.is_successful THEN f.energy_kwh END) AS peer_avg_kwh,
         AVG(CASE WHEN f.is_successful THEN f.avg_power_kw END) AS peer_avg_power
  FROM subject s
  JOIN fact_charging_attempt f
    ON f.station_id = s.station_id AND f.charger_id <> s.charger_id
   AND f.attempt_date BETWEEN s.episode_start AND s.episode_end
  GROUP BY 1
),
uptime AS (
  SELECT s.charger_id,
         SUM(a.uptime_minutes) / NULLIF(SUM(a.total_minutes), 0) AS uptime_ratio
  FROM subject s JOIN agg_charger_status_daily a
    ON a.charger_id = s.charger_id
   AND a.status_date BETWEEN s.episode_start AND s.episode_end
  GROUP BY 1
),
-- a charger whose fault log already covers the episode is not silent: the
-- operator can already see it
fault_cover AS (
  SELECT DISTINCT s.charger_id FROM subject s JOIN fact_fault f
    ON f.charger_id = s.charger_id
   AND f.opened_ts < s.episode_end::TIMESTAMP + INTERVAL 1 DAY
   AND COALESCE(f.closed_ts, TIMESTAMP '2026-09-05 00:00:00') > s.episode_start::TIMESTAMP
),
conn_conc AS (
  SELECT charger_id, MAX(share) AS connector_failure_share FROM (
    SELECT charger_id, connector_id,
           COUNT(*) * 1.0 / SUM(COUNT(*)) OVER (PARTITION BY charger_id) AS share
    FROM in_window WHERE NOT is_successful GROUP BY 1, 2) t GROUP BY 1
),
auth_conc AS (
  SELECT charger_id, MAX(share) AS auth_failure_share FROM (
    SELECT charger_id, auth_method,
           COUNT(*) * 1.0 / SUM(COUNT(*)) OVER (PARTITION BY charger_id) AS share
    FROM in_window WHERE NOT is_successful GROUP BY 1, 2) t GROUP BY 1
),
short_conc AS (
  SELECT charger_id,
         SUM(CASE WHEN outcome = 'aborted_short' THEN 1 ELSE 0 END) * 1.0
           / NULLIF(COUNT(*), 0) AS short_share
  FROM in_window WHERE NOT is_successful GROUP BY 1
),
scored AS (
  SELECT s.*, c.connector_count,
         u.uptime_ratio, p.peer_chargers, p.peer_avg_kwh, p.peer_avg_power,
         p.peer_avg_successful,
         p.peer_successes * 1.0 / NULLIF(p.peer_attempts, 0) AS peer_success_rate,
         s.successful_sessions * 1.0 / NULLIF(s.attempted_sessions, 0) AS success_rate,
         s.avg_power_kw / NULLIF(p.peer_avg_power, 0) AS power_ratio,
         cc.connector_failure_share, ac.auth_failure_share, sc.short_share
  FROM subject s
  JOIN dim_charger c USING (charger_id)
  LEFT JOIN peer p USING (charger_id)
  LEFT JOIN uptime u USING (charger_id)
  LEFT JOIN conn_conc cc USING (charger_id)
  LEFT JOIN auth_conc ac USING (charger_id)
  LEFT JOIN short_conc sc USING (charger_id)
),
flagged AS (
  SELECT *,
    CASE
      WHEN success_rate = 0 AND energy_kwh = 0 THEN 'silent_total'
      WHEN episode_kind = 'throughput' AND power_ratio < {power_floor} THEN 'low_power'
      WHEN connector_failure_share >= 0.80 AND connector_count > 1
        THEN 'connector_fault_partial'
      WHEN auth_failure_share >= 0.70 THEN 'payment_terminal'
      WHEN short_share >= 0.50 THEN 'aborted_short'
      ELSE 'degraded_unclassified'
    END AS failure_pattern
  FROM scored
  WHERE peer_chargers >= 1
    AND uptime_ratio >= {uptime_floor}
    AND attempted_sessions >= {min_attempts}
    AND charger_id NOT IN (SELECT charger_id FROM fault_cover)
    AND (
         (episode_kind = 'failure'
          AND success_rate <= peer_success_rate - {success_gap}
          AND failed_sessions >= {min_failures})
      OR (episode_kind = 'throughput' AND power_ratio < {power_floor})
    )
),
costed AS (
  SELECT f.*,
    CASE WHEN failure_pattern = 'low_power' THEN 0
         ELSE GREATEST(0, ROUND(peer_avg_successful - successful_sessions, 1))
    END AS estimated_lost_sessions,
    CASE WHEN failure_pattern = 'low_power'
         THEN GREATEST(0, ROUND((peer_avg_kwh - avg_kwh_per_session)
                                * successful_sessions, 1))
         ELSE GREATEST(0, ROUND((peer_avg_successful - successful_sessions)
                                * peer_avg_kwh, 1))
    END AS estimated_lost_kwh
  FROM flagged f
)
SELECT
  charger_id, station_id, station_name, city, region, location_type, max_power_kw,
  failure_pattern,
  attempted_sessions, successful_sessions, failed_sessions, zero_kwh_sessions,
  ROUND(energy_kwh, 1) AS energy_kwh,
  ROUND(100.0 * uptime_ratio, 3) AS reported_uptime_percent,
  ROUND(100.0 * success_rate, 2) AS delivered_success_percent,
  ROUND(100.0 * peer_success_rate, 2) AS peer_success_percent,
  peer_chargers,
  episode_start AS observed_start, episode_end AS observed_end,
  episode_days AS window_days,
  estimated_lost_sessions, estimated_lost_kwh,
  ROUND(estimated_lost_kwh * avg_tariff, 2) AS estimated_revenue_at_risk_gbp,
  ROUND(estimated_lost_kwh * avg_tariff / NULLIF(episode_days, 0), 2)
    AS daily_loss_rate_gbp,
  ROUND(avg_power_kw, 1) AS avg_power_kw,
  ROUND(peer_avg_power, 1) AS peer_avg_power_kw,
  ROUND(avg_tariff, 4) AS avg_tariff,
  FALSE AS has_alert,
  -- triage: an episode still running at the end of the observed period is
  -- still costing money today. One that stopped is already sunk.
  episode_end >= DATE '2026-09-04' AS is_ongoing,
  DATEDIFF('day', episode_end, DATE '2026-09-04') AS days_since_last_evidence,
  CASE WHEN peer_chargers >= 3 AND attempted_sessions >= 25 THEN 'high'
       WHEN peer_chargers >= 2 THEN 'medium' ELSE 'low' END AS confidence,
  CASE failure_pattern
    WHEN 'silent_total' THEN 'Dispatch engineer - physical inspection, unit reports available but delivers nothing'
    WHEN 'connector_fault_partial' THEN 'Inspect the affected connector - unit appears available on the other outlet'
    WHEN 'payment_terminal' THEN 'Check payment terminal - one auth method is failing while others work'
    WHEN 'low_power' THEN 'Investigate power delivery - sessions succeed but well below rated output'
    WHEN 'aborted_short' THEN 'Investigate session drop-outs - sessions start then terminate early'
    ELSE 'Investigate - delivered performance is materially below same-site peers'
  END AS recommended_action,
  ROW_NUMBER() OVER (ORDER BY estimated_lost_kwh * avg_tariff DESC)
    AS rank_by_revenue_at_risk,
  ROW_NUMBER() OVER (ORDER BY estimated_lost_kwh * avg_tariff
                              / NULLIF(episode_days, 0) DESC)
    AS rank_by_daily_loss_rate,
  ROW_NUMBER() OVER (ORDER BY (episode_end >= DATE '2026-09-04') DESC,
                              estimated_lost_kwh * avg_tariff
                              / NULLIF(episode_days, 0) DESC)
    AS dispatch_priority
FROM costed
ORDER BY dispatch_priority;
""")

step("mart_alert_blindspot", """
CREATE OR REPLACE TABLE mart_alert_blindspot AS
SELECT
  (SELECT COUNT(*) FROM fact_alert) AS visible_alert_count,
  (SELECT COUNT(DISTINCT charger_id) FROM fact_alert) AS visible_alert_device_count,
  (SELECT COUNT(*) FROM mart_silent_failures) AS silent_failure_device_count,
  (SELECT COUNT(*) FROM mart_silent_failures s
    WHERE EXISTS (SELECT 1 FROM fact_alert a WHERE a.charger_id = s.charger_id))
    AS alert_silent_failure_overlap,
  (SELECT ROUND(SUM(estimated_revenue_at_risk_gbp), 2) FROM mart_silent_failures)
    AS hidden_revenue_at_risk_gbp,
  (SELECT ROUND(SUM(daily_loss_rate_gbp), 2) FROM mart_silent_failures)
    AS hidden_daily_loss_rate_gbp,
  DATE '2026-08-06' AS observed_period_start,
  DATE '2026-09-04' AS observed_period_end,
  (SELECT COUNT(*) FROM dim_charger) AS pilot_chargers,
  (SELECT COUNT(*) FROM dim_station) AS pilot_stations;
""")

step("mart_reliability_summary", """
CREATE OR REPLACE TABLE mart_reliability_summary AS
WITH rep AS (
  SELECT SUM(uptime_minutes) AS up, SUM(total_minutes) AS tot
  FROM agg_charger_status_daily
),
dlv AS (
  SELECT COUNT(*) AS attempted,
         SUM(CASE WHEN is_successful THEN 1 ELSE 0 END) AS successful,
         SUM(CASE WHEN is_successful THEN 0 ELSE 1 END) AS failed,
         ROUND(SUM(energy_kwh), 1) AS kwh,
         ROUND(SUM(revenue_gbp), 2) AS revenue
  FROM fact_charging_attempt
),
conc AS (
  SELECT COALESCE(SUM(failed_sessions), 0) AS concentrated_failed
  FROM mart_silent_failures
)
SELECT
  -- status-derived reliability: share of time in a non-fault operational state
  ROUND(100.0 * rep.up / rep.tot, 2) AS reported_reliability_percent,
  ROUND(100.0 - 100.0 * rep.up / rep.tot, 2) AS reported_downtime_points,
  -- driver-experienced reliability: share of attempts that delivered energy
  ROUND(100.0 * dlv.successful / dlv.attempted, 2) AS delivered_reliability_percent,
  ROUND(100.0 * dlv.failed / dlv.attempted, 2) AS delivered_failure_rate_points,
  -- the headline gap. NOTE: this is NOT the failure rate. The two differ by the
  -- downtime that status reporting already captures:
  --   gap = delivered_failure_rate - reported_downtime
  ROUND(100.0 * rep.up / rep.tot - 100.0 * dlv.successful / dlv.attempted, 2)
    AS reliability_gap_points,
  dlv.attempted AS attempted_sessions, dlv.successful AS successful_sessions,
  dlv.failed AS failed_sessions, dlv.kwh AS kwh_delivered, dlv.revenue AS revenue_gbp,
  -- anatomy OF THE DELIVERED FAILURE RATE, not of the gap
  conc.concentrated_failed AS failures_on_detected_devices,
  dlv.failed - conc.concentrated_failed AS failures_baseline_diffuse,
  ROUND(100.0 * conc.concentrated_failed / dlv.attempted, 2)
    AS failure_points_from_detected_devices,
  ROUND(100.0 * (dlv.failed - conc.concentrated_failed) / dlv.attempted, 2)
    AS failure_points_baseline_diffuse,
  DATE '2026-08-06' AS period_start, DATE '2026-09-04' AS period_end
FROM rep, dlv, conc;
""")

EXPORTS = ["dim_station", "dim_charger", "dim_connector", "dim_customer",
           "fact_charging_attempt", "fact_status_interval", "fact_fault", "fact_alert",
           "agg_charger_status_daily", "agg_reliability_daily",
           "mart_silent_failures", "mart_alert_blindspot", "mart_reliability_summary"]


def main():
    if DB.exists():
        DB.unlink()
    con = duckdb.connect(str(DB))
    print("\nTransforming operational source data -> analytical layer\n")
    for name, sql in STEPS:
        con.execute(sql.format(raw=RAW.as_posix(), min_attempts=MIN_ATTEMPTS,
                               min_failures=MIN_FAILURES, min_lp_days=MIN_LOW_POWER_DAYS,
                               success_gap=SUCCESS_GAP, uptime_floor=UPTIME_FLOOR,
                               power_floor=POWER_RATIO_FLOOR))
        n = con.execute("SELECT COUNT(*) FROM {}".format(name)).fetchone()[0]
        print("  {:34s} {:>9,} rows".format(name, n))

    # Parquet is what Cube reads. Cube runs its own in-memory DuckDB and queries
    # these files, so it never holds a lock on voltway.duckdb and this script can
    # rebuild the warehouse while Cube is running.
    for t in EXPORTS:
        con.execute("COPY {t} TO '{p}/{t}.parquet' (FORMAT PARQUET)".format(
            t=t, p=MARTS.as_posix()))
        con.execute("COPY {t} TO '{p}/{t}.csv' (HEADER, DELIMITER ',')".format(
            t=t, p=MARTS.as_posix()))

    # A plain row rather than a dataframe: fetchdf() pulls in pandas and numpy
    # for two summary lines, and that dependency was never declared. It worked
    # on a laptop that happened to have them and failed on a clean machine the
    # first time CI ran.
    def _row(sql):
        cur = con.execute(sql)
        names = [d[0] for d in cur.description]
        return SimpleNamespace(**dict(zip(names, cur.fetchone(), strict=True)))

    s = _row("SELECT * FROM mart_reliability_summary")
    b = _row("SELECT * FROM mart_alert_blindspot")
    (MARTS / "_summary.json").write_text(json.dumps({
        "reported_reliability_percent": float(s.reported_reliability_percent),
        "delivered_reliability_percent": float(s.delivered_reliability_percent),
        "reliability_gap_points": float(s.reliability_gap_points),
        "attempted_sessions": int(s.attempted_sessions),
        "successful_sessions": int(s.successful_sessions),
        "visible_alert_count": int(b.visible_alert_count),
        "visible_alert_device_count": int(b.visible_alert_device_count),
        "silent_failure_device_count": int(b.silent_failure_device_count),
        "alert_silent_failure_overlap": int(b.alert_silent_failure_overlap),
        "hidden_revenue_at_risk_gbp": float(b.hidden_revenue_at_risk_gbp),
        "detection_thresholds": {
            "min_attempts": MIN_ATTEMPTS, "min_failures": MIN_FAILURES,
            "success_gap": SUCCESS_GAP, "uptime_floor": UPTIME_FLOOR,
            "power_ratio_floor": POWER_RATIO_FLOOR,
            "min_low_power_days": MIN_LOW_POWER_DAYS,
            "excluded": "chargers whose fault log already covers the episode"},
    }, indent=2), encoding="utf-8")

    print("\n  reported {:.2f}%   delivered {:.2f}%   gap {:.2f} pts".format(
        s.reported_reliability_percent, s.delivered_reliability_percent,
        s.reliability_gap_points))
    print("  {} alerts on {} devices  ·  {} silent failures detected  ·  overlap {}".format(
        b.visible_alert_count, b.visible_alert_device_count,
        b.silent_failure_device_count, b.alert_silent_failure_overlap))
    print("  hidden revenue at risk: GBP {:,.0f}\n".format(b.hidden_revenue_at_risk_gbp))
    con.close()


if __name__ == "__main__":
    main()
