"""FastAPI application.

Two callers, one API:
  * the IDE / TKBK Agent — `Authorization: Bearer cr_…`; the key is checked against the site's license
    (Pro / Teams), sees only its own tasks, limited by the plan's hours and concurrency;
  * the bot / site / admin — HMAC (X-TKBK-Ts + X-TKBK-Sig, same scheme as the bot), sees everything and
    passes the client's key in the body.
"""

from __future__ import annotations

import io
import logging
import re
import tarfile
import time
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse

from . import __version__
from .auth import HmacAuth
from .config import Settings
from .events import EventHub, sse_format
from .license import License, LicenseClient, concurrency_for
from .models import (
    KEY_RE,
    CreateTaskRequest,
    Deliver,
    Me,
    Source,
    TaskCreated,
    TaskStatus,
    TaskView,
    UsageReport,
    as_json_dict,
)
from .runner import DockerRunner, Runner, artifact_path, list_artifacts
from .sandbox import cleanup_orphans, remove_tree
from .store import SecretVault, TaskSecrets, TaskStore, utcnow
from .templates import TEMPLATES, validate_inputs
from .tkbk import check_key, infer_provider, key_id, key_prefix

log = logging.getLogger("tkbk.app")
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
TASK_ID_RE = re.compile(r"^t_[0-9a-f]{16}$")


@dataclass
class Principal:
    kind: str                      # "client" (Bearer cr_…) | "admin" (HMAC)
    api_key: str | None = None
    key_id: str | None = None
    license: License | None = None

    @property
    def is_client(self) -> bool:
        return self.kind == "client"


@dataclass
class AppDeps:
    settings: Settings
    store: TaskStore
    vault: SecretVault
    worker: Any
    http: httpx.AsyncClient
    hub: EventHub
    licenses: LicenseClient


class KeyRateLimiter:
    def __init__(self, per_minute: int = 60):
        self.per_minute = per_minute
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, kid: str) -> bool:
        now = time.monotonic()
        q = self._hits[kid]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True


