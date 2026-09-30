"""Capability：原子能力的统一外衣。

生命周期（**"付费三道闸"就长在这里**）::

    invoke(data, ctx)
      ├─ 1. parse          输入校验（Pydantic）。失败 → CapabilityInputError
      ├─ 2. plan           组装出"将要发出的真实请求"，**零成本**、可展示给用户
      ├─ 3. guard          非免费能力且未授权 → BilledCallBlocked（第三道闸）
      ├─ 4. execute        真的发请求 / 跑 ffmpeg
      └─ 5. 记账            成功才写 UsageRecord（阿里系也只对成功计费）

第 2 步是这套设计的价值所在：页面可以在**一分钱不花**的前提下，把"点了会发生
什么"完整呈现给用户，用户确认后再带 ``billed_allowed=True`` 重放同一个 plan。
"""

from __future__ import annotations

import shutil
import time
from abc import ABC, abstractmethod
from decimal import Decimal
from enum import StrEnum
from typing import Any, ClassVar

import anyio
from pydantic import BaseModel, Field, ValidationError

from .artifact import Artifact
from .errors import (
    BilledCallBlocked,
    CapabilityInputError,
    CredentialMissing,
    MediaToolMissing,
)
from .manifest import CapabilityManifest, CostUnit, Requirement
from .paths import redact
from .records import TaskRef, UsageRecord
from .runtime import RunContext
from .specs import ParamsSchema, PollSpec


class RunStatus(StrEnum):
    DRY_RUN = "dry_run"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RequestKind(StrEnum):
    HTTP = "http"
    COMMAND = "command"  # 本地 ffmpeg/ffprobe 调用


class PlannedRequest(BaseModel):
    """一次**将要**发出的请求。dry-run 的输出就是它。

    同时承载两类动作：发给供应商的 HTTP 请求，和本地执行的命令行。
    这样"点下去会发生什么"在页面上是统一的一种呈现。
    """

    kind: RequestKind = RequestKind.HTTP
    method: str = "POST"
    url: str = ""
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    body: Any = None
    #: kind=command 时的 argv（**始终是列表**，绝不拼 shell 字符串）
    command: list[str] | None = None
    note: str = ""

    def masked(self) -> PlannedRequest:
        """脱敏版（Authorization 等），可直接展示给用户或写日志。"""
        return self.model_copy(update={"headers": redact(self.headers)})


class Plan(BaseModel):
    """执行计划：做什么、用哪个模型、发什么请求、大概花多少钱。"""

    capability_id: str
    provider: str | None = None
    model_key: str | None = None
    model_name: str | None = None
    steps: list[str] = Field(default_factory=list)
    requests: list[PlannedRequest] = Field(default_factory=list)
    poll: PollSpec | None = None
    estimated_unit: CostUnit = CostUnit.FREE
    estimated_quantity: Decimal | None = None
    estimated_amount: Decimal | None = None
    currency: str = "CNY"
    warnings: list[str] = Field(default_factory=list)
    requires_billing: bool = False

    def describe(self) -> str:
        """人类可读摘要，直接给页面/日志用。"""
        head = f"[{self.capability_id}] {self.model_key or '-'}"
        cost = "免费"
        if self.requires_billing:
            qty = (
                f"{self.estimated_quantity}{self.estimated_unit.value}"
                if self.estimated_quantity
                else self.estimated_unit.value
            )
            money = (
                f"≈{self.estimated_amount}{self.currency}" if self.estimated_amount else "金额未知"
            )
            cost = f"计费 {qty} {money}"
        return f"{head} · {cost} · {len(self.requests)} 个请求"

    def masked(self) -> Plan:
        return self.model_copy(update={"requests": [r.masked() for r in self.requests]})


class CapabilityOutcome(BaseModel):
    """execute 的产出。"""

    output: Any = None
    artifacts: list[Artifact] = Field(default_factory=list)
    usage: UsageRecord | None = None
    task: TaskRef | None = None
    warnings: list[str] = Field(default_factory=list)


class RunResult(BaseModel):
    """统一返回信封：一次能力调用的完整交代。"""

    capability_id: str
    status: RunStatus
    trace_id: str
    plan: Plan | None = None
    output: Any = None
    artifacts: list[Artifact] = Field(default_factory=list)
    usage: UsageRecord | None = None
    task: TaskRef | None = None
    error: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    elapsed_ms: int | None = None

    @property
    def ok(self) -> bool:
        return self.status is not RunStatus.FAILED


