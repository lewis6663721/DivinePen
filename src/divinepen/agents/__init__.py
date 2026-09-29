"""L5 插件层的 SDK：智能体契约 + 加载器。

一个"智能体"就是一个可 ``pip install`` 的包，通过 entry point 注册::

    # divinepen-agent-jinrong/pyproject.toml
    [project.entry-points."divinepen.agents"]
    jinrong = "divinepen_agent_jinrong:PLUGIN"

插件**只能**通过 ``capability_id`` 消费底座能力，不得直接依赖某个供应商 SDK。
这样一个智能体既能跑在 DivinePen 后台里，也能被别的项目导入复用。

> 本期（P0–P3）只落契约，不实现任何业务智能体。见 ``docs/ARCHITECTURE.md`` §10。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from pydantic import BaseModel, Field

from ..core.manifest import Requirement
from ..core.registry import CapabilityRegistry

#: 插件声明的 entry point 组名
ENTRY_POINT_GROUP = "divinepen.agents"


class AgentManifest(BaseModel):
    """智能体自描述。页面「插件管理」直接渲染它。"""

    id: str = Field(description="插件标识，如 jinrong-video")
    name: str
    version: str = "0.1.0"
    description: str = ""
    #: 依赖的原子能力 id。启用前会逐个体检，缺哪个报哪个。
    capabilities: list[str] = Field(default_factory=list)
    #: 依赖的供应商凭据
    providers: list[str] = Field(default_factory=list)
    #: 额外运行前置（ffmpeg / GPU / 浏览器…）
    requires: list[Requirement] = Field(default_factory=list)
    author: str = ""
    homepage: str = ""


class AgentPlugin(ABC):
    """智能体插件的契约。

    :meth:`register` 只负责把能力注册进注册表；业务路由由 :attr:`api_path`
    暴露，服务端挂到 ``/agents/{manifest.id}`` 下。
    """

    manifest: ClassVar[AgentManifest]

    @abstractmethod
    def register(self, registry: CapabilityRegistry) -> None:
        """把本插件的能力注册进底座注册表。"""

    def api_path(self) -> str | None:
        """可选的业务路由模块路径，如 ``divinepen_agent_jinrong.api:router``。"""
        return None

    def missing_requirements(self, registry: CapabilityRegistry) -> list[str]:
        """体检：底座侧缺什么才能启用这个插件。"""
        import shutil

        missing = [f"缺少能力 {cid}" for cid in self.manifest.capabilities if not registry.has(cid)]
        for req in self.manifest.requires:
            if req in {Requirement.FFMPEG, Requirement.FFPROBE} and shutil.which(req.value) is None:
                missing.append(f"缺少 {req.value} 可执行文件")
            elif req is Requirement.FFMPEG_SUBTITLES and not _ffmpeg_has_subtitles():
                missing.append("ffmpeg 缺 subtitles 滤镜（未编译 libass）")
        return missing


def _ffmpeg_has_subtitles() -> bool:
    """同步探针（插件体检是同步方法）。结果缓存，只跑一次 ffmpeg。"""
    import shutil
    import subprocess

    cached = globals().get("_SUBTITLES_CACHE")
    if cached is not None:
        return cached
    if shutil.which("ffmpeg") is None:
        globals()["_SUBTITLES_CACHE"] = False
        return False
    out = subprocess.run(
        ["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, check=False
    ).stdout
    ok = any(
        line.split()[1:2] == ["subtitles"] for line in out.splitlines() if len(line.split()) >= 2
    )
    globals()["_SUBTITLES_CACHE"] = ok
    return ok


def discover_plugins() -> list[AgentPlugin]:
    """扫描已安装的智能体插件（entry_points）。"""
    from importlib.metadata import entry_points

    plugins: list[AgentPlugin] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        target = ep.load()
        plugin = target() if callable(target) else target
        if not isinstance(plugin, AgentPlugin):
            raise TypeError(f"{ep.value} 不是合法的 AgentPlugin")
        plugins.append(plugin)
    return plugins


__all__ = ["ENTRY_POINT_GROUP", "AgentManifest", "AgentPlugin", "discover_plugins"]
