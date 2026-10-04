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
| `/admin/credentials` | 供应商凭据**清单**：一行一家（Key 掩码 / 最近测试结果），展开行内才出现 Key 表单与**连通性测试**按钮 |
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

---

## 13. 配置管理页的信息架构（P3 修订）

**症状**：供应商是**可选目录**（几十上百家都可能），但"用户配过 Key 的"通常只有两三家。
早期把每家渲染成一张大卡片（表单 + 两个测试按钮 + 三段说明），于是：

1. 页面高度与供应商数量**线性增长**（100 家 = 100 屏表单）；
2. 同一段说明文案（"Key 不回显"、"只走文本模型"）被复制 N 遍 —— 重复的不是数据，是噪音；
3. 三个层级的东西混在一起：**全局单例设置**（连通性模型、OSS）× **per-provider 凭据** × **per-provider 测试动作**。

**改法：把"扫描"和"操作"拆开。**

| 层 | 载体 | 缩放行为 |
|---|---|---|
| 扫描 | 一行一家：名称 / code·模型数 / 地域 / Key 掩码 / 最近测试 / 状态轨 | 行高 ~42px，高度∝N 但常数很小 |
| 操作 | 行内 `<details>` 展开：Key 表单 + 探活 + 验证 Key + 最近结果 | 只和"展开了几行"有关 |
| 说明 | 页首一次「测试口径」，逐条解释探活 / 验 Key / 多模态禁用 | 全站一份，不随行复制 |

**配套**：

* 状态轨沿用签名①（`.cap::before` 的行式变体），颜色仍然只回答"这家现在能不能用"：
  `ready` 信号青（已配且最近测通）/ `need-key` 琥珀（没配或没测通）/ `off` 灰（供应商停用）。
* 排序：**已配置在前**。用户 90% 的时间只关心这一撮。
* 筛选：`?scope=all|configured|missing` 走 **GET 链接**（无 JS 也能用）+ 一个纯前端行过滤输入框。
  100 家时靠这两个定位，而不是靠滚动。
* 未配置 ≤ 3 家时自动展开那几行 —— 少点一次，且不会在 100 家未配置时炸开一屏。

> 不变式：**说明文案只写一次**。任何"随每条记录重复出现的解释性文字"都是设计缺陷，
> 应该上提到页面级或下沉到控件 `title`。

---

## 14. 能力目录分两级 + 侧栏分组可折叠（P4）

**症状**：16 个能力平铺成一屏卡片，三区（AI 工具包 / 剪辑 / 智能体）各自用
`sec-label` 分隔，结果两件事都没做好：

1. **不知道"这区整体什么状态"** —— 想知道"剪辑能用几个"得把卡片挨个读完；
2. **侧栏比正文还长** —— 9 个入口 × 3 个分组全展开，找一项要滚。

**改法一：目录拆成两级，用 URL 表达层级。**

| 层 | 路由 | 只回答 |
|---|---|---|
| 一级 | `/capabilities` | 有哪三区、各区多少个、就绪几个、缺什么**类型**的东西 |
| 二级 | `/capabilities?zone=toolkit` | 这一区具体是哪些能力、各自缺什么 |

* 一级页**一张能力卡片都不渲染** —— 它的价值就是"不下滑"。有测试守着这条。
* 二极页顶部带区域页签（`aria-current` 标当前区），进错区不用退回一级页。
* 非法 `?zone=` 回落到一级页，不报 404：URL 是人的手滑，不是错误。
* 智能体区在二级页内**再按 `AgentMode` 分三组** —— `mode` 是智能体自己的轴，
  不该让 toolkit / editing 陪着一起分层。

**改法二：缺什么就写什么。**

一级页原本写「缺 8 项依赖」—— 八个能力各缺一把 API Key 时，这句话既吓人又没用。
现在把 8 条 `未配置供应商 video.t2v 的 API Key` 收敛成「未配 API Key ×8」：
一级页只说**类型**和**数量**，具体缺哪一个下沉到二级页的能力卡片上。
收敛规则写在 `web._short_reason`，认不出来的文案原样透出（宁可不聚合，也不编）。

**改法三：侧栏分组做成 `<details>`。**

