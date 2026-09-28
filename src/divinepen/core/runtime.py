"""RunContext：能力运行时能看到的一切。

能力**不允许**自己去读环境变量、连数据库、开 HTTP 连接。它只能通过 RunContext
拿到端口实现。这条纪律是"同一个能力既能跑在脚本里、又能跑在服务里"的前提。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from .ports import (
    ArtifactStore,
    CredentialProvider,
    HttpTransport,
    ModelCatalog,
    ResolvePolicy,
    TaskSink,
    UsageSink,
)
from .specs import ResolvedModel

_logger = logging.getLogger("divinepen")


@dataclass(slots=True)
class RunContext:
    """一次调用所需的全部依赖与开关。"""

    credentials: CredentialProvider
    catalog: ModelCatalog
    usage: UsageSink
    tasks: TaskSink
    artifacts: ArtifactStore
    http: HttpTransport

    #: True 时只组装请求、不发网络。所有能力都必须支持。
    dry_run: bool = False
    #: "付费三道闸"的第三道。未置 True 时，任何非免费能力的真实调用都会被拦下。
    billed_allowed: bool = False
    trace_id: str = ""
    user_id: str | None = None
    logger: logging.Logger = field(default=_logger)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.trace_id:
            self.trace_id = uuid.uuid4().hex

    def child(self, **overrides: Any) -> RunContext:
        """派生一个上下文（改开关、换用户、带 trace 继承）。"""
        if "trace_id" not in overrides:
            overrides["trace_id"] = self.trace_id
        return replace(self, **overrides)

    async def resolve_model(
        self,
        capability: str,
        *,
        provider: str | None = None,
        model_key: str | None = None,
        policy: ResolvePolicy = ResolvePolicy.PRIORITY,
    ) -> ResolvedModel:
        """按能力选模型。能力层唯一的"用谁做"入口。"""
        return await self.catalog.resolve(
            capability, provider=provider, model_key=model_key, policy=policy
        )
