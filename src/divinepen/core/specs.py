"""模型/参数的契约。

这一层回答一个问题：**一个模型，从"页面上的参数表单"到"发给供应商的请求"，
中间的每一段信息用什么结构表达？**

    ParamsSchema  ── 页面渲染 + 校验（谁在页面上填什么）
        │  字段 key 用点号路径，直接映射请求体嵌套
        ▼
    ApiSpec       ── 怎么发（method / path / headers / 同步 or 异步轮询）
        │
        ▼
    ModelSpec     ── 一个可被 Router 选中的模型（provider + key + capability + 计价）

**Pydantic 是唯一真相源**：`ParamsSchema.from_pydantic()` 从能力的 Input 模型
派生表单，禁止手写 JSON schema。
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .artifact import Artifact
from .manifest import CostUnit


class FieldType(StrEnum):
    TEXT = "text"
    TEXTAREA = "textarea"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"
    ENUM = "enum"
    MULTI_ENUM = "multi_enum"
    URL = "url"
    JSON = "json"
    ARTIFACT = "artifact"
    ARTIFACT_LIST = "artifact_list"

    @property
    def control(self) -> str:
        """映射到页面控件，页面的渲染规则只依赖这一张表。"""
        return {
            FieldType.TEXT: "input",
            FieldType.TEXTAREA: "textarea",
            FieldType.INT: "number",
            FieldType.FLOAT: "number",
            FieldType.BOOL: "switch",
            FieldType.ENUM: "select",
            FieldType.MULTI_ENUM: "checkbox-group",
            FieldType.URL: "url-input",
            FieldType.JSON: "code-editor",
            FieldType.ARTIFACT: "media-picker",
            FieldType.ARTIFACT_LIST: "media-picker-multi",
        }[self]


class Option(BaseModel):
    label: str
    value: str


class FieldSpec(BaseModel):
    """一个可编辑参数。``key`` 是点号路径，与接口文档逐字对应。"""

    key: str
    label: str
    type: FieldType = FieldType.TEXT
    required: bool = False
    default: Any = None
    description: str = Field(default="", description="接口文档原文摘录，页面直接显示")
    placeholder: str = ""
    options: list[Option] = Field(default_factory=list)
    min: float | None = None
    max: float | None = None
    step: float | None = None
    max_length: int | None = None
    pattern: str | None = None
    #: True 表示这个参数直接影响计费，页面必须高亮（resolution / duration / n …）
    billed: bool = False
    #: True 表示收进「高级参数」折叠区，默认不打扰用户
    advanced: bool = False

    @property
    def control(self) -> str:
        return self.type.control


class GroupSpec(BaseModel):
    """参数分组。对应请求体的层级（input / parameters）或语义分块。"""

    key: str
    label: str
    description: str = ""
    collapsed: bool = False
    fields: list[FieldSpec] = Field(default_factory=list)


class ParamsSchema(BaseModel):
    groups: list[GroupSpec] = Field(default_factory=list)

    def fields(self) -> list[FieldSpec]:
        return [f for g in self.groups for f in g.fields]

    def field_map(self) -> dict[str, FieldSpec]:
        return {f.key: f for f in self.fields()}

    def defaults(self) -> dict[str, Any]:
        return {f.key: f.default for f in self.fields() if f.default is not None}

    def billed_keys(self) -> list[str]:
        return [f.key for f in self.fields() if f.billed]

    def validate_values(self, values: dict[str, Any]) -> dict[str, Any]:
        """校验用户提交的扁平值：未知键报错、必填缺失报错、类型/范围越界报错。

        返回补齐默认值后的扁平字典。**绝不静默丢字段**——多出来的键一定是
        前端或文档对不上，必须让调用方看见。
        """
        known = self.field_map()
        unknown = sorted(set(values) - set(known))
        if unknown:
            raise ValueError(f"未知参数：{', '.join(unknown)}")
        out: dict[str, Any] = {}
        missing: list[str] = []
        for key, spec in known.items():
            raw = values.get(key, spec.default)
            if raw is None:
                if spec.required:
                    missing.append(key)
                continue
            out[key] = _coerce(spec, raw)
        if missing:
            raise ValueError(f"缺少必填参数：{', '.join(missing)}")
        return out

    def build_body(self, values: dict[str, Any]) -> dict[str, Any]:
        """扁平值（点号路径）→ 接口文档要求的嵌套请求体。

        **dry-run 与真实调用共用这一份**：页面上看到的 body 就是发出去的 body。
        顺序按 schema 声明顺序，便于和文档逐行对照。
        """
        body: dict[str, Any] = {}
        for field in self.fields():
            if field.key not in values or values[field.key] is None:
                continue
            node = body
            parts = field.key.split(".")
            for part in parts[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node[parts[-1]] = values[field.key]
        return body

    # ── 从 Pydantic 派生（唯一真相源）────────────────────────────

    @classmethod
    def from_pydantic(cls, model: type[BaseModel], *, group_label: str = "参数") -> ParamsSchema:
        """把能力的 Input 模型翻译成表单 schema。

        字段用 ``json_schema_extra`` 声明展示信息::

            prompt: str = Field(json_schema_extra={"group": "input", "label": "提示词",
                                                  "doc": "…", "billed": False})
        """
        groups: dict[str, GroupSpec] = {}
        order: list[str] = []
        for name, info in model.model_fields.items():
            extra = dict(info.json_schema_extra or {})
            group_key = str(extra.get("group") or "main")
            if group_key not in groups:
                groups[group_key] = GroupSpec(
                    key=group_key,
                    label=str(extra.get("group_label") or group_label),
                    description=str(extra.get("group_doc") or ""),
                    collapsed=bool(extra.get("group_collapsed", False)),
                )
                order.append(group_key)
            groups[group_key].fields.append(_field_from(name, info, extra))
        return cls(groups=[groups[k] for k in order])


def _coerce(spec: FieldSpec, raw: Any) -> Any:
    """把页面传来的原始值转成目标类型，并做枚举/范围/长度校验。

    这里**不做静默修复**（不把 "abc" 变成 0，不把越界值截断），一律报错。
    参数错就是错，让用户看见比悄悄改掉安全得多。
    """
    t = spec.type
    try:
        if t is FieldType.INT:
            value: Any = int(raw)
        elif t is FieldType.FLOAT:
            value = float(raw)
        elif t is FieldType.BOOL:
            value = _as_bool(raw)
        elif t is FieldType.ENUM:
            value = str(raw)
            allowed = {o.value for o in spec.options}
            if allowed and value not in allowed:
                raise ValueError(f"只能是 {sorted(allowed)} 之一，收到 {value!r}")
        elif t is FieldType.MULTI_ENUM:
            seq = raw if isinstance(raw, (list, tuple, set)) else [raw]
            allowed = {o.value for o in spec.options}
            value = [str(v) for v in seq]
            bad = [v for v in value if allowed and v not in allowed]
            if bad:
                raise ValueError(f"含非法选项 {bad}，只能是 {sorted(allowed)} 之一")
        elif t in (FieldType.JSON, FieldType.ARTIFACT, FieldType.ARTIFACT_LIST):
            value = raw
        else:
            value = str(raw)
            if spec.max_length is not None and len(value) > spec.max_length:
                raise ValueError(f"长度 {len(value)} 超过上限 {spec.max_length}")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"参数 {spec.key} 取值非法：{exc}") from exc

    if t in (FieldType.INT, FieldType.FLOAT):
        if spec.min is not None and value < spec.min:
            raise ValueError(f"参数 {spec.key} 小于下限 {spec.min}")
        if spec.max is not None and value > spec.max:
            raise ValueError(f"参数 {spec.key} 大于上限 {spec.max}")
    return value


def _as_bool(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off", ""}:
            return False
    if isinstance(raw, (int, float)):
        return bool(raw)
    raise ValueError(f"无法解析为布尔值：{raw!r}")


def _field_from(name: str, info: Any, extra: dict[str, Any]) -> FieldSpec:
    annotation = info.annotation
    ftype, options = _infer_type(annotation)
    return FieldSpec(
        key=str(extra.get("key") or name),
        label=str(extra.get("label") or name),
        type=ftype,
        required=info.is_required(),
        default=None if info.is_required() else extra.get("default", info.default),
        description=str(extra.get("doc") or info.description or ""),
        placeholder=str(extra.get("placeholder") or ""),
        options=options,
        min=extra.get("min"),
        max=extra.get("max"),
        step=extra.get("step"),
        max_length=extra.get("max_length") or _max_length(info),
        pattern=extra.get("pattern"),
        billed=bool(extra.get("billed", False)),
        advanced=bool(extra.get("advanced", False)),
    )


def _infer_type(annotation: Any) -> tuple[FieldType, list[Option]]:
    origin = get_origin(annotation)
    args = get_args(annotation)

    if annotation is Artifact:
        return FieldType.ARTIFACT, []
    if annotation in (list[Artifact], tuple[Artifact, ...]):
        return FieldType.ARTIFACT_LIST, []
    if annotation is bool:
        return FieldType.BOOL, []
    if annotation is int:
        return FieldType.INT, []
    if annotation is float or annotation is Decimal:
        return FieldType.FLOAT, []
    if annotation is dict or annotation is list or origin in (dict, list):
        return FieldType.JSON, []
    if origin is Literal:
        options = [Option(label=str(a), value=str(a)) for a in args]
        return FieldType.ENUM, options
    if origin is list and args and get_origin(args[0]) is Literal:
        options = [Option(label=str(a), value=str(a)) for a in get_args(args[0])]
        return FieldType.MULTI_ENUM, options
    if isinstance(annotation, type) and issubclass(annotation, StrEnum):
        return FieldType.ENUM, [Option(label=m.value, value=m.value) for m in annotation]
    if annotation is str:
        return FieldType.TEXT, []
    # Optional[X] / X | None
    if origin is not None and type(None) in args:
        inner = next(a for a in args if a is not type(None))
        return _infer_type(inner)
    return FieldType.TEXT, []


def _max_length(info: Any) -> int | None:
    for meta in info.metadata:
        limit = getattr(meta, "max_length", None)
        if isinstance(limit, int):
            return limit
    return None


# ── 接口契约 ───────────────────────────────────────────────────────


class TaskStatus(StrEnum):
    """统一的任务状态。远端各家的枚举全部归一到这 6 个值。"""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    #: 可介入工作流停在人工闸口，等 resume 带参数回来
    AWAITING_INPUT = "AWAITING_INPUT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED}

    @property
    def is_active(self) -> bool:
        return self in {TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.AWAITING_INPUT}


class ArtifactPick(BaseModel):
    """从响应里捞一个产物出来。"""

    path: str = Field(description="响应中的点号路径，如 output.video_url")
    kind: str = "other"
    name: str = ""


class PollSpec(BaseModel):
    """异步任务的轮询契约（创建任务 → 轮询取结果）。"""

    path: str = Field(default="/api/v1/tasks/{task_id}", description="支持 {task_id} 占位")
    method: str = "GET"
    interval_seconds: float = 5.0
    timeout_seconds: float = 900.0
    task_id_path: str = "output.task_id"
    status_path: str = "output.task_status"
    succeeded: list[str] = Field(default_factory=lambda: ["SUCCEEDED"])
    failed: list[str] = Field(default_factory=lambda: ["FAILED"])
    canceled: list[str] = Field(default_factory=lambda: ["CANCELED"])
    pending: list[str] = Field(default_factory=lambda: ["PENDING"])
    running: list[str] = Field(default_factory=lambda: ["RUNNING"])
    artifacts: list[ArtifactPick] = Field(default_factory=list)
    usage_path: str | None = "usage"

    def normalize_status(self, raw: str | None) -> TaskStatus:
        value = (raw or "").upper()
        if value in {s.upper() for s in self.succeeded}:
            return TaskStatus.SUCCEEDED
        if value in {s.upper() for s in self.failed}:
            return TaskStatus.FAILED
        if value in {s.upper() for s in self.canceled}:
            return TaskStatus.CANCELED
        if value in {s.upper() for s in self.pending}:
            return TaskStatus.PENDING
        if value in {s.upper() for s in self.running}:
            return TaskStatus.RUNNING
        return TaskStatus.UNKNOWN


class ApiSpec(BaseModel):
    """怎么把一个模型跑起来。"""

    method: str = "POST"
    path: str = Field(description="相对 provider.base_url 的路径，或绝对 URL")
    headers: dict[str, str] = Field(default_factory=dict)
    mode: Literal["sync", "async_task"] = "sync"
    poll: PollSpec | None = None

    @field_validator("method")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


class Pricing(BaseModel):
    """计价口径。金额只用于**预估与展示**，真实账单以远端 usage 为准。"""

    unit: CostUnit = CostUnit.FREE
    unit_price: Decimal | None = None
    currency: str = "CNY"
    notes: str = ""

    @property
    def is_free(self) -> bool:
        return self.unit is CostUnit.FREE or not self.unit_price


class ModelSpec(BaseModel):
    """一个可被 Router 选中的模型。对应数据库表 ``dp_model``。"""

    model_config = ConfigDict(populate_by_name=True)

    provider: str
    key: str = Field(description="供应商侧的 API 模型名，如 wan2.7-t2v-2026-06-12")
    capability: str = Field(description="功能分类，如 video.t2v")
    name: str = ""
    region: str | None = None
    #: 供应商 Base URL。由目录/数据库在解析模型时填入，适配器据此拼绝对 URL。
    base_url: str = ""
    enabled: bool = True
    api: ApiSpec
    params: ParamsSchema = Field(default_factory=ParamsSchema)
    defaults: dict[str, Any] = Field(default_factory=dict)
    pricing: Pricing = Field(default_factory=Pricing)
    notes: str = ""
    doc_url: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.key

    @property
    def is_async(self) -> bool:
        return self.api.mode == "async_task"


class ResolvedModel(BaseModel):
    """Router 的产出：能力 + 具体模型 + 供应商 + 解析理由。"""

    capability: str
    model: ModelSpec
    reason: str = ""
    fallbacks: list[ModelSpec] = Field(default_factory=list)
