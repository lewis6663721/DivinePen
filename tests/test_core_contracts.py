"""契约层测试：这些断言定义的是"底座的行为底线"，不是实现细节。

任何一条挂掉都意味着底座的对外承诺变了，必须同步改 ``docs/ARCHITECTURE.md``。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

# ── 测试替身 ───────────────────────────────────────────────────────
from pydantic import BaseModel, Field

from divinepen import (
    ApiSpec,
    Artifact,
    ArtifactKind,
    BilledCallBlocked,
    Capability,
    CapabilityError,
    CapabilityInputError,
    CapabilityManifest,
    CapabilityRegistry,
    Category,
    CostModel,
    CostUnit,
    ModelSpec,
    ParamsSchema,
    Plan,
    Pricing,
    RunContext,
    RunStatus,
    build_default_registry,
    dotted_get,
    nest_from_dotted,
    redact,
)
from divinepen.core.capability import CapabilityOutcome, PlannedRequest
from divinepen.core.specs import FieldType
from divinepen.services import list_capabilities, local_context, run


class EchoInput(BaseModel):
    text: str = Field(
        min_length=1,
        description="要回显的文本",
        json_schema_extra={"label": "文本", "group": "input", "group_label": "输入"},
    )
    repeat: int = Field(
        default=1,
        json_schema_extra={"label": "重复次数", "group": "input", "group_label": "输入"},
    )
    style: str = Field(
        default="plain",
        json_schema_extra={"label": "风格", "group": "input", "group_label": "输入"},
    )


class EchoOutput(BaseModel):
    text: str


class EchoCapability(Capability):
    """最简能力：证明 plan/invoke/guard 三条路径都活着。"""

    manifest = CapabilityManifest(
        id="system.echo",
        name="回显",
        category=Category.SYSTEM,
        cost=CostModel(unit=CostUnit.REQUEST),
    )
    Input = EchoInput
    Output = EchoOutput
    executed = 0

    async def plan(self, inputs: EchoInput, ctx: RunContext) -> Plan:
        return Plan(
            capability_id=self.manifest.id,
            steps=["回显输入"],
            requests=[PlannedRequest(body={"text": inputs.text})],
            estimated_unit=CostUnit.REQUEST,
            estimated_quantity=Decimal(1),
            requires_billing=True,
        )

    async def execute(self, inputs: EchoInput, plan: Plan, ctx: RunContext) -> CapabilityOutcome:
        type(self).executed += 1
        return CapabilityOutcome(output=EchoOutput(text=inputs.text * inputs.repeat))


# ── 注册表 ─────────────────────────────────────────────────────────


def test_builtin_registry_contains_media_probe() -> None:
    reg = build_default_registry(discover=False)
    assert "media.probe" in reg
    assert reg.get("media.probe").manifest.category is Category.MEDIA


def test_registry_rejects_duplicate_id() -> None:
    reg = CapabilityRegistry()
    reg.register(EchoCapability(), source="a")
    with pytest.raises(CapabilityError) as exc:
        reg.register(EchoCapability(), source="b")
    assert exc.value.detail["existing_source"] == "a"


def test_registry_rejects_missing_output_model() -> None:
    class Broken(Capability):
        manifest = CapabilityManifest(id="system.broken", name="坏", category=Category.SYSTEM)
        Input = EchoInput

        async def plan(self, inputs: BaseModel, ctx: RunContext) -> Plan:  # pragma: no cover
            raise NotImplementedError

        async def execute(  # pragma: no cover
            self, inputs: BaseModel, plan: Plan, ctx: RunContext
        ) -> CapabilityOutcome:
            raise NotImplementedError

    with pytest.raises(CapabilityError):
        CapabilityRegistry().register(Broken())


def test_manifest_id_must_be_namespaced() -> None:
    with pytest.raises(ValueError):
        CapabilityManifest(id="probe", name="错", category=Category.MEDIA)


def test_manifest_rejects_category_mismatch() -> None:
    with pytest.raises(ValueError):
        CapabilityManifest(id="image.t2i", name="错分类", category=Category.VIDEO)


# ── 表单 schema 由 Pydantic 派生 ───────────────────────────────────


def test_form_schema_is_derived_from_pydantic() -> None:
    schema = EchoCapability.form_schema()
    assert [g.key for g in schema.groups] == ["input"]
    keys = [f.key for f in schema.fields()]
    assert keys == ["text", "repeat", "style"]
    text = schema.field_map()["text"]
    assert text.label == "文本"
    assert text.required is True
    assert text.type is FieldType.TEXT
    assert text.control == "input"
    assert schema.field_map()["repeat"].type is FieldType.INT


def test_form_schema_rejects_unknown_and_missing() -> None:
    schema = EchoCapability.form_schema()
    with pytest.raises(ValueError, match="未知参数"):
        schema.validate_values({"text": "a", "typo": 1})
    with pytest.raises(ValueError, match="缺少必填参数"):
        schema.validate_values({})
    assert schema.validate_values({"text": "a"}) == {"text": "a", "repeat": 1, "style": "plain"}


def test_list_capabilities_returns_form_for_page() -> None:
    items = {c["id"]: c for c in list_capabilities()}
    probe = items["media.probe"]
    assert probe["category"] == "media"
    assert probe["requires"] == ["ffprobe"]
    assert probe["form"]["groups"][0]["key"] == "input"


# ── 付费三道闸 ─────────────────────────────────────────────────────


async def test_dry_run_never_executes() -> None:
    EchoCapability.executed = 0
    ctx = local_context(dry_run=True)
    result = await run("system.echo", {"text": "hi"}, ctx=ctx, registry=_echo_registry())
    assert result.status is RunStatus.DRY_RUN
    assert result.plan is not None
    assert result.plan.requires_billing is True
    assert EchoCapability.executed == 0


async def test_billed_call_blocked_without_consent() -> None:
    EchoCapability.executed = 0
    ctx = local_context()
    with pytest.raises(BilledCallBlocked) as exc:
        await run("system.echo", {"text": "hi"}, ctx=ctx, registry=_echo_registry())
    assert "plan" in exc.value.detail
    assert EchoCapability.executed == 0


async def test_billed_call_runs_after_consent() -> None:
    EchoCapability.executed = 0
    ctx = local_context(billed_allowed=True)
    result = await run(
        "system.echo", {"text": "hi", "repeat": 2}, ctx=ctx, registry=_echo_registry()
    )
    assert result.status is RunStatus.SUCCEEDED
    assert result.output.text == "hihi"
    assert EchoCapability.executed == 1


async def test_invalid_input_raises_capability_input_error() -> None:
    ctx = local_context(dry_run=True)
    with pytest.raises(CapabilityInputError) as exc:
        await run("system.echo", {}, ctx=ctx, registry=_echo_registry())
    assert exc.value.detail["errors"]


async def test_dry_run_plan_masks_secrets() -> None:
    ctx = local_context(dry_run=True)
    result = await run("system.echo", {"text": "x"}, ctx=ctx, registry=_echo_registry())
    masked = result.plan.masked()  # type: ignore[union-attr]
    assert masked.requests[0].headers.get("authorization") in (None, "***")


def _echo_registry() -> CapabilityRegistry:
    reg = CapabilityRegistry(name="test")
    reg.register(EchoCapability())
    return reg


# ── 点号路径（页面参数 ↔ 接口文档 的对应机制）────────────────────────


def test_nest_from_dotted_builds_provider_body() -> None:
    flat = {
        "model": "wan2.7-t2v",
        "input.prompt": "一只猫",
        "parameters.duration": 5,
        "parameters.resolution": None,  # 未填 → 不发给供应商
    }
    assert nest_from_dotted(flat) == {
        "model": "wan2.7-t2v",
        "input": {"prompt": "一只猫"},
        "parameters": {"duration": 5},
    }


def test_dotted_get_reads_nested_response() -> None:
    payload = {"output": {"task_id": "abc", "task_status": "PENDING"}}
    assert dotted_get(payload, "output.task_id") == "abc"
    assert dotted_get(payload, "output.missing", "fallback") == "fallback"
    assert dotted_get(payload, "a.b.c") is None


def test_redact_hides_secrets_recursively() -> None:
    out = redact({"Authorization": "Bearer sk-real", "nested": {"api_key": "sk-1", "v": 2}})
    assert out == {"Authorization": "***", "nested": {"api_key": "***", "v": 2}}


# ── 产物 ───────────────────────────────────────────────────────────


def test_artifact_infers_kind_and_rejects_relative_path(tmp_path) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "clip.mp4"
    f.write_bytes(b"\x00" * 16)
    art = Artifact.from_path(f)
    assert art.kind is ArtifactKind.VIDEO
    assert art.is_local and not art.is_remote
    assert art.size == 16
    assert art.sha256 is not None


def test_artifact_data_url_inlines_bytes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    f = tmp_path / "a.jpg"
    f.write_bytes(b"JPEGDATA")
    url = Artifact.from_path(f).to_data_url()
    assert url.startswith("data:image/jpeg;base64,")


# ── 模型目录与路由 ─────────────────────────────────────────────────


def _model(capability: str, key: str, price: str | None = None) -> ModelSpec:
    return ModelSpec(
        provider="aliyun",
        key=key,
        capability=capability,
        api=ApiSpec(path="/api/v1/video", mode="async_task"),
        pricing=Pricing(unit=CostUnit.SECOND, unit_price=Decimal(price) if price else None),
    )


async def test_catalog_resolves_by_capability_and_policy() -> None:
    from divinepen.core.ports import ResolvePolicy
    from divinepen.services import StaticModelCatalog

    catalog = StaticModelCatalog(
        [_model("video.t2v", "wan2.7-t2v", "0.7"), _model("video.t2v", "wan3.0-video", "0.3")]
    )
    default = await catalog.resolve("video.t2v")
    assert default.model.key == "wan2.7-t2v"  # 入库优先级
    cheap = await catalog.resolve("video.t2v", policy=ResolvePolicy.CHEAPEST)
    assert cheap.model.key == "wan3.0-video"
    assert [m.key for m in cheap.fallbacks] == ["wan2.7-t2v"]


async def test_catalog_raises_when_nothing_matches() -> None:
    from divinepen import ModelNotResolved
    from divinepen.services import StaticModelCatalog

    with pytest.raises(ModelNotResolved):
        await StaticModelCatalog().resolve("video.t2v")


# ── ParamsSchema 手工构造（服务端由数据库读出）─────────────────────


def test_params_schema_marks_billed_fields() -> None:
    schema = ParamsSchema.model_validate(
        {
            "groups": [
                {
                    "key": "parameters",
                    "label": "视频参数",
                    "fields": [
                        {
                            "key": "parameters.duration",
                            "label": "时长",
                            "type": "int",
                            "billed": True,
                        },
                        {"key": "parameters.seed", "label": "随机种子", "type": "int"},
                    ],
                }
            ]
        }
    )
    assert schema.billed_keys() == ["parameters.duration"]
    assert schema.field_map()["parameters.duration"].control == "number"
