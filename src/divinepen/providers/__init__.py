"""L2 集成层：供应商适配器。

每个供应商一个模块，职责只有三件事：

1. **鉴权**：从 ``ctx.credentials`` 取 Key，拼标准请求头（绝不自己读环境变量）。
2. **参数映射**：把 ``ParamsSchema`` 的扁平值 → 供应商的嵌套请求体。
   参考图必须先过 ``ctx.artifacts.resolve_for_provider()``。
3. **响应归一**：把各自的 task_status / 错误结构，翻译成统一的
   ``TaskStatus`` / ``ProviderRequestError``。

不允许出现的东西：业务判断、重试写入库、直接 new httpx client。
传输一律走 ``ctx.http``（：class:`~divinepen.core.ports.HttpTransport`）。

首批实现（P2）：``aliyun``（DashScope / 百炼）。
"""

__all__: list[str] = []
