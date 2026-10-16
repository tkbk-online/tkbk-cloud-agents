"""Background queue: picks queued tasks (per-client concurrency), runs them, cleans old artifacts."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta

import httpx

from .auth import sign_headers
from .config import Settings
from .events import EventHub
from .models import TaskStatus, TaskView, as_json_dict
from .runner import Runner
from .store import SecretVault, TaskStore

log = logging.getLogger("tkbk.worker")


class Worker:
    def __init__(self, settings: Settings, store: TaskStore, vault: SecretVault, runner: Runner,
                 hub: EventHub, http: httpx.AsyncClient | None = None, tick: float = 1.0):
        self.settings = settings
        self.store = store
        self.vault = vault
        self.runner = runner
        self.hub = hub
        self.http = http
        self.tick = tick
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._janitor_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ control
    def start(self) -> None:
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._loop(), name="tkbk-worker")
        self._janitor_task = asyncio.create_task(self._janitor(), name="tkbk-janitor")

    async def stop(self) -> None:
        self._stop.set()
        for t in (self._loop_task, self._janitor_task):
            if t:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        for t in list(self._tasks.values()):
            t.cancel()
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    @property
    def running(self) -> int:
        return sum(1 for t in self._tasks.values() if not t.done())

    async def wake(self) -> None:
        """Try to dispatch immediately (called after a task becomes queued); also runs on every tick."""
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
            task = self.store.claim_next_queued(self.store.active_by_key())
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
            self.hub.finish(task.id)
        finally:
            self.vault.drop(task.id)
            final = self.store.get(task.id)
            if final is not None:
                await self._callback(final)

    async def _callback(self, task: TaskView) -> None:
        url = self.store.callback_url(task.id)
        if not url:
            return
        body = json.dumps({"ok": True, "event": "task.finished", "task": as_json_dict(task)}).encode()
        path = httpx.URL(url).raw_path.decode()
        headers = {"Content-Type": "application/json", **sign_headers(self.settings.internal_secret, "POST", path, body)}
        client = self.http or httpx.AsyncClient(timeout=15.0)
        try:
            await client.post(url, content=body, headers=headers)
        except httpx.HTTPError as e:
            log.warning("callback for %s failed: %s", task.id, e)
        finally:
            if self.http is None:
                await client.aclose()

    # ----------------------------------------------------------------- janitor
    async def _janitor(self) -> None:
        while not self._stop.is_set():
            try:
                self.sweep()
            except Exception:
                log.exception("janitor failed")
            await asyncio.sleep(600)

    def sweep(self, now: datetime | None = None) -> int:
        """Delete patch/archive/event files of tasks finished more than RESULTS_TTL_DAYS ago."""
        ttl = self.settings.results_ttl_days
        if ttl <= 0:
            return 0
        now = now or datetime.now(UTC)
        cutoff = (now - timedelta(days=ttl)).replace(microsecond=0).isoformat()
        removed = 0
        for tid in self.store.finished_before(cutoff):
            for p in (self.settings.results_dir / f"{tid}.patch", self.settings.results_dir / f"{tid}.tgz",
                      self.hub.path(tid)):
                if p.is_file():
                    try:
                        p.unlink()
                        removed += 1
                    except OSError:
                        pass
        return removed
