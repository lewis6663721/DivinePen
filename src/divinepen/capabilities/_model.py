"""模型绑定能力的通用外壳。

本地能力（ffmpeg）的 Input 是 Pydantic 模型、表单由它派生；模型绑定能力不一样 ——
**参数的真相在接口文档里**，落在数据库的 ``dp_model.params``（点号路径 schema）。
所以这里的能力只收一个扁平 ``params`` 字典，校验与拼体都交给 ``ParamsSchema``，
请求组装与响应解析交给 ``providers/aliyun.py``。

"付费三道闸"在这里依然成立：``plan()`` 只组装不发送；``invoke()`` 在
``requires_billing`` 且 ``billed_allowed=False`` 时抛 ``BilledCallBlocked``。
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any, ClassVar

import anyio
from pydantic import BaseModel, Field

from ..core.capability import (
    Capability,
    CapabilityOutcome,
    Plan,
)
from ..core.errors import CapabilityInputError, ProviderRequestError, ProviderTaskFailed
from ..core.manifest import CapabilityManifest, Category, CostModel, CostUnit, Requirement
from ..core.ports import HttpRequest, ResolvePolicy
from ..core.records import UsageRecord
from ..core.runtime import RunContext
from ..core.specs import ModelSpec, TaskStatus
from ..providers import aliyun


class ModelCallInput(BaseModel):
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="点号路径参数，与所选模型的接口文档逐字对应，如 parameters.duration。",
        json_schema_extra={"label": "参数", "group": "params", "group_label": "模型参数"},
    )
    provider: str | None = Field(
        default=None,
        description="供应商 code（如 aliyun-bailian）。与 model 一起用可唯一定位一个模型。",
        json_schema_extra={"label": "供应商", "group": "route", "group_label": "路由"},
    )
    model: str | None = Field(
        default=None,
        description="指定模型 key（如 wan2.7-t2v-2026-06-12）；不填则按策略自动选。",
        json_schema_extra={"label": "指定模型", "group": "route", "group_label": "路由"},
    )
    policy: str = Field(
        default="priority",
        description="选模型策略：priority / cheapest / explicit。",
        json_schema_extra={
            "label": "选模型策略",
            "group": "route",
            "group_label": "路由",
            "options": ["priority", "cheapest", "explicit"],
        },
    )


class ModelCallOutput(BaseModel):
    text: str | None = Field(default=None, description="文本类模型的回复")
    task_id: str | None = Field(default=None, description="本地任务台账 id")
    remote_task_id: str | None = Field(default=None, description="供应商侧 task_id")
    artifacts: list[str] = Field(default_factory=list, description="产物 URI 列表")
    polls: int = 0
    raw: dict[str, Any] = Field(default_factory=dict, description="供应商原始响应（截断保存）")


class ModelBackedCapability(Capability):
    """所有模型绑定能力的公共实现。子类只换 manifest。"""

    manifest: ClassVar[CapabilityManifest]
    Input = ModelCallInput
    Output = ModelCallOutput
    #: 参数表单来自所选模型的 ``dp_model.params``，不是来自 ``Input``。见基类同名量。
    model_driven: ClassVar[bool] = True

    # ── plan：只组装，不发送 ─────────────────────────────────────

    async def plan(self, inputs: ModelCallInput, ctx: RunContext) -> Plan:
        model, values = await self._resolve(inputs, ctx)
        body = model.params.build_body(values)
        api_key = await self._optional_key(ctx, model)
        request = aliyun.submit_request(
            model, body, api_key=api_key or f"$DIVINEPEN_{model.provider.upper()}_API_KEY"
        )
        warnings: list[str] = []
        if not api_key:
            warnings.append(f"未配置 {model.provider} 的 API Key，真实调用会被拒绝。")
        if not model.meta.get("verified", True):
            warnings.append("该模型参数未逐条核对文档，字段可能变动。")
        if model.is_async:
            warnings.append("异步任务：提交后轮询，远端 task_id 会落库。")
        billed = model.pricing.unit is not CostUnit.FREE
        return Plan(
            capability_id=self.manifest.id,
            provider=model.provider,
            model_key=model.key,
            model_name=model.display_name,
            steps=(
                ["提交异步任务", "轮询直到终态", "收集产物并记账"]
                if model.is_async
                else ["发送请求", "解析响应并记账"]
            ),
            requests=[request],
            poll=model.api.poll if model.is_async else None,
            estimated_unit=model.pricing.unit,
            estimated_quantity=_estimate_quantity(model, values),
            estimated_amount=None,
            currency=model.pricing.currency,
            requires_billing=billed,
            warnings=warnings,
        )

    # ── execute：真发请求 ────────────────────────────────────────

    async def execute(
        self, inputs: ModelCallInput, plan: Plan, ctx: RunContext
    ) -> CapabilityOutcome:
        model, values = await self._resolve(inputs, ctx, explicit=plan.model_key)
        body = model.params.build_body(values)
        api_key = await ctx.credentials.get(model.provider)
        request = aliyun.submit_request(model, body, api_key=api_key)
        response = await ctx.http.request(
            HttpRequest(
                method=request.method,
                url=request.url,
                headers=request.headers,
                json_body=request.body,
                timeout=120.0,
            )
        )
        if not response.ok:
            raise ProviderRequestError(
                f"{model.provider} 提交失败：HTTP {response.status_code}",
                status_code=response.status_code,
                request_id=response.request_id,
                detail={
                    "body": aliyun.parse_submit(model, response.body)["error"] or response.body
                },
            )
        payload = response.body if isinstance(response.body, dict) else {}

        task = None
        polls = 0
        if model.is_async:
            parsed = aliyun.parse_submit(model, payload)
            if parsed["error"] or not parsed["task_id"]:
                raise ProviderTaskFailed(
                    f"{model.provider} 提交未返回 task_id",
                    detail={"error": parsed["error"], "payload": payload},
                )
            task = await self._open_task(
                ctx,
                provider=model.provider,
                model=model.key,
                remote_task_id=str(parsed["task_id"]),
                payload=request.body,
            )
            payload, polls = await self._poll_until_done(
                model, str(parsed["task_id"]), api_key, ctx, task.id
            )
            # 图像族的 PollSpec 故意不声明产物路径（多 choice × 多 content 块，
            # 一条点号路径表达不了，见 catalog.ASYNC_POLL_IMAGE），所以这里
            # 契约取不到就交给图像族的解析器统一捞 —— 它遍历全部 choice。
            artifacts = aliyun.pick_artifacts(model, payload)
            if not artifacts:
                artifacts = aliyun.pick_image_artifacts(payload)
            await ctx.tasks.update(
                task.id,
                status=TaskStatus.SUCCEEDED.value,
                progress=100,
                polls=polls,
                result=payload,
                finished_at=_now_iso(),
            )
        else:
            parsed_error = aliyun.parse_submit(model, payload)["error"]
            if parsed_error:
                raise ProviderRequestError(
                    f"{model.provider} 返回业务错误：{parsed_error}",
                    request_id=response.request_id,
                    detail={"payload": payload},
                )
            artifacts = aliyun.pick_image_artifacts(payload)

        text = _extract_text(payload)
        quantity = aliyun.quantity_of(model, payload, artifacts)
        usage = UsageRecord(
            capability_id=self.manifest.id,
            provider=model.provider,
            model=model.key,
            unit=model.pricing.unit,
            quantity=Decimal(str(quantity)),
            amount=None,
            currency=model.pricing.currency,
            request_id=response.request_id,
            task_id=task.id if task else None,
            user_id=ctx.user_id,
            raw={"usage": payload.get("usage") or {}},
        )
        return CapabilityOutcome(
            output=ModelCallOutput(
                text=text,
                task_id=task.id if task else None,
                remote_task_id=str(aliyun.parse_submit(model, payload)["task_id"] or "") or None,
                artifacts=[a.uri for a in artifacts],
                polls=polls,
                raw=_truncate(payload),
            ),
            artifacts=artifacts,
            usage=usage,
            task=task,
        )

    # ── 内部 ────────────────────────────────────────────────────

    async def _open_task(
        self,
        ctx: RunContext,
        *,
        provider: str,
        model: str,
        remote_task_id: str,
        payload: dict[str, Any],
    ) -> Any:
        """把远端 task_id 记到账上。

        服务端（``capability_run``）在提交那一刻就已经建好了这一条任务，好让
        用户在任务中心立刻看见它。所以这里**优先复用那一行**，只把 remote id
        补上去；若无脑再 ``create`` 一条，一次生成就会在账本上留下两条互相
        不认识的任务 —— 一条有参数，一条有进度和远端 id，谁都拼不出全貌。
        库外调用（``ctx.extra`` 里没有 task_id）才新建。
        """
        bound = (ctx.extra or {}).get("task_id")
        if bound:
            ref = await ctx.tasks.update(
                str(bound),
                provider=provider,
                model=model,
                remote_task_id=remote_task_id,
                status=TaskStatus.PENDING.value,
                payload=payload,
            )
            if ref is not None:
                return ref
        return await ctx.tasks.create(
            capability_id=self.manifest.id,
            provider=provider,
            model=model,
            status=TaskStatus.PENDING.value,
            remote_task_id=remote_task_id,
            payload=payload,
            user_id=ctx.user_id,
        )

    async def _resolve(
        self, inputs: ModelCallInput, ctx: RunContext, *, explicit: str | None = None
    ) -> tuple[ModelSpec, dict[str, Any]]:
        policy = ResolvePolicy.EXPLICIT if explicit else _policy(inputs.policy)
        resolved = await ctx.catalog.resolve(
            self.manifest.model_capability or self.manifest.id,
            provider=inputs.provider,
            model_key=explicit or inputs.model,
            policy=policy,
        )
        model = resolved.model
        try:
            values = model.params.validate_values(inputs.params)
        except ValueError as exc:
            raise CapabilityInputError(
                f"{self.manifest.id} 参数校验失败：{exc}",
                detail={"model": model.key},
            ) from exc
        return model, values

    async def _optional_key(self, ctx: RunContext, model: ModelSpec) -> str:
        try:
            return await ctx.credentials.get(model.provider)
        except Exception:
            return ""

    async def _poll_until_done(
        self,
        model: ModelSpec,
        task_id: str,
        api_key: str,
        ctx: RunContext,
        local_task_id: str,
    ) -> tuple[dict[str, Any], int]:
        poll = model.api.poll
        if poll is None:
            raise ProviderTaskFailed(f"模型 {model.key} 是异步模式但没有轮询配置")
        deadline = time.monotonic() + poll.timeout_seconds
        polls = 0
        while True:
            await anyio.sleep(poll.interval_seconds)
            polls += 1
            request = aliyun.poll_request(model, task_id, api_key=api_key)
            response = await ctx.http.request(
                HttpRequest(
                    method=request.method,
                    url=request.url,
                    headers=request.headers,
                    timeout=60.0,
                )
            )
            if not response.ok:
                raise ProviderRequestError(
                    f"轮询失败：HTTP {response.status_code}",
                    status_code=response.status_code,
                    request_id=response.request_id,
                )
            parsed = aliyun.parse_poll(model, response.body)
            status: TaskStatus = parsed["status"]
            # 这里以前写 progress=min(90, polls * 3) —— 每轮询 3 次涨 9%，
            # 30 个周期爬到 90% 然后停住。那个数字和远端做到哪了毫无关系：
            # 远端 5% 和 95% 在本地显示一模一样，用户盯着"90%"等 20 分钟只会
            # 以为卡住了。**百炼的轮询接口根本不返回百分比**，所以现在只写
            # 真事实：轮询次数 + 状态。页面拿 updated_at 当心跳去算"最后回音"。
            await ctx.tasks.update(
                local_task_id,
                status=status.value,
                polls=polls,
            )
            if status is TaskStatus.SUCCEEDED:
                return response.body if isinstance(response.body, dict) else {}, polls
            if status in (TaskStatus.FAILED, TaskStatus.CANCELED):
                await ctx.tasks.update(
                    local_task_id,
                    status=status.value,
                    error={"message": parsed["error"]},
                    finished_at=_now_iso(),
                )
                raise ProviderTaskFailed(
                    f"远端任务{status.value}：{parsed['error']}",
                    detail={"remote_task_id": task_id, "payload": parsed["output"]},
                )
            if time.monotonic() > deadline:
                await ctx.tasks.update(
                    local_task_id,
                    status=TaskStatus.FAILED.value,
                    error={"message": f"轮询超时（{poll.timeout_seconds}s）"},
                    finished_at=_now_iso(),
                )
                raise ProviderTaskFailed(
                    f"轮询超时（{poll.timeout_seconds}s）",
                    detail={"remote_task_id": task_id, "polls": polls},
                )


def _policy(value: str) -> ResolvePolicy:
    try:
        return ResolvePolicy(value)
    except ValueError:
        return ResolvePolicy.PRIORITY


def _estimate_quantity(model: ModelSpec, values: dict[str, Any]) -> Decimal | None:
    unit = model.pricing.unit
    if unit is CostUnit.SECOND:
        raw = values.get("parameters.duration")
    elif unit is CostUnit.IMAGE:
        raw = values.get("parameters.n") or 1
    else:
        return None
    try:
        return Decimal(str(raw))
    except (TypeError, ValueError, ArithmeticError):
        return None


def _extract_text(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices") or []
    if choices:
        message = (choices[0] or {}).get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    output = payload.get("output") or {}
    if isinstance(output, dict):
        text = output.get("text") or output.get("finish_reason")
        if isinstance(text, str):
            return text
    return None


def _truncate(payload: dict[str, Any], limit: int = 4000) -> dict[str, Any]:
    import json

    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return payload
    return {"_truncated": True, "_preview": text[:limit]}


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def model_capability(
    *,
    cap_id: str,
    name: str,
    description: str,
    category: Category,
    model_capability: str,
    cost_unit: CostUnit,
    cost_notes: str = "",
    async_task: bool = False,
    cacheable: bool = False,
    tags: tuple[str, ...] = (),
) -> type[ModelBackedCapability]:
    """造一个模型绑定能力类。manifest 是唯一差异点。"""
    manifest = CapabilityManifest(
        id=cap_id,
        name=name,
        category=category,
        description=description,
        model_capability=model_capability,
        requires=[Requirement.NETWORK],
        cost=CostModel(unit=cost_unit, notes=cost_notes),
        async_task=async_task,
        cacheable=cacheable,
        tags=list(tags),
    )

    class _Generated(ModelBackedCapability):
        pass

    _Generated.manifest = manifest
    _Generated.__name__ = f"Cap_{cap_id.replace('.', '_')}"
    _Generated.__doc__ = description
    return _Generated
