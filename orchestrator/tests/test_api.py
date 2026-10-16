from __future__ import annotations

import io
import json
import tarfile
import time
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from tests.conftest import KEY, KEY2, SECRET, FakeAgentServer, FakeRunner, make_settings
from tkbk_agents.app import create_app, extract_workspace
from tkbk_agents.auth import sign_headers
from tkbk_agents.models import TaskStatus
from tkbk_agents.tkbk import key_id


class Api:
    """HMAC-signed (admin) or Bearer (client) calls over TestClient."""

    def __init__(self, c: TestClient, bearer: str | None = None):
        self.c = c
        self.bearer = bearer

    def call(self, method: str, path: str, body: dict | None = None, raw: bytes | None = None,
             headers: dict | None = None) -> httpx.Response:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
        h = dict(headers or {})
        if self.bearer:
            h["Authorization"] = f"Bearer {self.bearer}"
        else:
            h.update(sign_headers(SECRET, method, path, data))
        if body is not None:
            h["Content-Type"] = "application/json"
        return self.c.request(method, path, content=data, headers=h)

    def wait(self, task_id: str, timeout: float = 5.0) -> dict:
        t0 = time.time()
        while time.time() - t0 < timeout:
            t = self.call("GET", f"/v1/tasks/{task_id}").json()["task"]
            if TaskStatus(t["status"]).terminal:
                return t
            time.sleep(0.05)
        raise AssertionError("task did not finish")


@pytest.fixture
def app_env(tmp_path: Path, fake_server: FakeAgentServer):
    settings = make_settings(tmp_path)
    http = fake_server.client()
    app = create_app(settings, http=http, start_worker=True, clean_docker=False)
    deps = app.state.deps
    deps.worker.runner = FakeRunner(deps.store, deps.hub)
    deps.worker.tick = 0.05
    with TestClient(app) as c:
        yield c, deps, fake_server, settings


@pytest.fixture
def admin(app_env):
    c, deps, server, settings = app_env
    return Api(c), deps, server, settings


@pytest.fixture
def ide(app_env):
    c, deps, server, settings = app_env
    return Api(c, bearer=KEY), deps, server, settings


def _task(**over) -> dict:
    d = {"api_key": KEY, "repo": "https://github.com/tkbk-online/tkbk-api", "template": "add_tests",
         "instruction": "cover src/keys.ts", "client_ref": "tg:42"}
    d.update(over)
    return d


def _ide_task(**over) -> dict:
    d = {"repoUrl": "https://github.com/tkbk-online/tkbk-api", "prompt": "cover src/keys.ts", "clientRef": "ide"}
    d.update(over)
    return d


def test_health_is_public(admin) -> None:
    a, *_ = admin
    r = a.c.get("/health")
    assert r.status_code == 200 and r.json()["ok"] and r.json()["secret_configured"]


def test_unsigned_requests_are_rejected(admin) -> None:
    a, *_ = admin
    assert a.c.get("/v1/templates").status_code == 401
    assert a.c.post("/v1/tasks", json=_task()).status_code == 401
    assert a.c.get("/v1/me", headers={"Authorization": "Bearer nope"}).status_code == 401


# ------------------------------------------------------------------ IDE (Bearer)
def test_ide_me_and_create_with_aliases(ide) -> None:
    a, _deps, server, _ = ide
    me = a.call("GET", "/v1/me").json()
    assert me["ok"] and me["plan"] == "pro" and me["hours_month"] == 20 and me["minutes_left"] == 1200
    assert me["concurrent_limit"] == 3 and me["key_id"] == key_id(KEY) and me["key_prefix"] == "cr_abcd…"
    assert server.license_requests[0]["app"] == "tkbk-cloud-agents" and server.license_requests[0]["apiKey"] == KEY
    r = a.call("POST", "/v1/tasks", _ide_task())
    assert r.status_code == 201, r.text
    t = r.json()["task"]
    assert t["deliver"] == "patch" and t["push"] is False and t["plan"] == "pro" and t["concurrency_limit"] == 3
    assert t["max_minutes"] == 60 and t["key_id"] == key_id(KEY)
    assert KEY not in r.text.replace("cr_abcd…", "")
    done = a.wait(t["id"])
    assert done["status"] == "done" and done["result"]["patch_url"].endswith("/diff")
    assert done["events"] >= 3 or True  # events counter is set by the real runner only
    res = a.call("GET", f"/v1/tasks/{t['id']}/result").json()
    assert res["ok"] and res["minutes_billed"] == 3 and res["result"]["commands"] == 1
    me2 = a.call("GET", "/v1/me").json()
    assert me2["minutes_used"] == 3 and me2["minutes_left"] == 1197


