"""端口（Protocols）：能力与平台之间的唯一插座。

**这是底座能被复用的关键。** 能力层只面向这些 Protocol 编程：

* 跑在脚本里  → 注入内存实现（``divinepen.services.memory``）
* 跑在服务里  → 注入数据库 / OSS / Redis 实现（``apps/server/src/divinepen_server/ports``）

因此 ``pip install divinepen`` 得到的原子能力**零 Web、零 ORM** 依赖。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from .artifact import Artifact, ArtifactKind
from .records import TaskRef, UsageRecord
from .specs import ModelSpec, ParamsSchema, ResolvedModel

# ── 凭据 ───────────────────────────────────────────────────────────


@runtime_checkable
class CredentialProvider(Protocol):
    """取供应商 API Key。缺失时必须抛 ``errors.CredentialMissing``，页面据此引导配置。"""

    async def get(self, provider: str, *, workspace: str | None = None) -> str: ...

    async def has(self, provider: str, *, workspace: str | None = None) -> bool: ...


# ── 模型目录 ───────────────────────────────────────────────────────


class ResolvePolicy(StrEnum):
    PRIORITY = "priority"  # 按入库顺序/优先级
    CHEAPEST = "cheapest"  # 单价最低
    EXPLICIT = "explicit"  # 必须指定 model_key，找不到就报错，不兜底


@runtime_checkable
class ModelCatalog(Protocol):
    """模型注册表的读取面。写入面在服务层，能力层看不到。"""

    async def list_models(
        self,
        *,
        capability: str | None = None,
        provider: str | None = None,
        enabled_only: bool = True,
    ) -> list[ModelSpec]: ...

    async def get_model(self, provider: str, key: str) -> ModelSpec | None: ...

    async def resolve(
        self,
        capability: str,
        *,
        provider: str | None = None,
        model_key: str | None = None,
        policy: ResolvePolicy = ResolvePolicy.PRIORITY,
    ) -> ResolvedModel: ...

    async def form_schema(self, capability: str) -> ParamsSchema: ...


# ── 记账 / 任务 / 产物 ─────────────────────────────────────────────


@runtime_checkable
class UsageSink(Protocol):
    async def record(self, record: UsageRecord) -> None: ...


@runtime_checkable
class TaskSink(Protocol):
    """异步任务台账。**提交即落库**：拿到远端 task_id 立刻写，再继续。"""

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
    ) -> TaskRef: ...

    async def update(self, task_id: str, **patch: Any) -> TaskRef | None: ...

    async def get(self, task_id: str) -> TaskRef | None: ...

    async def active(self, *, capability_id: str | None = None) -> list[TaskRef]:
        """未到终态的任务。服务重启后据此续查，不丢在途任务。"""
        ...


@runtime_checkable
class ArtifactStore(Protocol):
    async def put(
        self,
        data: bytes,
        *,
        kind: ArtifactKind = ArtifactKind.OTHER,
        name: str | None = None,
        mime: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Artifact: ...

    async def read(self, artifact: Artifact) -> bytes: ...

    async def exists(self, artifact: Artifact) -> bool: ...

    async def resolve_for_provider(self, artifact: Artifact, *, provider: str) -> str:
        """把产物变成**该供应商可拉取**的 URL。

        阿里系要求参考图是公网 URL / OSS 临时 URL / base64 data URL；
        本地相对路径会导致 ``Failed to download``。所有供应商适配器在
        拼请求前必须走这里，不得自己拼路径。
        """
        ...


# ── 传输 ───────────────────────────────────────────────────────────


class HttpRequest(BaseModel):
    method: str = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    json_body: Any = None
    data: dict[str, Any] | None = None
    files: list[tuple[str, Any]] | None = None
    timeout: float | None = None


class HttpResponse(BaseModel):
    status_code: int
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    text: str = ""
    #: 原始字节。二进制产物（图片/视频）走这里，不参与序列化。
    content: bytes | None = Field(default=None, exclude=True, repr=False)
    request_id: str | None = None
    elapsed_ms: int | None = None
    url: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> Any:
        return self.body


@runtime_checkable
class HttpTransport(Protocol):
    """HTTP 出口。所有供应商请求都经过它，便于统一超时/重试/埋点/测试替身。"""

    async def request(self, req: HttpRequest) -> HttpResponse: ...