* 当前页所在分组**服务端直接渲染 `open`** —— 首屏就能看见自己在哪，不依赖 JS；
* 之后以用户自己的开合为准（`localStorage`，`data-remember="nav:<分组名>"`）；
* 分组标题行在包含当前页时带 `data-current`：**收起后标题行本身高亮**，
  所以"折叠"不会让人丢失位置感；
* 标题行右侧带该分组的入口数 —— 收起不等于失忆。

> 不变式：**折叠不能牺牲定位**。任何"把当前上下文藏起来"的交互，都必须
> 在折叠态留下可识别的锚点（这里是高亮标题行 + 计数）。

**代价**：一级页多了一次点击。可接受 —— 目录是"偶尔进"的地方，
而"这区能不能用"是每次进来都要问的第一个问题。

---

## 15. 侧栏默认收起、悬停滑出（P4）

**症状**：236px 的固定侧栏吃掉正文宽度的 18%，而 9 个入口一天点不了几次。
用得最多的是正文，最占地方的是目录 —— 主次反了。

**改法：侧栏不占布局，做成 off-canvas。**

```
.navwrap   左边缘 14px 的"热区"，position:fixed，全高
  └ .side  236px 的面板，translateX(-100%) 藏在屏幕外
```

三条规则把它拉出来：`:hover`（鼠标碰上热区）、`:focus-within`（Tab 键走进去）、
`.peek`（从侧栏跳页后的短暂亮出）。移开后延迟 0.28s 再收回 —— 立刻收会让人
"手一抖就没了"。

**关键取舍：**

1. **热区落在 `.main` 的 padding 里**（`--s8`=32px），不吃正文。正文从 x=0 开始，
   宽度从 1044px 变成 1280px。
2. **不能用 `overflow:hidden` 裁**：裁剪会让宽度过渡期间文字反复重排。改成
   `.side` 绝对定位 + `translateX` 位移，布局不动，只有合成层在动。
3. **键盘不能因为"藏起来了"就用不了** —— `:focus-within` 和 `:hover` 走同一条滑出规则。
   任何"视觉上隐藏"的实现都必须同时回答"Tab 键怎么办"。
4. **逃生口是「钉住」**：`body.nav-pinned` 把 `.shell` 从 `1fr` 变回 `236px 1fr`，
   侧栏回到常驻列、正文让位。状态存 localStorage —— 不能强制所有人接受一种偏好。
5. **覆盖式面板会盖住正文左上角**，这是 over-canvas 的固有代价，不是 bug。
   所以只在"鼠标在附近"时出现；离开即消失，不需要额外的关闭按钮。

**配套（跨页连续性）**：从侧栏点链接跳页后，新页面让侧栏自己亮 3 秒
（`sessionStorage` 一次性标记 + `pointerleave`/定时器双兜底收起）。
否则连续翻页时每翻一次都要重新去够左边缘。
定时器是必需的兜底：鼠标全程不动时 `pointerleave` 不会触发。

> 不变式：**牺牲可见性不能牺牲可达性**。收起态必须同时满足
> ①有可发现的手柄（`.navwrap::after`）②键盘可进入 ③有"我就是要它一直在"的开关。

## 16. 一个供应商多把密钥、其中恰好一把默认（P4）

**需求**：同一个供应商常常要配不止一把 Key —— 正式号与测试号并存、不同项目的
额度分开算、轮换期间新旧两把都要能用。而"调用时用哪一把"必须**唯一**，
否则同样一次请求在不同时间走了不同账号，账单根本对不上。

**表结构从"供应商 → 一把钥匙"改成"（供应商, 工作区, 名字）→ 一把钥匙"。**

```sql
label        VARCHAR(64) NOT NULL DEFAULT '默认'   -- 人给这把钥匙起的名字
is_default   TINYINT(1)  NOT NULL DEFAULT 0        -- 调用时用这一把
UNIQUE KEY uq_cred_scope (provider, workspace, label)
```

**四条不变量：**

1. **总有一把默认**。配第一把时自动接管（否则"配好了却发不出去"）；
   删掉默认那把时，自动把剩下最早的一把顶上来。
2. **默认只有一个**。切换默认是"先把这家全清零、再置一"，不产生两个默认。
3. **取钥匙看默认**。"这家能不能调"只有一个答案 —— 所有取 Key 的路径都走默认优先，
   页面也按 `is_default DESC` 排，顺序本身就是答案。