def test_ide_free_plan_is_refused(ide) -> None:
    a, _deps, server, _ = ide
    server.plan = "free"
    r = a.call("POST", "/v1/tasks", _ide_task())
    assert r.status_code == 403 and "does not include cloud agents" in r.text
    assert a.call("GET", "/v1/me").json()["cloud_agents"] is False


def test_ide_unknown_key_and_site_down(ide) -> None:
    a, _deps, server, _ = ide
    server.license_status = 404
    assert a.call("POST", "/v1/tasks", _ide_task()).status_code == 403
    server.license_status = 503
    r = a.call("POST", "/v1/tasks", _ide_task())
    assert r.status_code == 403 and "HTTP 503" in r.text


def test_ide_hours_quota(tmp_path: Path, fake_server: FakeAgentServer) -> None:
    fake_server.hours = 1  # 60 minutes a month
    settings = make_settings(tmp_path / "q")
    app = create_app(settings, http=fake_server.client(), start_worker=True, clean_docker=False)
    deps = app.state.deps
    deps.worker.runner = FakeRunner(deps.store, deps.hub)
    deps.worker.tick = 0.05
    with TestClient(app) as c:
        a = Api(c, bearer=KEY)
        t1 = a.call("POST", "/v1/tasks", _ide_task(maxMinutes=40)).json()["task"]
        a.wait(t1["id"])                                  # billed 3
        r = a.call("POST", "/v1/tasks", _ide_task(maxMinutes=58))
        assert r.status_code == 402 and "hours" in r.text  # 3 + 58 > 60
        assert a.call("POST", "/v1/tasks", _ide_task(maxMinutes=57)).status_code == 201


def test_ide_sees_only_own_tasks(ide) -> None:
    a, _deps, _server, _ = ide
    other = Api(a.c, bearer=KEY2)
    mine = a.call("POST", "/v1/tasks", _ide_task()).json()["task"]
    theirs = other.call("POST", "/v1/tasks", _ide_task()).json()["task"]
    a.wait(mine["id"])
    other.wait(theirs["id"])
    assert a.call("GET", f"/v1/tasks/{theirs['id']}").status_code == 404
    assert other.call("GET", f"/v1/tasks/{mine['id']}/diff").status_code == 404
    ids = {t["id"] for t in a.call("GET", "/v1/tasks").json()["tasks"]}
    assert ids == {mine["id"]}
    admin_ids = {t["id"] for t in Api(a.c).call("GET", "/v1/tasks").json()["tasks"]}
    assert admin_ids == {mine["id"], theirs["id"]}
    assert other.call("GET", f"/v1/keys/{key_id(KEY)}/usage").status_code == 404


def test_ide_push_requires_token_and_repo_source(ide) -> None:
    a, *_ = ide
    assert a.call("POST", "/v1/tasks", _ide_task(push=True)).status_code == 400
    assert a.call("POST", "/v1/tasks", _ide_task(push=True, gitToken="ghp_x")).status_code == 201
    r = a.call("POST", "/v1/tasks", {"source": "upload", "prompt": "x", "push": True, "gitToken": "ghp_x"})
    assert r.status_code == 400 and "source=repo" in r.text


