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
from divinepen.core.ports import HttpRequest, ResolvePolicy
from divinepen.core.specs import FieldType, ParamsSchema, TaskStatus

from . import modelform as mf
from . import progress
from . import runtime as rt
from .catalog import CATEGORY_LABELS, broad_label, broad_sort, category_label, category_sort
from .db import dumps
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

#: 一级目录（三大分区）的门面。名称、一句话定位、说明只写在这里一次 ——
#: 一级页的卡片、二级页的标题、二级页的页签全引用它，改文案不用满仓库找。
KIND_META: dict[str, dict[str, str]] = {
    "toolkit": {
        "label": "AI 工具包",
        "glyph": "◈",
        "tagline": "必须调模型 API 才能完成",
        "desc": "文生图 / 文生视频 / 对话 / 语音……参数 schema 取自接口文档，逐字对应。"
        "预演零成本（只看请求体），真实调用走三道闸。",
    },
    "editing": {
        "label": "剪辑",
        "glyph": "▤",
        "tagline": "硬代码，主要是 ffmpeg",
        "desc": "拼接、裁剪、画中画、字幕烧录、音轨分离。本地执行、零成本、可以真的跑出文件。",
    },
    "agent": {
        "label": "智能体",
        "glyph": "◉",
        "tagline": "以插件形式安装的业务能力",
        "desc": "pipeline = 流程写死、后台异步跑完返回结果；interactive = 每步之间允许人工介入；"
        "react = 基于 agentscope 的推理循环。",
    },
}


def _short_reason(text: str) -> str:
    """把「未配置供应商 video.t2v 的 API Key」压成「未配 API Key」。

    一级页只回答"缺的是哪一类东西"。八个能力各写一条 API Key 提示，在一张卡片上
    全是噪音；真要修哪一个，二级页的能力卡片上写着呢。
    """
    low = text.lower()
    if "api key" in low:
        return "未配 API Key"
    if "libass" in low or "ffmpeg" in low:
        return "ffmpeg 缺 libass"
    if "agentscope" in low:
        return "缺 agentscope"
    return text


def _tally(items: Any) -> dict[str, int]:
    out: dict[str, int] = {}
    for item in items:
        out[item] = out.get(item, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: (-kv[1], kv[0])))


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
#: 后台执行中的 asyncio 任务引用集合：不持有引用会被 GC 中途回收。
_BG: set[Any] = set()
#: 任务台账 id → 正在跑它的 asyncio 任务。取消按钮要凭这个找到人。
_RUNNING: dict[str, Any] = {}
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
    "range": "range",
    "bool": "switch",
    "enum": "select",
    "multi_enum": "checkbox-group",
    "combo": "combo",
    "url": "url-input",
    "json": "code-editor",
    "color": "color",
    "artifact": "media-picker",
    "artifact_list": "media-picker-multi",
}

#: 媒体参数能引用的文件类型。剪辑能力吃视频/音频/字幕，模型能力吃图片/音频。
_MEDIA_EXT = frozenset(
    {
        ".mp4",
        ".mov",
        ".mkv",
        ".webm",
        ".avi",
        ".flv",
        ".m4v",
        ".mp3",
        ".wav",
        ".m4a",
        ".aac",
        ".flac",
        ".ogg",
        ".opus",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".bmp",
        ".gif",
        ".tiff",
        ".srt",
        ".ass",
        ".vtt",
        ".txt",
        ".md",
    }
)
_MEDIA_CACHE: dict[str, Any] = {"at": 0.0, "items": []}


