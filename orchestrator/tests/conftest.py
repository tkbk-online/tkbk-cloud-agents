from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from tkbk_agents.config import Settings
from tkbk_agents.events import EventHub
from tkbk_agents.models import TaskResult, TaskStatus, TaskView
from tkbk_agents.store import TaskSecrets, TaskStore

SECRET = "unit-test-secret-0123456789abcdef0123456789"
KEY = "cr_abcdefghijklmnop1234567890"
KEY2 = "cr_zzzzzzzzzzzzzzzz0987654321"


def make_settings(tmp_path: Path, **over: Any) -> Settings:
    env = {
        "AGENTS_INTERNAL_SECRET": SECRET,
        "DATA_DIR": str(tmp_path / "data"),
        "TASKS_ROOT": str(tmp_path / "tasks"),
        "PUBLIC_BASE_URL": "https://agents.test",
        "TKBK_KEY_CHECK": "1",
        "LICENSE_MODE": "site",
        "LICENSE_CHECK_URL": "https://tkbk.online/api/license/check",
        "LICENSE_CACHE_SECONDS": "0",
        "MAX_CONCURRENT_TASKS": "4",
        "POLL_INTERVAL": "0",
        "SANDBOX_STARTUP_TIMEOUT": "5",
        "SANDBOX_IMAGE": "ghcr.io/openhands/agent-server:test-python",
    }
    env.update({k: str(v) for k, v in over.items()})
    return Settings.from_env(env)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def store(settings: Settings) -> TaskStore:
    s = TaskStore(settings.db_path)
    yield s
    s.close()


@pytest.fixture
def hub(settings: Settings) -> EventHub:
    return EventHub(settings.events_dir)


# ----------------------------------------------------------------- fake docker
@dataclass
class FakeDocker:
    calls: list[list[str]] = field(default_factory=list)
    running: str = "true"
    fail_on: Callable[[list[str]], str | None] | None = None

    async def __call__(self, *args: str, timeout: float = 120.0, **_: Any) -> str:
        from tkbk_agents.docker_cli import DockerError

        a = list(args)
        self.calls.append(a)
        if self.fail_on:
            msg = self.fail_on(a)
            if msg:
                raise DockerError(a, 1, msg)
        if a[:2] == ["inspect", "-f"]:
            return self.running + "\n"
        if a[:1] == ["run"]:
            return "deadbeefcafe\n"
        if a[:2] == ["ps", "-aq"] or a[:3] == ["network", "ls", "-q"]:
            return ""
        return ""

    def has(self, *prefix: str) -> bool:
        return any(c[: len(prefix)] == list(prefix) for c in self.calls)


SAMPLE_EVENTS = [
    {"id": "e1", "kind": "MessageEvent", "source": "user", "llm_message": {"content": [{"type": "text", "text": "task"}]}},
    {"id": "e2", "kind": "ActionEvent", "source": "agent", "tool_name": "terminal",
     "thought": [{"type": "text", "text": "Let me look around"}],
     "action": {"kind": "TerminalAction", "command": "ls -la"}},
    {"id": "e3", "kind": "ObservationEvent", "source": "environment", "tool_name": "terminal",
     "observation": {"kind": "TerminalObservation", "output": "README.md\napp.py", "exit_code": 0}},
    {"id": "e4", "kind": "ActionEvent", "source": "agent", "tool_name": "file_editor",
     "action": {"kind": "FileEditorAction", "command": "create", "path": "/workspace/repo/test_app.py"}},
    {"id": "e5", "kind": "MessageEvent", "source": "agent",
     "llm_message": {"content": [{"type": "text", "text": "Added tests for foo.\nAll green."}]}},
]


