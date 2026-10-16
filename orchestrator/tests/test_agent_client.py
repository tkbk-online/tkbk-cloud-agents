from __future__ import annotations

import httpx
import pytest

from tests.conftest import FakeAgentServer
from tkbk_agents.agent_client import AgentServerClient, AgentServerError, parse_metrics


@pytest.mark.asyncio
async def test_conversation_roundtrip(fake_server: FakeAgentServer) -> None:
    async with fake_server.client() as http:
        c = AgentServerClient("http://tkbk-sbx-x:8000", "sess", http=http)
        st = await c.start_conversation(llm={"model": "anthropic/claude-sonnet-5", "api_key": "k"},
                                        working_dir="/workspace/repo", message="hi", max_iterations=42)
        assert st.id.startswith("c0ffee") and st.execution_status == "idle"
        body = fake_server.conv_requests[0]
        assert body["max_iterations"] == 42 and body["confirmation_policy"] == {"kind": "NeverConfirm"}
        assert body["initial_message"]["content"][0]["text"] == "hi"
        assert body["workspace"] == {"working_dir": "/workspace/repo"}
        await c.run(st.id)
        s1 = await c.state(st.id)
        s2 = await c.state(st.id)
        assert (s1.execution_status, s2.execution_status) == ("running", "finished") and s2.terminal
        assert s2.metrics.prompt_tokens == 1000 and s2.metrics.cost_usd_reported == pytest.approx(0.0123)
        assert (await c.final_response(st.id)).startswith("Added tests")
        assert await c.count_actions(st.id) == 7
        assert fake_server.session_keys_seen == {"sess"}


@pytest.mark.asyncio
async def test_errors_are_wrapped() -> None:
    def h(req: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="capacity")

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http:
        c = AgentServerClient("http://tkbk-sbx-x:8000", "s", http=http)
        with pytest.raises(AgentServerError, match="429"):
            await c.run("cid")
        assert await c.final_response("cid") == ""   # swallowed on purpose

    def h409(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": "Conversation already running."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(h409)) as http:
        await AgentServerClient("http://tkbk-sbx-x:8000", "s", http=http).run("cid")  # already running: fine


def test_parse_metrics_is_lenient() -> None:
    assert parse_metrics({}).prompt_tokens == 0
    m = parse_metrics({"metrics": {"accumulated_cost": None, "accumulated_token_usage": {"prompt_tokens": "5"}}})
    assert m.prompt_tokens == 5 and m.cost_usd_reported == 0.0
