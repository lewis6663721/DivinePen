"""开箱即用的装配：``pip install divinepen`` 后立刻能跑能力。

import anyio, divinepen

async def main():
    registry = divinepen.registry()
    print([m["id"] for m in registry.catalog()])

    ctx = divinepen.local_context()          # 不配数据库、不起 Redis
    result = await divinepen.run(
        "media.probe", {"source": "/path/to/video.mp4"}, ctx=ctx
    )
    print(result.status, result.output)

anyio.run(main)
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..core.capability import RunResult
from ..core.registry import CapabilityRegistry, build_default_registry
from ..core.runtime import RunContext
from ..core.specs import ModelSpec
from .http import HttpxTransport
from .memory import (
    EnvCredentialProvider,
    InMemoryTaskSink,
    InMemoryUsageSink,
    LocalArtifactStore,
    StaticModelCatalog,
)

#: 供应商代码 → 环境变量前缀的别名。阿里云百炼的 Key 惯例叫 DASHSCOPE_API_KEY。
ENV_ALIASES: dict[str, str] = {
    "aliyun": "DASHSCOPE",
    "aliyun-bailian": "DASHSCOPE",
    "dashscope": "DASHSCOPE",
}


def local_context(
    *,
    media_root: str | Path = "media",
    models: Iterable[ModelSpec] = (),
    api_keys: dict[str, str] | None = None,
    dry_run: bool = False,
    billed_allowed: bool = False,
    user_id: str | None = None,
    ctx: RunContext | None = None,
    **extra: Any,
) -> RunContext:
    """组装一个不依赖任何外部服务的运行上下文。

    :param media_root: 产物落盘目录
    :param models: 预置模型（脚本里通常手写几个 ModelSpec）
    :param api_keys: ``{"aliyun": "sk-..."}``；不传则回落环境变量
    :param dry_run: True 时只组装请求，绝不发网络、绝不花钱
    :param billed_allowed: "付费三道闸"第三道开关，默认关闭
    """
    if ctx is not None:
        return ctx.child(**extra)
    return RunContext(
        credentials=EnvCredentialProvider(api_keys, aliases=ENV_ALIASES),
        catalog=StaticModelCatalog(models),
        usage=InMemoryUsageSink(),
        tasks=InMemoryTaskSink(),
        artifacts=LocalArtifactStore(media_root),
        http=HttpxTransport(),
        dry_run=dry_run,
        billed_allowed=billed_allowed,
        user_id=user_id,
        extra=dict(extra),
    )


async def run(
    capability_id: str,
    data: dict[str, Any] | Any,
    *,
    ctx: RunContext | None = None,
    registry: CapabilityRegistry | None = None,
    **ctx_kwargs: Any,
) -> RunResult:
    """一行调用一个能力。默认用内置注册表 + 本地上下文。"""
    reg = registry or build_default_registry()
    context = ctx or local_context(**ctx_kwargs)
    return await reg.get(capability_id).invoke(data, context)


def list_capabilities(
    *, registry: CapabilityRegistry | None = None, category: str | None = None
) -> list[dict[str, Any]]:
    """能力目录（含表单 schema），可直接喂给页面。"""
    reg = registry or build_default_registry()
    return [
        info
        for info in (cap.info() for cap in reg)
        if category is None or info["category"] == category
    ]
