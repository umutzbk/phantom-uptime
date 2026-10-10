"""A spending guard for live mode, not an authorisation system.

Replay costs nothing and is left alone: it is a recording, and the demo should
stay usable however often anyone opens it. Live mode calls a paid model, and the
URL of this demo goes out by email, so it needs a ceiling that does not depend
on everyone behaving.

In memory on purpose. One instance, one process, and a counter that resets when
the container restarts is the right size of solution for a demo. A second
instance would need a shared store, and that is the moment to reach for one.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque

# Per caller, and for everyone together. The global cap is the one that actually
# protects the account: a shared link means many callers behind one door.
PER_CALLER_PER_HOUR = int(os.getenv("LIVE_LIMIT_PER_CALLER_HOUR", "12"))
GLOBAL_PER_DAY = int(os.getenv("LIVE_LIMIT_GLOBAL_DAY", "200"))

_HOUR = 3600
_DAY = 86400

_lock = threading.Lock()
_by_caller: dict[str, deque[float]] = {}
_global: deque[float] = deque()


def caller_id(request) -> str:
    """Behind Caddy the socket address is the proxy, so prefer the forwarded
    address. Spoofable by anyone talking to the app directly, which is why the
    global cap exists as well."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _prune(bucket: deque[float], window: int, now: float) -> None:
    while bucket and now - bucket[0] > window:
        bucket.popleft()


def check(caller: str) -> tuple[bool, str]:
    """Ask before spending. Returns (allowed, reason if not)."""
    now = time.time()
    with _lock:
        _prune(_global, _DAY, now)
        if len(_global) >= GLOBAL_PER_DAY:
            return False, ("This demo has reached its daily limit for live "
                           "investigations. Replay mode still works and plays "
                           "back recorded live runs.")
        bucket = _by_caller.setdefault(caller, deque())
        _prune(bucket, _HOUR, now)
        if len(bucket) >= PER_CALLER_PER_HOUR:
            return False, ("You have run {} live investigations in the last hour, "
                           "which is the limit for this demo. Replay mode still "
                           "works.".format(PER_CALLER_PER_HOUR))
        bucket.append(now)
        _global.append(now)
        return True, ""


def snapshot() -> dict:
    """For the health endpoint, so an operator can see the headroom."""
    now = time.time()
    with _lock:
        _prune(_global, _DAY, now)
        return {"live_today": len(_global), "live_daily_cap": GLOBAL_PER_DAY,
                "per_caller_hourly_cap": PER_CALLER_PER_HOUR}
