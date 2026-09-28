"""L3 服务层：端口的默认实现 + 开箱即用的引导函数。

这一层提供"不配数据库也能跑"的默认装配：内存台账 + 本地产物仓 + 环境变量凭据。
服务端（``apps/server``）会用数据库实现替换掉它们，**能力代码一行都不用改**。
"""

from .bootstrap import list_capabilities, local_context, run
from .http import HttpxTransport
from .memory import (
    EnvCredentialProvider,
    InMemoryTaskSink,
    InMemoryUsageSink,
    LocalArtifactStore,
    StaticModelCatalog,
)

__all__ = [
    "EnvCredentialProvider",
    "HttpxTransport",
    "InMemoryTaskSink",
    "InMemoryUsageSink",
    "LocalArtifactStore",
    "StaticModelCatalog",
    "list_capabilities",
    "local_context",
    "run",
]
