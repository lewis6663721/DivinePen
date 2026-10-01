"""P4 测试：能力目录分两级 + 侧栏分组可折叠。

验收标准两条：

1. ``/capabilities`` 不再平铺 16 张卡片，只回答"有哪几个区、各区多少个"；
   点进 ``?zone=xxx`` 才列该区的能力 —— 且列出来的**不多不少**就是那一区。
2. 侧栏分组是一个个 ``<details>``，当前页所在的分组默认展开、标题行带 ``data-current``，
   收起状态下也能看出"我现在在这儿"。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import divinepen_server
from divinepen_server.catalog import MODELS  # noqa: F401  （确保 app 依赖已就绪）

ZONES = ("toolkit", "editing", "agent")


@pytest.fixture(scope="module")
def client():
    from divinepen_server.web import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def truth(client) -> dict[str, set[str]]:
    """每个区**应该**有哪些能力 —— 直接问注册表，不从页面反推。"""
    from divinepen.core.registry import build_default_registry

    out = {k: set() for k in ZONES}
    for kind, items in build_default_registry().by_kind().items():
        out[kind] = {i["id"] for i in items}
    return out


def _cap_links(html: str) -> set[str]:
    """页面上指向具体能力详情的链接集合。"""
    return set(re.findall(r'href="/capabilities/([^"?][^"]*)"', html))


def _zone_links(html: str) -> set[str]:
    return set(re.findall(r'href="/capabilities\?zone=([a-z]+)"', html))


# ── 一级页 ──────────────────────────────────────────────────────


def test_index_lists_the_three_zones(client):
    html = client.get("/capabilities").text
    assert _zone_links(html) == set(ZONES)


def test_index_does_not_dump_every_capability(client, truth):
    """一级页的价值就在于"不下滑" —— 一张能力卡片都不该出现。"""
    html = client.get("/capabilities").text
    assert _cap_links(html) == set()
    assert sum(len(v) for v in truth.values()) > 0, "注册表是空的，这个测试没意义"


def test_index_shows_per_zone_counts(client, truth):
    html = client.get("/capabilities").text
    for key in ZONES:
        block = html.split(f'/capabilities?zone={key}"', 1)[1].split("</a>", 1)[0]
        assert f">{len(truth[key])}<" in block


def test_unknown_zone_falls_back_to_the_index(client):
    html = client.get("/capabilities", params={"zone": "不存在"}).text
    assert _zone_links(html) == set(ZONES)
    assert _cap_links(html) == set()


# ── 二级页 ──────────────────────────────────────────────────────


@pytest.mark.parametrize("zone", ZONES)
def test_zone_page_lists_exactly_that_zone(client, truth, zone):
    html = client.get("/capabilities", params={"zone": zone}).text
    assert _cap_links(html) == truth[zone]


def test_zone_page_can_jump_to_the_other_zones(client):
    """进错区了不该退回一级页再点一次。"""
    html = client.get("/capabilities", params={"zone": "toolkit"}).text
    assert _zone_links(html) == set(ZONES)
    assert 'aria-current="page"' in html.split('class="ztabs"', 1)[1].split("</nav>", 1)[0]


def test_agent_zone_groups_by_run_mode(client, truth):
    html = client.get("/capabilities", params={"zone": "agent"}).text
    for mode in ("pipeline", "interactive", "react"):
        if any(mode in c for c in truth["agent"]):
            assert f"<b style=\"color:var(--signal)\">{mode} · " in html


# ── 侧栏 ────────────────────────────────────────────────────────


def test_sidebar_groups_are_collapsible(client):
    html = client.get("/capabilities").text
    groups = re.findall(r'<details class="navgroup"[^>]*>', html)
    assert len(groups) == 3
    assert all('data-remember="nav:' in g for g in groups)


def test_current_sidebar_group_is_open_and_marked(client):
    """当前分组默认展开；同时带 data-current，收起后仍能看出当前在哪。"""
    html = client.get("/capabilities").text
    current = [g for g in re.findall(r'<details class="navgroup"[^>]*>', html) if "data-current" in g]
    assert len(current) == 1
    assert "open" in current[0]
    # 能力目录属于"模型与能力"这一组
    assert 'data-remember="nav:模型与能力"' in current[0]


def test_capability_detail_keeps_the_same_group_open(client):
    html = client.get("/capabilities/media.trim").text
    current = [g for g in re.findall(r'<details class="navgroup"[^>]*>', html) if "data-current" in g]
    assert len(current) == 1
    assert 'data-remember="nav:模型与能力"' in current[0]


def test_other_pages_mark_their_own_group(client):
    html = client.get("/credentials").text
    current = [g for g in re.findall(r'<details class="navgroup"[^>]*>', html) if "data-current" in g]
    assert len(current) == 1
    assert 'data-remember="nav:系统"' in current[0]


# ── 侧栏默认隐藏（悬停滑出）──────────────────────────────────────


def test_sidebar_is_wrapped_for_offcanvas(client):
    html = client.get("/").text
    assert '<div class="navwrap" id="navwrap">' in html
    # 侧栏必须**在**热区容器里面：滑出动画靠 .navwrap:hover 触发
    inside = html.split('<div class="navwrap" id="navwrap">', 1)[1].split("</aside>", 1)[0]
    assert '<aside class="side">' in inside
    assert inside.count('<div class="side-top">') == 1


def test_sidebar_has_a_pin_button(client):
    html = client.get("/").text
    assert 'id="navpin"' in html
    assert 'aria-pressed="false"' in html
    assert "钉住" in html


def test_navwrap_precedes_main_in_the_shell(client):
    """主内容在最外层只有 .shell 一个网格，侧栏换列时不能把正文挤到别处。"""
    html = client.get("/").text
    shell = html.split('<div class="shell">', 1)[1]
    assert shell.index('id="navwrap"') < shell.index('<main class="main">')


def test_offcanvas_rules_exist_in_css():
    """CSS 是这功能的唯一实现，删一行就悄悄失效 —— 静态守一下。"""
    css = (Path(divinepen_server.__file__).parent / "static" / "app.css").read_text(
        encoding="utf-8"
    )
    assert ".navwrap{position:fixed" in css
    assert "transform:translateX(-100%)" in css
    # 悬停 / 键盘聚焦 / 翻页后的 peek 三条都要能把它拉出来
    assert ".navwrap:hover > .side,.navwrap:focus-within > .side,.navwrap.peek > .side" in css
    # 钉住 = 回到常驻列，正文让位
    assert "body.nav-pinned .shell{grid-template-columns:236px 1fr}" in css
    # 收起时留把手，否则没人知道左边能拉
    assert ".navwrap::after" in css


def test_offcanvas_scripts_are_wired():
    js = (Path(divinepen_server.__file__).parent / "static" / "app.js").read_text(encoding="utf-8")
    assert 'getElementById("navwrap")' in js
    assert 'getElementById("navpin")' in js
    assert '"nav-pinned"' in js
    assert "pointerleave" in js  # peek 的兜底收起
