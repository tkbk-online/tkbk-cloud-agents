from __future__ import annotations

import time

import pytest
from fastapi import FastAPI, HTTPException, Request
from starlette.testclient import TestClient

from tests.conftest import SECRET, make_settings
from tkbk_agents.auth import HmacAuth, RateLimiter, sign_headers, signature


def test_signature_matches_bot_scheme() -> None:
    # Same formula as app/ea_internal.py in the bot: "{ts}\n{METHOD}\n{path_qs}\n" + body
    sig = signature("s3cret", "1700000000", "post", "/v1/tasks?x=1", b'{"a":1}')
    assert sig == signature("s3cret", "1700000000", "POST", "/v1/tasks?x=1", b'{"a":1}')
    assert len(sig) == 64 and sig != signature("s3cret", "1700000001", "POST", "/v1/tasks?x=1", b'{"a":1}')


def _app(tmp_path, **over) -> FastAPI:
    settings = make_settings(tmp_path, **over)
    auth = HmacAuth(settings, RateLimiter(3))
    app = FastAPI()

    @app.post("/signed")
    async def signed(request: Request):
        await auth(request)
        return {"ok": True}

    return app


def test_hmac_accepts_valid_and_rejects_bad(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        body = b'{"x":1}'
        h = sign_headers(SECRET, "POST", "/signed", body)
        assert c.post("/signed", content=body, headers=h).status_code == 200
        # tampered body
        assert c.post("/signed", content=b'{"x":2}', headers=h).status_code == 401
        # stale timestamp
        old = sign_headers(SECRET, "POST", "/signed", body, ts=int(time.time()) - 600)
        assert c.post("/signed", content=body, headers=old).status_code == 401
        # wrong secret
        bad = sign_headers("other-secret-0123456789abcdef0123456789", "POST", "/signed", body)
        assert c.post("/signed", content=body, headers=bad).status_code == 401
        # rate limit: 3 per minute total, we already used 1 valid + 3 invalid (invalid don't count)
        assert c.post("/signed", content=body, headers=sign_headers(SECRET, "POST", "/signed", body)).status_code == 200
        assert c.post("/signed", content=body, headers=sign_headers(SECRET, "POST", "/signed", body)).status_code == 200
        assert c.post("/signed", content=body, headers=sign_headers(SECRET, "POST", "/signed", body)).status_code == 429


def test_hmac_without_secret_is_503(tmp_path) -> None:
    app = _app(tmp_path, AGENTS_INTERNAL_SECRET="short")
    with TestClient(app) as c:
        r = c.post("/signed", content=b"{}", headers=sign_headers("short", "POST", "/signed", b"{}"))
        assert r.status_code == 503


def test_hmac_query_string_is_part_of_signature(tmp_path) -> None:
    app = _app(tmp_path)
    with TestClient(app) as c:
        h = sign_headers(SECRET, "POST", "/signed?a=1", b"")
        assert c.post("/signed?a=1", headers=h).status_code == 200
        assert c.post("/signed?a=2", headers=h).status_code == 401


def test_rate_limiter_window() -> None:
    rl = RateLimiter(2)
    assert rl.allow(now=0) and rl.allow(now=1) and not rl.allow(now=2)
    assert rl.allow(now=61)


@pytest.mark.asyncio
async def test_hmac_body_too_large(tmp_path) -> None:
    settings = make_settings(tmp_path)
    auth = HmacAuth(settings)
    big = b"x" * (settings.max_body_bytes + 1)

    class R:  # minimal Request stand-in
        method = "POST"
        headers = sign_headers(SECRET, "POST", "/x", big)
        scope = {"raw_path": b"/x", "query_string": b""}

        async def body(self):
            return big

    with pytest.raises(HTTPException) as e:
        await auth(R())  # type: ignore[arg-type]
    assert e.value.status_code == 413
