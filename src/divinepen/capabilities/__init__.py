"""内置原子能力。

每个能力一个模块、一份 ``Input/Output``、一份 ``manifest``。
新增能力的步骤见 ``docs/guides/capability-authoring.md``。

目录按功能分类组织，与 ``manifest.category`` 对应：

* ``llm/``   文本、视觉理解（模型绑定薄壳）
* ``image/`` 文生图、图生图、图像编辑（模型绑定薄壳）
* ``video/`` 文生视频、图生视频、参考生视频、视频编辑（模型绑定薄壳，异步任务）
* ``audio/`` 一期留空：TTS/ASR 走 WebSocket，传输层没实现前不注册
* ``media/`` 本地 ffmpeg 剪辑（不绑模型，零成本）

模型绑定能力的参数 schema 来自数据库里的文档目录（点号路径），
请求组装与响应解析在 ``providers/aliyun.py``。
"""

from __future__ import annotations

from ..core.capability import Capability
from .image import AliyunImage
from .llm import AliyunChat, AliyunVision
from .media import BurnSubtitles, Concat, ExtractAudio, MediaProbe, Pip, Trim
from .video import AliyunI2V, AliyunR2V, AliyunT2V, AliyunVideoAll, AliyunVideoEdit


def builtin_capabilities() -> list[Capability]:
    """底座自带的能力实例。

    外部插件不走这里，走 ``entry_points(group="divinepen.capabilities")`` 自动发现。
    """
    return [
        # 本地剪辑（零成本，可真实执行）
        MediaProbe(),
        Trim(),
        Concat(),
        BurnSubtitles(),
        Pip(),
        ExtractAudio(),
        # 模型绑定（阿里系适配器；dry-run 免费，真实调用走三道闸）
        AliyunChat(),
        AliyunVision(),
        AliyunImage(),
        AliyunT2V(),
        AliyunI2V(),
        AliyunR2V(),
        AliyunVideoEdit(),
        AliyunVideoAll(),
    ]
