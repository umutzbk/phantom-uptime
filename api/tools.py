"""
The approved tool layer.

These eight functions are the ONLY things the copilot can do. Each one queries
Cube and returns structured JSON. There is no SQL here, no access to raw/, and
no hardcoded headline number - if Cube is down, a tool fails loudly rather than
answering from memory.

Every response carries the same envelope:
    metrics       the numbers
    definitions   pulled from Cube's own model, not restated here
    as_of         the latest date present in the data
    assumptions   what the number depends on
    evidence      the rows behind it, where that helps
    caveats       what the number is not
"""
from __future__ import annotations


from . import cube_client as cube

TOOL_SOURCE = "Cube semantic layer over analytical marts (no raw table access)"

PRIORITISATION_CAVEAT = (
    "This is an estimate for operational prioritisation, not an accounting-grade "
    "lost-revenue calculation."
)

_as_of: str | None = None


def data_as_of() -> str:
    """Latest date present in the analytical layer, from Cube."""
    global _as_of
    if _as_of is None:
        rows = cube.load({"measures": [], "dimensions": ["reliability_daily.reliability_date"],
                          "order": {"reliability_daily.reliability_date": "desc"}, "limit": 1})
        _as_of = (rows[0]["reliability_daily.reliability_date"][:10]
                  if rows else cube.PERIOD_END)
    return _as_of


def _envelope(tool: str, metrics: dict, used: list[str], *, start=None, end=None,
              region=None, assumptions=None, evidence=None, caveats=None) -> dict:
    return {
        "tool": tool,
        "source_layer": TOOL_SOURCE,
        "as_of": data_as_of(),
        "period": {"start": start or cube.PERIOD_START, "end": end or cube.PERIOD_END,
                   "region": region or "all pilot regions"},
        "metrics": metrics,
        "metric_definitions": cube.definitions(used),
        "assumptions": assumptions or [],
        "evidence": evidence or {},
        "caveats": caveats or [],
    }


def _region_filter(member: str, region: str | None) -> list[dict]:
    return [cube.eq(member, region)] if region else []


# ---------------------------------------------------------------- 1 & 2
def _reliability(start, end, region) -> dict:
    rows = cube.load({
        "measures": ["reliability_daily.reported_reliability_percent",
                     "reliability_daily.delivered_reliability_percent",
                     "reliability_daily.reliability_gap_points",
                     "reliability_daily.attempted_sessions",
                     "reliability_daily.successful_sessions",
                     "reliability_daily.failed_sessions",
                     "reliability_daily.uptime_minutes",
                     "reliability_daily.total_minutes"],
        "filters": cube.date_filter("reliability_daily.reliability_date", start, end)
                   + _region_filter("reliability_daily.region", region),
    })
    r = rows[0] if rows else {}
    return {k.split(".")[1]: cube.num(v) for k, v in r.items()}


def get_reported_reliability(date_range: dict | None = None, region: str | None = None) -> dict:
    """Status-derived reliability: share of charger-time in a non-fault state."""
    start, end = (date_range or {}).get("start"), (date_range or {}).get("end")
    m = _reliability(start, end, region)
    return _envelope(
        "get_reported_reliability",
        {"reported_reliability_percent": round(m.get("reported_reliability_percent", 0), 2),
         "uptime_minutes": m.get("uptime_minutes"),
         "total_minutes": m.get("total_minutes"),
         "reported_downtime_points": round(100 - m.get("reported_reliability_percent", 0), 2)},
        ["reported_reliability_percent", "uptime_minutes", "total_minutes"],
        start=start, end=end, region=region,
        assumptions=[
            "Uptime is time spent in a non-fault operational status "
            "(Available, Preparing, Charging).",
            "Weighted by minutes, so a busy day and a quiet day are not averaged equally.",
        ],
        caveats=[
            "This measures availability, not whether a driver could charge. "
            "A unit that reports itself operational but delivers no energy counts "
            "here as uptime.",
        ])


def get_delivered_reliability(date_range: dict | None = None, region: str | None = None) -> dict:
    """Driver-experienced reliability: share of attempts that delivered energy."""
    start, end = (date_range or {}).get("start"), (date_range or {}).get("end")
    m = _reliability(start, end, region)
    return _envelope(
        "get_delivered_reliability",
        {"delivered_reliability_percent": round(m.get("delivered_reliability_percent", 0), 2),
         "attempted_sessions": int(m.get("attempted_sessions", 0)),
         "successful_sessions": int(m.get("successful_sessions", 0)),
         "failed_sessions": int(m.get("failed_sessions", 0)),
         "delivered_failure_rate_points": round(
             100 - m.get("delivered_reliability_percent", 0), 2)},
        ["delivered_reliability_percent", "attempted_sessions", "successful_sessions",
         "failed_sessions"],
        start=start, end=end, region=region,
        assumptions=[
            "An attempt is one plug-in, successful or not.",
            "Success means meaningful energy was transferred, not that a "
            "transaction record exists.",
        ],
        caveats=[
            "Measured per attempt, while reported reliability is measured per "
            "minute. The two are not the same unit.",
        ])


