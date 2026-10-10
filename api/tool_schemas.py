"""
Anthropic tool definitions for the approved tool layer.

These are handed to Claude as the complete set of actions available. The
descriptions matter as much as the schemas: they tell the model when a tool is
the right one and, just as importantly, what it must not conclude from it.
"""

_DATE_RANGE = {
    "type": "object",
    "description": "Optional. Defaults to the whole observed pilot period "
                   "(2026-08-06 to 2026-09-04).",
    "properties": {
        "start": {"type": "string", "description": "ISO date, e.g. 2026-09-01"},
        "end": {"type": "string", "description": "ISO date, e.g. 2026-09-04"},
    },
}
_REGION = {
    "type": "string",
    "description": "Optional pilot region: West Midlands, East Midlands, "
                   "North West, Yorkshire or North East. Omit for the whole pilot.",
}

TOOL_SCHEMAS = [
    {
        "name": "get_reported_reliability",
        "description": (
            "Status-derived reliability: the share of charger-time spent in a "
            "non-fault operational state. This is the number an operator "
            "publishes. Use it when asked what reliability or uptime is. It does "
            "NOT tell you whether drivers could actually charge."
        ),
        "input_schema": {"type": "object",
                         "properties": {"date_range": _DATE_RANGE, "region": _REGION}},
    },
    {
        "name": "get_delivered_reliability",
        "description": (
            "Driver-experienced reliability: the share of charging attempts that "
            "actually delivered energy. Use it alongside reported reliability "
            "whenever someone asks about reliability, because the difference "
            "between the two is the whole point."
        ),
        "input_schema": {"type": "object",
                         "properties": {"date_range": _DATE_RANGE, "region": _REGION}},
    },
    {
        "name": "get_gap_anatomy",
        "description": (
            "Both reliability numbers plus a breakdown of the delivered failure "
            "rate into the part concentrated on detected devices (actionable) "
            "and the part spread thinly across the estate (baseline, not fixable "
            "by dispatching an engineer). Use this when asked why the gap exists "
            "or what can be done about it. Note the decomposition splits the "
            "FAILURE RATE, not the gap - they differ by reported downtime."
        ),
        "input_schema": {"type": "object",
                         "properties": {"date_range": _DATE_RANGE, "region": _REGION}},
    },
    {
        "name": "get_alert_blindspot",
        "description": (
            "Compares the operator's existing alert queue with the failures "
            "detection found. Returns alert counts, detected silent-failure "
            "count, how many appear in both, and the hidden revenue at risk. "
            "Use this for questions about what monitoring is missing, which "
            "chargers are not in the alert queue, or where hidden risk sits."
        ),
        "input_schema": {"type": "object",
                         "properties": {"date_range": _DATE_RANGE, "region": _REGION}},
    },
    {
        "name": "find_silent_failures",
        "description": (
            "The ranked portfolio of chargers detected as impaired while status "
            "reporting shows them healthy. Ordered by dispatch priority: still-"
            "running episodes first, then by daily loss rate. Use this to answer "
            "which chargers are creating hidden risk or where to send the first "
            "engineer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_range": _DATE_RANGE, "region": _REGION,
                "limit": {"type": "integer",
                          "description": "How many to return. Default 10."},
            },
        },
    },
    {
        "name": "get_charger_evidence",
        "description": (
            "Full evidence for one charger: attempts by outcome, status "
            "timeline, delivered success, same-site peer comparison, faults and "
            "alerts, plus full-period context showing whether it was working "
            "before. Defaults to the detected failure episode rather than the "
            "whole month. Use this before recommending any action on a specific "
            "charger."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "charger_id": {"type": "string", "description": "e.g. CHG-0034"},
                "date_range": _DATE_RANGE,
            },
            "required": ["charger_id"],
        },
    },
    {
        "name": "estimate_commercial_impact",
        "description": (
            "Costs a detected failure at three levels: this charger's observed "
            "episode, the whole detected portfolio, and a forward run-rate that "
            "counts only episodes still running. Always present the run-rate as "
            "an extrapolation and repeat that these are prioritisation "
            "estimates, not accounting-grade lost revenue."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "charger_id": {"type": "string", "description": "e.g. CHG-0034"},
                "date_range": _DATE_RANGE,
            },
            "required": ["charger_id"],
        },
    },
    {
        "name": "draft_ops_action",
        "description": (
            "Drafts an operations email and maintenance ticket for a charger, "
            "with the evidence and impact embedded. It only drafts: nothing is "
            "sent, no ticket is created, and no engineer is dispatched. Always "
            "tell the user the draft needs their approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "charger_id": {"type": "string", "description": "e.g. CHG-0034"},
                "date_range": _DATE_RANGE,
            },
            "required": ["charger_id"],
        },
    },
]
