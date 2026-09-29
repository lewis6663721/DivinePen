"""服务端运行时：把端口接到数据库 / 本地磁盘，并提供页面要用的组装与估算。

这里回答两个问题：

* **RunContext 从哪来**：``App.context()`` 用数据库实现的五个端口拼出来。
* **页面上的参数怎么变成请求**：``assemble_request()`` 按点号路径把扁平值还原成
  文档要求的嵌套请求体。这条路径与将来真实调用**共用同一份代码**，所以 dry-run
  里看到的 body 就是真发出去的 body，不是"另写一版给页面看的"。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from divinepen.core.artifact import Artifact, ArtifactKind
from divinepen.core.errors import CredentialMissing, ModelNotResolved
from divinepen.core.manifest import CostUnit
from divinepen.core.ports import (
    HttpRequest,
    ResolvePolicy,
)
from divinepen.core.records import TaskRef, UsageRecord
from divinepen.core.registry import CapabilityRegistry, build_default_registry
from divinepen.core.runtime import RunContext
from divinepen.core.specs import ModelSpec, ParamsSchema, ResolvedModel, TaskStatus
from divinepen.services.http import HttpxTransport

from .db import connect_store
from .settings import Settings
from .store import Repo, SecretBox, SessionSigner, mask_secret, now_iso

# ── 点号路径 ⇄ 嵌套字典 ────────────────────────────────────────────


def set_path(target: dict[str, Any], key: str, value: Any) -> None:
    node = target
    parts = key.split(".")
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def get_path(source: Any, key: str, default: Any = None) -> Any:
    node = source
    for part in key.split("."):
        if isinstance(node, dict):
            if part not in node:
                return default
            node = node[part]
        else:
            return default
    return node


def assemble_body(schema: ParamsSchema, values: dict[str, Any]) -> dict[str, Any]:
    """扁平值 → 嵌套请求体。**顺序按 schema 声明**，方便页面对照文档。"""
    body: dict[str, Any] = {}
    for field in schema.fields():
        if field.key not in values:
            continue
        value = values[field.key]
        if value is None:
            continue
        set_path(body, field.key, value)
    return body


# ── 端口实现 ───────────────────────────────────────────────────────


class DbCredentials:
    """``CredentialProvider`` 的数据库实现。缺 Key 时抛 CredentialMissing。"""

    def __init__(self, repo: Repo) -> None:
        self.repo = repo

    async def get(self, provider: str, *, workspace: str | None = None) -> str:
        key = await self.repo.raw_api_key(provider, workspace or "")
        if not key:
            raise CredentialMissing(
                f"供应商 {provider} 未配置 API Key", detail={"provider": provider}
            )
        return key

    async def has(self, provider: str, *, workspace: str | None = None) -> bool:
        return bool(await self.repo.raw_api_key(provider, workspace or ""))


class DbModelCatalog:
    """``ModelCatalog`` 的数据库实现。能力层的"用谁做"全部走这里。"""

    def __init__(self, repo: Repo) -> None:
        self.repo = repo
        self._base_urls: dict[str, str] | None = None

    async def _base_url(self, provider_code: str) -> str:
        if self._base_urls is None:
            rows = await self.repo.providers()
            self._base_urls = {row["code"]: row["base_url"] for row in rows}
        return self._base_urls.get(provider_code, "")

    async def _spec(self, row: dict[str, Any]) -> ModelSpec:
        return ModelSpec(
            provider=row["provider"],
            key=row["key"],
            capability=row["capability"],
            name=row["name"],
            base_url=await self._base_url(row["provider"]),
            enabled=bool(row["enabled"]),
            api={
                "method": row["method"],
                "path": row["path"],
                "headers": _loads(row["headers"]),
                "mode": row["mode"],
                "poll": _loads(row["poll"]),
            },
            params=ParamsSchema.model_validate(_loads(row["params"]) or {"groups": []}),
            defaults=_loads(row["defaults"]) or {},
            pricing=_loads(row["pricing"]) or {},
            notes=row["notes"],
            doc_url=row["doc_url"],
            meta={
                "source_doc": row["source_doc"],
                "verified": bool(row["verified"]),
                "transport": row["transport"],
                "priority": row["priority"],
            },
        )

    async def list_models(
        self,
        *,
        capability: str | None = None,
        provider: str | None = None,
        enabled_only: bool = True,
    ) -> list[ModelSpec]:
        rows = await self.repo.models(
            capability=capability, provider=provider, include_disabled=not enabled_only
        )
        return [await self._spec(row) for row in rows]

    async def get_model(self, provider: str, key: str) -> ModelSpec | None:
        row = await self.repo.model(f"{provider}:{key}")
        return await self._spec(row) if row else None

    async def resolve(
        self,
        capability: str,
        *,
        provider: str | None = None,
        model_key: str | None = None,
        policy: ResolvePolicy = ResolvePolicy.PRIORITY,
    ) -> ResolvedModel:
        candidates = await self.list_models(capability=capability, provider=provider)
        if model_key:
            candidates = [m for m in candidates if m.key == model_key]
        if not candidates:
            raise ModelNotResolved(
                f"没有可用的模型支持能力 {capability}",
                detail={"capability": capability, "model_key": model_key},
            )
        if policy is ResolvePolicy.EXPLICIT and not model_key:
            raise ModelNotResolved(
                f"能力 {capability} 要求显式指定模型", detail={"capability": capability}
            )
        ordered = sorted(candidates, key=_candidate_sort_key(policy))
        head, *rest = ordered
        reason = {
            ResolvePolicy.PRIORITY: "按目录优先级（priority）取最靠前的启用模型",
            ResolvePolicy.CHEAPEST: "按已知单价最低者优先；单价未登记时退回优先级",
            ResolvePolicy.EXPLICIT: "调用方显式指定的模型",
        }[policy]
        return ResolvedModel(capability=capability, model=head, reason=reason, fallbacks=rest)

    async def form_schema(self, capability: str) -> ParamsSchema:
        models = await self.list_models(capability=capability)
        if not models:
            return ParamsSchema(groups=[])
        return models[0].params


def _candidate_sort_key(policy: ResolvePolicy):
    if policy is ResolvePolicy.CHEAPEST:

        def key(model: ModelSpec) -> tuple[int, Decimal, int]:
            price = model.pricing.unit_price
            return (
                0 if price is not None else 1,
                price or Decimal(0),
                int(model.meta.get("priority", 100)),
            )

        return key
    return lambda model: int(model.meta.get("priority", 100))


class DbUsageSink:
    def __init__(self, repo: Repo) -> None:
        self.repo = repo

    async def record(self, record: UsageRecord) -> None:
        await self.repo.record_usage(
            {
                "capability_id": record.capability_id,
                "provider": record.provider,
                "model": record.model,
                "unit": record.unit.value,
                "quantity": float(record.quantity or 0),
                "amount": float(record.amount) if record.amount is not None else None,
                "currency": record.currency,
                "user_id": record.user_id,
                "task_id": record.task_id,
                "request_id": record.request_id,
                "at": record.at.isoformat(timespec="seconds"),
                "raw": record.raw,
            }
        )


class DbTaskSink:
    """``TaskSink``：提交即落库，远端 task_id 立刻持久化。"""

    def __init__(self, repo: Repo) -> None:
        self.repo = repo

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
        task_id = await self.repo.create_task(
            {
                "capability_id": capability_id,
                "provider": provider,
                "model": model,
                "status": status or TaskStatus.PENDING.value,
                "remote_task_id": remote_task_id,
                "payload": payload or {},
                "user_id": user_id,
            }
        )
        ref = await self.repo.task(task_id)
        return _to_task_ref(ref or {"id": task_id, "capability_id": capability_id})

    async def update(self, task_id: str, **patch: Any) -> TaskRef | None:
        await self.repo.update_task(task_id, **_flatten_task_patch(patch))
        ref = await self.repo.task(task_id)
        return _to_task_ref(ref) if ref else None

    async def get(self, task_id: str) -> TaskRef | None:
        ref = await self.repo.task(task_id)
        return _to_task_ref(ref) if ref else None

    async def active(self, *, capability_id: str | None = None) -> list[TaskRef]:
        rows = await self.repo.tasks(limit=200)
        active = [
            row
            for row in rows
            if TaskStatus(row["status"]).is_active
            and (capability_id is None or row["capability_id"] == capability_id)
        ]
        return [_to_task_ref(row) for row in active]


class LocalArtifactStore:
    """本地产物仓（``media/``）。一期不上 OSS，但接口照 OSS 的形状留好。"""

    def __init__(self, root: Path, *, oss_public_base: str = "") -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.oss_public_base = oss_public_base.rstrip("/")

    async def put(
        self,
        data: bytes,
        *,
        kind: ArtifactKind = ArtifactKind.OTHER,
        name: str | None = None,
        mime: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Artifact:
        import hashlib

        digest = hashlib.sha256(data).hexdigest()
        target = self.root / digest[:2] / (name or digest[:16])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return Artifact(
            uri=str(target.resolve()),
            kind=kind,
            mime=mime,
            size=len(data),
            sha256=digest,
            meta=meta or {},
        )

    async def read(self, artifact: Artifact) -> bytes:
        return artifact.path().read_bytes()

    async def exists(self, artifact: Artifact) -> bool:
        return artifact.is_local and artifact.path().is_file()

    async def resolve_for_provider(self, artifact: Artifact, *, provider: str) -> str:
        """供应商可达化。没有 OSS 就退回 base64 data URL —— 直白，且必然可拉取。"""
        if artifact.is_remote:
            return artifact.uri
        if self.oss_public_base:
            return urljoin(self.oss_public_base + "/", artifact.path().name)
        return artifact.to_data_url()


def _loads(value: Any) -> Any:
    from .db import loads

    return loads(value, None)


def _to_task_ref(row: dict[str, Any]) -> TaskRef:
    payload = row.get("payload_obj")
    if payload is None:
        payload = _loads(row.get("payload")) or {}
    result = row.get("result_obj")
    if result is None:
        result = _loads(row.get("result")) or {}
    return TaskRef(
        id=row["id"],
        capability_id=row.get("capability_id", ""),
        provider=row.get("provider", ""),
        model=row.get("model", ""),
        status=TaskStatus(row.get("status") or "UNKNOWN"),
        remote_task_id=row.get("remote_task_id"),
        progress=int(row.get("progress") or 0),
        payload=payload,
        result=result,
        user_id=row.get("user_id"),
    )


def _flatten_task_patch(patch: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in patch.items():
        if key in {"progress", "status", "remote_task_id", "user_id", "finished_at"}:
            out[key] = value
        elif key in {"payload", "result", "error"}:
            from .db import dumps

            out[key] = dumps(value)
        else:
            out[key] = value
    return out


# ── 组装与估算（页面 dry-run 与真实调用共用）─────────────────────


@dataclass(slots=True)
class RequestPreview:
    method: str
    url: str
    headers: dict[str, str]
    body: dict[str, Any]
    poll_url: str | None
    warnings: list[str]
    billing: list[dict[str, Any]]
    estimate: dict[str, Any]
    raw_headers: dict[str, str]

    @property
    def curl(self) -> str:
        """可复制的 curl。Authorization 一律脱敏 —— 页面上的东西可能被截图。"""
        parts = [f"curl -X {self.method} '{self.url}'"]
        for key, value in self.headers.items():
            shown = _mask_header(key, value)
            parts.append(f"  -H '{key}: {shown}'")
        if self.body:
            import json as _json

            parts.append("  -d '" + _json.dumps(self.body, ensure_ascii=False) + "'")
        return " \\\n".join(parts)


def _mask_header(key: str, value: str) -> str:
    if key.lower() in {"authorization", "x-api-key", "api-key"}:
        token = value.split(" ", 1)[-1]
        if token.startswith("$"):
            return value
        return f"{value.split(' ', 1)[0] if ' ' in value else 'Bearer'} {mask_secret(token)}"
    return value


def preview_request(
    model: dict[str, Any],
    values: dict[str, Any],
    *,
    base_url: str,
    api_key: str = "",
) -> RequestPreview:
    """把页面填的参数组装成"将要发出的请求"。

    **不联网**。URL/Header/Body 与真实调用完全一致，只有 Authorization 用占位符
    （有 Key 时用真实 Key，页面侧再脱敏显示）。
    """
    schema = ParamsSchema.model_validate(model["params_obj"])
    body = assemble_body(schema, values)
    headers = dict(model["headers_obj"])
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers.setdefault("Authorization", "Bearer $DIVINEPEN_ALIYUN_API_KEY")
    path = model["path"]
    url = path if path.startswith("http") else urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    warnings = _preview_warnings(model, values)
    poll = model.get("poll_obj") or None
    poll_url = None
    if poll:
        poll_url = urljoin(base_url.rstrip("/") + "/", str(poll.get("path", "")).lstrip("/"))
    return RequestPreview(
        method=model["method"],
        url=url,
        headers=headers,
        raw_headers=dict(model["headers_obj"]),
        body=body,
        poll_url=poll_url,
        warnings=warnings,
        billing=billing_fields(model, values),
        estimate=estimate_cost(model, values),
    )


def billing_fields(model: dict[str, Any], values: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for field in model.get("fields", []):
        if not field.get("billed"):
            continue
        out.append(
            {
                "key": field["key"],
                "label": field["label"],
                "value": values.get(field["key"], field.get("default")),
                "doc": field.get("description", ""),
            }
        )
    return out


def estimate_cost(model: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    """按已登记的计价口径估算。**单价没登记就直说"金额未知"**，不编。"""
    pricing = model.get("pricing_obj") or {}
    unit = pricing.get("unit", "free")
    quantity: Decimal | None = None
    if unit == CostUnit.SECOND.value:
        raw = values.get("parameters.duration") or 0
        try:
            quantity = Decimal(int(raw))
        except (TypeError, ValueError):
            quantity = None
    elif unit == CostUnit.IMAGE.value:
        try:
            quantity = Decimal(int(values.get("parameters.n") or 1))
        except (TypeError, ValueError):
            quantity = None
    price = pricing.get("unit_price")
    amount = None
    if quantity is not None and price:
        amount = (quantity * Decimal(str(price))).quantize(Decimal("0.0001"))
    return {
        "unit": unit,
        "quantity": None if quantity is None else str(quantity),
        "unit_price": price,
        "amount": None if amount is None else str(amount),
        "currency": pricing.get("currency", "CNY"),
        "notes": pricing.get("notes", ""),
        "known": amount is not None,
        "billed": unit != CostUnit.FREE.value,
    }


def _preview_warnings(model: dict[str, Any], values: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    if not model.get("enabled"):
        warnings.append("该模型当前为停用状态，真实调用会被拒绝。")
    meta_notes = []
    if not model.get("verified"):
        meta_notes.append("该模型的参数尚未逐条核对文档，字段可能变动。")
    if str(model.get("transport")) == "websocket":
        meta_notes.append("该模型走 WebSocket，当前一期未接入传输实现。")
    warnings.extend(meta_notes)
    media = values.get("input.media")
    if isinstance(media, list):
        for item in media:
            url = (item or {}).get("url", "") if isinstance(item, dict) else ""
            if url and not url.startswith(("http://", "https://", "data:")):
                warnings.append(
                    f"素材地址 {url!r} 不是供应商可拉取的 URL —— 阿里系会报 Failed to download。"
                )
    if str(model.get("mode")) == "async_task":
        warnings.append("异步任务：提交后需轮询 /api/v1/tasks/{task_id}，远端 task_id 会落库。")
    return warnings


# ── 应用容器 ───────────────────────────────────────────────────────


class App:
    """进程级单例：配置、存储、仓储、端口、能力注册表。"""

    def __init__(self, settings: Settings, store: Any) -> None:
        self.settings = settings
        self.store = store
        self.box = SecretBox(settings.credential_key)
        self.sessions = SessionSigner(settings.secret_key, settings.session_max_age_hours)
        self.repo = Repo(self.store, self.box)
        self.credentials = DbCredentials(self.repo)
        self.catalog = DbModelCatalog(self.repo)
        self.usage = DbUsageSink(self.repo)
        self.tasks = DbTaskSink(self.repo)
        self.artifacts = LocalArtifactStore(settings.media_root)
        self.http = HttpxTransport(timeout=120.0)
        try:
            self.registry: CapabilityRegistry = build_default_registry()
        except Exception:
            self.registry = CapabilityRegistry()

    @classmethod
    async def create(cls, settings: Settings) -> App:
        """存储连接是异步的，所以构造也必须是异步的。"""
        return cls(settings, await connect_store(settings.db_dsn))

    async def aclose(self) -> None:
        await self.http.aclose()
        await self.store.aclose()

    def context(
        self,
        *,
        dry_run: bool = True,
        billed_allowed: bool = False,
        user_id: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> RunContext:
        return RunContext(
            credentials=self.credentials,
            catalog=self.catalog,
            usage=self.usage,
            tasks=self.tasks,
            artifacts=self.artifacts,
            http=self.http,
            dry_run=dry_run,
            billed_allowed=billed_allowed,
            user_id=user_id,
            extra=extra or {},
        )

    async def base_url_for(self, provider_code: str) -> str:
        provider = await self.repo.provider(provider_code)
        return (provider or {}).get("base_url", "")


# ── 连通性测试（分级：先免费，再按策略最小真实请求）───────────────


async def probe_reachability(app: App, provider_code: str) -> dict[str, Any]:
    """**零成本**探活：DNS/TLS/HTTP 层能不能通。不验证 Key，也不产生任何计费。"""
    base_url = await app.base_url_for(provider_code)
    if not base_url:
        return {"ok": False, "kind": "reachability", "note": "供应商未配置 base_url"}
    started = time.perf_counter()
    try:
        resp = await app.http.request(HttpRequest(method="GET", url=base_url, timeout=15.0))
    except Exception as exc:
        return {
            "ok": False,
            "kind": "reachability",
            "note": f"网络不可达：{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    elapsed = int((time.perf_counter() - started) * 1000)
    return {
        "ok": True,
        "kind": "reachability",
        "status_code": resp.status_code,
        "elapsed_ms": elapsed,
        "note": f"网络可达（{resp.status_code}，{elapsed}ms）。此测试不验证 API Key，也不产生费用。",
    }


async def connectivity_chat(
    app: App, provider_code: str, model_key: str | None = None
) -> dict[str, Any]:
    """**最小真实请求**：一次文本 chat，max_tokens=1。

    政策（见 ``docs/ARCHITECTURE.md``）：允许的最小真实请求只走文本模型，
    **绝不**拿多模态模型做连通性测试 —— 那会真的烧钱。
    """
    settings = app.settings
    if not settings.allow_billed_calls:
        return {
            "ok": False,
            "kind": "live",
            "blocked": True,
            "note": "已关闭真实调用（DIVINEPEN_ALLOW_BILLED_CALLS=false）。"
            "这是默认值：连通性验证请先用零成本探活，或显式打开开关。",
        }
    model_key = model_key or settings.connectivity_model
    api_key = await app.repo.raw_api_key(provider_code)
    if not api_key:
        return {"ok": False, "kind": "live", "note": f"未配置 {provider_code} 的 API Key"}
    base_url = await app.base_url_for(provider_code)
    url = urljoin(base_url.rstrip("/") + "/", "compatible-mode/v1/chat/completions")
    started = time.perf_counter()
    try:
        resp = await app.http.request(
            HttpRequest(
                method="POST",
                url=url,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json_body={
                    "model": model_key,
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                    "stream": False,
                },
                timeout=30.0,
            )
        )
    except Exception as exc:
        return {
            "ok": False,
            "kind": "live",
            "note": f"请求失败：{type(exc).__name__}: {exc}",
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
        }
    elapsed = int((time.perf_counter() - started) * 1000)
    usage = (resp.body or {}).get("usage") if isinstance(resp.body, dict) else None
    ok = resp.ok
    if ok and usage:
        await app.repo.record_usage(
            {
                "capability_id": "llm.chat",
                "provider": provider_code,
                "model": model_key,
                "unit": CostUnit.TOKEN.value,
                "quantity": float(usage.get("total_tokens") or 0),
                "amount": None,
                "request_id": resp.request_id,
                "raw": {"purpose": "connectivity_test", "usage": usage},
                "at": now_iso(),
            }
        )
    return {
        "ok": ok,
        "kind": "live",
        "model": model_key,
        "status_code": resp.status_code,
        "elapsed_ms": elapsed,
        "request_id": resp.request_id,
        "usage": usage,
        "note": f"模型 {model_key} 返回 HTTP {resp.status_code}"
        + ("（Key 有效）" if ok else f"：{_error_message(resp.body)}"),
    }


def _error_message(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("message") or body.get("code") or body)[:200]
    return str(body)[:200]


__all__ = [
    "App",
    "DbCredentials",
    "DbModelCatalog",
    "DbTaskSink",
    "DbUsageSink",
    "HttpRequest",
    "LocalArtifactStore",
    "assemble_body",
    "connectivity_chat",
    "estimate_cost",
    "get_path",
    "preview_request",
    "probe_reachability",
    "set_path",
]
