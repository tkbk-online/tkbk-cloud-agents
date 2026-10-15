from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from tests.conftest import KEY, SECRET, FakeAgentServer, FakeRunner, make_settings
from tkbk_agents.app import create_app
from tkbk_agents.auth import sign_headers
from tkbk_agents.models import TaskStatus
from tkbk_agents.tkbk import key_id


class Api:
    """Signed client over TestClient."""

    def __init__(self, c: TestClient):
        self.c = c

    def call(self, method: str, path: str, body: dict | None = None) -> httpx.Response:
        raw = json.dumps(body).encode() if body is not None else b""
        headers = sign_headers(SECRET, method, path, raw)
        if body is not None:
            headers["Content-Type"] = "application/json"
        return self.c.request(method, path, content=raw, headers=headers)

    def wait(self, task_id: str, timeout: float = 5.0) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            t = self.call("GET", f"/v1/tasks/{task_id}").json()["task"]
            if TaskStatus(t["status"]).terminal:
                return t
            time.sleep(0.05)
        raise AssertionError("task did not finish")


@pytest.fixture
def api(tmp_path: Path, fake_server: FakeAgentServer):
    settings = make_settings(tmp_path)
    http = fake_server.client()
    runner_holder: dict = {}

    def build():
        app = create_app(settings, runner=None, http=http, start_worker=True, clean_docker=False)
        deps = app.state.deps
        runner = FakeRunner(deps.store)
        deps.worker.runner = runner
        deps.worker.tick = 0.05
        runner_holder["runner"] = runner
        runner_holder["store"] = deps.store
        return app

    app = build()
    with TestClient(app) as c:
        yield Api(c), runner_holder, fake_server, settings


def _task(**over) -> dict:
    d = {"api_key": KEY, "repo": "https://github.com/tkbk-online/tkbk-api", "template": "add_tests",
         "instruction": "cover src/keys.ts", "git_token": "ghp_x", "client_ref": "tg:42"}
    d.update(over)
    return d


def test_health_is_public(api) -> None:
    a, *_ = api
    r = a.c.get("/health")
    assert r.status_code == 200 and r.json()["ok"] and r.json()["secret_configured"]


def test_unsigned_requests_are_rejected(api) -> None:
    a, *_ = api
    assert a.c.get("/v1/templates").status_code == 401
    assert a.c.post("/v1/tasks", json=_task()).status_code == 401


def test_create_and_complete_task(api) -> None:
    a, holder, _server, _settings = api
    r = a.call("POST", "/v1/tasks", _task())
    assert r.status_code == 201, r.text
    t = r.json()["task"]
    assert t["status"] in ("queued", "preparing") and t["key_prefix"] == "cr_abcd…" and t["key_id"] == key_id(KEY)
    assert t["model"] == "claude-sonnet-5" and t["provider"] == "anthropic" and t["max_minutes"] == 30
    assert KEY not in r.text.replace(t["key_prefix"], "") and "ghp_x" not in r.text
    done = a.wait(t["id"])
    assert done["status"] == "done" and done["result"]["pr_url"].endswith("/pull/1")
    # secrets reached the runner and were dropped afterwards
    _task_seen, secrets = holder["runner"].seen[0]
    assert secrets.api_key == KEY and secrets.git_token == "ghp_x"
    assert len(a.c.app.state.deps.vault) == 0
    res = a.call("GET", f"/v1/tasks/{t['id']}/result").json()
    assert res["ok"] and res["status"] == "done" and res["minutes_billed"] == 3
    lst = a.call("GET", f"/v1/tasks?key_id={key_id(KEY)}").json()["tasks"]
    assert [x["id"] for x in lst] == [t["id"]]


@pytest.mark.parametrize("bad,code,msg", [
    (_task(api_key="sk-nope"), 422, "api_key"),
    (_task(repo="https://gitlab.com/a/b"), 422, "repo"),
    (_task(template="fix_issue", issue=None), 400, "requires `issue`"),
    (_task(template="pr_from_spec", instruction=""), 400, "requires `instruction`"),
    (_task(git_token=None), 400, "git_token"),
    (_task(max_minutes=999), 400, "server limit"),
])
def test_validation_errors(api, bad: dict, code: int, msg: str) -> None:
    a, *_ = api
    r = a.call("POST", "/v1/tasks", bad)
    assert r.status_code == code, r.text
    assert msg in r.text


