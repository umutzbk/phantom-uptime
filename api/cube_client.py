"""
Thin client for the Cube REST API.

Everything the tool layer knows about metrics comes through here. There is no
SQL in the tool layer and no direct access to the marts, so a metric can only
mean what Cube says it means.

Metric DEFINITIONS are pulled from Cube's own /meta endpoint rather than being
restated in Python. If a definition changes in the semantic layer, the text the
copilot shows the user changes with it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

import httpx

CUBE_URL = os.getenv("CUBE_URL", "http://localhost:4000/cubejs-api/v1")
TIMEOUT = float(os.getenv("CUBE_TIMEOUT", "30"))

# The observed pilot period. Every tool defaults to this unless asked otherwise.
PERIOD_START = "2026-08-06"
PERIOD_END = "2026-09-04"

_meta_cache: dict[str, Any] = {}
_meta_at: float = 0.0


class CubeError(RuntimeError):
    pass


# With dev mode off, which is how it should run in production, Cube requires a
# signed token on every request. Rather than add a JWT dependency for one
# fifteen-line function, sign it here with the shared secret from the compose
# environment. No secret ever leaves the server.
API_SECRET = os.getenv("CUBEJS_API_SECRET", "")
_token_cache: dict[str, Any] = {"value": None, "exp": 0.0}


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _token() -> str | None:
    """An HS256 token Cube will accept, refreshed well before it expires."""
    if not API_SECRET:
        return None
    now = time.time()
    if _token_cache["value"] and _token_cache["exp"] > now + 60:
        return _token_cache["value"]
    exp = int(now) + 3600
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"},
                             separators=(",", ":")).encode())
    payload = _b64(json.dumps({"exp": exp}, separators=(",", ":")).encode())
    signing_input = "{}.{}".format(header, payload).encode()
    sig = _b64(hmac.new(API_SECRET.encode(), signing_input, hashlib.sha256).digest())
    token = "{}.{}.{}".format(header, payload, sig)
    _token_cache.update({"value": token, "exp": exp})
    return token


def _client() -> httpx.Client:
    token = _token()
    headers = {"Authorization": token} if token else {}
    return httpx.Client(timeout=TIMEOUT, headers=headers)


def load(query: dict) -> list[dict]:
    """Run one Cube query and return its rows."""
    with _client() as c:
        r = c.post("{}/load".format(CUBE_URL), json={"query": query})
    if r.status_code != 200:
        raise CubeError("Cube returned {}: {}".format(r.status_code, r.text[:300]))
    body = r.json()
    if "error" in body:
        raise CubeError(str(body["error"])[:300])
    return body.get("data", [])


def meta(force: bool = False) -> dict[str, Any]:
    """Cube's own model metadata. Cached for a minute."""
    global _meta_cache, _meta_at
    if force or not _meta_cache or time.time() - _meta_at > 60:
        with _client() as c:
            r = c.get("{}/meta".format(CUBE_URL))
        if r.status_code != 200:
            raise CubeError("Cube /meta returned {}".format(r.status_code))
        _meta_cache = r.json()
        _meta_at = time.time()
    return _meta_cache


def definitions(names: list[str]) -> dict[str, str]:
    """Look up the description Cube holds for each measure or dimension.

    This is what makes 'the LLM does not invent metric logic' checkable: the
    text shown next to a number is the semantic layer's own definition.
    """
    out: dict[str, str] = {}
    try:
        m = meta()
    except CubeError:
        return {n: "definition unavailable - Cube not reachable" for n in names}
    # Cube names members as "cube_name.member". Index both the qualified name
    # and the bare member so callers can ask for either.
    index: dict[str, str] = {}
    for cube in m.get("cubes", []):
        for kind in ("measures", "dimensions"):
            for item in cube.get(kind, []):
                desc = item.get("description")
                if not desc:
                    continue
                text = " ".join(desc.split())
                index[item["name"]] = text
                index.setdefault(item["name"].split(".")[-1], text)
    for n in names:
        out[n] = index.get(n, "no description published in the semantic layer")
    return out


def healthy() -> bool:
    try:
        with _client() as c:
            return c.get("{}/../../readyz".format(CUBE_URL)).status_code == 200
    except Exception:
        return False


def date_filter(member: str, start: str | None, end: str | None) -> list[dict]:
    """Cube dateRange filter for the given time dimension."""
    return [{"member": member, "operator": "inDateRange",
             "values": [start or PERIOD_START, end or PERIOD_END]}]


def eq(member: str, value: str) -> dict:
    return {"member": member, "operator": "equals", "values": [value]}


def num(v: Any, default: float = 0.0) -> float:
    """Cube returns numerics as strings over JSON."""
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
