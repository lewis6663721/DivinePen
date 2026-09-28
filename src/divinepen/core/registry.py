"""能力注册表：id → Capability 的映射，以及插件的自动发现。

发现机制有两级：
1. **内置能力**：``divinepen.capabilities`` 包在导入时自注册（见该包 ``__init__``）。
2. **外部插件**：``importlib.metadata.entry_points(group="divinepen.capabilities")``。
   第三方包只需在自己的 ``pyproject.toml`` 里声明入口点，装上即注册，
   **不需要改底座任何一行代码**。
"""

from __future__ import annotations

from collections.abc import Iterator
from importlib.metadata import entry_points
from typing import Any

from pydantic import BaseModel

from .capability import Capability
from .errors import CapabilityError, CapabilityNotFound
from .manifest import CapabilityManifest, Category

#: 外部插件声明的入口点组名
ENTRY_POINT_GROUP = "divinepen.capabilities"


class CapabilityRegistry:
    """线程不安全的内存注册表。服务端在启动时构建一次，之后只读。"""

    def __init__(self, *, name: str = "default") -> None:
        self.name = name
        self._caps: dict[str, Capability] = {}
        self._sources: dict[str, str] = {}

    # ── 注册 ──────────────────────────────────────────────────────

    def register(
        self, cap: Capability | type[Capability], *, source: str = "builtin", replace: bool = False
    ) -> Capability:
        instance = cap() if isinstance(cap, type) else cap
        manifest = _validate(instance, source)
        if manifest.id in self._caps and not replace:
            raise CapabilityError(
                f"能力 id 重复注册：{manifest.id}",
                detail={"existing_source": self._sources[manifest.id], "new_source": source},
            )
        self._caps[manifest.id] = instance
        self._sources[manifest.id] = source
        return instance

    def register_many(self, caps: Any, *, source: str = "builtin") -> list[str]:
        ids: list[str] = []
        for cap in caps:
            ids.append(self.register(cap, source=source).manifest.id)
        return ids

    def unregister(self, capability_id: str) -> bool:
        self._sources.pop(capability_id, None)
        return self._caps.pop(capability_id, None) is not None

    # ── 发现 ──────────────────────────────────────────────────────

    def discover(self, group: str = ENTRY_POINT_GROUP) -> list[str]:
        """扫描外部插件入口点并注册。返回本次新注册的能力 id。"""
        registered: list[str] = []
        for ep in entry_points(group=group):
            target = ep.load()
            ids = self.register_many(
                target() if callable(target) else target, source=f"plugin:{ep.value}"
            )
            registered.extend(ids)
        return registered

    # ── 读取 ──────────────────────────────────────────────────────

    def get(self, capability_id: str) -> Capability:
        try:
            return self._caps[capability_id]
        except KeyError as exc:
            raise CapabilityNotFound(
                f"未注册的能力：{capability_id}",
                detail={"available": sorted(self._caps)},
            ) from exc

    def has(self, capability_id: str) -> bool:
        return capability_id in self._caps

    def source_of(self, capability_id: str) -> str | None:
        return self._sources.get(capability_id)

    def ids(self) -> list[str]:
        return sorted(self._caps)

    def manifests(self, *, category: Category | str | None = None) -> list[CapabilityManifest]:
        cats = {category} if category else None
        return [
            c.manifest
            for c in sorted(self._caps.values(), key=lambda c: c.manifest.id)
            if cats is None or c.manifest.category in cats
        ]

    def by_category(self) -> dict[str, list[CapabilityManifest]]:
        out: dict[str, list[CapabilityManifest]] = {}
        for m in self.manifests():
            out.setdefault(m.category.value, []).append(m)
        return out

    def for_model_capability(self, model_capability: str) -> list[Capability]:
        return [c for c in self._caps.values() if c.manifest.model_capability == model_capability]

    def catalog(self) -> list[dict[str, Any]]:
        """给页面用的能力目录（含表单 schema）。"""
        return [c.info() for c in sorted(self._caps.values(), key=lambda c: c.manifest.id)]

    # ── 容器协议 ──────────────────────────────────────────────────

    def __contains__(self, capability_id: object) -> bool:
        return isinstance(capability_id, str) and capability_id in self._caps

    def __iter__(self) -> Iterator[Capability]:
        return iter(self._caps.values())

    def __len__(self) -> int:
        return len(self._caps)


def _validate(cap: Capability, source: str) -> CapabilityManifest:
    manifest = getattr(cap, "manifest", None)
    if not isinstance(manifest, CapabilityManifest):
        raise CapabilityError(
            f"{type(cap).__name__} 缺少合法的 manifest",
            detail={"source": source, "hint": "子类必须声明 ClassVar[CapabilityManifest]"},
        )
    for attr in ("Input", "Output"):
        model = getattr(cap, attr, None)
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise CapabilityError(
                f"{manifest.id} 缺少合法的 {attr}（必须是 BaseModel 子类）",
                detail={"source": source},
            )
    return manifest


def build_default_registry(*, discover: bool = True) -> CapabilityRegistry:
    """构建包含内置能力（+ 已安装插件）的注册表。"""
    reg = CapabilityRegistry(name="default")
    from ..capabilities import builtin_capabilities

    reg.register_many(builtin_capabilities(), source="builtin")
    if discover:
        reg.discover()
    return reg
