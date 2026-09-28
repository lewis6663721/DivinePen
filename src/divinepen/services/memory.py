"""端口的**零依赖默认实现**（内存 / 本地磁盘）。

存在的意义：让 ``pip install divinepen`` 的人不配数据库、不起 Redis 就能
把原子能力跑起来。服务端会用数据库版本替换掉它们，能力代码一行都不用改。
"""

from __future__ import annotations

import mimetypes
import os
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..core.artifact import Artifact, ArtifactKind
from ..core.errors import ArtifactError, CredentialMissing, ModelNotResolved
from ..core.ports import ResolvePolicy
from ..core.records import TaskRef, UsageRecord, utcnow
from ..core.specs import ModelSpec, ParamsSchema, ResolvedModel, TaskStatus


class EnvCredentialProvider:
    """按 ``{PROVIDER}_API_KEY`` 约定读环境变量。

    约定：供应商代码 ``aliyun`` → ``DASHSCOPE_API_KEY`` 的映射由
    ``aliases`` 参数覆盖；默认用大写供应商名。
    """

    def __init__(
        self,
        values: dict[str, str] | None = None,
        *,
        aliases: dict[str, str] | None = None,
        prefix: str = "",
    ) -> None:
        self._values = {k.lower(): v for k, v in (values or {}).items()}
        self._aliases = {k.lower(): v for k, v in (aliases or {}).items()}
        self._prefix = prefix
        self._masked: set[str] = set()

    def add(self, provider: str, api_key: str) -> None:
        self._values[provider.lower()] = api_key

    def _env_name(self, provider: str) -> str:
        name = self._aliases.get(provider.lower(), provider.upper().replace("-", "_"))
        return f"{self._prefix}{name}_API_KEY"

    async def get(self, provider: str, *, workspace: str | None = None) -> str:
        value = self._values.get(provider.lower()) or os.getenv(self._env_name(provider))
        if not value:
            raise CredentialMissing(
                f"未配置 {provider} 的 API Key",
                detail={"provider": provider, "env": self._env_name(provider)},
            )
        return value

    async def has(self, provider: str, *, workspace: str | None = None) -> bool:
        return bool(self._values.get(provider.lower()) or os.getenv(self._env_name(provider)))


class InMemoryUsageSink:
    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    async def record(self, record: UsageRecord) -> None:
        self.records.append(record)

    def total(self, currency: str = "CNY") -> float:
        return float(sum((r.amount or 0) for r in self.records if r.currency == currency))


class InMemoryTaskSink:
    def __init__(self) -> None:
        self.tasks: dict[str, TaskRef] = {}

    async def create(
        self,
        *,
        capability_id: str,
        provider: str,
        model: str,
        status: str | None = None,
        remote_task_id: str | None = None,
        payload: dict[str, Any] | None = None,
        user_id: str | None = None,
    ) -> TaskRef:
        ref = TaskRef(
            id=uuid.uuid4().hex,
            capability_id=capability_id,
            provider=provider,
            model=model,
            status=TaskStatus(status) if status else TaskStatus.PENDING,
            remote_task_id=remote_task_id,
            payload=payload or {},
            user_id=user_id,
        )
        self.tasks[ref.id] = ref
        return ref

    async def update(self, task_id: str, **patch: Any) -> TaskRef | None:
        ref = self.tasks.get(task_id)
        if ref is None:
            return None
        data = ref.model_dump()
        data.update(patch)
        data["updated_at"] = utcnow()
        if data.get("status") in {TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELED}:
            data.setdefault("finished_at", utcnow())
        updated = TaskRef(**data)
        self.tasks[task_id] = updated
        return updated

    async def get(self, task_id: str) -> TaskRef | None:
        return self.tasks.get(task_id)

    async def active(self, *, capability_id: str | None = None) -> list[TaskRef]:
        return [
            t
            for t in self.tasks.values()
            if t.status.is_active and (capability_id is None or t.capability_id == capability_id)
        ]