# ---------------------------------------------------------------- 3
def get_gap_anatomy(date_range: dict | None = None, region: str | None = None) -> dict:
    """Break the reported-vs-delivered gap into the part worth acting on."""
    start, end = (date_range or {}).get("start"), (date_range or {}).get("end")
    m = _reliability(start, end, region)
    sf = cube.load({
        "measures": ["silent_failures.silent_failure_count",
                     "silent_failures.failed_sessions",
                     "silent_failures.estimated_revenue_at_risk_gbp"],
        "filters": _region_filter("silent_failures.region", region),
    })
    s = sf[0] if sf else {}
    attempts = m.get("attempted_sessions", 0) or 1
    detected_failed = cube.num(s.get("silent_failures.failed_sessions"))
    total_failed = m.get("failed_sessions", 0)
    failure_rate = 100 - m.get("delivered_reliability_percent", 0)
    detected_pts = 100.0 * detected_failed / attempts
    return _envelope(
        "get_gap_anatomy",
        {"reported_reliability_percent": round(m.get("reported_reliability_percent", 0), 2),
         "delivered_reliability_percent": round(m.get("delivered_reliability_percent", 0), 2),
         "reliability_gap_points": round(m.get("reliability_gap_points", 0), 2),
         "delivered_failure_rate_points": round(failure_rate, 2),
         "reported_downtime_points": round(100 - m.get("reported_reliability_percent", 0), 2),
         "failure_points_from_detected_devices": round(detected_pts, 2),
         "failure_points_baseline_diffuse": round(failure_rate - detected_pts, 2),
         "failed_attempts_total": int(total_failed),
         "failed_attempts_on_detected_devices": int(detected_failed),
         "detected_device_count": int(cube.num(s.get("silent_failures.silent_failure_count"))),
         "revenue_at_risk_on_detected_devices_gbp": round(
             cube.num(s.get("silent_failures.estimated_revenue_at_risk_gbp")), 2)},
        ["reliability_gap_points", "delivered_failure_rate_points",
         "failure_points_from_detected_devices", "failure_points_baseline_diffuse"],
        start=start, end=end, region=region,
        assumptions=[
            "The gap equals the delivered failure rate minus the downtime that "
            "status reporting already captures.",
            "Failures on detected devices are treated as the actionable share; "
            "everything else is diffuse baseline failure.",
        ],
        caveats=[
            "The decomposition splits the FAILURE RATE, not the gap. Failure rate "
            "minus reported downtime equals the gap.",
            "Baseline failures - declines, handshake errors, drivers giving up - "
            "are not fixable by dispatching an engineer.",
        ])


# ---------------------------------------------------------------- 4
def get_alert_blindspot(date_range: dict | None = None, region: str | None = None) -> dict:
    """Compare what the existing alert queue sees with what detection found."""
    start, end = (date_range or {}).get("start"), (date_range or {}).get("end")
    rows = cube.load({
        "measures": ["alert_blindspot.visible_alert_count",
                     "alert_blindspot.visible_alert_device_count",
                     "alert_blindspot.silent_failure_count",
                     "alert_blindspot.alert_silent_overlap",
                     "alert_blindspot.hidden_revenue_at_risk",
                     "alert_blindspot.hidden_daily_loss_rate",
                     "alert_blindspot.pilot_chargers",
                     "alert_blindspot.pilot_stations"]})
    r = {k.split(".")[1]: cube.num(v) for k, v in (rows[0] if rows else {}).items()}
    ongoing = cube.load({"measures": ["silent_failures.ongoing_count",
                                      "silent_failures.ongoing_daily_loss_rate_gbp"],
                         "filters": _region_filter("silent_failures.region", region)})
    return _envelope(
        "get_alert_blindspot",
        {"visible_alert_count": int(r.get("visible_alert_count", 0)),
         "visible_alert_device_count": int(r.get("visible_alert_device_count", 0)),
         "silent_failure_count": int(r.get("silent_failure_count", 0)),
         "alert_silent_overlap": int(r.get("alert_silent_overlap", 0)),
         "still_failing_now": int(cube.num(
             (ongoing[0] if ongoing else {}).get("silent_failures.ongoing_count"))),
         "hidden_revenue_at_risk_gbp": round(r.get("hidden_revenue_at_risk", 0), 2),
         "hidden_daily_loss_rate_gbp": round(r.get("hidden_daily_loss_rate", 0), 2),
         # only episodes still running can honestly be projected forward
         "ongoing_daily_loss_rate_gbp": round(cube.num(
             (ongoing[0] if ongoing else {}).get(
                 "silent_failures.ongoing_daily_loss_rate_gbp")), 2),
         "pilot_chargers": int(r.get("pilot_chargers", 0)),
         "pilot_stations": int(r.get("pilot_stations", 0))},
        ["visible_alert_count", "silent_failure_count", "alert_silent_overlap",
         "hidden_revenue_at_risk"],
        start=start, end=end, region=region,
        assumptions=[
            "The existing alert rules watch the fault log, so they can only "
            "surface conditions that produced a fault.",
            "Detected silent failures are units whose delivered performance is "
            "materially below same-site peers while status reporting shows them "
            "operational.",
        ],
        caveats=[PRIORITISATION_CAVEAT])


