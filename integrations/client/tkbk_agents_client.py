#!/usr/bin/env python3
"""Tiny signed client for the TKBK Cloud Agents API (stdlib only) + CLI.

    export AGENTS_URL=https://agents.tkbk.online AGENTS_INTERNAL_SECRET=...
    python tkbk_agents_client.py templates
    python tkbk_agents_client.py create --key cr_... --repo https://github.com/o/r \
        --template add_tests --instruction "cover src/keys.ts" --git-token ghp_...
    python tkbk_agents_client.py status t_...
    python tkbk_agents_client.py result t_...
    python tkbk_agents_client.py usage --month 2026-10

Signature (same as the bot's internal API): X-TKBK-Ts + X-TKBK-Sig =
hex HMAC-SHA256(secret, "{ts}\\n{METHOD}\\n{path_with_query}\\n{raw_body}").
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any


class AgentsClient:
    def __init__(self, base_url: str, secret: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        self.timeout = timeout

    def sign(self, method: str, path_qs: str, body: bytes) -> dict[str, str]:
        ts = str(int(time.time()))
        msg = f"{ts}\n{method.upper()}\n{path_qs}\n".encode() + body
        sig = hmac.new(self.secret.encode(), msg, hashlib.sha256).hexdigest()
        return {"X-TKBK-Ts": ts, "X-TKBK-Sig": sig}

    def call(self, method: str, path_qs: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        raw = json.dumps(body).encode() if body is not None else b""
        headers = {"Accept": "application/json", **self.sign(method, path_qs, raw)}
        if body is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path_qs, data=raw if body is not None else None,
                                     method=method.upper(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read() or b"{}") | {"http_status": e.code}
            except json.JSONDecodeError:
                return {"ok": False, "error": f"HTTP {e.code}", "http_status": e.code}
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return {"ok": False, "error": f"connection failed: {getattr(e, 'reason', e)}"}

    # convenience
    def create_task(self, **task: Any) -> dict[str, Any]:
        return self.call("POST", "/v1/tasks", task)

    def task(self, task_id: str) -> dict[str, Any]:
        return self.call("GET", f"/v1/tasks/{task_id}")

    def result(self, task_id: str) -> dict[str, Any]:
        return self.call("GET", f"/v1/tasks/{task_id}/result")

    def cancel(self, task_id: str) -> dict[str, Any]:
        return self.call("POST", f"/v1/tasks/{task_id}/cancel")

    def usage(self, month: str | None = None, key_id: str | None = None) -> dict[str, Any]:
        qs = "&".join(f"{k}={v}" for k, v in (("month", month), ("key_id", key_id)) if v)
        return self.call("GET", "/v1/usage" + (f"?{qs}" if qs else ""))

    def wait(self, task_id: str, poll: float = 10.0, timeout: float = 3 * 3600) -> dict[str, Any]:
        t0 = time.time()
        while time.time() - t0 < timeout:
            t = self.task(task_id).get("task") or {}
            if t.get("status") in ("done", "failed", "cancelled", "timeout"):
                return t
            time.sleep(poll)
        raise TimeoutError(task_id)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TKBK Cloud Agents client")
    ap.add_argument("--url", default=os.environ.get("AGENTS_URL", "http://localhost:8090"))
    ap.add_argument("--secret", default=os.environ.get("AGENTS_INTERNAL_SECRET", ""))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("templates")
    c = sub.add_parser("create")
    c.add_argument("--key", required=True)
    c.add_argument("--repo", required=True)
    c.add_argument("--template", default="custom")
    c.add_argument("--instruction", default="")
    c.add_argument("--issue", type=int)
    c.add_argument("--branch")
    c.add_argument("--model")
    c.add_argument("--git-token", default=os.environ.get("GITHUB_TOKEN"))
    c.add_argument("--deliver", default="pr", choices=["pr", "archive"])
    c.add_argument("--max-minutes", type=int)
    c.add_argument("--wait", action="store_true")
    for name in ("status", "result", "cancel"):
        sub.add_parser(name).add_argument("task_id")
    u = sub.add_parser("usage")
    u.add_argument("--month")
    u.add_argument("--key-id")
    a = ap.parse_args(argv)
    if not a.secret:
        print("AGENTS_INTERNAL_SECRET is required", file=sys.stderr)
        return 2
    cl = AgentsClient(a.url, a.secret)
    if a.cmd == "templates":
        out = cl.call("GET", "/v1/templates")
    elif a.cmd == "create":
        body = {k: v for k, v in {
            "api_key": a.key, "repo": a.repo, "template": a.template, "instruction": a.instruction,
            "issue": a.issue, "branch": a.branch, "model": a.model, "git_token": a.git_token,
            "deliver": a.deliver, "max_minutes": a.max_minutes, "client_ref": "cli",
        }.items() if v not in (None, "")}
        out = cl.create_task(**body)
        if a.wait and out.get("ok"):
            out = {"ok": True, "task": cl.wait(out["task"]["id"])}
    elif a.cmd == "status":
        out = cl.task(a.task_id)
    elif a.cmd == "result":
        out = cl.result(a.task_id)
    elif a.cmd == "cancel":
        out = cl.cancel(a.task_id)
    else:
        out = cl.usage(a.month, a.key_id)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
