"""视频生成能力（模型绑定薄壳）。全部是异步任务：提交 → 轮询 → 收产物。"""

from __future__ import annotations

from ...core.manifest import Category, CostUnit
from .._model import model_capability

_VIDEO_COST = CostUnit.SECOND
_VIDEO_NOTES = "按输出视频秒数 × 分辨率档位计费；失败不扣费"

AliyunT2V = model_capability(
    cap_id="video.t2v",
    name="文生视频",
    description="提示词 → 视频。wan2.7 / happyhorse 系列，异步任务。",
    category=Category.VIDEO,
    model_capability="video.t2v",
    cost_unit=_VIDEO_COST,
    cost_notes=_VIDEO_NOTES,
    async_task=True,
    cacheable=True,
    tags=["阿里系", "视频"],
)

AliyunI2V = model_capability(
    cap_id="video.i2v",
    name="图生视频",
    description="首帧/首尾帧/驱动音频 → 视频。",
    category=Category.VIDEO,
    model_capability="video.i2v",
    cost_unit=_VIDEO_COST,
    cost_notes=_VIDEO_NOTES,
    async_task=True,
    cacheable=True,
    tags=["阿里系", "视频"],
)

AliyunR2V = model_capability(
    cap_id="video.r2v",
    name="参考生视频",
    description="参考图/参考视频 → 保持主体一致性的视频。",
    category=Category.VIDEO,
    model_capability="video.r2v",
    cost_unit=_VIDEO_COST,
    cost_notes=_VIDEO_NOTES,
    async_task=True,
    cacheable=True,
    tags=["阿里系", "视频"],
)

AliyunVideoEdit = model_capability(
    cap_id="video.edit",
    name="视频编辑",
    description="指令 + 原视频（可附参考图）→ 编辑后的视频。",
    category=Category.VIDEO,
    model_capability="video.edit",
    cost_unit=_VIDEO_COST,
    cost_notes=_VIDEO_NOTES,
    async_task=True,
    tags=["阿里系", "视频"],
)

AliyunVideoAll = model_capability(
    cap_id="video.all",
    name="全能视频（wan3.0）",
    description="文生/图生/参考生/编辑共用一个入口，时长上限 30s。",
    category=Category.VIDEO,
    model_capability="video.all",
    cost_unit=_VIDEO_COST,
    cost_notes=_VIDEO_NOTES,
    async_task=True,
    cacheable=True,
    tags=["阿里系", "视频"],
)

__all__ = ["AliyunI2V", "AliyunR2V", "AliyunT2V", "AliyunVideoAll", "AliyunVideoEdit"]
