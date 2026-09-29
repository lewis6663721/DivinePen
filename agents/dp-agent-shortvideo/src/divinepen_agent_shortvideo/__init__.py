"""短视频包装智能体（首个业务插件）。

业务就一句话：**把一段素材裁出精华、烧上字幕、挂上角标**。
四步全是本地 ffmpeg 能力，所以这个插件零成本、可离线真跑 ——
作为"首个业务插件"，它先证明编排层与插件机制是通的，
业务方照这个形状换成带计费步骤的版本即可。

安装即生效（entry point ``divinepen.agents``），不需要改底座一行代码。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from divinepen.agents import AgentManifest, AgentPlugin
from divinepen.core.artifact import Artifact
from divinepen.core.capability import Capability
from divinepen.core.manifest import CapabilityManifest, Category, CostModel, Requirement
from divinepen.core.registry import CapabilityRegistry
from divinepen.pipeline import PipelineCapability, PipelineOutput, Recipe, StepSpec

__version__ = "0.1.0"


class PackageInput(BaseModel):
    source: Artifact = Field(
        description="待包装的原始视频。",
        json_schema_extra={"label": "原始视频", "group": "input", "group_label": "输入"},
    )
    subtitle: Artifact = Field(
        description="字幕文件（.srt / .ass）。",
        json_schema_extra={"label": "字幕文件", "group": "input", "group_label": "输入"},
    )
    logo: Artifact = Field(
        description="角标素材（图片，建议带透明通道）。",
        json_schema_extra={"label": "角标图片", "group": "input", "group_label": "输入"},
    )
    start: float = Field(
        default=0.0,
        ge=0,
        description="从原片第几秒开始取精华。",
        json_schema_extra={"label": "起始秒", "group": "cut", "group_label": "取精华"},
    )
    duration: float = Field(
        default=8.0,
        gt=0,
        le=60,
        description="精华片段时长。",
        json_schema_extra={"label": "时长（秒）", "group": "cut", "group_label": "取精华"},
    )
    font_size: int = Field(
        default=28,
        ge=8,
        le=96,
        description="字幕字号。",
        json_schema_extra={"label": "字幕字号", "group": "style", "group_label": "包装样式"},
    )
    position: str = Field(
        default="top_right",
        description="角标位置预设。",
        json_schema_extra={
            "label": "角标位置",
            "group": "style",
            "group_label": "包装样式",
            "options": ["top_right", "top_left", "bottom_right", "bottom_left", "center"],
        },
    )
    logo_width: int = Field(
        default=160,
        ge=32,
        le=1920,
        description="角标宽度（像素）。",
        json_schema_extra={"label": "角标宽度", "group": "style", "group_label": "包装样式"},
    )


RECIPE = Recipe(
    id="shortvideo.package",
    name="短视频包装",
    description="裁剪 → 烧字幕 → 挂角标。四步全本地，零成本。",
    steps=[
        StepSpec(
            id="probe",
            capability="media.probe",
            inputs={"source": "$params.source"},
            description="先探元信息，确认素材可读",
        ),
        StepSpec(
            id="clip",
            capability="media.trim",
            inputs={
                "source": "$params.source",
                "start": "$params.start",
                "duration": "$params.duration",
                "reencode": True,
            },
            description="裁出精华片段（重编码，切点精确）",
        ),
        StepSpec(
            id="titled",
            capability="media.burn_subtitles",
            inputs={
                "source": "$clip.uri",
                "subtitle": "$params.subtitle",
                "font_size": "$params.font_size",
            },
            description="烧上字幕",
        ),
        StepSpec(
            id="packaged",
            capability="media.pip",
            inputs={
                "base": "$titled.uri",
                "overlay": "$params.logo",
                "position": "$params.position",
                "width": "$params.logo_width",
            },
            description="挂上角标，出成品",
        ),
    ],
)


class ShortVideoPackage(PipelineCapability):
    manifest = CapabilityManifest(
        id="media.shortvideo_package",
        name="短视频包装（智能体）",
        category=Category.MEDIA,
        description="裁剪 → 烧字幕 → 挂角标 一条流水线。来自插件 divinepen-agent-shortvideo。",
        requires=[Requirement.FFMPEG, Requirement.FFPROBE],
        cost=CostModel(unit="free", notes="四步全本地 ffmpeg，不产生任何费用"),
        tags=["智能体", "流水线", "ffmpeg"],
    )
    recipe = RECIPE
    Input = PackageInput
    Output = PipelineOutput


class ShortVideoAgent(AgentPlugin):
    manifest = AgentManifest(
        id="shortvideo",
        name="短视频包装智能体",
        version=__version__,
        description="把原始素材包装成可发布的短视频：取精华、烧字幕、挂角标。",
        capabilities=["media.probe", "media.trim", "media.burn_subtitles", "media.pip"],
        requires=[Requirement.FFMPEG, Requirement.FFPROBE, Requirement.FFMPEG_SUBTITLES],
        author="DivinePen",
    )

    def register(self, registry: CapabilityRegistry) -> None:
        registry.register(ShortVideoPackage(), source=f"agent:{self.manifest.id}")

    def capabilities(self) -> list[Capability]:
        return [ShortVideoPackage()]


PLUGIN = ShortVideoAgent()

__all__ = ["PLUGIN", "RECIPE", "ShortVideoAgent", "ShortVideoPackage", "__version__"]
