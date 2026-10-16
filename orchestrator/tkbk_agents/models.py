"""API models (pydantic v2)."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

KEY_RE = re.compile(r"^cr_[A-Za-z0-9_\-]{9,197}$")
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
    PATCH = "patch"      # patch + summary + log (default for the IDE)
    ARCHIVE = "archive"  # patch + tar.gz of the working tree
    PR = "pr"            # push + pull request (needs git_token; bot/site flows)


class Source(StrEnum):
    REPO = "repo"        # git clone of `repo`
    UPLOAD = "upload"    # tar.gz of the working copy uploaded with PUT /v1/tasks/{id}/workspace


class TaskStatus(StrEnum):
    AWAITING_UPLOAD = "awaiting_upload"
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
RESERVING_STATUSES = (TaskStatus.AWAITING_UPLOAD, TaskStatus.QUEUED, *ACTIVE_STATUSES)


class CreateTaskRequest(BaseModel):
    """Body of POST /v1/tasks. With `Authorization: Bearer cr_…` the key comes from the header
    (`api_key` may be omitted); with HMAC the key must be in the body."""

    api_key: str | None = Field(default=None, description="Client TKBK key cr_… (held in memory for the task only)")
    source: Source = Source.REPO
    repo: str | None = Field(default=None, description="https://github.com/<owner>/<repo> (source=repo)")
    branch: str | None = Field(default=None, description="Base branch (default: repository default)")
    template: Template = Template.CUSTOM
    instruction: str = Field(default="", max_length=20000, description="The task for the agent (prompt)")
    issue: int | None = Field(default=None, ge=1, le=10**9, description="Issue number for fix_issue")
    model: str | None = Field(default=None, description="TKBK model id, e.g. claude-sonnet-5 or gpt-5.5")
    provider: Provider | None = Field(default=None, description="API format; inferred from the model if omitted")
    git_token: str | None = Field(default=None, max_length=512, description="GitHub token: private clone, push, PR")
    deliver: Deliver = Deliver.PATCH
    push: bool = Field(default=False, description="Push the result to branch tkbk/<taskId> (needs git_token)")
    max_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    max_iterations: int | None = Field(default=None, ge=1, le=5000)
    callback_url: HttpUrl | None = Field(default=None, description="POST (HMAC-signed) when the task ends")
    client_ref: str | None = Field(default=None, description="Caller reference: Telegram user id, IDE session…")
    keep_workspace: bool = Field(default=False, description="Keep the workspace dir after the task (debugging)")

    # aliases the IDE brief uses
    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            for src, dst in (("repoUrl", "repo"), ("prompt", "instruction"), ("apiKey", "api_key"),
                             ("gitToken", "git_token"), ("maxMinutes", "max_minutes"), ("clientRef", "client_ref")):
                if src in data and dst not in data:
                    data[dst] = data.pop(src)
        return data

    @field_validator("api_key")
    @classmethod
    def _key(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not KEY_RE.match(v):
            raise ValueError("api_key must look like cr_…")
        return v

    @field_validator("repo")
    @classmethod
    def _repo(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
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

    @model_validator(mode="after")
    def _source_consistency(self) -> CreateTaskRequest:
        if self.source is Source.REPO and not self.repo:
            raise ValueError("repo is required when source=repo")
        if self.source is Source.UPLOAD and self.template is Template.FIX_ISSUE:
            raise ValueError("fix_issue needs source=repo (the issue lives on GitHub)")
        return self


class TaskResult(BaseModel):
    patch_url: str | None = None
    archive_url: str | None = None
    log_url: str | None = None
    pr_url: str | None = None
    branch: str | None = None
    pushed: bool = False
    commit: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    summary: str = ""
    commands: int = 0


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
    source: Source = Source.REPO
    template: Template
    repo: str | None
    base_branch: str | None
    work_branch: str | None
    model: str
    provider: Provider
    deliver: Deliver
    push: bool = False
    key_id: str
    key_prefix: str
    plan: str = ""
    client_ref: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    max_minutes: int
    max_iterations: int
    minutes_billed: int = 0
    keep_workspace: bool = False
    concurrency_limit: int = 1
    metrics: TaskMetrics = Field(default_factory=TaskMetrics)
    error: str | None = None
    result: TaskResult | None = None
    events: int = 0


class TaskCreated(BaseModel):
    ok: bool = True
    task: TaskView
    upload_url: str | None = None


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


class Me(BaseModel):
    ok: bool = True
    key_id: str
    key_prefix: str
    plan: str
    cloud_agents: bool
    hours_month: int
    minutes_used: int
    minutes_left: int
    concurrent_limit: int
    active_tasks: int
    until: str | None = None
    error: str | None = None


def as_json_dict(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