# ---------------------------------------------------------------- 5
def find_silent_failures(date_range: dict | None = None, region: str | None = None,
                         limit: int = 10) -> dict:
    """Rank the detected hidden-failure portfolio by dispatch priority."""
    start, end = (date_range or {}).get("start"), (date_range or {}).get("end")
    dims = ["silent_failures.charger_id", "silent_failures.station_name",
            "silent_failures.city", "silent_failures.region",
            "silent_failures.failure_pattern", "silent_failures.confidence",
            "silent_failures.is_ongoing", "silent_failures.dispatch_priority",
            "silent_failures.window_days", "silent_failures.observed_start",
            "silent_failures.observed_end", "silent_failures.reported_uptime_percent",
            "silent_failures.delivered_success_percent",
            "silent_failures.peer_success_percent", "silent_failures.recommended_action"]
    rows = cube.load({
        "dimensions": dims,
        "measures": ["silent_failures.estimated_revenue_at_risk_gbp",
                     "silent_failures.daily_loss_rate_gbp",
                     "silent_failures.attempted_sessions",
                     "silent_failures.failed_sessions"],
        "filters": _region_filter("silent_failures.region", region),
        "order": {"silent_failures.dispatch_priority": "asc"},
        "limit": limit})
    out = []
    for r in rows:
        # r bound explicitly: harmless here because the lambda is used inside
        # the same iteration, and a habit worth keeping for the day it is not.
        g = lambda k, row=r: row.get("silent_failures." + k)   # noqa: E731
        out.append({
            "charger_id": g("charger_id"), "station_name": g("station_name"),
            "city": g("city"), "region": g("region"),
            "failure_pattern": g("failure_pattern"), "confidence": g("confidence"),
            "is_ongoing": g("is_ongoing") in (True, "true", "True"),
            "dispatch_priority": int(cube.num(g("dispatch_priority"))),
            "observed_start": str(g("observed_start"))[:10],
            "observed_end": str(g("observed_end"))[:10],
            "window_days": int(cube.num(g("window_days"))),
            "reported_uptime_percent": round(cube.num(g("reported_uptime_percent")), 2),
            "delivered_success_percent": round(cube.num(g("delivered_success_percent")), 2),
            "peer_success_percent": round(cube.num(g("peer_success_percent")), 2),
            "attempted_sessions": int(cube.num(g("attempted_sessions"))),
            "failed_sessions": int(cube.num(g("failed_sessions"))),
            "estimated_revenue_at_risk_gbp": round(
                cube.num(g("estimated_revenue_at_risk_gbp")), 2),
            "daily_loss_rate_gbp": round(cube.num(g("daily_loss_rate_gbp")), 2),
            "recommended_action": g("recommended_action"),
            "has_alert": False,
        })
    return _envelope(
        "find_silent_failures",
        {"returned": len(out), "ranking": "dispatch_priority", "chargers": out},
        ["silent_failure_count", "estimated_revenue_at_risk_gbp", "daily_loss_rate_gbp",
         "dispatch_priority"],
        start=start, end=end, region=region,
        assumptions=[
            "A charger is flagged only if its delivered success is materially "
            "below same-site peers over the same window while status reporting "
            "shows it operational.",
            "Dispatch priority puts episodes that are still running first, then "
            "ranks by daily loss rate - you dispatch on what is still bleeding, "
            "not on what has already been lost.",
        ],
        caveats=[
            PRIORITISATION_CAVEAT,
            "Detection is deliberately conservative. Short or low-traffic "
            "episodes fall below the thresholds and will be missed.",
        ])


# ---------------------------------------------------------------- 6
def _episode_window(charger_id: str) -> tuple[str | None, str | None]:
    rows = cube.load({
        "dimensions": ["silent_failures.observed_start", "silent_failures.observed_end"],
        "filters": [cube.eq("silent_failures.charger_id", charger_id)]})
    if not rows:
        return None, None
    return (str(rows[0]["silent_failures.observed_start"])[:10],
            str(rows[0]["silent_failures.observed_end"])[:10])


