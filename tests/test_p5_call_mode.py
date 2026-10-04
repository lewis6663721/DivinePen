"""调用方式契约：文档怎么规定发，就只能怎么发。

这个文件的由来是一次真实的 403：

    提交 文生图 wan2.7-image-pro，页面一切正常，供应商却回
    AccessDenied current user api does not support synchronous calls

参数对不对，有 ``test_p5_body_contract.py`` 守着；**怎么发**对不对由这个文件守。

根因说清楚，因为第一版的解释是错的：万象图像族**不是**"只支持异步"，
而是**同步与异步是两个不同的端点** ——

* 同步：``POST …/services/aigc/multimodal-generation/generation``
* 异步：``POST …/services/aigc/image-generation/generation``（该端点只受理异步，
  缺 ``X-DashScope-Async: enable`` 就返回上面那句报错）

我们当时恰好把 ``mode="sync"`` 配到了**异步专用端点**上。所以本文件里最要紧的
不是"图像都要异步"，而是 ``test_sync_must_not_use_the_async_only_endpoint``
（第 1 条）—— 它守的是**模式与端点必须自洽**这个不变量，正是当初缺的那条。
"""

from __future__ import annotations

from typing import Any

from divinepen.providers.aliyun import pick_artifacts, pick_image_artifacts, quantity_of
from divinepen_server.catalog import ASYNC_POLL, IMAGE_PATH, _models

#: 你给的 15 份文档里明确"异步需带 X-DashScope-Async: enable 且走独立异步端点"的模型。
#: 来源：万象图像生成2.7.md、万象图像生成2.6.md、千问图像生成.md、各视频文档。
DOC_ASYNC = {
    "qwen-image-3.0",
    "qwen-image-3.0-pro",
    "wan2.7-image-pro",
    "wan2.7-image",
    "wan2.6-image",
    "wan2.7-t2v",
    "wan2.7-i2v-2026-04-25",
    "wan2.7-r2v",
    "wan2.7-videoedit",
    "wan3.0-video",
}

#: 文档里查不到、调用方式无从核对的模型 —— 不许假装核对过。
DOC_MISSING = {"z-image-turbo"}


def _model(key: str):
    return next(m for m in _models() if m.key == key)


def _artifacts(model: Any, payload: dict[str, Any]) -> list[Any]:
    """异步任务成功后取产物：与 ``_model.py`` 里那条真实路径一致。"""
    return pick_artifacts(model, payload) or pick_image_artifacts(payload)


# ── 1. 模式与端点必须自洽（这条才是 403 的根因） ────────────────────


def test_sync_must_not_use_the_async_only_endpoint() -> None:
    """同步调用不许打在只受理异步的端点上 —— 就是这一步把 403 招来的。"""
    bad = [
        m.key
        for m in _models()
        if m.api.mode == "sync" and m.api.path == IMAGE_PATH and m.api.method == "POST"
    ]
    assert not bad, f"这些模型是同步模式，却指向异步专用端点 {IMAGE_PATH}：{bad}"


def test_async_models_declare_a_poll_contract() -> None:
    """标了异步却没有轮询契约，任务就永远停在 RUNNING。"""
    bad = [m.key for m in _models() if m.is_async and m.api.poll is None]
    assert not bad, bad


# ── 2. 文档要求异步的族，一个都不能留在 sync ──────────────────────


def test_documented_async_family_is_async() -> None:
    missing = {k for k in DOC_ASYNC if k not in {m.key for m in _models()}}
    assert not missing, f"测试引用了不存在的模型键：{missing}"
    bad = [k for k in sorted(DOC_ASYNC) if not _model(k).is_async]
    assert not bad, f"文档要求异步，目录却标了 sync：{bad}"


def test_async_models_carry_the_required_header() -> None:
    """异步是靠**请求头**说的，不是靠请求体。"""
    for key in sorted(DOC_ASYNC):
        assert _model(key).api.headers.get("X-DashScope-Async") == "enable", key


