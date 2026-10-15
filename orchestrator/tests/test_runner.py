"""End-to-end run of DockerRunner with: fake docker CLI, fake agent-server (MockTransport),
fake GitHub API, and a REAL local git repository standing in for github.com."""

from __future__ import annotations

import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from tests.conftest import KEY, FakeAgentServer, FakeDocker, make_settings
from tkbk_agents import gitops
from tkbk_agents import runner as runner_mod
from tkbk_agents.models import Deliver, TaskStatus, Template
from tkbk_agents.runner import DockerRunner, billed_minutes, work_branch_name
from tkbk_agents.store import TaskSecrets, TaskStore


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    _git("init", "-q", "--bare", "-b", "main", str(bare))
    src = tmp_path / "seed"
    _git("init", "-q", "-b", "main", str(src))
    (src / "app.py").write_text("def add(a, b):\n    return a + b\n")
    _git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=src)
    _git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=src)
    _git("remote", "add", "origin", str(bare), cwd=src)
    _git("push", "-q", "origin", "main", cwd=src)
    return bare


@pytest.fixture
def local_github(monkeypatch: pytest.MonkeyPatch, origin: Path):
    """Redirect clone/push to the local bare repo and simulate the agent editing files."""
    real_clone = gitops.clone

    async def fake_clone(repo_url: str, dest: Path, branch: str | None, token: str | None) -> str:
        sha = await real_clone(str(origin), dest, branch, None)
        return sha

    async def fake_push(dest: Path, branch: str, token: str | None) -> None:
        await gitops.git("push", "-u", "origin", f"HEAD:refs/heads/{branch}", cwd=dest)

    monkeypatch.setattr(gitops, "clone", fake_clone)
    monkeypatch.setattr(gitops, "push", fake_push)
    return origin


def _make(store: TaskStore, **over):
    d = dict(key_id="kid", key_prefix="cr_abcd…", repo="https://github.com/tkbk-online/demo", template=Template.ADD_TESTS,
             instruction="cover add()", model="claude-sonnet-5", provider="anthropic", deliver=Deliver.PR,
             max_minutes=30, max_iterations=50)
    d.update(over)
    return store.create(**d)


class EditingServer(FakeAgentServer):
    """When the agent 'runs', drop a file into the repo like a real agent would."""

    def __init__(self, repo_dir_getter, **kw):
        super().__init__(**kw)
        self._repo_dir_getter = repo_dir_getter

    def _sandbox(self, request, path):
        if path.endswith("/run"):
            d = self._repo_dir_getter()
            (d / "test_app.py").write_text("from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n")
        return super()._sandbox(request, path)


@pytest.mark.asyncio
async def test_pr_delivery_end_to_end(tmp_path: Path, local_github: Path) -> None:
    settings = make_settings(tmp_path)
    store = TaskStore(settings.db_path)
    task = _make(store)
    repo_dir = settings.tasks_root / task.id / "workspace" / "repo"
    server = EditingServer(lambda: repo_dir, statuses=["running", "running", "finished"])
    docker = FakeDocker()
    async with server.client() as http:
        r = DockerRunner(settings, store, docker_fn=docker, http=http)
        await r.run(task, TaskSecrets(api_key=KEY, git_token="ghp_tok"))
    final = store.get(task.id)
    assert final is not None and final.status is TaskStatus.DONE, final.error
    assert final.result and final.result.pr_url == "https://github.com/tkbk-online/demo/pull/7"
    assert final.result.changed_files == ["test_app.py"]
    assert final.result.branch == work_branch_name(task.id, Template.ADD_TESTS) == final.work_branch
    assert final.base_branch == "main" and final.minutes_billed >= 1
    assert final.metrics.prompt_tokens == 1000 and final.metrics.iterations == 7
    assert final.result.summary.startswith("Added tests")
    # the PR was created with the client's token and the agent summary; branch exists in "GitHub"
    assert server.pr_calls[0]["auth"] == "Bearer ghp_tok" and server.pr_calls[0]["head"] == final.work_branch
    assert server.pr_calls[0]["base"] == "main" and "Added tests" in server.pr_calls[0]["body"]
    assert final.work_branch in _git("branch", "--list", final.work_branch, cwd=local_github)
    # the LLM config sent to the sandbox carries the client's key and TKBK base URL
    llm = server.conv_requests[0]["agent_settings"]["llm"]
    assert llm["api_key"] == KEY and llm["model"] == "anthropic/claude-sonnet-5"
    assert llm["base_url"] == "https://api.tkbk.online"
    assert "cover add()" in server.conv_requests[0]["initial_message"]["content"][0]["text"]
    # docker: run + full cleanup; the client key never appears in docker args
    assert docker.has("run", "-d") and docker.has("rm", "-f") and docker.has("network", "rm")
    assert all(KEY not in " ".join(c) and "ghp_tok" not in " ".join(c) for c in docker.calls)
    assert not (settings.tasks_root / task.id).exists()
    store.close()


