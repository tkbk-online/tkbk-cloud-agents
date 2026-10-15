"""API models (pydantic v2)."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, field_validator

KEY_RE = re.compile(r"^cr_[A-Za-z0-9_\-]{16,128}$")
GITHUB_REPO_RE = re.compile(r"^https://github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?/?$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,127}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\-]{0,99}$")
REF_RE = re.compile(r"^[A-Za-z0-9:._@\-]{1,64}$")


class Template(StrEnum):
    FIX_ISSUE = "fix_issue"
    ADD_TESTS = "add_tests"
    UPDATE_DEPS = "update_deps"
    PR_FROM_SPEC = "pr_from_spec"
    CUSTOM = "custom"


class Provider(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


class Deliver(StrEnum):
    PR = "pr"
    ARCHIVE = "archive"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    RUNNING = "running"
    DELIVERING = "delivering"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"

    @property
    def terminal(self) -> bool:
        return self in (TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.TIMEOUT)


ACTIVE_STATUSES = (TaskStatus.PREPARING, TaskStatus.RUNNING, TaskStatus.DELIVERING)


class CreateTaskRequest(BaseModel):
    api_key: str = Field(description="Client TKBK key cr_... (held in memory for the task only)")
    repo: str = Field(description="https://github.com/<owner>/<repo>")
    branch: str | None = Field(default=None, description="Base branch (default: repository default)")
    template: Template = Template.CUSTOM
    instruction: str = Field(default="", max_length=20000)
    issue: int | None = Field(default=None, ge=1, le=10**9, description="Issue number for fix_issue")
    model: str | None = Field(default=None, description="TKBK model id, e.g. claude-sonnet-5 or gpt-5.5")
    provider: Provider | None = Field(default=None, description="API format; inferred from the model if omitted")
    git_token: str | None = Field(default=None, max_length=512, description="GitHub token: private clone, push, PR")
    deliver: Deliver = Deliver.PR
    max_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    max_iterations: int | None = Field(default=None, ge=1, le=5000)
    callback_url: HttpUrl | None = Field(default=None, description="POST (HMAC-signed) when the task ends")
    client_ref: str | None = Field(default=None, description="Caller reference: Telegram user id, IDE session...")
    keep_workspace: bool = Field(default=False, description="Keep the workspace dir after the task (debugging)")

    @field_validator("api_key")
    @classmethod
    def _key(cls, v: str) -> str:
        v = v.strip()
        if not KEY_RE.match(v):
            raise ValueError("api_key must look like cr_...")
        return v

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str) -> str:
        v = v.strip()
        if not GITHUB_REPO_RE.match(v):
            raise ValueError("repo must be https://github.com/<owner>/<repo>")
        return v.removesuffix("/").removesuffix(".git")

    @field_validator("branch")
    @classmethod
    def _branch(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not BRANCH_RE.match(v) or ".." in v or v.endswith("/"):
            raise ValueError("bad branch name")
        return v

    @field_validator("model")
    @classmethod
    def _model(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not MODEL_RE.match(v) or "/" in v:
            raise ValueError("bad model id (no provider prefix, e.g. claude-sonnet-5)")
        return v

    @field_validator("client_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not REF_RE.match(v):
            raise ValueError("bad client_ref")
        return v

    @field_validator("instruction")
    @classmethod
    def _instruction(cls, v: str) -> str:
        return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", v).strip()


class TaskResult(BaseModel):
    pr_url: str | None = None
    branch: str | None = None
    commit: str | None = None
    archive_url: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    summary: str = ""


class TaskMetrics(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd_reported: float = 0.0
    iterations: int = 0


class TaskView(BaseModel):
    id: str
    status: TaskStatus
    phase: str = ""
    template: Template
    repo: str
    base_branch: str | None
    work_branch: str | None
    model: str
    provider: Provider
    deliver: Deliver
    key_id: str
    key_prefix: str
    client_ref: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    max_minutes: int
    max_iterations: int
    minutes_billed: int = 0
    keep_workspace: bool = False
    metrics: TaskMetrics = Field(default_factory=TaskMetrics)
    error: str | None = None
    result: TaskResult | None = None


class TaskCreated(BaseModel):
    ok: bool = True
    task: TaskView


class UsageRow(BaseModel):
    key_id: str
    key_prefix: str
    tasks: int
    minutes: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd_reported: float


class UsageReport(BaseModel):
    ok: bool = True
    month: str
    rows: list[UsageRow]


class TemplateInfo(BaseModel):
    id: Template
    title: str
    description: str
    needs: list[str]


def as_json_dict(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
