from __future__ import annotations

import httpx
import pytest

from tests.conftest import KEY
from tkbk_agents.models import Provider
from tkbk_agents.tkbk import check_key, infer_provider, key_id, key_prefix, llm_settings


def test_key_id_is_stable_and_not_reversible() -> None:
    assert key_id(KEY) == key_id(KEY) and len(key_id(KEY)) == 24 and KEY[3:] not in key_id(KEY)
    assert key_prefix(KEY) == "cr_abcd…"


def test_provider_inference() -> None:
    assert infer_provider("claude-sonnet-5", None) is Provider.ANTHROPIC
    assert infer_provider("Claude-Opus-5", None) is Provider.ANTHROPIC
    assert infer_provider("gpt-5.5", None) is Provider.OPENAI
    assert infer_provider("deepseek-v4", None) is Provider.OPENAI
    assert infer_provider("claude-sonnet-5", Provider.OPENAI) is Provider.OPENAI


def test_llm_settings_routes_to_tkbk_formats(settings) -> None:
    a = llm_settings(settings, "claude-sonnet-5", Provider.ANTHROPIC, KEY)
    assert a["model"] == "anthropic/claude-sonnet-5" and a["base_url"] == "https://api.tkbk.online"
    o = llm_settings(settings, "glm-5.3", Provider.OPENAI, KEY)
    assert o["model"] == "openai/glm-5.3" and o["base_url"] == "https://api.tkbk.online/openai/v1"
    assert a["api_key"] == KEY and a["native_tool_calling"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status,ok,err", [(200, True, None), (401, False, "rejected"), (503, False, "gateway error")])
async def test_check_key(settings, status: int, ok: bool, err: str | None) -> None:
    seen: dict = {}

    def h(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("Authorization")
        return httpx.Response(status, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
        r = await check_key(settings, KEY, c)
    assert r.ok is ok and (err is None or err in (r.error or ""))
    assert seen["auth"] == f"Bearer {KEY}"


@pytest.mark.asyncio
async def test_check_key_unreachable_and_disabled(tmp_path) -> None:
    from tests.conftest import make_settings

    def boom(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    async with httpx.AsyncClient(transport=httpx.MockTransport(boom)) as c:
        r = await check_key(make_settings(tmp_path), KEY, c)
        assert not r.ok and "unreachable" in (r.error or "")
        r2 = await check_key(make_settings(tmp_path, TKBK_KEY_CHECK="0"), KEY, c)
        assert r2.ok
