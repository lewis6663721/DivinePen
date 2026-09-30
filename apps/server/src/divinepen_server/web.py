"""HTTP 层：Jinja2 服务端渲染 + 少量原生 JS。

**为什么不用 SPA**：这个后台的读者是运营和开发者，看的是"文档参数 ↔ 控件"的
一一对应。服务端渲染让 HTML 里直接带着后端算好的 plan、计费估算、缺失项，
不需要前端再实现一遍业务规则；规则只有一份，就不会对不上。

页面清单（对应需求）：
``/``           总览
``/capabilities`` 能力目录（来自能力注册表，含插件）
``/models``     模型管理（来自数据库，点号路径参数表单）
``/providers``  供应商
``/credentials`` 配置管理：API Key + 连通性测试、OSS
``/usage``      用量与费用
``/tasks``      任务中心
``/users``      用户管理
``/plugins``    插件管理
"""

from __future__ import annotations

import html
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from divinepen.agents import discover_plugins
from divinepen.core.specs import TaskStatus

from . import runtime as rt
from .catalog import category_label, category_sort
from .settings import Settings, get_settings
from .store import seed, verify_password

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

NAV: list[tuple[str, list[tuple[str, str, str]]]] = [
    (
        "工作台",
        [
            ("/", "总览", "dashboard"),
            ("/tasks", "任务中心", "tasks"),
        ],
    ),
    (
        "模型与能力",
        [
            ("/capabilities", "能力目录", "capabilities"),
            ("/models", "模型管理", "models"),
            ("/providers", "供应商", "providers"),
        ],
    ),
    (
        "系统",
        [
            ("/credentials", "配置管理", "credentials"),
            ("/usage", "用量与费用", "usage"),
            ("/users", "用户管理", "users"),
            ("/plugins", "插件管理", "plugins"),
        ],
    ),
]

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_JSON_TOKEN = re.compile(
    r'(?P<key>"(?:[^"\\]|\\.)*")(?P<colon>\s*:)|'
    r'(?P<str>"(?:[^"\\]|\\.)*")|'
    r"(?P<num>-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|"
    r"(?P<kw>\btrue\b|\bfalse\b|\bnull\b)"
)


def md_bold(text: Any) -> Markup:
    """文档摘录里的 ``**重点**`` → ``<b>``。先转义再替换，不引入 XSS。"""
    escaped = html.escape(str(text or ""))
    return Markup(_BOLD.sub(r"<b>\1</b>", escaped))


def json_hl(value: Any) -> Markup:
    """JSON 高亮。给"将要发出的请求"面板用——它是页面的主角，值得长得好一点。"""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return Markup(f"<pre class='code'>{html.escape(value)}</pre>")
    text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    escaped = html.escape(text)

    def repl(match: re.Match[str]) -> str:
        if match.group("key"):
            return f'<span class="k">{match.group("key")}</span>{match.group("colon")}'
        if match.group("str"):
            return f'<span class="s">{match.group("str")}</span>'
        if match.group("num"):
            return f'<span class="n">{match.group("num")}</span>'
        return f'<span class="k">{match.group("kw")}</span>'

    return Markup(_JSON_TOKEN.sub(repl, escaped))


def human_bytes(size: Any) -> str:
    if not size:
        return "-"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def money(amount: Any, currency: str = "CNY") -> str:
    if amount is None:
        return "金额未知"
    try:
        return f"{float(amount):.4f} {currency}"
    except (TypeError, ValueError):
        return str(amount)


#: 进程级共享状态：lifespan 里建，路由里读。测试可在同进程内替换。
state: dict[str, Any] = {}
#: create_app 时把本次配置存进来，供 lifespan 读取（lifespan 不能带额外参数）。
settings_ref: dict[str, Settings] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    instance = await rt.App.create(settings_ref["settings"])
    state["app"] = instance
    state["seed"] = await seed(instance.repo)
    try:
        yield
    finally:
        await instance.aclose()