4. **测试结果记在被测的那把上**（按 `id`，不按供应商）。多把钥匙的场景下，
   "这个供应商通不通"是个没有答案的问题 —— 不通的永远是具体那一把。
   例外是**零成本探活**：它查的是网络，多把钥匙共用一个 base_url，
   所以结果记在默认那把上。

**升级不能让人正在用的钥匙失效**：`CREATE TABLE IF NOT EXISTS` 管不了已存在的表，
而旧结构把"只能有一把"写进了表级 `UNIQUE(provider, workspace)` —— `ALTER` 拆不掉它。
SQLite 只能重建表（拷数据、换名），MySQL 可以就地 `DROP INDEX` + `ADD UNIQUE`。
老行一律标成 `默认` + `is_default=1`：**升级后的第一秒，系统必须和升级前一样能用**。

**两个容易写错的地方，都写进了测试：**

* **"未指定"与"明确不要"不是一回事。** `save_credential(make_default=None)` 表示
  "没意见"（第一把就自动接管），`False` 表示"别动默认"。页面表单没勾选框时传 `None`；
  传 `False` 会让第一把钥匙永远成不了默认，配完一次都调不通。
* **同名的钥匙是覆盖，不是再添一把。** 换密钥（轮换）与换默认（改主意）是两件事，
  覆盖一把已有的钥匙时不改它的默认身份，除非用户明确要求。

**页面上**：一把钥匙一行（名字 / 掩码 / 最近测试 / 动作），默认那把整行带左边一道
琥珀色脊线 + `data-default="1"` —— 颜色负责"一眼看到"，属性负责读屏能念出来。
"最近一次"的结论取这家**最新**的一条测试记录，而不是默认那把的，否则刚测完备用钥匙
会看起来像没生效。


## 17. 任务中心从"账本"变成"工作台"（P5）

**需求**：任务中心原来只读 —— 能看，不能动。而真实使用里，"再跑一次"和
"复制这单、改一个参数、再发一单"是出图/出片最常见的动作；跑错了要能停；
过时的记录要能清。

**五个动作，四条边界。**

| 动作 | 路由 | 语义 |
| --- | --- | --- |
| 取消 | `POST /tasks/{id}/cancel` | 掐本地协程 + **尽力**通知远端 `/cancel`，状态置 `CANCELED` |
| 重跑 | `POST /tasks/{id}/retry` | 用同一份 payload **新建**一条任务并立即执行 |
| 复制改参 | `GET /tasks/{id}/clone` | 303 到 `/capabilities/{cap}?<payload>`，表单回填 |
| 删除 | `POST /tasks/{id}/delete` | 只清台账记录，不动产物文件 |
| 批量清账 | `POST /tasks/purge` | 按状态清（`terminal` / `failed` / `succeeded`） |

1. **跑着的不许删、不许重跑。** 正在跑的任务是**正在花的钱**；删掉它，钱照扣而
   本地查无此人。删除与重跑都先看 `TaskStatus.is_active`。
2. **重跑是新建，不是覆盖。** 原来那条的失败原因、耗时、远端 `task_id` 都是证据，
   覆盖掉就说不清"这次为什么和上次不一样"。代价是列表会多一行 —— 这是对的。
3. **取消是两段式的：本地必成，远端尽力。** 本地 `asyncio.Task.cancel()` 一停，
   轮询循环就结束了，但供应商那边的生成**不会跟着停**。所以拿到 `remote_task_id`
   就补一刀 `POST /api/v1/tasks/{id}/cancel`（见文档「管理异步任务」），
   **best-effort，结果如实回报**（"远端取消没成功，它可能还在跑"）。
   远端取消失败**不能回滚**本地取消 —— 用户点的是"别再花我钱了"，本地停住是底线。
   `_RUNNING: dict[task_id, asyncio.Task]` 是这个动作的抓手；没有它就没法"找到
   在跑的那个人"。
4. **复制改参不在这里造第二个参数编辑器。** 能力页那套「供应商 → 模型 → 参数」
   表单才是参数的唯一真相源（下拉值、默认值、必填、点号路径全在那儿）。
   clone 只做一件事：把 payload 转成查询串，303 回能力页。改完提交走的还是
   同一条校验路径，不会出现"两套表单各说各话"。

