from __future__ import annotations

import pytest

from tests.conftest import KEY, FakeAgentServer, FakeDocker
from tkbk_agents.sandbox import LABEL, Sandbox, SandboxSpec, cleanup_orphans, run_args


def _spec() -> SandboxSpec:
    return SandboxSpec(task_id="t_0123456789abcdef", image="img:tag", memory="4g", cpus="2", pids_limit=512,
                       egress_proxy="http://tkbk-agents-egress:3128", workspace_host_dir="/var/lib/tkbk-agents/tasks/t/workspace",
                       session_key="SESSION", secret_key="SECRET", network="tkbk-task-t_0123456789abcdef")


def test_run_args_hardening_flags() -> None:
    a = run_args(_spec())
    joined = " ".join(a)
    for flag in ("--cap-drop ALL", "--security-opt no-new-privileges", "--memory 4g", "--memory-swap 4g",
                 "--cpus 2", "--pids-limit 512", "--network tkbk-task-t_0123456789abcdef", "--ulimit core=0",
                 f"--label {LABEL}=t_0123456789abcdef", "-v /var/lib/tkbk-agents/tasks/t/workspace:/workspace",
                 "-e HTTPS_PROXY=http://tkbk-agents-egress:3128", "-e OH_SESSION_API_KEYS_0=SESSION",
                 "-e OH_ENABLE_VSCODE=false", "-e DO_NOT_TRACK=1", "--restart no"):
        assert flag in joined, flag
    assert a[-5:] == ["img:tag", "--host", "0.0.0.0", "--port", "8000"]
    assert "-p" not in a and "--privileged" not in a and "docker.sock" not in joined
    assert KEY not in joined  # client secrets never go to docker run


@pytest.mark.asyncio
async def test_sandbox_start_stop_sequence(settings, fake_server: FakeAgentServer) -> None:
    d = FakeDocker()
    async with fake_server.client() as http:
        sb = Sandbox(settings, "t_aaaaaaaaaaaaaaaa", docker_fn=d, http=http)
        await sb.start()
        assert sb.workspace_dir.is_dir()
        assert d.calls[0][:3] == ["network", "create", "--internal"]
        assert d.has("network", "connect", sb.spec.network, settings.egress_container)
        assert d.has("network", "connect", sb.spec.network, settings.self_container)
        assert d.has("run", "-d", "--name", sb.spec.container)
        await sb.stop()
        assert d.has("rm", "-f", sb.spec.container)
        assert d.has("network", "disconnect", "-f", sb.spec.network, settings.egress_container)
        assert d.calls[-1] == ["network", "rm", sb.spec.network]
        assert not sb.task_dir.exists()


@pytest.mark.asyncio
async def test_sandbox_start_fails_when_container_dies(settings, fake_server: FakeAgentServer) -> None:
    d = FakeDocker(running="false")

    def h(req):
        import httpx
        return httpx.Response(503)

    import httpx
    async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as http:
        sb = Sandbox(settings, "t_bbbbbbbbbbbbbbbb", docker_fn=d, http=http)
        with pytest.raises(RuntimeError, match="exited during startup"):
            await sb.start()
        await sb.stop()
        assert d.has("network", "rm", sb.spec.network)


@pytest.mark.asyncio
async def test_sandbox_stop_keeps_workspace_when_asked(settings, fake_server: FakeAgentServer) -> None:
    d = FakeDocker()
    async with fake_server.client() as http:
        sb = Sandbox(settings, "t_cccccccccccccccc", docker_fn=d, http=http)
        await sb.start()
        await sb.stop(keep_workspace=True)
    assert sb.workspace_dir.is_dir()


@pytest.mark.asyncio
async def test_cleanup_orphans_removes_labelled_resources() -> None:
    class D(FakeDocker):
        async def __call__(self, *args, **kw):
            a = list(args)
            self.calls.append(a)
            if a[:2] == ["ps", "-aq"]:
                return "c1\nc2\n"
            if a[:3] == ["network", "ls", "-q"]:
                return "n1\n"
            if a[:2] == ["network", "inspect"]:
                return "tkbk-agents-egress tkbk-agents-api "
            return ""

    d = D()
    out = await cleanup_orphans(d)
    assert out == {"containers": 2, "networks": 1}
    assert d.has("rm", "-f", "c1", "c2") and d.has("network", "rm", "n1")
    assert d.has("network", "disconnect", "-f", "n1", "tkbk-agents-egress")
