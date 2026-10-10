"""
The conversational copilot: a Claude tool-calling loop over the approved tools.

The model does not compute anything. It decides which approved tool to call,
reads the structured result, and explains it in business language. Every number
it can say came back from Cube through api/tools.py.

Two modes:
  live    real Claude API call, real tools, real Cube metrics
  replay  a previously captured live investigation, replayed step by step

Replay exists because a demo runs on someone else's wifi. It is not a mock -
the traces are recordings of this same loop against this same data.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Iterator

import anthropic

from . import figure_check
from . import tools as T
from .tool_schemas import TOOL_SCHEMAS

MODEL = os.getenv("ANTHROPIC_MODEL", "claude-opus-5")
EFFORT = os.getenv("COPILOT_EFFORT", "medium")
MAX_TURNS = 12
# Captured traces live in traces/ on a developer machine and in demo_traces/
# in the image, because replay has to work on a host that has never run capture.
TRACE_DIR = Path(os.getenv("TRACE_DIR", "")) if os.getenv("TRACE_DIR") \
    else (Path(__file__).parent.parent / "traces")
if not TRACE_DIR.exists() or not any(TRACE_DIR.glob("*.json")):
    _fallback = Path(__file__).parent.parent / "demo_traces"
    if _fallback.exists():
        TRACE_DIR = _fallback

SYSTEM_PROMPT = """You are Phantom Uptime, the reliability copilot for Voltway Networks, \
a fictional UK rapid EV charging operator. You support commercial directors, \
operations managers and regional managers: people who make decisions, not \
people who write SQL.

This is a demonstration on a synthetic Midlands and North of England pilot \
estate. The operator is fictional; the data is entirely generated and nothing \
here is a finding about any real company. Say so if anyone asks whether \
the data is real. Do not state the size of the estate unless a tool has \
returned it.

HOW YOU WORK
You do not know any figures. You cannot calculate, estimate or recall a number.
Every figure you state must have come back from a tool in this conversation. If
a tool has not returned it, you do not have it, and you say so rather than
filling the gap.

Plan before you call. For anything beyond a single lookup, work out which tools
you need and in what order, then call them. Call independent tools together in
one turn rather than one at a time.

THE TWO NUMBERS
Reported reliability is time-based: the share of charger-time in a non-fault
operational status. It is what an operator publishes.
Delivered reliability is attempt-based: the share of charging attempts that
actually transferred energy. It is what a driver experiences.
They use different denominators and are not two versions of the same
accounting measure. When someone asks about reliability, give both, and explain
the relationship in those terms: reported reliability is status-derived uptime,
delivered reliability is based on successful driver charging outcomes, and the
gap shows where status-based monitoring overstates the driver experience.

Do not overclaim about the gap. Most of it is diffuse baseline failure spread
across the estate: declines, handshake errors, drivers giving up. That part is
not fixable by dispatching an engineer. The actionable part is the failures
concentrated on individual chargers. Say which is which.

WINDOWS
Charger evidence comes back in two windows and you must never merge them. The
ACTIVE EPISODE is the detected failure window. The FULL PERIOD is the whole
observed month. When you describe a charger, state which window each figure
belongs to. If a tool returns a note explaining a figure - for example Charging
minutes carried over from a session that began before the episode - repeat that
explanation rather than quoting the raw number on its own.

HOW YOU ANSWER
You are writing for a commercial director or operations director. Keep the
register professional and plain: no slang, no jokes, no conversational asides.
Lead with the answer, then the evidence. Business language in the prose. No
table names, no metric identifiers, no JSON.
Attach the as_of date to any figure you quote.
State what the number does not cover. A copilot that names its own blind spots
is more useful than one that sounds certain.
Call revenue-at-risk figures what they are: estimates for operational
prioritisation, not accounting-grade lost revenue. Label anything monthly or
annualised as an extrapolation.
Keep it short. Three or four short paragraphs, or a tight list. No headers, no
bold, no bullet-point walls.