**顺手修掉的一个幽灵账**：模型型能力原来会在一次生成里建**两条** `dp_task` ——
服务端建一条（有参数、任务中心立刻可见），`_model.execute()` 拿到远端 `task_id`
后又建一条（有进度和远端 id）。两条互不认识，谁都拼不出全貌。
现在 `RunContext.extra["task_id"]` 把服务端那条带进去，`_open_task()` **复用**它，
只补远端 id；只有库外调用（`extra` 里没有 task_id）才新建。

**列表页的两处收敛**：终态任务不再画进度条（SUCCEEDED 恒满格，旁边还写着
`SUCCEEDED` 和 `100%`，一个事实说三遍）；跑着的才画，并把"当前那一格"标成
`i.now`（此前这个类名在 CSS 里有、模板里从来没输出过 —— 跑着的任务看起来
和排队的一模一样）。`state()` 宏加 `data-status`：颜色给眼睛，属性给测试和读屏。


## 18. 一段 body 只能有一个组装者（P5 修订）

**事故**：`image.t2i` 提交必 400 ——
`InvalidParameter content format is incorrect`。
页面上参数填了、预览也显示一份合法 body，一提交就被打回。

**根因不是参数抄错，是"扁平值 → 请求体"这件事有两份实现**：

| | 走哪份代码 | 有没有 messages 包壳 |
| --- | --- | --- |
| 页面预览（dry-run） | `apps/server/…/runtime.assemble_body` | 有 |
| 真实提交 | `divinepen.core.specs.ParamsSchema.build_body` | 没有 |

《千问图像生成》要求 `input.messages` 是
`[{"role":"user","content":[{"text":"…"}]}]`（"content **array**（必选）"）。
包壳这条规则当初只加在了预览那份上，于是**预览合法、真实请求发裸字符串**。
用户看到的现象是"照着文档填、预览也对，怎么一提交就 400"。

**修法不是"把两份改成一致"，是让第二份不存在**：

- `runtime.assemble_body` 退化成一行转发到 `ParamsSchema.build_body`，
  预览与真实调用**从结构上不可能不一致**；
- 文档对字段形状的要求（"必须是数组"）属于**字段自己的属性**，
  所以落到 `FieldSpec.wrap`（本期只有 `"messages"`），由 `_f(..., wrap=...)`
  在目录里声明 —— 不在调用方各自实现；
- `normalize_value()` 是唯一的规范化入口，另有一层按 key 的兜底：
  老库里的 `params` JSON 还没有 `wrap`，不能因为"库里没这个标记"
  就继续发裸字符串。目录重新导入后由标记正式接管。

**顺带收紧的一处**：必填文本只填空白 = 没填。
以前它一路走到供应商那儿换一句 `InvalidParameter`；现在在页面校验阶段
就报"缺少必填参数：input.messages"。

**契约测试**（`tests/test_p5_body_contract.py`）守两条，第二条更重要：

1. **形状** —— 文档写 content 是数组，就必须是数组（对着文档逐字）；
2. **同源** —— `preview_request(...).body == schema.build_body(values)`。
   第 1 条只守住今天这个字段；第 2 条守住的是"以后没人能再偷偷分家"。

同系模型也一并覆盖：凡吃 `input.messages` 的模型（千问图像 pro / 标准）
纯文本提示词都必须被包成数组，而不只是今天报错的那一个。

---

## 19. 拿不出证据的进度，不许编（P5 修订）

**事故**：任务中心里一个 `RUNNING` 的任务，看不出它到底是不是真在跑。
用户的原话是「RUNNING 看不出是否真在跑，要进度条」。

**查下来是两个毛病，第二个才是要命的那个**：

| 毛病 | 表现 | 性质 |
| --- | --- | --- |
| 页面不自刷新 | 打开详情页那一刻是什么样，之后就一直是什么样 | 只是不好用 |
| **进度是编的** | `progress = min(90, polls * 3)` | **在用假数字制造真焦虑** |

第二条的细节：每轮询 3 次涨 9%，30 个周期（约 3 分钟）爬到 90% 然后停住。
而百炼的轮询接口 `GET /api/v1/tasks/{task_id}` **只回
`PENDING / RUNNING / SUCCEEDED`，从来不返回百分比**（见
`providers/aliyun.parse_poll`，它只取 `status`）。
所以那个数字**与远端做到哪了毫无关系**：远端 5% 和 95% 在本地显示一模一样。
用户盯着一个停住的"90%"等 20 分钟，只会得出"卡住了"的结论 ——
修一个"看不出在跑"的 bug，顺手造出一个"看着像卡住"的 bug。

