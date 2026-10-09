"""产物（Artifact）：能力之间**只传文件引用 + 元数据**，不传对象。

设计约束（来自项目历史踩坑）：
* 阿里系生成接口要求参考图是**供应商可拉取**的资源。本地相对路径会
  ``Failed to download``。因此产物必须能转换成一个供应商可达的 URL，
  见 :meth:`Artifact.to_data_url` 与 ``ArtifactStore.resolve_for_provider``。
* base64 内联有硬上限（阿里系 20MB，且 PNG 不允许透明通道）。
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
from enum import StrEnum
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .errors import ArtifactError, ArtifactTooLarge

#: 单张参考图 base64 内联的硬上限（阿里云百炼约束）。
PROVIDER_INLINE_MAX_BYTES = 20 * 1024 * 1024

_SUFFIX_KIND: dict[str, ArtifactKind] = {
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".webp": "image",
    ".bmp": "image",
    ".gif": "image",
    ".mp4": "video",
    ".mov": "video",
    ".mkv": "video",
    ".webm": "video",
    ".avi": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
    ".aac": "audio",
    ".flac": "audio",
    ".ogg": "audio",
    ".srt": "subtitle",
    ".ass": "subtitle",
    ".vtt": "subtitle",
    ".txt": "text",
    ".md": "text",
    ".json": "json",
}


class ArtifactKind(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    SUBTITLE = "subtitle"
    TEXT = "text"
    JSON = "json"
    ARCHIVE = "archive"
    OTHER = "other"


class Artifact(BaseModel):
    """一次能力调用产出的、或作为输入传入的文件。"""

    model_config = ConfigDict(frozen=True)

    uri: str = Field(description="file:// / http(s):// / oss:// / data: 或本地绝对路径")
    name: str | None = Field(
        default=None,
        description="人类可读名，如「生成图片」/「生成视频」；页面预览拿它当标题",
    )
    kind: ArtifactKind = ArtifactKind.OTHER
    mime: str | None = None
    size: int | None = None
    sha256: str | None = None
    duration: float | None = Field(default=None, description="音视频时长（秒）")
    width: int | None = None
    height: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)

    # ── 构造 ───────────────────────────────────────────────────────

    @model_validator(mode="before")
    @classmethod
    def _accept_bare_string(cls, data: Any) -> Any:
        """允许直接传路径或 URL 字符串。

        ``{"source": "/tmp/a.mp4"}`` 是最自然的写法，不该逼用户写成
        ``{"source": {"uri": "/tmp/a.mp4"}}``。类型推断交给下面的 after 校验器。
        """
        if isinstance(data, str):
            return {"uri": data}
        return data

    @model_validator(mode="after")
    def _fill_missing(self) -> Self:
        mime = self.mime
        kind = self.kind
        if mime is None and self.is_local:
            guessed, _ = mimetypes.guess_type(self.uri)
            mime = guessed
        if kind is ArtifactKind.OTHER:
            kind = _kind_from(self.uri, mime)
        if kind is not self.kind or mime is not self.mime:
            object.__setattr__(self, "kind", kind)
            object.__setattr__(self, "mime", mime)
        return self

    @classmethod
    def from_path(cls, path: str | Path, **meta: Any) -> Artifact:
        """从本地文件构造，顺便算出 size / sha256 / mime。"""
        p = Path(path).expanduser()
        if not p.is_file():
            raise ArtifactError(f"文件不存在：{p}", detail={"path": str(p)})
        data = p.read_bytes()
        mime, _ = mimetypes.guess_type(p.name)
        return cls(
            uri=str(p.resolve()),
            mime=mime,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            meta=meta,
        )

    @classmethod
    def from_url(cls, url: str, *, kind: ArtifactKind | None = None, **meta: Any) -> Artifact:
        return cls(uri=url, kind=kind or _kind_from(url, None), meta=meta)

    # ── 查询 ───────────────────────────────────────────────────────

    @property
    def scheme(self) -> str:
        parts = urlparse(self.uri)
        return parts.scheme or "file"

    @property
    def is_local(self) -> bool:
        return not urlparse(self.uri).scheme or self.uri.startswith("file://")

    @property
    def is_remote(self) -> bool:
        return self.scheme in {"http", "https", "oss"}

    def path(self) -> Path:
        if not self.is_local:
            raise ArtifactError(f"不是本地产物：{self.uri}", detail={"uri": self.uri})
        raw = self.uri[7:] if self.uri.startswith("file://") else self.uri
        return Path(raw)

    # ── 供应商可达化 ───────────────────────────────────────────────

    def to_data_url(self, *, max_bytes: int = PROVIDER_INLINE_MAX_BYTES) -> str:
        """把本地文件编成 ``data:*;base64,`` URL。

        这是绕开"相对路径供应商拉不到"的最省事方案；代价是请求体变大。
        超限直接抛 :class:`ArtifactTooLarge` —— 绝不静默截断。
        """
        if not self.is_local:
            return self.uri
        p = self.path()
        data = p.read_bytes()
        if len(data) > max_bytes:
            raise ArtifactTooLarge(
                f"内联参考图超过 {max_bytes // 1024 // 1024}MB 上限，请改用公网 URL 或 OSS",
                detail={"path": str(p), "size": len(data), "max_bytes": max_bytes},
            )
        if self.kind is ArtifactKind.IMAGE and p.suffix.lower() == ".png" and _has_alpha(data):
            raise ArtifactError(
                "参考图 PNG 含透明通道，阿里系会拒绝。请先合成到不透明底色再上传。",
                detail={"path": str(p)},
            )
        mime = self.mime or "application/octet-stream"
        return f"data:{mime};base64,{base64.b64encode(data).decode()}"

    def with_meta(self, **extra: Any) -> Artifact:
        return self.model_copy(update={"meta": {**self.meta, **extra}})


def _kind_from(uri: str, mime: str | None) -> ArtifactKind:
    suffix = Path(urlparse(uri).path).suffix.lower()
    if suffix in _SUFFIX_KIND:
        return ArtifactKind(_SUFFIX_KIND[suffix])
    if mime:
        top = mime.split("/", 1)[0]
        if top in {"image", "video", "audio", "text"}:
            return ArtifactKind.IMAGE if top == "image" else ArtifactKind(top)
        if mime == "application/json":
            return ArtifactKind.JSON
    return ArtifactKind.OTHER


def _has_alpha(png_bytes: bytes) -> bool:
    """读 PNG IHDR 判断 color type 是否带 alpha（6=RGBA, 4=灰度+alpha）。"""
    if len(png_bytes) < 26 or png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    return png_bytes[25] in {4, 6}
