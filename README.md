# DivinePen

> 多模态视频创作底座。内置 AI 工具包（图像 / 视频 / 音频生成）+ 基础音视频剪辑能力，支持可插拔智能体插件扩展。

[![Python](https://img.shields.io/badge/python-3.12-blue)]() [![Status](https://img.shields.io/badge/status-P0%20skeleton-yellow)]()

---

## 这是什么

不是又一个"AI 视频工具"，而是**别人做 AI 视频工具时站的那块地基**。

三件事：

1. **原子能力库** —— 文生图、图生视频、语音合成、字幕烧录、画中画、音轨分离……每个能力是一个
   独立可调用的函数，`pip install divinepen` 就能用，可以脱离整个平台单独跑。
2. **模型注册表** —— 供应商、模型、参数、计价全部入库管理。页面上的参数表单与接口文档
   **逐字对应**，改了文档改配置，不改代码。
3. **任务与费用台账** —— 异步任务提交即落库、可续查；每一次真实调用都记账。

智能体（金融视频生成、漫画短剧生成……）是**插件**，装你需要的那个，不装就没有。

---

## 五条硬约束

这些约束写进了代码结构里，不是文档承诺。

| # | 约束 | 代码里长什么样 |
|---|---|---|
| 1 | **能力 ≠ 模型** | 调用方只说 `video.t2v`，不说 `wan2.7-t2v`。Router 按策略选模型 |
| 2 | **Pydantic 是唯一真相源** | `ParamsSchema.from_pydantic(Input)` 派生页面表单，禁止手写 JSON schema |
| 3 | **付费三道闸** | `plan()` 免费组装真实请求 → 用户确认 → `billed_allowed=True` 才执行 |
| 4 | **原子可复用** | 底座主包零 Web / 零 ORM / 零 Redis，只依赖 pydantic · httpx · anyio |
| 5 | **插件化** | 智能体与能力都通过 `entry_points` 发现，装上即注册，不改底座一行 |

第 3 条是最要紧的。任何会花钱的调用，都必须先能**免费地把它要发的请求完整打印出来**：

```python
plan = await run("video.t2v", payload, ctx=local_context(dry_run=True))
print(plan.plan.describe())        # [video.t2v] wan2.7-t2v · 计费 5second ≈0.70CNY · 1 个请求
print(plan.plan.requests[0].body)  # 完整请求体，一分钱没花
```

---

## 架构分层

依赖方向**严格单向**，箭头不可逆。

```
L5  智能体插件      divinepen-agent-xxx ·           entry_points 发现，按需安装
        ▼
L4  编排            pipelines · 一次多能力的组合     P3
        ▼
L3  服务层          FastAPI · DB · Redis · 任务中心 · 费用    apps/server
        ▼
L2  供应商适配器    providers/aliyun · 鉴权/参数映射/响应归一  divinepen.providers
        ▼
L1  能力层          capabilities/ · 每个能力 = Input+Output+manifest  divinepen.capabilities
        ▼
L0  契约层          core/ · Capability · ModelSpec · Artifact · RunContext · ports  divinepen.core
```

**能力层永远不知道 Web、数据库、Redis 的存在。** 它只面向 `core/ports.py` 里的
Protocol 编程，所以同一份能力代码既能跑在脚本里、又能跑在服务里。

详见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)。

---

## 快速开始

```bash
git clone git@github.com:lewis6663721/DivinePen.git && cd DivinePen
python3 -m venv .venv && .venv/bin/pip install pydantic httpx anyio pytest pytest-asyncio
PYTHONPATH=src .venv/bin/python scripts/smoke.py      # 端到端冒烟，零成本
PYTHONPATH=src .venv/bin/python -m pytest -q          # 契约测试
```

`scripts/smoke.py` 会走完：能力注册 → 表单 schema 派生 → dry-run 打印真实命令行 →
用 ffmpeg 真跑一次 `media.probe`。

### 起控制台

```bash
uv pip install --python .venv/bin/python -e . -e apps/server --no-deps   # 或 make install
make serve          # http://127.0.0.1:8801
make seed           # 把内置模型目录写进 SQLite（默认不覆盖你改过的参数）
make gaps           # 列出还不能放心调用的模型（待核对 / 未接入 / 已停用）
```

默认 **SQLite + 本地开发模式**：不配 MySQL、不配 Redis 也能把页面跑起来。
默认 `admin / divinepen`（开发口令，上线前改掉），默认关闭真实计费调用。
页面清单：总览 / 能力目录 / 模型管理 / 供应商 / 配置管理（API Key + 连通性测试）/
用量与费用 / 任务中心 / 用户管理 / 插件管理。

当脚本用：

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
│   └── agents/                  #    L5 插件 SDK：AgentPlugin 契约
├── apps/server/                 # ② 后台服务（发布名 divinepen-server）：FastAPI + DB + 任务中心
├── agents/                      # ③ 业务智能体插件（独立发行，按需安装）
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
```

**铁律**：任何标记 `@pytest.mark.billed` 的用例都会发真实计费请求，默认必须跳过，
只在明确知情时手动跑。日常开发一律用 `dry_run=True` 或本地能力。

---

## 路线图

| 阶段 | 内容 | 状态 |
|---|---|---|
| **P0** | 仓库骨架 + 四层契约 + 冒烟通过 | ✅ |
| **P1** | 服务端骨架 + 模型管理 + 阿里系目录入库 + 参数表单 | 🚧 控制台已跑通（SQLite 先行）；MySQL/Tortoise 待换 |
| **P2** | 阿里系能力接入（LLM / 图像 / 视频 / 音频）+ ffmpeg 剪辑套件 | ⬜ |
| **P3** | 编排层 + 可插拔智能体 + 首个业务插件 | ⬜ |

---

## 已知问题

- `wan2.7-videoedit` 命名与其它文档不一致，待与官方确认后统一。
- `图生视频_hh.md` 的模型名疑为笔误（与文生视频文档重名），对应模型**默认停用**。
- `llm.md` 里没核到 `qwen3.8-max` 这个写法，连通性测试默认模型暂用 `qwen-plus`（配置项可改）。
- `语音合成qwen_tts.md` 缺声音复刻接口参考，`audio.tts.clone` 暂无法建模。
- TTS / ASR 走 WebSocket，一期只登记不接入，页面上标"未接入"。