**修法：先删掉编造，再谈显示。**

1. **轮询循环只写能核对的**：`status` + `polls`。
   不写 `progress`。`updated_at` 每次轮询都会被 `update_task` 刷新，
   所以它天然就是"最后一次回音"的时间戳 —— 想要心跳，不需要新字段。
2. **判断"活着"和"到哪了"是两件事**，前者有证据，后者没有：
   - 有证据的（真事实）：已运行多久 · 回过几次话 · 最后回音在几秒前 · 远端状态原话
   - 没证据的：完成百分比 —— **留空写 `—`，并说明为什么留空**
     （`title="供应商不报百分比 —— 宁可留空，不编一个"`）
3. **条纹的语义被刻意收窄**。`.pbar` 是一条会走的斜纹，它只说
   「它在动」，**不**表示"完成了百分之多少"。真百分比走 `.filmstrip`
   （签名 3）。两种形态对应"我们知道多少"，不混用。
   `prefers-reduced-motion` 下动效停掉，但换成一条静态信号色 ——
   动效可以停，"在跑"这件事不能因此消失。
4. **卡住判定要分同步/异步**，因为异步有心跳、同步没有：
   - 异步：轮询间隔 6 秒，`updated_at` 超过 90 秒（15 个周期）没动
     → 「可能卡住了」。这是进程没了/请求挂死的那个真场景。
   - 同步（`image.t2i`、ffmpeg）：中间态根本探不到，阈值放到半小时，
     措辞也降级成"多半是挂了"。**一次两分钟的 ffmpeg 拼接不叫卡住。**
   - 判据用 `remote_task_id` 是否存在来区分同步/异步：
     两者在 `_open_task` 同一处写下，同步任务永远没有远端 id，
     所以不必为了一个 UI 判断去 join 模型目录。
5. **停刷新也是一种诚实**。`AWAITING_INPUT` 等的不是时间是人，
   页面 refresh 一万次闸口也不会自己松开 —— 继续刷只会让人以为"它在跑"。
   同理终态必须停。停不停由服务端的 `data-task-watch` 说了算，
   JS 只管照做：**状态机的判断不该散落在浏览器里再写一遍**。

**自刷新的实现（详情页）**：`GET /tasks/{id}/progress` 只回**一块** HTML，
由 `_macros.html 的 task_progress()` 渲染 —— 和详情页用的是同一个宏。
这是 §18 那条教训的直接沿用：**一个概念只在一处长出写法**。
两处各画一份，"刷新一下变了样"就成了新的假信息源，
而且这次骗人的是页面自己。片段不套 `base.html`（不回整站），
跑完时由 JS 整页刷一次，把产物、按钮、暂停态一起带走。

**契约测试**（`tests/test_p5_progress.py`，30 条）守三条：

1. **不许编数** —— `test_running_async_task_reports_no_percentage` 特意用
   旧公式会给出 90 的 `polls=30`，确认它现在是"不知道"；
   `test_polling_writes_no_progress_field` 是行为级的：真跑一遍轮询循环，
   断言 `tasks.update` 的每一次调用里都**没有** `progress` 键（源码级
   断言挡不住重构，行为级挡得住）。
2. **不许说错** —— 心跳新鲜时不能报"卡住"（冤枉一个在跑的任务比漏报更糟），
   时间戳读不出来时也没资格下这个结论。
3. **不许分家** —— `test_fragment_route_returns_the_same_card_as_the_page`
   把片段和页面折叠空白后做包含判定，等于在说"这一块在两边是同一段 HTML"。

**已知待办**：任务中心列表页只做了"诚实"（在跑的行给条纹 + `已运行 Xm`，
进度格写 `—`），**没有做自刷新** —— 列表是快照，详情页才是那个活的视图。

## 20. 调用方式：同步与异步是两个端点（P5 修订）

一次真实的 403：

    提交 文生图 wan2.7-image-pro，页面一切正常，供应商却回
    AccessDenied current user api does not support synchronous calls

参数对不对已经有 §18 守着（body 只能有一个组装者），这次错的是**更前面一层**：
目录里"这个模型该怎么调用"配错了。