def create_app(settings: Settings | None = None, runner: Runner | None = None,
               http: httpx.AsyncClient | None = None, start_worker: bool = True,
               clean_docker: bool = True) -> FastAPI:
    from .worker import Worker

    settings = settings or Settings.from_env()
    settings.results_dir.mkdir(parents=True, exist_ok=True)
    settings.events_dir.mkdir(parents=True, exist_ok=True)
    store = TaskStore(settings.db_path)
    vault = SecretVault()
    hub = EventHub(settings.events_dir)
    http_client = http or httpx.AsyncClient(timeout=30.0)
    licenses = LicenseClient(settings, http_client)
    runner = runner or DockerRunner(settings, store, hub, http=http)
    worker = Worker(settings, store, vault, runner, hub, http=http)
    deps = AppDeps(settings, store, vault, worker, http_client, hub, licenses)
    hmac_auth = HmacAuth(settings)
    key_limiter = KeyRateLimiter()

    if settings.license_mode == "stub":
        log.warning("LICENSE_MODE=stub: every well-formed cr_ key is treated as plan %r. LOCAL USE ONLY.",
                    settings.stub_plan)

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

    # ---------------------------------------------------------------- auth
    async def principal(request: Request) -> Principal:
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            key = auth[7:].strip()
            if not KEY_RE.match(key):
                raise HTTPException(401, "bad bearer key (expected cr_…)")
            kid = key_id(key)
            if not key_limiter.allow(kid):
                raise HTTPException(429, "rate limit for this key")
            lic = await licenses.check(key)
            return Principal("client", api_key=key, key_id=kid, license=lic)
        await hmac_auth(request)
        return Principal("admin")

    def require_cloud(p: Principal) -> License:
        lic = p.license
        if lic is None or not lic.ok:
            err = (lic.error if lic else None) or "no license"
            code = 503 if "unreachable" in err or "rate limit" in err else 403
            raise HTTPException(code, err)
        return lic

    def task_for(p: Principal, task_id: str) -> TaskView:
        if not TASK_ID_RE.match(task_id):
            raise HTTPException(404, "task not found")
        task = store.get(task_id)
        if task is None or (p.is_client and task.key_id != p.key_id):
            raise HTTPException(404, "task not found")
        return task

    # ------------------------------------------------------------- public
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": __version__, "active": store.count_active(),
                "secret_configured": settings.secret_ok, "license_mode": settings.license_mode}

    # ------------------------------------------------------------ account
    @app.get("/v1/me", response_model=Me)
    async def me(p: Principal = Depends(principal)) -> Me:
        if not p.is_client:
            raise HTTPException(400, "GET /v1/me is for Bearer cr_… callers")
        lic = p.license
        assert lic is not None and p.key_id and p.api_key
        used = store.minutes_this_month(p.key_id)
        quota = lic.minutes
        return Me(key_id=p.key_id, key_prefix=key_prefix(p.api_key), plan=lic.plan, cloud_agents=lic.ok,
                  hours_month=lic.hours, minutes_used=used, minutes_left=max(0, quota - used),
                  concurrent_limit=concurrency_for(settings, lic.plan) if lic.ok else 0,
                  active_tasks=store.count_active(p.key_id), until=lic.until, error=lic.error)

    @app.get("/v1/templates")
    async def templates(_: Principal = Depends(principal)) -> dict[str, Any]:
        return {"ok": True, "templates": [as_json_dict(t) for t in TEMPLATES.values()],
                "default_model": settings.default_model,
                "limits": {"default_max_minutes": settings.default_max_minutes,
                           "max_max_minutes": settings.max_max_minutes,
                           "default_max_iterations": settings.default_max_iterations,
                           "upload_max_bytes": settings.upload_max_bytes}}

    # -------------------------------------------------------------- tasks
    @app.post("/v1/tasks", status_code=201, response_model=TaskCreated)
    async def create_task(req: CreateTaskRequest, p: Principal = Depends(principal)) -> TaskCreated:
        if p.is_client:
            if req.api_key and req.api_key != p.api_key:
                raise HTTPException(400, "api_key in the body does not match the bearer key")
            api_key, lic = p.api_key, require_cloud(p)
        else:
            if not req.api_key:
                raise HTTPException(400, "api_key is required")
            api_key = req.api_key
            lic = await licenses.check(api_key)
            if not lic.ok:
                raise HTTPException(403, lic.error or "no cloud agents on this key")
            if settings.key_check:
                chk = await check_key(settings, api_key, http_client)
                if not chk.ok:
                    raise HTTPException(401 if "rejected" in (chk.error or "") else 502, chk.error or "key check failed")
        assert api_key is not None
        err = validate_inputs(req.template, req.instruction, req.issue)
        if err:
            raise HTTPException(400, err)
        if req.deliver is Deliver.PR and not req.git_token:
            raise HTTPException(400, "deliver=pr requires git_token (push + pull request)")
        if req.push and not req.git_token:
            raise HTTPException(400, "push=true requires git_token")
        if (req.push or req.deliver is Deliver.PR) and req.source is Source.UPLOAD:
            raise HTTPException(400, "push/PR need source=repo")
        max_minutes = req.max_minutes or settings.default_max_minutes
        if max_minutes > settings.max_max_minutes:
            raise HTTPException(400, f"max_minutes exceeds the server limit of {settings.max_max_minutes}")
        kid = key_id(api_key)
        used = store.minutes_this_month(kid)
        if used + max_minutes > lic.minutes:
            raise HTTPException(402, f"monthly cloud-agent hours of plan {lic.plan} exhausted "
                                     f"({used}/{lic.minutes} min used, task needs {max_minutes})")
        if settings.monthly_minutes_cap and used + max_minutes > settings.monthly_minutes_cap:
            raise HTTPException(402, f"monthly minutes cap reached ({used}/{settings.monthly_minutes_cap})")
        model = req.model or settings.default_model
        task = store.create(
            status=TaskStatus.AWAITING_UPLOAD if req.source is Source.UPLOAD else TaskStatus.QUEUED,
            source=req.source, key_id=kid, key_prefix=key_prefix(api_key), plan=lic.plan, repo=req.repo,
            base_branch=req.branch, template=req.template, instruction=req.instruction, issue=req.issue,
            model=model, provider=infer_provider(model, req.provider), deliver=req.deliver, push=req.push,
            max_minutes=max_minutes, max_iterations=req.max_iterations or settings.default_max_iterations,
            concurrency_limit=concurrency_for(settings, lic.plan), client_ref=req.client_ref,
            callback_url=str(req.callback_url) if req.callback_url else None, keep_workspace=req.keep_workspace,
        )
        vault.put(task.id, TaskSecrets(api_key=api_key, git_token=req.git_token))
        hub.open(task.id).emit("status", status=str(task.status), phase="")
        upload_url = None
        if req.source is Source.UPLOAD:
            upload_url = f"{settings.public_base_url}/v1/tasks/{task.id}/workspace"
        else:
            await worker.wake()
        return TaskCreated(task=store.get(task.id) or task, upload_url=upload_url)

    @app.put("/v1/tasks/{task_id}/workspace")
    async def upload_workspace(task_id: str, request: Request, p: Principal = Depends(principal)) -> dict[str, Any]:
        task = task_for(p, task_id)
        if task.status is not TaskStatus.AWAITING_UPLOAD:
            raise HTTPException(409, f"task is {task.status}, not awaiting an upload")
        length = int(request.headers.get("content-length") or 0)
        if length > settings.upload_max_bytes:
            raise HTTPException(413, f"workspace archive exceeds {settings.upload_max_bytes} bytes")
        buf = io.BytesIO()
        async for chunk in request.stream():
            buf.write(chunk)
            if buf.tell() > settings.upload_max_bytes:
                raise HTTPException(413, f"workspace archive exceeds {settings.upload_max_bytes} bytes")
        buf.seek(0)
        repo_dir = settings.tasks_root / task.id / "workspace" / "repo"
        try:
            files = extract_workspace(buf, repo_dir)
        except (tarfile.TarError, OSError, ValueError) as e:
            remove_tree(settings.tasks_root / task.id)
            raise HTTPException(400, f"bad workspace archive: {e}") from e
        store.set_status(task.id, TaskStatus.QUEUED, "")
        hub.open(task.id).emit("status", status="queued", phase="", files=files)
        await worker.wake()
        return {"ok": True, "task": as_json_dict(store.get(task.id)), "files": files}  # type: ignore[arg-type]

    @app.get("/v1/tasks")
    async def list_tasks(p: Principal = Depends(principal), key_id_: str | None = Query(default=None, alias="key_id"),
                         status: TaskStatus | None = None, limit: int = Query(default=50, ge=1, le=500)) -> dict:
        kid = p.key_id if p.is_client else key_id_
        return {"ok": True, "tasks": [as_json_dict(t) for t in store.list(kid, status, limit)]}

    @app.get("/v1/tasks/{task_id}")
    async def get_task(task_id: str, p: Principal = Depends(principal)) -> dict:
        task = task_for(p, task_id)
        return {"ok": True, "task": as_json_dict(task), "log_tail": hub.replay(task.id)[-20:]}

    @app.post("/v1/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str, p: Principal = Depends(principal)) -> dict:
        task_for(p, task_id)
        if not store.request_cancel(task_id):
            raise HTTPException(409, "task already finished")
        t = store.get(task_id)
        if t and t.status is TaskStatus.CANCELLED:
            hub.open(task_id).emit("done", status="cancelled", error="cancelled by client")
            hub.finish(task_id)
            vault.drop(task_id)
        return {"ok": True, "task": as_json_dict(t)}  # type: ignore[arg-type]

    @app.get("/v1/tasks/{task_id}/result")
    async def get_result(task_id: str, p: Principal = Depends(principal)) -> dict:
        task = task_for(p, task_id)
        if not task.status.terminal:
            raise HTTPException(409, f"task is {task.status}")
        return {"ok": True, "status": task.status, "error": task.error,
                "minutes_billed": task.minutes_billed, "metrics": as_json_dict(task.metrics),
                "result": as_json_dict(task.result) if task.result else None,
                "artifacts": list_artifacts(settings, hub, task.id)}

    @app.get("/v1/tasks/{task_id}/diff")
    async def get_diff(task_id: str, p: Principal = Depends(principal)) -> PlainTextResponse:
        task = task_for(p, task_id)
        hit = artifact_path(settings, hub, task.id, "patch")
        if not hit:
            raise HTTPException(404, "no patch for this task (yet)")
        return PlainTextResponse(hit[0].read_text(encoding="utf-8"), media_type="text/x-patch")

    @app.get("/v1/tasks/{task_id}/artifacts")
    async def get_artifacts(task_id: str, p: Principal = Depends(principal)) -> dict:
        task = task_for(p, task_id)
        return {"ok": True, "artifacts": list_artifacts(settings, hub, task.id)}

    @app.get("/v1/tasks/{task_id}/artifacts/{name}")
    async def get_artifact(task_id: str, name: str, p: Principal = Depends(principal)) -> FileResponse:
        task = task_for(p, task_id)
        hit = artifact_path(settings, hub, task.id, name)
        if not hit:
            raise HTTPException(404, "no such artifact")
        ext = {"patch": "patch", "archive": "tgz", "log": "jsonl"}[name]
        return FileResponse(hit[0], media_type=hit[1], filename=f"{task.id}.{ext}")

    # legacy alias kept for the bot stub
    @app.get("/v1/tasks/{task_id}/archive")
    async def get_archive(task_id: str, p: Principal = Depends(principal)) -> FileResponse:
        return await get_artifact(task_id, "archive", p)

    @app.get("/v1/tasks/{task_id}/events")
    async def stream_events(task_id: str, request: Request, p: Principal = Depends(principal),
                            after: int = Query(default=0, ge=0)) -> StreamingResponse:
        task = task_for(p, task_id)
        last_id = request.headers.get("Last-Event-ID")
        if last_id and last_id.isdigit():
            after = max(after, int(last_id))

        async def gen() -> AsyncIterator[str]:
            live = hub.get(task.id)
            if live is None or task.status.terminal:
                for ev in hub.replay(task.id, after):
                    yield sse_format(ev)
                return
            async for ev in live.subscribe(after):
                if await request.is_disconnected():
                    return
                yield sse_format(ev)
                if ev.get("type") == "done":
                    return

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # --------------------------------------------------------------- usage
    @app.get("/v1/usage", response_model=UsageReport)
    async def usage(p: Principal = Depends(principal), month: str | None = None,
                    key_id_: str | None = Query(default=None, alias="key_id")) -> UsageReport:
        month = month or utcnow().strftime("%Y-%m")
        if not MONTH_RE.match(month):
            raise HTTPException(400, "month must be YYYY-MM")
        kid = p.key_id if p.is_client else key_id_
        return UsageReport(month=month, rows=store.usage(month, kid))

    @app.get("/v1/keys/{kid}/usage", response_model=UsageReport)
    async def key_usage(kid: str, p: Principal = Depends(principal), month: str | None = None) -> UsageReport:
        if p.is_client and kid != p.key_id:
            raise HTTPException(404, "unknown key")
        month = month or utcnow().strftime("%Y-%m")
        if not MONTH_RE.match(month):
            raise HTTPException(400, "month must be YYYY-MM")
        return UsageReport(month=month, rows=store.usage(month, kid))

    return app


def extract_workspace(buf: io.BytesIO, repo_dir) -> int:
    """Safely extract an uploaded tar(.gz) into repo_dir. Returns the number of files."""
    remove_tree(repo_dir)
    repo_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=buf, mode="r:*") as tar:
        members = tar.getmembers()
        if not members:
            raise ValueError("empty archive")
        # strip a single top-level directory ("repo/…") if every entry shares it
        tops = {m.name.split("/", 1)[0] for m in members if m.name not in (".", "")}
        strip = len(tops) == 1 and all("/" in m.name or m.isdir() for m in members)
        count = 0
        for m in members:
            if strip:
                if "/" not in m.name:
                    continue
                m.name = m.name.split("/", 1)[1]
            if not m.name or m.name == ".":
                continue
            if m.issym() or m.islnk():
                continue  # links are dropped: they could point outside the sandbox workspace
            tar.extract(m, path=repo_dir, filter="data")
            if m.isfile():
                count += 1
    return count