def asset(name: str) -> str:
    """静态资源 URL 带文件 mtime 版本号。

    StaticFiles 不发 Cache-Control，浏览器会按 Last-Modified 做**启发式缓存** ——
    改了 CSS 用户却还看旧的。带上 mtime 后，文件一变 URL 就变，缓存自然失效；
    文件没变时 URL 不变，照样吃缓存。
    """
    path = STATIC_DIR / name
    try:
        version = int(path.stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{name}?v={version}"


def control_of(field: Any) -> str:
    """字段 → 页面控件。

    模板里拿到的字段有时是 ``FieldSpec`` 对象（能力页），有时是数据库反序列化出的
    dict（模型页）。dict 上没有 ``control`` 属性，直接 ``f.control`` 会得到
    Undefined 然后静默落到 text 分支 —— bool 就会变成文本框。这张表是唯一的映射。
    """
    if hasattr(field, "control"):
        return str(field.control)
    return _CONTROL_MAP.get(str(field.get("type", "text")), "input")


_CONTROL_MAP = {
    "text": "input",
    "textarea": "textarea",
    "int": "number",
    "float": "number",
    "bool": "switch",
    "enum": "select",
    "multi_enum": "checkbox-group",
    "url": "url-input",
    "json": "code-editor",
    "artifact": "media-picker",
    "artifact_list": "media-picker-multi",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings_ref["settings"] = settings
    app = FastAPI(
        title="DivinePen 控制台",
        version="0.1.0.dev0",
        docs_url="/api/docs",
        lifespan=lifespan,
    )
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["bold"] = md_bold
    templates.env.filters["jsonhl"] = json_hl
    templates.env.filters["bytes"] = human_bytes
    templates.env.filters["money"] = money
    templates.env.globals["control_of"] = control_of
    templates.env.globals["asset"] = asset

    # ── 公共 helpers ───────────────────────────────────────────

    def get_app() -> rt.App:
        instance = state.get("app")
        if instance is None:  # pragma: no cover - 仅在生命周期外调用时出现
            raise HTTPException(status_code=503, detail="服务尚未就绪")
        return instance

    def flash_url(path: str, message: str, level: str = "ok") -> str:
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}{urlencode({'msg': message, 'level': level})}"

    async def nav_counts(instance: rt.App) -> dict[str, Any]:
        counts = await instance.repo.task_counts()
        active = sum(n for s, n in counts.items() if TaskStatus(s).is_active)
        return {
            "tasks_active": active,
            "models": (await instance.repo.overview())["models"] or {},
            "flash_msg": None,
        }

    def ctx(request: Request, **extra: Any) -> dict[str, Any]:
        base = {
            "request": request,
            "nav": NAV,
            "path": request.url.path,
            "msg": request.query_params.get("msg"),
            "level": request.query_params.get("level", "ok"),
            "user": getattr(request.state, "user", None),
            "settings": settings,
        }
        base.update(extra)
        return base

    def render(request: Request, template: str, status_code: int = 200, **extra: Any):
        return templates.TemplateResponse(
            request, template, ctx(request, **extra), status_code=status_code
        )

    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
        request.state.user = None
        if settings.auth_required and not request.url.path.startswith(
            ("/static", "/login", "/healthz")
        ):
            instance = state.get("app")
            if instance is not None:
                payload = instance.sessions.verify(request.cookies.get("dp_session"))
                if payload:
                    request.state.user = await instance.repo.user(str(payload.get("uid", "")))
            if request.state.user is None:
                return RedirectResponse(flash_url("/login", "请先登录", "bill"), status_code=303)
        return await call_next(request)

    @app.exception_handler(Exception)
    async def _boom(request: Request, exc: Exception):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)
        return render(request, "error.html", status_code=500, title="出错了", detail=str(exc))

    # ── 健康检查 ───────────────────────────────────────────────

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        instance = get_app()
        return JSONResponse(
            {
                "ok": True,
                "db": instance.store.description,
                "media_root": str(settings.media_root),
                "capabilities": instance.registry.ids(),
                "encryption": instance.box.backend,
                "auth_required": settings.auth_required,
                "billed_calls": settings.allow_billed_calls,
            }
        )

    # ── 登录 ───────────────────────────────────────────────────

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return render(request, "login.html", title="登录")

    @app.post("/login")
    async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
        instance = get_app()
        user = await instance.repo.user_by_name(username.strip())
        if not user or not user["enabled"] or not verify_password(password, user["password_hash"]):
            return RedirectResponse(flash_url("/login", "用户名或密码不正确", "danger"), 303)
        token = instance.sessions.sign({"uid": user["id"], "u": user["username"]})
        response = RedirectResponse(flash_url("/", f"欢迎回来，{user['display_name']}"), 303)
        response.set_cookie(
            "dp_session",
            token,
            httponly=True,
            samesite="lax",
            max_age=settings.session_max_age_hours * 3600,
        )
        return response

    @app.post("/logout")
    async def logout():
        response = RedirectResponse(flash_url("/login", "已退出登录"), 303)
        response.delete_cookie("dp_session")
        return response

    # ── 总览 ───────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        instance = get_app()
        overview = await instance.repo.overview()
        usage = await instance.repo.usage_summary()
        tasks = await instance.repo.task_counts()
        credentials = await instance.repo.credentials()
        providers = {p["code"]: p for p in await instance.repo.providers()}
        models = await instance.repo.models(include_disabled=False)

        by_category: dict[str, list[dict[str, Any]]] = {}
        for model in models:
            by_category.setdefault(model["capability"], []).append(model)

        gaps: list[dict[str, str]] = []
        for row in await instance.repo.models():
            if not row["enabled"]:
                continue
            note = ""
            if not row["verified"]:
                note = "参数未逐条核对文档"
            if row["transport"] != "http":
                note = f"传输方式为 {row['transport']}，一期未接入"
            if note:
                gaps.append({"id": row["id"], "name": row["name"], "note": note})
            elif not any(c["provider"] == row["provider"] and c["has_key"] for c in credentials):
                gaps.append({"id": row["id"], "name": row["name"], "note": "供应商未配置 API Key"})

        return render(
            request,
            "dashboard.html",
            title="总览",
            overview=overview,
            usage=usage,
            task_counts=tasks,
            models_by_category=by_category,
            counts=await nav_counts(instance),
            capabilities=instance.registry.catalog(),
            credentials=credentials,
            providers=providers,
            gaps=gaps[:8],
            seed_result=state.get("seed", {}),
            encryption=instance.box.backend,
        )

    # ── 能力目录（注册表视角）──────────────────────────────────

    @app.get("/capabilities", response_class=HTMLResponse)
    async def capabilities(request: Request):
        instance = get_app()
        catalog = instance.registry.catalog()
        for item in catalog:
            cap = instance.registry.get(item["id"])
            item["missing"] = await cap.check_ready(instance.context())
            item["source"] = instance.registry.source_of(item["id"])
        return render(
            request,
            "capabilities.html",
            title="能力目录",
            catalog=catalog,
            counts=await nav_counts(instance),
        )

    @app.get("/capabilities/{capability_id}", response_class=HTMLResponse)
    async def capability_detail(request: Request, capability_id: str):
        instance = get_app()
        if not instance.registry.has(capability_id):
            return render(
                request,
                "error.html",
                status_code=404,
                title="找不到能力",
                detail=f"未注册的能力：{capability_id}",
                counts=await nav_counts(instance),
            )
        cap = instance.registry.get(capability_id)
        info = cap.info()
        return render(
            request,
            "capability.html",
            title=info["name"],
            cap=info,
            missing=await cap.check_ready(instance.context()),
            source=instance.registry.source_of(capability_id),
            result=None,
            values={},
            counts=await nav_counts(instance),
        )

    @app.post("/capabilities/{capability_id}/dry-run", response_class=HTMLResponse)
    async def capability_dry_run(request: Request, capability_id: str):
        instance = get_app()
        if not instance.registry.has(capability_id):
            return render(
                request,
                "error.html",
                status_code=404,
                title="找不到能力",
                detail=f"未注册的能力：{capability_id}",
            )
        cap = instance.registry.get(capability_id)
        info = cap.info()
        form = await request.form()
        raw_values = {k: v for k, v in form.items() if not k.startswith("_")}
        values = _form_values(form, cap.form_schema())
        result: dict[str, Any]
        try:
            run = await cap.invoke(
                values, instance.context(dry_run=True, extra={"registry": instance.registry})
            )
            result = {
                "kind": "ok",
                "status": run.status.value,
                "plan": run.plan.masked().model_dump(mode="json") if run.plan else None,
                "summary": run.plan.describe() if run.plan else "",
                "elapsed_ms": run.elapsed_ms,
            }
        except Exception as exc:
            detail = getattr(exc, "as_dict", None)
            result = {
                "kind": "error",
                "error": detail() if callable(detail) else {"message": str(exc)},
            }
        return render(
            request,
            "capability.html",
            title=info["name"],
            cap=info,
            missing=await cap.check_ready(instance.context()),
            source=instance.registry.source_of(capability_id),
            result=result,
            values=raw_values,
            counts=await nav_counts(instance),
        )

    # ── 模型管理（数据库视角）─────────────────────────────────

    @app.get("/models", response_class=HTMLResponse)
    async def models_page(request: Request):
        instance = get_app()
        capability = request.query_params.get("capability") or None
        provider = request.query_params.get("provider") or None
        rows = await instance.repo.models(capability=capability, provider=provider)
        credentials = {c["provider"]: c for c in await instance.repo.credentials()}
        providers = {p["code"]: p for p in await instance.repo.providers()}
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(row["capability"], []).append(row)
        ordered = sorted(grouped.items(), key=lambda kv: category_sort(kv[0]))
        return render(
            request,
            "models.html",
            title="模型管理",
            grouped=ordered,
            total=len(rows),
            credentials=credentials,
            providers=providers,
            categories=sorted(
                {row["capability"] for row in await instance.repo.models()},
                key=category_sort,
            ),
            provider_list=await instance.repo.providers(),
            selected={"capability": capability, "provider": provider},
            counts=await nav_counts(instance),
        )

    @app.get("/models/{model_id:path}", response_class=HTMLResponse)
    async def model_detail(request: Request, model_id: str):
        instance = get_app()
        model = await instance.repo.model(model_id)
        if not model:
            return render(
                request,
                "error.html",
                status_code=404,
                title="找不到模型",
                detail=f"库里没有这个模型：{model_id}",
                counts=await nav_counts(instance),
            )
        provider = await instance.repo.provider(model["provider"])
        credential = await instance.repo.credential(model["provider"])
        return render(
            request,
            "model.html",
            title=model["name"],
            model=model,
            provider=provider,
            credential=credential,
            capability_label=category_label(model["capability"]),
            preview=None,
            values={},
            counts=await nav_counts(instance),
        )

    @app.post("/models/{model_id:path}/preview", response_class=HTMLResponse)
    async def model_preview(request: Request, model_id: str):
        instance = get_app()
        model = await instance.repo.model(model_id)
        if not model:
            return render(
                request,
                "error.html",
                status_code=404,
                title="找不到模型",
                detail=f"库里没有这个模型：{model_id}",
            )
        form = await request.form()
        raw_values = {k: v for k, v in form.items() if not k.startswith("_")}
        # 按 schema 校验并补默认值：**未知键直接报错**，因为这一定意味着
        # 页面控件与文档参数对不上了，正是我们要暴露的问题。
        from divinepen.core.specs import ParamsSchema

        schema = ParamsSchema.model_validate(model["params_obj"])
        error = None
        normalized: dict[str, Any] = {}
        try:
            normalized = schema.validate_values(_form_values(form, schema))
        except ValueError as exc:
            error = str(exc)
        preview = None
        if error is None:
            api_key = ""
            if request.query_params.get("reveal") == "1":
                api_key = await instance.repo.raw_api_key(model["provider"])
            provider_row = await instance.repo.provider(model["provider"]) or {}
            preview = rt.preview_request(
                model,
                normalized,
                base_url=str(provider_row.get("base_url", "")),
                api_key=api_key,
            )
        return render(
            request,
            "model.html",
            title=model["name"],
            model=model,
            provider=await instance.repo.provider(model["provider"]),
            credential=await instance.repo.credential(model["provider"]),
            capability_label=category_label(model["capability"]),
            preview=preview,
            preview_error=error,
            values=raw_values,
            counts=await nav_counts(instance),
        )

    @app.post("/models/{model_id:path}/toggle")
    async def model_toggle(request: Request, model_id: str):
        instance = get_app()
        form = await request.form()
        enabled = form.get("enabled") == "1"
        await instance.repo.set_model_enabled(model_id, enabled)
        return RedirectResponse(
            flash_url(f"/models/{model_id}", f"已{'启用' if enabled else '停用'} {model_id}"), 303
        )

    @app.post("/models/{model_id:path}/default")
    async def model_set_default(request: Request, model_id: str):
        instance = get_app()
        form = await request.form()
        key = str(form.get("key") or "")
        raw = _coerce_json(form.get("value"))
        ok = await instance.repo.update_model_field_default(model_id, key, raw)
        level = "ok" if ok else "danger"
        note = f"已更新默认值 {key}" if ok else f"该模型没有参数 {key}"
        return RedirectResponse(flash_url(f"/models/{model_id}", note, level), 303)

    # ── 供应商 ─────────────────────────────────────────────────

    @app.get("/providers", response_class=HTMLResponse)
    async def providers_page(request: Request):
        instance = get_app()
        providers = await instance.repo.providers()
        credentials = {c["provider"]: c for c in await instance.repo.credentials()}
        all_models = await instance.repo.models()
        counts: dict[str, int] = {}
        for row in all_models:
            counts[row["provider"]] = counts.get(row["provider"], 0) + 1
        return render(
            request,
            "providers.html",
            title="供应商",
            providers=providers,
            credentials=credentials,
            model_counts=counts,
            counts=await nav_counts(instance),
        )

    @app.post("/providers/{code}/toggle")
    async def provider_toggle(request: Request, code: str):
        instance = get_app()
        form = await request.form()
        enabled = form.get("enabled") == "1"
        await instance.repo.set_provider_enabled(code, enabled)
        return RedirectResponse(
            flash_url("/providers", f"供应商 {code} 已{'启用' if enabled else '停用'}"), 303
        )

    # ── 配置管理（凭据 / OSS）──────────────────────────────────

    @app.get("/credentials", response_class=HTMLResponse)
    async def credentials_page(request: Request):
        instance = get_app()
        credentials = await instance.repo.credentials()
        by_provider = {c["provider"]: c for c in credentials}
        providers = await instance.repo.providers()
        return render(
            request,
            "credentials.html",
            title="配置管理",
            providers=providers,
            credential_map=by_provider,
            settings_map=await instance.repo.settings(),
            encryption=instance.box.backend,
            counts=await nav_counts(instance),
        )

    @app.post("/credentials/{provider}")
    async def credential_save(
        request: Request, provider: str, api_key: str = Form(""), workspace: str = Form("")
    ):
        instance = get_app()
        if not api_key.strip():
            return RedirectResponse(flash_url("/credentials", "API Key 不能为空", "danger"), 303)
        await instance.repo.save_credential(provider, api_key, workspace=workspace.strip())
        note = f"已保存 {provider} 的 API Key"
        if instance.box.backend != "fernet":
            note += "（注意：未配置 DIVINEPEN_CREDENTIAL_KEY，当前为明文存储）"
        return RedirectResponse(flash_url("/credentials", note, "bill"), 303)

    @app.post("/credentials/{provider}/probe")
    async def credential_probe(request: Request, provider: str):
        instance = get_app()
        outcome = await rt.probe_reachability(instance, provider)
        await instance.repo.record_credential_test(
            provider, bool(outcome.get("ok")), outcome.get("note", "")
        )
        return RedirectResponse(
            flash_url(
                "/credentials", outcome.get("note", ""), "ok" if outcome.get("ok") else "danger"
            ),
            303,
        )

    @app.post("/credentials/{provider}/test")
    async def credential_test(request: Request, provider: str, model: str = Form("")):
        instance = get_app()
        outcome = await rt.connectivity_chat(instance, provider, model.strip() or None)
        if not outcome.get("blocked"):
            await instance.repo.record_credential_test(
                provider, bool(outcome.get("ok")), outcome.get("note", "")
            )
        return RedirectResponse(
            flash_url(
                "/credentials",
                outcome.get("note", ""),
                "ok" if outcome.get("ok") else ("bill" if outcome.get("blocked") else "danger"),
            ),
            303,
        )

    @app.post("/settings/connectivity-model")
    async def set_connectivity_model(request: Request, value: str = Form(...)):
        instance = get_app()
        await instance.repo.set_setting("connectivity_model", value.strip())
        return RedirectResponse(flash_url("/credentials", "已更新连通性测试模型"), 303)

    @app.post("/settings/cost-threshold")
    async def set_cost_threshold(request: Request, value: str = Form(...)):
        instance = get_app()
        await instance.repo.set_setting("cost_alert_threshold", value.strip())
        return RedirectResponse(flash_url("/usage", "已更新费用告警阈值"), 303)

    # ── 用量与费用 ─────────────────────────────────────────────

    @app.get("/usage", response_class=HTMLResponse)
    async def usage_page(request: Request):
        instance = get_app()
        entries = await instance.repo.usage(limit=200)
        summary = await instance.repo.usage_summary()
        return render(
            request,
            "usage.html",
            title="用量与费用",
            entries=entries,
            summary=summary,
            pending=await instance.repo.pending_billed_count(),
            settings_map=await instance.repo.settings(),
            counts=await nav_counts(instance),
        )

    # ── 任务中心 ───────────────────────────────────────────────

    @app.get("/tasks", response_class=HTMLResponse)
    async def tasks_page(request: Request):
        instance = get_app()
        status = request.query_params.get("status") or None
        rows = await instance.repo.tasks(status=status, limit=200)
        return render(
            request,
            "tasks.html",
            title="任务中心",
            tasks=rows,
            task_counts=await instance.repo.task_counts(),
            active_task=await instance.repo.tasks(status="RUNNING", limit=1),
            selected_status=status,
            all_statuses=[s.value for s in TaskStatus],
            counts=await nav_counts(instance),
        )

    @app.get("/tasks/{task_id}", response_class=HTMLResponse)
    async def task_detail(request: Request, task_id: str):
        instance = get_app()
        row = await instance.repo.task(task_id)
        if not row:
            return render(
                request,
                "error.html",
                status_code=404,
                title="找不到任务",
                detail=f"任务 {task_id} 不存在",
            )
        return render(
            request,
            "task.html",
            title=f"任务 {task_id}",
            task=row,
            counts=await nav_counts(instance),
        )

    # ── 用户管理 ───────────────────────────────────────────────

    @app.get("/users", response_class=HTMLResponse)
    async def users_page(request: Request):
        instance = get_app()
        return render(
            request,
            "users.html",
            title="用户管理",
            users=await instance.repo.users(),
            counts=await nav_counts(instance),
        )

    @app.post("/users")
    async def user_create(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        display_name: str = Form(""),
        role: str = Form("user"),
    ):
        instance = get_app()
        username = username.strip()
        if len(password) < 6:
            return RedirectResponse(flash_url("/users", "密码至少 6 位", "danger"), 303)
        if await instance.repo.user_by_name(username):
            return RedirectResponse(flash_url("/users", f"用户名 {username} 已存在", "danger"), 303)
        await instance.repo.create_user(
            username, password, display_name=display_name.strip(), role=role
        )
        return RedirectResponse(flash_url("/users", f"已创建用户 {username}"), 303)

    @app.post("/users/{user_id}/toggle")
    async def user_toggle(request: Request, user_id: str):
        instance = get_app()
        form = await request.form()
        await instance.repo.set_user_enabled(user_id, form.get("enabled") == "1")
        return RedirectResponse(flash_url("/users", "已更新用户状态"), 303)

    @app.post("/users/{user_id}/role")
    async def user_role(request: Request, user_id: str, role: str = Form(...)):
        instance = get_app()
        await instance.repo.set_user_role(user_id, role)
        return RedirectResponse(flash_url("/users", "已更新角色"), 303)

    @app.post("/users/{user_id}/password")
    async def user_password(request: Request, user_id: str, password: str = Form(...)):
        instance = get_app()
        if len(password) < 6:
            return RedirectResponse(flash_url("/users", "密码至少 6 位", "danger"), 303)
        await instance.repo.set_user_password(user_id, password)
        return RedirectResponse(flash_url("/users", "已重置密码"), 303)

    # ── 插件管理 ───────────────────────────────────────────────

    @app.get("/plugins", response_class=HTMLResponse)
    async def plugins_page(request: Request):
        instance = get_app()
        try:
            plugins = discover_plugins()
        except Exception as exc:
            plugins = []
            plugin_error = f"{type(exc).__name__}: {exc}"
        else:
            plugin_error = None
        described = [
            {
                "manifest": p.manifest.model_dump(mode="json"),
                "missing": p.missing_requirements(instance.registry),
            }
            for p in plugins
        ]
        return render(
            request,
            "plugins.html",
            title="插件管理",
            plugins=described,
            plugin_error=plugin_error,
            registry_ids=instance.registry.ids(),
            counts=await nav_counts(instance),
        )

    # ── 目录重新导入 ───────────────────────────────────────────

    @app.post("/catalog/reseed")
    async def catalog_reseed(request: Request, force: str = Form("0")):
        instance = get_app()
        outcome = await seed(instance.repo, force=force == "1")
        models = outcome["models"]
        note = (
            f"目录导入完成：新增 {models['inserted']}、更新 {models['updated']}、"
            f"跳过 {models['skipped']}"
        )
        level = "bill" if force == "1" else "ok"
        return RedirectResponse(flash_url("/models", note, level), 303)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


def _form_values(form: Any, schema: Any) -> dict[str, Any]:
    """表单 → schema 输入值。

    两条必须在这里处理的语义：

    * 空字符串 = 未填 → 丢掉。留空不等于"传空串"，否则必填校验与默认值都会失效。
    * 未勾选的 checkbox **不会**出现在表单里 → 显式补 ``False``。
      否则用户取消勾选一个默认开启的开关（比如 prompt_extend），服务端会
      因为"没收到这个键"而用回默认的 True —— 界面和实际请求就对不上了。
    """
    values = {
        key: _coerce_json(value)
        for key, value in form.items()
        if not key.startswith("_") and value != ""
    }
    for field in schema.fields():
        if field.type.value == "bool" and field.key not in values:
            values[field.key] = False
    return values


def _coerce_json(raw: Any) -> Any:
    """页面里的 JSON 参数是字符串，这里尽力还原；还原不了就当普通字符串。"""
    if not isinstance(raw, str):
        return raw
    text = raw.strip()
    if not text:
        return ""
    if text[0] in "[{":
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return raw
    return raw


app = create_app()
