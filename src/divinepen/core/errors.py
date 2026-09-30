"""底座异常体系。

约定：
* 每一个异常都有稳定的 ``code`` 字符串，直接作为 API 错误响应与页面提示的判别键。
* 异常只携带结构化信息，不做展示层格式化（展示交给 `apps/server`）。
"""

from __future__ import annotations

from typing import Any


class DivinePenError(Exception):
    """所有底座异常的基类。"""

    code: str = "divinepen_error"

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail: dict[str, Any] = detail or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "detail": self.detail}

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        if not self.detail:
            return self.message
        return f"{self.message} | {self.detail}"


# ── 配置 / 环境 ────────────────────────────────────────────────────


class ConfigurationError(DivinePenError):
    """配置缺失或非法（含 .env、数据库中的配置项）。"""

    code = "configuration_error"


# ── 能力层 ─────────────────────────────────────────────────────────


class CapabilityError(DivinePenError):
    code = "capability_error"


class CapabilityNotFound(CapabilityError):
    """请求的 capability_id 未注册。"""

    code = "capability_not_found"


class PipelinePaused(CapabilityError):
    """可介入工作流停在人工闸口。

    不是失败：``detail`` 里带着 ``step_id`` / ``outputs`` / ``start_index`` /
    ``gate_prompt``，服务端据此把任务置为 AWAITING_INPUT，等 resume 带参数回来续跑。
    """

    code = "pipeline_paused"


class CapabilityInputError(CapabilityError):
    """输入未通过 Pydantic 校验。``detail['errors']`` 带字段级错误。"""

    code = "capability_input_error"


class CapabilityUnavailable(CapabilityError):
    """能力存在于注册表，但当前环境跑不了（缺 ffmpeg / 缺模型 / 未启用）。"""

    code = "capability_unavailable"


class CredentialMissing(CapabilityUnavailable):
    """缺 API Key。页面据此跳转到「配置管理」并引导用户填写。"""

    code = "credential_missing"


class ModelNotResolved(CapabilityUnavailable):
    """没有可用的模型能承接该能力（未入库 / 未启用 / 策略筛完为空）。"""

    code = "model_not_resolved"


# ── 计费闸门 ───────────────────────────────────────────────────────


class BilledCallBlocked(DivinePenError):
    """拦下一次会花钱的调用。

    这是"付费三道闸"的第三道：能力已 ``plan()`` 出将要发出的请求，
    但调用方没有显式授予付费许可。调用方应把 ``detail['plan']`` 展示给用户，
    用户确认后带 ``billed_allowed=True`` 重新调用。
    """

    code = "billed_call_blocked"


class BudgetExceeded(DivinePenError):
    code = "budget_exceeded"


# ── 供应商 / 传输 ──────────────────────────────────────────────────


class ProviderError(DivinePenError):
    code = "provider_error"


class ProviderRequestError(ProviderError):
    """HTTP 层失败（连接、4xx、5xx）。"""

    code = "provider_request_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request_id: str | None = None,
        raw: Any = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        payload = dict(detail or {})
        payload.update(
            {
                "status_code": status_code,
                "request_id": request_id,
                "raw": raw,
            }
        )
        super().__init__(message, detail=payload)
        self.status_code = status_code
        self.request_id = request_id
        self.raw = raw


class ProviderTaskFailed(ProviderError):
    """远端异步任务终态为失败。"""

    code = "provider_task_failed"


class ProviderTaskTimeout(ProviderError):
    code = "provider_task_timeout"


# ── 产物 / 存储 ────────────────────────────────────────────────────


class ArtifactError(DivinePenError):
    code = "artifact_error"


class ArtifactTooLarge(ArtifactError):
    """产物超出供应商可内联/可拉取的上限（如 base64 参考图 > 20MB）。"""

    code = "artifact_too_large"


class MediaToolMissing(CapabilityUnavailable):
    """外部二进制缺失（ffmpeg / ffprobe）。"""

    code = "media_tool_missing"