**先说对原因，因为第一版解释是错的。** 我当时写的是"万象图像族只支持异步"——
不对。万象/千问图像**同步异步都支持，但是两个不同的端点**：

| | 端点 | 请求头 |
|---|---|---|
| 同步 | `POST …/services/aigc/multimodal-generation/generation` | Content-Type + Authorization |
| 异步 | `POST …/services/aigc/image-generation/generation` | 再加 **`X-DashScope-Async: enable`（必选）** |

文档在异步端点下写着：「**X-DashScope-Async**（必选）… HTTP 请求只支持异步，必须设置为
`enable`。**重要** 缺少此请求头将报错："current user api does not support synchronous calls"」——
供应商的报错与文档警告**一字不差**。而万象2.7 的同步章节还写着「一次请求即可获得结果，
流程简单，**推荐大多数场景使用**」。

所以事故的准确描述是：**我们把一个同步调用打在了异步专用端点上**。
`IMAGE_PATH` 常量就是那个异步端点，而当时 `wan2.7-image*` 的 `mode="sync"` 指向了它。
第一版解释之所以更危险，是因为它把"我配错了"说成了"供应商只支持异步"——
后者会让人以为同步没有出路，从而放弃一个文档明确推荐的做法。

**修法（按你的要求统一走异步）**：

* **图像**：`qwen-image-3.0` / `qwen-image-3.0-pro` 改走异步 —— 文档明说
  「异步接口与同步接口**共用相同的请求参数结构**，仅需在请求头中增加
  `X-DashScope-Async: enable`」，**但请求地址不同**，别沿用同步地址。万象族同。
* **视频**：本就异步（`video-synthesis` + `GET /api/v1/tasks/{task_id}`），14 个模型
  的产物键统一是 `video_url`（逐份文档核过）。
* **音频**：**改不了**。两份音频文档只给 WebSocket
  （`wss://…/api-ws/v1/inference`），没有 HTTP 任务接口 —— TTS 与 ASR 都是长连接协议。
* **LLM / 向量 / 重排**：**改不了**。llm.md 只有 OpenAI 兼容的
  `POST /compatible-mode/v1/chat/completions`，没有任务型异步接口。

**四条不变量（都进了契约测试）**：

1. **模式与端点必须自洽** —— 同步调用不许指向异步专用端点。这条才是当初缺的那一条
   （`test_sync_must_not_use_the_async_only_endpoint`）。
2. **标了异步就必须有轮询契约**，否则任务永远停在 RUNNING。
3. **异步标志只能是头，不能又是参数** —— 扫声明的字段名。
4. **多图不许丢**。三份文档的返回形状**各不相同**，这是本次最容易栽的地方：

   | | 形状 |
   |---|---|
   | 千问图像 | 一个 choice，图像在同一 choice 的多个 `content[]` 块里 |
   | 万象2.6 | `n=2` 时是**两个 choice**，每个 choice 各带一张 |
   | 万象2.7 | 同 2.6；组图生成同样多 choice |
   | 老万相 t2i | `output.results[].url` |

   所以 `ASYNC_POLL_IMAGE` **故意不声明产物路径**（一条点号路径表达不了），
   统一交给 `aliyun.pick_image_artifacts`（遍历全部 choice × 全部 content 块）。
   写成 `output.choices.0.message.content` 会**悄悄只取第一张** —— 静默丢产物比报错难查。
   结算数量也跟着文档字段名走：万象报 `image_count`，千问报 `output_image_count`。

**出处必须是真的。** 本轮还查出 `z-image-turbo` 挂着
`source_doc=千问图像生成.md`、`verified=True`，而它在 15 份文档里**一次都没出现**
（连 "z-image" 字样都没有）。假的出处比没有出处更危险：它会让下一个读目录的人
以为有人核对过。已改为 `verified=False` + 空 `source_doc`，调用方式留在同步端点
（至少自洽），**待补文档后再定**。这条也进了测试
（`test_undocumented_model_claims_no_provenance`）。

**两条运维硬约束（文档原话）**：

* **结果 URL 只留 24 小时**，任务状态与图像 URL 都会被自动清除 —— 必须及时落盘。
* **不可跨地域 / 跨业务空间查询**：「必须使用提交任务时的地域、业务空间和API Key
  查询任务」。所以提交与查询必须同区同 Key，轮询请求要复用提交时那份凭据。
