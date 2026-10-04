"""阿里云百炼（DashScope）适配器。

职责边界很窄：**只懂"怎么把 ModelSpec + 请求体变成 HTTP 请求、怎么把响应读回来"**。
不知道能力、不知道页面、不知道台账 —— 那些在 ``capabilities/_model.py`` 和
``services/`` 里。这样换供应商时只加一个同形状的模块。

两条从文档里读出来的硬规则：

* 异步任务必须带 ``X-DashScope-Async: enable``，否则 403；
* 提交后立刻拿 ``output.task_id``，轮询 ``GET /api/v1/tasks/{task_id}``。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urljoin

from ..core.artifact import Artifact, ArtifactKind
from ..core.capability import PlannedRequest, RequestKind
from ..core.paths import dotted_get
from ..core.specs import ModelSpec, TaskStatus


def absolute_url(model: ModelSpec, path: str) -> str:
    if path.startswith("http"):
        return path
    base = (model.base_url or "").rstrip("/")
    return urljoin(base + "/", path.lstrip("/"))


def submit_request(model: ModelSpec, body: dict[str, Any], *, api_key: str) -> PlannedRequest:
    """提交请求。body 里补 ``model`` 字段 —— 阿里系所有端点都要求它。"""
    payload = {"model": model.key, **body}
    headers = dict(model.api.headers)
    headers["Authorization"] = f"Bearer {api_key}"
    return PlannedRequest(
        kind=RequestKind.HTTP,
        method=model.api.method,
        url=absolute_url(model, model.api.path),
        headers=headers,
        body=payload,
        note="异步任务需 X-DashScope-Async: enable" if model.is_async else "同步返回",
    )


def poll_request(model: ModelSpec, task_id: str, *, api_key: str) -> PlannedRequest:
    poll = model.api.poll
    if poll is None:
        raise ValueError(f"模型 {model.key} 没有轮询配置")
    return PlannedRequest(
        kind=RequestKind.HTTP,
        method=poll.method,
        url=absolute_url(model, poll.path.format(task_id=task_id)),
        headers={"Authorization": f"Bearer {api_key}"},
        note="轮询异步任务状态",
    )


def cancel_request(model: ModelSpec, task_id: str, *, api_key: str) -> PlannedRequest:
    """取消一个远端异步任务。

    端点就挂在轮询路径下面（``/api/v1/tasks/{task_id}/cancel``），见文档
    「管理异步任务」。取消本身不计费；但**只有 PENDING/RUNNING 才取消得掉**，
    已完成的会返回失败，调用方按 best-effort 处理即可。
    """
    poll = model.api.poll
    if poll is None:
        raise ValueError(f"模型 {model.key} 没有轮询配置，不存在可取消的远端任务")
    path = poll.path.replace("{task_id}", task_id).rstrip("/") + "/cancel"
    return PlannedRequest(
        kind=RequestKind.HTTP,
        method="POST",
        url=absolute_url(model, path),
        headers={"Authorization": f"Bearer {api_key}"},
        note="取消远端异步任务",
    )


def parse_submit(model: ModelSpec, payload: Any) -> dict[str, Any]:
    """提交响应 → {task_id?, output, usage, error}。"""
    payload = payload if isinstance(payload, dict) else {}
    return {
        "task_id": dotted_get(
            payload, (model.api.poll.task_id_path if model.api.poll else "output.task_id")
        ),
        "output": payload.get("output") or {},
        "usage": payload.get("usage") or {},
        "request_id": payload.get("request_id"),
        "error": _error_of(payload),
    }


def parse_poll(model: ModelSpec, payload: Any) -> dict[str, Any]:
    poll = model.api.poll
    payload = payload if isinstance(payload, dict) else {}
    raw_status = dotted_get(payload, poll.status_path if poll else "output.task_status")
    return {
        "status": (poll or _DEFAULT_POLL).normalize_status(str(raw_status or "")),
        "output": payload.get("output") or {},
        "usage": payload.get("usage") or {},
        "error": _error_of(payload),
    }


def pick_artifacts(model: ModelSpec, payload: Any) -> list[Artifact]:
    """按 PollSpec.artifacts 的点号路径从响应里捞产物（远程 URL，不下载）。"""
    poll = model.api.poll
    if poll is None:
        return []
    out: list[Artifact] = []
    for pick in poll.artifacts:
        value = dotted_get(payload, pick.path)
        urls = value if isinstance(value, list) else ([value] if value else [])
        for index, url in enumerate(urls):
            if isinstance(url, dict):
                url = url.get("url") or url.get("video_url") or url.get("image_url")
            if not isinstance(url, str) or not url.startswith(("http", "data:")):
                continue
            out.append(
                Artifact(
                    uri=url,
                    kind=_kind(pick.kind),
                    name=pick.name or f"{pick.path.split('.')[-1]}-{index}",
                )
            )
    return out


def pick_image_artifacts(payload: Any) -> list[Artifact]:
    """同步图像响应：``output.results[].url`` 或 ``output.choices[].message.content[].image``。"""
    payload = payload if isinstance(payload, dict) else {}
    output = payload.get("output") or {}
    urls: list[str] = []
    for item in output.get("results") or []:
        if isinstance(item, dict) and item.get("url"):
            urls.append(item["url"])
    for choice in output.get("choices") or []:
        content = ((choice or {}).get("message") or {}).get("content") or []
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("image"):
                urls.append(part["image"])
    return [Artifact(uri=u, kind=ArtifactKind.IMAGE, name=f"image-{i}") for i, u in enumerate(urls)]


def quantity_of(model: ModelSpec, payload: Any, artifacts: list[Artifact]) -> float:
    """按计价口径从响应 usage 里取数量。取不到就 0 —— 不编。"""
    usage = (payload or {}).get("usage") if isinstance(payload, dict) else None
    usage = usage or {}
    unit = model.pricing.unit.value
    if unit == "second":
        return float(usage.get("duration") or usage.get("video_duration") or 0)
    if unit == "image":
        return float(usage.get("image_count") or len(artifacts) or 0)
    if unit == "token":
        return float(usage.get("total_tokens") or 0)
    if unit == "character":
        return float(usage.get("characters") or 0)
    return 0.0


def _error_of(payload: dict[str, Any]) -> str:
    code = payload.get("code")
    message = payload.get("message")
    if code or message:
        return f"{code or ''} {message or ''}".strip()
    output = payload.get("output") or {}
    if isinstance(output, dict):
        return str(output.get("message") or output.get("code") or "")
    return ""


def _kind(value: str) -> ArtifactKind:
    try:
        return ArtifactKind(value)
    except ValueError:
        return ArtifactKind.OTHER


class _DefaultPoll:
    @staticmethod
    def normalize_status(raw: str) -> TaskStatus:
        try:
            return TaskStatus((raw or "").upper())
        except ValueError:
            return TaskStatus.UNKNOWN


_DEFAULT_POLL = _DefaultPoll()