WHEN YOU RETURN A DRAFT
The drafted email and ticket are displayed to the user in full, directly below
your answer. Do not restate their contents. Say what the draft recommends, the
single figure that justifies it, and that it needs a person to approve and send
it. Three sentences is usually enough.

WHAT YOU NEVER DO
You never dispatch an engineer, send an email, raise a ticket or change a
price. draft_ops_action produces a draft and nothing more. When you return
one, say plainly that it needs a person to approve and send it.
You never present a drafted action as a completed one.
You never invent a charger ID, a station name or a figure.
You never work a figure out for yourself. Every number you state must appear in
a tool result exactly as it was returned. If you catch yourself subtracting,
dividing or netting two figures to reach a third, stop: state the two you were
given, or leave it out. A number that is nearly right is worse than a number you
did not give, because the person reading it cannot tell which is which.
You never say that you have checked, confirmed or verified a figure. You have no
way to check one. Report what the tools returned and let the person judge it.
You never use an em dash. Use a full stop, a comma, a colon or brackets instead.
You never call something the largest, the worst, the top or the main one unless a
tool ordered it that way and you say which ordering you mean. find_silent_failures
returns chargers ranked by dispatch_priority, which puts running episodes first
and then ranks by daily loss rate. That is not the same order as total revenue at
risk, and a charger can lead one and not the other. If you want a superlative,
name the measure: "the largest ongoing loss", "the highest daily rate". A wrong
superlative survives every check on the figures, because each figure in it is
real.
If someone asks you to run SQL, query a table directly, or reach the raw
operational data, say plainly that you cannot: you call approved tools, and the
metrics behind them are defined in the semantic layer. Then offer the tool that
answers what they were actually after. Doing the equivalent silently hides the
boundary that makes this system trustworthy."""


class LiveModeUnavailable(RuntimeError):
    pass


# A transient 429 or 529 should cost a retry, not the whole investigation. The
# timeout is per request, and the loop can make several, so it is deliberately
# shorter than a person's patience rather than longer.
CLIENT_TIMEOUT = float(os.getenv("ANTHROPIC_TIMEOUT", "60"))
CLIENT_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "3"))


def _client() -> anthropic.Anthropic:
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise LiveModeUnavailable(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env, or use replay mode.")
    return anthropic.Anthropic(timeout=CLIENT_TIMEOUT, max_retries=CLIENT_RETRIES)


def _summarise(result: dict) -> dict:
    """A compact view of a tool result, for the investigation trace in the UI."""
    m = result.get("metrics", {})
    if "chargers" in m:
        top = m["chargers"][:3]
        return {"returned": m.get("returned"),
                "top": [{"charger_id": c["charger_id"], "station_name": c["station_name"],
                         "failure_pattern": c["failure_pattern"],
                         "daily_loss_rate_gbp": c["daily_loss_rate_gbp"]} for c in top]}
    return {k: v for k, v in m.items() if not isinstance(v, (dict, list))}


def investigate(question: str, model: str | None = None,
                history: list[dict] | None = None) -> Iterator[dict]:
    """Run one investigation, yielding trace events as they happen.

    `history` carries prior question/answer pairs so a follow-up like "why that
    one?" resolves. Only the text is replayed - thinking blocks belong to the
    turn that produced them and are not echoed across turns.
    """
    client = _client()
    model = model or MODEL
    messages: list[dict[str, Any]] = []
    for prior in (history or [])[-3:]:
        if prior.get("question") and prior.get("answer"):
            messages.append({"role": "user", "content": prior["question"]})
            messages.append({"role": "assistant", "content": prior["answer"]})
    messages.append({"role": "user", "content": question})
    started = time.time()
    # Kept so the answer can be checked against what the tools returned.
    # Deliberately not named tool_results: that name already belongs to the
    # blocks sent back to the model, and reusing it put raw payloads into the
    # request and broke every live investigation.
    checked_results: list[Any] = []
    answer_parts: list[str] = []

    yield {"type": "question", "question": question,
           "model": model, "effort": EFFORT, "mode": "live"}

    for turn in range(MAX_TURNS):
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOL_SCHEMAS,
            output_config={"effort": EFFORT},
            messages=messages,
        )

        if response.stop_reason == "refusal":
            yield {"type": "error",
                   "message": "The model declined this request.",
                   "detail": getattr(response.stop_details, "explanation", None)}
            return

        # The model's own words between tool calls: this is the plan.
        for block in response.content:
            if block.type == "text" and block.text.strip():
                kind = "thought" if response.stop_reason == "tool_use" else "answer"
                if kind == "answer":
                    answer_parts.append(block.text)
                yield {"type": kind, "text": block.text}

        if response.stop_reason != "tool_use":
            yield {"type": "figure_check",
                   **figure_check.check("\n\n".join(answer_parts), checked_results)}
            yield {"type": "done", "turns": turn + 1,
                   "elapsed_s": round(time.time() - started, 1),
                   "usage": {"input_tokens": response.usage.input_tokens,
                             "output_tokens": response.usage.output_tokens}}
            return

        # Echo the assistant turn back unchanged - thinking blocks included.
        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            name, args = block.name, dict(block.input or {})
            t0 = time.time()
            try:
                result = T.REGISTRY[name](**args)
                error = None
            except Exception as exc:                            # noqa: BLE001
                result = {"error": str(exc)[:300]}
                error = str(exc)[:300]
            if not error:
                # Only successful results count towards the figure check: a
                # failure has no numbers the answer is entitled to quote.
                checked_results.append(result)

            # The full result travels with the trace. The interface composes
            # itself from what the agent actually called, so it needs the same
            # payload the model saw - not a summary of it.
            yield {"type": "tool_call", "tool": name, "arguments": args,
                   "ms": int((time.time() - t0) * 1000),
                   "source_layer": result.get("source_layer"),
                   "as_of": result.get("as_of"),
                   "result_summary": None if error else _summarise(result),
                   "result": None if error else result,
                   "error": error}

            tool_results.append({
                "type": "tool_result", "tool_use_id": block.id,
                "content": json.dumps(result, default=str),
                **({"is_error": True} if error else {}),
            })

        # All results for one assistant turn go back in a single user message.
        messages.append({"role": "user", "content": tool_results})

    yield {"type": "error",
           "message": "Investigation did not converge within {} turns.".format(MAX_TURNS)}


def run(question: str, model: str | None = None) -> dict:
    """Collect a full investigation into one trace object."""
    events = list(investigate(question, model))
    return _assemble(question, events, mode="live")


def _assemble(question: str, events: list[dict], mode: str) -> dict:
    answer = "\n\n".join(e["text"] for e in events if e["type"] == "answer")
    return {
        "question": question,
        "mode": mode,
        "answer": answer.strip(),
        "plan": [e["text"] for e in events if e["type"] == "thought"],
        "tool_calls": [e for e in events if e["type"] == "tool_call"],
        "events": events,
        "captured_at": next((e.get("captured_at") for e in events if "captured_at" in e), None),
    }


# ---------------------------------------------------------------- replay

def trace_path(question: str) -> Path:
    slug = "".join(c if c.isalnum() else "-" for c in question.lower())[:60].strip("-")
    return TRACE_DIR / "{}.json".format(slug)


def save_trace(trace: dict) -> Path:
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    trace["captured_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    p = trace_path(trace["question"])
    p.write_text(json.dumps(trace, indent=2, default=str), encoding="utf-8")
    return p


def list_traces() -> list[dict]:
    if not TRACE_DIR.exists():
        return []
    out = []
    for p in sorted(TRACE_DIR.glob("*.json")):
        try:
            t = json.loads(p.read_text(encoding="utf-8"))
            out.append({"question": t["question"], "captured_at": t.get("captured_at"),
                        "tool_calls": len(t.get("tool_calls", [])), "file": p.name})
        except Exception:                                       # noqa: BLE001
            logging.getLogger("copilot").warning("unreadable trace: %s", p.name)
    return out


def load_trace(question: str) -> dict | None:
    p = trace_path(question)
    if not p.exists():
        return None
    t = json.loads(p.read_text(encoding="utf-8"))
    t["mode"] = "replay"
    return t
