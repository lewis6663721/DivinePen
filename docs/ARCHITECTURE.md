# DivinePen 架构设计

> **状态**：v0.1 提案稿（未定稿，等拍板）。定稿后转为 living doc；此后每个结构性决定单独记 `docs/decisions/ADR-xxxx-*.md`。
> **范围**：底座本体 = 模型管理 / AI 工具包 / 基础剪辑 / 配置·用户·费用·任务中心，以及智能体插件规范。
> **不在本期范围**：具体业务智能体（金融视频、漫剧）、模型训练、发布投放。

---

## 1. 一句话定位

**DivinePen 是一个「原子能力 + 模型注册表 + 任务运行时」的 Python 底座：视频创作类应用在它之上做组合，而不是在它里面写业务。**

三类使用者，决定了架构必须同时满足：

| 使用者 | 需要的能力 | 架构含义 |
| --- | --- | --- |
| 终端用户 | 在管理后台配 Key、管用户、看费用、看任务、用工具包 | 需要一套 `apps/server` 后台 |
| 上层智能体（将来插拔安装的那些） | `import divinepen` 拿原子能力，组合成业务流程 | 能力必须在**独立可发布包**里，且不依赖 Web/DB |
| 未来的你（新项目从零起步） | `pip install divinepen` 就有模型表、凭据、用量、ffmpeg 封装 | 底座必须**不绑定某个业务/某张业务表** |

**核心矛盾与解法**：模型管理要落库（MySQL），但原子能力必须零 Web/DB 依赖才能被任意项目复用
→ 用**端口—适配器**拆开：能力只面向抽象端口，落库实现放在 `apps/server`。

---

## 2. 设计原则（硬约束，不可商量）

1. **原子化 = 单一职责 + 独立可调用**。每个能力只做一件事（生成一张图 / 烧一次字幕 / 分离一次音轨），输入输出是 Pydantic 模型，能单测、能单独 import、能被别的项目直接用。
2. **契约优先，Pydantic 是唯一真相源**。参数 schema、表单、校验、文档全部从 Pydantic 派生。**禁止手写 JSON schema**（参考反面教材：`agent_drama_storyboard/app/services/catalog.py` 577 行手写 `params_schema`）。
3. **能力 ≠ 模型**。调用方只说 `image.t2i`，永不说 `qwen-image-3.0`。换模型、换供应商不改上层编排。
4. **零成本可验证**。所有会花钱的调用必须支持 `dry_run=True`（只组装请求、不发网络）；真调用走"付费三道闸"（预检 → 显式确认 → 提交即落库 task_id）。任何自动化测试/自检**不得**触发真实计费调用。
5. **诚实状态**。失败就是失败，不许静默降级成"成功"（沿用 storyboard 项目的教训）。
6. **先拆接口，后置进程**。原子化 ≠ 微服务化：默认进程内 `import`；只有当需要共享**状态**（模型注册表、OSS）或共享**稀缺资源**（GPU/渲染集群）时才拆成独立服务。

---

## 3. 分层与依赖方向

```
L5 插件层      agents/divinepen-agent-*        智能体（可插拔，entry_points 发现）
                        ↓ 只能向下依赖
L4 应用层      apps/server                     FastAPI + 管理后台 + Celery + DB 端口实现
                        ↓
L3 服务层      divinepen/services              端口定义（凭据/用量/任务/产物/存储）
                        ↓
L2 集成层      divinepen/providers             供应商适配器（aliyun / openai / ...）
                        ↓
L1 能力层      divinepen/capabilities          原子能力（llm / image / video / audio / media）
                        ↓
L0 契约层      divinepen/core                  零第三方依赖（仅 pydantic）
```

**依赖单向向下，禁止反向**。L1 及以下**不得** import 任何 Web 框架、ORM、Redis 客户端。

---

## 4. 关键抽象

### 4.1 Capability —— "做什么"

```python
class CapabilityManifest(BaseModel):
    id: str                    # 全局唯一稳定契约，如 "image.t2i"、"media.burn_subtitles"
    version: str               # 语义化版本
    category: Literal["llm", "image", "video", "audio", "media"]
    model_capability: str|None # 绑定模型分类，如 "video.t2v"；纯本地能力（ffmpeg）为 None
    requires: list[str]        # ["network"] / ["ffmpeg"] / ["ffprobe"]
    cost: CostModel            # 计费口径：token / per_second / per_image / free
    cacheable: bool

class Capability(ABC):
    manifest: ClassVar[CapabilityManifest]
    Input:  ClassVar[type[BaseModel]]   # ← schema / 表单 / 校验的唯一来源
    Output: ClassVar[type[BaseModel]]
    async def run(self, inputs: BaseModel, ctx: RunContext) -> BaseModel: ...
```

