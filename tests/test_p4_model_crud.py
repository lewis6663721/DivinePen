"""P4 测试：模型的增 / 改 / 删。

目录是 seed 进来的，但目录盖不住现实 —— 自建服务、供应商刚上线的型号、
内部代理，都得有个地方登记。所以模型管理页不只是"看一眼"，它要能建、能改、
能删。这一层守的是三条底线：

1. **填什么就是什么**：表单里的参数路径、类型、可选值一个不丢地落进库；
2. **错要错在那一格**：校验失败得指到具体某一行，而不是笼统地说"保存失败"，
   还得把已经填好的东西原样还回去 —— 重填一遍是最没道理的惩罚；
3. **删之前先看有没有人指着它**：任务记录里存的是模型名，删了就对不上账。

另外还有一条不那么显眼、但已经埋过一次雷的：**勾选框不勾就不提交**。
所以参数行的字段名里带着行号（``p_key.3``），绝不靠提交顺序对齐 ——
否则第二行勾了、第一行没勾，"勾"就会滑到第一行头上。见
``test_unchecked_boxes_cannot_slide_to_another_row``。
"""

from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import FormData

from divinepen_server import modelform as mf


def run(coro):
    return asyncio.run(coro)


# ── 表单层 ────────────────────────────────────────────────────


def form(*pairs) -> FormData:
    return FormData(list(pairs))


def test_form_layer_keeps_every_column():
    spec = mf.build_spec(
        form(
            ("provider", "aliyun-bailian"),
            ("key", "wan2.7-t2v-2026-06-12"),
            ("capability", "video.t2v"),
            ("path", "/api/v1/services/aigc/video-generation/video-synthesis"),
            ("enabled", "1"),
            ("p_key.0", "input.prompt"),
            ("p_label.0", "提示词"),
            ("p_type.0", "text"),
            ("p_required.0", "1"),
            ("p_billed.0", "1"),
            ("p_key.1", "parameters.resolution"),
            ("p_label.1", "分辨率"),
            ("p_type.1", "enum"),
            ("p_options.1", "480P\n720P=720P"),
            ("p_default.1", "720P"),
            ("p_desc.1", "文档原文：输出分辨率"),
            ("p_key.2", "parameters.duration"),
            ("p_type.2", "int"),
            ("p_default.2", "5"),
            ("p_advanced.2", "1"),
        )
    )
    fields = {f.key: f for g in spec.params.groups for f in g.fields}
    assert set(fields) == {"input.prompt", "parameters.resolution", "parameters.duration"}
    prompt = fields["input.prompt"]
    assert prompt.required and prompt.billed
    assert prompt.label == "提示词"
    resolution = fields["parameters.resolution"]
    # 只写一个词时，显示名与提交值相同 —— 多数文档只给提交值
    assert [(o.label, o.value) for o in resolution.options] == [("480P", "480P"), ("720P", "720P")]
    assert resolution.default == "720P"
    assert resolution.description == "文档原文：输出分辨率"
    # 按类型转成真类型，不是字符串
    assert fields["parameters.duration"].default == 5
    assert fields["parameters.duration"].advanced is True
    assert spec.enabled is True
    # 分组：没写分组的按 input 收，写在路径里的那一层要跟着走
    assert [g.key for g in spec.params.groups] == ["input", "parameters"]


def test_unchecked_boxes_cannot_slide_to_another_row():
    """复选框只提交勾上的那些 —— 所以它们绝不能和其它字段一样靠位置对齐。"""
    spec = mf.build_spec(
        form(
            ("provider", "p"),
            ("key", "m"),
            ("capability", "c"),
            ("path", "/x"),
            # 第 0 行什么都没勾，第 1 行三样全勾
            ("p_key.0", "input.a"),
            ("p_type.0", "text"),
            ("p_key.1", "input.b"),
            ("p_type.1", "text"),
            ("p_required.1", "1"),
            ("p_billed.1", "1"),
            ("p_advanced.1", "1"),
        )
    )
    a, b = (f for g in spec.params.groups for f in g.fields)
    assert (a.required, a.billed, a.advanced) == (False, False, False)
    assert (b.required, b.billed, b.advanced) == (True, True, True)


