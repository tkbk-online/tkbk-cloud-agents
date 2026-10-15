"""Minimal client for the OpenHands agent-server REST API (software-agent-sdk v1.55).

Endpoints used:
    GET  /health
    POST /api/conversations                      -> ConversationInfo {id, execution_status, metrics…}
    POST /api/conversations/{id}/run
    POST /api/conversations/{id}/pause
    GET  /api/conversations/{id}
    GET  /api/conversations/{id}/agent_final_response -> {response}
    GET  /api/conversations/{id}/events/search?limit=…
Auth: X-Session-API-Key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from .models import TaskMetrics

TERMINAL = {"finished", "error", "stuck"}


class AgentServerError(RuntimeError):
    pass


@dataclass
class ConversationState:
    id: str
    execution_status: str
    metrics: TaskMetrics = field(default_factory=TaskMetrics)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.execution_status in TERMINAL


def parse_metrics(info: dict[str, Any]) -> TaskMetrics:
    m = info.get("metrics") or {}
    usage = m.get("accumulated_token_usage") or {}
    return TaskMetrics(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cache_read_tokens=int(usage.get("cache_read_tokens") or 0),
        cache_write_tokens=int(usage.get("cache_write_tokens") or 0),
        cost_usd_reported=float(m.get("accumulated_cost") or 0.0),
    )


class AgentServerClient:
    def __init__(self, base_url: str, session_key: str, http: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._own = http is None
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0))
        self.headers = {"X-Session-API-Key": session_key}

    async def aclose(self) -> None:
        if self._own:
            await self.http.aclose()

    async def _req(self, method: str, path: str, **kw: Any) -> httpx.Response:
        try:
            r = await self.http.request(method, f"{self.base_url}{path}", headers=self.headers, **kw)
        except httpx.HTTPError as e:
            raise AgentServerError(f"{method} {path}: {type(e).__name__}: {e}") from e
        if r.status_code >= 400:
            raise AgentServerError(f"{method} {path}: HTTP {r.status_code}: {r.text[:300]}")
        return r

    async def start_conversation(
        self, *, llm: dict[str, Any], working_dir: str, message: str, max_iterations: int,
    ) -> ConversationState:
        body = {
            "agent_settings": {"llm": llm},
            "workspace": {"working_dir": working_dir},
            "initial_message": {"role": "user", "content": [{"type": "text", "text": message}]},
            "max_iterations": max_iterations,
            "stuck_detection": True,
            "confirmation_policy": {"kind": "NeverConfirm"},
            "autotitle": False,
        }
        r = await self._req("POST", "/api/conversations", json=body, timeout=120.0)
        info = r.json()
        return ConversationState(id=str(info["id"]), execution_status=str(info.get("execution_status", "idle")),
                                 metrics=parse_metrics(info), raw=info)

    async def run(self, conversation_id: str) -> None:
        await self._req("POST", f"/api/conversations/{conversation_id}/run")

    async def pause(self, conversation_id: str) -> None:
        try:
            await self._req("POST", f"/api/conversations/{conversation_id}/pause")
        except AgentServerError:
            pass

    async def state(self, conversation_id: str) -> ConversationState:
        r = await self._req("GET", f"/api/conversations/{conversation_id}")
        info = r.json()
        return ConversationState(id=conversation_id, execution_status=str(info.get("execution_status", "")),
                                 metrics=parse_metrics(info), raw=info)

    async def final_response(self, conversation_id: str) -> str:
        try:
            r = await self._req("GET", f"/api/conversations/{conversation_id}/agent_final_response")
        except AgentServerError:
            return ""
        return str(r.json().get("response") or "")

    async def count_actions(self, conversation_id: str) -> int:
        """Rough iteration count: number of action events."""
        try:
            r = await self._req("GET", f"/api/conversations/{conversation_id}/events/count",
                                params={"kind": "ActionEvent"})
            data = r.json()
            return int(data if isinstance(data, int) else data.get("count", 0))
        except (AgentServerError, ValueError, AttributeError):
            return 0
