"""A username and password gate for the public demo.

Deliberately small. There is no user store, no registration and no password
reset: one shared credential, held in the environment, checked in constant time,
and carried in a signed cookie. That is the right amount of security for a demo
that must not be readable by anyone who finds the URL, and no more.

If DEMO_USER and DEMO_PASSWORD are unset the gate is off, so local development
is unchanged.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

USER = os.getenv("DEMO_USER", "")
PASSWORD = os.getenv("DEMO_PASSWORD", "")
SECRET = os.getenv("SESSION_SECRET", "") or secrets.token_hex(32)
COOKIE = "copilot_session"
TTL_SECONDS = 12 * 60 * 60

# Cross-site cookies (a Vercel frontend calling this backend) must be Secure and
# SameSite=None. Same-origin use is happier with Lax, so it is configurable.
SECURE = os.getenv("COOKIE_SECURE", "1") not in ("0", "false", "False", "")
SAMESITE = "none" if os.getenv("CORS_ORIGINS") else "lax"

# Anything needed to render or pass the login screen itself.
OPEN_PATHS = ("/health", "/login", "/logout", "/favicon.ico")
OPEN_PREFIXES = ("/static/assets/",)


def enabled() -> bool:
    return bool(USER and PASSWORD)


def _sign(payload: bytes) -> str:
    return hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()


def issue() -> str:
    """A cookie value that proves a password was presented, and when."""
    body = json.dumps({"u": USER, "exp": int(time.time()) + TTL_SECONDS}).encode()
    b64 = base64.urlsafe_b64encode(body).decode().rstrip("=")
    return "{}.{}".format(b64, _sign(body))


def valid(token: str | None) -> bool:
    if not token or "." not in token:
        return False
    b64, sig = token.rsplit(".", 1)
    try:
        body = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
        claims = json.loads(body)
    except Exception:                                          # noqa: BLE001
        return False
    if not hmac.compare_digest(sig, _sign(body)):
        return False
    return claims.get("u") == USER and claims.get("exp", 0) > time.time()


def check_credentials(user: str, password: str) -> bool:
    """Constant time on both fields, so neither can be probed by timing."""
    ok_user = hmac.compare_digest((user or "").encode(), USER.encode())
    ok_pass = hmac.compare_digest((password or "").encode(), PASSWORD.encode())
    return ok_user and ok_pass


def is_open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(OPEN_PREFIXES)