def test_a_deleted_row_leaves_a_hole_not_a_shift():
    """删掉中间一行只会留个空行号，别人的值不会滑过来。"""
    spec = mf.build_spec(
        form(
            ("provider", "p"),
            ("key", "m"),
            ("capability", "c"),
            ("path", "/x"),
            ("p_key.0", "input.a"),
            ("p_type.0", "text"),
            ("p_key.7", "input.g"),
            ("p_type.7", "text"),
            ("p_billed.7", "1"),
        )
    )
    fields = {f.key: f for g in spec.params.groups for f in g.fields}
    assert set(fields) == {"input.a", "input.g"}
    assert fields["input.a"].billed is False
    assert fields["input.g"].billed is True


def test_errors_point_at_the_offending_row():
    with pytest.raises(mf.FormError) as exc:
        mf.build_spec(
            form(
                ("provider", "p"),
                ("key", "m"),
                ("capability", "c"),
                ("path", "/x"),
                ("p_key.0", "input.prompt"),
                ("p_type.0", "text"),
                ("p_key.1", "input.style"),
                ("p_type.1", "enum"),
                ("p_options.1", ""),
                ("p_key.2", "input.prompt"),
                ("p_type.2", "text"),
                ("p_key.3", "input .b ad"),
                ("p_type.3", "text"),
                ("p_key.9", "input.z"),
                ("p_type.9", "mm"),
            )
        )
    errors = exc.value.errors
    assert "p_options.1" in errors, "单选不给可选值 → 必须指到那一行"
    assert "p_key.2" in errors and "重复" in errors["p_key.2"]
    assert "p_key.3" in errors and "点号路径" in errors["p_key.3"]
    assert "p_type.9" in errors
    # 报错只该落在出错的行上：没出错的行不许被牵连
    assert not [k for k in errors if k.startswith("p_key.0")]


def test_a_missing_must_have_field_says_so():
    with pytest.raises(mf.FormError) as exc:
        mf.build_spec(form(("provider", "p"), ("key", "m"), ("path", "/x"), ("p_type.0", "text")))
    assert "capability" in exc.value.errors


def test_pricing_needs_a_price():
    with pytest.raises(mf.FormError) as exc:
        mf.build_spec(
            form(
                ("provider", "p"),
                ("key", "m"),
                ("capability", "c"),
                ("path", "/x"),
                ("pricing_unit", "second"),
            )
        )
    assert "pricing_unit_price" in exc.value.errors
    # 免费就不该逼人填单价
    free = mf.build_spec(
        form(
            ("provider", "p"),
            ("key", "m"),
            ("capability", "c"),
            ("path", "/x"),
            ("pricing_unit", "free"),
        )
    )
    assert free.pricing.unit_price is None


def test_broken_json_in_headers_is_a_field_error_not_a_crash():
    with pytest.raises(mf.FormError) as exc:
        mf.build_spec(
            form(
                ("provider", "p"),
                ("key", "m"),
                ("capability", "c"),
                ("path", "/x"),
                ("headers", '{"X-Async": enable}'),
            )
        )
    assert "headers" in exc.value.errors

    ok = mf.build_spec(
        form(
            ("provider", "p"),
            ("key", "m"),
            ("capability", "c"),
            ("path", "/x"),
            ("headers", '{"X-DashScope-Async": "enable"}'),
            ("mode", "async_task"),
            ("poll_interval", "3"),
            ("poll_succeeded", "SUCCEEDED, SUCCESS"),
        )
    )
    assert ok.api.headers == {"X-DashScope-Async": "enable"}
    assert ok.api.poll.interval_seconds == 3.0
    assert ok.api.poll.succeeded == ["SUCCEEDED", "SUCCESS"]
    # 同步模式不该凭空长出一个轮询契约
    assert (
        mf.build_spec(
            form(("provider", "p"), ("key", "m"), ("capability", "c"), ("path", "/x"))
        ).api.poll
        is None
    )


def test_echo_form_gives_back_what_you_typed():
    echoed = mf.echo_form(
        form(
            ("provider", "p"),
            ("key", "m"),
            ("notes", "我填的备注"),
            ("p_key.0", "input.a"),
            ("p_billed.0", "1"),
            ("p_options.0", "x\ny"),
        )
    )
    assert echoed["notes"] == "我填的备注"
    assert echoed["rows"][0]["billed"] is True
    assert echoed["rows"][0]["options"] == "x\ny"


