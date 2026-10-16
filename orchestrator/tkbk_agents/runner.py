"""Runs one task end to end: workspace -> sandbox -> agent -> patch (+archive / push / PR) -> billing."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from . import gitops
from .agent_client import AgentServerClient, AgentServerError
from .config import Settings
from .docker_cli import DockerFn, docker
from .events import EventHub, TaskEvents, summarize_agent_event
from .models import Deliver, Provider, Source, TaskMetrics, TaskResult, TaskStatus, TaskView, Template
from .sandbox import REPO_IN_SANDBOX, Sandbox
from .store import TaskSecrets, TaskStore, utcnow
from .templates import IssueInfo, build_prompt
from .tkbk import llm_settings

log = logging.getLogger("tkbk.runner")
_now = time.monotonic  # indirection: tests patch this without touching asyncio's clock


class Runner(Protocol):
    async def run(self, task: TaskView, secrets: TaskSecrets) -> None: ...


class Cancelled(Exception):
    pass


class TaskTimeout(Exception):
    pass


def billed_minutes(seconds: float) -> int:
    return max(1, math.ceil(seconds / 60.0))


def work_branch_name(task_id: str, template: Template) -> str:
    return f"tkbk/{task_id.removeprefix('t_')}"


def _slug(text: str, n: int = 60) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n] + ("…" if len(text) > n else "")


@dataclass
class DockerRunner:
    settings: Settings
    store: TaskStore
    hub: EventHub
    docker_fn: DockerFn = docker
    http: httpx.AsyncClient | None = None      # injected in tests (MockTransport)
    github: gitops.GitHub | None = None

    async def run(self, task: TaskView, secrets: TaskSecrets) -> None:
        s = self.settings
        store = self.store
        ev = self.hub.open(task.id)
        sandbox = Sandbox(s, task.id, docker_fn=self.docker_fn, http=self.http)
        gh = self.github or gitops.GitHub(http=self.http)
        t0 = _now()
        status, error = TaskStatus.DONE, None
        result = TaskResult()
        metrics = TaskMetrics()
        conversation_id: str | None = None
        client: AgentServerClient | None = None
        base_sha = ""
        work_branch = work_branch_name(task.id, task.template)
        store.update(task.id, work_branch=work_branch)

        def phase(st: TaskStatus, ph: str) -> None:
            store.set_status(task.id, st, ph)
            ev.emit("status", status=str(st), phase=ph)

        try:
            self._checkpoint(task.id)
            instruction, issue_no = store.instruction_and_issue(task.id)
            issue: IssueInfo | None = None
            if task.source is Source.REPO:
                assert task.repo
                owner, repo_name = gitops.parse_repo(task.repo)
                phase(TaskStatus.PREPARING, "fetching-inputs")
                if task.template is Template.FIX_ISSUE and issue_no:
                    issue = await gh.get_issue(owner, repo_name, issue_no, secrets.git_token)
                base_branch = task.base_branch or await gh.default_branch(owner, repo_name, secrets.git_token)
                store.update(task.id, base_branch=base_branch)
                phase(TaskStatus.PREPARING, "cloning")
                sandbox.prepare_workspace()
                base_sha = await gitops.clone(task.repo, sandbox.repo_dir, base_branch, secrets.git_token)
            else:
                owner = repo_name = ""
                phase(TaskStatus.PREPARING, "importing-workspace")
                base_sha = await gitops.baseline(sandbox.repo_dir, s.git_name, s.git_email)
                store.update(task.id, base_branch=await gitops.current_branch(sandbox.repo_dir))
            await gitops.create_branch(sandbox.repo_dir, work_branch)
            sandbox.chown_workspace()

            self._checkpoint(task.id)
            phase(TaskStatus.PREPARING, "starting-sandbox")
            await sandbox.start()

            client = AgentServerClient(sandbox.spec.base_url, sandbox.spec.session_key, http=self.http)
            prompt = build_prompt(task.template, repo=task.repo or "uploaded workspace", work_branch=work_branch,
                                  workdir=REPO_IN_SANDBOX, instruction=instruction, issue=issue)
            llm = llm_settings(s, task.model, Provider(task.provider), secrets.api_key)
            conv = await client.start_conversation(llm=llm, working_dir=REPO_IN_SANDBOX, message=prompt,
                                                   max_iterations=task.max_iterations)
            conversation_id = conv.id
            await client.run(conversation_id)
            phase(TaskStatus.RUNNING, "agent")

            deadline = _now() + task.max_minutes * 60
            metrics = await self._poll(client, conversation_id, deadline, task.id, ev)
            metrics.iterations = await client.count_actions(conversation_id)
            result.summary = (await client.final_response(conversation_id))[:20000]

        except Cancelled:
            status, error = TaskStatus.CANCELLED, "cancelled by client"
        except TaskTimeout:
            status, error = TaskStatus.TIMEOUT, f"time limit of {task.max_minutes} min reached"
            if client and conversation_id:
                await client.pause(conversation_id)
                try:
                    metrics = (await client.state(conversation_id)).metrics
                    result.summary = (await client.final_response(conversation_id))[:20000]
                except AgentServerError:
                    pass
        except (gitops.GitError, AgentServerError, RuntimeError, OSError) as e:
            status, error = TaskStatus.FAILED, f"{type(e).__name__}: {str(e)[:800]}"
            log.warning("task %s failed: %s", task.id, error)
            ev.emit("log", level="error", message=error)

        try:
            if status in (TaskStatus.DONE, TaskStatus.TIMEOUT) and sandbox.repo_dir.exists():
                phase(TaskStatus.DELIVERING, "delivering")
                deliver_error = await self._deliver(task, secrets, sandbox, gh, owner, repo_name, work_branch,
                                                    base_sha, result, timed_out=status is TaskStatus.TIMEOUT)
                if deliver_error and status is TaskStatus.DONE:
                    status, error = TaskStatus.FAILED, deliver_error
                elif deliver_error:
                    error = f"{error}; {deliver_error}"
        finally:
            if client:
                await client.aclose()
            try:
                await sandbox.stop(keep_workspace=task.keep_workspace)
            except Exception as e:  # noqa: BLE001 — cleanup must not mask the task outcome
                log.warning("task %s cleanup: %s", task.id, e)
            minutes = billed_minutes(sandbox.elapsed_seconds or (_now() - t0))
            result.commands = sum(1 for e in ev.replay() if e.get("type") == "agent" and e.get("kind") == "action")
            if not metrics.iterations:
                metrics.iterations = result.commands  # events/count may be unavailable; our own log is exact
            store.update(task.id, status=status, phase="", error=error, result=result, metrics=metrics,
                         minutes_billed=minutes, finished_at=utcnow(), events=ev.seq)
            ev.emit("done", status=str(status), error=error, minutes_billed=minutes,
                    result=result.model_dump(mode="json"))
            self.hub.finish(task.id)

    # ------------------------------------------------------------------ helpers
    def _checkpoint(self, task_id: str) -> None:
        if self.store.cancel_requested(task_id):
            raise Cancelled()

    async def _poll(self, client: AgentServerClient, cid: str, deadline: float, task_id: str,
                    ev: TaskEvents) -> TaskMetrics:
        last = TaskMetrics()
        seen_running = False
        page: str | None = None
        seen_ids: set[str] = set()
        while True:
            self._checkpoint(task_id)
            if _now() >= deadline:
                await self._pull_events(client, cid, page, seen_ids, ev)
                raise TaskTimeout()
            st = await client.state(cid)
            last = st.metrics
            self.store.update(task_id, metrics=last)
            page = await self._pull_events(client, cid, page, seen_ids, ev)
            if st.execution_status == "running":
                seen_running = True
            elif st.terminal or (seen_running and st.execution_status in ("idle", "paused")):
                if st.execution_status == "error":
                    raise AgentServerError(f"agent error: {st.raw.get('error') or st.execution_status}")
                return last
            elif st.execution_status == "waiting_for_confirmation":
                return last  # NeverConfirm is set; should not happen. Treat as finished to avoid a hang.
            await asyncio.sleep(self.settings.poll_interval)

    async def _pull_events(self, client: AgentServerClient, cid: str, page: str | None,
                           seen: set[str], ev: TaskEvents) -> str | None:
        """Fetch new agent events (cursor-based), emit compact versions. Returns the next cursor."""
        for _ in range(10):  # bounded per poll
            items, next_page = await client.events_page(cid, page)
            for raw in items:
                eid = str(raw.get("id") or "")
                if eid and eid in seen:
                    continue
                if eid:
                    seen.add(eid)
                compact = summarize_agent_event(raw)
                if compact:
                    ev.emit("agent", **compact)
            if not next_page or next_page == page or not items:
                return page if not next_page else next_page
            page = next_page
        return page

    async def _deliver(self, task: TaskView, secrets: TaskSecrets, sandbox: Sandbox, gh: gitops.GitHub,
                       owner: str, repo_name: str, work_branch: str, base_sha: str, result: TaskResult,
                       timed_out: bool) -> str | None:
        """Patch always; archive / push / PR on request. Returns an error string or None."""
        s = self.settings
        repo_dir = sandbox.repo_dir
        summary_line = _slug(result.summary.splitlines()[0]) if result.summary.strip() else task.template.value
        try:
            sha = await gitops.commit_all(repo_dir, f"TKBK agent: {summary_line}", s.git_name, s.git_email)
            result.commit = sha or await gitops.head(repo_dir)
            result.branch = work_branch
            result.changed_files = await gitops.changed_files(repo_dir, base_sha)
        except gitops.GitError as e:
            return f"commit failed: {e}"
        base = f"{s.public_base_url}/v1/tasks/{task.id}"
        result.log_url = f"{base}/artifacts/log"
        if not result.changed_files:
            result.summary = (result.summary + "\n\n(no file changes were produced)").strip()
            return None
        try:
            patch = await gitops.patch(repo_dir, base_sha)
            s.results_dir.mkdir(parents=True, exist_ok=True)
            (s.results_dir / f"{task.id}.patch").write_text(patch, encoding="utf-8")
            result.patch_url = f"{base}/diff"
        except (gitops.GitError, OSError) as e:
            return f"patch failed: {e}"
        if task.deliver is Deliver.ARCHIVE:
            try:
                gitops.make_archive(repo_dir, s.results_dir / f"{task.id}.tgz")
                result.archive_url = f"{base}/artifacts/archive"
            except OSError as e:
                return f"archive failed: {e}"
        if (task.push or task.deliver is Deliver.PR) and task.source is Source.REPO:
            if not secrets.git_token:
                return "push/PR needs git_token"
            try:
                await gitops.push(repo_dir, work_branch, secrets.git_token)
                result.pushed = True
            except gitops.GitError as e:
                return f"push failed: {e}"
            if task.deliver is Deliver.PR:
                try:
                    title = ("[timeout] " if timed_out else "") + f"TKBK agent: {summary_line}"
                    pr = await gh.create_pr(owner, repo_name, head=work_branch, base=task.base_branch or "main",
                                            title=title, body=self._pr_body(task, result, timed_out),
                                            token=secrets.git_token, draft=timed_out)
                    result.pr_url = pr.url
                except gitops.GitError as e:
                    return f"PR failed: {e}"
        return None

    @staticmethod
    def _pr_body(task: TaskView, result: TaskResult, timed_out: bool) -> str:
        head = f"Task `{task.id}` · template `{task.template.value}` · model `{task.model}` · TKBK Cloud Agents"
        note = "\n\n> ⚠️ The time limit was reached; this is a partial result (draft)." if timed_out else ""
        files = "\n".join(f"- `{f}`" for f in result.changed_files[:200])
        return f"{head}{note}\n\n## Agent summary\n\n{result.summary or '(none)'}\n\n## Changed files\n\n{files}\n"


def artifact_path(settings: Settings, hub: EventHub, task_id: str, name: str) -> tuple[Path, str] | None:
    """(path, media type) of a stored artifact or None."""
    table: dict[str, tuple[Path, str]] = {
        "patch": (settings.results_dir / f"{task_id}.patch", "text/x-patch; charset=utf-8"),
        "archive": (settings.results_dir / f"{task_id}.tgz", "application/gzip"),
        "log": (hub.path(task_id), "application/x-ndjson"),
    }
    hit = table.get(name)
    return hit if hit and hit[0].is_file() else None


def list_artifacts(settings: Settings, hub: EventHub, task_id: str) -> list[dict[str, Any]]:
    out = []
    for name in ("patch", "archive", "log"):
        hit = artifact_path(settings, hub, task_id, name)
        if hit:
            p, media = hit
            out.append({"name": name, "bytes": p.stat().st_size, "media_type": media.split(";")[0],
                        "url": f"{settings.public_base_url}/v1/tasks/{task_id}/artifacts/{name}"})
    return out
