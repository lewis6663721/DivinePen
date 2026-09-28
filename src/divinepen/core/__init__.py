"""L0 契约层：零第三方依赖（仅 pydantic），是整个底座的承重墙。

对外只暴露"语言"，不暴露"实现"：

* :class:`Capability` / :class:`CapabilityManifest` —— 做什么
* :class:`ModelSpec` / :class:`ParamsSchema` / :class:`ApiSpec` —— 用谁做、参数长什么样
* :class:`Artifact` —— 产物怎么在能力之间流转
* :class:`RunContext` —— 运行时看到的一切（端口注入）
* ``ports`` —— 能力与平台之间的插座
* ``errors`` —— 稳定的错误码
"""

from .artifact import Artifact, ArtifactKind
from .capability import (
    Capability,
    CapabilityOutcome,
    Plan,
    PlannedRequest,
    RequestKind,
    RunResult,
    RunStatus,
)
from .errors import (
    ArtifactError,
    ArtifactTooLarge,
    BilledCallBlocked,
    BudgetExceeded,
    CapabilityError,
    CapabilityInputError,
    CapabilityNotFound,
    CapabilityUnavailable,
    ConfigurationError,
    CredentialMissing,
    DivinePenError,
    MediaToolMissing,
    ModelNotResolved,
    ProviderError,
    ProviderRequestError,
    ProviderTaskFailed,
    ProviderTaskTimeout,
)
from .manifest import (
    CapabilityManifest,
    CapabilityStatus,
    Category,
    CostModel,
    CostUnit,
    Requirement,
)
from .paths import dotted_get, dotted_set, flatten_to_dotted, nest_from_dotted, redact
from .ports import (
    ArtifactStore,
    CredentialProvider,
    HttpRequest,
    HttpResponse,
    HttpTransport,
    ModelCatalog,
    ResolvePolicy,
    TaskSink,
    UsageSink,
)
from .records import TaskRef, UsageRecord, utcnow
from .registry import CapabilityRegistry, build_default_registry
from .runtime import RunContext
from .specs import (
    ApiSpec,
    ArtifactPick,
    FieldSpec,
    FieldType,
    GroupSpec,
    ModelSpec,
    Option,
    ParamsSchema,
    PollSpec,
    Pricing,
    ResolvedModel,
    TaskStatus,
)

__all__ = [  # noqa: RUF022 —— 按语义分组排列，比字典序更好读
    # 能力
    "Capability",
    "CapabilityOutcome",
    "CapabilityManifest",
    "CapabilityRegistry",
    "CapabilityStatus",
    "Category",
    "CostModel",
    "CostUnit",
    "Requirement",
    "Plan",
    "PlannedRequest",
    "RequestKind",
    "RunResult",
    "RunStatus",
    # 模型与参数
    "ApiSpec",
    "ArtifactPick",
    "FieldSpec",
    "FieldType",
    "GroupSpec",
    "ModelSpec",
    "Option",
    "ParamsSchema",
    "PollSpec",
    "Pricing",
    "ResolvedModel",
    "ResolvePolicy",
    "TaskStatus",
    # 产物与记录
    "Artifact",
    "ArtifactKind",
    "TaskRef",
    "UsageRecord",
    "utcnow",
    # 运行时与端口
    "ArtifactStore",
    "CredentialProvider",
    "HttpRequest",
    "HttpResponse",
    "HttpTransport",
    "ModelCatalog",
    "RunContext",
    "TaskSink",
    "UsageSink",
    # 工具
    "build_default_registry",
    "dotted_get",
    "dotted_set",
    "flatten_to_dotted",
    "nest_from_dotted",
    "redact",
    # 错误
    "ArtifactError",
    "ArtifactTooLarge",
    "BilledCallBlocked",
    "BudgetExceeded",
    "CapabilityError",
    "CapabilityInputError",
    "CapabilityNotFound",
    "CapabilityUnavailable",
    "ConfigurationError",
    "CredentialMissing",
    "DivinePenError",
    "MediaToolMissing",
    "ModelNotResolved",
    "ProviderError",
    "ProviderRequestError",
    "ProviderTaskFailed",
    "ProviderTaskTimeout",
]