def test_verified_mark_tracks_whether_the_params_actually_moved():
    """核对印记是"我逐字对过文档"。改个备注不该把它抹掉；
    参数动过了才该退回 —— 印记乱掉就等于没有印记。"""
    base_form = form(
        ("provider", "p"),
        ("key", "m"),
        ("capability", "c"),
        ("path", "/x"),
        ("p_key.0", "input.prompt"),
        ("p_type.0", "text"),
        ("p_label.0", "提示词"),
    )
    first = mf.build_spec(base_form)
    stored = {
        "provider": "p",
        "key": "m",
        "verified": 1,
        "source_doc": "视频生成.md",
        "priority": 42,
        "params_obj": json.loads(json.dumps(first.params.model_dump(mode="json"))),
    }
    notes_only = mf.build_spec(
        form(*[(k, v) for k, v in base_form.multi_items()] + [("notes", "改个错别字")]),
        existing=stored,
    )
    assert notes_only.meta["verified"] is True
    assert notes_only.meta["source_doc"] == "视频生成.md"
    assert notes_only.meta["priority"] == 42

    moved = mf.build_spec(
        form(
            ("provider", "p"),
            ("key", "m"),
            ("capability", "c"),
            ("path", "/x"),
            ("p_key.0", "input.prompt"),
            ("p_type.0", "textarea"),
        ),
        existing=stored,
    )
    assert moved.meta["verified"] is False


# ── 页面层 ────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def client():
    from divinepen_server.web import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def instance():
    """只用来做 monkeypatch。**不要**从测试线程调用它的 repo：
    aiosqlite 的连接绑在 app 自己的事件循环上，跨循环用会炸，
    而"跨循环能凑合跑"本身就是不该依赖的东西。事实一律从页面取。"""
    from divinepen_server import web as web_module

    return web_module.state["app"]


def _flash(response) -> str:
    """重定向里带的话要解码后再断言 —— flash_url 会把中文百分号编码，
    直接拿原始 Location 去找中文，永远找不到，看着像"没提示"。"""
    return unquote(response.headers["location"])


def _first_provider(client) -> str:
    """从新增页的供应商下拉里取第一个真选项 —— 空的那条是"选一个供应商"。"""
    html = client.get("/models/new").text
    match = re.search(r'<select name="provider">.*?<option value="([^"]+)"', html, re.S)
    assert match, "新增页没有列出任何供应商"
    return match.group(1)


# 注意：**必须**用 dict 传 data。这套 httpx + starlette TestClient 下，
# data=[(k, v), (k, v)] 会安安静静地发出一个空 body —— 页面于是老老实实报
# "provider 要选一个供应商"，看着像路由坏了，其实是测试没把数据发出去。
# 好在字段名里带了行号，每个键天然唯一，dict 就够用，不必牺牲表达力。
def _create_payload(provider: str, key: str) -> dict[str, str]:
    return {
        "provider": provider,
        "key": key,
        "capability": "pytest.tmp",
        "name": "测试用模型",
        "method": "POST",
        "path": "/api/v1/pytest",
        "mode": "sync",
        "transport": "http",
        "pricing_unit": "second",
        "pricing_unit_price": "0.7",
        "p_key.0": "input.prompt",
        "p_label.0": "提示词",
        "p_type.0": "text",
        "p_required.0": "1",
        "p_billed.0": "1",
        "p_key.1": "parameters.resolution",
        "p_label.1": "分辨率",
        "p_type.1": "enum",
        "p_options.1": "480P\n720P",
        "p_default.1": "720P",
    }


def test_the_row_editor_offers_every_field_type(client):
    """行编辑器里的"类型"下拉必须列全 —— 它就是"一个页面兼容任何参数"的那张表。

    这条测试盯的是一个**静默**故障：Jinja 宏读不到渲染上下文时不会报错，
    ``{% for %}`` 会安安静静地循环零次，页面上留下一个空下拉。
    空下拉比报错难查得多。
    """
    html = client.get("/models/new").text
    for value, text in mf.PARAM_TYPES:
        assert f'<option value="{value}"' in html, f"类型下拉里少了 {value}"
        assert text in html
    assert 'name="p_key.0"' in html


def test_the_same_row_markup_serves_both_the_form_and_the_js_template(client):
    """已有行和 JS 用的模板必须是同一份 markup，否则加一行才发现少个字段。"""
    html = client.get("/models/new").text
    assert 'name="p_key.__i__"' in html, "JS 行模板没渲染出来"
    assert html.count('name="p_required.') == html.count('name="p_key.')


