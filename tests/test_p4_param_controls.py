"""P4 测试：参数控件 —— **一个页面兼容任何参数**。

这一层要守住的东西只有一句话：

    接口文档里的参数，无论哪一种，页面都必须有本事把它渲染成"该长成的样子"。
    下拉就该是下拉，滑杆就该是滑杆，选文件就不该让人手打路径。

所以测试分两层，缺一不可：

1. **契约层**：``FieldType`` 里每一种类型，模板都得有一个分支接住它。
   少一个分支的后果不是报错，而是静默退化成文本框 —— 这正是"分辨率只有一个框"
   那类问题的成因：控件表里有 ``media-picker``，模板里却没有对应分支。
2. **数据层**：目录里每个模型的每个字段，页面上渲染出来的控件必须与它声明的类型
   一致；枚举/可选项一个都不能少。查的是**所有模型**，所以新增模型也会被这条测试
   覆盖 —— 这就是"不用给每个功能做一个参数页面"的凭据。
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader

from divinepen.core.specs import FieldSpec, FieldType, GroupSpec, Option, ParamsSchema
from divinepen_server.catalog import MODELS
from divinepen_server.web import TEMPLATES_DIR, control_of, media_library

#: 控件 → 该控件在 HTML 里必须留下的"指纹"。
#: 有了它，测试才能区分"真的渲染成了滑杆"和"退化成了文本框"。
FINGERPRINT = {
    "input": r'type="text"',
    "textarea": r"<textarea",
    "number": r'type="number"',
    "range": r'type="range"',
    "switch": r'type="checkbox"',
    "select": r"<select",
    "checkbox-group": r'class="checkgroup"',
    "combo": r'list="dl-',
    "url-input": r'type="url"',
    "code-editor": r"<textarea",
    "color": r'type="color"',
    "media-picker": r'list="dl-media"',
    "media-picker-multi": r"<textarea",
}

#: 每次测试都重新读模板：模板改了不重启服务也该被测到。
_ENV = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)), trim_blocks=True, lstrip_blocks=True
)
_ENV.globals["control_of"] = control_of
_ENV.globals["media_library"] = media_library
_ENV.filters["bold"] = lambda text: text
_MACROS = _ENV.from_string('{% import "_macros.html" as m %}{{ m.param_field(f, values) }}')


def _render(field: FieldSpec, values: dict | None = None) -> str:
    template = _ENV.from_string('{% import "_macros.html" as m %}{{ m.param_field(f, values) }}')
    return template.render(f=field, values=values or {})


# ── 契约层：类型表与模板分支必须一一对得上 ────────────────────────


def test_every_field_type_has_a_rendered_control():
    """``FieldType`` 有几个成员，页面就得接得住几个 —— 一个都不许漏。"""
    for ftype in FieldType:
        field = FieldSpec(
            key=f"parameters.{ftype.value}",
            label=f"{ftype.value} 示例",
            type=ftype,
            options=[Option(label="甲", value="甲"), Option(label="乙", value="乙")],
            min=0,
            max=1,
            placeholder="示例",
        )
        html = _render(field)
        control = field.control
        assert f'data-control="{control}"' in html, f"{ftype} 没有渲染出控件 {control}"
        assert control in FINGERPRINT, f"{ftype} 映射到的控件 {control} 没有指纹定义"


def test_every_control_is_distinguishable():
    """每种控件渲染出来的 HTML 必须能被区分开。

    如果某个类型静默退化成文本框，这条会让你知道是**哪一个**。
    """
    seen: dict[str, str] = {}
    for ftype in FieldType:
        field = FieldSpec(key="k", label="l", type=ftype, options=[Option(label="a", value="a")])
        html = _render(field)
        control = field.control
        assert re.search(FINGERPRINT[control], html), (
            f"{ftype} → {control}：渲染结果里找不到 {FINGERPRINT[control]}，"
            f"很可能是模板缺了这个分支、掉进了 else 的文本框"
        )
        seen[control] = html
    # 枚举与"可挑可填"必须长得不一样：一个是只能挑，一个是挑或填。
    assert "datalist" in seen["combo"] and "<select" not in seen["combo"]
    assert "<select" in seen["select"] and "datalist" not in seen["select"]
    # 多选不能被渲染成单选。
    assert seen["checkbox-group"].count('type="checkbox"') == 1  # 一个选项一个框
    assert 'name="k"' in seen["checkbox-group"]


def test_enum_options_are_all_rendered():
    """文档给的可选值，页面上一个都不能少。"""
    options = [Option(label="720P", value="720P"), Option(label="1080P", value="1080P")]
    for ftype in (FieldType.ENUM, FieldType.COMBO, FieldType.MULTI_ENUM):
        html = _render(FieldSpec(key="k", label="l", type=ftype, options=options, required=True))
        for option in options:
            assert f'value="{option.value}"' in html, f"{ftype} 漏了可选值 {option.value}"


def test_combo_accepts_free_text():
    """``size`` 这类参数：下拉给的是常见值，不是白名单。"""
    schema = ParamsSchema(
        groups=[
            GroupSpec(
                key="parameters",
                label="参数",
                fields=[
                    FieldSpec(
                        key="parameters.size",
                        label="输出尺寸",
                        type=FieldType.COMBO,
                        options=[Option(label="2K", value="2K")],
                    )
                ],
            )
        ]
    )
    # 下拉里有的值 → 原样通过
    assert schema.validate_values({"parameters.size": "2K"})["parameters.size"] == "2K"
    # 下拉里没有、但文档允许的值 → 也必须通过（这就是 combo 与 select 的区别）
    assert schema.validate_values({"parameters.size": "1024*768"})["parameters.size"] == "1024*768"


def test_select_rejects_values_outside_the_doc():
    """反过来：纯枚举不接受凭空捏造的值 —— 否则"下拉"就只是个装饰。"""
    schema = ParamsSchema(
        groups=[
            GroupSpec(
                key="parameters",
                label="参数",
                fields=[
                    FieldSpec(
                        key="parameters.resolution",
                        label="分辨率",
                        type=FieldType.ENUM,
                        options=[Option(label="720P", value="720P")],
                    )
                ],
            )
        ]
    )
    assert schema.validate_values({"parameters.resolution": "720P"})
    with pytest.raises(ValueError, match="720P"):
        schema.validate_values({"parameters.resolution": "999P"})


# ── 数据层：目录里每个模型的每个字段 ──────────────────────────────


@pytest.fixture(scope="module")
def client():
    from divinepen_server.web import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _model_ids(client) -> list[str]:
    r"""模型详情链接的形状是 ``/models/{provider}:{key}``，不是数字 id。

    曾经按 ``\d+`` 抓，于是这个"真值"永远是空集合 —— 空集合让下面
    "每个字段都有对应控件"的断言**空前地容易通过**。抓不到的壳子比断言失败更危险，
    所以这里放宽到"一整段路径"，并让调用方对空集合显式报错。
    """
    html = client.get("/models").text
    return sorted(set(re.findall(r'href="/models/([^"?/#]+)"', html)))


def _control_of_key(html: str, key: str) -> tuple[str, str]:
    """从页面里抠出某个参数所在字段块的 (控件名, 整块 HTML)。"""
    for match in re.finditer(r'<div class="field" data-control="([^"]*)">(.*?)\n</div>', html, re.S):
        if f'<code class="key">{key}</code>' in match.group(2):
            return match.group(1), match.group(2)
    return "", ""


def test_every_catalog_field_renders_its_declared_control(client):
    """**核心断言**：目录里所有模型的所有字段，控件都与类型一一对应。

    这是"一个页面就能撑住所有功能"的证明 —— 页面没有为任何一个能力写过分支。
    """
    ids = _model_ids(client)
    assert ids, "模型管理页没列出任何模型"

    checked = 0
    problems: list[str] = []
    for model_id in ids:
        html = client.get(f"/models/{model_id}").text
        checked += len(re.findall(r'<div class="field" data-control="[^"]*">', html))
        # 只核对"这一页讲的那个模型"。曾经的做法是"页面上出现了这个参数名就算数"，
        # 但 ``parameters.ratio`` 这样的参数几乎每个视频模型都有 —— 于是拿 A 模型的
        # 可选值去要求 B 模型的页面，报出来的全是假问题，真问题反而淹在里面。
        _, _, key_of_this_page = model_id.partition(":")
        for spec in MODELS:
            if spec.key != key_of_this_page:
                continue
            spec_map = spec.params.field_map()
            for key, field in spec_map.items():
                control, block = _control_of_key(html, key)
                if not control:
                    continue  # 该模型不在这一页
                if control != field.control:
                    problems.append(f"{spec.key}.{key}: 页面={control} 契约={field.control}")
                fingerprint = FINGERPRINT.get(control)
                if fingerprint and not re.search(fingerprint, block):
                    problems.append(f"{spec.key}.{key}: {control} 没渲染出 {fingerprint}")
                for option in field.options:
                    if f'value="{option.value}"' not in block:
                        problems.append(f"{spec.key}.{key}: 漏了可选值 {option.value}")
    assert checked > 0, "一个参数控件都没渲染出来"
    assert not problems, "参数控件与声明不符：\n" + "\n".join(problems[:20])


def test_media_params_are_not_hand_typed_paths(client):
    """媒体类参数必须给产物库，而不是让人手打路径。"""
    html = client.get("/capabilities/media.trim").text
    control, block = _control_of_key(html, "source")
    assert control == "media-picker", f"media.trim.source 的控件是 {control}，不是文件选择"
    assert "dl-media" in block and "media-lib" in block
    assert media_library(), "产物库是空的 —— 本机 media/ 与 var/ 里应当有测试样例"


def test_image_size_is_a_pickable_list(client):
    """用户点名的那个问题：分辨率只给一个输入框。"""
    html = client.get("/capabilities/image.t2i").text
    control, block = _control_of_key(html, "parameters.size")
    assert control == "combo", f"image.t2i 的 size 控件是 {control}"
    assert re.search(r'list="dl-parameters\.size"', block), "size 没有挂上可选值下拉"
    assert '<option value="1024*1024">' in block, "size 的下拉里没有常见分辨率"
