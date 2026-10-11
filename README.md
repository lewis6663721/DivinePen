# DivinePen

> 多模态视频创作底座。内置 AI 工具包（文本 / 图像 / 视频生成）+ 基础音视频剪辑能力，支持可插拔智能体插件扩展。

[![Python](https://img.shields.io/badge/python-3.12-blue)]() [![Status](https://img.shields.io/badge/status-P1--P3_ready-orange)]() [![Tests](https://img.shields.io/badge/tests-224%20passed-brightgreen)]()

---

## 这是什么

不是又一个"AI 视频工具"，而是**别人做 AI 视频工具时站的那块地基**。

三件事：

1. **原子能力库** —— 文生图、图生视频、字幕烧录、画中画、抽音轨……每个能力是一个
   独立可调用的函数，`pip install divinepen` 就能用，可以脱离整个平台单独跑。
2. **模型注册表** —— 供应商、模型、参数、计价全部入库管理。页面上的参数表单与接口文档
   **逐字对应**，改了文档改配置，不改代码。
3. **任务与费用台账** —— 异步任务提交即落库、可续查；每一次真实调用都记账。

智能体（短视频包装、金融视频生成、漫画短剧生成……）是**插件**，装你需要的那个，不装就没有。

---

## 能力清单

底座内置这些能力（`divinepen.list_capabilities()` 可枚举全部）：

### 本地剪辑（`media.*`，零成本、可离线真跑）

| 能力 ID | 说明 | 依赖 |
|---|---|---|
| `media.probe` | 读取媒体元信息（时长 / 分辨率 / 编码 / 流） | `ffprobe` |
| `media.trim` | 裁剪片段 | `ffmpeg` |
| `media.concat` | 多段拼接 | `ffmpeg` |
| `media.burn_subtitles` | 烧录字幕（`.srt` / `.ass`） | `ffmpeg` + `libass` |
| `media.pip` | 画中画 / 挂角标 | `ffmpeg` |
| `media.extract_audio` | 抽出音轨 | `ffmpeg` |

### 模型绑定（`llm.*` / `image.*` / `video.*`，走阿里云百炼适配器）

| 能力 ID | 说明 | 计费 |
|---|---|---|
| `llm.chat` | 文本对话 | 按 token |
| `llm.vision` | 图像理解 | 按 token |
| `image.t2i` | 文生图 | 按张 |
| `video.t2v` | 文生视频 | 按秒 |
| `video.i2v` | 图生视频 | 按秒 |
| `video.r2v` | 参考生视频 | 按秒 |
| `video.edit` | 视频编辑 | 按秒 |
| `video.all` | 视频全能力（族） | 按秒 |

> **能力 ≠ 模型**：调用方只说 `video.t2v`，由 Router 按策略选模型。参数 schema 全部来自
> 数据库里的模型目录，页面表单因此与接口文档逐字对应。
>
> `audio.*`（TTS / ASR）一期留空：走 WebSocket，传输层未实现前不注册。

---

## 五条硬约束

这些约束写进了代码结构里，不是文档承诺。

| # | 约束 | 代码里长什么样 |
|---|---|---|
| 1 | **能力 ≠ 模型** | 调用方只说 `video.t2v`，不说 `wan2.7-t2v`。Router 按策略选模型 |
| 2 | **Pydantic 是唯一真相源** | `ParamsSchema.from_pydantic(Input)` 派生页面表单，禁止手写 JSON schema |
| 3 | **付费先能免费预演** | `plan()` 免费组装真实请求；能力层用 `billed_allowed` 拦真实调用 |
| 4 | **原子可复用** | 底座主包零 Web / 零 ORM / 零 Redis，只依赖 pydantic · httpx · anyio |
| 5 | **插件化** | 智能体与能力都通过 `entry_points` 发现，装上即注册，不改底座一行 |

第 3 条是最要紧的。任何会花钱的调用，都必须先能**免费地把它要发的请求完整打印出来**：

```python
plan = await run("video.t2v", payload, ctx=local_context(dry_run=True))
print(plan.plan.describe())        # [video.t2v] wan2.7-t2v · 计费 5second ≈0.70CNY · 1 个请求
print(plan.plan.requests[0].body)  # 完整请求体，一分钱没花
```

`billed_allowed` 这道闸在**能力层**始终生效（`divinepen` 侧默认关闭）；控制台是单人开发模式，
提交任务时直接放行，不弹二次确认。

---

## 架构分层

依赖方向**严格单向**，箭头不可逆。

```
L5  插件层          agents/divinepen-agent-*   智能体（entry_points 发现，按需安装）
        ▼
L4  应用层          apps/server                FastAPI + 管理后台 + 任务中心 + DB 端口实现
        ▼
L3  服务层          divinepen/services         端口默认实现（内存台账 / 本地产物仓 / httpx 出口）
        ▼
L2  集成层          divinepen/providers        供应商适配器：aliyun（鉴权 / 参数映射 / 响应归一）
        ▼
L1  能力层          divinepen/capabilities     原子能力：llm / image / video / media
        ▼
L0  契约层          divinepen/core             零第三方依赖（仅 pydantic）：Capability · ModelSpec · Artifact · RunContext · ports
```

**能力层永远不知道 Web、数据库、Redis 的存在。** 它只面向 `core/ports.py` 里的
Protocol 编程，所以同一份能力代码既能跑在脚本里、又能跑在服务里。

详见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## 快速开始

### 1. 装上底座，跑一遍零成本冒烟

```bash
git clone git@github.com:lewis6663721/DivinePen.git && cd DivinePen

# 方式 A：uv（推荐，仓库里有 uv.lock）
uv sync --all-extras

# 方式 B：纯 pip（不装 uv）
python3 -m venv .venv
.venv/bin/pip install pydantic httpx anyio pytest pytest-asyncio
PYTHONPATH=src .venv/bin/python -m pytest -q     # 契约测试

# 方式 A 用 uv 时，也可以直接：
make smoke     # 端到端冒烟：注册 → 表单 schema → dry-run → 真跑一次 media.probe
make test      # 契约测试（标记 billed 的用例默认跳过）
```

`scripts/smoke.py` 会走完：能力注册 → 表单 schema 派生 → dry-run 打印真实命令行 →
用 ffmpeg 真跑一次 `media.probe`。**全程零成本、零外部服务。**

### 2. 起控制台

```bash
make install              # uv sync --all-extras
make seed                 # 把内置模型目录写进库（默认不覆盖你改过的参数）
make serve                # http://127.0.0.1:8801 —— 零配置即可看页面
```

默认 **SQLite + 本地开发模式**：不配 MySQL、不配 Redis 也能把页面跑起来。
数据库落在 `var/divinepen.sqlite3`，产物落在 `media/`。
默认账号 `admin / divinepen`（开发口令，上线前改掉），本地默认**关闭登录校验**。

要连 MySQL、写 API Key、开登录校验，再 `cp .env.example .env` 编辑对应项（见下方「配置」）。

常用命令：

```bash
make gaps         # 列出还不能放心调用的模型（待核对 / 未接入 / 已停用）
make run          # 带 --reload 的前台开发服务
make check        # ruff + mypy + pytest，提交前跑
```

控制台页面：**总览 / 能力目录 / 模型管理 / 供应商 / 配置管理（API Key + 连通性测试） /
用量与费用 / 任务中心 / 用户管理 / 插件管理**。

想让任务真的跑起来，有两种方式：

- **页面**：进「配置管理」写入供应商 API Key（Fernet 加密落库），再到「能力目录」选能力提交。
- **环境变量**：`.env` 里填 `DASHSCOPE_API_KEY=`（仅本地脚本用；服务运行时以数据库为准）。

### 3. 当脚本用

```python
import anyio, divinepen

async def main():
    for cap in divinepen.list_capabilities():
        print(cap["id"], cap["category"], cap["requires"])

    ctx = divinepen.local_context()                      # 不配数据库、不起 Redis
    result = await divinepen.run("media.probe", {"source": "/path/clip.mp4"}, ctx=ctx)
    print(result.status, result.output.duration)

anyio.run(main)
```

---

## 仓库结构

```
DivinePen/
├── src/divinepen/               # ① 底座主包（发布名 divinepen）
│   ├── core/                    #    L0 契约：capability / specs / artifact / runtime / ports / errors
│   ├── capabilities/            #    L1 能力：llm / image / video / audio / media
│   ├── providers/               #    L2 适配器：aliyun…
│   ├── services/                #    L3 默认实现：内存台账 + 本地产物仓 + httpx 出口
│   ├── pipeline.py              #    L4 编排：一次多能力的组合（本身也是 Capability）
│   └── agents/                  #    L5 插件 SDK：AgentPlugin 契约
├── apps/server/                 # ② 后台服务（发布名 divinepen-server）：FastAPI + DB + 任务中心
├── agents/                      # ③ 业务智能体插件（独立发行，按需安装）
│   └── dp-agent-shortvideo/     #    首个插件：裁剪 → 烧字幕 → 挂角标（纯本地，零成本）
├── design/                      # 视觉样张 specimen.html
├── docs/                        # 架构 / 决策记录 / 指南
├── scripts/                     # smoke.py 等运维脚本
└── tests/                       # 契约测试：这些断言定义的是"承诺"
```

**发布物划分**（互不牵连，各自 `pip install`）：

| 包名 | 内容 | 谁需要 |
|---|---|---|
| `divinepen` | 原子能力 + 注册表 + 默认实现 | 任何项目 |
| `divinepen-server` | 管理后台 / 任务中心 / 费用台账 | 要一套管理界面的人 |
| `divinepen-agent-xxx` | 单个业务智能体 | 需要该业务的人 |

---

## 配置

全部走环境变量，前缀 `DIVINEPEN_`，可选 `.env`（`cp .env.example .env`）。常用的几项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DIVINEPEN_DB_URL` | 空 → SQLite `var/divinepen.sqlite3` | 也可填 `mysql://user:pass@host:port/db` |
| `DIVINEPEN_CREDENTIAL_KEY` | 空 | Fernet 密钥，用于加密落库的 API Key；生产必填 |
| `DIVINEPEN_AUTH_REQUIRED` | `false` | 是否强制登录；**上线必须 `true`** |
| `DIVINEPEN_SECRET_KEY` | 空 → 每次随机 | 会话签名密钥；生产固定一个 32 字节 hex |
| `DIVINEPEN_MEDIA_ROOT` | `./media` | 产物本地目录 |
| `DIVINEPEN_OSS_ENABLED` / `_ENDPOINT` / `_BUCKET` / … | `false` | 配了走 OSS，不配回落本地目录 |
| `DASHSCOPE_API_KEY` | 空 | 阿里云百炼 Key（仅脚本用；服务以数据库凭据为准） |

生成凭据加密密钥：

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## 设计方向

后台管理界面走 **「石墨调色台」**：深色石墨底、单一信号青强调、
等宽字体承担全部数据角色、**钱有专属颜色**。

样张与规格：

```bash
make specimen     # 打开 design/specimen.html
```

完整决策记录见 [`.interface-design/system.md`](.interface-design/system.md)。

---

## 开发

```bash
make check        # ruff + mypy + pytest，提交前跑
make test         # 契约测试（标记 billed 的用例默认跳过）
make smoke        # 端到端冒烟
make fmt          # ruff format + 自动修
make type         # mypy（strict）
```

**铁律**：任何标记 `@pytest.mark.billed` 的用例都会发真实计费请求，默认必须跳过，
只在明确知情时手动跑。日常开发一律用 `dry_run=True` 或本地能力。

---

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0** | 仓库骨架 + 四层契约 + 冒烟通过 | ✅ |
| **P1** | 服务端骨架 + 模型管理 + 阿里系目录入库 + 参数表单 | ✅ 控制台已跑通（SQLite 零配置 / MySQL 可选） |
| **P2** | 阿里系能力接入（LLM / 图像 / 视频）+ ffmpeg 剪辑套件 | ✅ dry-run 全绿，真跑走三道闸 |
| **P3** | 编排层 + 可插拔智能体 + 首个业务插件 | ✅ 编排层 + `divinepen-agent-shortvideo` 已装已发现 |
| **P4** | 音频能力接入（TTS / ASR，走 WebSocket）+ 音轨分离 | ⬜ 传输层未实现，暂留空 |
| **P5** | Celery / Redis 异步执行 + 队列与限流 | ⬜ 当前进程内后台任务 |

---

## 已知问题

- `wan2.7-videoedit` 命名与其它文档不一致，待与官方确认后统一。
- `图生视频_hh.md` 的模型名疑为笔误（与文生视频文档重名），对应模型**默认停用**。
- 连通性测试默认模型为 `qwen3.7-plus`（旧版 `qwen-plus` 仍可用，推荐迁移）。
- `语音合成qwen_tts.md` 缺声音复刻接口参考，`audio.tts.clone` 暂无法建模。
- TTS / ASR 走 WebSocket，一期只登记不接入，页面上标"未接入"。
- 本机 ffmpeg 未编译 libass（无 `subtitles` 滤镜）：`media.burn_subtitles` 预演可看、真跑会被体检拦下；
  换带 libass 的 ffmpeg 即恢复。体检项为 `Requirement.FFMPEG_SUBTITLES`。
- `mypy` 钉在 `1.x`：`2.x` 的 strict 默认更严，会一次性报 57 个存量错，升级应作为独立一次「清存量 + 升版本」。

---

## 许可

Proprietary（私有项目，暂不对外发布）。