def _last_successful_charge(charger_id: str) -> str | None:
    rows = cube.load({
        "dimensions": ["charging_attempts.started_at"],
        "filters": [cube.eq("charging_attempts.charger_id", charger_id),
                    cube.eq("charging_attempts.outcome", "success")],
        "order": {"charging_attempts.started_at": "desc"},
        "limit": 1})
    if not rows:
        return None
    return str(rows[0]["charging_attempts.started_at"])[:19].replace("T", " ")


def _period_totals(charger_id: str, start, end) -> dict:
    r = cube.load({
        "measures": ["charging_attempts.attempted_sessions",
                     "charging_attempts.successful_sessions",
                     "charging_attempts.kwh_delivered",
                     "charging_attempts.delivered_reliability_percent"],
        "filters": [cube.eq("charging_attempts.charger_id", charger_id)]
                   + cube.date_filter("charging_attempts.attempt_date", start, end)})
    x = r[0] if r else {}
    return {"attempted_sessions": int(cube.num(x.get("charging_attempts.attempted_sessions"))),
            "successful_sessions": int(cube.num(x.get("charging_attempts.successful_sessions"))),
            "kwh_delivered": round(cube.num(x.get("charging_attempts.kwh_delivered")), 1),
            "delivered_success_percent": round(
                cube.num(x.get("charging_attempts.delivered_reliability_percent")), 2)}


