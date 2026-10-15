from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import httpx
import pytest

from tkbk_agents import gitops


def _run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repo with one commit on main, playing the role of GitHub."""
    bare = tmp_path / "origin.git"
    _run("init", "-q", "--bare", "-b", "main", str(bare))
    src = tmp_path / "src"
    _run("init", "-q", "-b", "main", str(src))
    (src / "README.md").write_text("hello\n")
    (src / "node_modules").mkdir()
    (src / "node_modules" / "junk.js").write_text("x")
    _run("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=src)
    _run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init", cwd=src)
    _run("remote", "add", "origin", str(bare), cwd=src)
    _run("push", "-q", "origin", "main", cwd=src)
    return bare


def test_parse_repo() -> None:
    assert gitops.parse_repo("https://github.com/tkbk-online/tkbk-api") == ("tkbk-online", "tkbk-api")
    assert gitops.parse_repo("https://github.com/a/b.git") == ("a", "b")
    with pytest.raises(gitops.GitError):
        gitops.parse_repo("https://example.com/a/b")


def test_token_env_never_puts_token_in_argv() -> None:
    env = gitops._token_env("ghp_secret")
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert "ghp_secret" not in env["GIT_CONFIG_VALUE_0"]  # base64-encoded basic auth
    assert gitops._token_env(None) == {}


@pytest.mark.asyncio
async def test_clone_branch_commit_push_archive(origin: Path, tmp_path: Path) -> None:
    dest = tmp_path / "ws" / "repo"
    base = await gitops.clone(str(origin), dest, "main", None)
    assert len(base) == 40 and (dest / "README.md").exists()
    await gitops.create_branch(dest, "tkbk/custom-1")
    assert await gitops.current_branch(dest) == "tkbk/custom-1"
    assert await gitops.commit_all(dest, "nothing", "A", "a@b") is None
    (dest / "new.py").write_text("print(1)\n")
    sha = await gitops.commit_all(dest, "TKBK agent: add new.py", "TKBK Cloud Agent", "agents@tkbk.online")
    assert sha and sha != base and await gitops.head(dest) == sha
    assert await gitops.changed_files(dest, base) == ["new.py"]
    log = _run("log", "-1", "--format=%an <%ae>", cwd=dest).strip()
    assert log == "TKBK Cloud Agent <agents@tkbk.online>"
    await gitops.push(dest, "tkbk/custom-1", None)
    assert "tkbk/custom-1" in _run("branch", "--list", "tkbk/custom-1", cwd=origin)

    out = tmp_path / "res" / "t.tgz"
    size = gitops.make_archive(dest, out)
    assert size > 0
    with tarfile.open(out) as tar:
        names = tar.getnames()
    assert "repo/new.py" in names and "repo/README.md" in names
    assert not any("node_modules" in n for n in names)


@pytest.mark.asyncio
async def test_git_error_masks_credentials(tmp_path: Path) -> None:
    with pytest.raises(gitops.GitError) as e:
        await gitops.git("clone", "https://user:tok3n@127.0.0.1:1/none.git", str(tmp_path / "x"), timeout=30)
    assert "tok3n" not in str(e.value)


@pytest.mark.asyncio
async def test_github_api_calls() -> None:
    calls: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if req.url.path.endswith("/issues/3"):
            return httpx.Response(200, json={"title": "T", "body": "B", "html_url": "https://github.com/a/b/issues/3"})
        if req.url.path == "/repos/a/b":
            return httpx.Response(200, json={"default_branch": "develop"})
        if req.url.path == "/repos/a/b/pulls":
            return httpx.Response(201, json={"html_url": "https://github.com/a/b/pull/9", "number": 9})
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http:
        gh = gitops.GitHub(http=http)
        issue = await gh.get_issue("a", "b", 3, "tok")
        assert issue.title == "T" and issue.body == "B"
        assert await gh.default_branch("a", "b", None) == "develop"
        pr = await gh.create_pr("a", "b", head="x", base="develop", title="t", body="b", token="tok", draft=True)
        assert pr.url.endswith("/pull/9") and pr.number == 9
    assert calls[0].headers["Authorization"] == "Bearer tok" and "Authorization" not in calls[1].headers
    with pytest.raises(gitops.GitError):
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http:
            await gitops.GitHub(http=http).get_issue("a", "b", 4, None)
