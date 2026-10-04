"""P3 测试：编排层 + 插件发现 + 首个业务智能体（纯本地真跑）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

import divinepen
from divinepen.core.artifact import Artifact
from divinepen.core.manifest import AgentMode, CapabilityManifest, Category
from divinepen.pipeline import (
    PipelineCapability,
    PipelineError,
    PipelineOutput,
    Recipe,
    StepSpec,
    resolve_refs,
)

pytest_plugins = []


def _has_subtitles_filter() -> bool:
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True
    ).stdout
    return any(
        line.split()[1:2] == ["subtitles"] for line in out.splitlines() if len(line.split()) >= 2
    )


needs_subtitles = pytest.mark.skipif(
    not _has_subtitles_filter(), reason="本机 ffmpeg 未编译 libass（无 subtitles 滤镜）"
)


# ── 夹具 ───────────────────────────────────────────────────────────


def _clip(path: Path, seconds: float = 2.0, color: str = "red") -> Path:
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


def _logo(path: Path) -> Path:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=yellow@0.8:size=80x40:duration=0.1",
            "-frames:v",
            "1",
            str(path),
        ],
        check=True,
    )
    return path


def _srt(path: Path) -> Path:
    path.write_text("1\n00:00:00,000 --> 00:00:02,000\n DivinePen 编排层自检\n", encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def assets(tmp_path_factory) -> dict[str, Path]:
    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
        pytest.skip("需要 ffmpeg")
    root = tmp_path_factory.mktemp("p3")
    return {
        "clip": _clip(root / "clip.mp4"),
        "logo": _logo(root / "logo.png"),
        "srt": _srt(root / "sub.srt"),
    }


# ── 引用解析 ───────────────────────────────────────────────────────


def test_resolve_refs_params_and_outputs():
    outputs = {"clip": {"uri": "/tmp/a.mp4", "out": {"duration": 2.0}, "artifacts": []}}
    assert resolve_refs("$clip.uri", outputs=outputs, params={}) == "/tmp/a.mp4"
    assert resolve_refs("$clip.out.duration", outputs=outputs, params={}) == 2.0
    assert resolve_refs("$params.x", outputs={}, params={"x": 7}) == 7
    assert resolve_refs(["$clip.uri", "plain"], outputs=outputs, params={}) == [
        "/tmp/a.mp4",
        "plain",
    ]


def test_resolve_refs_dry_run_placeholder():
    assert (
        resolve_refs("$clip.uri", outputs={}, params={}, placeholder="<上一步产物>")
        == "<上一步产物>"
    )


def test_resolve_refs_unknown_param_raises():
    with pytest.raises(PipelineError):
        resolve_refs("$params.nope", outputs={}, params={})


def test_resolve_refs_missing_step_raises_when_not_dry():
    with pytest.raises(PipelineError):
        resolve_refs("$clip.uri", outputs={}, params={})


# ── 插件发现 ───────────────────────────────────────────────────────


def test_plugin_discovered_and_registered():
    registry = divinepen.registry()
    assert registry.has("media.shortvideo_package"), f"已注册：{registry.ids()}"
    assert registry.source_of("media.shortvideo_package") == "agent:shortvideo"

    from divinepen.agents import discover_plugins

    plugins = discover_plugins()
    assert any(p.manifest.id == "shortvideo" for p in plugins)
    agent = next(p for p in plugins if p.manifest.id == "shortvideo")
    missing = agent.missing_requirements(registry)
    if _has_subtitles_filter():
        assert missing == []
    else:
        # 环境缺 libass 时，体检必须只报这一条，不能漏报也不能误报
        assert missing == ["ffmpeg 缺 subtitles 滤镜（未编译 libass）"]


# ── 流水线：dry-run 不花钱、引用是占位符 ───────────────────────────


async def test_pipeline_dry_run(assets, tmp_path):
    registry = divinepen.registry()
    ctx = divinepen.local_context(media_root=tmp_path / "media", dry_run=True, registry=registry)
    result = await registry.get("media.shortvideo_package").invoke(
        {
            "source": str(assets["clip"]),
            "subtitle": str(assets["srt"]),
            "logo": str(assets["logo"]),
            "start": 0,
            "duration": 1,
        },
        ctx,
    )
    assert result.status.value == "dry_run"
    assert len(result.plan.steps) == 4
    assert all("<上一步产物>" in s or "media." in s for s in result.plan.steps)
    assert result.plan.requires_billing is False  # 四步全本地
    if not _has_subtitles_filter():
        # 环境缺 libass 时，预演必须把这件事说出来，而不是等真跑才炸
        assert any("libass" in w for w in result.plan.warnings)


# ── 流水线：真实执行 ───────────────────────────────────────────────


@needs_subtitles
async def test_pipeline_real_run(assets, tmp_path):
    registry = divinepen.registry()
    ctx = divinepen.local_context(media_root=tmp_path / "media", registry=registry)
    result = await registry.get("media.shortvideo_package").invoke(
        {
            "source": str(assets["clip"]),
            "subtitle": str(assets["srt"]),
            "logo": str(assets["logo"]),
            "start": 0,
            "duration": 1,
            "font_size": 20,
            "position": "top_right",
            "logo_width": 60,
        },
        ctx,
    )
    assert result.status.value == "succeeded", result.error
    output = result.output
    assert [s.step_id for s in output.steps] == ["probe", "clip", "titled", "packaged"]
    assert all(s.status == "succeeded" for s in output.steps)
    final = Path(output.final_artifacts[0])
    assert final.is_file() and final.stat().st_size > 0


async def test_pipeline_step_failure_is_attributed(assets, tmp_path):
    registry = divinepen.registry()
    ctx = divinepen.local_context(media_root=tmp_path / "media", registry=registry)
    result = await registry.get("media.shortvideo_package").invoke(
        {
            "source": str(assets["clip"]),
            "subtitle": "/nonexistent/sub.srt",  # 第三步会炸
            "logo": str(assets["logo"]),
            "duration": 1,
        },
        ctx,
    )
    assert result.status.value == "failed"
    assert "titled" in (result.error or {}).get("message", "")


# ── interactive：人工闸口暂停 / 续跑 ────────────────────────────────


class _GateRecipeInput(BaseModel):
    source: Artifact = Field(description="源")
    logo: Artifact = Field(description="角标")
    duration: float = Field(default=1.0, gt=0)


_GATE_RECIPE = Recipe(
    id="test.gate",
    name="闸口自检",
    steps=[
        StepSpec(
            id="clip",
            capability="media.trim",
            inputs={"source": "$params.source", "duration": "$params.duration", "reencode": True},
        ),
        StepSpec(
            id="packaged",
            capability="media.pip",
            gate=True,
            gate_prompt="裁剪结果 OK 吗？",
            inputs={
                "base": "$clip.uri",
                "overlay": "$params.logo",
                "position": "top_right",
                "width": 40,
            },
        ),
    ],
)


class _GateCap(PipelineCapability):
    manifest = CapabilityManifest(
        id="media.test_gate",
        name="闸口自检",
        category=Category.MEDIA,
        description="测试用：第二步是人工闸口",
        agent_mode=AgentMode.INTERACTIVE,
    )
    recipe = _GATE_RECIPE
    Input = _GateRecipeInput
    Output = PipelineOutput


async def test_gate_pauses_then_resumes(assets, tmp_path):
    registry = divinepen.registry()
    registry.register(_GateCap(), source="agent:test", replace=True)
    values = {"source": str(assets["clip"]), "logo": str(assets["logo"]), "duration": 1}

    ctx = divinepen.local_context(media_root=tmp_path / "media", registry=registry)
    first = await registry.get("media.test_gate").invoke(values, ctx)
    assert first.status.value == "failed"
    assert first.error["code"] == "pipeline_paused"
    detail = first.error["detail"]
    assert detail["step_id"] == "packaged"
    assert detail["start_index"] == 1
    assert detail["outputs"]["clip"]["uri"]  # 中间产物保留下来了

    # 带着确认续跑：从暂停态恢复，不重跑第一步
    ctx2 = divinepen.local_context(
        media_root=tmp_path / "media",
        registry=registry,
        resume={
            "outputs": detail["outputs"],
            "steps": detail["steps"],
            "start_index": detail["start_index"],
        },
        gate_approvals={"packaged": {"width": 50}},
    )
    second = await registry.get("media.test_gate").invoke(values, ctx2)
    assert second.status.value == "succeeded", second.error
    # 续跑会把暂停前的步骤历史一起带上，但不会重跑它们
    assert [s.step_id for s in second.output.steps] == ["clip", "packaged"]
    assert Path(second.output.final_artifacts[0]).is_file()


# ── 后台执行端点（pipeline 模式的运行方式）─────────────────────────


def test_background_run_endpoint_creates_task(assets):
    """端点职责：提交即落库 + 303 去任务中心。

    后台协程跑在**服务自己的事件循环**上；TestClient 每个请求一个循环，
    观察不到后台完成（那是测试客户端的限制，不是服务的）。完整链路在真服务上验。
    """
    from fastapi.testclient import TestClient

    from divinepen_server.web import create_app

    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.post(
            "/capabilities/media.trim/run",
            data={"source": str(assets["clip"]), "start": "0", "duration": "0.5"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        task_url = r.headers["location"].split("?")[0]
        page = client.get(task_url).text
        assert task_url.rsplit("/", 1)[-1] in page
        assert ("PENDING" in page) or ("RUNNING" in page) or ("SUCCEEDED" in page)
