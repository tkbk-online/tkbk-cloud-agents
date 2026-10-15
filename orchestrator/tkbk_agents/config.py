"""Settings read from the environment (see .env.example at the repo root)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class Settings:
    internal_secret: str
    public_base_url: str
    data_dir: Path
    db_path: Path
    results_dir: Path
    anthropic_base_url: str
    openai_base_url: str
    models_url: str
    default_model: str
    key_check: bool
    sandbox_image: str
    sandbox_memory: str
    sandbox_cpus: str
    sandbox_pids_limit: int
    sandbox_uid: int
    egress_proxy: str
    tasks_root: Path
    self_container: str
    egress_container: str
    max_concurrent: int
    default_max_minutes: int
    max_max_minutes: int
    default_max_iterations: int
    monthly_minutes_cap: int
    git_name: str
    git_email: str
    poll_interval: float
    sandbox_startup_timeout: float
    max_body_bytes: int = 256 * 1024
    hmac_max_skew_s: int = 300
    rate_per_minute: int = 120

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        data_dir = Path(env.get("DATA_DIR", "./data"))
        secret = env.get("AGENTS_INTERNAL_SECRET", "").strip()
        # Allow sharing the bot's secret when the owner prefers one value (see README).
        if not secret:
            secret = env.get("EA_INTERNAL_SECRET", "").strip()
        default_max = _int(env, "DEFAULT_MAX_MINUTES", 30, 1, 24 * 60)
        max_max = _int(env, "MAX_MAX_MINUTES", 120, 1, 24 * 60)
        if default_max > max_max:
            raise ConfigError("DEFAULT_MAX_MINUTES must not exceed MAX_MAX_MINUTES")
        return cls(
            internal_secret=secret,
            public_base_url=env.get("PUBLIC_BASE_URL", "http://localhost:8090").rstrip("/"),
            data_dir=data_dir,
            db_path=Path(env.get("DB_PATH", str(data_dir / "agents.db"))),
            results_dir=Path(env.get("RESULTS_DIR", str(data_dir / "results"))),
            anthropic_base_url=env.get("TKBK_ANTHROPIC_BASE_URL", "https://api.tkbk.online").rstrip("/"),
            openai_base_url=env.get("TKBK_OPENAI_BASE_URL", "https://api.tkbk.online/openai/v1").rstrip("/"),
            models_url=env.get("TKBK_MODELS_URL", "https://api.tkbk.online/v1/models"),
            default_model=env.get("DEFAULT_MODEL", "claude-sonnet-5"),
            key_check=_bool(env, "TKBK_KEY_CHECK", True),
            sandbox_image=env.get("SANDBOX_IMAGE", "ghcr.io/openhands/agent-server:1.55.0-python"),
            sandbox_memory=env.get("SANDBOX_MEMORY", "4g"),
            sandbox_cpus=env.get("SANDBOX_CPUS", "2"),
            sandbox_pids_limit=_int(env, "SANDBOX_PIDS_LIMIT", 512, 16, 65536),
            sandbox_uid=_int(env, "SANDBOX_UID", 10001, 0, 2**31),
            egress_proxy=env.get("SANDBOX_EGRESS_PROXY", "http://tkbk-agents-egress:3128"),
            tasks_root=Path(env.get("TASKS_ROOT", "/var/lib/tkbk-agents/tasks")),
            self_container=env.get("SELF_CONTAINER", "tkbk-agents-api"),
            egress_container=env.get("EGRESS_CONTAINER", "tkbk-agents-egress"),
            max_concurrent=_int(env, "MAX_CONCURRENT_TASKS", 3, 1, 64),
            default_max_minutes=default_max,
            max_max_minutes=max_max,
            default_max_iterations=_int(env, "DEFAULT_MAX_ITERATIONS", 100, 1, 5000),
            monthly_minutes_cap=_int(env, "MONTHLY_MINUTES_CAP_PER_KEY", 0, 0, 10**7),
            git_name=env.get("GIT_AUTHOR_NAME", "TKBK Cloud Agent"),
            git_email=env.get("GIT_AUTHOR_EMAIL", "agents@tkbk.online"),
            poll_interval=_float(env, "POLL_INTERVAL", 5.0),
            sandbox_startup_timeout=_float(env, "SANDBOX_STARTUP_TIMEOUT", 180.0),
        )

    @property
    def secret_ok(self) -> bool:
        return len(self.internal_secret) >= MIN_SECRET_LEN
