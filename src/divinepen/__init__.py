"""Top-level package: ``divinepen`` —— 视频创作底座的原子能力层。

设计契约（改动前请先读 ``docs/ARCHITECTURE.md``）：

1. **零 Web / 零 ORM / 零 Redis**。这个包只依赖 pydantic / httpx / anyio。
   需要数据库的一切都在 ``divinepen-server`` 里。
2. **能力 ≠ 模型**。调用方只说 ``image.t2i``，不说 ``qwen-image-3.0``。
3. **Pydantic 是唯一真相源**。参数表单、校验、文档都从它派生。
4. **付费三道闸**。``plan()`` 免费 → 用户确认 → ``billed_allowed=True`` 才真执行。

最小用法::

    import anyio, divinepen

    async def main():
        for cap in divinepen.list_capabilities():
            print(cap["id"], "->", cap["category"])

    anyio.run(main)
"""

from __future__ import annotations

from .core import *  # noqa: F403
from .core import __all__ as _core_all
from .core.registry import CapabilityRegistry, build_default_registry
from .services.bootstrap import list_capabilities, local_context, run

__version__ = "0.1.0.dev0"

__all__ = [
    *_core_all,
    "CapabilityRegistry",
    "__version__",
    "build_default_registry",
    "list_capabilities",
    "local_context",
    "registry",
    "run",
]


def registry(*, discover: bool = True) -> CapabilityRegistry:
    """构建默认能力注册表（内置能力 + 已安装的插件）。"""
    return build_default_registry(discover=discover)