def get_charger_evidence(charger_id: str, date_range: dict | None = None) -> dict:
    """Everything behind a single charger: attempts, status, peers, alerts.

    Defaults to the detected failure episode rather than the whole month. A
    three-day failure averaged over thirty days looks like a mildly poor
    charger, which is exactly the mistake this tool exists to prevent.
    """
    start, end = (date_range or {}).get("start"), (date_range or {}).get("end")
    scope = "explicit date range" if (start or end) else "full observed period"
    if not start and not end:
        ep_s, ep_e = _episode_window(charger_id)
        if ep_s:
            start, end, scope = ep_s, ep_e, "detected failure episode"
    dfil = cube.date_filter("charging_attempts.attempt_date", start, end)

    by_outcome = cube.load({
        "dimensions": ["charging_attempts.outcome"],
        "measures": ["charging_attempts.attempted_sessions",
                     "charging_attempts.kwh_delivered"],
        "filters": [cube.eq("charging_attempts.charger_id", charger_id)] + dfil})

    totals = cube.load({
        "dimensions": ["charging_attempts.station_id", "charging_attempts.station_name"],
        "measures": ["charging_attempts.attempted_sessions",
                     "charging_attempts.successful_sessions",
                     "charging_attempts.failed_sessions",
                     "charging_attempts.zero_kwh_sessions",
                     "charging_attempts.kwh_delivered",
                     "charging_attempts.revenue_gbp",
                     "charging_attempts.average_power_kw",
                     "charging_attempts.delivered_reliability_percent"],
        "filters": [cube.eq("charging_attempts.charger_id", charger_id)] + dfil})
    t = totals[0] if totals else {}
    station_id = t.get("charging_attempts.station_id")

    peers = (cube.load({
        "measures": ["charging_attempts.attempted_sessions",
                     "charging_attempts.successful_sessions",
                     "charging_attempts.delivered_reliability_percent",
                     "charging_attempts.average_power_kw"],
        "filters": [cube.eq("charging_attempts.station_id", station_id or ""),
                    {"member": "charging_attempts.charger_id",
                     "operator": "notEquals", "values": [charger_id]}] + dfil})
             if station_id else [])

    status = cube.load({
        "measures": ["charger_status_daily.available_minutes",
                     "charger_status_daily.preparing_minutes",
                     "charger_status_daily.charging_minutes",
                     "charger_status_daily.faulted_minutes",
                     "charger_status_daily.offline_minutes",
                     "charger_status_daily.reported_reliability_percent"],
        "filters": [cube.eq("charger_status_daily.charger_id", charger_id)]
                   + cube.date_filter("charger_status_daily.status_date", start, end)})

    timeline = cube.load({
        "dimensions": ["status_intervals.status"],
        "measures": ["status_intervals.interval_count", "status_intervals.minutes"],
        "filters": [cube.eq("status_intervals.charger_id", charger_id)]
                   + cube.date_filter("status_intervals.interval_start", start, end),
        "order": {"status_intervals.minutes": "desc"}})

    alerts = cube.load({"measures": ["alerts.alert_count"],
                        "filters": [cube.eq("alerts.charger_id", charger_id)]})
    faults = cube.load({"measures": ["faults.fault_count", "faults.faulted_minutes"],
                        "filters": [cube.eq("faults.charger_id", charger_id)]})

    flag = cube.load({
        "dimensions": ["silent_failures.failure_pattern", "silent_failures.is_ongoing",
                       "silent_failures.dispatch_priority", "silent_failures.confidence",
                       "silent_failures.recommended_action",
                       "silent_failures.observed_start", "silent_failures.observed_end"],
        "filters": [cube.eq("silent_failures.charger_id", charger_id)]})

    g = lambda r, k: cube.num(r.get(k))
    peer = peers[0] if peers else {}
    st = status[0] if status else {}
    f = flag[0] if flag else {}

    return _envelope(
        "get_charger_evidence",
        {"charger_id": charger_id,
         "window_scope": scope,
         "station_id": station_id, "station_name": t.get("charging_attempts.station_name"),
         "attempted_sessions": int(g(t, "charging_attempts.attempted_sessions")),
         "successful_sessions": int(g(t, "charging_attempts.successful_sessions")),
         "failed_sessions": int(g(t, "charging_attempts.failed_sessions")),
         "zero_kwh_sessions": int(g(t, "charging_attempts.zero_kwh_sessions")),
         "kwh_delivered": round(g(t, "charging_attempts.kwh_delivered"), 1),
         "revenue_gbp": round(g(t, "charging_attempts.revenue_gbp"), 2),
         "delivered_success_percent": round(
             g(t, "charging_attempts.delivered_reliability_percent"), 2),
         "avg_power_kw": round(g(t, "charging_attempts.average_power_kw"), 1),
         "reported_uptime_percent": round(
             g(st, "charger_status_daily.reported_reliability_percent"), 2),
         "fault_events": int(g(faults[0] if faults else {}, "faults.fault_count")),
         "alerts_raised": int(g(alerts[0] if alerts else {}, "alerts.alert_count")),
         "peer_comparison": {
             "peer_attempted_sessions": int(g(peer, "charging_attempts.attempted_sessions")),
             "peer_successful_sessions": int(g(peer, "charging_attempts.successful_sessions")),
             "peer_delivered_success_percent": round(
                 g(peer, "charging_attempts.delivered_reliability_percent"), 2),
             "peer_average_power_kw": round(g(peer, "charging_attempts.average_power_kw"), 1)},
         "status_minutes": {
             "available": g(st, "charger_status_daily.available_minutes"),
             "preparing": g(st, "charger_status_daily.preparing_minutes"),
             "charging": g(st, "charger_status_daily.charging_minutes"),
             "faulted": g(st, "charger_status_daily.faulted_minutes"),
             "offline": g(st, "charger_status_daily.offline_minutes")},
         "charging_sessions_started_in_window": int(
             g(t, "charging_attempts.successful_sessions")),
         "last_successful_charge_started": _last_successful_charge(charger_id),
         # Three cases, because a partial failure is not a silent one. Saying
         # "no Charging time recorded" on a unit that charged 26 times reads as
         # a broken screen, and it is the reading a sceptical viewer will catch.
         "charging_minutes_note": (
             "This unit did charge {} times inside the window. The failures are "
             "the remaining attempts, so treat the Charging time as partial "
             "service rather than healthy operation.".format(
                 int(g(t, "charging_attempts.successful_sessions")))
             if g(t, "charging_attempts.successful_sessions") > 0
             else "Any Charging minutes shown here are the tail of a session that "
                  "began before the episode started and ran across midnight. No "
                  "successful session started inside this window."
             if g(st, "charger_status_daily.charging_minutes") > 0
             else "No Charging time recorded in this window."),
         "episode_delivered_nothing": g(t, "charging_attempts.successful_sessions") == 0,
         "detection": {
             "flagged": bool(flag),
             "failure_pattern": f.get("silent_failures.failure_pattern"),
             "is_ongoing": f.get("silent_failures.is_ongoing") in (True, "true", "True"),
             "dispatch_priority": int(cube.num(f.get("silent_failures.dispatch_priority"))) or None,
             "confidence": f.get("silent_failures.confidence"),
             "observed_start": str(f.get("silent_failures.observed_start"))[:10],
             "observed_end": str(f.get("silent_failures.observed_end"))[:10],
             "recommended_action": f.get("silent_failures.recommended_action")},
         "full_period_context": _period_totals(
             charger_id, cube.PERIOD_START, cube.PERIOD_END),
         "full_period_window": {"start": cube.PERIOD_START, "end": cube.PERIOD_END}},
        ["delivered_reliability_percent", "reported_reliability_percent",
         "preparing_minutes", "alert_count", "fault_count",
         "zero_kwh_sessions", "failed_sessions", "attempted_sessions",
         "successful_sessions"],
        start=start, end=end,
        evidence={
            "attempts_by_outcome": [
                {"outcome": r["charging_attempts.outcome"],
                 "attempts": int(cube.num(r["charging_attempts.attempted_sessions"])),
                 "kwh": round(cube.num(r["charging_attempts.kwh_delivered"]), 1)}
                for r in by_outcome],
            "status_timeline": [
                {"status": r["status_intervals.status"],
                 "transitions": int(cube.num(r["status_intervals.interval_count"])),
                 "minutes": round(cube.num(r["status_intervals.minutes"]), 1)}
                for r in timeline],
            "timeline_note": "Counts only status changes that began inside the "
                             "window shown.",
            "outcome_note": (
                "attempts_by_outcome splits the attempts by outcome label. The "
                "zero_energy row is one of those labels, not the total that "
                "delivered nothing: use metrics.zero_kwh_sessions for that, "
                "which also counts payment failures and aborted sessions that "
                "transferred no energy.")},
        assumptions=[
            "Peer comparison uses the other chargers at the same station over the "
            "same window, so demand conditions are comparable.",
            "Headline figures cover the ACTIVE EPISODE only. "
            "full_period_context covers the whole observed month. The contrast "
            "between them is what shows the unit was working before it stopped "
            "- do not present them as the same window.",
        ],
        caveats=[
            "Absence of a fault or alert is evidence about the monitoring, not "
            "evidence that the charger is healthy.",
            "Two counts here are close but not the same: failed_sessions is "
            "every attempt that did not succeed, zero_kwh_sessions is every "
            "attempt that delivered no energy. A session can fail having "
            "delivered a little energy, so the second is the smaller number. "
            "Quote whichever you mean and name it.",
        ])


