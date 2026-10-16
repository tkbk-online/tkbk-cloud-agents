"""Git and GitHub operations done by the orchestrator (never by the agent).

The agent only sees a clone on a work branch. Tokens are passed to git through the
environment (GIT_CONFIG_* / credential helper), never on the command line.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import tarfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from .models import GITHUB_REPO_RE
from .templates import IssueInfo

GITHUB_API = "https://api.github.com"
EXCLUDE_FROM_ARCHIVE = {"node_modules", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".tox"}


class GitError(RuntimeError):
    pass


def parse_repo(url: str) -> tuple[str, str]:
    m = GITHUB_REPO_RE.match(url)
    if not m:
        raise GitError(f"not a GitHub repository URL: {url}")
    return m.group(1), m.group(2)


def _token_env(token: str | None) -> dict[str, str]:
    """git config via env: http.<github>.extraheader = AUTHORIZATION: basic base64(x-access-token:token)."""
    if not token:
        return {}
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {auth}",
    }


async def git(*args: str, cwd: Path | str | None = None, env: dict[str, str] | None = None,
              timeout: float = 300.0) -> str:
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C", **(env or {})}
    # safe.directory=*: task workspaces are chowned to the sandbox user while the orchestrator runs as root.
    proc = await asyncio.create_subprocess_exec(
        "git", "-c", "safe.directory=*", *args, cwd=str(cwd) if cwd else None, env=full_env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise GitError(f"git {args[0]}: timeout after {timeout}s")
    if proc.returncode != 0:
        text = f"git {' '.join(args[:2])} failed ({proc.returncode}): {err.decode(errors='replace').strip()[:600]}"
        raise GitError(re.sub(r"https://[^@\s/]+@", "https://***@", text))  # never echo embedded credentials
    return out.decode(errors="replace")


async def clone(repo_url: str, dest: Path, branch: str | None, token: str | None) -> str:
    """Shallow clone; returns HEAD sha."""
    args = ["clone", "--depth", "50", "--no-tags"]
    if branch:
        args += ["--branch", branch]
    args += [repo_url + ".git" if not repo_url.endswith(".git") else repo_url, str(dest)]
    await git(*args, env=_token_env(token), timeout=600)
    return (await git("rev-parse", "HEAD", cwd=dest)).strip()


async def current_branch(dest: Path) -> str:
    return (await git("rev-parse", "--abbrev-ref", "HEAD", cwd=dest)).strip()


async def baseline(dest: Path, name: str, email: str) -> str:
    """An uploaded working copy: make it a git repo (if needed) and commit its exact state as the base.
    The patch of the task is computed against this commit. Returns the base sha."""
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    if not (dest / ".git").exists():
        await git("init", "-q", "-b", "main", str(dest))
    await git("add", "-A", cwd=dest)
    await git("commit", "-q", "--allow-empty", "-m", "TKBK cloud agent: baseline of the uploaded workspace",
              cwd=dest, env=env)
    return (await git("rev-parse", "HEAD", cwd=dest)).strip()


async def patch(dest: Path, base_sha: str) -> str:
    """Unified diff base..HEAD, binary-safe (`git apply` friendly)."""
    return await git("diff", "--binary", "--no-color", f"{base_sha}..HEAD", cwd=dest, timeout=120)


async def create_branch(dest: Path, name: str) -> None:
    await git("checkout", "-b", name, cwd=dest)


async def commit_all(dest: Path, message: str, name: str, email: str) -> str | None:
    """Stage and commit everything the agent left uncommitted. Returns the new sha or None."""
    await git("add", "-A", cwd=dest)
    status = await git("status", "--porcelain", cwd=dest)
    if not status.strip():
        return None
    env = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    await git("commit", "-q", "-m", message, cwd=dest, env=env)
    return (await git("rev-parse", "HEAD", cwd=dest)).strip()


async def head(dest: Path) -> str:
    return (await git("rev-parse", "HEAD", cwd=dest)).strip()


async def changed_files(dest: Path, base_sha: str) -> list[str]:
    out = await git("diff", "--name-only", f"{base_sha}..HEAD", cwd=dest)
    return [line.strip() for line in out.splitlines() if line.strip()]


async def push(dest: Path, branch: str, token: str | None) -> None:
    await git("push", "-u", "origin", f"HEAD:refs/heads/{branch}", cwd=dest, env=_token_env(token), timeout=600)


def make_archive(repo_dir: Path, out_path: Path) -> int:
    """tar.gz of the working tree (with .git, without build/cache dirs). Returns size in bytes."""
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        parts = Path(info.name).parts
        if any(p in EXCLUDE_FROM_ARCHIVE for p in parts[1:]):
            return None
        return info

    with tarfile.open(out_path, "w:gz") as tar:
        tar.add(str(repo_dir), arcname="repo", filter=_filter)
    return out_path.stat().st_size


# ---------------------------------------------------------------------- GitHub API
@dataclass
class PullRequest:
    url: str
    number: int


class GitHub:
    def __init__(self, http: httpx.AsyncClient | None = None, api: str = GITHUB_API):
        self.http = http or httpx.AsyncClient(timeout=30.0)
        self.api = api.rstrip("/")

    def _headers(self, token: str | None) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
             "User-Agent": "tkbk-cloud-agents/0.1"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def get_issue(self, owner: str, repo: str, number: int, token: str | None) -> IssueInfo:
        r = await self.http.get(f"{self.api}/repos/{owner}/{repo}/issues/{number}", headers=self._headers(token))
        if r.status_code != 200:
            raise GitError(f"GitHub issue #{number}: HTTP {r.status_code}")
        d = r.json()
        return IssueInfo(number=number, title=str(d.get("title") or ""), body=str(d.get("body") or ""),
                         url=str(d.get("html_url") or ""))

    async def default_branch(self, owner: str, repo: str, token: str | None) -> str:
        r = await self.http.get(f"{self.api}/repos/{owner}/{repo}", headers=self._headers(token))
        if r.status_code != 200:
            raise GitError(f"GitHub repo {owner}/{repo}: HTTP {r.status_code}")
        return str(r.json().get("default_branch") or "main")

    async def create_pr(self, owner: str, repo: str, *, head: str, base: str, title: str, body: str,
                        token: str, draft: bool = False) -> PullRequest:
        payload = {"title": title[:250], "head": head, "base": base, "body": body[:60000], "draft": draft}
        r = await self.http.post(f"{self.api}/repos/{owner}/{repo}/pulls", headers=self._headers(token), json=payload)
        if r.status_code not in (200, 201):
            raise GitError(f"GitHub create PR: HTTP {r.status_code}: {r.text[:300]}")
        d = r.json()
        return PullRequest(url=str(d["html_url"]), number=int(d["number"]))
