"""文本 / 视觉理解能力（模型绑定薄壳）。"""

from __future__ import annotations

from ...core.manifest import Category, CostUnit
from .._model import model_capability

AliyunChat = model_capability(
    cap_id="llm.chat",
    name="文本对话",
    description="OpenAI 兼容端点的文本对话。参数与 llm.md 逐字对应（messages / temperature / …）。",
    category=Category.LLM,
    model_capability="llm.chat",
    cost_unit=CostUnit.TOKEN,
    cost_notes="按输入/输出 token 计费，以响应 usage 为准",
    tags=["阿里系", "文本"],
)

AliyunVision = model_capability(
    cap_id="llm.vision",
    name="视觉理解",
    description="看图说话：messages 的 content 里放 image_url + text。",
    category=Category.LLM,
    model_capability="llm.vision",
    cost_unit=CostUnit.TOKEN,
    cost_notes="按 token 计费，图像按像素折算 token",
    tags=["阿里系", "多模态"],
)

__all__ = ["AliyunChat", "AliyunVision"]
