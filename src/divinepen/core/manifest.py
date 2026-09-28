"""能力清单（Manifest）：一个原子能力对外自报的家底。

**Capability 的 id 是全局稳定契约。** 上层应用、智能体插件、pipeline 模板
只认 ``id``，不认实现。因此 id 一旦发布就不可改名，只能加版本 / 标记废弃。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, Field, field_validator, model_validator

#: ``image.t2i`` / ``media.burn_subtitles`` —— 至少两段，小写字母数字下划线
ID_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$"


class Category(StrEnum):
    """能力的粗分类。页面据此分栏，数据库据此聚合。"""

    LLM = "llm"
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    MEDIA = "media"  # 本地 ffmpeg 剪辑，不绑模型
    SYSTEM = "system"  # 编排/文本/存储等底座自身能力


class Requirement(StrEnum):
    """运行前置条件。启用/调用前据此做体检，缺什么报什么。"""

    NETWORK = "network"
    FFMPEG = "ffmpeg"
    FFPROBE = "ffprobe"
    GPU = "gpu"
    OSS = "oss"
    REDIS = "redis"
    BROWSER = "browser"


class CostUnit(StrEnum):
    FREE = "free"
    REQUEST = "request"
    TOKEN = "token"
    IMAGE = "image"
    SECOND = "second"
    CHARACTER = "character"


class CostModel(BaseModel):
    unit: CostUnit = CostUnit.FREE
    notes: str = ""

    @property
    def is_free(self) -> bool:
        return self.unit is CostUnit.FREE


class CapabilityStatus(StrEnum):
    STABLE = "stable"
    BETA = "beta"
    DEPRECATED = "deprecated"


class CapabilityManifest(BaseModel):
    """一个能力的自描述。能力的 ``Input``/``Output`` 由 Pydantic 承载，不在这里。"""

    id: str
    name: str
    version: str = "0.1.0"
    category: Category
    description: str = ""
    #: 绑定模型分类（= dp_model.capability）。纯本地能力（ffmpeg）为 None。
    model_capability: str | None = None
    requires: list[Requirement] = Field(default_factory=list)
    cost: CostModel = Field(default_factory=CostModel)
    #: 是否需要「提交 → 轮询 → 收尾」的异步任务生命周期
    async_task: bool = False
    #: 同输入是否可命中产物缓存（贵能力建议 True）
    cacheable: bool = False
    status: CapabilityStatus = CapabilityStatus.STABLE
    replaced_by: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        import re

        if not re.match(ID_PATTERN, v):
            raise ValueError(f"能力 id 非法：{v!r}，应形如 'image.t2i'（{ID_PATTERN}）")
        return v

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        # 不变式：id 的第一段就是分类。这条约束让"看 id 就知道归到哪个页面分栏"
        # 永远成立，也避免同一件事出现 image.foo / video.foo 两套命名。
        if not self.id.startswith(f"{self.category.value}."):
            raise ValueError(
                f"能力 id {self.id!r} 的前缀必须是分类 {self.category.value!r}，"
                f"即应形如 {self.category.value}.xxx"
            )
        if self.status is CapabilityStatus.DEPRECATED and not self.replaced_by:
            raise ValueError(f"能力 {self.id!r} 已废弃，必须声明 replaced_by")
        return self

    @property
    def namespace(self) -> str:
        return self.id.split(".", 1)[0]

    @property
    def needs_credential(self) -> bool:
        return Requirement.NETWORK in self.requires and self.model_capability is not None
