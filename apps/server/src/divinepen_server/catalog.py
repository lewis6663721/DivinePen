"""阿里系（阿里云百炼 / DashScope）模型目录 —— 一期 seed 的唯一真相源。

三条铁律（来自 ``docs/ARCHITECTURE.md`` §5.2）：

1. 字段 ``key`` 用**点号路径**，与接口文档的请求体嵌套逐字对应。页面控件、校验、
   拼请求体全部依赖它，因此不需要任何额外映射表。
2. ``doc`` 字段保存文档原文摘录 → 页面直接显示参数说明，改文档只改数据。
3. ``billed=True`` 的参数在页面上必须高亮：``resolution``、``duration``、``n``
   这些直接决定花多少。

每个模型带 ``verified`` 标记：``True`` = 参数已逐条对着文档核过；``False`` =
**存疑待核**（文档笔误、参数未在文档中出现、或传输方式不是普通 HTTP）。
页面会把未核实的模型显式标出来，绝不假装它是准的。
"""

from __future__ import annotations

from typing import Any

from divinepen.core.manifest import CostUnit
from divinepen.core.specs import (
    ApiSpec,
    ArtifactPick,
    FieldSpec,
    FieldType,
    GroupSpec,
    ModelSpec,
    Option,
    ParamsSchema,
    PollSpec,
    Pricing,
)

# ── 供应商 ─────────────────────────────────────────────────────────

PROVIDER_ALIYUN = "aliyun-bailian"

PROVIDERS: list[dict[str, Any]] = [
    {
        "code": PROVIDER_ALIYUN,
        "name": "阿里云百炼 · 华北2（北京）",
        "base_url": "https://dashscope.aliyuncs.com",
        "region": "cn-beijing",
        "auth_type": "bearer",
        "docs_url": "https://help.aliyun.com/zh/model-studio/",
        "notes": "新域名 https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com；"
        "模型、Endpoint、API Key 必须同地域，否则调用失败。",
        "enabled": 1,
        "sort": 10,
    },
    {
        "code": "aliyun-bailian-intl",
        "name": "阿里云百炼 · 新加坡（国际站）",
        "base_url": "https://dashscope-intl.aliyuncs.com",
        "region": "ap-southeast-1",
        "auth_type": "bearer",
        "docs_url": "https://www.alibabacloud.com/help/en/model-studio/",
        "notes": "国际站 Key 与北京站不通用。",
        "enabled": 1,
        "sort": 20,
    },
    {
        "code": "openai",
        "name": "OpenAI 兼容（自定义）",
        "base_url": "https://api.openai.com/v1",
        "region": None,
        "auth_type": "bearer",
        "docs_url": "",
        "notes": "占位。任何 OpenAI 兼容端点都可挂在这里（含 vLLM / Ollama）。",
        "enabled": 0,
        "sort": 90,
    },
]

# ── 构造助手（把重复的部分收干，模型定义只留差异）──────────────────

VIDEO_PATH = "/api/v1/services/aigc/video-generation/video-synthesis"
IMAGE_PATH = "/api/v1/services/aigc/image-generation/generation"
SYNC_IMAGE_PATH = "/api/v1/services/aigc/multimodal-generation/generation"
ASYNC_POLL = PollSpec(
    path="/api/v1/tasks/{task_id}",
    task_id_path="output.task_id",
    status_path="output.task_status",
    interval_seconds=6.0,
    timeout_seconds=1800.0,
    artifacts=[ArtifactPick(path="output.video_url", kind="video", name="生成视频")],
    usage_path="usage",
)


def _f(
    key: str,
    label: str,
    type_: FieldType = FieldType.TEXT,
    *,
    required: bool = False,
    default: Any = None,
    doc: str = "",
    options: tuple[str, ...] = (),
    billed: bool = False,
    min: float | None = None,
    max: float | None = None,
    step: float | None = None,
    max_length: int | None = None,
    advanced: bool = False,
    placeholder: str = "",
) -> FieldSpec:
    return FieldSpec(
        key=key,
        label=label,
        type=type_,
        required=required,
        default=default,
        description=doc,
        options=[Option(label=o, value=o) for o in options],
        billed=billed,
        min=min,
        max=max,
        step=step,
        max_length=max_length,
        advanced=advanced,
        placeholder=placeholder,
    )


def _prompt_field(*, required: bool = True, max_length: int = 5000) -> FieldSpec:
    return _f(
        "input.prompt",
        "提示词",
        FieldType.TEXTAREA,
        required=required,
        max_length=max_length,
        doc="文本提示词，用来描述生成视频中期望包含的元素和视觉特点。支持中英文，"
        f"长度不超过 {max_length} 个字符，超过部分会自动截断。",
        placeholder="例：一只小猫在月光下奔跑",
    )


