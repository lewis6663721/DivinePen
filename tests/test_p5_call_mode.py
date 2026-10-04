"""调用方式契约：文档说只能异步，就不能按同步发。

这个文件的由来是一次真实的 403：

    用户提交 文生图 wan2.7-image-pro，页面一切正常，供应商却回
    ``AccessDenied current user api does not support synchronous calls``。

根因不是参数抄错，而是**目录把"怎么调用"这件写错了**：我把万相图像族
标成了 ``mode="sync"``（还配了一句"同步返回图像 URL"的注释），而文档
（万象图像生成2.7 / 2.6 两份都写着）原话是：

    X-DashScope-Async（必选）… 缺少此请求头将报错：
    "current user api does not support synchronous calls"

参数对不对，有 ``test_p5_body_contract.py`` 守着；**怎么发**对不对，
由这个文件守。两者的共同教训是：目录里的每一句断言都得能指到文档原文，
指不到的地方就是下一次事故的种子。
"""

from __future__ import annotations

from typing import Any

from divinepen.providers.aliyun import pick_artifacts, pick_image_artifacts
from divinepen_server.catalog import _models

#: 文档明确"HTTP 只支持异步"的模型族 —— 必须带 X-DashScope-Async 提交。
#: 来源：万象图像生成2.7.md / 万象图像生成2.6.md / 各视频文档。
#: （happyhorse 那几个没读文档，先不写进来 —— 宁可漏也不瞎断言。）
ASYNC_ONLY = {
    "wan2.7-image-pro",
    "wan2.7-image",
    "wan2.6-image",
    "wan2.7-t2v",
    "wan2.7-i2v-2026-04-25",
    "wan2.7-r2v",
    "wan2.7-videoedit",
    "wan3.0-video",
}


def _model(key: str):
    return next(m for m in _models() if m.key == key)


def test_async_only_family_is_marked_async() -> None:
    """万相图像 / 视频族一个都不能留在 sync —— 403 就是这么来的。"""
    bad = [k for k in sorted(ASYNC_ONLY) if not _model(k).is_async]
    assert not bad, f"这些模型的文档只支持异步，目录却标了 sync：{bad}"


def test_async_models_carry_the_required_header() -> None:
    """异步是靠**请求头**说的，不是靠请求体。"""
    for key in sorted(ASYNC_ONLY):
        model = _model(key)
        assert model.api.headers.get("X-DashScope-Async") == "enable", key
        assert model.api.poll is not None, f"{key} 标了异步却没有轮询契约"


def test_async_flag_never_leaks_into_the_body() -> None:
    """X-DashScope-Async 是头，绝不能同时被声明成一个参数。"""
    for key in sorted(ASYNC_ONLY):
        model = _model(key)
        declared = {f.key for f in model.params.fields()}
        assert not any("DashScope" in k for k in declared), f"{key}: {declared}"


def test_image_async_response_keeps_every_image() -> None:
    """``n>1`` 时产物在 content 数组里，取下标 0 会悄悄丢图。"""
    payload: dict[str, Any] = {
        "output": {
            "task_id": "a81ee7cb",
            "task_status": "SUCCEEDED",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"image": "https://oss/a.png?Expires=1", "type": "image"},
                            {"image": "https://oss/b.png?Expires=2", "type": "image"},
                        ],
                    },
                }
            ],
        },
        "usage": {"image_count": 2},
    }
    got = pick_artifacts(_model("wan2.7-image-pro"), payload)
    assert [a.uri for a in got] == ["https://oss/a.png?Expires=1", "https://oss/b.png?Expires=2"]


def test_legacy_image_shape_still_parses() -> None:
    """老万相 t2i 的 ``output.results[].url`` 形状也要兜住。"""
    got = pick_image_artifacts({"output": {"results": [{"url": "https://oss/old.png"}]}})
    assert [a.uri for a in got] == ["https://oss/old.png"]