class Capability(ABC):
    """所有原子能力的基类。子类必须声明 ``manifest`` / ``Input`` / ``Output``。"""

    manifest: ClassVar[CapabilityManifest]
    Input: ClassVar[type[BaseModel]]
    Output: ClassVar[type[BaseModel]]

    # ── 自描述（页面表单直接用）──────────────────────────────────

    @classmethod
    def form_schema(cls) -> ParamsSchema:
        return ParamsSchema.from_pydantic(cls.Input)

    @classmethod
    def info(cls) -> dict[str, Any]:
        m = cls.manifest
        return {
            "id": m.id,
            "name": m.name,
            "version": m.version,
            "category": m.category.value,
            "description": m.description,
            "model_capability": m.model_capability,
            "agent_mode": m.agent_mode.value if m.agent_mode else None,
            "requires": [r.value for r in m.requires],
            "cost": m.cost.model_dump(mode="json"),
            "async_task": m.async_task,
            "cacheable": m.cacheable,
            "status": m.status.value,
            "tags": list(m.tags),
            "form": cls.form_schema().model_dump(mode="json"),
        }

    # ── 输入 ──────────────────────────────────────────────────────

    def parse(self, data: dict[str, Any] | BaseModel) -> BaseModel:
        if isinstance(data, self.Input):
            return data
        payload = data.model_dump() if isinstance(data, BaseModel) else dict(data)
        try:
            return self.Input.model_validate(payload)
        except ValidationError as exc:
            raise CapabilityInputError(
                f"{self.manifest.id} 输入校验失败",
                detail={"errors": exc.errors(include_url=False)},
            ) from exc

    # ── 前置体检 ──────────────────────────────────────────────────

    async def check_ready(self, ctx: RunContext) -> list[str]:
        """返回"缺什么"的人类可读列表；空列表表示就绪。

        默认实现覆盖外部二进制与凭据两类。子类可扩展（如缺某个必填的产品配置）。
        """
        missing: list[str] = []
        for req in self.manifest.requires:
            if req is Requirement.FFMPEG and shutil.which("ffmpeg") is None:
                missing.append("缺少 ffmpeg 可执行文件")
            elif req is Requirement.FFPROBE and shutil.which("ffprobe") is None:
                missing.append("缺少 ffprobe 可执行文件")
            elif req is Requirement.FFMPEG_SUBTITLES and not await ffmpeg_has_filter("subtitles"):
                missing.append("当前 ffmpeg 未编译字幕滤镜（libass），字幕烧录不可用")
        if self.manifest.needs_credential:
            provider = self.manifest.model_capability or self.manifest.namespace
            ready = await _credential_ready(ctx, provider)
            if ready is False:
                missing.append(f"未配置供应商 {provider} 的 API Key")
        return missing

    async def assert_ready(self, ctx: RunContext) -> None:
        missing = await self.check_ready(ctx)
        if missing:
            provider = self.manifest.model_capability or self.manifest.namespace
            if any("API Key" in m for m in missing):
                raise CredentialMissing(" · ".join(missing), detail={"provider": provider})
            raise MediaToolMissing(" · ".join(missing), detail={"missing": missing})

    # ── 核心：plan / execute ──────────────────────────────────────

    @abstractmethod
    async def plan(self, inputs: BaseModel, ctx: RunContext) -> Plan:
        """组装将要发出的请求。**不得发网络请求、不得花钱。**"""

    @abstractmethod
    async def execute(self, inputs: BaseModel, plan: Plan, ctx: RunContext) -> CapabilityOutcome:
        """真的执行。只有通过 invoke() 走到这里才允许。"""

    # ── 统一切入口 ────────────────────────────────────────────────

    async def invoke(self, data: dict[str, Any] | BaseModel, ctx: RunContext) -> RunResult:
        started = time.perf_counter()
        inputs = self.parse(data)
        plan = await self.plan(inputs, ctx)

        if ctx.dry_run:
            return self._pack(RunStatus.DRY_RUN, ctx, plan, started)

        if plan.requires_billing and not ctx.billed_allowed:
            raise BilledCallBlocked(
                f"{self.manifest.id} 会产生费用，需用户确认后才能执行",
                detail={"plan": plan.masked().model_dump(mode="json")},
            )

        from .errors import DivinePenError

        try:
            outcome = await self.execute(inputs, plan, ctx)
        except DivinePenError as exc:
            ctx.logger.warning("capability failed: %s | %s", self.manifest.id, exc)
            return self._pack(RunStatus.FAILED, ctx, plan, started, error=exc.as_dict())

        if outcome.usage is not None:
            await ctx.usage.record(outcome.usage)

        return self._pack(RunStatus.SUCCEEDED, ctx, plan, started, outcome=outcome)

    # ── 内部 ──────────────────────────────────────────────────────

    def _pack(
        self,
        status: RunStatus,
        ctx: RunContext,
        plan: Plan,
        started: float,
        *,
        outcome: CapabilityOutcome | None = None,
        error: dict[str, Any] | None = None,
    ) -> RunResult:
        return RunResult(
            capability_id=self.manifest.id,
            status=status,
            trace_id=ctx.trace_id,
            plan=plan,
            output=outcome.output if outcome else None,
            artifacts=list(outcome.artifacts) if outcome else [],
            usage=outcome.usage if outcome else None,
            task=outcome.task if outcome else None,
            error=error,
            warnings=list(outcome.warnings) if outcome else [],
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )


_FFMPEG_FILTER_CACHE: dict[str, bool] = {}


async def ffmpeg_has_filter(name: str) -> bool:
    """探测本机 ffmpeg 是否带某个滤镜（进程内缓存）。

    发行版/ Homebrew 的 ffmpeg 经常不带 libass（``subtitles``/``ass``）或
    freetype（``drawtext``）。缺滤镜不是调用方的错，体检要提前说清楚。
    """
    if name in _FFMPEG_FILTER_CACHE:
        return _FFMPEG_FILTER_CACHE[name]
    if shutil.which("ffmpeg") is None:
        _FFMPEG_FILTER_CACHE[name] = False
        return False
    completed = await anyio.run_process(["ffmpeg", "-hide_banner", "-filters"], check=False)
    names: set[str] = set()
    for line in (completed.stdout or b"").decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 2 and len(parts[0]) <= 3:
            names.add(parts[1])
    ok = name in names
    _FFMPEG_FILTER_CACHE[name] = ok
    return ok


async def _credential_ready(ctx: RunContext, provider: str) -> bool | None:
    """True/False 有结论；None 表示端口不支持探测，不阻断。"""
    has = getattr(ctx.credentials, "has", None)
    if has is None:
        return None
    try:
        return bool(await has(provider))
    except Exception:
        return None