def _negative_field() -> FieldSpec:
    return _f(
        "input.negative_prompt",
        "反向提示词",
        FieldType.TEXTAREA,
        max_length=500,
        doc="描述不希望在画面中看到的内容，可对画面进行限制。支持中英文，不超过 500 个字符。",
        advanced=True,
    )


def _seed_field() -> FieldSpec:
    return _f(
        "parameters.seed",
        "随机种子",
        FieldType.INT,
        default=0,
        min=0,
        max=2147483647,
        doc="取值范围 [0, 2147483647]。未指定时系统自动生成随机种子。"
        "相同 seed 也不能保证结果完全一致。",
        advanced=True,
    )


def _watermark_field(*, default: bool = False) -> FieldSpec:
    return _f(
        "parameters.watermark",
        "AI 生成水印",
        FieldType.BOOL,
        default=default,
        doc="是否添加水印标识，水印位于视频右下角，文案固定为「AI生成」。"
        f"默认 {'true' if default else 'false'}。",
        advanced=True,
    )


def _prompt_extend_field() -> FieldSpec:
    return _f(
        "parameters.prompt_extend",
        "提示词智能改写",
        FieldType.BOOL,
        default=True,
        doc="开启后使用大模型对输入 prompt 进行智能改写。对较短的 prompt 效果提升明显，但会增加耗时。",
    )


def _media_field(*, doc: str, required: bool = True, label: str = "媒体素材") -> FieldSpec:
    return _f(
        "input.media",
        label,
        FieldType.JSON,
        required=required,
        doc=doc,
        placeholder='[{"type":"first_frame","url":"https://…/first.png"}]',
    )


def _video_params(
    *,
    resolution_options: tuple[str, ...] = ("720P", "1080P"),
    resolution_default: str = "1080P",
    duration_min: int = 2,
    duration_max: int = 15,
    duration_default: int = 5,
    duration_doc: str = "",
    watermark_default: bool = False,
    ratio_options: tuple[str, ...] = ("16:9", "9:16", "1:1", "4:3", "3:4"),
    ratio_default: str = "16:9",
    input_fields: list[FieldSpec] | None = None,
    extra_params: list[FieldSpec] | None = None,
) -> ParamsSchema:
    params = [
        _f(
            "parameters.resolution",
            "分辨率档位",
            FieldType.ENUM,
            default=resolution_default,
            options=resolution_options,
            billed=True,
            doc="**直接影响费用**。生成视频的分辨率档位，用于控制视频的清晰度（总像素）。"
            f"可选值：{'、'.join(resolution_options)}，默认 {resolution_default}。",
        ),
        _f(
            "parameters.ratio",
            "宽高比",
            FieldType.ENUM,
            default=ratio_default,
            options=ratio_options,
            doc="生成视频的宽高比。不同宽高比对应的输出分辨率（宽×高）不同。",
        ),
        _f(
            "parameters.duration",
            "时长（秒）",
            FieldType.INT,
            default=duration_default,
            min=duration_min,
            max=duration_max,
            billed=True,
            doc=duration_doc
            or f"**直接影响费用，按秒计费**。取值为 [{duration_min}, {duration_max}] 之间的整数，"
            f"默认 {duration_default}。",
        ),
        _prompt_extend_field(),
        _watermark_field(default=watermark_default),
        _seed_field(),
    ]
    params.extend(extra_params or [])
    return ParamsSchema(
        groups=[
            GroupSpec(key="input", label="输入", fields=list(input_fields or [])),
            GroupSpec(key="parameters", label="视频参数", fields=params),
        ]
    )


def _async_video_api(*, path: str = VIDEO_PATH) -> ApiSpec:
    return ApiSpec(
        method="POST",
        path=path,
        headers={"Content-Type": "application/json", "X-DashScope-Async": "enable"},
        mode="async_task",
        poll=ASYNC_POLL,
    )


def _price(unit: CostUnit, note: str) -> Pricing:
    return Pricing(unit=unit, unit_price=None, currency="CNY", notes=note)


VIDEO_PRICE = _price(
    CostUnit.SECOND,
    "按输出视频秒数 × 分辨率档位计费；具体单价见百炼「模型价格」页。失败不扣费。",
)
IMAGE_PRICE = _price(CostUnit.IMAGE, "按生成张数计费；具体单价见百炼「模型价格」页。")
TOKEN_PRICE = _price(CostUnit.TOKEN, "按输入/输出 token 计费，以响应中的 usage 为准。")
CHAR_PRICE = _price(CostUnit.CHARACTER, "按输入字符数计费，以响应中的 usage 为准。")

