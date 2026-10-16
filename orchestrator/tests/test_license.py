from __future__ import annotations

import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.conftest import KEY, FakeAgentServer, make_settings
from tkbk_agents.license import (
    LicenseClient,
    _parse_license,
    canonical_json,
    concurrency_for,
    verify_signature,
)


def test_canonical_json_matches_license_sign_ts() -> None:
    # keys sorted at every level, no spaces, undefined (None) dropped, JSON.stringify escaping
    v = {"b": [3, {"z": True, "a": None}], "a": "ё \"q\"", "n": 1.0}
    assert canonical_json(v) == '{"a":"ё \\"q\\"","b":[3,{"z":true}],"n":1}'


def test_ed25519_roundtrip_with_raw_public_key() -> None:
    priv = Ed25519PrivateKey.generate()
    raw_pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub_b64 = base64.b64encode(raw_pub).decode()
    payload = {"plan": "pro", "sub": "abc", "features": ["cloud_agents"], "limits": {"cloudAgentHours": 20}}
    sig = base64.b64encode(priv.sign(canonical_json(payload).encode())).decode()
    assert verify_signature(payload, sig, pub_b64)
    assert not verify_signature({**payload, "plan": "team"}, sig, pub_b64)
    assert not verify_signature(payload, "AAAA", pub_b64)
    assert not verify_signature(payload, sig, "not-base64!!")


@pytest.mark.parametrize("lic,ok,err", [
    ({"plan": "pro", "features": ["cloud_agents"], "limits": {"cloudAgentHours": 20}}, True, None),
    ({"plan": "free", "features": [], "limits": {"cloudAgentHours": 0}}, False, "does not include"),
    ({"plan": "pro", "features": ["cloud_agents"], "limits": {"cloudAgentHours": 20}, "revoked": True}, False, "revoked"),
    ({"plan": "pro", "features": ["cloud_agents"], "limits": {"cloudAgentHours": 20},
      "until": "2020-01-01T00:00:00.000Z"}, False, "expired"),
    ({"plan": "team_plus", "features": ["cloud_agents"]}, True, None),  # limits missing -> plan fallback (300 h)
])
def test_parse_license(lic: dict, ok: bool, err: str | None) -> None:
    out = _parse_license(lic)
    assert out.ok is ok and (err is None or err in (out.error or ""))
    if lic["plan"] == "team_plus":
        assert out.hours == 300


@pytest.mark.asyncio
async def test_client_caches_and_never_logs_key(tmp_path, fake_server: FakeAgentServer, caplog) -> None:
    settings = make_settings(tmp_path, LICENSE_CACHE_SECONDS="600")
    async with fake_server.client() as http:
        lc = LicenseClient(settings, http)
        a = await lc.check(KEY)
        b = await lc.check(KEY)
        assert a.ok and a.plan == "pro" and a.hours == 20 and b is a
        assert len(fake_server.license_requests) == 1
        lc.forget(KEY)
        await lc.check(KEY)
        assert len(fake_server.license_requests) == 2
    assert KEY not in caplog.text


@pytest.mark.asyncio
async def test_client_verifies_signature_when_public_key_set(tmp_path) -> None:
    priv = Ed25519PrivateKey.generate()
    raw_pub = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    settings = make_settings(tmp_path, LICENSE_PUBLIC_KEY=base64.b64encode(raw_pub).decode())
    lic = {"plan": "pro", "sub": "s", "features": ["cloud_agents"], "limits": {"cloudAgentHours": 20}, "revoked": False}

    def h(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        good = body.get("label") != "tamper"
        sig = base64.b64encode(priv.sign(canonical_json(lic).encode())).decode()
        payload = lic if good else {**lic, "plan": "team_plus"}
        return httpx.Response(200, json={"ok": True, "license": payload, "signature": sig})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http:
        out = await LicenseClient(settings, http).check(KEY)
        assert out.ok and out.plan == "pro"
        settings2 = make_settings(tmp_path / "2", LICENSE_PUBLIC_KEY=base64.b64encode(raw_pub).decode())
        lc = LicenseClient(settings2, http)
        lc.s = settings2  # same key; tamper the payload by changing the label the server echoes back
        # emulate a tampered answer: the server signs the original but returns a different payload
        async def tampered(req):
            return h(httpx.Request("POST", req.url, json={"label": "tamper"}))
        http2 = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: h(httpx.Request("POST", r.url, json={"label": "tamper"}))))
        bad = await LicenseClient(settings2, http2).check(KEY)
        assert not bad.ok and "signature" in (bad.error or "")
        await http2.aclose()


@pytest.mark.asyncio
async def test_client_error_paths(tmp_path, fake_server: FakeAgentServer) -> None:
    settings = make_settings(tmp_path)
    async with fake_server.client() as http:
        fake_server.license_status = 404
        assert "not found" in (await LicenseClient(settings, http).check(KEY)).error
        fake_server.license_status = 429
        assert "rate limit" in (await LicenseClient(settings, http).check(KEY)).error

    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as http:
        assert "unreachable" in (await LicenseClient(settings, http).check(KEY)).error


def test_concurrency_per_plan(tmp_path) -> None:
    s = make_settings(tmp_path, CONCURRENT_PER_PLAN="pro:3,team:5", CONCURRENT_DEFAULT="2")
    assert concurrency_for(s, "pro") == 3 and concurrency_for(s, "team") == 5 and concurrency_for(s, "team_plus") == 2