@pytest.mark.asyncio
async def test_archive_delivery(tmp_path: Path, local_github: Path) -> None:
    settings = make_settings(tmp_path)
    store = TaskStore(settings.db_path)
    task = _make(store, deliver=Deliver.ARCHIVE, keep_workspace=True)
    repo_dir = settings.tasks_root / task.id / "workspace" / "repo"
    server = EditingServer(lambda: repo_dir)
    async with server.client() as http:
        await DockerRunner(settings, store, docker_fn=FakeDocker(), http=http).run(task, TaskSecrets(api_key=KEY))
    final = store.get(task.id)
    assert final and final.status is TaskStatus.DONE and final.result
    assert final.result.archive_url == f"https://agents.test/v1/tasks/{task.id}/archive"
    with tarfile.open(settings.results_dir / f"{task.id}.tgz") as tar:
        assert "repo/test_app.py" in tar.getnames()
    assert repo_dir.exists()  # keep_workspace
    assert not server.pr_calls
    store.close()


@pytest.mark.asyncio
async def test_no_changes_is_done_without_pr(tmp_path: Path, local_github: Path) -> None:
    settings = make_settings(tmp_path)
    store = TaskStore(settings.db_path)
    task = _make(store)
    server = FakeAgentServer(final="Nothing to do.")
    async with server.client() as http:
        await DockerRunner(settings, store, docker_fn=FakeDocker(), http=http).run(task, TaskSecrets(KEY, "ghp"))
    final = store.get(task.id)
    assert final and final.status is TaskStatus.DONE and final.result
    assert final.result.pr_url is None and "no file changes" in final.result.summary and not server.pr_calls
    store.close()


@pytest.mark.asyncio
async def test_fix_issue_fetches_issue_text(tmp_path: Path, local_github: Path) -> None:
    settings = make_settings(tmp_path)
    store = TaskStore(settings.db_path)
    task = _make(store, template=Template.FIX_ISSUE, issue=12, instruction="")
    server = FakeAgentServer(github_issue={"title": "add() breaks on None", "body": "repro: add(None, 1)",
                                           "html_url": "https://github.com/tkbk-online/demo/issues/12"})
    async with server.client() as http:
        await DockerRunner(settings, store, docker_fn=FakeDocker(), http=http).run(task, TaskSecrets(KEY, "ghp"))
    prompt = server.conv_requests[0]["initial_message"]["content"][0]["text"]
    assert "issue #12" in prompt and "add() breaks on None" in prompt and "repro: add(None, 1)" in prompt
    store.close()


@pytest.mark.asyncio
async def test_timeout_delivers_draft_pr(tmp_path: Path, local_github: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(tmp_path)
    store = TaskStore(settings.db_path)
    task = _make(store, max_minutes=1)
    repo_dir = settings.tasks_root / task.id / "workspace" / "repo"
    server = EditingServer(lambda: repo_dir, statuses=["running"])  # never finishes

    class Clock:
        t = time.monotonic()

        @staticmethod
        def monotonic() -> float:
            Clock.t += 31.0
            return Clock.t

    monkeypatch.setattr(runner_mod, "_now", Clock.monotonic)
    async with server.client() as http:
        await DockerRunner(settings, store, docker_fn=FakeDocker(), http=http).run(task, TaskSecrets(KEY, "ghp"))
    final = store.get(task.id)
    assert final and final.status is TaskStatus.TIMEOUT and "time limit" in (final.error or "")
    assert final.result and final.result.pr_url and server.pr_calls[0]["draft"] is True
    assert server.pr_calls[0]["title"].startswith("[timeout]")
    store.close()


@pytest.mark.asyncio
async def test_cancel_during_run(tmp_path: Path, local_github: Path) -> None:
    settings = make_settings(tmp_path)
    store = TaskStore(settings.db_path)
    task = _make(store)

    class CancellingServer(FakeAgentServer):
        def _sandbox(self, request, path):
            if path.endswith("/run"):
                store.request_cancel(task.id)
            return super()._sandbox(request, path)

    server = CancellingServer(statuses=["running"])
    docker = FakeDocker()
    async with server.client() as http:
        await DockerRunner(settings, store, docker_fn=docker, http=http).run(task, TaskSecrets(KEY, "ghp"))
    final = store.get(task.id)
    assert final and final.status is TaskStatus.CANCELLED and not server.pr_calls
    assert docker.has("rm", "-f")
    store.close()


@pytest.mark.asyncio
async def test_sandbox_failure_is_reported(tmp_path: Path, local_github: Path) -> None:
    settings = make_settings(tmp_path)
    store = TaskStore(settings.db_path)
    task = _make(store)
    docker = FakeDocker(fail_on=lambda a: "no space left on device" if a[:1] == ["run"] else None)
    async with FakeAgentServer().client() as http:
        await DockerRunner(settings, store, docker_fn=docker, http=http).run(task, TaskSecrets(KEY, "ghp"))
    final = store.get(task.id)
    assert final and final.status is TaskStatus.FAILED and "no space left" in (final.error or "")
    assert docker.has("network", "rm")  # network cleaned even though run failed
    store.close()


def test_billed_minutes_rounds_up() -> None:
    assert billed_minutes(0) == 1 and billed_minutes(59) == 1 and billed_minutes(61) == 2 and billed_minutes(600) == 10