# ---------------------------------------------------------------- 7
def estimate_commercial_impact(charger_id: str, date_range: dict | None = None) -> dict:
    """Cost a detected failure, at charger, portfolio and run-rate level."""
    start, end = (date_range or {}).get("start"), (date_range or {}).get("end")
    rows = cube.load({
        "dimensions": ["silent_failures.charger_id", "silent_failures.station_name",
                       "silent_failures.failure_pattern", "silent_failures.window_days",
                       "silent_failures.confidence", "silent_failures.is_ongoing",
                       "silent_failures.observed_start", "silent_failures.observed_end",
                       "silent_failures.delivered_success_percent",
                       "silent_failures.peer_success_percent"],
        "measures": ["silent_failures.estimated_lost_sessions",
                     "silent_failures.estimated_lost_kwh",
                     "silent_failures.estimated_revenue_at_risk_gbp",
                     "silent_failures.daily_loss_rate_gbp",
                     "silent_failures.attempted_sessions",
                     "silent_failures.failed_sessions"],
        "filters": [cube.eq("silent_failures.charger_id", charger_id)]})
    if not rows:
        return _envelope("estimate_commercial_impact",
                         {"charger_id": charger_id, "flagged": False,
                          "message": "This charger is not in the detected set for the period."},
                         [], start=start, end=end, caveats=[PRIORITISATION_CAVEAT])
    r = rows[0]
    g = lambda k: cube.num(r.get("silent_failures." + k))
    portfolio = cube.load({"measures": ["silent_failures.estimated_revenue_at_risk_gbp",
                                        "silent_failures.daily_loss_rate_gbp",
                                        "silent_failures.ongoing_daily_loss_rate_gbp",
                                        "silent_failures.silent_failure_count",
                                        "silent_failures.ongoing_count"]})
    p = portfolio[0] if portfolio else {}
    daily = g("daily_loss_rate_gbp")
    ongoing_now = r.get("silent_failures.is_ongoing") in (True, "true", "True")
    # Only episodes still running can be projected forward. Annualising a fault
    # that already stopped invents a loss that is not occurring.
    port_daily = cube.num(p.get("silent_failures.ongoing_daily_loss_rate_gbp"))

    return _envelope(
        "estimate_commercial_impact",
        {"charger_id": charger_id,
         "station_name": r.get("silent_failures.station_name"),
         "failure_pattern": r.get("silent_failures.failure_pattern"),
         "confidence": r.get("silent_failures.confidence"),
         "is_ongoing": r.get("silent_failures.is_ongoing") in (True, "true", "True"),
         "observed_window": {
             "start": str(r.get("silent_failures.observed_start"))[:10],
             "end": str(r.get("silent_failures.observed_end"))[:10],
             "days": int(g("window_days"))},
         "this_charger": {
             "attempted_sessions": int(g("attempted_sessions")),
             "failed_sessions": int(g("failed_sessions")),
             "delivered_success_percent": round(g("delivered_success_percent"), 2),
             "peer_success_percent": round(g("peer_success_percent"), 2),
             "estimated_lost_sessions": round(g("estimated_lost_sessions"), 1),
             "estimated_lost_kwh": round(g("estimated_lost_kwh"), 1),
             "observed_revenue_at_risk_gbp": round(g("estimated_revenue_at_risk_gbp"), 2),
             "daily_loss_rate_gbp": round(daily, 2)},
         "all_detected_failures": {
             "device_count": int(cube.num(p.get("silent_failures.silent_failure_count"))),
             "still_failing_now": int(cube.num(p.get("silent_failures.ongoing_count"))),
             "observed_revenue_at_risk_gbp": round(
                 cube.num(p.get("silent_failures.estimated_revenue_at_risk_gbp")), 2),
             "combined_daily_loss_rate_gbp": round(port_daily, 2)},
         "run_rate_estimate": {
             "basis": "EXTRAPOLATED from the observed daily loss rate of episodes "
                      "that are still running. Closed episodes are excluded.",
             "charger_is_ongoing": ongoing_now,
             "charger_monthly_gbp": round(daily * 30, 2) if ongoing_now else 0.0,
             "charger_annualised_gbp": round(daily * 365, 2) if ongoing_now else 0.0,
             "ongoing_portfolio_daily_gbp": round(port_daily, 2),
             "ongoing_portfolio_monthly_gbp": round(port_daily * 30, 2),
             "ongoing_portfolio_annualised_gbp": round(port_daily * 365, 2)}},
        ["estimated_lost_sessions", "estimated_lost_kwh",
         "estimated_revenue_at_risk_gbp", "daily_loss_rate_gbp"],
        start=start, end=end,
        assumptions=[
            "Expected sessions come from the other chargers at the same station "
            "over the same window.",
            "Drivers turned away are assumed not to have simply charged on a "
            "neighbouring unit, which makes this an upper bound.",
            "Average kWh and tariff are taken from peer sessions in the window.",
            "Run-rate figures assume the fault continues unrepaired.",
        ],
        caveats=[
            PRIORITISATION_CAVEAT,
            "Observed figures cover the detected episode only. Monthly and "
            "annualised figures are extrapolations, not measurements.",
            "Run-rate projections count only episodes still running. A closed "
            "episode costs nothing today and is not annualised.",
        ])