- 能力通过 `importlib.metadata.entry_points()` 自动发现（组名 `divinepen.capabilities`），第三方包装上即注册。
- 能力**不知道**自己用哪个模型/供应商——由 `ctx` 里的 Router 决定。

### 4.2 模型管理四件套 —— "用谁做"

| 实体 | 表 | 说明 |
| --- | --- | --- |
| Provider | `dp_provider` | 供应商：aliyun / openai / deepseek / 自定义兼容。含 base_url、地域、鉴权方式、各能力 endpoint 模板 |
| ModelDefinition | `dp_model` | 单个模型：API 模型名、**功能分类**、调用模式（sync / async-task）、endpoint、`params_schema`、`default_params`、计费规则、启用状态 |
| Credential | `dp_credential` | 每个 Provider（可选按业务空间）的 API Key，Fernet 加密存储 + 掩码展示 |
| UsageRecord | `dp_usage` | 每次真实调用的用量与费用（口径见 4.5） |

**Router**：`capability_id → (provider, model)` 的解析策略（指定 / 最便宜 / 优先级 / 降级链），是 L1 之上的薄层。

### 4.3 Artifact —— "产物"

能力之间**只传 Artifact（文件引用 + 元数据）**，不传对象。`Artifact` 含 `uri`、`mime`、`size`、`sha256`、`meta`。
存储走 `ArtifactStore` 端口：默认本地目录实现；`apps/server` 提供 OSS 实现（OSS 配置可选，未配则回落本地）。

### 4.4 RunContext —— 运行时上下文

携带：`credential`、`router`、`usage_sink`、`task_sink`、`artifact_store`、`dry_run`、`trace_id`、`logger`。
**这是能力与平台解耦的唯一通道**：跑在脚本里时注入内存实现，跑在服务里时注入 DB 实现。

### 4.5 端口（L3）—— 能力的"插座"

| 端口 | 职责 | 默认实现（在 `divinepen`） | 服务实现（在 `apps/server`） |
| --- | --- | --- | --- |
| `CredentialProvider` | 取 API Key | 环境变量 / 显式传入 | `dp_credential` 解密 |
| `UsageSink` | 记账 | 内存 / 日志 | `dp_usage` 落库 + 聚合 |
| `TaskSink` | 异步任务台账 | 内存 | `dp_task` 落库（远端 task_id 持久化） |
| `ArtifactStore` | 存产物 | 本地目录 | 本地 / OSS |
| `ModelCatalog` | 查模型与参数 | 内置 YAML catalog | `dp_model` 查询 |

---

## 5. 模型管理模块（本项目的硬骨头）

### 5.1 功能分类 taxonomy（v1，可扩展）

```
llm.chat | llm.vision | llm.embedding | llm.rerank
image.t2i | image.i2i | image.edit
video.t2v | video.i2v | video.r2v | video.edit | video.all
audio.tts | audio.tts.clone | audio.asr | audio.music | audio.separate
media.*   （ffmpeg 本地能力，不绑模型）
```

分类是**一级公民**：模型按分类聚合，页面按分类筛选，Router 按分类选模型。

### 5.2 `params_schema` —— 让页面参数与接口文档一一对应

结构（示例，阿里系文生视频）：

```jsonc
{
  "groups": [
    { "key": "input", "label": "输入", "fields": [
        { "key": "input.prompt", "label": "提示词", "type": "text",
          "required": true, "max_length": 5000, "doc": "文本提示词…" },
        { "key": "input.negative_prompt", "label": "反向提示词", "type": "text", "max_length": 500 }
    ]},
    { "key": "parameters", "label": "视频参数", "fields": [
        { "key": "parameters.resolution", "label": "分辨率", "type": "enum",
          "options": ["720P", "1080P"], "default": "1080P", "billed": true },
        { "key": "parameters.duration", "label": "时长(秒)", "type": "int",
          "min": 2, "max": 15, "default": 5, "billed": true },
        { "key": "parameters.prompt_extend", "label": "提示词智能改写", "type": "bool", "default": true }
    ]}
  ],
  "codegen": { "session": "CreateTask", "http": { "method": "POST",
                "path": "/api/v1/services/aigc/video-generation/video-synthesis",
                "headers": { "X-DashScope-Async": "enable" } } },
  "polling":  { "path": "/api/v1/tasks/{task_id}" }
}
```

