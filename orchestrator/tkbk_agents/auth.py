"""HMAC request signing — the same scheme as the bot's internal API (ea_internal.py).

Headers: X-TKBK-Ts (unix seconds) and X-TKBK-Sig = hex HMAC-SHA256(secret,
"{ts}\\n{METHOD}\\n{path_with_query}\\n{raw_body}"). path_with_query is the request
target exactly as sent (raw, URL-encoded); raw_body is the exact body bytes ("" for GET).
|now - ts| > 300 s or a bad signature -> 401; no secret (or shorter than 32 chars) -> 503.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import deque

from fastapi import HTTPException, Request

from .config import Settings


def signature(secret: str, ts: str, method: str, path_qs: str, body: bytes) -> str:
    msg = f"{ts}\n{method.upper()}\n{path_qs}\n".encode() + body
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def sign_headers(secret: str, method: str, path_qs: str, body: bytes, ts: int | None = None) -> dict[str, str]:
    """Headers a client adds to a request (used by the SDK stubs, tests and callbacks)."""
    ts_s = str(int(time.time()) if ts is None else ts)
    return {"X-TKBK-Ts": ts_s, "X-TKBK-Sig": signature(secret, ts_s, method, path_qs, body)}


class RateLimiter:
    """Total signed requests per minute (sliding window)."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        while self._hits and now - self._hits[0] > 60:
            self._hits.popleft()
        if len(self._hits) >= self.per_minute:
            return False
        self._hits.append(now)
        return True


def path_with_query(request: Request) -> str:
    raw_path = request.scope.get("raw_path")
    path = raw_path.decode() if raw_path else request.url.path
    qs = request.scope.get("query_string", b"")
    return f"{path}?{qs.decode()}" if qs else path


class HmacAuth:
    """FastAPI dependency: `Depends(HmacAuth(settings))`."""

    def __init__(self, settings: Settings, limiter: RateLimiter | None = None):
        self.settings = settings
        self.limiter = limiter or RateLimiter(settings.rate_per_minute)

    async def __call__(self, request: Request) -> None:
        if not self.settings.secret_ok:
            raise HTTPException(503, "internal API secret is not configured")
        ts = request.headers.get("X-TKBK-Ts", "")
        sig = request.headers.get("X-TKBK-Sig", "")
        if not ts.isdigit() or len(ts) > 12 or abs(time.time() - int(ts)) > self.settings.hmac_max_skew_s:
            raise HTTPException(401, "bad or stale timestamp")
        body = await request.body()
        if len(body) > self.settings.max_body_bytes:
            raise HTTPException(413, "body too large")
        expected = signature(self.settings.internal_secret, ts, request.method, path_with_query(request), body)
        if not hmac.compare_digest(expected, sig.lower()):
            raise HTTPException(401, "bad signature")
        if not self.limiter.allow():
            raise HTTPException(429, "rate limit")