_VIDEO_REF_IMAGE_DOC = (
    "媒体素材数组，每个元素包含 type 与 url。首帧生视频：first_frame；"
    "首帧+音频：first_frame+driving_audio；首尾帧：first_frame+last_frame；"
    "视频续写：first_clip。图像格式 JPEG/JPG/PNG（不支持透明通道）/BMP/WEBP，"
    "分辨率 [240, 8000] 像素，宽高比 1:8～8:1，不超过 20MB。"
    "url 必须是公网 URL、OSS 临时链接或 base64 data URL —— 本地相对路径会下载失败。"
)
_VIDEO_REF_MEDIA_DOC = (
    "媒体素材数组，每个元素包含 type 与 url。type 可选 reference_image（参考图像）、"
    "reference_video（参考视频）、first_frame（首帧图像）。参考图像 + 参考视频 ≤ 5，"
    "每种 type 最多出现一次。prompt 中用「图1、图2」「视频1」按数组顺序指代素材。"
)
_VIDEO_EDIT_MEDIA_DOC = (
    "媒体素材数组，待编辑的视频为必传项（type=video），可附参考图（type=reference_image）"
    "用于换装、换风格等编辑。url 必须是供应商可拉取的地址。"
)

# ── 模型 ───────────────────────────────────────────────────────────


