from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import KEY
from tkbk_agents.models import CreateTaskRequest, Template
from tkbk_agents.templates import TEMPLATES, IssueInfo, build_prompt, validate_inputs


def test_request_normalises_repo_and_key() -> None:
    r = CreateTaskRequest(api_key=f" {KEY} ", repo="https://github.com/tkbk-online/tkbk-api.git/", instruction="x")
    assert r.api_key == KEY and r.repo == "https://github.com/tkbk-online/tkbk-api"


@pytest.mark.parametrize("bad", ["sk-abc", "cr_short", "cr_" + "x" * 200, ""])
def test_request_rejects_bad_keys(bad: str) -> None:
    with pytest.raises(ValidationError):
        CreateTaskRequest(api_key=bad, repo="https://github.com/a/b")


@pytest.mark.parametrize("bad", ["https://gitlab.com/a/b", "git@github.com:a/b.git", "https://github.com/a",
                                 "https://github.com/a/b/tree/main", "http://github.com/a/b"])
def test_request_rejects_non_github_repos(bad: str) -> None:
    with pytest.raises(ValidationError):
        CreateTaskRequest(api_key=KEY, repo=bad)


@pytest.mark.parametrize("bad", ["anthropic/claude-sonnet-5", "../x", "a b"])
def test_request_rejects_bad_models(bad: str) -> None:
    with pytest.raises(ValidationError):
        CreateTaskRequest(api_key=KEY, repo="https://github.com/a/b", model=bad)


def test_request_strips_control_chars() -> None:
    r = CreateTaskRequest(api_key=KEY, repo="https://github.com/a/b", instruction="do\x00 this\x1b[31m")
    assert r.instruction == "do this[31m"


def test_validate_inputs() -> None:
    assert validate_inputs(Template.FIX_ISSUE, "", None) == "template fix_issue requires `issue`"
    assert validate_inputs(Template.FIX_ISSUE, "", 12) is None
    assert validate_inputs(Template.UPDATE_DEPS, "", None) is None
    assert "requires `instruction`" in (validate_inputs(Template.PR_FROM_SPEC, " ", None) or "")
    assert validate_inputs(Template.CUSTOM, "fix it", None) is None


def test_all_templates_listed() -> None:
    assert set(TEMPLATES) == set(Template)


def test_prompt_contains_task_guardrails_and_instruction() -> None:
    issue = IssueInfo(number=5, title="Crash on empty input", body="Steps: run with ''", url="https://github.com/a/b/issues/5")
    p = build_prompt(Template.FIX_ISSUE, repo="https://github.com/a/b", work_branch="tkbk/fix-issue-1",
                     workdir="/workspace/repo", instruction="prefer a small patch", issue=issue)
    assert "issue #5" in p and "Crash on empty input" in p and "prefer a small patch" in p
    assert "Do NOT push" in p and "/workspace/repo" in p and "tkbk/fix-issue-1" in p
    for t in (Template.ADD_TESTS, Template.UPDATE_DEPS, Template.PR_FROM_SPEC, Template.CUSTOM):
        p = build_prompt(t, repo="r", work_branch="b", workdir="/w", instruction="SPEC-TEXT")
        assert "SPEC-TEXT" in p and "Do NOT push" in p
