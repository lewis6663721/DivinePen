"""点号路径工具。

整个底座用**点号路径**做参数寻址，因为阿里系（以及大多数供应商）的请求体是嵌套的::

    {"model": "...", "input": {"prompt": "..."}, "parameters": {"duration": 5}}

页面上一个控件的 key 就写 ``parameters.duration``，与接口文档逐字对应，
不需要任何额外的映射表。同类工具也用于从响应里取 task_id / 状态 / 产物 URL。
"""

from __future__ import annotations

from typing import Any

_MISSING: Any = object()


def dotted_get(obj: Any, path: str, default: Any = None) -> Any:
    """按点号路径取值，任一层缺失（或不是 dict/list）则返回 default。"""
    if not path:
        return default
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part, _MISSING)
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            cur = cur[idx] if 0 <= idx < len(cur) else _MISSING
        else:
            return default
        if cur is _MISSING:
            return default
    return cur


def dotted_set(obj: dict[str, Any], path: str, value: Any) -> None:
    """按点号路径写入，中间层不存在则创建 dict。"""
    parts = path.split(".")
    cur = obj
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def nest_from_dotted(flat: dict[str, Any]) -> dict[str, Any]:
    """``{"parameters.duration": 5}`` → ``{"parameters": {"duration": 5}}``。

    值本身是 None 的键会被丢弃（未填写的可选参数不发给供应商）。
    """
    out: dict[str, Any] = {}
    for key, value in flat.items():
        if value is None:
            continue
        dotted_set(out, key, value)
    return out


def flatten_to_dotted(nested: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """:func:`nest_from_dotted` 的逆操作。"""
    out: dict[str, Any] = {}
    for key, value in nested.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            out.update(flatten_to_dotted(value, path))
        else:
            out[path] = value
    return out


def redact(
    value: Any, *, keys: tuple[str, ...] = ("api_key", "apikey", "authorization", "token")
) -> Any:
    """递归脱敏：命中的键替换为掩码。日志与接口响应统一走这里。"""
    if isinstance(value, dict):
        return {
            k: ("***" if any(s in k.lower() for s in keys) else redact(v, keys=keys))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, keys=keys) for v in value]
    return value
