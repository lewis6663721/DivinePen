"""body 组装契约：文档要什么形状，就发什么形状。

这个文件的由来是一次真实的 400：

    页面预览显示的是合法的 messages 数组，真实请求发出去的却是裸字符串。
    用户看到的是"照着文档填了、预览也对，怎么一提交就
    InvalidParameter content format is incorrect"。

根因不是参数抄错了，而是"扁平值 → 请求体"这件事有**两份实现**：
预览走服务端 ``assemble_body``，真实调用走核心 ``ParamsSchema.build_body``，
而文档要求的 messages 包法只加在了前者身上。

所以这里守两条，第二条比第一条重要：

1. **形状**：文档写 content 是 ``[{"text": ...}]`` 数组，就必须是数组；
2. **同源**：预览与真实提交必须出自同一个函数。
   第 1 条只能守住今天这个字段，第 2 条守住的是"以后没人能再偷偷分家"。
"""

from __future__ import annotations

from typing import Any

import pytest

from divinepen.core.specs import FieldSpec, FieldType, ParamsSchema, normalize_messages
from divinepen_server import runtime as rt


def _messages_schema(*, wrap: str | None = "messages") -> ParamsSchema:
    return ParamsSchema(
        groups=[
            {
                "key": "input",
                "label": "输入",
                "fields": [
                    {
                        "key": "input.messages",
                        "label": "描述",
                        "type": "textarea",
                        "required": True,
                        "wrap": wrap,
                    }
                ],
            },
            {
                "key": "parameters",
                "label": "图像参数",
                "fields": [
                    {"key": "parameters.size", "label": "尺寸", "type": "text"},
                    {"key": "parameters.n", "label": "数量", "type": "int"},
                ],
            },
        ]
    )


def _model_row(schema: ParamsSchema) -> dict[str, Any]:
    """照 ``repo.model()`` 的行结构造一条，给 ``preview_request`` 用。"""
    return {
        "params_obj": schema.model_dump(),
        "headers_obj": {"Content-Type": "application/json"},
        "path": "/api/v1/services/aigc/multimodal-generation/generation",
        "method": "POST",
        "poll_obj": None,
    }


# ── ① 形状：对着《千问图像生成》逐字核 ────────────────────────────


def test_a_plain_prompt_becomes_the_messages_array_the_doc_shows():
    """文档原文：``"messages": [{"role":"user","content":[{"text": "..."}]}]``。

    这一条就是那次 400 的直接断言 —— 裸字符串会在这里挂掉。
    """
    body = _messages_schema().build_body(
        {"input.messages": "小猫吃鱼", "parameters.size": "1920*1080", "parameters.n": 1}
    )
    assert body == {
        "input": {"messages": [{"role": "user", "content": [{"text": "小猫吃鱼"}]}]},
        "parameters": {"size": "1920*1080", "n": 1},
    }
    assert isinstance(body["input"]["messages"], list), "裸字符串就是这次 400 的原样"


def test_hand_built_json_messages_are_passed_through():
    """图生图要自己拼 image + text，拼好的 JSON 不能再包一层。"""
    raw = '[{"role":"user","content":[{"image":"https://x/a.png"},{"text":"改成夜景"}]}]'
    body = _messages_schema().build_body({"input.messages": raw})
    assert body["input"]["messages"][0]["content"][0]["image"] == "https://x/a.png"
    assert body["input"]["messages"][0]["content"][1]["text"] == "改成夜景"


def test_malformed_json_falls_back_to_plain_text():
    """用户想写 JSON 但括号写坏了 —— 他要的是出图，不是被校验括号。"""
    body = _messages_schema().build_body({"input.messages": "[一只猫"})
    assert body["input"]["messages"][0]["content"] == [{"text": "[一只猫"}]


def test_prose_that_starts_with_a_brace_does_not_crash():
    assert normalize_messages("{画面要亮}") == [
        {"role": "user", "content": [{"text": "{画面要亮}"}]}
    ]


def test_a_whitespace_only_prompt_counts_as_missing():
    """只打了几个空格的"必填描述"就是没填。

    以前它会一路走到供应商那儿，换来一句 ``InvalidParameter``；现在在页面
    校验阶段就说清楚缺的是哪个字段。
    """
    schema = _messages_schema()
    with pytest.raises(ValueError, match=r"input\.messages"):
        schema.validate_values({"input.messages": "   "})
    with pytest.raises(ValueError, match=r"input\.messages"):
        schema.validate_values({"input.messages": ""})


# ── ② 同源：预览即所发 ───────────────────────────────────────────


def test_the_preview_body_is_the_body_the_capability_actually_sends():
    """真实调用走 ``ModelSpec.params.build_body``；预览走 ``preview_request``。

    两者必须逐字节相同 —— 这是"预览即所发"的唯一判据，
    也是这次事故本可以在测试阶段就拦下来的地方。
    """
    schema = _messages_schema()
    values = {"input.messages": "小猫吃鱼", "parameters.size": "1024*1024", "parameters.n": 1}

    preview = rt.preview_request(
        _model_row(schema), values, base_url="https://dashscope.aliyuncs.com"
    )
    # _model.execute() 里就是这一句
    sent = schema.build_body(values)
    assert preview.body == sent