三条规则：
1. **字段 `key` 用点号路径**，直接映射接口请求体的嵌套结构 → 页面控件与文档参数**一对一**，不写映射表。
2. `doc` 字段保存文档原文摘录 → 页面直接显示参数说明，改文档只改数据、不改代码。
3. `billed: true` 的字段在页面上**高亮提示计费**（`resolution`、`duration` 是钱）。

### 5.3 页面（管理后台）

| 路由 | 内容 |
| --- | --- |
| `/admin/providers` | 供应商列表 / endpoint 模板 / 启用状态 |
| `/admin/models` | 模型列表（按功能分类分组）、参数表单（由 `params_schema` 渲染）、默认参数 JSON |
| `/admin/credentials` | 每个供应商的 API Key（掩码）、**连通性测试**按钮 |
| `/admin/usage` | 费用看板：按供应商 / 模型 / 用户 / 时间段聚合 |
| `/admin/tasks` | 任务中心：状态、进度、远端 task_id、产物、重试 |
| `/admin/agents` | 插件（智能体）列表、启用/停用、依赖检查 |
| `/admin/users` | 用户与角色 |

**未配置 Key 的引导**：任何能力的入口先做 `credential` 前置检查，缺配置 → 跳 `/admin/credentials` 并提示"请先配置 X 供应商的 API Key"。

### 5.4 连通性测试（需要拍板：测什么、花不花钱）

待确认：测接口用 **只读的模型列表 / 免费接口**，还是发一次最小真实请求（会产生费用）？
我的建议：默认用 **只读探测**（`GET {base_url}/models` + 一次 `HEAD/OPTIONS` 可达性），把"真实小额请求"做成需要用户二次勾选的进阶选项，并在 UI 上标明可能计费。

---

## 6. 阿里系能力清单（一期，来自 `agents/docs/阿里系/`）

> 只做**注册与参数建模**，不真实调用。

| 能力 id | 文档 | API 模型名 | 调用模式 | 计费口径 |
| --- | --- | --- | --- | --- |
| `llm.chat` / `llm.vision` | llm.md | qwen3.8-max、qwen-vl-max/plus、qwen-long、qwen-turbo | sync（OpenAI 兼容） | tokens |
| `image.t2i` / `image.i2i` | 千问图像生成.md | qwen-image-3.0、qwen-image-3.0-pro | sync | 张 |
| `image.t2i` / `image.edit` | 万象图像生成2.6/2.7.md | wan2.6-image、wan2.7-image、wan2.7-image-pro | async-task | 张 |
| `video.t2v` | 文生视频_wan2.7.md | wan2.7-t2v、wan2.7-t2v-2026-06-12 | async-task | 秒×分辨率 |
| `video.i2v` | 图生视频_wan2.7.md | wan2.7-i2v-2026-04-25 | async-task | 秒×分辨率 |
| `video.r2v` | 参考生视频_wan2.7.md | wan2.7-r2v-2026-06-12 | async-task | 秒×分辨率 |
| `video.edit` | 视频编辑_wan2.7.md | **wan2.7-videoedit**（文档无连字符；storyboard 库里写的是 `wan2.7-video-edit`，待定） | async-task | 秒 |
| `video.all` | 视频生成_wan3.0.md | wan3.0-video、wan3.0-video-prime | async-task | 秒（2–30s） |
| `video.t2v/i2v/r2v/edit` | 文生/图生/参考生/视频编辑_hh.md | happyhorse-1.1-t2v、happyhorse-1.1-r2v、happyhorse-1.0-video-edit | async-task | 秒（3–15s） |
| `audio.tts` | 语音合成qwen_tts.md | qwen-audio-3.0-tts-flash/plus、cosyvoice-v3-flash/plus、cosyvoice-v3.5-* | sync / stream | 字符 |
| `audio.tts.clone` | 同上（**缺复刻 API 参考**） | 声音复刻 | async enrollment | 次 |
| `audio.asr` | 语音识别funasr.md | fun-asr-realtime(-2025-11-07 / -2026-02-28)、fun-asr-flash-8k-realtime、qwen-audio-3.0-asr-flash-streaming、paraformer-realtime | stream | 秒 |