class LocalArtifactStore:
    """本地产物仓。``root`` 下按 ``YYYY/MM/`` 分目录落盘。"""

    def __init__(self, root: str | Path = "media") -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    async def put(
        self,
        data: bytes,
        *,
        kind: ArtifactKind = ArtifactKind.OTHER,
        name: str | None = None,
        mime: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Artifact:
        now = utcnow()
        folder = self.root / f"{now:%Y}" / f"{now:%m}"
        folder.mkdir(parents=True, exist_ok=True)
        filename = name or f"{uuid.uuid4().hex}"
        if "." not in filename and (suffix := _suffix_for(mime)):
            filename = f"{filename}{suffix}"
        target = folder / filename
        target.write_bytes(data)
        return Artifact.from_path(target, kind=kind, mime=mime, **(meta or {}))

    async def read(self, artifact: Artifact) -> bytes:
        if artifact.is_local:
            return artifact.path().read_bytes()
        raise ArtifactError(
            "本地产物仓无法读取远程产物，请使用服务端实现（支持 OSS/HTTP 回源）",
            detail={"uri": artifact.uri},
        )

    async def exists(self, artifact: Artifact) -> bool:
        return artifact.is_local and artifact.path().is_file()

    async def resolve_for_provider(self, artifact: Artifact, *, provider: str) -> str:
        # 远程产物本身就是供应商可拉取的
        if artifact.is_remote:
            return artifact.uri
        return artifact.to_data_url()


class StaticModelCatalog:
    """内存模型目录。用于脚本、测试，以及给服务端实现做行为基准。"""

    def __init__(self, models: Iterable[ModelSpec] = ()) -> None:
        self._models: dict[tuple[str, str], ModelSpec] = {}
        self._order: list[tuple[str, str]] = []
        for spec in models:
            self.add(spec)

    def add(self, spec: ModelSpec, *, replace: bool = False) -> ModelSpec:
        key = (spec.provider, spec.key)
        if key not in self._models:
            self._order.append(key)
        elif not replace:
            raise ValueError(f"模型重复：{spec.provider}/{spec.key}")
        self._models[key] = spec
        return spec

    async def list_models(
        self,
        *,
        capability: str | None = None,
        provider: str | None = None,
        enabled_only: bool = True,
    ) -> list[ModelSpec]:
        out = []
        for key in self._order:
            spec = self._models[key]
            if enabled_only and not spec.enabled:
                continue
            if capability and spec.capability != capability:
                continue
            if provider and spec.provider != provider:
                continue
            out.append(spec)
        return out

    async def get_model(self, provider: str, key: str) -> ModelSpec | None:
        return self._models.get((provider, key))

    async def resolve(
        self,
        capability: str,
        *,
        provider: str | None = None,
        model_key: str | None = None,
        policy: ResolvePolicy = ResolvePolicy.PRIORITY,
    ) -> ResolvedModel:
        if model_key:
            for spec in await self.list_models(capability=capability, provider=provider):
                if spec.key == model_key:
                    return ResolvedModel(capability=capability, model=spec, reason="显式指定")
            raise ModelNotResolved(
                f"指定的模型不可用：{provider or '*'}/{model_key}",
                detail={"capability": capability},
            )

        candidates = await self.list_models(capability=capability, provider=provider)
        if not candidates:
            raise ModelNotResolved(
                f"没有可用的模型承接能力 {capability}",
                detail={"capability": capability, "provider": provider},
            )

        if policy is ResolvePolicy.CHEAPEST:
            priced = [c for c in candidates if c.pricing.unit_price is not None]
            if priced:
                best = min(priced, key=lambda c: c.pricing.unit_price or 0)
                rest = [c for c in candidates if c is not best]
                return ResolvedModel(
                    capability=capability, model=best, reason="单价最低", fallbacks=rest
                )

        chosen, *rest = candidates
        return ResolvedModel(
            capability=capability, model=chosen, reason="按入库优先级", fallbacks=rest
        )

    async def form_schema(self, capability: str) -> ParamsSchema:
        models = await self.list_models(capability=capability)
        if not models:
            return ParamsSchema()
        return models[0].params


def _suffix_for(mime: str | None) -> str:
    if not mime:
        return ""
    return mimetypes.guess_extension(mime) or ""
