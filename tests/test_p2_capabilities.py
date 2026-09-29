"""P2 能力测试：ffmpeg 剪辑套件（可真实执行）+ 模型绑定薄壳（只测 dry-run 与闸门）。

纪律：这里**绝不发真实计费请求**。模型绑定能力只断言 plan 的形状与
"付费三道闸"的拦截行为；ffmpeg 能力本地真跑（零成本）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import divinepen
from divinepen.core.errors import BilledCallBlocked
from divinepen.core.specs import ApiSpec, ModelSpec, ParamsSchema, PollSpec

# ── 夹具 ───────────────────────────────────────────────────────────


def _make_clip(path: Path, seconds: float = 1.0, color: str = "red") -> Path:
    import subprocess

    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:size=160x120:rate=10:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
    )
    return path


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> dict[str, Path]:
    if shutil.which("ffmpeg") is None:
        pytest.skip("需要 ffmpeg")
    root = tmp_path_factory.mktemp("clips")
    return {
        "a": _make_clip(root / "a.mp4", 1.0, "red"),
        "b": _make_clip(root / "b.mp4", 1.0, "blue"),
    }


@pytest.fixture()
def media_ctx(tmp_path):
    return divinepen.local_context(media_root=tmp_path / "media")


def _video_model(**overrides) -> ModelSpec:
    spec = ModelSpec(
        provider="aliyun-bailian",
        key="wan2.7-t2v-2026-06-12",
        capability="video.t2v",
        name="文生视频测试模型",
        base_url="https://dashscope.aliyuncs.com",
        api=ApiSpec(
            method="POST",
            path="/api/v1/services/aigc/video-generation/video-synthesis",
            headers={"X-DashScope-Async": "enable"},
            mode="async_task",
            poll=PollSpec(path="/api/v1/tasks/{task_id}"),
        ),
        params=ParamsSchema.model_validate(
            {
                "groups": [
                    {
                        "key": "input",
                        "label": "输入",
                        "fields": [
                            {
                                "key": "input.prompt",
                                "label": "提示词",
                                "type": "textarea",
                                "required": True,
                            }
                        ],
                    },
                    {
                        "key": "parameters",
                        "label": "视频参数",
                        "fields": [
                            {
                                "key": "parameters.duration",
                                "label": "时长",
                                "type": "int",
                                "default": 5,
                                "billed": True,
                            },
                            {
                                "key": "parameters.resolution",
                                "label": "分辨率",
                                "type": "enum",
                                "default": "1080P",
                                "options": [
                                    {"label": "720P", "value": "720P"},
                                    {"label": "1080P", "value": "1080P"},
                                ],
                                "billed": True,
                            },
                        ],
                    },
                ]
            }
        ),
        pricing={"unit": "second", "notes": "按秒计费"},
    )
    if not overrides:
        return spec
    # model_copy 不做校验，pricing 这类嵌套模型会留下裸 dict —— 重新过一遍验证
    return ModelSpec.model_validate({**spec.model_dump(mode="json"), **overrides})


# ── 注册表 ─────────────────────────────────────────────────────────


def test_registry_has_p2_capabilities():
    ids = {m["id"] for m in divinepen.list_capabilities()}
    expected = {
        "media.probe",
        "media.trim",
        "media.concat",
        "media.burn_subtitles",
        "media.pip",
        "media.extract_audio",
        "llm.chat",
        "llm.vision",
        "image.t2i",
        "video.t2v",
        "video.i2v",
        "video.r2v",
        "video.edit",
        "video.all",
    }
    assert expected <= ids


# ── ffmpeg：dry-run 的 argv 必须长对 ────────────────────────────────


async def test_trim_plan_argv(media_ctx):
    result = await divinepen.run(
        "media.trim",
        {"source": "/tmp/clip.mp4", "start": 2, "end": 5, "reencode": True},
        ctx=media_ctx.child(dry_run=True),
    )
    argv = result.plan.requests[0].command
    assert argv[0] == "ffmpeg"
    assert "-ss" in argv and argv[argv.index("-ss") + 1] == "2"
    assert "-to" in argv and argv[argv.index("-to") + 1] == "5"
    assert "libx264" in argv  # reencode=True 必须重编码


async def test_trim_plan_copy_mode(media_ctx):
    result = await divinepen.run(
        "media.trim", {"source": "/tmp/clip.mp4", "start": 0}, ctx=media_ctx.child(dry_run=True)
    )
    argv = result.plan.requests[0].command
    assert "copy" in argv and "libx264" not in argv


async def test_concat_plan(media_ctx):
    result = await divinepen.run(
        "media.concat",
        {"sources": ["/tmp/a.mp4", "/tmp/b.mp4"], "reencode": False},
        ctx=media_ctx.child(dry_run=True),
    )
    argv = result.plan.requests[0].command
    assert argv[1:5] == ["-y", "-f", "concat", "-safe"]


async def test_pip_plan_position_preset(media_ctx):
    result = await divinepen.run(
        "media.pip",
        {"base": "/tmp/a.mp4", "overlay": "/tmp/b.mp4", "position": "top_right", "width": 320},
        ctx=media_ctx.child(dry_run=True),
    )
    argv = result.plan.requests[0].command
    assert any("overlay=x=W-w-20:y=20" in part for part in argv)
    assert any("scale=320:-2" in part for part in argv)


async def test_burn_subtitles_plan_escapes(media_ctx):
    result = await divinepen.run(
        "media.burn_subtitles",
        {"source": "/tmp/a.mp4", "subtitle": "/tmp/sub.srt", "font_size": 30},
        ctx=media_ctx.child(dry_run=True),
    )
    argv = result.plan.requests[0].command
    vf = argv[argv.index("-vf") + 1]
    assert vf.startswith("subtitles=filename=")
    assert "FontSize=30" in vf


# ── ffmpeg：真实执行（零成本）──────────────────────────────────────


async def test_trim_real(clips, tmp_path):
    ctx = divinepen.local_context(media_root=tmp_path / "media")
    result = await divinepen.run(
        "media.trim", {"source": str(clips["a"]), "start": 0, "duration": 0.5}, ctx=ctx
    )
    assert result.status.value == "succeeded"
    out = Path(result.output.uri)
    assert out.is_file() and out.stat().st_size > 0


async def test_concat_real(clips, tmp_path):
    ctx = divinepen.local_context(media_root=tmp_path / "media")
    result = await divinepen.run(
        "media.concat", {"sources": [str(clips["a"]), str(clips["b"])], "reencode": True}, ctx=ctx
    )
    assert result.status.value == "succeeded"
    assert Path(result.output.uri).stat().st_size > 0


async def test_extract_audio_real(clips, tmp_path):
    ctx = divinepen.local_context(media_root=tmp_path / "media")
    result = await divinepen.run(
        "media.extract_audio",
        {"source": str(clips["a"]), "format": "wav", "channels": 1, "sample_rate": 16000},
        ctx=ctx,
    )
    assert result.status.value == "succeeded"
    assert result.output.uri.endswith(".wav")
    assert Path(result.output.uri).is_file()


async def test_pip_real(clips, tmp_path):
    ctx = divinepen.local_context(media_root=tmp_path / "media")
    result = await divinepen.run(
        "media.pip",
        {
            "base": str(clips["a"]),
            "overlay": str(clips["b"]),
            "position": "bottom_right",
            "width": 80,
        },
        ctx=ctx,
    )
    assert result.status.value == "succeeded"
    assert Path(result.output.uri).stat().st_size > 0


# ── 模型绑定：dry-run 与三道闸 ─────────────────────────────────────


async def test_video_t2v_dry_run_builds_doc_shaped_body():
    model = _video_model()
    ctx = divinepen.local_context(
        dry_run=True, models=[model], api_keys={"aliyun-bailian": "sk-test"}
    )
    result = await divinepen.run(
        "video.t2v",
        {"params": {"input.prompt": "一只猫", "parameters.duration": 8}},
        ctx=ctx,
    )
    request = result.plan.requests[0]
    assert (
        request.url
        == "https://dashscope.aliyuncs.com/api/v1/services/aigc/video-generation/video-synthesis"
    )
    assert request.headers["X-DashScope-Async"] == "enable"
    assert request.body["model"] == "wan2.7-t2v-2026-06-12"
    assert request.body["input"] == {"prompt": "一只猫"}
    assert request.body["parameters"]["duration"] == 8
    assert request.body["parameters"]["resolution"] == "1080P"  # 默认值补上
    assert result.plan.requires_billing is True
    assert result.plan.estimated_quantity == 8
    assert result.plan.poll is not None


async def test_billed_call_blocked_without_confirmation():
    model = _video_model()
    ctx = divinepen.local_context(models=[model], api_keys={"aliyun-bailian": "sk-test"})
    with pytest.raises(BilledCallBlocked):
        await divinepen.run("video.t2v", {"params": {"input.prompt": "一只猫"}}, ctx=ctx)


async def test_unknown_param_rejected():
    from divinepen.core.errors import CapabilityInputError

    model = _video_model()
    ctx = divinepen.local_context(dry_run=True, models=[model])
    with pytest.raises(CapabilityInputError):
        await divinepen.run(
            "video.t2v", {"params": {"input.prompt": "x", "parameters.bogus": 1}}, ctx=ctx
        )


async def test_llm_chat_dry_run_openai_shape():
    model = _video_model(
        key="qwen-plus",
        capability="llm.chat",
        api=ApiSpec(method="POST", path="/compatible-mode/v1/chat/completions", mode="sync"),
        params=ParamsSchema.model_validate(
            {
                "groups": [
                    {
                        "key": "input",
                        "label": "对话",
                        "fields": [
                            {"key": "messages", "label": "消息", "type": "json", "required": True}
                        ],
                    }
                ]
            }
        ),
        pricing={"unit": "token"},
    )
    ctx = divinepen.local_context(dry_run=True, models=[model])
    result = await divinepen.run(
        "llm.chat",
        {"params": {"messages": [{"role": "user", "content": "你好"}]}},
        ctx=ctx,
    )
    request = result.plan.requests[0]
    assert request.url.endswith("/compatible-mode/v1/chat/completions")
    assert request.body["messages"][0]["content"] == "你好"
