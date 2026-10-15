"""Runs one task end to end: sandbox -> clone -> agent -> commit/push/PR or archive -> billing."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from . import gitops
from .agent_client import AgentServerClient, AgentServerError
from .config import Settings
from .docker_cli import DockerFn, docker
from .models import Deliver, Provider, TaskMetrics, TaskResult, TaskStatus, TaskView, Template
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
    return f"tkbk/{template.value.replace('_', '-')}-{task_id.removeprefix('t_')}"


def _slug(text: str, n: int = 60) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n] + ("…" if len(text) > n else "")


@dataclass
class DockerRunner:
    settings: Settings
    store: TaskStore
    docker_fn: DockerFn = docker
    http: httpx.AsyncClient | None = None      # injected in tests (MockTransport)
    github: gitops.GitHub | None = None

    async def run(self, task: TaskView, secrets: TaskSecrets) -> None:
        s = self.settings
        store = self.store
        sandbox = Sandbox(s, task.id, docker_fn=self.docker_fn, http=self.http)
        gh = self.github or gitops.GitHub(http=self.http)
        t0 = _now()
        status, error = TaskStatus.DONE, None
        result = TaskResult()
        metrics = TaskMetrics()
        conversation_id: str | None = None
        client: AgentServerClient | None = None
        base_sha = ""
        owner, repo_name = gitops.parse_repo(task.repo)
        work_branch = work_branch_name(task.id, task.template)
        store.update(task.id, work_branch=work_branch)
        try:
            # 1) inputs that need the network: issue text, default branch
            self._checkpoint(task.id)
            store.set_status(task.id, TaskStatus.PREPARING, "fetching-inputs")
            instruction, issue_no = store.instruction_and_issue(task.id)
            issue: IssueInfo | None = None
            if task.template is Template.FIX_ISSUE and issue_no:
                issue = await gh.get_issue(owner, repo_name, issue_no, secrets.git_token)
            base_branch = task.base_branch or await gh.default_branch(owner, repo_name, secrets.git_token)
            store.update(task.id, base_branch=base_branch)

            # 2) clone (by the orchestrator, with the token; the sandbox never sees it)
            store.set_status(task.id, TaskStatus.PREPARING, "cloning")
            sandbox.prepare_workspace()
            base_sha = await gitops.clone(task.repo, sandbox.repo_dir, base_branch, secrets.git_token)
            await gitops.create_branch(sandbox.repo_dir, work_branch)
            sandbox.chown_workspace()

            # 3) sandbox
            self._checkpoint(task.id)
            store.set_status(task.id, TaskStatus.PREPARING, "starting-sandbox")
            await sandbox.start()

            # 4) conversation
            client = AgentServerClient(sandbox.spec.base_url, sandbox.spec.session_key, http=self.http)
            prompt = build_prompt(task.template, repo=task.repo, work_branch=work_branch, workdir=REPO_IN_SANDBOX,
                                  instruction=instruction, issue=issue)
            llm = llm_settings(s, task.model, Provider(task.provider), secrets.api_key)
            conv = await client.start_conversation(llm=llm, working_dir=REPO_IN_SANDBOX, message=prompt,
                                                   max_iterations=task.max_iterations)
            conversation_id = conv.id
            await client.run(conversation_id)
            store.set_status(task.id, TaskStatus.RUNNING, "agent")

            # 5) poll until terminal / timeout / cancel
            deadline = _now() + task.max_minutes * 60
            metrics = await self._poll(client, conversation_id, deadline, task.id)
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

        # 6) deliver whatever exists (also after a timeout), then clean up
        try:
            if status in (TaskStatus.DONE, TaskStatus.TIMEOUT) and sandbox.repo_dir.exists():
                store.set_status(task.id, TaskStatus.DELIVERING, "delivering")
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
            store.update(task.id, status=status, phase="", error=error, result=result, metrics=metrics,
                         minutes_billed=minutes, finished_at=utcnow())

    # ------------------------------------------------------------------ helpers
    def _checkpoint(self, task_id: str) -> None:
        if self.store.cancel_requested(task_id):
            raise Cancelled()

    async def _poll(self, client: AgentServerClient, cid: str, deadline: float, task_id: str) -> TaskMetrics:
        last = TaskMetrics()
        seen_running = False
        while True:
            self._checkpoint(task_id)
            if _now() >= deadline:
                raise TaskTimeout()
            st = await client.state(cid)
            last = st.metrics
            self.store.update(task_id, metrics=last)
            if st.execution_status == "running":
                seen_running = True
            elif st.terminal or (seen_running and st.execution_status in ("idle", "paused")):
                if st.execution_status == "error":
                    raise AgentServerError(f"agent error: {st.raw.get('error') or st.execution_status}")
                return last
            elif st.execution_status in ("waiting_for_confirmation",):
                # NeverConfirm is set; should not happen. Treat as finished to avoid a hang.
                return last
            await asyncio.sleep(self.settings.poll_interval)

    async def _deliver(self, task: TaskView, secrets: TaskSecrets, sandbox: Sandbox, gh: gitops.GitHub,
                       owner: str, repo_name: str, work_branch: str, base_sha: str, result: TaskResult,
                       timed_out: bool) -> str | None:
        """Returns an error string or None."""
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
        if not result.changed_files:
            result.summary = (result.summary + "\n\n(no file changes were produced)").strip()
            return None
        if task.deliver is Deliver.PR:
            if not secrets.git_token:
                return "PR delivery needs git_token"
            try:
                await gitops.push(repo_dir, work_branch, secrets.git_token)
                title = ("[timeout] " if timed_out else "") + f"TKBK agent: {summary_line}"
                body = self._pr_body(task, result, timed_out)
                pr = await gh.create_pr(owner, repo_name, head=work_branch, base=task.base_branch or "main",
                                        title=title, body=body, token=secrets.git_token, draft=timed_out)
                result.pr_url = pr.url
            except gitops.GitError as e:
                return f"push/PR failed: {e}"
        else:
            out = s.results_dir / f"{task.id}.tgz"
            try:
                gitops.make_archive(repo_dir, out)
            except OSError as e:
                return f"archive failed: {e}"
            result.archive_url = f"{s.public_base_url}/v1/tasks/{task.id}/archive"
        return None

    @staticmethod
    def _pr_body(task: TaskView, result: TaskResult, timed_out: bool) -> str:
        head = f"Task `{task.id}` · template `{task.template.value}` · model `{task.model}` · TKBK Cloud Agents"
        note = "\n\n> ⚠️ The time limit was reached; this is a partial result (draft)." if timed_out else ""
        files = "\n".join(f"- `{f}`" for f in result.changed_files[:200])
        return f"{head}{note}\n\n## Agent summary\n\n{result.summary or '(none)'}\n\n## Changed files\n\n{files}\n"
