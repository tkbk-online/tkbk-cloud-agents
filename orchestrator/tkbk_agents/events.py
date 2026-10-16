"""Task event log: compact agent events persisted as JSONL + in-process pub/sub for SSE.

Event shape (what the IDE receives):
    {"seq": 12, "ts": "...", "type": "status"|"agent"|"log"|"done",
     "status": "...", "phase": "...",             # for status/done
     "kind": "action"|"observation"|"message"|"error", "tool": "terminal", "text": "...",  # for agent
     "message": "..."}                            # for log
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_TEXT = 2000


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _trim(s: Any, n: int = MAX_TEXT) -> str:
    s = "" if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def summarize_agent_event(ev: dict[str, Any]) -> dict[str, Any] | None:
    """Compact view of an agent-server event; None for events the IDE does not need."""
    kind = str(ev.get("kind") or "")
    source = str(ev.get("source") or "")
    if kind == "ActionEvent":
        action = ev.get("action") or {}
        akind = str(action.get("kind") or "")
        text = action.get("command") or action.get("path") or action.get("message") or ""
        if not text:
            text = json.dumps({k: v for k, v in action.items() if k != "kind"}, ensure_ascii=False)
        out = {"kind": "action", "tool": str(ev.get("tool_name") or akind), "text": _trim(text)}
        thought = ev.get("thought") or []
        if isinstance(thought, list) and thought:
            t = thought[0].get("text") if isinstance(thought[0], dict) else None
            if t:
                out["thought"] = _trim(t, 500)
        return out
    if kind == "ObservationEvent":
        obs = ev.get("observation") or {}
        text = obs.get("output") or obs.get("content") or obs.get("text") or ""
        if isinstance(text, list):
            text = " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in text)
        exit_code = obs.get("exit_code")
        out = {"kind": "observation", "tool": str(ev.get("tool_name") or obs.get("kind") or ""), "text": _trim(text)}
        if exit_code is not None:
            out["exit_code"] = exit_code
        return out
    if kind == "AgentErrorEvent":
        return {"kind": "error", "tool": str(ev.get("tool_name") or ""), "text": _trim(ev.get("error"))}
    if kind == "MessageEvent" and source == "agent":
        msg = ev.get("llm_message") or {}
        content = msg.get("content") or []
        text = " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content) \
            if isinstance(content, list) else str(content)
        return {"kind": "message", "tool": "", "text": _trim(text)} if text.strip() else None
    return None


class TaskEvents:
    """Append-only JSONL log for one task + fan-out to live subscribers."""

    def __init__(self, path: Path):
        self.path = path
        self.seq = 0
        self._subs: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self.closed = False
        if path.exists():
            try:
                self.seq = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
            except OSError:
                self.seq = 0

    def emit(self, type_: str, **fields: Any) -> dict[str, Any]:
        self.seq += 1
        ev = {"seq": self.seq, "ts": _now(), "type": type_, **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        for q in list(self._subs):
            q.put_nowait(ev)
        return ev

    def close(self) -> None:
        self.closed = True
        for q in list(self._subs):
            q.put_nowait(None)

    def replay(self, after_seq: int = 0) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if int(ev.get("seq", 0)) > after_seq:
                out.append(ev)
        return out

    async def subscribe(self, after_seq: int = 0) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._subs.add(q)
        try:
            last = after_seq
            for ev in self.replay(after_seq):
                last = ev["seq"]
                yield ev
            if self.closed:
                return
            while True:
                ev = await q.get()
                if ev is None:
                    return
                if ev["seq"] > last:
                    last = ev["seq"]
                    yield ev
        finally:
            self._subs.discard(q)


class EventHub:
    """Registry of live TaskEvents by task id (the runner writes, SSE handlers read)."""

    def __init__(self, root: Path):
        self.root = root
        self._live: dict[str, TaskEvents] = {}

    def path(self, task_id: str) -> Path:
        return self.root / f"{task_id}.events.jsonl"

    def open(self, task_id: str) -> TaskEvents:
        te = self._live.get(task_id)
        if te is None:
            te = TaskEvents(self.path(task_id))
            self._live[task_id] = te
        return te

    def get(self, task_id: str) -> TaskEvents | None:
        return self._live.get(task_id)

    def finish(self, task_id: str) -> None:
        te = self._live.pop(task_id, None)
        if te:
            te.close()

    def replay(self, task_id: str, after_seq: int = 0) -> list[dict[str, Any]]:
        te = self._live.get(task_id)
        return te.replay(after_seq) if te else TaskEvents(self.path(task_id)).replay(after_seq)


def sse_format(ev: dict[str, Any]) -> str:
    return f"id: {ev.get('seq', 0)}\nevent: {ev.get('type', 'message')}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
