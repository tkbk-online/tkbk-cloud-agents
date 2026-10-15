#!/usr/bin/env python3
"""Local smoke test of the sandbox layer — no LLM key needed.

Starts (with plain `docker` commands):
  1. an `--internal` network `tkbk-smoke`
  2. the squid egress proxy from deploy/egress attached to it (+ a normal bridge for Internet)
  3. the OpenHands agent-server sandbox image with the exact hardening flags the orchestrator uses
  4. a curl helper on the same network
and checks:
  - the sandbox answers GET /health
  - the sandbox has NO direct route to the Internet
  - through the proxy it reaches api.tkbk.online (allow-listed) and is denied for example.com
  - the sandbox user is uid 10001 with no capabilities

Run from the repo root:  python scripts/smoke_sandbox.py   (needs Docker; pulls ~2 GB once)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "orchestrator"))

from tkbk_agents.sandbox import SandboxSpec, run_args  # noqa: E402

IMAGE = os.environ.get("SANDBOX_IMAGE", "ghcr.io/openhands/agent-server:1.55.0-python")
SQUID = os.environ.get("EGRESS_IMAGE", "ubuntu/squid:6.6-24.04_edge")
CURL = "curlimages/curl:8.14.1"
NET = "tkbk-smoke"
EGRESS = "tkbk-smoke-egress"
TASK = "smoke0000"


def sh(*args: str, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise SystemExit(f"docker {' '.join(args[:3])} failed: {p.stderr.strip()[:800]}")
    return p


def host_path(p: Path) -> str:
    """Docker Desktop on Windows wants a Windows path; Git Bash mangles POSIX ones."""
    return str(p)


def cleanup() -> None:
    for c in (f"tkbk-sbx-{TASK}", EGRESS, "tkbk-smoke-curl"):
        sh("rm", "-f", c, check=False)
    sh("network", "rm", NET, check=False)
    sh("network", "rm", f"{NET}-egress", check=False)


def curl(*args: str) -> tuple[int, str]:
    """(exit code, http status). For a denied HTTPS CONNECT curl exits 56 and its stderr says
    'Received HTTP code 403 from proxy after CONNECT'; we report that as status 403."""
    p = sh("run", "--rm", "--name", "tkbk-smoke-curl", "--network", NET, CURL, "-sS", "-o", "/dev/null",
           "-w", "%{http_code}", "--max-time", "20", *args, check=False, timeout=60)
    status = p.stdout.strip()
    if p.returncode != 0 and "403" in p.stderr:  # "CONNECT tunnel failed, response 403"
        status = "403"
    return p.returncode, status


def main() -> int:
    cleanup()
    results: dict[str, bool] = {}
    try:
        sh("network", "create", "--internal", NET)
        sh("network", "create", f"{NET}-egress")
        egress_dir = ROOT / "deploy" / "egress"
        sh("run", "-d", "--name", EGRESS, "--network", f"{NET}-egress",
           "-v", f"{host_path(egress_dir / 'squid.conf')}:/etc/squid/squid.conf:ro",
           "-v", f"{host_path(egress_dir / 'allowlist.txt')}:/etc/squid/allowlist.txt:ro", SQUID)
        sh("network", "connect", NET, EGRESS)
        time.sleep(3)
        if sh("inspect", "-f", "{{.State.Running}}", EGRESS).stdout.strip() != "true":
            print(sh("logs", EGRESS, check=False).stderr[-2000:])
            raise SystemExit("egress proxy did not start")

        ws = ROOT / ".smoke-workspace"
        ws.mkdir(exist_ok=True)
        spec = SandboxSpec(task_id=TASK, image=IMAGE, memory="2g", cpus="1", pids_limit=256,
                           egress_proxy=f"http://{EGRESS}:3128", workspace_host_dir=host_path(ws),
                           session_key="smoke-session", secret_key="smoke-secret-key-0123456789abcdef",
                           network=NET)
        args = run_args(spec)
        print("docker", " ".join(a if " " not in a else repr(a) for a in args[:40]), "...")
        sh(*args, timeout=900)
        sbx = spec.container

        # 1) health
        ok = False
        for _ in range(60):
            code, status = curl(f"http://sandbox:8000/health")
            if code == 0 and status == "200":
                ok = True
                break
            if sh("inspect", "-f", "{{.State.Running}}", sbx, check=False).stdout.strip() != "true":
                print(sh("logs", "--tail", "40", sbx, check=False).stderr[-3000:])
                break
            time.sleep(3)
        results["sandbox /health == 200"] = ok

        # 2) no direct route out of the internal network
        code, status = curl("https://example.com/")
        results["direct Internet from sandbox network is blocked"] = code != 0 or status == "000"

        # 3) proxy allow-list
        code, status = curl("-x", f"http://{EGRESS}:3128", "https://api.tkbk.online/v1/models")
        results["proxy -> api.tkbk.online reachable (HTTP 401 without key)"] = status in ("401", "403", "200")
        code, status = curl("-x", f"http://{EGRESS}:3128", "https://example.com/")
        results["proxy -> example.com denied (403 from squid)"] = status == "403"
        code, status = curl("-x", f"http://{EGRESS}:3128", "https://pypi.org/simple/pip/")
        results["proxy -> pypi.org reachable"] = status == "200"

        # 4) from inside the sandbox: python honours HTTPS_PROXY (what litellm/pip/git will use)
        p = sh("exec", sbx, "python3", "-c",
               "import urllib.request,os;print(urllib.request.urlopen('https://api.tkbk.online/v1/models',timeout=20).status)",
               check=False, timeout=60)
        results["sandbox python via HTTPS_PROXY reaches api.tkbk.online"] = ("401" in p.stderr) or ("200" in p.stdout)
        p = sh("exec", sbx, "sh", "-c", "id -u; grep -E '^Cap(Eff|Bnd)' /proc/self/status", check=False)
        lines = p.stdout.splitlines()
        results["sandbox runs as uid 10001 with empty capability sets"] = (
            lines[:1] == ["10001"] and len(lines) >= 3
            and all(line.split()[-1] == "0000000000000000" for line in lines[1:3]))
        p = sh("inspect", sbx, check=False)
        hc = json.loads(p.stdout)[0]["HostConfig"]
        results["HostConfig: memory/pids/cap-drop applied"] = (
            hc.get("Memory") == 2 * 1024**3 and hc.get("PidsLimit") == 256 and hc.get("CapDrop") == ["ALL"]
            and "no-new-privileges" in (hc.get("SecurityOpt") or []))
    finally:
        cleanup()
    width = max(len(k) for k in results) + 2
    for k, v in results.items():
        print(f"{'PASS' if v else 'FAIL'}  {k.ljust(width)}")
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
