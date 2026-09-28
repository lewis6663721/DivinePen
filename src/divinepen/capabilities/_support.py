"""能力实现共用的底层动作：跑命令行、把远程产物落到本地。

**纪律**：命令行永远以 argv 列表传递，绝不经过 shell。这样用户提供的文件名
（可能含 ``; rm -rf /``）不可能被解释成命令。
"""

from __future__ import annotations

import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import anyio

from ..core.artifact import Artifact
from ..core.errors import ArtifactError, MediaToolMissing, ProviderRequestError
from ..core.ports import HttpRequest
from ..core.runtime import RunContext


@dataclass(slots=True)
class CommandResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    elapsed_ms: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run_command(
    argv: list[str],
    *,
    timeout: float = 300.0,
    cwd: str | Path | None = None,
) -> CommandResult:
    """执行外部命令。argv[0] 不存在时抛 ``MediaToolMissing``（页面据此提示安装）。

    刻意用 ``anyio.run_process`` 而不是自己 spawn + 读流：它替我们处理了
    取消时杀子进程、双流并发读取（避免管道写满导致死锁）这两件容易写错的事。
    """
    if not argv:
        raise ValueError("argv 不能为空")
    if shutil.which(argv[0]) is None and not Path(argv[0]).is_file():
        raise MediaToolMissing(f"找不到可执行文件：{argv[0]}", detail={"argv": argv})

    started = time.perf_counter()
    try:
        with anyio.fail_after(timeout):
            completed = await anyio.run_process(argv, check=False, cwd=str(cwd) if cwd else None)
    except TimeoutError as exc:
        raise MediaToolMissing(f"命令超时（{timeout}s）：{argv[0]}", detail={"argv": argv}) from exc
    return CommandResult(
        argv=argv,
        returncode=completed.returncode,
        stdout=(completed.stdout or b"").decode("utf-8", "replace"),
        stderr=(completed.stderr or b"").decode("utf-8", "replace"),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


@asynccontextmanager
async def local_file(artifact: Artifact, ctx: RunContext) -> AsyncIterator[Path]:
    """把产物转成**本地可读路径**：本地文件直接用，远程的先下载到临时目录。

    这与上传方向相反——上传走 ``ctx.artifacts.resolve_for_provider``，
    这里负责把供应商产物拉回来加工。临时目录用完即清。
    """
    if artifact.is_local:
        path = artifact.path()
        if not path.is_file():
            raise ArtifactError(f"文件不存在：{path}", detail={"uri": artifact.uri})
        yield path
        return

    resp = await ctx.http.request(HttpRequest(method="GET", url=artifact.uri, timeout=120.0))
    if not resp.ok:
        raise ProviderRequestError(
            f"下载产物失败：HTTP {resp.status_code}",
            status_code=resp.status_code,
            request_id=resp.request_id,
        )
    suffix = Path(artifact.uri.split("?")[0]).suffix or ""
    workdir = Path(tempfile.mkdtemp(prefix="divinepen-"))
    target = workdir / f"input{suffix}"
    target.write_bytes(resp.content or resp.text.encode())
    try:
        yield target
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
