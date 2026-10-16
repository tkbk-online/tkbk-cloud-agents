"""Client license check against the site: POST https://tkbk.online/api/license/check.

Request  { apiKey, deviceId, app, version, label? }
Answer   { ok, license: { sub, plan, until, features[], limits{cloudAgentHours,…}, via, revoked, … },
           signature (Ed25519, base64, over the canonical JSON of `license`), publicKeyId }

Cloud agents need the `cloud_agents` feature (Pro / Teams / Teams+). The monthly quota is
`limits.cloudAgentHours` (Pro 20, Teams 100, Teams+ 300 as of src/lib/license.ts on the site).

Modes: `site` (default) asks the site; `stub` grants every well-formed cr_ key a configurable plan
without any network — for local runs only, and the server logs it loudly at startup.
The key is never logged or stored: the cache is keyed by key_id = sha256(key)[:24].
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger("tkbk.license")

CLOUD_FEATURE = "cloud_agents"
PLAN_HOURS_FALLBACK = {"free": 0, "pro": 20, "team": 100, "team_plus": 300}


@dataclass(frozen=True)
class License:
    ok: bool
    plan: str = "free"
    sub: str = ""
    hours: int = 0
    until: str | None = None
    via: str = "own"
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def minutes(self) -> int:
        return self.hours * 60


def canonical_json(value: Any) -> str:
    """Same bytes as licenseSign.ts: keys sorted at every level, no spaces, JSON.stringify escaping."""
    if isinstance(value, list):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, dict):
        items = sorted((k, v) for k, v in value.items() if v is not None)
        return "{" + ",".join(f"{json.dumps(k, ensure_ascii=False)}:{canonical_json(v)}" for k, v in items) + "}"
    if isinstance(value, bool) or value is None:
        return json.dumps(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return json.dumps(value, ensure_ascii=False)


def verify_signature(payload: dict[str, Any], signature: str, public_key_b64: str) -> bool:
    """Ed25519 over canonical JSON. Never raises; False when `cryptography` is missing."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:  # pragma: no cover
        log.error("cryptography is not installed; cannot verify license signatures")
        return False
    try:
        raw = base64.b64decode(public_key_b64.strip(), validate=True)
        sig = base64.b64decode(signature, validate=True)
        if len(raw) != 32 or len(sig) != 64:
            return False
        Ed25519PublicKey.from_public_bytes(raw).verify(sig, canonical_json(payload).encode())
        return True
    except (ValueError, InvalidSignature):
        return False


def _parse_license(lic: dict[str, Any]) -> License:
    plan = str(lic.get("plan") or "free")
    features = lic.get("features") or []
    limits = lic.get("limits") or {}
    until = lic.get("until")
    hours = limits.get("cloudAgentHours")
    if hours is None:
        hours = PLAN_HOURS_FALLBACK.get(plan, 0)
    expired = False
    if until:
        try:
            expired = datetime.fromisoformat(str(until).replace("Z", "+00:00")) < datetime.now(UTC)
        except ValueError:
            expired = False
    if lic.get("revoked"):
        return License(False, plan=plan, sub=str(lic.get("sub") or ""), error="license revoked", raw=lic)
    if CLOUD_FEATURE not in features or int(hours) <= 0:
        return License(False, plan=plan, sub=str(lic.get("sub") or ""),
                       error=f"plan {plan} does not include cloud agents", raw=lic)
    if expired:
        return License(False, plan=plan, sub=str(lic.get("sub") or ""), error="plan expired", raw=lic)
    return License(True, plan=plan, sub=str(lic.get("sub") or ""), hours=int(hours), until=str(until) if until else None,
                   via=str(lic.get("via") or "own"), raw=lic)


class LicenseClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s = settings
        self.http = http
        self._cache: dict[str, tuple[float, License]] = {}
        self.device_id = "cloud-" + hashlib.sha256(settings.public_base_url.encode()).hexdigest()[:24]

    @staticmethod
    def key_id(api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()[:24]

    async def check(self, api_key: str) -> License:
        kid = self.key_id(api_key)
        hit = self._cache.get(kid)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        lic = await self._fetch(api_key, kid)
        ttl = self.s.license_cache_seconds if lic.ok else min(60.0, self.s.license_cache_seconds)
        self._cache[kid] = (time.monotonic() + ttl, lic)
        return lic

    def forget(self, api_key: str) -> None:
        self._cache.pop(self.key_id(api_key), None)

    async def _fetch(self, api_key: str, kid: str) -> License:
        if self.s.license_mode == "stub":
            plan = self.s.stub_plan
            hours = PLAN_HOURS_FALLBACK.get(plan, 20)
            return License(True, plan=plan, sub=kid, hours=hours, via="stub",
                           raw={"plan": plan, "features": [CLOUD_FEATURE], "limits": {"cloudAgentHours": hours}})
        body = {"apiKey": api_key, "deviceId": self.device_id, "app": self.s.license_app,
                "version": self.s.version, "label": "TKBK Cloud Agents"}
        try:
            r = await self.http.post(self.s.license_check_url, json=body, timeout=20)
        except httpx.HTTPError as e:
            return License(False, error=f"license service unreachable: {type(e).__name__}")
        if r.status_code == 404:
            return License(False, error="key not found")
        if r.status_code == 429:
            return License(False, error="license service rate limit, retry later")
        if r.status_code != 200:
            return License(False, error=f"license service HTTP {r.status_code}")
        try:
            data = r.json()
        except ValueError:
            return License(False, error="license service: bad JSON")
        lic = data.get("license") if isinstance(data, dict) else None
        if not data.get("ok") or not isinstance(lic, dict):
            return License(False, error=f"license service: {data.get('error', 'no license')}")
        if self.s.license_public_key:
            if not verify_signature(lic, str(data.get("signature") or ""), self.s.license_public_key):
                return License(False, error="license signature invalid")
        return _parse_license(lic)


def concurrency_for(settings: Settings, plan: str) -> int:
    return settings.concurrent_per_plan.get(plan, settings.concurrent_default)