**待办（文档缺口）**：
- `图生视频_hh.md` 里的模型名写成 `happyhorse-1.1-t2v`（与 `文生视频_hh.md` 同名），疑为文档笔误 → 建模前需核对。
- `语音合成qwen_tts.md` 缺**声音复刻**接口参考（创建/查询、样本要求、计费）→ 需要补文档。
- 地域：百炼新域名 `https://{WorkspaceId}.{region}.maas.aliyuncs.com`，模型/Endpoint/Key **必须同地域**；这个约束要落到 Provider 的地域字段上做校验。

---

## 7. AI 工具包（一期能力清单）

按分类落地，每个能力一个模块、一份 `Input/Output`、一组单测（`dry_run` 跑通即算过）：

| 分类 | 能力 |
| --- | --- |
| 文本 | `llm.chat`、`llm.vision`、`llm.structured_output`（按 Pydantic 出 JSON） |
| 图像 | `image.t2i`、`image.i2i`、`image.edit` |
| 视频 | `video.t2v`、`video.i2v`、`video.r2v`、`video.edit`、`video.all` |
| 音频 | `audio.tts`、`audio.tts.clone`、`audio.asr` |
| 通用 | `text.translate`、`text.polish`（可选，走 llm） |

---

## 8. 基础剪辑能力（ffmpeg，本地、免费、同步）

| 能力 id | 功能 | 实现要点 |
| --- | --- | --- |
| `media.burn_subtitles` | 字幕烧录 | `subtitles=` / `ass=` 滤镜；需 ASS 样式支持（字体、描边、位置） |
| `media.picture_in_picture` | 画中画 | `overlay` 滤镜；位置/缩放/圆角/透明度 |
| `media.concat` | 视频拼接 | `concat` demuxer（同编码）或 `concat` 滤镜（异构）+ 统一转码 |
| `media.extract_audio` | 音频分离 | `-vn -acodec` 提取，或 `-map 0:a` 多轨拆出 |
| `media.trim` | 音视频裁剪 | `-ss/-to` 快速定位；帧级精确用 `-accurate_seek` |
| `media.transcode` | 转码/改分辨率 | 统一输出规格 |
| `media.watermark` | 水印 | `overlay`（图）/ `drawtext`（字） |
| `media.speed` | 变速 | `setpts` + `atempo`（音画同步） |
| `media.thumbnail` | 抽帧/封面 | `-frames:v 1` |
| `media.merge_av` | 音视频合成 | 分离视频 + 配音/配乐重新合成 |
| `media.probe` | 元信息探测 | `ffprobe` → 时长/分辨率/码率/音轨（所有剪辑能力的共同前置） |

统一约定：能力输入输出都是 `Artifact` + 结构化参数；`ffmpeg` 命令行**全部走参数化拼装**（不经 shell、不拼字符串），执行前可 `dry_run` 打印命令。

---

## 9. 用户 / 费用 / 任务中心

- **用户管理**：账号、角色（admin / user）、权限（沿用 storyboard 的 capability-gated 授权思路：配置驱动、按账号授权、零迁移）。
- **费用管理（token 费用）**：`dp_usage` 记录每次真实调用的 `provider / model / capability / user / 用量 / 单价 / 金额 / task_id`；提供按维度聚合看板、预算告警（阈值）、导出。**失败不扣费**（阿里系只有成功才计费，本地台账以远端 `usage` 为准）。
- **任务中心**：统一 `dp_task` 表承载所有异步任务（视频生成、图像异步、复刻、长剪辑）。核心规则（来自 storyboard 的踩坑）：
  - **提交即落库**：拿到远端 `task_id` 立刻持久化，再返回；
  - **后台续查**：独立 resync 循环（lifespan 启动）轮询并收尾，刷新/重启不丢任务；
  - **远端成功 + 本地异常** 时按远端 ID 抢救回结果；
  - 状态诚实：PENDING / RUNNING / SUCCEEDED / FAILED / CANCELED，不美化。

---

## 10. 智能体插件规范（一期只定契约，不实现业务）

```python
class AgentPlugin(ABC):
    manifest: AgentManifest   # id / name / version / 依赖的 capability id 列表 / 需要的凭据
    def register(self, registry: CapabilityRegistry) -> None: ...
    api: APIRouter | None     # 可选的业务路由，挂到 /agents/{id}/*
```

- 发现方式：`entry_points(group="divinepen.agents")`。
- 安装方式：`pip install divinepen-agent-jinrong` → 重启（或热载）即可在 `/admin/agents` 看到并启用。
- 插件只能通过 `capability_id` 消费底座能力，**不得**直接依赖某个供应商 SDK。
- 依赖校验：启用前检查所需 capability / credential / 外部二进制（ffmpeg）是否就绪，缺什么报什么。