def test_archive_delivery_needs_no_token_and_result_404_until_done(api) -> None:
    a, *_ = api
    r = a.call("POST", "/v1/tasks", _task(git_token=None, deliver="archive", model="gpt-5.5"))
    assert r.status_code == 201, r.text
    t = r.json()["task"]
    assert t["provider"] == "openai"
    a.wait(t["id"])
    assert a.call("GET", f"/v1/tasks/{t['id']}/archive").status_code == 404  # FakeRunner made no file
    assert a.call("GET", "/v1/tasks/t_nope/result").status_code == 404


def test_key_rejected_by_gateway(api) -> None:
    a, _holder, server, _ = api
    server.key_ok = False
    r = a.call("POST", "/v1/tasks", _task())
    assert r.status_code == 401 and "rejected" in r.text
    server.key_ok = True


def test_cancel_queued_and_finished(api) -> None:
    a, holder, *_ = api
    # pause the worker so the task stays queued
    holder["store"]
    a.c.app.state.deps.worker.settings = make_settings(Path(a.c.app.state.deps.settings.data_dir).parent,
                                                        MAX_CONCURRENT_TASKS=1)
    # fill the single slot with a never-finishing fake
    import asyncio

    class Slow(FakeRunner):
        async def run(self, task, secrets):
            await asyncio.sleep(10)

    a.c.app.state.deps.worker.runner = Slow(holder["store"])
    first = a.call("POST", "/v1/tasks", _task()).json()["task"]
    second = a.call("POST", "/v1/tasks", _task()).json()["task"]
    time.sleep(0.2)
    s = a.call("GET", f"/v1/tasks/{second['id']}").json()["task"]
    assert s["status"] == "queued"
    r = a.call("POST", f"/v1/tasks/{second['id']}/cancel")
    assert r.status_code == 200 and r.json()["task"]["status"] == "cancelled"
    assert a.call("POST", f"/v1/tasks/{second['id']}/cancel").status_code == 409
    r = a.call("POST", f"/v1/tasks/{first['id']}/cancel")
    assert r.status_code == 200 and r.json()["task"]["status"] == "preparing"  # active: cancel at checkpoint


def test_usage_and_monthly_cap(tmp_path: Path, fake_server: FakeAgentServer) -> None:
    settings = make_settings(tmp_path / "cap", MONTHLY_MINUTES_CAP_PER_KEY=50)
    app = create_app(settings, http=fake_server.client(), start_worker=True, clean_docker=False)
    deps = app.state.deps
    deps.worker.runner = FakeRunner(deps.store)
    deps.worker.tick = 0.05
    with TestClient(app) as c:
        a = Api(c)
        t1 = a.call("POST", "/v1/tasks", _task(max_minutes=20)).json()["task"]
        a.wait(t1["id"])
        t2 = a.call("POST", "/v1/tasks", _task(max_minutes=20)).json()["task"]
        a.wait(t2["id"])
        # 3 + 3 billed so far; a 48-minute task would exceed 50
        r = a.call("POST", "/v1/tasks", _task(max_minutes=48))
        assert r.status_code == 402 and "cap" in r.text
        month = t1["created_at"][:7]
        u = a.call("GET", f"/v1/usage?month={month}").json()
        assert u["ok"] and u["rows"][0]["key_id"] == key_id(KEY) and u["rows"][0]["minutes"] == 6
        assert u["rows"][0]["tasks"] == 2
        k = a.call("GET", f"/v1/keys/{key_id(KEY)}/usage?month={month}").json()
        assert k["rows"][0]["minutes"] == 6
        assert a.call("GET", "/v1/usage?month=2026-13").status_code == 400


def test_templates_endpoint(api) -> None:
    a, *_ = api
    r = a.call("GET", "/v1/templates").json()
    assert {t["id"] for t in r["templates"]} == {"fix_issue", "add_tests", "update_deps", "pr_from_spec", "custom"}
    assert r["default_model"] == "claude-sonnet-5" and r["limits"]["max_max_minutes"] == 120


def test_restart_fails_active_tasks(tmp_path: Path, fake_server: FakeAgentServer) -> None:
    from tkbk_agents.store import TaskStore

    settings = make_settings(tmp_path / "restart")
    store = TaskStore(settings.db_path)
    store.create(key_id="k", key_prefix="cr_x…", repo="https://github.com/a/b", template="custom", model="m",
                 provider="anthropic", deliver="pr", max_minutes=5, max_iterations=5, status=TaskStatus.RUNNING)
    store.close()
    app = create_app(settings, http=fake_server.client(), start_worker=False, clean_docker=False)
    with TestClient(app) as c:
        a = Api(c)
        tasks = a.call("GET", "/v1/tasks").json()["tasks"]
        assert tasks[0]["status"] == "failed" and "restarted" in tasks[0]["error"]
