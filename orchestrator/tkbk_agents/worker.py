"""Background queue: picks queued tasks, runs them with the Runner under a concurrency cap."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

from .auth import sign_headers
from .config import Settings
from .models import TaskStatus, TaskView, as_json_dict
from .runner import Runner
from .store import SecretVault, TaskStore

log = logging.getLogger("tkbk.worker")


class Worker:
    def __init__(self, settings: Settings, store: TaskStore, vault: SecretVault, runner: Runner,
                 http: httpx.AsyncClient | None = None, tick: float = 1.0):
        self.settings = settings
        self.store = store
        self.vault = vault
        self.runner = runner
        self.http = http
        self.tick = tick
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._loop(), name="tkbk-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for t in list(self._tasks.values()):
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    @property
    def running(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    async def wake(self) -> None:
        """Try to dispatch immediately (called after POST /v1/tasks); also runs on every tick."""
        await self._dispatch()

    # -------------------------------------------------------------------- loop
    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self._dispatch()
            except Exception:
                log.exception("dispatch failed")
            await asyncio.sleep(self.tick)

    async def _dispatch(self) -> None:
        self._tasks = {k: v for k, v in self._tasks.items() if not v.done()}
        while len(self._tasks) < self.settings.max_concurrent:
            task = self.store.claim_next_queued()
            if task is None:
                return
            secrets = self.vault.get(task.id)
            if secrets is None:
                self.store.set_status(task.id, TaskStatus.FAILED, error="secrets missing (orchestrator restarted?)")
                continue
            self._tasks[task.id] = asyncio.create_task(self._run_one(task), name=f"task-{task.id}")

    async def _run_one(self, task: TaskView) -> None:
        secrets = self.vault.get(task.id)
        assert secrets is not None
        try:
            await self.runner.run(task, secrets)
        except asyncio.CancelledError:
            self.store.set_status(task.id, TaskStatus.FAILED, error="orchestrator shutting down")
            raise
        except Exception as e:
            log.exception("task %s crashed", task.id)
            self.store.set_status(task.id, TaskStatus.FAILED, error=f"internal: {type(e).__name__}: {e}"[:800])
        finally:
            self.vault.drop(task.id)
            final = self.store.get(task.id)
            if final is not None:
                await self._callback(final)

    async def _callback(self, task: TaskView) -> None:
        row = self.store.callback_url(task.id)
        if not row:
            return
        body = json.dumps({"ok": True, "event": "task.finished", "task": as_json_dict(task)}).encode()
        path = httpx.URL(row).raw_path.decode()
        headers = {"Content-Type": "application/json", **sign_headers(self.settings.internal_secret, "POST", path, body)}
        client = self.http or httpx.AsyncClient(timeout=15.0)
        try:
            await client.post(row, content=body, headers=headers)
        except httpx.HTTPError as e:
            log.warning("callback for %s failed: %s", task.id, e)
        finally:
            if self.http is None:
                await client.aclose()
