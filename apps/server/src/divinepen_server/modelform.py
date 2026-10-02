"""表单 ⇄ ModelSpec：模型管理里「增 / 改」的唯一入口。

页面上的模型表单不是给某个能力定制的表，而是一份 ``dp_model`` 的编辑面：

    元信息（谁家的、叫什么、怎么发） + 参数 schema（能填什么） + 计价（怎么算钱）

参数 schema 用**行编辑器**，不用 JSON 文本域。理由不是"JSON 太难"，而是
参数类型总共十四种、每种对应一个控件（见 ``FieldType``）——把「类型」做成一个
下拉之后，行编辑器比手写 ``{"type": "enum", "options": [...]}`` 少犯一整类错，
而且它和参数控件体系共用同一张类型表：这里加一行，调用页那边自动多一种控件。

**身份不可改**：``id = provider:key``。改了它就是另一个模型 —— 任务记录、
计费口径、以及"这个能力的默认模型"全挂在它身上。要换名字请删了重建，
页面不提供改名，这是刻意留的窄口。
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from divinepen.core.manifest import CostUnit
from divinepen.core.specs import (
    ApiSpec,
    FieldSpec,
    FieldType,
    GroupSpec,
    ModelSpec,
    Option,
    ParamsSchema,
    PollSpec,
    Pricing,
)

# ── 下拉选项：这里的 value 全部是领域里的枚举值，不是给页面看的字符串 ──────────

PARAM_TYPES: list[tuple[str, str]] = [
    ("text", "单行文本"),
    ("textarea", "多行文本"),
    ("int", "整数"),
    ("float", "小数"),
    ("range", "滑杆"),
    ("bool", "开关"),
    ("enum", "单选（下拉）"),
    ("multi_enum", "多选（复选）"),
    ("combo", "可填可选的组合框"),
    ("url", "URL"),
    ("json", "JSON"),
    ("color", "颜色"),
    ("artifact", "单个产物（图/音/视）"),
    ("artifact_list", "多个产物"),
]
PARAM_TYPE_MAP = {value: value for value, _ in PARAM_TYPES}
MULTI_VALUE_TYPES = {"enum", "multi_enum", "combo"}
#: 枚举类之外的类型没有"可选值"，行编辑器里那一列要禁用
TRANSFORMS = {
    "http": "HTTP",
    "websocket": "WebSocket",
}
TRANSPORTS = [("http", "HTTP"), ("websocket", "WebSocket（一期只登记）")]
MODES = [("sync", "同步"), ("async_task", "异步任务（提交后轮询）")]
COST_UNITS = [
    ("free", "免费"),
    ("request", "按次"),
    ("token", "按 token"),
    ("second", "按秒"),
    ("image", "按张"),
    ("character", "按字符"),
]
#: 参数分组：key 是请求体里的层级（与文档一致），label 是页面上的分块标题
KNOWN_GROUPS = {
    "input": "输入",
    "parameters": "参数",
    "messages": "消息",
    "options": "选项",
    "output": "输出",
}

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*(\[\d+\])?)*$")


class FormError(ValueError):
    """字段级错误，页面按字段名原地回显，不弹一个笼统的"保存失败"。"""

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__("；".join(f"{k}：{v}" for k, v in errors.items()))


def _txt(form: Any, name: str, default: str = "") -> str:
    value = form.get(name)
    return str(value).strip() if value is not None else default


def _has(form: Any, name: str) -> bool:
    """复选框：HTML 不勾选时整个字段都不出现，所以判"在不在"而不是判值。"""
    return name in form


def _json_field(form: Any, name: str, errors: dict[str, str], default: Any) -> Any:
    raw = _txt(form, name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        errors[name] = f"不是合法 JSON：{exc.msg}（第 {exc.lineno} 行）"
        return default


def parse_options(raw: str) -> list[Option]:
    """一列可选值，一行一个。``北京=beijing`` 给出「显示名 = 提交值」，
    只写一个词时两者相同 —— 多数文档只给提交值，页面照样能读。"""
    options: list[Option] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "=" in line:
            label, _, value = line.partition("=")
            options.append(Option(label=label.strip() or value.strip(), value=value.strip()))
        else:
            options.append(Option(label=line, value=line))
    return options


def _cast(raw: str, ftype: str) -> Any:
    raw = raw.strip()
    if raw == "":
        return None
    if ftype == "int":
        return int(raw)
    if ftype in ("float", "range"):
        return float(raw)
    if ftype == "bool":
        return raw.lower() in ("1", "true", "yes", "on", "是", "真")
    return raw


#: 参数行的每一列都是一个字段名 + 行号（p_key.3 = 第 3 行的参数路径）
_PARAM_COLS = ("p_key", "p_label", "p_type", "p_group", "p_default", "p_options", "p_desc")
_PARAM_TICKS = ("p_required", "p_billed", "p_advanced")


def param_row_indices(form: Any) -> list[int]:
    """参数行有哪些行号，按行号升序。

    行号写在**字段名**里，而不是靠提交顺序：复选框不勾选压根不提交，
    用平行数组对齐的话，第二行勾了、第一行没勾，"勾"就会滑到第一行头上。
    行号既定，删掉中间一行也只留个空号，谁也不受影响。
    """
    found: set[int] = set()
    for name in form:
        head, _, tail = name.partition(".")
        if head in _PARAM_COLS and tail.isdigit():
            found.add(int(tail))
    return sorted(found)


def parse_params(form: Any, errors: dict[str, str]) -> ParamsSchema:
    """把行编辑器还原成 ParamsSchema。行号序即组内字段序。"""
    seen: dict[str, int] = {}
    buckets: dict[str, list[FieldSpec]] = {}
    for i in param_row_indices(form):
        key = _txt(form, f"p_key.{i}")
        if not key:
            continue  # 行编辑器永远留一个空行给"再加一个"，空行按没写处理
        if key in seen:
            # 不写"第 N 行"：行号是字段名里的机器编号，删过行就会有空洞，
            # 换算成屏幕上的第几行反而会指错。
            errors[f"p_key.{i}"] = f"参数路径 {key} 在这一行重复了；同一个模型里必须唯一"
            continue
        if not _KEY_RE.match(key):
            errors[f"p_key.{i}"] = "点号路径写错了：形如 input.prompt、parameters.duration"
            continue
        ftype = _txt(form, f"p_type.{i}") or "text"
        if ftype not in PARAM_TYPE_MAP:
            errors[f"p_type.{i}"] = f"未知类型 {ftype}"
            continue
        raw_default = _txt(form, f"p_default.{i}")
        try:
            default = _cast(raw_default, ftype)
        except ValueError:
            errors[f"p_default.{i}"] = f"默认值 '{raw_default}' 不是合法的 {ftype}"
            default = None
        options = parse_options(_txt(form, f"p_options.{i}"))
        if ftype in MULTI_VALUE_TYPES and ftype != "combo" and not options:
            errors[f"p_options.{i}"] = "单选 / 多选必须给出可选值（一行一个）"
        # 分组留空就按参数路径的第一段分 —— 请求体的层级本来就在路径里写着
        # （parameters.resolution 属于 parameters）。让人再手填一遍分组，
        # 两个地方迟早对不上，而对不上时页面会把参数分到错的分块里去。
        # 这一列留着是为了能"覆盖"，不是为了能"填"。
        group_key = _txt(form, f"p_group.{i}") or (key.split(".")[0] if "." in key else "input")
        seen[key] = i
        buckets.setdefault(group_key, []).append(
            FieldSpec(
                key=key,
                label=_txt(form, f"p_label.{i}") or key.split(".")[-1],
                type=FieldType(ftype),
                required=f"p_required.{i}" in form,
                default=default,
                description=_txt(form, f"p_desc.{i}"),
                options=options,
                billed=f"p_billed.{i}" in form,
                advanced=f"p_advanced.{i}" in form,
            )
        )
    return ParamsSchema(
        groups=[
            GroupSpec(key=g, label=KNOWN_GROUPS.get(g, g), fields=fields)
            for g, fields in buckets.items()
        ]
    )


def build_spec(form: Any, *, existing: dict[str, Any] | None = None) -> ModelSpec:
    """表单 → ModelSpec。``existing`` 非空表示这是一次编辑。"""
    errors: dict[str, str] = {}
    provider = _txt(form, "provider")
    key = _txt(form, "key")
    capability = _txt(form, "capability")
    if not provider:
        errors["provider"] = "要选一个供应商"
    if not key:
        errors["key"] = "填供应商侧的 API 模型名，例如 wan2.7-t2v-2026-06-12"
    elif not re.match(r"^[A-Za-z0-9][A-Za-z0-9._\-:/]{0,120}$", key):
        errors["key"] = "模型名里不要有空格或中文；用文档里逐字给出的那个字符串"
    if not capability:
        errors["capability"] = "填能力分类，例如 image.t2i、video.r2v；没有合适的就新建一个"
    path = _txt(form, "path")
    if not path:
        errors["path"] = "填相对供应商 Base URL 的路径（或以 http 开头的绝对地址）"

    mode = _txt(form, "mode") or "sync"
    if mode not in ("sync", "async_task"):
        errors["mode"] = f"未知模式 {mode}"
    transport = _txt(form, "transport") or "http"
    if transport not in TRANSFORMS:
        errors["transport"] = f"未知传输方式 {transport}"

    headers = _json_field(form, "headers", errors, {})
    if not isinstance(headers, dict):
        errors["headers"] = "请求头应该是一个 JSON 对象"
        headers = {}

    poll = None
    if mode == "async_task":

        def _num(name: str, fallback: float) -> float:
            raw = _txt(form, name)
            if not raw:
                return fallback
            try:
                return float(raw)
            except ValueError:
                errors[name] = f"'{raw}' 不是数字"
                return fallback

        sep = re.compile(r"[,，\s]+")
        poll = PollSpec(
            path=_txt(form, "poll_path") or "/api/v1/tasks/{task_id}",
            method=_txt(form, "poll_method") or "GET",
            interval_seconds=_num("poll_interval", 5.0),
            timeout_seconds=_num("poll_timeout", 900.0),
            task_id_path=_txt(form, "poll_task_id_path") or "output.task_id",
            status_path=_txt(form, "poll_status_path") or "output.task_status",
            succeeded=[s for s in sep.split(_txt(form, "poll_succeeded")) if s] or ["SUCCEEDED"],
            failed=[s for s in sep.split(_txt(form, "poll_failed")) if s] or ["FAILED"],
        )

    params = parse_params(form, errors)

    unit = _txt(form, "pricing_unit") or "free"
    if unit not in {value for value, _ in COST_UNITS}:
        errors["pricing_unit"] = f"未知计价单位 {unit}"
        unit = "free"
    price_raw = _txt(form, "pricing_unit_price")
    unit_price: Decimal | None = None
    if price_raw:
        try:
            unit_price = Decimal(price_raw)
        except InvalidOperation:
            errors["pricing_unit_price"] = f"'{price_raw}' 不是金额"
    if unit != "free" and unit_price is None:
        errors["pricing_unit_price"] = "选了计费单位就必须填单价（预估要用它算钱）"

    if errors:
        raise FormError(errors)

    # 元信息里这三项不是用户填的：核对状态是"我确认过参数没抄错"的印记，
    # 页面改一次参数就自动退回未核对；优先级和来源文档跟着记录走。
    base = existing or {}
    verified = bool(base.get("verified"))
    if (
        existing
        and verified
        and _params_signature(params) != _params_signature(existing.get("params_obj"))
    ):
        # 只有参数真的变了才退回「待核对」。改个备注、改个备注里的错别字，
        # 不该把"我逐字对过文档"这个印记抹掉 —— 印记乱掉就等于没有印记。
        verified = False
    return ModelSpec(
        provider=provider,
        key=key,
        capability=capability,
        name=_txt(form, "name") or key,
        enabled=_has(form, "enabled"),
        api=ApiSpec(
            method=_txt(form, "method") or "POST", path=path, headers=headers, mode=mode, poll=poll
        ),
        params=params,
        defaults=dict(existing.get("defaults_obj") or {}) if existing else {},
        pricing=Pricing(
            unit=CostUnit(unit),
            unit_price=unit_price,
            currency=_txt(form, "pricing_currency") or "CNY",
            notes=_txt(form, "pricing_notes"),
        ),
        notes=_txt(form, "notes"),
        doc_url=_txt(form, "doc_url"),
        meta={
            "transport": transport,
            # 来源文档是"这个模型的参数是从哪份文档抄来的"，改一次表单不该
            # 把它冲掉；只有页面手工新建的模型才没有来源文档。
            "source_doc": base.get("source_doc") or "手工录入",
            "verified": verified,
            "priority": int(base.get("priority") or 100),
        },
    )


def _params_signature(params: Any) -> str:
    """参数的规范化指纹：字段顺序、数字与字符串的写法都归一，只比"内容"。"""
    if params is None:
        return "{}"
    if hasattr(params, "model_dump"):
        params = params.model_dump(mode="json")
    return json.dumps(params, sort_keys=True, ensure_ascii=False, default=str)


def spec_to_form(spec: dict[str, Any]) -> dict[str, Any]:
    """dp_model 的一行 → 表单初值，供编辑页回填。"""
    poll = spec.get("poll_obj") or {}
    pricing = spec.get("pricing_obj") or {}
    params = spec.get("params_obj") or {"groups": []}
    rows: list[dict[str, Any]] = []
    for i, (group, field) in enumerate(
        (g, f) for g in params.get("groups", []) for f in g.get("fields", [])
    ):
        rows.append(
            {
                "i": i,
                "key": field.get("key", ""),
                "label": field.get("label", ""),
                "type": field.get("type", "text"),
                "group": group.get("key", "input"),
                "required": bool(field.get("required")),
                "billed": bool(field.get("billed")),
                "advanced": bool(field.get("advanced")),
                "default": _default_text(field.get("default")),
                "options": "\n".join(
                    f"{o.get('label')}={o.get('value')}"
                    if o.get("label") != o.get("value")
                    else str(o.get("value"))
                    for o in field.get("options", [])
                ),
                "desc": field.get("description", ""),
            }
        )
    return {
        "provider": spec.get("provider", ""),
        "key": spec.get("key", ""),
        "name": spec.get("name", ""),
        "capability": spec.get("capability", ""),
        "enabled": bool(spec.get("enabled")),
        "method": spec.get("method", "POST"),
        "path": spec.get("path", ""),
        "mode": spec.get("mode", "sync"),
        "transport": spec.get("transport", "http"),
        "headers": json.dumps(spec.get("headers_obj") or {}, ensure_ascii=False, indent=2)
        if spec.get("headers_obj")
        else "",
        "poll": poll,
        "pricing_unit": pricing.get("unit", "free"),
        "pricing_unit_price": _default_text(pricing.get("unit_price")),
        "pricing_currency": pricing.get("currency", "CNY"),
        "pricing_notes": pricing.get("notes", ""),
        "notes": spec.get("notes", ""),
        "doc_url": spec.get("doc_url", ""),
        "rows": rows or [_blank_row()],
        "verified": bool(spec.get("verified")),
    }


FORM_FIELDS = (
    "provider",
    "key",
    "name",
    "capability",
    "method",
    "path",
    "mode",
    "transport",
    "headers",
    "pricing_unit",
    "pricing_unit_price",
    "pricing_currency",
    "pricing_notes",
    "notes",
    "doc_url",
    "poll_path",
    "poll_method",
    "poll_interval",
    "poll_timeout",
    "poll_task_id_path",
    "poll_status_path",
    "poll_succeeded",
    "poll_failed",
)


def echo_form(form: Any) -> dict[str, Any]:
    """校验失败时把用户刚填的东西原样还回去。

    重填一遍是最没道理的惩罚：错的是某一格，不该赔上整张表。
    """
    rows = [
        {
            "i": i,
            "key": _txt(form, f"p_key.{i}"),
            "label": _txt(form, f"p_label.{i}"),
            "type": _txt(form, f"p_type.{i}") or "text",
            "group": _txt(form, f"p_group.{i}"),
            "required": f"p_required.{i}" in form,
            "billed": f"p_billed.{i}" in form,
            "advanced": f"p_advanced.{i}" in form,
            "default": _txt(form, f"p_default.{i}"),
            "options": _txt(form, f"p_options.{i}"),
            "desc": _txt(form, f"p_desc.{i}"),
        }
        for i in param_row_indices(form)
    ]
    state = {name: _txt(form, name) for name in FORM_FIELDS}
    state["rows"] = rows or [_blank_row()]
    state["enabled"] = _has(form, "enabled")
    # 回显也要给出轮询契约那一块，否则"填错了再改"会在轮询字段上崩掉，
    # 而人恰恰是在填轮询的时候最容易填错。
    state["poll"] = {
        "path": _txt(form, "poll_path"),
        "method": _txt(form, "poll_method"),
        "task_id_path": _txt(form, "poll_task_id_path"),
        "status_path": _txt(form, "poll_status_path"),
        "interval_seconds": _txt(form, "poll_interval"),
        "timeout_seconds": _txt(form, "poll_timeout"),
        "succeeded": _split_list(_txt(form, "poll_succeeded")),
        "failed": _split_list(_txt(form, "poll_failed")),
    }
    return state


def _split_list(raw: str) -> list[str]:
    return [part for part in re.split(r"[,，\s]+", raw) if part]


def _blank_row(i: int = 0) -> dict[str, Any]:
    return {
        "i": i,
        "key": "",
        "label": "",
        "type": "text",
        "group": "",
        "required": False,
        "billed": False,
        "advanced": False,
        "default": "",
        "options": "",
        "desc": "",
    }


def _default_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
