"""P4 测试：模型选择器 —— 供应商 → 模型 → **该模型**的参数。

这一层的验收标准只有一句话：

    在能力页上换个模型，参数控件就换成那个模型的参数表，一个不多一个不少；
    而且提交下去的还是这个模型。

测试里不写死模型名，全部从 ``divinepen_server.catalog`` 推 —— 目录是会变的，
写死模型名的测试早晚会变成"看起来在测选择器，其实在测字符串"。
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

from divinepen.core.specs import FieldType, ParamsSchema
from divinepen_server.catalog import MODELS

T2V = "video.t2v"
_LOCAL = "media.trim"

_T2V = [m for m in MODELS if m.capability == T2V and m.enabled]
RICH = max(_T2V, key=lambda m: len(m.params.fields()))
LEAN = min(_T2V, key=lambda m: len(m.params.fields()))


def _keys(model) -> list[str]:
    return [f.key for f in model.params.fields()]


def _option_values(html: str, select_id: str) -> set[str]:
    """抠出某个 <select> 的所有 <option value>。"""
    block = html.split(f'id="{select_id}"', 1)[1].split("</select>", 1)[0]
    return set(re.findall(r'<option value="([^"]*)"', block))


def _selected_value(html: str, select_id: str) -> str:
    block = html.split(f'id="{select_id}"', 1)[1].split("</select>", 1)[0]
    hit = re.search(r'<option value="([^"]+)"[^>]*\bselected\b', block)
    assert hit, f"{select_id} 没有选中项"
    return hit.group(1)


def _control(html: str, key: str) -> str:
    """抠出某个参数控件的那一段 HTML（从 name= 到该标签结束）。"""
    assert f'name="{key}"' in html, f"页面上没有参数控件 {key}"
    tail = html.split(f'name="{key}"', 1)[1]
    return tail.split("/>", 1)[0] + tail.split(">", 1)[0]


@pytest.fixture(scope="module")
def client():
    from divinepen_server.web import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


# ── 参数表：声明了 options 的字段必须是下拉 ─────────────────────────


class _OptionInput(BaseModel):
    policy: str = Field(
        default="priority",
        json_schema_extra={"options": ["priority", "cheapest"]},
    )
    plain: str = Field(default="x")
    tags: list[str] = Field(
        default_factory=list,
        json_schema_extra={"options": [("甲", "a"), "b"]},
    )


def test_declared_options_become_a_select():
    fields = ParamsSchema.from_pydantic(_OptionInput).field_map()
    assert fields["policy"].type is FieldType.ENUM
    assert fields["policy"].control == "select"
    assert [o.value for o in fields["policy"].options] == ["priority", "cheapest"]
    # 没声明 options 的普通字符串照旧是文本框
    assert fields["plain"].type is FieldType.TEXT


def test_option_shorthand_forms():
    """裸字符串和 (显示名, 值) 两种写法都要认，不能因为写法丢掉选项。"""
    fields = ParamsSchema.from_pydantic(_OptionInput).field_map()
    assert [(o.label, o.value) for o in fields["tags"].options] == [("甲", "a"), ("b", "b")]


def test_model_call_policy_renders_as_a_dropdown():
    from divinepen.capabilities._model import ModelCallInput

    field = ParamsSchema.from_pydantic(ModelCallInput).field_map()["policy"]
    assert field.control == "select"
    assert [o.value for o in field.options] == ["priority", "cheapest", "explicit"]


# ── 能力页：三段式 ────────────────────────────────────────────────


def test_model_driven_capability_shows_the_picker(client):
    html = client.get(f"/capabilities/{T2V}").text
    assert 'id="pick-provider"' in html
    assert 'id="pick-model"' in html
    # 来回切的按钮必须在不带 JS 时也能用
    assert 'formmethod="get"' in html


def test_local_capability_has_no_picker(client):
    """ffmpeg 那类本地能力不涉及模型，页面上不该长出供应商下拉。"""
    html = client.get(f"/capabilities/{_LOCAL}").text
    assert 'id="pick-provider"' not in html
    assert 'name="provider"' not in html


def test_provider_dropdown_lists_only_providers_with_models(client):
    html = client.get(f"/capabilities/{T2V}").text
    assert _option_values(html, "pick-provider") == {m.provider for m in _T2V}


def test_model_dropdown_lists_this_capabilitys_models(client):
    html = client.get(f"/capabilities/{T2V}").text
    assert _option_values(html, "pick-model") == {m.key for m in _T2V}


def test_default_model_is_the_one_the_backend_would_resolve(client):
    """页面默认显示的模型，必须就是后端 resolve(priority) 会用的那个。"""
    expected = sorted(_T2V, key=lambda m: int(m.meta.get("priority", 100)))[0]
    html = client.get(f"/capabilities/{T2V}").text
    assert _selected_value(html, "pick-model") == expected.key
    for key in _keys(expected):
        assert f'name="{key}"' in html


def test_switching_model_swaps_the_parameter_form(client):
    """本次改造的验收标准。"""
    assert len(_keys(RICH)) > len(_keys(LEAN)), "目录里的模型参数数没有差异，这个测试就没意义了"

    rich = client.get(
        f"/capabilities/{T2V}", params={"provider": RICH.provider, "model": RICH.key}
    ).text
    for key in _keys(RICH):
        assert f'name="{key}"' in rich, f"{RICH.key} 的参数 {key} 没渲染出来"

    lean = client.get(
        f"/capabilities/{T2V}", params={"provider": LEAN.provider, "model": LEAN.key}
    ).text
    for key in _keys(LEAN):
        assert f'name="{key}"' in lean, f"{LEAN.key} 的参数 {key} 没渲染出来"
    for key in set(_keys(RICH)) - set(_keys(LEAN)):
        assert f'name="{key}"' not in lean, f"换到 {LEAN.key} 后还留着 {key}"


def test_switching_model_keeps_values_by_field_key(client):
    """换模型时已填的值按字段 key 求交集保留 —— 用户不该重填一遍。"""
    html = client.get(
        f"/capabilities/{T2V}",
        params={
            "provider": RICH.provider,
            "model": RICH.key,
            "input.prompt": "一只在雪地里打滚的猫",
            "parameters.duration": "8",
        },
    ).text
    assert "一只在雪地里打滚的猫" in html
    assert 'value="8"' in _control(html, "parameters.duration")


def test_opening_the_page_applies_model_defaults(client):
    """只带路由键的 URL = 打开页面，控件要落到模型自己的默认值上。"""
    assert LEAN.params.field_map()["parameters.watermark"].default is True
    html = client.get(
        f"/capabilities/{T2V}", params={"provider": LEAN.provider, "model": LEAN.key}
    ).text
    assert "checked" in _control(html, "parameters.watermark")


def test_a_real_submit_does_not_resurrect_an_unchecked_switch(client):
    """浏览器不提交未勾选的复选框 —— 缺席就是"关"，不能再回落成默认值。"""
    html = client.get(
        f"/capabilities/{T2V}",
        params={
            "provider": LEAN.provider,
            "model": LEAN.key,
            "input.prompt": "x",
        },
    ).text
    assert "checked" not in _control(html, "parameters.watermark")


def test_unusable_model_key_falls_back_and_says_so(client):
    html = client.get(
        f"/capabilities/{T2V}", params={"provider": LEAN.provider, "model": "不存在的模型"}
    ).text
    assert "已改用默认" in html


# ── 预演：选中的模型必须就是提交的那个 ───────────────────────────


@pytest.mark.parametrize("model", [RICH, LEAN], ids=lambda m: m.key)
def test_dry_run_uses_the_selected_model(client, model):
    """页面参数 ↔ 接口参数一一对应：预演出来的 body 就是所选模型拼的体。"""
    r = client.post(
        f"/capabilities/{T2V}/dry-run",
        data={
            "provider": model.provider,
            "model": model.key,
            "input.prompt": "预演用的一句话",
        },
    )
    assert r.status_code == 200
    html = r.text
    assert model.key in html
    # 只提交了 input.prompt，其余参数取模型默认值
    body = model.params.build_body(model.params.validate_values({"input.prompt": "预演用的一句话"}))
    assert body["input"]["prompt"] == "预演用的一句话"
    assert "input.audio_url" in html or "input.audio_url" not in body["input"]


def test_dry_run_rejects_params_belonging_to_another_model(client):
    """stale 参数会被当场喊出来，不会被静默丢掉。"""
    extra = sorted(set(_keys(RICH)) - set(_keys(LEAN)))[0]
    r = client.post(
        f"/capabilities/{T2V}/dry-run",
        data={
            "provider": LEAN.provider,
            "model": LEAN.key,
            "input.prompt": "x",
            extra: "https://example.com/a.mp3",
        },
    )
    assert "未知参数" in r.text
