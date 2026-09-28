"""运行时记录：用量与任务台账。

这两个是**跨层数据结构**（能力层产出、服务层落库、页面展示），因此放在契约层，
不依赖任何存储实现。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from .artifact import Artifact
from .manifest import CostUnit
from .specs import TaskStatus


def utcnow() -> datetime:
    return datetime.now(UTC)


class UsageRecord(BaseModel):
    """一次**真实**调用的用量与费用。

    约定：只有成功的结果才记账（阿里系也只对成功计费）。
    金额以远端返回的 usage 为准；本地 ``unit_price`` 只用于预估与展示。
    """

    capability_id: str
    provider: str
    model: str
    unit: CostUnit = CostUnit.FREE
    quantity: Decimal = Decimal(0)
    amount: Decimal | None = None
    currency: str = "CNY"
    request_id: str | None = None
    task_id: str | None = None
    user_id: str | None = None
    at: datetime = Field(default_factory=utcnow)
    raw: dict[str, Any] = {}


class TaskRef(BaseModel):
    """异步任务台账项。**远端 task_id 必须持久化**，否则刷新/重启就丢真钱。"""

    id: str
    capability_id: str
    provider: str
    model: str
    status: TaskStatus = TaskStatus.PENDING
    remote_task_id: str | None = None
    progress: int = 0
    payload: dict[str, Any] = {}
    result: dict[str, Any] = {}
    artifacts: list[Artifact] = []
    error: dict[str, Any] | None = None
    user_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