def media_library(limit: int = 200) -> list[dict[str, str]]:
    """媒体参数的「产物库」：本机已有的视频/音频/图片/字幕。

    媒体类参数不该逼用户手打路径 —— 但他也不该只能从库里挑（公网 URL、
    OSS 临时链接都是合法输入）。所以页面给的是"既能挑、也能填"。

    路径按**相对于仓库根**给出，和 ``media.trim`` 之类能力实际接收的写法一致
    （``var/sample.mp4``），免得用户从这里挑完，能力那边又找不到文件。

    结果缓存 5 秒：渲染一页会问很多次，但它不是需要实时的数据。
    """
    import time

    now = time.time()
    if now - _MEDIA_CACHE["at"] < 5.0 and _MEDIA_CACHE["items"]:
        return _MEDIA_CACHE["items"][:limit]
    root = Path(get_settings().root)
    roots = [get_settings().media_root, root / "var"]
    found: list[tuple[float, dict[str, str]]] = []
    for base in roots:
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in _MEDIA_EXT:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            try:
                rel = str(path.relative_to(root))
            except ValueError:
                rel = str(path)
            found.append((stat.st_mtime, {"name": path.name, "path": rel}))
    found.sort(key=lambda item: item[0], reverse=True)
    items = [item[1] for item in found]
    _MEDIA_CACHE.update({"at": now, "items": items})
    return items[:limit]


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
    templates.env.globals["media_library"] = media_library

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

    def nav_group_of(path: str) -> str | None:
        """当前页属于侧栏哪一个分组 —— 那个分组要自动展开、并高亮标题行。"""
        for group, items in NAV:
            for href, _label, _key in items:
                if path == href or (href != "/" and path.startswith(href + "/")):
                    return group
        return None

    # ── 面包屑自动生成 ──────────────────────────────────────────
    # 根据 URL 路径推断面包屑层级。每个页面不用单独传 crumbs，
    # 只要路径遵循 ``/section`` 或 ``/section/detail`` 的惯例就能自动拼出来。
    crumb_labels: dict[str, str] = {
        "capabilities": "能力目录",
        "models": "模型管理",
        "credentials": "配置管理",
        "providers": "供应商",
        "usage": "用量与费用",
        "tasks": "任务中心",
        "users": "用户管理",
        "plugins": "插件管理",
        "login": "登录",
        "logout": "退出",
    }

    def auto_crumbs(path: str, title: str) -> list[dict[str, str]]:
        crumbs: list[dict[str, str]] = [{"label": "DivinePen", "href": "/"}]
        parts = [p for p in path.strip("/").split("/") if p]
        if not parts:
            return crumbs
        # 第一段 = 分区
        section = parts[0]
        label = crumb_labels.get(section, section)
        if len(parts) == 1:
            # 分区首页（如 /models）：不链自己
            crumbs.append({"label": label, "href": ""})
        else:
            # 有子级：分区变成链接
            crumbs.append({"label": label, "href": f"/{section}"})
            # 第二段 = 详情
            detail = parts[1]
            # 特殊后缀处理：/edit
            if len(parts) >= 3 and parts[-1] == "edit":
                crumbs.append({"label": detail, "href": f"/{section}/{detail}"})
                crumbs.append({"label": "编辑", "href": ""})
            else:
                crumbs.append({"label": detail, "href": ""})
        return crumbs

    def ctx(request: Request, **extra: Any) -> dict[str, Any]:
        title = extra.get("title", "")
        crumbs = extra.pop("crumbs", None) or auto_crumbs(request.url.path, title)
        base = {
            "request": request,
            "nav": NAV,
            "nav_group": nav_group_of(request.url.path),
            "path": request.url.path,
            "msg": request.query_params.get("msg"),
            "level": request.query_params.get("level", "ok"),
            "user": getattr(request.state, "user", None),
            "settings": settings,
            "crumbs": crumbs,
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
    async def capabilities(request: Request, zone: str | None = None):
        """能力目录 = 两级，不在一屏里平铺 16 张卡片。

        ``/capabilities`` 只回答"有哪几个区、各区多少个"；点进去
        ``/capabilities?zone=toolkit`` 才列该区的能力。三区各有各的判断标准
        （要不要 Key、要不要 ffmpeg、装没装插件），挤在一屏反而看不清。
        """
        instance = get_app()
        by_kind = instance.registry.by_kind()
        for group in by_kind.values():
            for item in group:
                cap = instance.registry.get(item["id"])
                item["missing"] = await cap.check_ready(instance.context())

        # 按大类分组（图片/视频/音频/文本）—— 能力目录二级页用
        toolkit_groups: list[dict[str, Any]] = []
        raw_toolkit = by_kind.get("toolkit", [])
        buckets: dict[str, list[dict[str, Any]]] = {}
        for item in raw_toolkit:
            prefix = item["id"].split(".", 1)[0]
            item["subcategory"] = category_label(item.get("model_capability") or item["id"])
            buckets.setdefault(prefix, []).append(item)
        for prefix in sorted(buckets, key=broad_sort):
            toolkit_groups.append(
                {
                    "prefix": prefix,
                    "label": broad_label(prefix + "."),
                    "caps": buckets[prefix],
                }
            )

        zones: list[dict[str, Any]] = []
        for key, meta in KIND_META.items():
            items = by_kind.get(key, [])
            zones.append(
                {
                    "key": key,
                    **meta,
                    "total": len(items),
                    "ready": sum(1 for i in items if not i["missing"]),
                    "missing": sum(len(i["missing"]) for i in items),
                    "missing_reasons": _tally(
                        _short_reason(m) for i in items for m in i["missing"]
                    ),
                    "model_kinds": len(
                        {i["model_capability"] for i in items if i.get("model_capability")}
                    ),
                    "modes": {
                        mode: sum(1 for i in items if i.get("agent_mode") == mode)
                        for mode in ("pipeline", "interactive", "react")
                    },
                    "sample": [i["name"] for i in items[:3]],
                }
            )
        current = next((z for z in zones if z["key"] == zone), None)
        return render(
            request,
            "capabilities.html",
            title="能力目录" if current is None else f"能力目录 · {current['label']}",
            zones=zones,
            zone=current,
            by_kind=by_kind,
            toolkit_groups=toolkit_groups,
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
        raw = {key: value for key, value in request.query_params.items() if not key.startswith("_")}
        # 只带路由键的 URL = "打开这一页"，不是"提交一次表单"。
        picker, values, _ = await _capability_input(
            instance, cap, raw, submitted=any(k not in ROUTE_KEYS for k in raw)
        )
        info = cap.info()
        if picker is not None:
            info["form"] = picker["form"]
        return render(
            request,
            "capability.html",
            title=info["name"],
            cap=info,
            picker=picker,
            missing=await cap.check_ready(instance.context()),
            source=instance.registry.source_of(capability_id),
            result=None,
            values=values,
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
        form = await request.form()
        raw_values = {k: v for k, v in form.items() if not k.startswith("_")}
        picker, values, payload = await _capability_input(instance, cap, raw_values, submitted=True)
        info = cap.info()
        if picker is not None:
            info["form"] = picker["form"]
        result: dict[str, Any]
        try:
            run = await cap.invoke(
                payload, instance.context(dry_run=True, extra={"registry": instance.registry})
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
            picker=picker,
            missing=await cap.check_ready(instance.context()),
            source=instance.registry.source_of(capability_id),
            result=result,
            values=values,
            counts=await nav_counts(instance),
        )

    # ── 模型管理（数据库视角）─────────────────────────────────

    @app.get("/models", response_class=HTMLResponse)
    async def models_page(request: Request):
        instance = get_app()
        capability = request.query_params.get("capability") or None
        provider = request.query_params.get("provider") or None
        rows = await instance.repo.models(capability=capability, provider=provider)
        credentials = await instance.repo.default_credentials()
        providers = {p["code"]: p for p in await instance.repo.providers()}

        # 两级分组：大类（图片/视频/音频/文本）→ 子分类（文生图/图生图…）
        cap_buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            cap_buckets.setdefault(row["capability"], []).append(row)

        broad_buckets: dict[str, dict[str, Any]] = {}
        for cap_key, cap_rows in sorted(cap_buckets.items(), key=lambda kv: category_sort(kv[0])):
            prefix = cap_key.split(".", 1)[0]
            if prefix not in broad_buckets:
                broad_buckets[prefix] = {
                    "prefix": prefix,
                    "label": broad_label(cap_key),
                    "subs": [],
                }
            broad_buckets[prefix]["subs"].append(
                {
                    "capability": cap_key,
                    "label": category_label(cap_key),
                    "models": cap_rows,
                }
            )

        grouped = sorted(broad_buckets.values(), key=lambda g: broad_sort(g["prefix"]))
        # 分类下拉：能力分类键 → 中文名
        all_caps = {row["capability"] for row in await instance.repo.models()}
        categories = sorted(
            [{"key": c, "label": category_label(c)} for c in all_caps],
            key=lambda x: category_sort(x["key"]),
        )

        return render(
            request,
            "models.html",
            title="模型管理",
            grouped=grouped,
            total=len(rows),
            credentials=credentials,
            providers=providers,
            categories=categories,
            provider_list=await instance.repo.providers(),
            selected={"capability": capability, "provider": provider},
            counts=await nav_counts(instance),
        )

    # ── 模型的增 / 改 / 删 ─────────────────────────────────────
    # 目录是 seed 进来的，但目录不可能覆盖全部现实：自建服务、供应商刚上线的
    # 型号、内部代理，都得有个地方登记。所以这里给全量 CRUD，而不是只让页面
    # 开关一下。两个刻意留窄的口子：
    #   ① 身份（provider:key）不可改 —— 改了就是另一个模型，任务记录、
    #      计费口径、"这个能力的默认模型"全挂在它身上；
    #   ② 被任务记录引用过的模型不能删 —— 只能停用。删掉它，历史任务就会
    #      显示一个查不到的模型名，账单对不上账常常就是从这种"少了一行"开始的。

    async def model_form_ctx(instance: rt.App) -> dict[str, Any]:
        # 能力分类下拉：合并库里已有的 + catalog 里定义的，去重后按大类分组
        db_caps = set(await instance.repo.capabilities())
        all_caps = db_caps | set(CATEGORY_LABELS.keys())
        # 按 broad category 分组
        cap_groups: dict[str, list[dict[str, str]]] = {}
        for cap_key in sorted(all_caps, key=category_sort):
            prefix = cap_key.split(".", 1)[0]
            if prefix not in cap_groups:
                cap_groups[prefix] = []
            cap_groups[prefix].append(
                {
                    "key": cap_key,
                    "label": category_label(cap_key),
                }
            )
        capabilities_grouped = sorted(
            [
                {"prefix": k, "broad": broad_label(k + "."), "caps": v}
                for k, v in cap_groups.items()
            ],
            key=lambda g: broad_sort(g["prefix"]),
        )
        return {
            "providers": await instance.repo.providers(),
            "capabilities": sorted(all_caps),
            "capabilities_grouped": capabilities_grouped,
            "param_types": mf.PARAM_TYPES,
            "modes": mf.MODES,
            "transports": mf.TRANSPORTS,
            "cost_units": mf.COST_UNITS,
        }

    @app.get("/models/new", response_class=HTMLResponse)
    async def model_new(request: Request):
        instance = get_app()
        return render(
            request,
            "model_form.html",
            title="新增模型",
            spec=mf.spec_to_form({}),
            editing=False,
            errors={},
            **await model_form_ctx(instance),
            counts=await nav_counts(instance),
        )

    @app.post("/models/new", response_class=HTMLResponse)
    async def model_create(request: Request):
        instance = get_app()
        form = await request.form()
        try:
            spec = mf.build_spec(form)
        except mf.FormError as exc:
            return render(
                request,
                "model_form.html",
                status_code=422,
                title="新增模型",
                spec=mf.echo_form(form),
                editing=False,
                errors=exc.errors,
                **await model_form_ctx(instance),
                counts=await nav_counts(instance),
            )
        model_id = f"{spec.provider}:{spec.key}"
        if await instance.repo.model(model_id):
            return render(
                request,
                "model_form.html",
                status_code=422,
                title="新增模型",
                spec=mf.echo_form(form),
                editing=False,
                errors={"key": f"{model_id} 已经在库里了；同一供应商下模型名唯一，要改就去编辑它"},
                **await model_form_ctx(instance),
                counts=await nav_counts(instance),
            )
        await instance.repo.upsert_model(spec, force=True)
        note = f"已新增 {model_id}（{len(spec.params.fields())} 个参数）"
        if not spec.enabled:
            note += "；新模型一律先停用，参数对着文档核过再启用"
        return RedirectResponse(flash_url(f"/models/{model_id}", note), 303)

    @app.get("/models/{model_id:path}/edit", response_class=HTMLResponse)
    async def model_edit(request: Request, model_id: str):
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
        return render(
            request,
            "model_form.html",
            title=f"编辑 {model['name']}",
            spec=mf.spec_to_form(model),
            editing=True,
            model_id=model_id,
            errors={},
            **await model_form_ctx(instance),
            counts=await nav_counts(instance),
        )

    @app.post("/models/{model_id:path}/edit", response_class=HTMLResponse)
    async def model_update(request: Request, model_id: str):
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
        form = await request.form()
        try:
            spec = mf.build_spec(form, existing=model)
        except mf.FormError as exc:
            return render(
                request,
                "model_form.html",
                status_code=422,
                title=f"编辑 {model['name']}",
                spec=mf.echo_form(form),
                editing=True,
                model_id=model_id,
                errors=exc.errors,
                **await model_form_ctx(instance),
                counts=await nav_counts(instance),
            )
        if f"{spec.provider}:{spec.key}" != model_id:
            return RedirectResponse(
                flash_url(
                    f"/models/{model_id}/edit",
                    "供应商与模型名是身份，不能改；要换名字请删掉重建。",
                    "danger",
                ),
                303,
            )
        await instance.repo.upsert_model(spec, force=True)
        note = f"已保存 {model_id}"
        if model["verified"] and not spec.meta["verified"]:
            note += "；参数动过了，核对印记已退回「待核对」"
        return RedirectResponse(flash_url(f"/models/{model_id}", note), 303)

    @app.post("/models/{model_id:path}/verify")
    async def model_verify(request: Request, model_id: str):
        instance = get_app()
        form = await request.form()
        ok = form.get("verified") == "1"
        await instance.repo.set_model_verified(model_id, ok)
        return RedirectResponse(
            flash_url(
                f"/models/{model_id}",
                "已标记为「已核对」——参数逐字对过文档了" if ok else "已退回「待核对」",
            ),
            303,
        )

    @app.post("/models/{model_id:path}/delete")
    async def model_delete(request: Request, model_id: str):
        instance = get_app()
        used = await instance.repo.model_in_use(model_id)
        if used:
            return RedirectResponse(
                flash_url(
                    f"/models/{model_id}",
                    f"{model_id} 被 {used} 条任务记录引用，删掉它们就会指向一个查不到的"
                    "模型。要下线请改用「停用」。",
                    "danger",
                ),
                303,
            )
        await instance.repo.delete_model(model_id)
        return RedirectResponse(flash_url("/models", f"已删除 {model_id}"), 303)

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
        credentials = await instance.repo.default_credentials()
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
    async def credentials_page(request: Request, scope: str = "all"):
        """配置管理：**一行一个供应商**，展开才出现表单。

        为什么不是"一张卡一个供应商"：供应商是可选目录（几十上百家都可能），
        而用户真正配过 Key 的通常只有两三家。卡片平铺会让页面高度与供应商数量
        线性增长，并且把同一段说明文案复制 N 遍 —— 重复的不是数据，是噪音。
        所以这里只保留**扫描层**（一行：名字 / 地域 / Key / 最近测试），
        配置与测试动作收进行内展开（原生 ``<details>``）。
        """
        instance = get_app()
        credentials = await instance.repo.credentials()
        by_provider = await instance.repo.default_credentials()
        keys_by_provider: dict[str, list[dict[str, Any]]] = {}
        for key in credentials:
            keys_by_provider.setdefault(key["provider"], []).append(key)
        providers = await instance.repo.providers()
        model_counts: dict[str, int] = {}
        for row in await instance.repo.models():
            model_counts[row["provider"]] = model_counts.get(row["provider"], 0) + 1

        rows: list[dict[str, Any]] = []
        for p in providers:
            code = p["code"]
            c = by_provider.get(code)
            configured = bool(c and c.get("has_key"))
            tested_ok = bool(c and c.get("last_test_at") and c.get("last_test_ok"))
            # 状态轨的颜色只回答一件事：这家现在能不能用。
            if not p.get("enabled"):
                state = "off"
            elif configured and tested_ok:
                state = "ready"
            else:
                state = "need-key"
            keys = keys_by_provider.get(code, [])
            # 「最近一次」= 这家最新的一条测试记录。多把钥匙时不能只看默认那把 ——
            # 刚测完的往往正是备用的那一把，只盯默认会让人以为测试没生效。
            last = max(keys, key=lambda k: k.get("last_test_at") or "", default=None)
            rows.append(
                {
                    "p": p,
                    "c": c,
                    "keylist": keys,
                    "n_keys": len(keys),
                    "last": last,
                    "configured": configured,
                    "state": state,
                    "models": model_counts.get(code, 0),
                    "open": False,
                }
            )

        n_total = len(rows)
        n_configured = sum(1 for r in rows if r["configured"])
        n_missing = n_total - n_configured

        # 已配置的排前面（用户 90% 的时间只关心这一撮），组内保持目录顺序。
        rows.sort(key=lambda r: (not r["configured"], not r["p"].get("enabled")))

        if scope == "configured":
            rows = [r for r in rows if r["configured"]]
        elif scope == "missing":
            rows = [r for r in rows if not r["configured"]]

        # 连通性测试只许用文本模型 —— 多模态才是真烧钱。
        chat_models = await instance.catalog.list_models(capability="llm.chat", enabled_only=False)

        return render(
            request,
            "credentials.html",
            title="配置管理",
            scope=scope,
            rows=rows,
            n_total=n_total,
            n_configured=n_configured,
            n_missing=n_missing,
            settings_map=await instance.repo.settings(),
            chat_models=chat_models,
            encryption=instance.box.backend,
            counts=await nav_counts(instance),
        )

    @app.post("/credentials/{provider}")
    async def credential_save(
        request: Request,
        provider: str,
        api_key: str = Form(""),
        label: str = Form(""),
        workspace: str = Form(""),
        make_default: str = Form(""),
    ):
        """加一把钥匙，或覆盖同名的那个。

        名字留空 = 记成「默认」。这是最常见的情形（一个供应商一把钥匙），
        不该逼用户先想一个名字出来。
        """
        instance = get_app()
        if not api_key.strip():
            return RedirectResponse(flash_url("/credentials", "API Key 不能为空", "danger"), 303)
        name = label.strip() or "默认"
        # 没勾选时传 None（"没意见"），而不是 False（"别设"）——
        # 两者在多把钥匙的语义里不是一回事：第一把钥匙必须自动接管默认，
        # 否则会出现"配好了却发不出去"的状态。
        cred_id = await instance.repo.save_credential(
            provider,
            api_key,
            label=name,
            workspace=workspace.strip(),
            make_default=True if make_default else None,
        )
        row = await instance.repo.credential_by_id(cred_id)
        note = f"已保存 {provider} 的密钥「{name}」"
        if row and row.get("is_default"):
            note += "，并设为默认"
        if instance.box.backend != "fernet":
            note += "（注意：未配置 DIVINEPEN_CREDENTIAL_KEY，当前为明文存储）"
        return RedirectResponse(flash_url("/credentials", note, "bill"), 303)

    @app.post("/credentials/{provider}/default")
    async def credential_set_default(request: Request, provider: str, cred_id: str = Form("")):
        instance = get_app()
        row = await instance.repo.credential_by_id(cred_id)
        if not row or row["provider"] != provider:
            return RedirectResponse(flash_url("/credentials", "没有这把密钥", "danger"), 303)
        await instance.repo.set_default_credential(provider, cred_id)
        return RedirectResponse(
            flash_url("/credentials", f"{provider} 的默认密钥已换成「{row['label']}」", "ok"), 303
        )

    @app.post("/credentials/{provider}/delete")
    async def credential_delete(request: Request, provider: str, cred_id: str = Form("")):
        instance = get_app()
        row = await instance.repo.credential_by_id(cred_id)
        if not row or row["provider"] != provider:
            return RedirectResponse(flash_url("/credentials", "没有这把密钥", "danger"), 303)
        await instance.repo.delete_credential(cred_id)
        note = f"已删除 {provider} 的密钥「{row['label']}」"
        if row.get("is_default"):
            nxt = await instance.repo.credential(provider, row.get("workspace", "") or "")
            note += f"，默认已顺延给「{nxt['label']}」" if nxt else "，该供应商现在没有可用密钥"
        return RedirectResponse(flash_url("/credentials", note, "bill"), 303)

    @app.post("/credentials/{provider}/probe")
    async def credential_probe(request: Request, provider: str):
        """零成本探活：查的是**网络**，所以结果记在默认那把钥匙上。

        多把钥匙共用同一个 base_url，"哪把钥匙能连通"这个问题在探活这一层
        没有意义 —— 真正分得出钥匙差别的是下面的连通性测试。
        """
        instance = get_app()
        outcome = await rt.probe_reachability(instance, provider)
        default = await instance.repo.credential(provider)
        if default:
            await instance.repo.record_credential_test(
                default["id"], bool(outcome.get("ok")), outcome.get("note", "")
            )
        return RedirectResponse(
            flash_url(
                "/credentials", outcome.get("note", ""), "ok" if outcome.get("ok") else "danger"
            ),
            303,
        )

    @app.post("/credentials/{provider}/test")
    async def credential_test(
        request: Request, provider: str, cred_id: str = Form(""), model: str = Form("")
    ):
        """真实（最小）连通性测试：能点名测哪一把钥匙。

        不点名就测默认那把。结果按 **id** 记 —— 测的既然是那一把，
        结论就该落在那一把头上。
        """
        instance = get_app()
        row = await instance.repo.credential_by_id(cred_id) if cred_id else None
        if row is None:
            row = await instance.repo.credential(provider)
        outcome = await rt.connectivity_chat(
            instance, provider, model.strip() or None, row["label"] if row else None
        )
        if row and not outcome.get("blocked"):
            await instance.repo.record_credential_test(
                row["id"], bool(outcome.get("ok")), outcome.get("note", "")
            )
        prefix = f"「{row['label']}」：" if row else ""
        return RedirectResponse(
            flash_url(
                "/credentials",
                prefix + outcome.get("note", ""),
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

    @app.post("/settings/oss")
    async def set_oss(
        request: Request,
        oss_enabled: str = Form(""),
        oss_endpoint: str = Form(""),
        oss_bucket: str = Form(""),
        oss_access_key_id: str = Form(""),
        oss_access_key_secret: str = Form(""),
    ):
        """保存 OSS 配置到 .env。

        AccessKey Secret 只写不回显 —— 留空表示不改动。
        """
        env_path = get_settings().root / ".env"
        lines: list[str] = []
        if env_path.exists():
            lines = env_path.read_text().splitlines()

        # 要写入的键值对。
        updates: dict[str, str] = {
            "OSS_ENABLED": "true" if oss_enabled else "false",
            "OSS_ENDPOINT": oss_endpoint.strip(),
            "OSS_BUCKET": oss_bucket.strip(),
            "OSS_ACCESS_KEY_ID": oss_access_key_id.strip(),
        }
        # Secret 留空 = 不动原来的值。
        if oss_access_key_secret:
            updates["OSS_ACCESS_KEY_SECRET"] = oss_access_key_secret

        for key, value in updates.items():
            # 找已有的行（含注释掉的也替换）。
            found = False
            for i, line in enumerate(lines):
                stripped = line.lstrip("# ").strip()
                if stripped.startswith(f"{key}=") or stripped == key:
                    lines[i] = f"{key}={value}"
                    found = True
                    break
            if not found:
                lines.append(f"{key}={value}")

        env_path.write_text("\n".join(lines) + "\n")

        # 同步刷新内存中的 settings，让本进程立即生效。
        s = get_settings()
        s.oss_enabled = oss_enabled == "1" or oss_enabled == "true"
        s.oss_endpoint = updates["OSS_ENDPOINT"]
        s.oss_bucket = updates["OSS_BUCKET"]
        s.oss_access_key_id = updates["OSS_ACCESS_KEY_ID"]
        if oss_access_key_secret:
            s.oss_access_key_secret = oss_access_key_secret

        return RedirectResponse(
            flash_url("/credentials", "OSS 配置已保存，重启服务后完全生效"), 303
        )

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
            # 每行的"活着吗"由同一个 describe() 算 —— 列表和详情页不会各说各话。
            watches={row["id"]: progress.describe(row) for row in rows},
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
            watch=progress.describe(row),
            counts=await nav_counts(instance),
        )

    @app.get("/tasks/{task_id}/progress", response_class=HTMLResponse)
    async def task_progress_fragment(request: Request, task_id: str):
        """只回进度卡那一块，给详情页的轮询用。

        为什么是片段而不是 JSON：这样"页面上看到的"和"刷新后看到的"是
        **同一个宏**画出来的，不会再有第二份渲染逻辑悄悄跑偏。成本也低 ——
        一次主键查询 + 一个模板，2 秒一次也扛得住。

        `data-task-watch` 由这里给：什么时候不用再刷，是服务端说了算，
        JS 只管照做 —— 状态机的判断不该散落在浏览器里再写一遍。
        """
        instance = get_app()
        row = await instance.repo.task(task_id)
        if not row:
            return HTMLResponse('<div class="pcard"><b>任务不存在</b></div>', status_code=404)
        return render(request, "task_progress.html", watch=progress.describe(row))

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

    @app.post("/capabilities/{capability_id}/run")
    async def capability_run(request: Request, capability_id: str):
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
        form = await request.form()
        raw = {k: v for k, v in form.items() if not k.startswith("_") and v != ""}
        picker, _, payload = await _capability_input(instance, cap, raw, submitted=True)
        task_id = await instance.repo.create_task(
            {
                "capability_id": capability_id,
                "provider": (picker or {}).get("provider", {}).get("code", "")
                if picker
                else (cap.manifest.model_capability or ""),
                "model": (picker or {}).get("model", {}).get("key", "") if picker else "",
                "status": "PENDING",
                "payload": payload,
                "user_id": getattr(request.state, "user", None) and request.state.user["id"],
            }
        )
        _spawn(
            _run_background(instance, task_id, capability_id, payload, billed_allowed=True),
            task_id=task_id,
        )
        return RedirectResponse(
            flash_url(f"/tasks/{task_id}", f"已提交执行，任务 {task_id[:12]}…"), 303
        )

    @app.post("/tasks/{task_id}/resume")
    async def task_resume(request: Request, task_id: str):
        instance = get_app()
        row = await instance.repo.task(task_id)
        if not row or row["status"] != "AWAITING_INPUT":
            return RedirectResponse(
                flash_url("/tasks", "该任务不在等待人工介入的状态", "danger"), 303
            )
        paused = (row.get("result_obj") or {}).get("paused") or {}
        form = await request.form()
        raw = str(form.get("approvals") or "{}").strip()
        try:
            approvals = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return RedirectResponse(
                flash_url(f"/tasks/{task_id}", "介入参数不是合法 JSON", "danger"), 303
            )
        if not isinstance(approvals, dict):
            approvals = {paused.get("step_id", ""): approvals}
        step_id = paused.get("step_id")
        if step_id and step_id not in approvals:
            approvals = {**approvals, step_id: {}}
        resume = {
            "outputs": paused.get("outputs") or {},
            "steps": paused.get("steps") or [],
            "start_index": paused.get("start_index") or 0,
        }
        await instance.repo.update_task(task_id, status="PENDING", result=dumps({}))
        _spawn(
            _run_background(
                instance,
                task_id,
                row["capability_id"],
                row.get("payload_obj") or {},
                billed_allowed=True,
                resume=resume,
                approvals=approvals,
            ),
            task_id=task_id,
        )
        return RedirectResponse(flash_url(f"/tasks/{task_id}", "已带着你的确认继续执行"), 303)

    # ── 任务的增 / 删 / 改 / 查：取消 · 删除 · 重跑 · 复制改参 ─────
    # 任务中心原来只是账本（只读）。这里把它变成工作台，但守住两条线：
    #   ① **跑着的任务不删**：只有终态才允许删 —— 删掉一条正在跑的任务，
    #      远端还在扣费，本地却查无此人；
    #   ② **删除是账，不是产物**：删任务不回删产物（那份 mp4 / png 在 media/ 里），
    #      账单对账时产物还在。所以这是"清列表"，不是"销毁证据"。

    @app.post("/tasks/{task_id}/cancel")
    async def task_cancel(request: Request, task_id: str):
        instance = get_app()
        row = await instance.repo.task(task_id)
        if not row:
            return RedirectResponse(flash_url("/tasks", "任务不存在", "danger"), 303)
        if TaskStatus(row["status"]).is_terminal:
            return RedirectResponse(
                flash_url(f"/tasks/{task_id}", "任务已经结束了，不用取消", "warn"), 303
            )
        note = await _stop_task(instance, row)
        await instance.repo.update_task(
            task_id,
            status=TaskStatus.CANCELED.value,
            finished_at=_now(),
            error=dumps({"message": "用户取消"}),
        )
        return RedirectResponse(flash_url(f"/tasks/{task_id}", f"已取消{note}"), 303)

    @app.post("/tasks/{task_id}/retry")
    async def task_retry(request: Request, task_id: str):
        """原样重跑：拿同一份 payload 新建一条任务，立刻执行。

        不复用原来那一行 —— 原来那条的失败原因、耗时、远端 id 都是证据，
        覆盖掉就说不清"这次为什么和上次不一样"了。
        """
        instance = get_app()
        row = await instance.repo.task(task_id)
        if not row:
            return RedirectResponse(flash_url("/tasks", "任务不存在", "danger"), 303)
        if TaskStatus(row["status"]).is_active:
            return RedirectResponse(
                flash_url(f"/tasks/{task_id}", "任务还在跑，先取消或等它结束", "warn"), 303
            )
        cap_id = row["capability_id"]
        if not instance.registry.has(cap_id):
            return RedirectResponse(
                flash_url(f"/tasks/{task_id}", f"能力 {cap_id} 未安装，无法重跑", "danger"), 303
            )
        payload = row.get("payload_obj") or {}
        new_id = await instance.repo.create_task(
            {
                "capability_id": cap_id,
                "provider": row.get("provider") or "",
                "model": row.get("model") or "",
                "status": "PENDING",
                "payload": payload,
                "user_id": row.get("user_id"),
            }
        )
        _spawn(
            _run_background(instance, new_id, cap_id, payload, billed_allowed=True),
            task_id=new_id,
        )
        return RedirectResponse(
            flash_url(f"/tasks/{new_id}", f"已按原参数重跑，新任务 {new_id[:12]}…"), 303
        )

    @app.get("/tasks/{task_id}/clone")
    async def task_clone(request: Request, task_id: str):
        """复制改参：把这份参数**原样带回能力页**，改一个字段就能再跑。

        不在这里做一个"参数编辑器"——能力页那套「供应商 → 模型 → 参数」的表单
        才是参数的唯一真相源（下拉、默认值、必填都在那儿）。复制到那里改，
        改完提交走的还是同一条校验路径，不会出现"两套表单各说各话"。
        """
        instance = get_app()
        row = await instance.repo.task(task_id)
        if not row:
            return RedirectResponse(flash_url("/tasks", "任务不存在", "danger"), 303)
        cap_id = row["capability_id"]
        if not instance.registry.has(cap_id):
            return RedirectResponse(
                flash_url(f"/tasks/{task_id}", f"能力 {cap_id} 未安装，无法复制", "danger"), 303
            )
        query = urlencode(_clone_pairs(row), doseq=True)
        return RedirectResponse(f"/capabilities/{cap_id}?{query}", 303)

    @app.post("/tasks/{task_id}/delete")
    async def task_delete(request: Request, task_id: str):
        instance = get_app()
        row = await instance.repo.task(task_id)
        if not row:
            return RedirectResponse(flash_url("/tasks", "任务不存在", "danger"), 303)
        if TaskStatus(row["status"]).is_active:
            return RedirectResponse(
                flash_url(f"/tasks/{task_id}", "任务还在跑，先取消再删", "danger"), 303
            )
        await instance.repo.delete_task(task_id)
        return RedirectResponse(flash_url("/tasks", f"已删除任务 {task_id[:12]}…"), 303)

    @app.post("/tasks/purge")
    async def tasks_purge(request: Request, scope: str = Form("terminal")):
        """批量清账。`scope=terminal` 清所有终态；`failed` 只清失败的。

        绝不清 active（PENDING / RUNNING / AWAITING_INPUT）—— 那是正在花的钱。
        """
        instance = get_app()
        wanted = {
            "terminal": [
                TaskStatus.SUCCEEDED.value,
                TaskStatus.FAILED.value,
                TaskStatus.CANCELED.value,
            ],
            "failed": [TaskStatus.FAILED.value, TaskStatus.CANCELED.value],
            "succeeded": [TaskStatus.SUCCEEDED.value],
        }.get(scope, [])
        removed = await instance.repo.purge_tasks(wanted)
        return RedirectResponse(flash_url("/tasks", f"已清理 {removed} 条任务记录"), 303)

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


# ── 模型驱动的三段式：供应商 → 模型 → **该模型**的参数 ──────────────

#: 路由字段，不是模型参数。别让它们混进 params 里发给供应商。
ROUTE_KEYS = frozenset({"provider", "model", "policy"})


def _spec_brief(model: Any) -> dict[str, Any]:
    """把一个 ModelSpec 压成模板够用的一小块。"""
    return {
        "key": model.key,
        "name": model.display_name,
        "capability": model.capability,
        "enabled": model.enabled,
        "verified": bool(model.meta.get("verified", True)),
        "async": model.is_async,
        "transport": model.meta.get("transport", "http"),
        "pricing_unit": model.pricing.unit.value,
        "params_count": len(model.params.fields()),
        "billed_keys": model.params.billed_keys(),
        "doc_url": model.doc_url,
        "source_doc": model.meta.get("source_doc", ""),
        "path": model.api.path,
        "method": model.api.method,
        "notes": model.notes,
        "pricing": model.pricing.model_dump(mode="json"),
    }


async def _picker_context(
    instance: rt.App,
    cap: Any,
    *,
    provider: str | None = None,
    model_key: str | None = None,
) -> dict[str, Any]:
    """模型驱动能力的「供应商 / 模型 / 参数表单」上下文。

    三条口径，写在页面上也写在这里，免得下次再讨论一遍：

    * 供应商下拉**只列该能力下有模型的**。否则在「文生视频」里选阿里云会带出 36 个
      模型，其中 32 个在这一屏毫无用处 —— 用户得先自己挑出那 4 个。
    * 未配 API Key 的供应商**照列、加标注**，不隐藏。隐藏会让人以为这功能不支持；
      而且没 Key 也能预演（看请求体是零成本的）。
    * 默认选中的模型走 ``resolve(priority)`` 的同一口径 —— 页面显示的模型和真实
      调用会用的模型，不许有两个答案。
    """
    catalog = instance.catalog
    model_capability = cap.manifest.model_capability or cap.manifest.id
    specs = await catalog.list_models(capability=model_capability, enabled_only=False)

    creds = await instance.repo.default_credentials()
    provider_rows = {row["code"]: row for row in await instance.repo.providers()}

    by_provider: dict[str, list[Any]] = {}
    for spec in specs:  # dp_model 按 (capability, priority, key) 出库，顺序即优先级
        by_provider.setdefault(spec.provider, []).append(spec)

    providers: list[dict[str, Any]] = []
    for code, models in by_provider.items():
        row = provider_rows.get(code) or {}
        providers.append(
            {
                "code": code,
                "name": row.get("name") or code,
                "region": row.get("region") or "",
                "base_url": row.get("base_url") or "",
                "has_key": bool((creds.get(code) or {}).get("has_key")),
                "provider_enabled": bool(row.get("enabled", 1)),
                "usable": sum(1 for m in models if m.enabled),
                "models": models,
            }
        )
    # 配了 Key 的排前面：那是用户真会用的那几家。
    providers.sort(key=lambda p: (not p["has_key"], not p["provider_enabled"], p["code"]))

    warnings: list[str] = []
    chosen = next((p for p in providers if p["code"] == provider), None)
    if provider and chosen is None:
        warnings.append(f"供应商 {provider} 在 {model_capability} 下没有模型，已改用默认。")
    if chosen is None:
        chosen = next((p for p in providers if p["has_key"] and p["usable"]), None) or (
            providers[0] if providers else None
        )

    candidates = chosen["models"] if chosen else []
    model = next((m for m in candidates if m.key == model_key and m.enabled), None)
    if model is None and model_key:
        model = next((m for m in candidates if m.key == model_key), None)
        if model is None:
            warnings.append(f"模型 {model_key} 不属于 {model_capability}，已改用默认。")

    if model is None and chosen is not None:
        # 默认模型不在这里"再挑一次"：直接问后端同一套 resolve。
        # 页面显示的模型和真实调用会用的模型，只能有一个答案。
        try:
            resolved = await catalog.resolve(
                model_capability, provider=chosen["code"], policy=ResolvePolicy.PRIORITY
            )
            model = next((m for m in candidates if m.key == resolved.model.key), None)
        except Exception:  # 目录里没有可用模型是正常状态，不是异常
            model = None
        if model is None:  # 全都停用时也得让人看见它们，而不是空白
            model = next((m for m in candidates if m.enabled), None) or (
                candidates[0] if candidates else None
            )

    if model is None:
        reason = "该能力还没有可用模型"
    elif model_key and model.key == model_key:
        reason = "调用方显式指定的模型"
    else:
        reason = f"{chosen['name']} 下按目录优先级选中的 {model_capability} 模型"

    return {
        "capability": model_capability,
        "providers": [{**p, "models": [_spec_brief(m) for m in p["models"]]} for p in providers],
        "provider": chosen,
        "models": [_spec_brief(m) for m in candidates],
        "model": _spec_brief(model) if model else None,
        "reason": reason,
        "warnings": warnings,
        # 表单的真相源：**选中的那个模型**的参数 schema，不是能力的 Input。
        "form": model.params.model_dump(mode="json") if model else {"groups": []},
        "fields": model.params.fields() if model else [],
        "schema": model.params if model else ParamsSchema(),
    }


async def _capability_input(
    instance: rt.App,
    cap: Any,
    raw: dict[str, Any],
    *,
    submitted: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    """一次提交（查询串或表单）→ ``(picker, 回填值, invoke 输入)``。

    模型驱动能力在这里被拆成两段：页面上选的是**模型**，能力真正要的是
    ``params`` 那一袋点号路径参数 —— 拆错了，"页面参数 ↔ 接口参数一一对应"
    这句话就断了。

    ``submitted`` 决定要不要补布尔值。浏览器**不提交未勾选的复选框**，所以
    "缺席"只在真提交时才等于"关"；只是打开页面（``?provider=..&model=..``）
    就补一堆 False 的话，模型自己的默认值会被无声吃掉 —— 比如 happyhorse 的
    ``parameters.watermark`` 默认开，一进页面却显示关着。
    """
    if not cap.model_driven:
        # 普通能力：表单就是能力的 Input。预演和真实执行共用同一份归一化，
        # 否则"页面上看到的 body"和"真发出去的 body"又会分叉。
        return None, dict(raw), _form_values(raw, cap.form_schema())

    picker = await _picker_context(
        instance,
        cap,
        provider=raw.get("provider") or None,
        model_key=raw.get("model") or None,
    )
    values = _form_values(raw, picker["schema"]) if submitted else {}
    values = {k: v for k, v in values.items() if k not in ROUTE_KEYS}
    model = picker["model"]
    return (
        picker,
        values,
        {
            "provider": (picker["provider"] or {}).get("code"),
            "model": (model or {}).get("key"),
            "policy": "explicit" if model else "priority",
            "params": values,
        },
    )


def _form_values(form: Any, schema: Any) -> dict[str, Any]:
    """表单 → schema 输入值。

    三条必须在这里处理的语义：

    * 空字符串 = 未填 → 丢掉。留空不等于"传空串"，否则必填校验与默认值都会失效。
    * 未勾选的 checkbox **不会**出现在表单里 → 显式补 ``False``。
      否则用户取消勾选一个默认开启的开关（比如 prompt_extend），服务端会
      因为"没收到这个键"而用回默认的 True —— 界面和实际请求就对不上了。
    * 同名多值（多选框组、媒体多选）按 ``multi_items`` 收成数组。
      直接 ``dict(form)`` 只留得下最后一个，"选了 16:9 和 1:1"会静默变成只选 1:1。
    """
    source = form.multi_items() if hasattr(form, "multi_items") else form.items()
    raw: dict[str, Any] = {}
    for key, value in source:
        if key.startswith("_") or value == "":
            continue
        if key in raw:
            prev = raw[key]
            raw[key] = [*prev, value] if isinstance(prev, list) else [prev, value]
        else:
            raw[key] = value

    types = {f.key: f.type for f in schema.fields()}
    values: dict[str, Any] = {}
    for key, value in raw.items():
        ftype = types.get(key)
        if ftype is FieldType.MULTI_ENUM:
            values[key] = value if isinstance(value, list) else [value]
        elif ftype is FieldType.ARTIFACT_LIST:
            # 多选媒体统一是"一行一个"，文本框、追加、接口回读共用这一种写法。
            chunks = value if isinstance(value, list) else [value]
            values[key] = [
                line.strip() for chunk in chunks for line in str(chunk).splitlines() if line.strip()
            ]
        else:
            if isinstance(value, list):  # 单个参数的重复提交：后写的赢
                value = value[-1]
            values[key] = _coerce_json(value)

    for field in schema.fields():
        if field.type is FieldType.BOOL and field.key not in values:
            values[field.key] = False
    return values


def _log_bg_error(task: Any) -> None:
    import logging

    exc = task.exception() if not task.cancelled() else None
    if exc:
        logging.getLogger("divinepen.server").error("后台任务异常: %r", exc)


# ── 异步后台执行（pipeline / interactive 模式的运行方式）─────


async def _run_background(
    instance: rt.App,
    task_id: str,
    capability_id: str,
    values: dict[str, Any],
    *,
    billed_allowed: bool,
    resume: dict[str, Any] | None = None,
    approvals: dict[str, Any] | None = None,
) -> None:
    """后台跑一个能力，把生命周期写进 dp_task。

    pipeline 模式：一路跑完，结果落 task.result；
    interactive 模式：停在闸口时抛 PipelinePaused → 任务转 AWAITING_INPUT，
    暂停态（outputs / start_index）存在 task.result 里，等 resume 续跑。
    """
    from divinepen.core.errors import DivinePenError

    cap = instance.registry.get(capability_id)
    await instance.repo.update_task(task_id, status="RUNNING")
    ctx = instance.context(
        dry_run=False,
        billed_allowed=billed_allowed,
        extra={
            "registry": instance.registry,
            "resume": resume or {},
            "gate_approvals": approvals or {},
            # 让模型型能力把远端 task_id 补到**这一行**账上，而不是另起一条。
            "task_id": task_id,
        },
    )
    try:
        result = await cap.invoke(values, ctx)
    except DivinePenError as exc:
        await instance.repo.update_task(
            task_id, status="FAILED", error=dumps(exc.as_dict()), finished_at=_now()
        )
        return
    error = result.error or {}
    if error.get("code") == "pipeline_paused":
        await instance.repo.update_task(
            task_id,
            status="AWAITING_INPUT",
            result=dumps({"paused": error.get("detail", {})}),
            progress=_paused_progress(error),
        )
        return
    if result.status.value == "failed":
        await instance.repo.update_task(
            task_id, status="FAILED", error=dumps(error), finished_at=_now()
        )
        return
    await instance.repo.update_task(
        task_id,
        status="SUCCEEDED",
        progress=100,
        result=dumps(
            {
                "output": _output_dump(result),
                "artifacts": _artifact_uris(result),
                "elapsed_ms": result.elapsed_ms,
            }
        ),
        finished_at=_now(),
    )


def _output_dump(result: Any) -> Any:
    output = result.output
    return output.model_dump(mode="json") if hasattr(output, "model_dump") else output


def _artifact_uris(result: Any) -> list[str]:
    """产物地址：能力直接挂在 result.artifacts 上；单步能力在 output.uri；
    流水线在 output.final_artifacts。三处都看，页面才不缺产物。"""
    uris = [a.uri for a in result.artifacts]
    if uris:
        return uris
    dump = _output_dump(result)
    if isinstance(dump, dict):
        if dump.get("uri"):
            return [str(dump["uri"])]
        if dump.get("final_artifacts"):
            return [str(u) for u in dump["final_artifacts"]]
    return []


def _spawn(coro: Any, *, task_id: str | None = None) -> None:
    import asyncio

    task = asyncio.get_running_loop().create_task(coro)
    _BG.add(task)
    if task_id:
        _RUNNING[task_id] = task
        task.add_done_callback(lambda _t, tid=task_id: _RUNNING.pop(tid, None))
    task.add_done_callback(_BG.discard)
    task.add_done_callback(_log_bg_error)


def _now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


async def _stop_task(instance: rt.App, row: dict[str, Any]) -> str:
    """停下一个还在跑的任务：先掐本地的协程，再尽力通知远端。

    为什么要分两步：本地 asyncio 任务一取消，轮询循环就停了，但供应商那边的
    生成**不会跟着停** —— 不通知它，钱照扣。所以拿到 remote_task_id 就补一刀
    ``/cancel``。远端往往已经跑完或不可取消，所以是 best-effort，结果如实回报。
    """
    import contextlib

    task_id = row["id"]
    handle = _RUNNING.pop(task_id, None)
    if handle is not None and not handle.done():
        handle.cancel()
        with contextlib.suppress(BaseException):
            await handle

    remote_id = row.get("remote_task_id")
    if not remote_id:
        return ""
    return await _cancel_remote(instance, row, str(remote_id))


async def _cancel_remote(instance: rt.App, row: dict[str, Any], remote_task_id: str) -> str:
    from divinepen.core.errors import DivinePenError
    from divinepen.providers import aliyun

    provider = row.get("provider") or ""
    model_key = row.get("model") or ""
    try:
        model = await instance.catalog.get_model(provider, model_key)
        if model is None or model.api.poll is None:
            return ""
        api_key = await instance.credentials.get(provider)
        req = aliyun.cancel_request(model, remote_task_id, api_key=api_key)
        resp = await instance.http.request(
            HttpRequest(method=req.method, url=req.url, headers=req.headers, timeout=30.0)
        )
    except DivinePenError as exc:
        return f"；远端取消没成功（{exc}），它可能还在跑"
    except Exception as exc:  # 网络、无 Key 等：不影响本地已完成取消这件事
        return f"；远端取消没成功（{type(exc).__name__}），它可能还在跑"
    if resp.ok:
        return "，远端任务也已请求取消"
    return f"；远端取消返回 HTTP {resp.status_code}，它可能已经跑完或正在跑"


def _clone_pairs(row: dict[str, Any]) -> list[tuple[str, str]]:
    """任务 payload → 能力页的查询串。

    模型型能力的 payload 是 ``{provider, model, policy, params:{点号路径}}``，
    普通能力（剪辑）是扁平的表单值。两种都得还原成"能力页打开时该长什么样"。
    """
    payload = row.get("payload_obj") or {}
    pairs: list[tuple[str, str]] = []
    for key in ("provider", "model", "policy"):
        if payload.get(key):
            pairs.append((key, str(payload[key])))
    params = payload.get("params")
    body = (
        params
        if isinstance(params, dict)
        else {k: v for k, v in payload.items() if k not in ROUTE_KEYS}
    )
    for key, value in body.items():
        if isinstance(value, list):
            pairs.extend((str(key), _flat_scalar(item)) for item in value)
        else:
            flat = _flat_scalar(value)
            if flat:
                pairs.append((str(key), flat))
    return pairs


def _flat_scalar(value: Any) -> str:
    """参数值 → 能塞进 URL 的字符串。空串在表单里等于"没填"，会被丢掉。"""
    if isinstance(value, bool):
        return "true" if value else ""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _paused_progress(error: dict[str, Any]) -> int:
    detail = error.get("detail") or {}
    total = max(int(detail.get("total_steps") or 0), 1)
    done = int(detail.get("start_index") or 0)
    return min(95, int(done / total * 100))


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