# ------------------------------------------------------------ fake agent-server
@dataclass
class FakeAgentServer:
    """One httpx.MockTransport for: the sandbox, GitHub, the TKBK models endpoint and the site's license check."""

    statuses: list[str] = field(default_factory=lambda: ["running", "finished"])
    final: str = "Added tests for foo.\nAll green."
    key_ok: bool = True
    plan: str = "pro"                       # what the site answers for every key
    hours: int | None = None                # None -> plan default
    license_status: int = 200               # e.g. 404 key_not_found, 503
    github_issue: dict[str, Any] | None = None
    pr_calls: list[dict[str, Any]] = field(default_factory=list)
    conv_requests: list[dict[str, Any]] = field(default_factory=list)
    license_requests: list[dict[str, Any]] = field(default_factory=list)
    session_keys_seen: set[str] = field(default_factory=set)
    events: list[dict[str, Any]] = field(default_factory=lambda: list(SAMPLE_EVENTS))
    _i: int = 0

    def metrics(self) -> dict[str, Any]:
        return {"accumulated_cost": 0.0123,
                "accumulated_token_usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                                            "cache_read_tokens": 500, "cache_write_tokens": 50}}

    def license_payload(self) -> dict[str, Any]:
        hours = self.hours if self.hours is not None else {"free": 0, "pro": 20, "team": 100, "team_plus": 300}[self.plan]
        features = [] if self.plan == "free" else ["skills_hub", "premium_skills", "cloud_agents", "priority_support"]
        return {"v": 1, "sub": "sub-" + self.plan, "plan": self.plan, "until": "2099-01-01T00:00:00.000Z",
                "features": features, "limits": {"devices": 3, "cloudAgentHours": hours, "keys": 1},
                "via": "own", "revoked": False, "deviceId": "cloud-x", "deviceOk": True, "devices": 1,
                "app": "tkbk-cloud-agents", "issuedAt": "2026-10-16T00:00:00.000Z", "expiresAt": "2026-10-24T00:00:00.000Z"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "api.tkbk.online":
            return httpx.Response(200 if self.key_ok else 401, json={"data": []})
        if host == "tkbk.online" and path == "/api/license/check":
            body = json.loads(request.content)
            self.license_requests.append(body)
            if self.license_status != 200:
                return httpx.Response(self.license_status, json={"ok": False, "error": "key_not_found"})
            return httpx.Response(200, json={"ok": True, "license": self.license_payload(),
                                             "signature": "x", "publicKeyId": "abcdefgh"})
        if host == "api.github.com":
            return self._github(request, path)
        if host.startswith("tkbk-sbx-"):
            return self._sandbox(request, path)
        return httpx.Response(404, text=f"unrouted {request.url}")

    def _github(self, request: httpx.Request, path: str) -> httpx.Response:
        m = re.match(r"^/repos/([^/]+)/([^/]+)(/.*)?$", path)
        if not m:
            return httpx.Response(404)
        rest = m.group(3) or ""
        if rest == "" and request.method == "GET":
            return httpx.Response(200, json={"default_branch": "main"})
        if rest.startswith("/issues/"):
            if self.github_issue is None:
                return httpx.Response(404)
            return httpx.Response(200, json=self.github_issue)
        if rest == "/pulls" and request.method == "POST":
            body = json.loads(request.content)
            self.pr_calls.append({"auth": request.headers.get("Authorization"), **body})
            return httpx.Response(201, json={"html_url": f"https://github.com/{m.group(1)}/{m.group(2)}/pull/7",
                                             "number": 7})
        return httpx.Response(404)

    def _sandbox(self, request: httpx.Request, path: str) -> httpx.Response:
        if path == "/health":
            return httpx.Response(200, text="OK")
        self.session_keys_seen.add(request.headers.get("X-Session-API-Key", ""))
        if path == "/api/conversations" and request.method == "POST":
            body = json.loads(request.content)
            self.conv_requests.append(body)
            return httpx.Response(201, json={"id": "c0ffee00-0000-0000-0000-000000000001",
                                             "execution_status": "idle", "metrics": self.metrics()})
        m = re.match(r"^/api/conversations/([^/]+)(/.*)?$", path)
        if not m:
            return httpx.Response(404)
        rest = m.group(2) or ""
        if rest in ("/run", "/pause"):
            return httpx.Response(200, json={"success": True})
        if rest == "" and request.method == "GET":
            st = self.statuses[min(self._i, len(self.statuses) - 1)]
            self._i += 1
            return httpx.Response(200, json={"id": m.group(1), "execution_status": st, "metrics": self.metrics()})
        if rest == "/agent_final_response":
            return httpx.Response(200, json={"response": self.final})
        if rest == "/events/count":
            return httpx.Response(200, json=7)
        if rest == "/events/search":
            page = request.url.params.get("page_id")
            if page:
                return httpx.Response(200, json={"items": [], "next_page_id": None})
            return httpx.Response(200, json={"items": self.events, "next_page_id": "p2"})
        return httpx.Response(404, text=f"unrouted sandbox {path}")

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


@pytest.fixture
def fake_server() -> FakeAgentServer:
    return FakeAgentServer()


# ------------------------------------------------------------------ fake runner
class FakeRunner:
    """Finishes tasks immediately with a canned result (API tests)."""

    def __init__(self, store: TaskStore, hub: EventHub, outcome: TaskStatus = TaskStatus.DONE):
        self.store = store
        self.hub = hub
        self.outcome = outcome
        self.seen: list[tuple[TaskView, TaskSecrets]] = []

    async def run(self, task: TaskView, secrets: TaskSecrets) -> None:
        self.seen.append((task, secrets))
        from tkbk_agents.store import utcnow

        ev = self.hub.open(task.id)
        ev.emit("status", status="running", phase="agent")
        ev.emit("agent", kind="action", tool="terminal", text="pytest -q")
        result = TaskResult(patch_url=f"https://agents.test/v1/tasks/{task.id}/diff", branch=task.work_branch or "b",
                            changed_files=["a.py"], summary="done", commands=1)
        self.store.update(task.id, status=self.outcome, phase="", minutes_billed=3, finished_at=utcnow(), result=result)
        ev.emit("done", status=str(self.outcome), minutes_billed=3, result=result.model_dump(mode="json"))
        self.hub.finish(task.id)