def _models() -> list[ModelSpec]:
    out: list[ModelSpec] = []

    # ── 文生视频 wan2.7 ───────────────────────────────────────────
    t2v_params = _video_params(
        input_fields=[
            _prompt_field(),
            _negative_field(),
            _f(
                "input.audio_url",
                "音频文件 URL",
                FieldType.URL,
                doc="模型将使用该音频生成视频。支持公网 URL / OSS 临时 URL；格式 wav、mp3，"
                "时长 2～30s，不超过 15MB。超长自动截取，不足则补无声。不传则由模型自动配音。",
                advanced=True,
            ),
        ]
    )
    for key, priority in (("wan2.7-t2v-2026-06-12", 10), ("wan2.7-t2v", 20)):
        out.append(
            ModelSpec(
                provider=PROVIDER_ALIYUN,
                key=key,
                capability="video.t2v",
                name=f"文生视频 {key}",
                region="cn-beijing",
                api=_async_video_api(),
                params=t2v_params,
                pricing=VIDEO_PRICE,
                notes="多镜头叙事靠 prompt 自然语言控制，无需 shot_type 参数（传了也不生效）。",
                doc_url="https://help.aliyun.com/zh/model-studio/text-to-video-api-reference",
                meta={
                    "source_doc": "文生视频_wan2.7.md",
                    "verified": True,
                    "priority": priority,
                },
            )
        )

    # ── 图生视频 wan2.7 ───────────────────────────────────────────
    out.append(
        ModelSpec(
            provider=PROVIDER_ALIYUN,
            key="wan2.7-i2v-2026-04-25",
            capability="video.i2v",
            name="图生视频 wan2.7-i2v-2026-04-25",
            region="cn-beijing",
            api=_async_video_api(),
            params=_video_params(
                input_fields=[
                    _prompt_field(required=False),
                    _negative_field(),
                    _media_field(doc=_VIDEO_REF_IMAGE_DOC, label="首帧 / 尾帧 / 驱动音频"),
                ]
            ),
            pricing=VIDEO_PRICE,
            notes="视频宽高比尽量与输入素材保持一致；模型按所选分辨率档位自动缩放到相近总像素。",
            doc_url="https://help.aliyun.com/zh/model-studio/image-to-video-api-reference",
            meta={"source_doc": "图生视频_wan2.7.md", "verified": True, "priority": 10},
        )
    )

    # ── 参考生视频 wan2.7 ─────────────────────────────────────────
    r2v_params = _video_params(
        duration_max=10,
        duration_doc="**直接影响费用，按秒计费**。取值为 [2, 10] 或 [2, 15] 之间的整数"
        "（随模型版本），默认 5。",
        input_fields=[
            _prompt_field(),
            _negative_field(),
            _media_field(doc=_VIDEO_REF_MEDIA_DOC, label="参考图 / 参考视频 / 首帧"),
        ],
    )
    for key, priority in (("wan2.7-r2v-2026-06-12", 10), ("wan2.7-r2v", 20)):
        out.append(
            ModelSpec(
                provider=PROVIDER_ALIYUN,
                key=key,
                capability="video.r2v",
                name=f"参考生视频 {key}",
                region="cn-beijing",
                api=_async_video_api(),
                params=r2v_params,
                pricing=VIDEO_PRICE,
                notes="参考素材为主体角色时仅包含单一角色；空镜视频不推荐作为参考视频。",
                doc_url="https://help.aliyun.com/zh/model-studio/video-to-video-api-reference",
                meta={
                    "source_doc": "参考生视频_wan2.7.md",
                    "verified": True,
                    "priority": priority,
                },
            )
        )

    # ── 视频编辑 wan2.7 ───────────────────────────────────────────
    # duration 在这个模型上语义特殊（0 = 沿用输入视频时长），所以直接改时长档位，
    # 不再追加同名字段 —— 重复 key 是 schema 层面的错误，页面会渲染出两个控件。
    out.append(
        ModelSpec(
            provider=PROVIDER_ALIYUN,
            key="wan2.7-videoedit",
            capability="video.edit",
            name="视频编辑 wan2.7-videoedit",
            region="cn-beijing",
            api=_async_video_api(),
            params=_video_params(
                duration_min=0,
                duration_max=10,
                duration_default=0,
                duration_doc="取值 [2, 10] 的整数；**默认值 0 代表直接使用输入视频的时长**，不做截断。",
                input_fields=[
                    _f(
                        "input.prompt",
                        "编辑指令",
                        FieldType.TEXTAREA,
                        required=True,
                        max_length=5000,
                        doc="描述要做的编辑，如「为人物换上酷闪的衣服，再戴参考图里的帽子」。",
                    ),
                    _negative_field(),
                    _media_field(doc=_VIDEO_EDIT_MEDIA_DOC, label="原视频 + 参考图"),
                ],
            ),
            pricing=VIDEO_PRICE,
            notes="文档中模型名写作 wan2.7-videoedit（无连字符）；storyboard 库里的 "
            "wan2.7-video-edit 与之不一致，此处按文档为准。",
            doc_url="https://help.aliyun.com/zh/model-studio/video-edit-api-reference",
            meta={"source_doc": "视频编辑_wan2.7.md", "verified": True, "priority": 10},
        )
    )

    # ── 视频生成 wan3.0（全能）─────────────────────────────────────
    wan3_params = _video_params(
        resolution_options=("1080P", "720P", "480P"),
        duration_max=30,
        ratio_options=("adaptive", "16:9", "9:16", "1:1", "4:3", "3:4"),
        ratio_default="adaptive",
        input_fields=[
            _prompt_field(),
            _media_field(
                doc="媒体素材数组，用于指定首帧等输入素材；支持 first_frame 首帧图像。"
                "图像与音频的格式/体积限制同 wan2.7 系列。",
                required=False,
                label="输入素材",
            ),
        ],
        extra_params=[
            _f(
                "parameters.fps",
                "帧率",
                FieldType.INT,
                default=30,
                min=1,
                max=60,
                doc="输出视频帧率，默认 30。",
            ),
            _f(
                "parameters.audio",
                "输出声音",
                FieldType.BOOL,
                default=True,
                doc="默认输出视频包含声音。",
            ),
        ],
    )
    for key, priority, note in (
        ("wan3.0-video", 10, "标准版"),
        ("wan3.0-video-prime", 20, "高速版，能力对齐标准版，端到端速度显著提升"),
    ):
        out.append(
            ModelSpec(
                provider=PROVIDER_ALIYUN,
                key=key,
                capability="video.all",
                name=f"视频生成 {key}（{note}）",
                region="cn-beijing",
                api=_async_video_api(),
                params=wan3_params,
                pricing=VIDEO_PRICE,
                notes="全能模型：文生 / 图生 / 参考生 / 编辑共用一个入口，时长上限 30s。",
                doc_url="https://help.aliyun.com/zh/model-studio/video-generation-api-reference",
                meta={"source_doc": "视频生成_wan3.0.md", "verified": True, "priority": priority},
            )
        )

    # ── happyhorse（hh 系）────────────────────────────────────────
    hh_res = ("480P", "720P", "1080P")
    hh_video_params = _video_params(
        resolution_options=hh_res,
        duration_min=3,
        duration_max=15,
        watermark_default=True,
        input_fields=[_prompt_field(), _negative_field()],
    )
    for key, priority in (("happyhorse-1.1-t2v", 30), ("happyhorse-1.0-t2v", 40)):
        out.append(
            ModelSpec(
                provider=PROVIDER_ALIYUN,
                key=key,
                capability="video.t2v",
                name=f"文生视频 {key}",
                region="cn-beijing",
                api=_async_video_api(),
                params=hh_video_params,
                pricing=VIDEO_PRICE,
                notes="水印默认为**开启**（与 wan2.7 相反）。时长 3～15s。",
                meta={"source_doc": "文生视频_hh.md", "verified": True, "priority": priority},
            )
        )

    hh_r2v_params = _video_params(
        resolution_options=hh_res,
        duration_min=3,
        duration_max=15,
        watermark_default=True,
        input_fields=[_prompt_field(), _media_field(doc=_VIDEO_REF_MEDIA_DOC, label="参考图像")],
    )
    for key, priority in (("happyhorse-1.1-r2v", 30), ("happyhorse-1.0-r2v", 40)):
        out.append(
            ModelSpec(
                provider=PROVIDER_ALIYUN,
                key=key,
                capability="video.r2v",
                name=f"参考生视频 {key}",
                region="cn-beijing",
                api=_async_video_api(),
                params=hh_r2v_params,
                pricing=VIDEO_PRICE,
                notes="输入为参考图像 + 提示词。",
                meta={"source_doc": "参考生视频_hh.md", "verified": True, "priority": priority},
            )
        )

    hh_edit_params = _video_params(
        resolution_options=("720P", "1080P"),
        watermark_default=True,
        input_fields=[
            _f(
                "input.prompt",
                "编辑指令",
                FieldType.TEXTAREA,
                required=True,
                max_length=5000,
                doc="描述要做的编辑。该文档未见 duration 参数，时长由模型自行控制。",
            ),
            _media_field(doc=_VIDEO_EDIT_MEDIA_DOC, label="原视频"),
        ],
    )
    for group in hh_edit_params.groups:
        group.fields = [f for f in group.fields if f.key != "parameters.duration"]
    out.append(
        ModelSpec(
            provider=PROVIDER_ALIYUN,
            key="happyhorse-1.0-video-edit",
            capability="video.edit",
            name="视频编辑 happyhorse-1.0-video-edit",
            region="cn-beijing",
            api=_async_video_api(),
            params=hh_edit_params,
            pricing=_price(CostUnit.SECOND, "按秒计费。"),
            notes="该文档未见 duration 参数，时长由模型自行控制。",
            meta={"source_doc": "视频编辑_hh.md", "verified": True, "priority": 20},
        )
    )

    # ── 图生视频 _hh：文档存疑，登记但停用 ────────────────────────
    out.append(
        ModelSpec(
            provider=PROVIDER_ALIYUN,
            key="happyhorse-1.1-i2v",
            capability="video.i2v",
            name="图生视频 happyhorse-1.1-i2v（存疑）",
            region="cn-beijing",
            enabled=False,
            api=_async_video_api(),
            params=hh_video_params,
            pricing=VIDEO_PRICE,
            notes="**文档存疑**：`图生视频_hh.md` 里列出的模型名是 happyhorse-1.1-t2v / "
            "happyhorse-1.0-t2v（与文生视频文档同名），疑为文档笔误。此处按 i2v 命名登记但"
            "默认停用，核对文档后再启用 —— 宁可缺一个模型，不要错一个模型名。",
            meta={
                "source_doc": "图生视频_hh.md",
                "verified": False,
                "priority": 50,
                "blocked_by": "模型名待核对",
            },
        )
    )

    # ── 图像：千问图像生成 ────────────────────────────────────────
    qwen_image_params = ParamsSchema(
        groups=[
            GroupSpec(
                key="input",
                label="输入",
                description="多模态 messages 结构：content 数组内放 text 与 image_url。",
                fields=[
                    _f(
                        "input.messages",
                        "对话消息",
                        FieldType.JSON,
                        required=True,
                        doc="OpenAI 风格的消息数组。文生图只放一条 user 消息；图像编辑/多图融合时"
                        "在同一条 content 里追加 image_url 项。",
                        placeholder='[{"role":"user","content":[{"text":"一只在月光下奔跑的猫"}]}]',
                    ),
                    _f(
                        "input.negative_prompt",
                        "反向提示词",
                        FieldType.TEXTAREA,
                        doc="描述不希望出现的内容。",
                        advanced=True,
                    ),
                ],
            ),
            GroupSpec(
                key="parameters",
                label="图像参数",
                fields=[
                    _f(
                        "parameters.size",
                        "输出尺寸",
                        FieldType.TEXT,
                        default="1328*1328",
                        billed=True,
                        doc="输出图像宽*高，如 1328*1328。**直接影响费用**。",
                    ),
                    _f(
                        "parameters.n",
                        "生成张数",
                        FieldType.INT,
                        default=1,
                        min=1,
                        max=6,
                        billed=True,
                        doc="一次请求生成的图像数量，默认 1。**按张计费**。",
                    ),
                    _f(
                        "parameters.prompt_extend",
                        "提示词智能改写",
                        FieldType.BOOL,
                        default=True,
                        doc="默认开启（建议开启）。取值由请求是否开启改写以及改写执行结果决定。",
                    ),
                    _f(
                        "parameters.watermark",
                        "AI 生成水印",
                        FieldType.BOOL,
                        default=False,
                        doc="默认 false，表示不添加水印。",
                        advanced=True,
                    ),
                    _seed_field(),
                ],
            ),
        ]
    )
    for key, priority, note in (
        ("qwen-image-3.0-pro", 10, "专业版"),
        ("qwen-image-3.0", 20, "标准版"),
    ):
        out.append(
            ModelSpec(
                provider=PROVIDER_ALIYUN,
                key=key,
                capability="image.t2i",
                name=f"文生图 {key}（{note}）",
                region="cn-beijing",
                api=ApiSpec(
                    method="POST",
                    path=SYNC_IMAGE_PATH,
                    headers={"Content-Type": "application/json"},
                    mode="sync",
                ),
                params=qwen_image_params,
                pricing=IMAGE_PRICE,
                notes="同一模型兼顾文生图、图生图与图像编辑：区别只在 messages 里有没有 image_url。",
                doc_url="https://help.aliyun.com/zh/model-studio/qwen-image-api-reference",
                meta={"source_doc": "千问图像生成.md", "verified": True, "priority": priority},
            )
        )

    # ── 图像：万象 2.7 / 2.6 ──────────────────────────────────────
    wan_image_params = ParamsSchema(
        groups=[
            GroupSpec(
                key="input",
                label="输入",
                fields=[
                    _f(
                        "input.prompt",
                        "提示词",
                        FieldType.TEXTAREA,
                        required=True,
                        max_length=5000,
                        doc="文本提示词。参考图编辑时直接描述要改什么，"
                        "如「参考这个风格的图片，生成番茄炒蛋」。",
                    ),
                    _f(
                        "input.negative_prompt",
                        "反向提示词",
                        FieldType.TEXTAREA,
                        max_length=500,
                        doc="例：低分辨率，低画质，肢体畸形，手指畸形，画面过饱和，蜡像感，"
                        "人脸无细节，过度光滑，画面具有AI感。",
                        advanced=True,
                    ),
                    _f(
                        "input.ref_image_url",
                        "参考图 URL",
                        FieldType.URL,
                        doc="图生图 / 图像编辑时的参考图，必须是供应商可拉取的地址"
                        "（公网 URL 或 OSS 临时链接）。",
                        advanced=True,
                    ),
                ],
            ),
            GroupSpec(
                key="parameters",
                label="图像参数",
                fields=[
                    _f(
                        "parameters.ratio",
                        "宽高比",
                        FieldType.ENUM,
                        required=True,
                        default="1:1",
                        options=("1:1", "16:9", "9:16", "4:3", "3:4"),
                        billed=True,
                        doc="**必选**。生成图像的宽高比，决定输出像素规模，直接影响费用。",
                    ),
                    _f(
                        "parameters.n",
                        "生成张数",
                        FieldType.INT,
                        default=1,
                        min=1,
                        max=4,
                        billed=True,
                        doc="取值范围 1-4，默认 1；开启组图模式时代表最大生成图像数量，"
                        "取值范围 1-12，默认 12。**按张计费**。",
                    ),
                    _f(
                        "parameters.size",
                        "输出尺寸",
                        FieldType.TEXT,
                        default="1376*768",
                        doc="输出图像宽*高，如 1376*768。",
                    ),
                    _f(
                        "parameters.watermark",
                        "AI 生成水印",
                        FieldType.BOOL,
                        default=False,
                        doc="默认 false，表示不添加水印。",
                        advanced=True,
                    ),
                    _seed_field(),
                ],
            ),
        ]
    )
    for key, priority, note in (
        ("wan2.7-image-pro", 10, "专业版"),
        ("wan2.7-image", 20, "标准版"),
        ("wan2.6-image", 30, "2.6 版本，支持流式返回 progress"),
    ):
        out.append(
            ModelSpec(
                provider=PROVIDER_ALIYUN,
                key=key,
                capability="image.t2i",
                name=f"文生图 {key}（{note}）",
                region="cn-beijing",
                api=ApiSpec(
                    method="POST",
                    path=IMAGE_PATH,
                    headers={"Content-Type": "application/json"},
                    mode="sync",
                ),
                params=wan_image_params,
                pricing=IMAGE_PRICE,
                notes="万象系列统一走 image-generation/generation，同步返回图像 URL。",
                doc_url="https://help.aliyun.com/zh/model-studio/wan-image-generation-api-reference",
                meta={
                    "source_doc": "万象图像生成2.7.md",
                    "verified": key != "wan2.6-image",
                    "priority": priority,
                },
            )
        )

    # ── 文本 ──────────────────────────────────────────────────────
    llm_params = ParamsSchema(
        groups=[
            GroupSpec(
                key="input",
                label="对话",
                fields=[
                    _f(
                        "messages",
                        "消息数组",
                        FieldType.JSON,
                        required=True,
                        doc="OpenAI 兼容的 messages：role 为 system/user/assistant，content 为文本；"
                        '视觉理解时 content 改为 [{"type":"image_url",...},{"type":"text",...}]。',
                        placeholder='[{"role":"user","content":"用一句话介绍你自己"}]',
                    ),
                    _f(
                        "temperature",
                        "温度",
                        FieldType.FLOAT,
                        default=0.7,
                        min=0.0,
                        max=2.0,
                        step=0.1,
                        doc="采样温度，越大越发散。默认 0.7。",
                    ),
                    _f(
                        "top_p",
                        "核采样",
                        FieldType.FLOAT,
                        default=0.8,
                        min=0.0,
                        max=1.0,
                        step=0.05,
                        doc="核采样阈值。",
                        advanced=True,
                    ),
                    _f(
                        "max_tokens",
                        "最大输出 token",
                        FieldType.INT,
                        min=1,
                        billed=True,
                        doc="限制本次生成的最大 token 数。**影响费用**。",
                    ),
                    _f(
                        "response_format",
                        "输出格式",
                        FieldType.ENUM,
                        default="text",
                        options=("text", "json_object"),
                        doc="json_object 时模型只输出 JSON 对象，适合结构化抽取。",
                    ),
                    _f(
                        "enable_thinking",
                        "思考模式",
                        FieldType.BOOL,
                        doc="混合思考模型专用：开启后先输出思考过程再给答案。",
                        advanced=True,
                    ),
                    _seed_field(),
                ],
            )
        ]
    )
    llm_api = ApiSpec(
        method="POST",
        path="/compatible-mode/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        mode="sync",
    )
    for key, ability, priority, note, verified in (
        ("qwen3.8-max", "llm.chat", 10, "旗舰文本模型", False),
        ("qwen-max", "llm.chat", 20, "上一代旗舰", True),
        ("qwen-plus", "llm.chat", 30, "均衡档，性价比首选", True),
        ("qwen-turbo", "llm.chat", 40, "最快最省", True),
        ("qwen-long", "llm.chat", 50, "超长上下文（文档级）", True),
        ("qwen-flash", "llm.chat", 60, "低延迟档", True),
        ("qwen-vl-max", "llm.vision", 10, "视觉理解旗舰", True),
        ("qwen-vl-plus", "llm.vision", 20, "视觉理解均衡档", True),
    ):
        out.append(
            ModelSpec(
                provider=PROVIDER_ALIYUN,
                key=key,
                capability=ability,
                name=f"{key}（{note}）",
                region="cn-beijing",
                api=llm_api,
                params=llm_params,
                pricing=TOKEN_PRICE,
                notes="走 OpenAI 兼容端点，任何 OpenAI 客户端可直接指向它。",
                doc_url="https://help.aliyun.com/zh/model-studio/compatibility-of-openai-with-dashscope",
                meta={
                    "source_doc": "llm.md",
                    "verified": verified,
                    "priority": priority,
                    "blocked_by": "" if verified else "模型名未在 llm.md 中确认，待核对",
                },
            )
        )

    # ── 语音：TTS / ASR（一期只登记，不接入）───────────────────────
    tts_params = ParamsSchema(
        groups=[
            GroupSpec(
                key="input",
                label="合成输入",
                fields=[
                    _f(
                        "input.text",
                        "待合成文本",
                        FieldType.TEXTAREA,
                        required=True,
                        doc="要合成的文本内容。",
                    ),
                    _f(
                        "input.voice",
                        "音色",
                        FieldType.TEXT,
                        required=True,
                        doc="音色名，如 Cherry / Ethan 等，取值见「音色列表」文档。**必选**。",
                    ),
                    _f(
                        "parameters.format",
                        "音频格式",
                        FieldType.ENUM,
                        default="mp3",
                        options=("mp3", "wav", "pcm", "opus"),
                        doc="输出音频格式。",
                    ),
                    _f(
                        "parameters.sample_rate",
                        "采样率",
                        FieldType.ENUM,
                        default="24000",
                        options=("8000", "16000", "22050", "24000", "44100", "48000"),
                        doc="输出音频采样率。",
                        advanced=True,
                    ),
                ],
            )
        ]
    )
    for key, priority in (
        ("qwen-audio-3.0-tts-flash", 10),
        ("qwen-audio-3.0-tts-plus", 20),
        ("cosyvoice-v3-flash", 30),
        ("cosyvoice-v3-plus", 40),
    ):
        out.append(
            ModelSpec(
                provider=PROVIDER_ALIYUN,
                key=key,
                capability="audio.tts",
                name=f"语音合成 {key}",
                region="cn-beijing",
                enabled=False,
                api=ApiSpec(
                    method="WEBSOCKET", path="/api-ws/v1/inference", headers={}, mode="sync"
                ),
                params=tts_params,
                pricing=CHAR_PRICE,
                notes="**一期未接入**：该家族以 WebSocket 流式为主（/api-ws/v1/inference），"
                "不是普通 HTTP 请求-响应，需要单独的传输实现。",
                doc_url="https://help.aliyun.com/zh/model-studio/qwen-tts",
                meta={
                    "source_doc": "语音合成qwen_tts.md",
                    "verified": False,
                    "transport": "websocket",
                    "priority": priority,
                    "blocked_by": "需 WebSocket 传输实现",
                },
            )
        )

    for key, priority in (
        ("fun-asr-realtime-2026-02-28", 10),
        ("fun-asr-realtime-2025-11-07", 20),
        ("fun-asr-flash-8k-realtime", 30),
        ("qwen-audio-3.0-asr-flash-streaming", 40),
    ):
        out.append(
            ModelSpec(
                provider=PROVIDER_ALIYUN,
                key=key,
                capability="audio.asr",
                name=f"语音识别 {key}",
                region="cn-beijing",
                enabled=False,
                api=ApiSpec(
                    method="WEBSOCKET", path="/api-ws/v1/inference", headers={}, mode="sync"
                ),
                params=ParamsSchema(groups=[]),
                pricing=_price(CostUnit.SECOND, "按音频秒数计费。"),
                notes="**一期未接入**：实时流式识别走 WebSocket"
                "（wss://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference）。",
                meta={
                    "source_doc": "语音识别funasr.md",
                    "verified": False,
                    "transport": "websocket",
                    "priority": priority,
                    "blocked_by": "需 WebSocket 传输实现",
                },
            )
        )

    # ── 文档缺口，显式登记（不假装做了）──────────────────────────
    out.append(
        ModelSpec(
            provider=PROVIDER_ALIYUN,
            key="qwen-audio-3.0-tts-voice-clone",
            capability="audio.tts.clone",
            name="声音复刻（文档缺口）",
            region="cn-beijing",
            enabled=False,
            api=ApiSpec(method="POST", path="/api/v1/services/aigc/voice-clone", mode="sync"),
            params=ParamsSchema(groups=[]),
            pricing=_price(CostUnit.REQUEST, "按次计费。"),
            notes="**无法建模**：`语音合成qwen_tts.md` 里没有声音复刻的接口参考"
            "（创建/查询、样本要求、计费口径都缺）。补齐文档后才有资格进这张表。",
            meta={
                "source_doc": "语音合成qwen_tts.md",
                "verified": False,
                "priority": 90,
                "blocked_by": "缺接口文档",
            },
        )
    )
    return out