# ---------------------------------------------------------------- 8
# How each detected pattern actually presents. The draft has to describe the
# failure in front of it: a low-power unit is still delivering energy, and
# telling an engineer it "delivers nothing" sends them looking for the wrong
# fault - or worse, gets the draft ignored next time.
PATTERN_COPY = {
    "silent_total": (
        "reporting available but delivering no energy",
        "The unit never enters a fault or offline state, so it does not appear in the "
        "alert queue. The only signal is the absence of energy while its site peers "
        "charge normally."),
    "connector_fault_partial": (
        "one connector appears unresponsive",
        "The unit stays available on its other outlet, so nothing is flagged, but a "
        "share of drivers arrive at the affected connector and get nothing."),
    "payment_terminal": (
        "payment authorisation failing on one method",
        "Other authorisation methods still work, so the unit looks healthy in status "
        "reporting while a share of drivers are declined at the terminal."),
    "low_power": (
        "delivering well below rated output",
        "Sessions complete, so nothing is flagged and no fault is raised, but each "
        "driver receives materially less energy than at neighbouring units."),
    "aborted_short": (
        "sessions starting then terminating early",
        "Each attempt registers as a transaction, so nothing is flagged, but drivers "
        "are leaving without a useful charge."),
}
PATTERN_DEFAULT = (
    "delivered performance materially below site peers",
    "Nothing in the status stream reads as a fault, so this unit does not appear in "
    "the alert queue, but its delivered performance is well below the other chargers "
    "at the same site.")


