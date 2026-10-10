"""
FastAPI backend for Phantom Uptime, the reliability copilot.

Exposes the approved tool layer over HTTP and, next, the Claude tool-calling
loop. Nothing here computes a business metric: every number comes back from
Cube through api/tools.py.

Run:  uvicorn api.main:app --reload --port 8000
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (FileResponse, HTMLResponse, RedirectResponse,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()                      # ANTHROPIC_API_KEY stays server-side

# Uvicorn leaves the root logger at WARNING, so an audit line at INFO would be
# written and then dropped. Give this logger its own handler and level: the
# record of what ran is not optional output.
logger = logging.getLogger("copilot")
logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(_h)
    logger.propagate = False

from . import agent                                    # noqa: E402
from . import auth                                     # noqa: E402
from . import figure_check                             # noqa: E402
from . import limits                                   # noqa: E402
from . import cube_client as cube                      # noqa: E402
from . import tools as T                               # noqa: E402
from .tool_schemas import TOOL_SCHEMAS                 # noqa: E402

app = FastAPI(
    title="Phantom Uptime",
    description=(
        "Synthetic scenario. A conversational copilot over a governed EV "
        "charging reliability model. The agent calls approved tools; the tools "
        "read Cube metrics; nothing reads raw operational data."
    ),
    version="0.3.0",
)

# Same-origin by default. A frontend on another host (Vercel) has to be named
# explicitly, and credentials are allowed because the session is a cookie.
_origins = [o.strip() for o in os.getenv(
    "CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


LOGIN_PAGE = Path(__file__).parent.parent / "web" / "login.html"


def _login_html(error: str = "") -> str:
    html = LOGIN_PAGE.read_text(encoding="utf-8")
    if error:
        html = html.replace("<!--ERROR-->",
                            '<div class="err">{}</div>'.format(error))
    return html


@app.middleware("http")
async def gate(request: Request, call_next):
    """Everything except the login screen and the health check needs a session.

    The health check stays open on purpose: a load balancer or a colleague
    checking whether the box is up should not need the password, and it returns
    no data about the estate.
    """
    if not auth.enabled() or auth.is_open(request.url.path):
        return await call_next(request)
    if auth.valid(request.cookies.get(auth.COOKIE)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return Response('{"detail":"Not authenticated"}', status_code=401,
                        media_type="application/json")
    return HTMLResponse(_login_html(), status_code=401)


@app.get("/login")
def login_page():
    if not auth.enabled():
        return RedirectResponse("/", status_code=302)
    return HTMLResponse(_login_html())


@app.post("/login")
def login(username: str = Form(""), password: str = Form("")):
    if not auth.enabled():
        return RedirectResponse("/", status_code=302)
    if not auth.check_credentials(username, password):
        # One message for both cases: which half was wrong is not the visitor's
        # business.
        return HTMLResponse(_login_html("Those details were not recognised."),
                            status_code=401)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie(auth.COOKIE, auth.issue(), max_age=auth.TTL_SECONDS,
                 httponly=True, secure=auth.SECURE, samesite=auth.SAMESITE,
                 path="/")
    return r


@app.get("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(auth.COOKIE, path="/")
    return r


DEMO_QUESTIONS = [
    "What is our reliability this month?",
    "Why is reported reliability different from delivered reliability?",
    "Which chargers are missing from the alert queue?",
    "Which charger should we send the first engineer to?",
    "Why CHG-0034?",
    "What is the commercial impact?",
    "Draft an operations email for CHG-0034.",
    "What would I tell the Commercial Director?",
]


class ToolCall(BaseModel):
    arguments: dict[str, Any] = {}


@app.get("/health")
def health(request: Request) -> dict:
    """Liveness plus a real dependency check, not just 'the process is up'.

    This endpoint sits outside the login gate so the box can be checked without
    a password, which means it must not describe the inside of the box. An
    anonymous caller gets up or down. A signed-in one gets the detail the UI
    needs.
    """
    cube_ok, detail = True, "reachable"
    try:
        cube.load({"measures": ["reliability_summary.reported_reliability_percent"]})
    except Exception as exc:                                  # noqa: BLE001
        cube_ok, detail = False, str(exc)[:200]

    status = "ok" if cube_ok else "degraded"
    if auth.enabled() and not auth.valid(request.cookies.get(auth.COOKIE)):
        return {"status": status, "auth_required": True}

    return {
        "status": status,
        "cube": {"reachable": cube_ok, "detail": detail, "url": cube.CUBE_URL},
        "tools": sorted(T.REGISTRY),
        "anthropic_key_present": bool(os.getenv("ANTHROPIC_API_KEY")),
        "mode": os.getenv("COPILOT_MODE", "live"),
        "auth_required": auth.enabled(),
        "live_budget": limits.snapshot(),
    }


@app.get("/api/tools")
def list_tools() -> dict:
    """The complete set of things the copilot is allowed to do."""
    return {"tools": TOOL_SCHEMAS,
            "note": "The agent may call these and nothing else. No ad-hoc SQL, "
                    "no access to raw operational tables."}


@app.post("/api/tools/{name}")
def call_tool(name: str, body: ToolCall) -> dict:
    if name not in T.REGISTRY:
        raise HTTPException(404, "Unknown tool: {}".format(name))
    try:
        return T.REGISTRY[name](**body.arguments)
    except TypeError as exc:
        raise HTTPException(422, "Bad arguments for {}: {}".format(name, exc)) from exc
    except cube.CubeError as exc:
        raise HTTPException(503, _cube_down(exc)) from exc


@app.get("/api/dashboard")
def dashboard() -> dict:
    """Everything the landing screen needs, in one round trip."""
    try:
        blind = T.get_alert_blindspot()
        gap = T.get_gap_anatomy()
        top = T.find_silent_failures(limit=8)
    except cube.CubeError as exc:
        raise HTTPException(503, _cube_down(exc)) from exc
    chargers = top["metrics"]["chargers"]
    return {
        "as_of": blind["as_of"],
        "pilot": {"chargers": blind["metrics"]["pilot_chargers"],
                  "stations": blind["metrics"]["pilot_stations"],
                  "scope": "Midlands and North of England pilot — synthetic scenario"},
        "reliability": {
            "reported_percent": gap["metrics"]["reported_reliability_percent"],
            "delivered_percent": gap["metrics"]["delivered_reliability_percent"],
            "gap_points": gap["metrics"]["reliability_gap_points"],
            "delivered_failure_rate_points": gap["metrics"]["delivered_failure_rate_points"],
            # The screen has to be able to show the reconciliation, not just the
            # two halves of the split.
            "reported_downtime_points": gap["metrics"]["reported_downtime_points"],
            "actionable_points": gap["metrics"]["failure_points_from_detected_devices"],
            "baseline_points": gap["metrics"]["failure_points_baseline_diffuse"],
        },
        "blindspot": {
            "visible_alert_count": blind["metrics"]["visible_alert_count"],
            "visible_alert_device_count": blind["metrics"]["visible_alert_device_count"],
            "silent_failure_count": blind["metrics"]["silent_failure_count"],
            "overlap": blind["metrics"]["alert_silent_overlap"],
            "still_failing_now": blind["metrics"]["still_failing_now"],
            "hidden_revenue_at_risk_gbp": blind["metrics"]["hidden_revenue_at_risk_gbp"],
            "ongoing_daily_loss_rate_gbp": blind["metrics"]["ongoing_daily_loss_rate_gbp"],
            "ongoing_annualised_gbp": round(
                blind["metrics"]["ongoing_daily_loss_rate_gbp"] * 365, 2),
        },
        "top_priority": chargers[0] if chargers else None,
        "portfolio": chargers,
        "caveat": T.PRIORITISATION_CAVEAT,
    }


MAX_QUESTION = 2000
MAX_HISTORY_TURNS = 3
MAX_HISTORY_CHARS = 4000


class ChatRequest(BaseModel):
    question: str = Field(max_length=MAX_QUESTION)
    mode: Literal["live", "replay"] | None = None   # defaults to env
    history: list[dict] | None = None               # prior [{question, answer}] turns

    def clean_history(self) -> list[dict]:
        """History is caller-supplied and is replayed to the model as its own
        prior words, so it is untrusted input, not state. Keep the last few
        turns, keep them strings, keep them short. This does not make a forged
        turn impossible; it makes it bounded and cheap to reason about."""
        out = []
        for prior in (self.history or [])[-MAX_HISTORY_TURNS:]:
            if not isinstance(prior, dict):
                continue
            q, a = prior.get("question"), prior.get("answer")
            if isinstance(q, str) and isinstance(a, str) and q.strip() and a.strip():
                out.append({"question": q[:MAX_HISTORY_CHARS],
                            "answer": a[:MAX_HISTORY_CHARS]})
        return out


@app.get("/api/questions")
def suggested_questions() -> dict:
    """The demo set, with whichever of them already have a captured trace."""
    captured = {t["question"] for t in agent.list_traces()}
    return {"questions": [{"question": q, "has_trace": q in captured} for q in DEMO_QUESTIONS],
            "traces": agent.list_traces()}


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    """Run one investigation.

    Live mode calls Claude and the tools for real. Replay mode returns a
    previously captured live run, so the demo still works if the network does
    not. Replay is a recording of this same loop, not a mock.
    """
    mode = req.mode or os.getenv("COPILOT_MODE", "live")
    if mode == "replay":
        trace = agent.load_trace(req.question)
        if not trace:
            raise HTTPException(404, "No captured trace for that question.")
        return trace
    try:
        return agent.run(req.question)
    except agent.LiveModeUnavailable as exc:
        trace = agent.load_trace(req.question)
        if trace:
            trace["fallback_reason"] = str(exc)
            return trace
        raise HTTPException(503, str(exc)) from exc


@app.post("/api/replay")
def replay(req: ChatRequest) -> dict:
    trace = agent.load_trace(req.question)
    if not trace:
        raise HTTPException(404, "No captured trace for that question.")
    return trace


def _audit(**fields) -> None:
    """One line per investigation, as JSON, so a run can be accounted for after
    the fact: what mode, how long, how many turns, which tools, whether the
    figures checked out. The question is recorded; nothing else about the caller
    is, and no secret ever reaches here."""
    logger.info("investigation %s", json.dumps(fields, default=str))


def _cube_down(exc: Exception) -> str:
    """The client is told the layer is down. What it was saying goes to the log,
    where an operator can read it and a visitor cannot."""
    logger.warning("cube error: %s", exc)
    return ("The semantic layer is unavailable, so the tools cannot answer. "
            "Recorded investigations still work.")


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, default=str) + chr(10) + chr(10)



@app.post("/api/chat/stream")
def chat_stream(req: ChatRequest, request: Request):
    """Server-sent events for one investigation.

    Live mode streams the agent's own generator, so the progress the UI shows is
    the investigation actually happening - planning, each approved tool call,
    then the answer. Replay streams the recorded events of a previous live run
    at a readable pace. Neither is a simulated progress bar.
    """
    mode = req.mode or os.getenv("COPILOT_MODE", "live")
    started = time.time()
    seen = {"turns": 0, "tools": [], "figure_check": None, "status": "ok"}

    def watch(chunks):
        """Pass every chunk through byte for byte, and read enough of them to
        account for the run afterwards. The generators yield encoded SSE frames,
        not events, so this parses rather than assumes."""
        for chunk in chunks:
            if chunk.startswith("data: "):
                try:
                    e = json.loads(chunk[6:])
                except ValueError:
                    e = {}
                t = e.get("type")
                if t == "tool_call":
                    seen["tools"].append(e.get("tool"))
                elif t == "figure_check":
                    seen["figure_check"] = ("pass" if not e.get("unsourced")
                                            else "{} unsourced".format(len(e["unsourced"])))
                elif t == "done":
                    seen["turns"] = e.get("turns", 0)
                elif t == "error":
                    seen["status"] = "error"
            yield chunk
        _audit(question=req.question[:200], mode=mode,
               duration_ms=int((time.time() - started) * 1000), **seen)

    def live():
        allowed, reason = limits.check(limits.caller_id(request))
        if not allowed:
            seen["status"] = "rate_limited"
            trace = agent.load_trace(req.question)
            yield _sse({"type": "notice", "message": reason})
            if trace:
                yield from replayed(trace)
            else:
                yield _sse({"type": "error", "message": reason})
            return
        try:
            for event in agent.investigate(req.question, history=req.clean_history()):
                yield _sse(event)
        except agent.LiveModeUnavailable as exc:
            trace = agent.load_trace(req.question)
            if not trace:
                yield _sse({"type": "error", "message": str(exc)})
                return
            yield _sse({"type": "notice",
                        "message": "Live mode unavailable, replaying a captured "
                                   "investigation instead.", "detail": str(exc)})
            yield from replayed(trace)
        except Exception:                                       # noqa: BLE001
            logger.exception("live investigation failed")
            yield _sse({"type": "error",
                        "message": "The investigation could not be completed. "
                                   "Switch to replay mode to continue."})

    def replayed(trace: dict):
        events = trace["events"]
        # Traces captured before the check existed still get one: the recording
        # holds both the answer and the tool results it was built from.
        if not any(e.get("type") == "figure_check" for e in events):
            results = [e["result"] for e in events
                       if e.get("type") == "tool_call" and e.get("result")]
            answer = "\n\n".join(e["text"] for e in events
                                  if e.get("type") == "answer")
            verdict = {"type": "figure_check", **figure_check.check(answer, results)}
            events = [e for e in events if e.get("type") != "done"]
            events.append(verdict)
            events += [e for e in trace["events"] if e.get("type") == "done"]
        for event in events:
            e = dict(event)
            e["mode"] = "replay"
            e["captured_at"] = trace.get("captured_at")
            yield _sse(e)
            # pace it so a viewer can read the trace as it builds
            time.sleep(0.5 if e["type"] == "tool_call" else 0.25)

    def replay_only():
        trace = agent.load_trace(req.question)
        if trace:
            yield from replayed(trace)
            return
        # Not one of the prepared questions. Run it live if we can - refusing
        # would make every charger in the overview a dead end.
        if not os.getenv("ANTHROPIC_API_KEY"):
            yield _sse({"type": "error",
                        "message": "No captured investigation for that question, and live "
                                   "mode is unavailable."})
            return
        yield _sse({"type": "notice",
                    "message": "No captured investigation for this question - running it live."})
        yield from live()

    return StreamingResponse(watch(replay_only() if mode == "replay" else live()),
                             media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


WEB = Path(__file__).parent.parent / "web"
if WEB.exists():
    app.mount("/static", StaticFiles(directory=str(WEB)), name="static")

    @app.get("/")
    def index():
        return FileResponse(str(WEB / "index.html"))
