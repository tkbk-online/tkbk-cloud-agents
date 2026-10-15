"""Thin async wrapper over the `docker` CLI (talks to the host daemon via the mounted socket)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence


class DockerError(RuntimeError):
    def __init__(self, args: Sequence[str], code: int | None, stderr: str):
        self.args_ = list(args)
        self.code = code
        self.stderr = stderr
        super().__init__(f"docker {' '.join(args[:3])}… failed ({code}): {stderr.strip()[:500]}")


DockerFn = Callable[..., Awaitable[str]]


async def docker(*args: str, timeout: float = 120.0, binary: str = "docker") -> str:
    """Run `docker <args>` and return stdout. Raises DockerError on non-zero exit or timeout."""
    proc = await asyncio.create_subprocess_exec(
        binary, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise DockerError(args, None, f"timeout after {timeout}s")
    if proc.returncode != 0:
        raise DockerError(args, proc.returncode, err.decode(errors="replace"))
    return out.decode(errors="replace")