---

## 11. 提议的仓库结构

```
DivinePen/
├── pyproject.toml              # uv workspace 根
├── uv.lock
├── README.md  CHANGELOG.md  CONTRIBUTING.md  LICENSE
├── .gitignore  .editorconfig  .pre-commit-config.yaml  Makefile
├── docker-compose.yml          # mysql + redis（minio 可选）
├── .env.example
│
├── src/divinepen/              # ★ 主包：pip install divinepen 即得全部原子能力（零 Web/DB 依赖）
│   ├── core/                   # L0 契约：capability / manifest / registry / artifact /
│   │                           #         runtime(RunContext,Result,Error) / schema / plugin / errors
│   ├── providers/              # L2 适配器：aliyun, openai, deepseek, 自定义兼容
│   ├── capabilities/           # L1 原子能力
│   │   ├── llm/  image/  video/  audio/
│   │   └── media/              #    ffmpeg 基础剪辑
│   ├── services/               # L3 端口定义 + 默认（内存/本地）实现
│   │   └── ports.py            #   CredentialProvider / UsageSink / TaskSink /
│   │                           #   ArtifactStore / ModelCatalog
│   └── agents/                 # L5 插件 SDK（契约 + 加载器）
│
├── apps/server/                # L4 后台服务（独立发行 divinepen-server）
│   └── src/divinepen_server/
│       ├── main.py             #   FastAPI + lifespan（续查循环）
│       ├── api/                #   REST: /api/v1/{capabilities,models,tasks,usage,...}
│       ├── web/                #   管理后台页面
│       ├── db/                 #   tortoise models + aerich migrations + seeds(catalog)
│       ├── ports/              #   端口的服务侧实现（DB/OSS/Redis）
│       └── workers/            #   celery 任务
│
├── agents/                     # 官方智能体插件（各含 pyproject，独立发布；一期空）
├── tests/                      # 单元 + 契约测试 + 零成本 dry-run 用例
├── scripts/                    # dev/seed/自检脚本
└── docs/
    ├── ARCHITECTURE.md         # 本文
    ├── decisions/              # ADR
    ├── guides/                 # 快速开始 / 写一个能力 / 写一个智能体
    └── reference/              # 模型清单、参数对照表（由 seed 生成的产物）
```

**发行物划分**（决定"pip install 什么得到什么"）：

| 发行名 | 内容 | 依赖 |
| --- | --- | --- |
| `divinepen` | 契约 + 能力 + 适配器 + 端口默认实现 | pydantic、httpx、（extra）dashscope、ffmpeg |
| `divinepen-server` | FastAPI 后台 + DB + Redis + Celery + 页面 | `divinepen` + fastapi + tortoise + aerich + celery |
| `divinepen-agent-<name>` | 单个智能体插件 | `divinepen` |
| `divinepen-ffmpeg`（可选拆分） | 剪辑能力单独发版，供不装 AI 依赖的项目用 | ffmpeg 二进制 |

---

## 12. 技术选型

| 层 | 选型 | 备注 |
| --- | --- | --- |
| 语言/运行时 | Python 3.12 | 本机已有 3.12.13 |
| 包/环境 | **uv**（workspace + lock） | 本机已有 uv、poetry；建议统一 uv |
| Web | FastAPI + Uvicorn | |
| ORM / 迁移 | Tortoise-ORM + **Aerich** | |
| DB | MySQL（本机 3306 已就绪） | 库名建议 `divinepen` |
| 缓存/队列 | Redis（本机 6379 已就绪） + Celery | 异步任务、限流、续查调度 |
| 校验/契约 | Pydantic v2 | 唯一真相源 |
| HTTP | httpx（异步） | 统一超时/重试/日志埋点 |
| 剪辑 | ffmpeg / ffprobe（`/opt/homebrew/bin/ffmpeg` 已装） | 参数化调用，禁 shell 拼接 |
| 智能体 | agentscope（可选依赖） | 只作为插件可选项，不进底座核心 |
| 日志 | structlog 或 stdlib + JSON formatter | 需带 trace_id 贯穿 |

---

## 13. 里程碑