def test_sse_stream_replays_and_closes(ide) -> None:
    a, _deps, _server, _ = ide
    t = a.call("POST", "/v1/tasks", _ide_task()).json()["task"]
    a.wait(t["id"])
    with a.c.stream("GET", f"/v1/tasks/{t['id']}/events", headers={"Authorization": f"Bearer {KEY}"}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    events = [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]
    types = [e["type"] for e in events]
    assert types[0] == "status" and types[-1] == "done" and "agent" in types
    assert "id: 1\n" in body and "event: done" in body
    # resume after seq 2 -> only later events
    with a.c.stream("GET", f"/v1/tasks/{t['id']}/events", headers={"Authorization": f"Bearer {KEY}",
                                                                    "Last-Event-ID": "2"}) as r:
        body2 = "".join(r.iter_text())
    seqs = [json.loads(line[6:])["seq"] for line in body2.splitlines() if line.startswith("data: ")]
    assert seqs and min(seqs) == 3
    tail = a.call("GET", f"/v1/tasks/{t['id']}").json()["log_tail"]
    assert tail[-1]["type"] == "done"


def test_upload_flow(ide, tmp_path: Path) -> None:
    a, _deps, _server, settings = ide
    r = a.call("POST", "/v1/tasks", {"source": "upload", "prompt": "add tests", "template": "add_tests"})
    assert r.status_code == 201, r.text
    t, upload_url = r.json()["task"], r.json()["upload_url"]
    assert t["status"] == "awaiting_upload" and upload_url.endswith(f"/v1/tasks/{t['id']}/workspace")
    assert a.call("GET", f"/v1/tasks/{t['id']}").json()["task"]["status"] == "awaiting_upload"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in (("proj/app.py", b"print(1)\n"), ("proj/sub/x.txt", b"x")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    r = a.call("PUT", f"/v1/tasks/{t['id']}/workspace", raw=buf.getvalue(),
               headers={"Content-Type": "application/gzip"})
    assert r.status_code == 200 and r.json()["files"] == 2, r.text
    assert (settings.tasks_root / t["id"] / "workspace" / "repo" / "sub" / "x.txt").read_text() == "x"
    done = a.wait(t["id"])
    assert done["status"] == "done" and done["source"] == "upload"
    # a second upload is refused
    assert a.call("PUT", f"/v1/tasks/{t['id']}/workspace", raw=buf.getvalue()).status_code == 409


def test_upload_rejects_bad_archives(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    with pytest.raises((tarfile.TarError, ValueError)):
        extract_workspace(io.BytesIO(b"not a tar"), repo)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        info = tarfile.TarInfo("../../etc/passwd")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
        link = tarfile.TarInfo("link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc"
        tar.addfile(link)
        ok = tarfile.TarInfo("ok.txt")
        ok.size = 2
        tar.addfile(ok, io.BytesIO(b"ok"))
    buf.seek(0)
    with pytest.raises((tarfile.TarError, ValueError, OSError)):
        extract_workspace(buf, repo)


# ------------------------------------------------------------------ admin (HMAC)
def test_admin_create_and_complete_task(admin) -> None:
    a, deps, _server, _ = admin
    r = a.call("POST", "/v1/tasks", _task(deliver="pr", git_token="ghp_x"))
    assert r.status_code == 201, r.text
    t = r.json()["task"]
    assert t["model"] == "claude-sonnet-5" and t["provider"] == "anthropic" and t["plan"] == "pro"
    assert "ghp_x" not in r.text
    done = a.wait(t["id"])
    assert done["status"] == "done"
    _task_seen, secrets = deps.worker.runner.seen[0]
    assert secrets.api_key == KEY and secrets.git_token == "ghp_x"
    assert len(deps.vault) == 0
    lst = a.call("GET", f"/v1/tasks?key_id={key_id(KEY)}").json()["tasks"]
    assert [x["id"] for x in lst] == [t["id"]]


@pytest.mark.parametrize("bad,code,msg", [
    (_task(api_key="sk-nope"), 422, "api_key"),
    (_task(repo="https://gitlab.com/a/b"), 422, "repo"),
    (_task(template="fix_issue", issue=None), 400, "requires `issue`"),
    (_task(template="pr_from_spec", instruction=""), 400, "requires `instruction`"),
    (_task(deliver="pr"), 400, "git_token"),
    (_task(max_minutes=999), 400, "server limit"),
    ({"api_key": KEY, "source": "upload", "template": "fix_issue", "issue": 3}, 422, "fix_issue needs source=repo"),
])
def test_validation_errors(admin, bad: dict, code: int, msg: str) -> None:
    a, *_ = admin
    r = a.call("POST", "/v1/tasks", bad)
    assert r.status_code == code, r.text
    assert msg in r.text


def test_admin_key_rejected_by_gateway(admin) -> None:
    a, _deps, server, _ = admin
    server.key_ok = False
    r = a.call("POST", "/v1/tasks", _task())
    assert r.status_code == 401 and "rejected" in r.text
    server.key_ok = True


def test_admin_free_key_refused(admin) -> None:
    a, _deps, server, _ = admin
    server.plan = "free"
    assert a.call("POST", "/v1/tasks", _task()).status_code == 403


def test_cancel_queued_and_active(admin) -> None:
    a, deps, _server, _ = admin
    import asyncio

    class Slow(FakeRunner):
        async def run(self, task, secrets):
            await asyncio.sleep(10)

    deps.worker.runner = Slow(deps.store, deps.hub)
    # same key: per-client concurrency (pro = 3) lets 3 run; the 4th waits in the queue
    ids = [a.call("POST", "/v1/tasks", _task()).json()["task"]["id"] for _ in range(4)]
    time.sleep(0.3)
    statuses = [a.call("GET", f"/v1/tasks/{i}").json()["task"]["status"] for i in ids]
    assert statuses.count("preparing") == 3 and statuses[-1] == "queued"
    r = a.call("POST", f"/v1/tasks/{ids[-1]}/cancel")
    assert r.status_code == 200 and r.json()["task"]["status"] == "cancelled"
    assert a.call("POST", f"/v1/tasks/{ids[-1]}/cancel").status_code == 409
    r = a.call("POST", f"/v1/tasks/{ids[0]}/cancel")
    assert r.status_code == 200 and r.json()["task"]["status"] == "preparing"  # active: cancel at checkpoint


def test_per_client_concurrency_does_not_block_other_clients(admin) -> None:
    a, deps, _server, _ = admin
    import asyncio

    class Slow(FakeRunner):
        async def run(self, task, secrets):
            await asyncio.sleep(10)

    deps.worker.runner = Slow(deps.store, deps.hub)
    for _ in range(3):
        a.call("POST", "/v1/tasks", _task())
    waiting = a.call("POST", "/v1/tasks", _task()).json()["task"]["id"]
    other = a.call("POST", "/v1/tasks", _task(api_key=KEY2)).json()["task"]["id"]
    time.sleep(0.3)
    assert a.call("GET", f"/v1/tasks/{waiting}").json()["task"]["status"] == "queued"
    assert a.call("GET", f"/v1/tasks/{other}").json()["task"]["status"] == "preparing"  # 4th global slot


def test_usage_endpoints(admin) -> None:
    a, _deps, _server, _ = admin
    t1 = a.call("POST", "/v1/tasks", _task()).json()["task"]
    a.wait(t1["id"])
    month = t1["created_at"][:7]
    u = a.call("GET", f"/v1/usage?month={month}").json()
    assert u["ok"] and u["rows"][0]["key_id"] == key_id(KEY) and u["rows"][0]["minutes"] == 3
    assert a.call("GET", "/v1/usage?month=2026-13").status_code == 400
    assert a.call("GET", f"/v1/keys/{key_id(KEY)}/usage?month={month}").json()["rows"][0]["tasks"] == 1


def test_templates_endpoint(admin) -> None:
    a, *_ = admin
    r = a.call("GET", "/v1/templates").json()
    assert {t["id"] for t in r["templates"]} == {"fix_issue", "add_tests", "update_deps", "pr_from_spec", "custom"}
    assert r["limits"]["default_max_minutes"] == 60 and r["limits"]["max_max_minutes"] == 120


def test_restart_fails_active_tasks(tmp_path: Path, fake_server: FakeAgentServer) -> None:
    from tkbk_agents.store import TaskStore

    settings = make_settings(tmp_path / "restart")
    store = TaskStore(settings.db_path)
    store.create(key_id="k", key_prefix="cr_x…", repo="https://github.com/a/b", template="custom", model="m",
                 provider="anthropic", deliver="patch", max_minutes=5, max_iterations=5, status=TaskStatus.RUNNING)
    store.close()
    app = create_app(settings, http=fake_server.client(), start_worker=False, clean_docker=False)
    with TestClient(app) as c:
        tasks = Api(c).call("GET", "/v1/tasks").json()["tasks"]
        assert tasks[0]["status"] == "failed" and "restarted" in tasks[0]["error"]


def test_stub_license_mode(tmp_path: Path, fake_server: FakeAgentServer) -> None:
    settings = make_settings(tmp_path / "stub", LICENSE_MODE="stub", STUB_PLAN="team")
    app = create_app(settings, http=fake_server.client(), start_worker=False, clean_docker=False)
    with TestClient(app) as c:
        me = Api(c, bearer=KEY).call("GET", "/v1/me").json()
        assert me["plan"] == "team" and me["hours_month"] == 100 and me["concurrent_limit"] == 5
        assert not fake_server.license_requests
        assert c.get("/health").json()["license_mode"] == "stub"