def test_create_edit_delete_round_trip(client):
    provider = _first_provider(client)
    model_id = f"{provider}:pytest-tmp-model"
    assert client.get(f"/models/{model_id}").status_code == 404

    created = client.post(
        "/models/new", data=_create_payload(provider, "pytest-tmp-model"), follow_redirects=False
    )
    assert created.status_code == 303, created.text[:400]
    detail = client.get(f"/models/{model_id}")
    assert detail.status_code == 200
    assert "测试用模型" in detail.text
    # 新模型默认停用：参数没核过就不该能被调用
    assert "参数待核对" in detail.text

    listed = client.get("/models")
    assert f'href="/models/{model_id}"' in listed.text

    # 改：把分辨率改成单选、加一个可选值、并启用
    assert (
        client.post(
        f"/models/{model_id}/edit",
        data={
            **_create_payload(provider, "pytest-tmp-model"),
            "enabled": "1",
            "p_options.1": "480P\n720P\n1080P=1080P",
        },
        follow_redirects=False,
    )
    after = client.get(f"/models/{model_id}")
    assert "1080P" in after.text
    assert "启用" in after.text

    # 删：没被任务引用过，直接删掉
    deleted = client.post(f"/models/{model_id}/delete", follow_redirects=False)
    assert deleted.status_code == 303
    assert client.get(f"/models/{model_id}").status_code == 404


def test_editing_survives_a_validation_error(client):
    provider = _first_provider(client)
    response = client.post(
        "/models/new",
        data={
            "provider": provider,
            "key": "bad-key",
            "capability": "c",
            "path": "/x",
            "notes": "这段备注不能丢",
            "p_key.0": "input .broken",
            "p_type.0": "enum",
            "p_options.0": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 422
    assert "这段备注不能丢" in response.text, "校验失败后用户填的内容要原样还回去"
    assert "点号路径" in response.text


def test_identity_cannot_be_renamed(client):
    """供应商 + 模型名是身份：任务记录、计费口径、默认模型都挂在它身上。"""
    provider = _first_provider(client)
    model_id = f"{provider}:pytest-rename-me"
    assert (
        client.post(
            "/models/new",
            data=_create_payload(provider, "pytest-rename-me"),
            follow_redirects=False,
        ).status_code
        == 303
    )
    renamed = client.post(
        f"/models/{model_id}/edit",
        data={"provider": provider, "key": "something-else", "capability": "c", "path": "/x"},
        follow_redirects=False,
    )
    assert renamed.status_code == 303
    assert "不能改" in _flash(renamed)
    assert client.get(f"/models/{model_id}").status_code == 200, "原模型必须还在"
    assert client.get(f"/models/{provider}:something-else").status_code == 404
    client.post(f"/models/{model_id}/delete", follow_redirects=False)


def test_delete_is_refused_while_tasks_reference_it(client, instance, monkeypatch):
    """删除前的闸门。真正的引用计数另有存储层测试；这里盯的是路由有没有问过它。"""
    provider = _first_provider(client)
    model_id = f"{provider}:pytest-referenced"
    assert (
        client.post(
            "/models/new",
            data=_create_payload(provider, "pytest-referenced"),
            follow_redirects=False,
        ).status_code
        == 303
    )

    async def two_tasks(_model_id: str) -> int:
        return 2

    monkeypatch.setattr(instance.repo, "model_in_use", two_tasks)
    refused = client.post(f"/models/{model_id}/delete", follow_redirects=False)
    assert refused.status_code == 303
    assert "停用" in _flash(refused), "要告诉人下一步该干什么"
    assert client.get(f"/models/{model_id}").status_code == 200, "有引用就不许删"

    monkeypatch.undo()
    assert client.post(f"/models/{model_id}/delete", follow_redirects=False).status_code == 303
    assert client.get(f"/models/{model_id}").status_code == 404


def test_verify_mark_can_be_given_and_taken_back(client):
    provider = _first_provider(client)
    model_id = f"{provider}:pytest-verify-me"
    assert (
        client.post(
            "/models/new",
            data=_create_payload(provider, "pytest-verify-me"),
            follow_redirects=False,
        ).status_code
        == 303
    )
    assert (
        client.post(
            f"/models/{model_id}/verify", data={"verified": "1"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert "参数已核对文档" in client.get(f"/models/{model_id}").text
    assert (
        client.post(
            f"/models/{model_id}/verify", data={"verified": "0"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert "参数待核对" in client.get(f"/models/{model_id}").text
    client.post(f"/models/{model_id}/delete", follow_redirects=False)


def test_the_models_page_has_a_way_in(client):
    """页面上得有一个"新增模型"的入口 —— 功能存在但找不到入口等于不存在。"""
    html = client.get("/models").text
    assert 'href="/models/new"' in html
    assert re.search(r'href="/models/[^"]+/edit"', html), "列表里要能直接进编辑"


def test_duplicate_key_is_refused(client, instance):
    provider = _first_provider(client)
    first = client.post(
        "/models/new", data=_create_payload(provider, "pytest-dup"), follow_redirects=False
    )
    assert first.status_code == 303
    again = client.post(
        "/models/new", data=_create_payload(provider, "pytest-dup"), follow_redirects=False
    )
    assert again.status_code == 422
    assert "已经在库里了" in again.text
    client.post(f"/models/{provider}:pytest-dup/delete", follow_redirects=False)


# ── 存储层 ────────────────────────────────────────────────────


@pytest.fixture()
def repo(tmp_path):
    from divinepen_server.db import SqliteStore
    from divinepen_server.store import Repo, SecretBox

    return Repo(SqliteStore(tmp_path / "crud.sqlite3"), SecretBox("test-key-for-crud"))


def test_model_in_use_counts_the_task_rows_that_name_it(repo):
    provider, key = "aliyun-bailian", "wan2.7-t2v"

    async def scenario():
        assert await repo.model_in_use(f"{provider}:{key}") == 0
        await repo.create_task({"capability_id": "video.t2v", "provider": provider, "model": key})
        await repo.create_task({"capability_id": "video.t2v", "provider": provider, "model": key})
        assert await repo.model_in_use(f"{provider}:{key}") == 2
        # 同名但换了供应商，是另一个模型，不该被算进来
        assert await repo.model_in_use(f"somewhere-else:{key}") == 0

    run(scenario())


def test_created_model_survives_a_round_trip_through_the_store(repo):
    spec = mf.build_spec(
        form(
            ("provider", "aliyun-bailian"),
            ("key", "pytest-store-model"),
            ("capability", "image.t2i"),
            ("path", "/api/v1/x"),
            ("enabled", "1"),
            ("p_key.0", "input.prompt"),
            ("p_type.0", "text"),
            ("p_required.0", "1"),
            ("p_key.1", "parameters.size"),
            ("p_type.1", "enum"),
            ("p_options.1", "1024*1024\n2048*2048"),
            ("p_billed.1", "1"),
        )
    )

    async def scenario():
        assert await repo.upsert_model(spec, force=True) == "inserted"
        row = await repo.model("aliyun-bailian:pytest-store-model")
        assert row is not None
        assert [f["key"] for f in row["fields"]] == ["input.prompt", "parameters.size"]
        assert row["fields"][0]["required"] is True
        assert [o["value"] for o in row["fields"][1]["options"]] == ["1024*1024", "2048*2048"]
        assert [f["key"] for f in row["billed_fields"]] == ["parameters.size"]
        # 编辑页回填要能还原出这些行（含行号）
        back = mf.spec_to_form(row)
        assert [r["key"] for r in back["rows"]] == ["input.prompt", "parameters.size"]
        assert back["rows"][1]["options"] == "1024*1024\n2048*2048"
        assert back["enabled"] is True
        # 保存一份没改动的表单，核对印记不该丢
        echoed = mf.build_spec(
            form(
                *[(k, v) for k, v in back.items() if isinstance(v, str)]
                + [(f"p_key.{r['i']}", r["key"]) for r in back["rows"]]
                + [(f"p_type.{r['i']}", r["type"]) for r in back["rows"]]
                + [(f"p_label.{r['i']}", r["label"]) for r in back["rows"]]
                + [(f"p_group.{r['i']}", r["group"]) for r in back["rows"]]
                + [(f"p_default.{r['i']}", r["default"]) for r in back["rows"]]
                + [(f"p_options.{r['i']}", r["options"]) for r in back["rows"]]
                + [(f"p_desc.{r['i']}", r["desc"]) for r in back["rows"]]
                + [(f"p_required.{r['i']}", "1") for r in back["rows"] if r["required"]]
                + [(f"p_billed.{r['i']}", "1") for r in back["rows"] if r["billed"]]
                + [(f"p_advanced.{r['i']}", "1") for r in back["rows"] if r["advanced"]]
            ),
            existing=row,
        )
        assert mf._params_signature(echoed.params) == mf._params_signature(row["params_obj"])
        await repo.set_model_verified("aliyun-bailian:pytest-store-model", True)
        await repo.delete_model("aliyun-bailian:pytest-store-model")
        assert await repo.model("aliyun-bailian:pytest-store-model") is None

    run(scenario())