def test_preview_reports_the_wrapped_shape_not_the_raw_text():
    schema = _messages_schema()
    preview = rt.preview_request(
        _model_row(schema), {"input.messages": "小猫吃鱼"}, base_url="https://x"
    )
    assert preview.body["input"]["messages"][0]["role"] == "user"


# ── 别把不该包的也包了 ──────────────────────────────────────────


def test_the_llm_messages_field_is_left_alone():
    """``llm.chat`` 的字段名是 ``messages``（不带 input. 前缀），是 JSON 类型，
    本来就该由调用方整段给出。兜底规则不能误伤它。
    """
    schema = ParamsSchema(
        groups=[
            {
                "key": "input",
                "label": "对话",
                "fields": [
                    {"key": "messages", "label": "消息数组", "type": "json", "required": True}
                ],
            }
        ]
    )
    raw = [{"role": "user", "content": "用一句话介绍你自己"}]
    assert schema.build_body({"messages": raw}) == {"messages": raw}


def test_prompt_fields_are_left_as_plain_strings():
    """视频生成用 ``input.prompt``，文档要的就是字符串 —— 不能顺手包成数组。"""
    schema = ParamsSchema(
        groups=[
            {
                "key": "input",
                "label": "输入",
                "fields": [{"key": "input.prompt", "label": "提示词", "type": "textarea"}],
            }
        ]
    )
    assert schema.build_body({"input.prompt": "小猫吃鱼"}) == {"input": {"prompt": "小猫吃鱼"}}


# ── 老库兼容：DB 里那份 params 还没有 wrap ───────────────────────


def test_a_field_without_the_wrap_flag_is_still_wrapped_by_key():
    """``wrap`` 是后加的字段，DB 里的 params JSON 是上次导入写下的。

    不能因为"库里还没这个标记"就继续发裸字符串 —— 那正是这次的 400。
    目录重新导入后由 ``wrap="messages"`` 正式接管。
    """
    field = FieldSpec(key="input.messages", label="描述", type=FieldType.TEXTAREA)
    assert field.wrap is None
    schema = ParamsSchema(
        groups=[{"key": "input", "label": "输入", "fields": [field.model_dump()]}]
    )
    body = schema.build_body({"input.messages": "小猫吃鱼"})
    assert isinstance(body["input"]["messages"], list)


def test_the_seeded_catalog_marks_its_messages_field():
    """种子目录必须显式打标，别让线上永远靠兜底活着。"""
    from divinepen_server.catalog import _models

    marked = [
        (m.key, f.key) for m in _models() for f in m.params.fields() if f.key == "input.messages"
    ]
    assert marked, "目录里应该有吃 messages 的模型（千问图像）"
    for model_key, field_key in marked:
        model = next(m for m in _models() if m.key == model_key)
        spec = model.params.field_map()[field_key]
        assert spec.wrap == "messages", f"{model_key} 的 {field_key} 没打 wrap 标记"


def test_every_image_model_wraps_a_plain_prompt():
    """对着整本目录过一遍：凡是吃 ``input.messages`` 的模型，纯文本提示词
    都必须被包成数组 —— 覆盖到所有"将来新增的同系模型"，不只今天这两个。
    """
    from divinepen_server.catalog import _models

    checked = 0
    for model in _models():
        spec = model.params.field_map().get("input.messages")
        if spec is None:
            continue
        body = model.params.build_body({"input.messages": "一只猫"})
        assert isinstance(body["input"]["messages"], list), model.key
        checked += 1
    assert checked >= 2, f"应当覆盖到千问图像的 pro / 标准两个模型，实际 {checked}"


# ── 事故复现：用户那条 400 的载荷 ───────────────────────────────


def test_the_400_payload_from_the_task_log_now_builds_a_legal_body():
    """t_6a6430047227150b 的提交载荷原样重放。

    当时发出去的是 ``"input": {"messages": "小猫吃鱼"}`` → HTTP 400
    ``InvalidParameter content format is incorrect``。现在同一个载荷必须
    产出供应商要的形状。
    """
    from divinepen_server.catalog import _models

    payload = {
        "input.messages": "小猫吃鱼",
        "parameters.size": "1920*1080",
        "parameters.n": "1",
        "parameters.prompt_extend": "true",
        "parameters.seed": "0",
        "parameters.watermark": False,
    }
    model = next(m for m in _models() if m.key == "qwen-image-3.0-pro")
    schema = model.params
    normalized = schema.validate_values({k: _coerce_like_form(v) for k, v in payload.items()})
    body = schema.build_body(normalized)

    assert model.params.field_map()["input.messages"].wrap == "messages"
    messages = body["input"]["messages"]
    assert isinstance(messages, list), "发出裸字符串 = 复现 400"
    assert messages == [{"role": "user", "content": [{"text": "小猫吃鱼"}]}]
    assert body["parameters"]["seed"] == 0
    assert body["parameters"]["watermark"] is False


def _coerce_like_form(value: Any) -> Any:
    """表单送来的都是字符串；这里模拟一次页面提交的类型。"""
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value
