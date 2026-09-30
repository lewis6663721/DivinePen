"""编排层：把多个原子能力串成一条可复现的流水线。

设计取舍（刻意保持小）：

* **顺序 DAG，不做通用图**。一期所有业务流水线都是"上一步的产物喂下一步"，
  列表即拓扑；真需要分支/并行时再升级，不提前背复杂度。
* **引用是字符串约定**：``"$<step_id>.uri"`` 取某步产物地址，
  ``"$<step_id>.out.<字段>"`` 取某步输出模型的字段，``"$params.<key>"`` 取流水线入参。
  写错引用在 plan 阶段就报错，不留到执行中途。
* **dry-run 时引用解析成占位符**。因为上一步还没真跑、产物不存在；
  页面要的是"将会发生什么"，不是假产物。
* 流水线本身是一个 ``Capability``，所以**付费三道闸、体检、注册表、页面全部白拿**。
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from .core.capability import (
    Capability,
    CapabilityOutcome,
    Plan,
    RunResult,
)
from .core.errors import CapabilityError, PipelinePaused
from .core.manifest import CostUnit
from .core.registry import CapabilityRegistry
from .core.runtime import RunContext

REF_PREFIX = "$"


class StepSpec(BaseModel):
    id: str = Field(description="步骤 id，被后续步骤以 $<id>.* 引用")
    capability: str = Field(description="要调用的原子能力 id")
    inputs: dict[str, Any] = Field(default_factory=dict, description="能力入参；值里可含 $ 引用")
    description: str = ""
    #: 人工闸口：执行到这里先停下，等 resume 带确认/改参回来（interactive 模式的核心）
    gate: bool = False
    gate_prompt: str = Field(default="", description="停在闸口时问用户什么")
class Recipe(BaseModel):
    """一条流水线的静态定义。"""

    id: str
    name: str
    description: str = ""
    steps: list[StepSpec]

    def step(self, step_id: str) -> StepSpec:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise CapabilityError(f"流水线 {self.id} 没有步骤 {step_id}")


class PipelineStepResult(BaseModel):
    step_id: str
    capability: str
    status: str
    output: Any = None
    artifacts: list[str] = Field(default_factory=list)
    elapsed_ms: int | None = None


class PipelineOutput(BaseModel):
    steps: list[PipelineStepResult] = Field(default_factory=list)
    final_artifacts: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.status != "failed" for s in self.steps)


class PipelineError(CapabilityError):
    """某一步失败。带上 step_id，页面能直接指认是哪一步炸的。"""


def resolve_refs(
    value: Any,
    *,
    outputs: dict[str, Any],
    params: dict[str, Any],
    placeholder: str | None = None,
) -> Any:
    """把 ``$…`` 引用换成真实值。``placeholder`` 非空即 dry-run 模式。"""
    if isinstance(value, str) and value.startswith(REF_PREFIX):
        return _resolve_one(value, outputs=outputs, params=params, placeholder=placeholder)
    if isinstance(value, list):
        return [
            resolve_refs(v, outputs=outputs, params=params, placeholder=placeholder) for v in value
        ]
    if isinstance(value, dict):
        return {
            k: resolve_refs(v, outputs=outputs, params=params, placeholder=placeholder)
            for k, v in value.items()
        }
    return value


def _resolve_one(
    ref: str,
    *,
    outputs: dict[str, Any],
    params: dict[str, Any],
    placeholder: str | None,
) -> Any:
    path = ref[len(REF_PREFIX) :]
    if path.startswith("params."):
        key = path[len("params.") :]
        if key not in params:
            raise PipelineError(f"引用了不存在的流水线入参：{ref}")
        return params[key]
    head, _, rest = path.partition(".")
    if head not in outputs:
        if placeholder is not None:
            return placeholder
        raise PipelineError(f"引用了尚未产出的步骤：{ref}")
    entry = outputs[head]
    if not rest or rest == "uri":
        uri = entry.get("uri")
        if uri:
            return uri
        artifacts = entry.get("artifacts") or []
        if artifacts:
            return artifacts[0]
        if placeholder is not None:
            return placeholder
        raise PipelineError(f"步骤 {head} 没有产物可引用：{ref}")
    if rest.startswith("out."):
        field = rest[len("out.") :]
        out = entry.get("out") or {}
        if field not in out:
            raise PipelineError(f"步骤 {head} 的输出没有字段 {field}：{ref}")
        return out[field]
    raise PipelineError(f"无法理解的引用：{ref}")


def _snapshot(result: RunResult) -> dict[str, Any]:
    output = result.output
    dump = output.model_dump(mode="json") if isinstance(output, BaseModel) else output
    uri = None
    if isinstance(dump, dict):
        uri = dump.get("uri")
    artifacts = [a.uri for a in result.artifacts]
    if not uri and artifacts:
        uri = artifacts[0]
    return {"uri": uri, "out": dump if isinstance(dump, dict) else {}, "artifacts": artifacts}


class PipelineCapability(Capability):
    """流水线能力基类。子类给 ``manifest`` / ``recipe`` / ``Input``。"""

    recipe: ClassVar[Recipe]

    async def plan(self, inputs: BaseModel, ctx: RunContext) -> Plan:
        params = inputs.model_dump(mode="json")
        registry: CapabilityRegistry = ctx.extra.get("registry") or _registry_from(ctx)
        plan = Plan(capability_id=self.manifest.id, steps=[], requests=[])
        outputs: dict[str, Any] = {}
        for step in self.recipe.steps:
            resolved = resolve_refs(
                step.inputs, outputs=outputs, params=params, placeholder="<上一步产物>"
            )
            cap = registry.get(step.capability)
            sub = await cap.invoke(
                resolved, ctx.child(dry_run=True, extra={**ctx.extra, "registry": registry})
            )
            if sub.plan is None:
                raise PipelineError(f"步骤 {step.id} 无法生成计划")
            label = step.description or step.capability
            plan.steps.append(f"{step.id} · {label}（{sub.plan.describe()}）")
            plan.requests.extend(sub.plan.requests)
            if sub.plan.requires_billing:
                plan.requires_billing = True
                plan.estimated_unit = sub.plan.estimated_unit
                if sub.plan.estimated_quantity is not None:
                    plan.estimated_quantity = (
                        plan.estimated_quantity or 0
                    ) + sub.plan.estimated_quantity
            plan.warnings.extend(f"[{step.id}] {w}" for w in sub.plan.warnings)
            outputs[step.id] = {"uri": "<上一步产物>", "out": {}, "artifacts": []}
        if plan.requires_billing:
            plan.warnings.append("流水线含计费步骤：确认前不会花一分钱。")
        return plan

    async def execute(self, inputs: BaseModel, plan: Plan, ctx: RunContext) -> CapabilityOutcome:
        params = inputs.model_dump(mode="json")
        registry: CapabilityRegistry = ctx.extra.get("registry") or _registry_from(ctx)
        resume = ctx.extra.get("resume") or {}
        outputs: dict[str, Any] = dict(resume.get("outputs") or {})
        steps: list[PipelineStepResult] = [
            PipelineStepResult.model_validate(s) for s in (resume.get("steps") or [])
        ]
        start_index = int(resume.get("start_index") or 0)
        approvals: dict[str, Any] = ctx.extra.get("gate_approvals") or {}
        for index, step in enumerate(self.recipe.steps):
            if index < start_index:
                continue
            if step.gate and step.id not in approvals:
                raise PipelinePaused(
                    f"流水线停在人工闸口：{step.id}",
                    detail={
                        "step_id": step.id,
                        "gate_prompt": step.gate_prompt or step.description,
                        "start_index": index,
                        "total_steps": len(self.recipe.steps),
                        "outputs": outputs,
                        "steps": [s.model_dump(mode="json") for s in steps],
                    },
                )
            resolved = resolve_refs(step.inputs, outputs=outputs, params=params)
            override = approvals.get(step.id)
            if isinstance(override, dict):
                resolved = {**resolved, **override}
            cap = registry.get(step.capability)
            result = await cap.invoke(
                resolved,
                ctx.child(
                    billed_allowed=ctx.billed_allowed, extra={**ctx.extra, "registry": registry}
                ),
            )
            if result.status.value == "failed":
                raise PipelineError(
                    f"步骤 {step.id}（{step.capability}）失败",
                    detail={"error": result.error},
                )
            outputs[step.id] = _snapshot(result)
            steps.append(
                PipelineStepResult(
                    step_id=step.id,
                    capability=step.capability,
                    status=result.status.value,
                    output=result.output.model_dump(mode="json")
                    if isinstance(result.output, BaseModel)
                    else result.output,
                    artifacts=[a.uri for a in result.artifacts],
                    elapsed_ms=result.elapsed_ms,
                )
            )
        last = steps[-1] if steps else None
        final = list(last.artifacts) if last else []
        if not final and last and isinstance(last.output, dict) and last.output.get("uri"):
            final = [last.output["uri"]]
        return CapabilityOutcome(output=PipelineOutput(steps=steps, final_artifacts=final))

    @classmethod
    def info(cls) -> dict[str, Any]:
        data = super().info()
        data["recipe"] = {
            "id": cls.recipe.id,
            "name": cls.recipe.name,
            "description": cls.recipe.description,
            "steps": [s.model_dump(mode="json") for s in cls.recipe.steps],
        }
        return data


def _registry_from(ctx: RunContext) -> CapabilityRegistry:
    registry = ctx.extra.get("registry")
    if registry is None:
        raise PipelineError(
            "流水线需要 RunContext.extra['registry']：编排要按 id 取能力，而注册表不属于端口契约"
        )
    return registry


def cost_free() -> CostUnit:
    return CostUnit.FREE


__all__ = [
    "PipelineCapability",
    "PipelineError",
    "PipelineOutput",
    "PipelineStepResult",
    "Recipe",
    "StepSpec",
    "resolve_refs",
]
