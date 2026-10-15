"""FastAPI application: HMAC-signed REST for the site / bot / IDE."""

from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from . import __version__
from .auth import HmacAuth
from .config import Settings
from .models import (
    CreateTaskRequest,
    Deliver,
    TaskCreated,
    TaskStatus,
    TaskView,
    TemplateInfo,
    UsageReport,
    as_json_dict,
)
from .runner import DockerRunner, Runner
from .sandbox import cleanup_orphans
from .store import SecretVault, TaskSecrets, TaskStore, utcnow
from .templates import TEMPLATES, validate_inputs
from .tkbk import check_key, infer_provider, key_id, key_prefix
from .worker import Worker

log = logging.getLogger("tkbk.app")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass
class AppDeps:
    settings: Settings
    store: TaskStore
    vault: SecretVault
    worker: Worker
    http: httpx.AsyncClient


def create_app(settings: Settings | None = None, runner: Runner | None = None,
               http: httpx.AsyncClient | None = None, start_worker: bool = True,
               clean_docker: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    store = TaskStore(settings.db_path)
    vault = SecretVault()
    http_client = http or httpx.AsyncClient(timeout=30.0)
    runner = runner or DockerRunner(settings, store, http=http)
    worker = Worker(settings, store, vault, runner, http=http)
    deps = AppDeps(settings, store, vault, worker, http_client)
    auth = HmacAuth(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        lost = store.fail_active_on_restart("orchestrator restarted; resubmit the task")
        if lost:
            log.warning("marked %d tasks failed after restart", len(lost))
        if clean_docker:
            removed = await cleanup_orphans()
            if any(removed.values()):
                log.warning("cleaned orphans: %s", removed)
        if start_worker:
            worker.start()
        try:
            yield
        finally:
            if start_worker:
                await worker.stop()
            if http is None:
                await http_client.aclose()
            store.close()

    app = FastAPI(title="TKBK Cloud Agents", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.deps = deps

    @app.exception_handler(HTTPException)
    async def _http_exc(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"ok": False, "error": str(exc.detail)}, status_code=exc.status_code,
                            headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------- public
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "active": store.count_active(), "secret_configured": settings.secret_ok}

    # ------------------------------------------------------------- signed
    signed = [Depends(auth)]

    @app.get("/v1/templates", dependencies=signed)
    async def templates() -> dict[str, Any]:
        return {"ok": True, "templates": [as_json_dict(t) for t in TEMPLATES.values()],
                "default_model": settings.default_model,
                "limits": {"default_max_minutes": settings.default_max_minutes,
                           "max_max_minutes": settings.max_max_minutes,
                           "default_max_iterations": settings.default_max_iterations}}

    @app.post("/v1/tasks", dependencies=signed, status_code=201, response_model=TaskCreated)
    async def create_task(req: CreateTaskRequest) -> TaskCreated:
        err = validate_inputs(req.template, req.instruction, req.issue)
        if err:
            raise HTTPException(400, err)
        if req.deliver is Deliver.PR and not req.git_token:
            raise HTTPException(400, "deliver=pr requires git_token (push + pull request)")
        max_minutes = req.max_minutes or settings.default_max_minutes
        if max_minutes > settings.max_max_minutes:
            raise HTTPException(400, f"max_minutes exceeds the server limit of {settings.max_max_minutes}")
        kid = key_id(req.api_key)
        if settings.monthly_minutes_cap:
            used = store.minutes_this_month(kid)
            if used + max_minutes > settings.monthly_minutes_cap:
                raise HTTPException(402, f"monthly minutes cap reached ({used}/{settings.monthly_minutes_cap})")
        chk = await check_key(settings, req.api_key, http_client)
        if not chk.ok:
            raise HTTPException(401 if "rejected" in (chk.error or "") else 502, chk.error or "key check failed")
        model = req.model or settings.default_model
        task = store.create(
            key_id=kid, key_prefix=key_prefix(req.api_key), repo=req.repo, base_branch=req.branch,
            template=req.template, instruction=req.instruction, issue=req.issue, model=model,
            provider=infer_provider(model, req.provider), deliver=req.deliver, max_minutes=max_minutes,
            max_iterations=req.max_iterations or settings.default_max_iterations, client_ref=req.client_ref,
            callback_url=str(req.callback_url) if req.callback_url else None, keep_workspace=req.keep_workspace,
        )
        vault.put(task.id, TaskSecrets(api_key=req.api_key, git_token=req.git_token))
        await worker.wake()
        return TaskCreated(task=store.get(task.id) or task)

    @app.get("/v1/tasks", dependencies=signed)
    async def list_tasks(key_id_: str | None = Query(default=None, alias="key_id"),
                         status: TaskStatus | None = None, limit: int = Query(default=50, ge=1, le=500)) -> dict:
        return {"ok": True, "tasks": [as_json_dict(t) for t in store.list(key_id_, status, limit)]}

    @app.get("/v1/tasks/{task_id}", dependencies=signed)
    async def get_task(task_id: str) -> dict:
        task = _task_or_404(store, task_id)
        return {"ok": True, "task": as_json_dict(task)}

    @app.post("/v1/tasks/{task_id}/cancel", dependencies=signed)
    async def cancel_task(task_id: str) -> dict:
        _task_or_404(store, task_id)
        if not store.request_cancel(task_id):
            raise HTTPException(409, "task already finished")
        return {"ok": True, "task": as_json_dict(store.get(task_id))}  # type: ignore[arg-type]

    @app.get("/v1/tasks/{task_id}/result", dependencies=signed)
    async def get_result(task_id: str) -> dict:
        task = _task_or_404(store, task_id)
        if not task.status.terminal:
            raise HTTPException(409, f"task is {task.status}")
        return {"ok": True, "status": task.status, "error": task.error,
                "minutes_billed": task.minutes_billed, "metrics": as_json_dict(task.metrics),
                "result": as_json_dict(task.result) if task.result else None}

    @app.get("/v1/tasks/{task_id}/archive", dependencies=signed)
    async def get_archive(task_id: str) -> FileResponse:
        task = _task_or_404(store, task_id)
        path = settings.results_dir / f"{task.id}.tgz"
        if not (task.result and task.result.archive_url) or not path.is_file():
            raise HTTPException(404, "no archive for this task")
        return FileResponse(path, media_type="application/gzip", filename=f"{task.id}.tgz")

    @app.get("/v1/usage", dependencies=signed, response_model=UsageReport)
    async def usage(month: str | None = None, key_id_: str | None = Query(default=None, alias="key_id")) -> UsageReport:
        month = month or utcnow().strftime("%Y-%m")
        if not MONTH_RE.match(month):
            raise HTTPException(400, "month must be YYYY-MM")
        return UsageReport(month=month, rows=store.usage(month, key_id_))

    @app.get("/v1/keys/{kid}/usage", dependencies=signed, response_model=UsageReport)
    async def key_usage(kid: str, month: str | None = None) -> UsageReport:
        month = month or utcnow().strftime("%Y-%m")
        if not MONTH_RE.match(month):
            raise HTTPException(400, "month must be YYYY-MM")
        return UsageReport(month=month, rows=store.usage(month, kid))

    return app


def _task_or_404(store: TaskStore, task_id: str) -> TaskView:
    task = store.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    return task


__all__ = ["TemplateInfo", "create_app"]
