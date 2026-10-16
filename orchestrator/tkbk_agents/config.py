"""Settings read from the environment (see .env.example at the repo root)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

MIN_SECRET_LEN = 32


class ConfigError(ValueError):
    pass


def _int(env: Mapping[str, str], name: str, default: int, lo: int = 0, hi: int = 10**9) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from e
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be in [{lo}, {hi}], got {value}")
    return value


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from e


def _bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _list(env: Mapping[str, str], name: str, default: list[str]) -> list[str]:
    raw = env.get(name)
    if raw is None:
        return default
    return [x.strip() for x in raw.split(",") if x.strip()]


def _plan_map(env: Mapping[str, str], name: str, default: dict[str, int]) -> dict[str, int]:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    out: dict[str, int] = {}
    for part in raw.split(","):
        if not part.strip():
            continue
        try:
            plan, n = part.split(":")
            out[plan.strip()] = int(n)
        except ValueError as e:
            raise ConfigError(f"{name}: expected plan:n pairs, got {part!r}") from e
    return out


@dataclass(frozen=True)
class Settings:
    internal_secret: str
    public_base_url: str
    data_dir: Path
    db_path: Path
    results_dir: Path
    events_dir: Path
    anthropic_base_url: str
    openai_base_url: str
    models_url: str
    default_model: str
    key_check: bool
    # license
    license_mode: str
    license_check_url: str
    license_public_key: str
    license_cache_seconds: float
    license_app: str
    stub_plan: str
    concurrent_per_plan: dict[str, int]
    concurrent_default: int
    # sandbox
    sandbox_image: str
    sandbox_memory: str
    sandbox_cpus: str
    sandbox_pids_limit: int
    sandbox_uid: int
    egress_proxy: str
    no_proxy: str
    tasks_root: Path
    tasks_volume: str
    attach_containers: list[str]
    max_concurrent: int
    default_max_minutes: int
    max_max_minutes: int
    default_max_iterations: int
    monthly_minutes_cap: int
    upload_max_bytes: int
    results_ttl_days: int
    git_name: str
    git_email: str
    poll_interval: float
    sandbox_startup_timeout: float
    version: str = __version__
    max_body_bytes: int = 256 * 1024
    hmac_max_skew_s: int = 300
    rate_per_minute: int = 120
    extra: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        data_dir = Path(env.get("DATA_DIR", "./data"))
        secret = env.get("AGENTS_INTERNAL_SECRET", "").strip() or env.get("EA_INTERNAL_SECRET", "").strip()
        default_max = _int(env, "DEFAULT_MAX_MINUTES", 60, 1, 24 * 60)
        max_max = _int(env, "MAX_MAX_MINUTES", 120, 1, 24 * 60)
        if default_max > max_max:
            raise ConfigError("DEFAULT_MAX_MINUTES must not exceed MAX_MAX_MINUTES")
        mode = env.get("LICENSE_MODE", "site").strip().lower()
        if mode not in ("site", "stub"):
            raise ConfigError("LICENSE_MODE must be site or stub")
        return cls(
            internal_secret=secret,
            public_base_url=env.get("PUBLIC_BASE_URL", "http://localhost:8090").rstrip("/"),
            data_dir=data_dir,
            db_path=Path(env.get("DB_PATH", str(data_dir / "agents.db"))),
            results_dir=Path(env.get("RESULTS_DIR", str(data_dir / "results"))),
            events_dir=Path(env.get("EVENTS_DIR", str(data_dir / "events"))),
            anthropic_base_url=env.get("TKBK_ANTHROPIC_BASE_URL", "https://api.tkbk.online").rstrip("/"),
            openai_base_url=env.get("TKBK_OPENAI_BASE_URL", "https://api.tkbk.online/openai/v1").rstrip("/"),
            models_url=env.get("TKBK_MODELS_URL", "https://api.tkbk.online/v1/models"),
            default_model=env.get("DEFAULT_MODEL", "claude-sonnet-5"),
            key_check=_bool(env, "TKBK_KEY_CHECK", True),
            license_mode=mode,
            license_check_url=env.get("LICENSE_CHECK_URL", "https://tkbk.online/api/license/check"),
            license_public_key=env.get("LICENSE_PUBLIC_KEY", "").strip(),
            license_cache_seconds=_float(env, "LICENSE_CACHE_SECONDS", 600.0),
            license_app=env.get("LICENSE_APP", "tkbk-cloud-agents"),
            stub_plan=env.get("STUB_PLAN", "pro"),
            concurrent_per_plan=_plan_map(env, "CONCURRENT_PER_PLAN", {"pro": 3, "team": 5, "team_plus": 10}),
            concurrent_default=_int(env, "CONCURRENT_DEFAULT", 3, 1, 64),
            sandbox_image=env.get("SANDBOX_IMAGE", "ghcr.io/openhands/agent-server:1.55.0-python"),
            sandbox_memory=env.get("SANDBOX_MEMORY", "4g"),
            sandbox_cpus=env.get("SANDBOX_CPUS", "2"),
            sandbox_pids_limit=_int(env, "SANDBOX_PIDS_LIMIT", 512, 16, 65536),
            sandbox_uid=_int(env, "SANDBOX_UID", 10001, 0, 2**31),
            egress_proxy=env.get("SANDBOX_EGRESS_PROXY", "http://tkbk-agents-egress:3128"),
            no_proxy=env.get("SANDBOX_NO_PROXY", "localhost,127.0.0.1"),
            tasks_root=Path(env.get("TASKS_ROOT", "/var/lib/tkbk-agents/tasks")),
            tasks_volume=env.get("TASKS_VOLUME", "").strip(),
            attach_containers=_list(env, "SANDBOX_ATTACH_CONTAINERS",
                                    [env.get("EGRESS_CONTAINER", "tkbk-agents-egress"),
                                     env.get("SELF_CONTAINER", "tkbk-agents-api")]),
            max_concurrent=_int(env, "MAX_CONCURRENT_TASKS", 3, 1, 64),
            default_max_minutes=default_max,
            max_max_minutes=max_max,
            default_max_iterations=_int(env, "DEFAULT_MAX_ITERATIONS", 100, 1, 5000),
            monthly_minutes_cap=_int(env, "MONTHLY_MINUTES_CAP_PER_KEY", 0, 0, 10**7),
            upload_max_bytes=_int(env, "UPLOAD_MAX_MB", 200, 1, 4096) * 1024 * 1024,
            results_ttl_days=_int(env, "RESULTS_TTL_DAYS", 7, 0, 3650),
            git_name=env.get("GIT_AUTHOR_NAME", "TKBK Cloud Agent"),
            git_email=env.get("GIT_AUTHOR_EMAIL", "agents@tkbk.online"),
            poll_interval=_float(env, "POLL_INTERVAL", 5.0),
            sandbox_startup_timeout=_float(env, "SANDBOX_STARTUP_TIMEOUT", 180.0),
        )

    @property
    def secret_ok(self) -> bool:
        return len(self.internal_secret) >= MIN_SECRET_LEN

    @property
    def self_container(self) -> str:
        return self.attach_containers[-1] if len(self.attach_containers) > 1 else "tkbk-agents-api"

    @property
    def egress_container(self) -> str:
        return self.attach_containers[0] if self.attach_containers else "tkbk-agents-egress"
