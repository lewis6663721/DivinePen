"""内置原子能力。

每个能力一个模块、一份 ``Input/Output``、一份 ``manifest``。
新增能力的步骤见 ``docs/guides/capability-authoring.md``。

目录按功能分类组织，与 ``manifest.category`` 对应：

* ``llm/``   文本、视觉理解
* ``image/`` 文生图、图生图、图像编辑
* ``video/`` 文生视频、图生视频、参考生视频、视频编辑
* ``audio/`` 语音合成、声音复刻、语音识别
* ``media/`` 本地 ffmpeg 剪辑（不绑模型）
"""

from __future__ import annotations

from ..core.capability import Capability
from .media import MediaProbe


def builtin_capabilities() -> list[Capability]:
    """底座自带的能力实例。

    外部插件不走这里，走 ``entry_points(group="divinepen.capabilities")`` 自动发现。
    """
    return [MediaProbe()]
