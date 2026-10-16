from __future__ import annotations

import asyncio
import json

import pytest

from tests.conftest import SAMPLE_EVENTS
from tkbk_agents.events import EventHub, TaskEvents, sse_format, summarize_agent_event


def test_summarize_agent_events() -> None:
    out = [summarize_agent_event(e) for e in SAMPLE_EVENTS]
    assert out[0] is None  # user message is not echoed
    assert out[1] == {"kind": "action", "tool": "terminal", "text": "ls -la", "thought": "Let me look around"}
    assert out[2] == {"kind": "observation", "tool": "terminal", "text": "README.md\napp.py", "exit_code": 0}
    assert out[3]["kind"] == "action" and out[3]["tool"] == "file_editor" and out[3]["text"] == "create"
    assert out[4] == {"kind": "message", "tool": "", "text": "Added tests for foo.\nAll green."}
    assert summarize_agent_event({"kind": "AgentErrorEvent", "error": "boom"})["kind"] == "error"
    assert summarize_agent_event({"kind": "SystemPromptEvent"}) is None
    long = summarize_agent_event({"kind": "ObservationEvent", "observation": {"output": "x" * 5000}})
    assert len(long["text"]) == 2000 and long["text"].endswith("…")


def test_task_events_persist_and_replay(tmp_path) -> None:
    te = TaskEvents(tmp_path / "t.jsonl")
    te.emit("status", status="queued")
    te.emit("agent", kind="action", tool="terminal", text="ls")
    assert [e["seq"] for e in te.replay()] == [1, 2] and te.replay(1)[0]["type"] == "agent"
    again = TaskEvents(tmp_path / "t.jsonl")
    assert again.seq == 2
    again.emit("done", status="done")
    assert json.loads((tmp_path / "t.jsonl").read_text().splitlines()[-1])["seq"] == 3


@pytest.mark.asyncio
async def test_subscribe_replays_then_streams_until_close(tmp_path) -> None:
    hub = EventHub(tmp_path)
    te = hub.open("t_1")
    te.emit("status", status="queued")
    got: list[dict] = []

    async def consume() -> None:
        async for ev in te.subscribe(0):
            got.append(ev)

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    te.emit("agent", kind="action", tool="terminal", text="pytest")
    te.emit("done", status="done")
    hub.finish("t_1")
    await asyncio.wait_for(task, 2)
    assert [e["type"] for e in got] == ["status", "agent", "done"]
    assert hub.get("t_1") is None and len(hub.replay("t_1")) == 3


def test_sse_format() -> None:
    s = sse_format({"seq": 7, "type": "agent", "text": "ё"})
    assert s == 'id: 7\nevent: agent\ndata: {"seq": 7, "type": "agent", "text": "ё"}\n\n'