def draft_ops_action(charger_id: str, date_range: dict | None = None) -> dict:
    """Compose an operations email. Drafts only - nothing is ever sent."""
    ev = get_charger_evidence(charger_id, date_range)
    im = estimate_commercial_impact(charger_id, date_range)
    e, i = ev["metrics"], im["metrics"]
    det = e.get("detection", {})

    # Refuse to draft on a charger detection did not flag. A confident alert
    # about a healthy unit costs more trust than a missed one.
    if not det.get("flagged"):
        return _envelope(
            "draft_ops_action",
            {"charger_id": charger_id, "drafted": False, "sent": False,
             "requires_human_approval": False,
             "reason": "{} is not in the detected set for this period, so there is "
                       "nothing to raise. Over the observed month it took {} attempts "
                       "at {}% delivered success. Ask for its evidence if you want to "
                       "look at it anyway.".format(
                           charger_id,
                           e.get("full_period_context", {}).get("attempted_sessions", 0),
                           e.get("full_period_context", {}).get("delivered_success_percent", 0))},
            [], start=(date_range or {}).get("start"), end=(date_range or {}).get("end"),
            caveats=["No draft was produced. Raising an action on a healthy charger "
                     "wastes a field visit and erodes trust in the next alert."])

    tc = i.get("this_charger", {})
    peer = e.get("peer_comparison", {})
    sm = e.get("status_minutes", {})
    headline, why = PATTERN_COPY.get(det.get("failure_pattern"), PATTERN_DEFAULT)

    subject = "[ACTION] {} - {} {}".format(e.get("station_name"), charger_id, headline)

    delivered = (
        "  Energy delivered: {:.1f} kWh\n"
        "  Average power: {} kW against {} kW at neighbouring units\n".format(
            e.get("kwh_delivered", 0), e.get("avg_power_kw", "n/a"),
            peer.get("peer_average_power_kw", "n/a"))
        if det.get("failure_pattern") == "low_power"
        else "  Energy delivered: {:.1f} kWh\n".format(e.get("kwh_delivered", 0)))

    body = (
        "{} at {} needs a physical inspection.\n\n"
        "What the monitoring shows\n"
        "  Reported uptime over the episode: {:.2f}%\n"
        "  Faults logged: {}    Alerts raised: {}\n"
        "  Status minutes: available {:.0f}, preparing {:.0f}, charging {:.0f}, "
        "faulted {:.0f}, offline {:.0f}\n\n"
        "What drivers experienced ({} to {}, {} days{})\n"
        "  Charging attempts: {}\n"
        "  Successful: {}   Delivered success rate: {:.2f}%\n"
        "{}"
        "  Other chargers at this site over the same period: {:.1f}% success\n\n"
        "Why this is being raised\n"
        "  {}\n\n"
        "Commercial impact\n"
        "  Estimated lost sessions: about {:.0f}\n"
        "  Estimated lost energy: {:.0f} kWh\n"
        "  Estimated revenue at risk: GBP {:,.2f}\n"
        "  Current loss rate: GBP {:,.2f} per day\n"
        "  {}\n\n"
        "Recommended action\n"
        "  {}\n\n"
        "Confidence: {}.   Data as of {}.\n"
        "Prepared by the reliability copilot from governed metrics. No action has been "
        "taken and no engineer has been dispatched."
    ).format(
        charger_id, e.get("station_name"),
        e.get("reported_uptime_percent", 0), e.get("fault_events", 0),
        e.get("alerts_raised", 0),
        sm.get("available", 0), sm.get("preparing", 0), sm.get("charging", 0),
        sm.get("faulted", 0), sm.get("offline", 0),
        i.get("observed_window", {}).get("start"), i.get("observed_window", {}).get("end"),
        i.get("observed_window", {}).get("days"),
        ", still failing" if i.get("is_ongoing") else ", now closed",
        e.get("attempted_sessions", 0), e.get("successful_sessions", 0),
        e.get("delivered_success_percent", 0),
        delivered,
        peer.get("peer_delivered_success_percent", 0),
        why,
        tc.get("estimated_lost_sessions", 0), tc.get("estimated_lost_kwh", 0),
        tc.get("observed_revenue_at_risk_gbp", 0), tc.get("daily_loss_rate_gbp", 0),
        PRIORITISATION_CAVEAT,
        det.get("recommended_action", "Investigate on site."),
        i.get("confidence", "unknown"), ev["as_of"])

    return _envelope(
        "draft_ops_action",
        {"charger_id": charger_id,
         "drafted": True,
         "failure_pattern": det.get("failure_pattern"),
         "channel": "email",
         "to": ["field-operations@voltway.example"],
         "cc": ["regional-manager@voltway.example"],
         "subject": subject,
         "body": body,
         "requires_human_approval": True,
         "sent": False,
         "ticket_draft": {
             "queue": "field_maintenance",
             "priority": "P2" if i.get("is_ongoing") else "P3",
             # A field engineer reads this, not a database. The pattern key is
             # an internal identifier and should not lead a maintenance ticket.
             "summary": "{} - {} - {}".format(
                 charger_id,
                 str(det.get("failure_pattern") or "unclassified").replace("_", " "),
                 e.get("station_name")),
             "recommended_action": det.get("recommended_action")}},
        [], start=(date_range or {}).get("start"), end=(date_range or {}).get("end"),
        assumptions=["Every figure in the draft came from the tools above, "
                     "not from the language model."],
        caveats=["This is a draft. Nothing is sent, no ticket is created, and no "
                 "engineer is dispatched without a person approving it."])


REGISTRY = {
    "get_reported_reliability": get_reported_reliability,
    "get_delivered_reliability": get_delivered_reliability,
    "get_gap_anatomy": get_gap_anatomy,
    "get_alert_blindspot": get_alert_blindspot,
    "find_silent_failures": find_silent_failures,
    "get_charger_evidence": get_charger_evidence,
    "estimate_commercial_impact": estimate_commercial_impact,
    "draft_ops_action": draft_ops_action,
}