| 阶段 | 目标 | 交付 |
| --- | --- | --- |
| **P0 骨架与契约** | 仓库结构、`divinepen.core` 契约、CI、文档 | 能 `pip install -e .`，能注册并 `dry_run` 跑通一个假能力 |
| **P1 模型管理 + 阿里系 catalog** | Provider/Model/Credential 表 + 参数 schema + 管理页 + seed | 阿里系全部模型入库，页面参数与文档一一对应（**不调真实接口**） |
| **P2 工具包 + 剪辑能力** | 阿里系 image/video/audio 能力 + ffmpeg 全部剪辑能力 | 每个能力有 dry-run 用例；剪辑能力真跑（免费）验证 |
| **P3 平台化** | 任务中心 + 费用看板 + 用户/权限 + 配置引导 + 插件加载器 | 全链路可用；真实生成需用户显式点击 |
| **P4 首个智能体** | 用底座能力组合出第一个业务插件 | 验证"插拔式"成立（金融视频或漫剧，二选一） |

---

## 14. 待你拍板的问题

1. **前端形态**：管理后台走 **Jinja2 服务端渲染 + 原生 JS**（沿用你 storyboard 项目的路子，最快、和你现有习惯一致），还是另起 **Vue3/React SPA**（前后端彻底分离，但要多一套构建链）？
2. **仓库形态**：认可上面「`src/divinepen` 主包（零 DB）+ `apps/server` 后台 + `agents/*` 插件」这个 uv workspace 单仓多包结构吗？包名 `divinepen` 已在 PyPI 空闲（已查，404）。
3. **连通性测试**：API Key 连通性测试用「只读探测（免费）」还是「最小真实请求（计费）」？建议前者为默认、后者需二次勾选。
4. **一期切哪一刀**：我建议第一刀切 **P0+P1（骨架 + 模型管理 + 阿里系 catalog 入库 + 参数表单）**，先让你看到"参数完全对应文档"这件事成不成立。可以吗？

---

## 附：从 Storyboard 项目继承的经验（避免重踩）

- `seed_model_definitions()` **不得**在启动时覆盖库里已改过的 `default_params` → 只补缺失，`force=True` 才重刷。
- 异步任务的 `task_id` **必须**提交即落库 + 后台续查，否则刷新/重启就丢真钱。
- 参考图给阿里系必须**供应商可拉取**（公网 URL / OSS 临时 URL / base64 data URL），相对路径会 `Failed to download`（好在提交即失败、不扣费）。
- 模型输出可能回显 prompt（`orig_prompt`），日志/接口脱敏要覆盖这类回显字段。
- 地域必须一致：模型、Endpoint、API Key 三者同地域，否则调用失败。

---

## 12. 能力目录三分区与智能体三模式（P3 修订）

`category` 回答"干什么"（llm/image/video/audio/media），**目录分区**回答"靠什么干"，
两者正交。分区由注册表在注册时判定（`CapabilityKind`）：

| 分区 | 判定 | 例子 | 成本形态 |
|---|---|---|---|
| `toolkit` AI 工具包 | 绑定模型（`model_capability` 非空），必须调供应商 API | `video.t2v`、`image.t2i`、`llm.chat` | 计费，走三道闸 |
| `editing` 剪辑 | 本地硬代码实现，主要是 ffmpeg | `media.trim`、`media.concat`、`media.pip` | 免费 |
| `agent` 智能体 | 由插件注册（source=`agent:*`）或声明了 `agent_mode` | `media.shortvideo_package` | 视步骤而定 |

智能体再按 `AgentMode` 分三种运行模式：

* **react**：基于 agentscope 的 ReAct 循环（LLM 推理 / 记忆 / 上下文管理），步骤由模型临场决定。
  agentscope 是**可选依赖**（`pip install agentscope`），没装时插件体检明说，不让它假装能推理。
* **pipeline**：流程写死（`Recipe`），用户只给输入；服务端 `POST /capabilities/{id}/run`
  提交即落库 `dp_task` 并后台执行，完事把结果与产物写回任务。
* **interactive**：同一条 workflow，但步骤可标 `gate=True`（人工闸口）。执行到闸口抛
  `PipelinePaused`，任务转 `AWAITING_INPUT`，暂停态（中间产物、停在哪一步）落库；
  `POST /tasks/{id}/resume` 带 `gate_approvals`（可原样确认、可改该步入参）续跑，不重跑已完成步骤。

不变式：闸口暂停**不是失败**（错误码 `pipeline_paused`），页面据此渲染"确认并继续"；
后台执行与预演共用同一份 plan/组装代码，所以"点下去会发生什么"永远先免费可见。
