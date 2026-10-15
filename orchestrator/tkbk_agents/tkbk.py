"""TKBK gateway adapter: key hygiene, key pre-check, LLM settings for the agent-server."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import httpx

from .config import Settings
from .models import KEY_RE, Provider


def key_id(api_key: str) -> str:
    """Stable, non-reversible id for billing rows: sha256(key)[:24]."""
    return hashlib.sha256(api_key.encode()).hexdigest()[:24]


def key_prefix(api_key: str) -> str:
    return api_key[:7] + "…"


def infer_provider(model: str, explicit: Provider | None) -> Provider:
    if explicit is not None:
        return explicit
    return Provider.ANTHROPIC if model.lower().startswith("claude") else Provider.OPENAI


def llm_settings(settings: Settings, model: str, provider: Provider, api_key: str) -> dict:
    """`agent_settings.llm` for POST /api/conversations (litellm naming).

    anthropic/<model> + base_url https://api.tkbk.online -> litellm appends /v1/messages.
    openai/<model>    + base_url https://api.tkbk.online/openai/v1 -> /chat/completions.
    """
    if provider is Provider.ANTHROPIC:
        litellm_model, base_url = f"anthropic/{model}", settings.anthropic_base_url
    else:
        litellm_model, base_url = f"openai/{model}", settings.openai_base_url
    return {
        "usage_id": "agent",
        "model": litellm_model,
        "base_url": base_url,
        "api_key": api_key,
        "native_tool_calling": True,
        "caching_prompt": True,
        "num_retries": 4,
        "timeout": 300,
    }


@dataclass(frozen=True)
class KeyCheck:
    ok: bool
    error: str | None = None


async def check_key(settings: Settings, api_key: str, http: httpx.AsyncClient) -> KeyCheck:
    """GET models with the client's key: 401/403 -> the key is invalid or disabled."""
    if not KEY_RE.match(api_key):
        return KeyCheck(False, "bad key format")
    if not settings.key_check:
        return KeyCheck(True)
    try:
        r = await http.get(settings.models_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=15)
    except httpx.HTTPError as e:
        return KeyCheck(False, f"gateway unreachable: {type(e).__name__}")
    if r.status_code in (401, 403):
        return KeyCheck(False, "key rejected by the TKBK gateway")
    if r.status_code >= 500:
        return KeyCheck(False, f"gateway error {r.status_code}")
    return KeyCheck(True)