MODELS: list[ModelSpec] = _models()

#: 能力分类 → 中文名（页面分栏用；顺序即展示顺序）
CATEGORY_LABELS: dict[str, str] = {
    "llm.chat": "文本对话",
    "llm.vision": "视觉理解",
    "llm.embedding": "文本向量",
    "llm.rerank": "重排序",
    "image.t2i": "文生图",
    "image.i2i": "图生图",
    "image.edit": "图像编辑",
    "video.t2v": "文生视频",
    "video.i2v": "图生视频",
    "video.r2v": "参考生视频",
    "video.edit": "视频编辑",
    "video.all": "全能视频",
    "audio.tts": "语音合成",
    "audio.tts.clone": "声音复刻",
    "audio.asr": "语音识别",
    "audio.music": "音乐生成",
}

CATEGORY_ORDER: list[str] = list(CATEGORY_LABELS)


def category_label(capability: str) -> str:
    if capability.startswith("media."):
        return "本地剪辑（ffmpeg）"
    return CATEGORY_LABELS.get(capability, capability)


def category_sort(capability: str) -> int:
    try:
        return CATEGORY_ORDER.index(capability)
    except ValueError:
        return len(CATEGORY_ORDER)


__all__ = [
    "CATEGORY_LABELS",
    "CATEGORY_ORDER",
    "MODELS",
    "PROVIDERS",
    "PROVIDER_ALIYUN",
    "category_label",
    "category_sort",
]
