"""图像生成能力（模型绑定薄壳）。同步返回图像 URL。"""

from __future__ import annotations

from ...core.manifest import Category, CostUnit
from .._model import model_capability

AliyunImage = model_capability(
    cap_id="image.t2i",
    name="文生图 / 图像编辑",
    description="千问图像与万象系列。文生图、图生图、编辑共用入口，区别在参数里有没有参考图。",
    category=Category.IMAGE,
    model_capability="image.t2i",
    cost_unit=CostUnit.IMAGE,
    cost_notes="按生成张数计费",
    cacheable=True,
    tags=["阿里系", "图像"],
)

__all__ = ["AliyunImage"]