def test_async_flag_never_leaks_into_the_body() -> None:
    """X-DashScope-Async 是头，绝不能同时被声明成一个参数。"""
    for key in sorted(DOC_ASYNC):
        declared = {f.key for f in _model(key).params.fields()}
        assert not any("DashScope" in k for k in declared), f"{key}: {declared}"


# ── 3. 不许假装核对过（出处要能指回文档原文） ──────────────────────


def test_undocumented_model_claims_no_provenance() -> None:
    """文档里没有的模型，不许标 verified、也不许编 source_doc。

    z-image-turbo 曾在 15 份文档里一次都没出现，却挂着
    ``source_doc=千问图像生成.md`` 和 ``verified=True`` —— 假的出处比没有出处更危险。
    """
    for key in sorted(DOC_MISSING):
        model = _model(key)
        assert not model.meta.get("verified"), f"{key} 声称核对过，但文档里根本没有它"
        assert not model.meta.get("source_doc"), f"{key} 指向了不存在的出处"


# ── 4. 多图不许丢（三份文档的返回形状各不相同） ────────────────────


def test_qwen_image_single_choice_many_content_blocks() -> None:
    """千问图像：一个 choice，图像在同一 choice 的多个 content 块里。"""
    payload = {
        "output": {
            "task_status": "SUCCEEDED",
            "choices": [
                {
                    "message": {
                        "content": [
                            {"image": "https://oss/a.png", "type": "image"},
                            {"image": "https://oss/b.png", "type": "image"},
                        ]
                    }
                }
            ],
        },
        "usage": {"output_image_count": 2},
    }
    got = _artifacts(_model("qwen-image-3.0-pro"), payload)
    assert [a.uri for a in got] == ["https://oss/a.png", "https://oss/b.png"]


def test_wan26_two_choices_each_one_image() -> None:
    """万象2.6：``n=2`` 是**两个 choice** —— 只取下标 0 会悄悄丢第二张。"""
    payload = {
        "output": {
            "task_status": "SUCCEEDED",
            "choices": [
                {"message": {"content": [{"image": "https://oss/1.png", "type": "image"}]}},
                {"message": {"content": [{"image": "https://oss/2.png", "type": "image"}]}},
            ],
        },
        "usage": {"image_count": 2},
    }
    got = _artifacts(_model("wan2.6-image"), payload)
    assert [a.uri for a in got] == ["https://oss/1.png", "https://oss/2.png"]


def test_image_family_declares_no_single_artifact_path() -> None:
    """图像族不许把产物钉死在单条点号路径上（表达不了多 choice × 多块）。"""
    for key in ("qwen-image-3.0-pro", "wan2.7-image-pro", "wan2.6-image"):
        assert _model(key).api.poll is not None
        assert _model(key).api.poll.artifacts == [], key


def test_legacy_image_shape_still_parses() -> None:
    """老万相 t2i 的 ``output.results[].url`` 形状也要兜住。"""
    got = pick_image_artifacts({"output": {"results": [{"url": "https://oss/old.png"}]}})
    assert [a.uri for a in got] == ["https://oss/old.png"]


# ── 5. 结算数量跟着文档的字段名走 ─────────────────────────────────


def test_image_quantity_reads_both_documented_keys() -> None:
    """万象报 ``image_count``，千问报 ``output_image_count`` —— 两个都得认。"""
    params = _model("qwen-image-3.0-pro")
    qwen = {"usage": {"output_image_count": 3}}
    wan = {"usage": {"image_count": 2}}
    assert quantity_of(params, qwen, []) == 3.0
    assert quantity_of(params, wan, []) == 2.0


def test_video_poll_still_picks_video_url() -> None:
    """视频族与图像族共用轮询骨架，但产物键是 ``video_url``，别被一起改坏。"""
    assert ASYNC_POLL.artifacts, "视频的产物路径不能被清空"
    assert ASYNC_POLL.artifacts[0].path == "output.video_url"
