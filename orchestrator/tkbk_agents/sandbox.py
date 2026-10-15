"""One sandbox container per task.

Layout per task (host paths; TASKS_ROOT is bind-mounted into the orchestrator at the same path):
    {TASKS_ROOT}/{task_id}/workspace/        -> /workspace in the sandbox (repo at /workspace/repo)

Network: a dedicated `--internal` network `tkbk-task-{id}`; the egress proxy and the
orchestrator are attached to it for the task's lifetime. The sandbox has no other
network, so the only way out is HTTP(S)_PROXY -> squid allow-list.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from .config import Settings
from .docker_cli import DockerError, DockerFn, docker

LABEL = "tkbk.cloud-agents"
SANDBOX_PORT = 8000
WORKSPACE_IN_SANDBOX = "/workspace"
REPO_IN_SANDBOX = f"{WORKSPACE_IN_SANDBOX}/repo"


@dataclass(frozen=True)
class SandboxSpec:
    task_id: str
    image: str
    memory: str
    cpus: str
    pids_limit: int
    egress_proxy: str
    workspace_host_dir: str
    session_key: str
    secret_key: str
    network: str
    max_concurrent_runs: int = 1

    @property
    def container(self) -> str:
        return f"tkbk-sbx-{self.task_id}"

    @property
    def base_url(self) -> str:
        return f"http://{self.container}:{SANDBOX_PORT}"


def run_args(spec: SandboxSpec) -> list[str]:
    """`docker run` arguments. Pure function (unit-tested): no client secrets ever appear here."""
    env = {
        "OH_SESSION_API_KEYS_0": spec.session_key,
        "OH_SECRET_KEY": spec.secret_key,
        "OH_CONVERSATIONS_PATH": f"{WORKSPACE_IN_SANDBOX}/.openhands/conversations",
        "OH_PERSISTENCE_DIR": f"{WORKSPACE_IN_SANDBOX}/.openhands/persistence",
        "OH_WORKSPACE_PATH": REPO_IN_SANDBOX,
        "OH_MAX_CONCURRENT_RUNS": str(spec.max_concurrent_runs),
        "OH_ENABLE_VSCODE": "false",
        "OH_ENABLE_BROWSER": "false",
        "OH_TELEMETRY_EXPORTER": "none",
        "DO_NOT_TRACK": "1",
        "HTTP_PROXY": spec.egress_proxy,
        "HTTPS_PROXY": spec.egress_proxy,
        "http_proxy": spec.egress_proxy,
        "https_proxy": spec.egress_proxy,
        "NO_PROXY": "localhost,127.0.0.1",
        "no_proxy": "localhost,127.0.0.1",
        "GIT_TERMINAL_PROMPT": "0",
    }
    args = [
        "run", "-d",
        "--name", spec.container,
        "--hostname", "sandbox",
        "--label", f"{LABEL}={spec.task_id}",
        "--network", spec.network,
        "--network-alias", "sandbox",
        "--memory", spec.memory,
        "--memory-swap", spec.memory,          # no swap for the sandbox
        "--cpus", spec.cpus,
        "--pids-limit", str(spec.pids_limit),
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--ulimit", "core=0",
        "--ulimit", "nofile=65536:65536",
        # no tmpfs on /tmp: the agent-server is a PyInstaller binary that unpacks itself into /tmp
        # and needs an exec-able, disk-backed /tmp (tmpfs would also count against the memory limit).
        "-v", f"{spec.workspace_host_dir}:{WORKSPACE_IN_SANDBOX}",
        "--restart", "no",
    ]
    for k, v in env.items():
        args += ["-e", f"{k}={v}"]
    args += [spec.image, "--host", "0.0.0.0", "--port", str(SANDBOX_PORT)]
    return args


@dataclass
class Sandbox:
    settings: Settings
    task_id: str
    docker_fn: DockerFn = docker
    http: httpx.AsyncClient | None = None
    spec: SandboxSpec = field(init=False)
    _started: bool = field(default=False, init=False)
    _network_created: bool = field(default=False, init=False)
    _attached: list[str] = field(default_factory=list, init=False)
    started_at: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        s = self.settings
        self.spec = SandboxSpec(
            task_id=self.task_id,
            image=s.sandbox_image,
            memory=s.sandbox_memory,
            cpus=s.sandbox_cpus,
            pids_limit=s.sandbox_pids_limit,
            egress_proxy=s.egress_proxy,
            workspace_host_dir=str(self.workspace_dir),
            session_key=secrets.token_urlsafe(32),
            secret_key=secrets.token_urlsafe(32),
            network=f"tkbk-task-{self.task_id}",
        )

    @property
    def task_dir(self) -> Path:
        return self.settings.tasks_root / self.task_id

    @property
    def workspace_dir(self) -> Path:
        return self.task_dir / "workspace"

    @property
    def repo_dir(self) -> Path:
        return self.workspace_dir / "repo"

    # ------------------------------------------------------------ lifecycle
    def prepare_workspace(self) -> None:
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.chown_workspace()

    def chown_workspace(self) -> None:
        """Give the workspace to the sandbox user (uid 10001). No-op where chown is impossible (tests, Windows)."""
        uid = self.settings.sandbox_uid
        if not hasattr(os, "chown") or os.name == "nt":
            return
        for root, dirs, files in os.walk(self.workspace_dir):
            try:
                os.chown(root, uid, uid)
                for n in files:
                    os.lchown(os.path.join(root, n), uid, uid)
            except PermissionError:
                return

    async def start(self) -> None:
        d, spec = self.docker_fn, self.spec
        self.prepare_workspace()
        await d("network", "create", "--internal", "--label", f"{LABEL}={self.task_id}", spec.network)
        self._network_created = True
        for name in (self.settings.egress_container, self.settings.self_container):
            try:
                await d("network", "connect", spec.network, name)
                self._attached.append(name)
            except DockerError as e:
                if "already exists" not in e.stderr:
                    raise
        await d(*run_args(spec), timeout=300)
        self._started = True
        self.started_at = time.monotonic()
        await self.wait_healthy()

    async def wait_healthy(self) -> None:
        deadline = time.monotonic() + self.settings.sandbox_startup_timeout
        client = self.http or httpx.AsyncClient()
        try:
            while time.monotonic() < deadline:
                try:
                    r = await client.get(f"{self.spec.base_url}/health", timeout=5)
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                try:
                    state = (await self.docker_fn("inspect", "-f", "{{.State.Running}}", self.spec.container)).strip()
                except DockerError:
                    state = "false"
                if state != "true":
                    logs = ""
                    try:
                        logs = await self.docker_fn("logs", "--tail", "30", self.spec.container)
                    except DockerError:
                        pass
                    raise RuntimeError(f"sandbox exited during startup: {logs[-1500:]}")
                await asyncio.sleep(2)
        finally:
            if self.http is None:
                await client.aclose()
        raise RuntimeError("sandbox did not become healthy in time")

    async def stop(self, keep_workspace: bool = False) -> None:
        d, spec = self.docker_fn, self.spec
        if self._started:
            try:
                await d("rm", "-f", spec.container, timeout=60)
            except DockerError:
                pass
            self._started = False
        for name in self._attached:
            try:
                await d("network", "disconnect", "-f", spec.network, name)
            except DockerError:
                pass
        self._attached.clear()
        if self._network_created:
            try:
                await d("network", "rm", spec.network)
            except DockerError:
                pass
            self._network_created = False
        if not keep_workspace:
            remove_tree(self.task_dir)

    @property
    def elapsed_seconds(self) -> float:
        return 0.0 if self.started_at is None else time.monotonic() - self.started_at


def remove_tree(path: Path) -> None:
    """rmtree that copes with read-only files (git objects on Windows) and never raises."""
    if not path.exists():
        return

    def _onexc(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    try:
        shutil.rmtree(path, onexc=_onexc)
    except OSError:
        pass


async def cleanup_orphans(docker_fn: DockerFn = docker) -> dict[str, int]:
    """Remove sandboxes/networks left over from a previous orchestrator process."""
    removed = {"containers": 0, "networks": 0}
    try:
        ids = (await docker_fn("ps", "-aq", "--filter", f"label={LABEL}")).split()
        if ids:
            await docker_fn("rm", "-f", *ids, timeout=120)
            removed["containers"] = len(ids)
        nets = (await docker_fn("network", "ls", "-q", "--filter", f"label={LABEL}")).split()
        for n in nets:
            try:
                # Detach whatever is still attached (egress / api), then remove.
                out = await docker_fn("network", "inspect", "-f", "{{range .Containers}}{{.Name}} {{end}}", n)
                for name in out.split():
                    await docker_fn("network", "disconnect", "-f", n, name)
                await docker_fn("network", "rm", n)
                removed["networks"] += 1
            except DockerError:
                continue
    except DockerError:
        pass
    return removed
