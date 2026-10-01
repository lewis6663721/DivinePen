"""P4 测试：一个供应商可以配**多把**密钥，其中恰好一把是默认。

为什么需要这个能力：同一个供应商的额度往往是分开的（正式号 / 测试号 /
不同项目各申请一把），但"调用时用哪一把"必须唯一 —— 否则同样一次请求
在两个时间点可能走不同的账号，账单对不上。

因此本文件守住四条不变量：

* **总有一把默认**（配第一把时自动成为默认，删掉默认时自动顺延）；
* **默认只有一个**（切换是"先全清零再置一"，不留两条）；
* **取钥匙看默认**（"这个供应商能不能调"只有一个答案）；
* **测试结果落在被测试的那一把上**（按 id 记，不按供应商记）。

另有两件事一并守住：老库（一供应商一把钥匙的表结构）升级后**原有的钥匙不能失效**，
以及页面上同一个名字是**覆盖**而不是又加一把。
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PROVIDER = "aliyun-bailian"
OTHER = "openai"

#: 升级前的表结构：``UNIQUE(provider, workspace)`` 把"只能有一把"写死在表定义里。
OLD_SCHEMA = """
CREATE TABLE dp_credential (
  id            TEXT PRIMARY KEY,
  provider      TEXT NOT NULL,
  workspace     TEXT NOT NULL DEFAULT '',
  api_key       TEXT NOT NULL DEFAULT '',
  encrypted     INTEGER NOT NULL DEFAULT 0,
  last_test_at  TEXT,
  last_test_ok  INTEGER,
  last_test_note TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL DEFAULT '',
  updated_at    TEXT NOT NULL DEFAULT '',
  UNIQUE(provider, workspace)
);
"""


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def repo(tmp_path: Path):
    from divinepen_server.db import SqliteStore
    from divinepen_server.store import Repo
    from divinepen_server.store import SecretBox as Box

    return Repo(SqliteStore(tmp_path / "creds.sqlite3"), Box("test-key-for-credentials"))


@pytest.fixture(scope="module")
def client():
    from divinepen_server.web import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


async def _keys(repo, provider=PROVIDER) -> list[dict]:
    return await repo.credentials(provider)


async def _default_label(repo, provider=PROVIDER) -> str:
    row = await repo.credential(provider)
    return row["label"] if row else ""


# ── 总有一把默认 ────────────────────────────────────────────────


def test_first_key_becomes_default_on_its_own(repo):
    """配第一把钥匙不该还要用户再点一次"设为默认" —— 那等于配完用不了。"""
    run(repo.save_credential(PROVIDER, "sk-first"))
    assert run(_default_label(repo)) == "默认"
    assert run(repo.raw_api_key(PROVIDER)) == "sk-first"


def test_second_key_does_not_steal_the_default(repo):
    run(repo.save_credential(PROVIDER, "sk-first"))
    run(repo.save_credential(PROVIDER, "sk-second", label="测试"))
    assert run(_default_label(repo)) == "默认", "加一把备用钥匙，不该悄悄改掉默认"
    assert run(repo.raw_api_key(PROVIDER)) == "sk-first"


def test_switching_default_leaves_exactly_one(repo):
    run(repo.save_credential(PROVIDER, "sk-first"))
    run(repo.save_credential(PROVIDER, "sk-second", label="测试"))
    second = run(repo.credential(PROVIDER, "", "测试"))
    run(repo.set_default_credential(PROVIDER, second["id"]))

    keys = run(_keys(repo))
    assert [k["label"] for k in keys if k["is_default"]] == ["测试"], "默认必须唯一"
    assert run(repo.raw_api_key(PROVIDER)) == "sk-second"


def test_named_key_becomes_default_when_asked(repo):
    run(repo.save_credential(PROVIDER, "sk-first"))
    run(repo.save_credential(PROVIDER, "sk-second", label="测试", make_default=True))
    assert run(_default_label(repo)) == "测试"


def test_choosing_a_default_keeps_the_other_keys_usable(repo):
    """换默认只是换"调用走哪把"，另外几把不该消失或失效。"""
    run(repo.save_credential(PROVIDER, "sk-a"))
    run(repo.save_credential(PROVIDER, "sk-b", label="测试"))
    second = run(repo.credential(PROVIDER, "", "测试"))
    run(repo.set_default_credential(PROVIDER, second["id"]))
    assert run(repo.raw_api_key(PROVIDER, "", "默认")) == "sk-a"


# ── 覆盖，而不是又加一把 ────────────────────────────────────────


def test_same_name_overwrites_instead_of_piling_up(repo):
    run(repo.save_credential(PROVIDER, "sk-old", label="测试"))
    run(repo.save_credential(PROVIDER, "sk-new", label="测试"))
    keys = run(_keys(repo))
    assert [k["label"] for k in keys] == ["测试"], "同名应当是覆盖"
    assert run(repo.raw_api_key(PROVIDER, "", "测试")) == "sk-new"


def test_rotating_the_default_key_keeps_it_the_default(repo):
    """换密钥（轮换）与换默认（改主意）是两件事，别把它们绑在一起。"""
    run(repo.save_credential(PROVIDER, "sk-old"))
    run(repo.save_credential(PROVIDER, "sk-rotated"))
    assert run(_default_label(repo)) == "默认"
    assert run(repo.raw_api_key(PROVIDER)) == "sk-rotated"


def test_overwriting_a_spare_key_leaves_it_spare(repo):
    run(repo.save_credential(PROVIDER, "sk-main"))
    run(repo.save_credential(PROVIDER, "sk-old", label="测试"))
    run(repo.save_credential(PROVIDER, "sk-new", label="测试"))
    assert run(_default_label(repo)) == "默认", "改备用钥匙不该顺手把它推成默认"


def test_same_name_on_another_provider_is_a_separate_key(repo):
    run(repo.save_credential(PROVIDER, "sk-a", label="测试"))
    run(repo.save_credential(OTHER, "sk-b", label="测试"))
    assert run(repo.raw_api_key(PROVIDER, "", "测试")) == "sk-a"
    assert run(repo.raw_api_key(OTHER, "", "测试")) == "sk-b"


def test_blank_name_falls_back_to_the_default_label(repo):
    run(repo.save_credential(PROVIDER, "sk-a", label="   "))
    assert run(repo.raw_api_key(PROVIDER)) == "sk-a"
    assert run(_default_label(repo)) == "默认"


# ── 删掉之后 ────────────────────────────────────────────────────


def test_deleting_the_default_promotes_the_oldest_remaining(repo):
    run(repo.save_credential(PROVIDER, "sk-first"))
    run(repo.save_credential(PROVIDER, "sk-second", label="测试"))
    first = run(repo.credential(PROVIDER, "", "默认"))
    run(repo.delete_credential(first["id"]))

    keys = run(_keys(repo))
    assert [k["label"] for k in keys] == ["测试"]
    assert keys[0]["is_default"] is True
    assert run(repo.raw_api_key(PROVIDER)) == "sk-second"


def test_deleting_the_last_key_leaves_the_provider_unconfigured(repo):
    run(repo.save_credential(PROVIDER, "sk-only"))
    only = run(repo.credential(PROVIDER))
    run(repo.delete_credential(only["id"]))
    assert run(_keys(repo)) == []
    assert run(repo.raw_api_key(PROVIDER)) == ""


def test_deleting_an_unknown_key_is_a_no_op(repo):
    assert run(repo.delete_credential("没有这个 id")) is None


# ── 结果为谁而记 ────────────────────────────────────────────────


def test_test_result_lands_on_the_key_that_was_tested(repo):
    """按 id 记，不按供应商记：不通的永远是具体那一把。"""
    run(repo.save_credential(PROVIDER, "sk-first"))
    run(repo.save_credential(PROVIDER, "sk-second", label="测试"))
    second = run(repo.credential(PROVIDER, "", "测试"))
    run(repo.record_credential_test(second["id"], False, "401 无效的 API Key"))

    keys = {k["label"]: k for k in run(_keys(repo))}
    assert keys["测试"]["last_test_ok"] is False
    assert "401" in keys["测试"]["last_test_note"]
    assert keys["默认"]["last_test_ok"] is None, "没测过的那把不该跟着背锅"


def test_default_lookup_is_not_just_the_last_row(repo):
    """多把钥匙时按 provider 建字典只会留下最后一行 —— 那会绕过默认。

    ``default_credentials()`` 是给"这家配了吗"用的，它必须给出默认那把。
    """
    run(repo.save_credential(PROVIDER, "sk-first"))
    run(repo.save_credential(PROVIDER, "sk-second", label="测试"))  # 最后插入的是备用
    default = run(repo.default_credentials())[PROVIDER]
    assert default["label"] == "默认"
    assert default["api_key_masked"]


def test_masked_value_never_leaks_the_secret(repo):
    run(repo.save_credential(PROVIDER, "sk-abcdefghijklmnop"))
    row = run(repo.credential(PROVIDER))
    assert "efghijklmnop" not in row["api_key_masked"]
    assert row["has_key"] is True


def test_key_is_stored_encrypted(repo):
    run(repo.save_credential(PROVIDER, "sk-plaintext-canary"))
    assert run(repo.raw_api_key(PROVIDER)) == "sk-plaintext-canary", "取出来要还原"
    raw = run(repo.db.query("SELECT api_key FROM dp_credential WHERE provider = ?", (PROVIDER,)))
    assert "sk-plaintext-canary" not in raw[0]["api_key"], "库里不能留明文"


# ── 老库升级 ────────────────────────────────────────────────────


def test_old_one_key_per_provider_schema_upgrades_without_losing_the_key(tmp_path: Path):
    """升级不该让任何一把已经能用的钥匙失效。"""
    path = tmp_path / "legacy.sqlite3"
    from divinepen_server.db import SqliteStore
    from divinepen_server.store import Repo, SecretBox

    box = SecretBox("test-key-for-credentials")
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.execute(
        "INSERT INTO dp_credential (id, provider, workspace, api_key, encrypted, created_at, updated_at)"
        " VALUES ('aliyun-bailian:-', ?, '', ?, 1, '2026-01-01', '2026-01-01')",
        (PROVIDER, box.seal("sk-legacy")),
    )
    conn.commit()
    conn.close()

    repo = Repo(SqliteStore(path), box)
    keys = run(repo.credentials())
    assert len(keys) == 1
    assert keys[0]["label"] == "默认"
    assert keys[0]["is_default"] is True, "老的那把直接接管默认"
    assert run(repo.raw_api_key(PROVIDER)) == "sk-legacy"

    # 老结构下这里会撞 UNIQUE(provider, workspace)，第二把根本插不进去。
    run(repo.save_credential(PROVIDER, "sk-second", label="测试"))
    assert sorted(k["label"] for k in run(repo.credentials())) == sorted(["默认", "测试"])


def test_migration_runs_once_and_can_reopen(tmp_path: Path):
    path = tmp_path / "twice.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(OLD_SCHEMA)
    conn.commit()
    conn.close()

    from divinepen_server.db import SqliteStore
    from divinepen_server.store import Repo, SecretBox

    for _ in range(2):  # 开两次库：迁移必须幂等
        repo = Repo(SqliteStore(path), SecretBox("test-key-for-credentials"))
        run(repo.save_credential(PROVIDER, "sk-a", label="测试"))
    assert len(run(repo.credentials())) == 1


# ── 页面与路由 ──────────────────────────────────────────────────


def _add(client, provider: str, label: str, key: str, make_default: bool = False):
    data = {"label": label, "api_key": key}
    if make_default:
        data["make_default"] = "1"
    return client.post(f"/credentials/{provider}", data=data, follow_redirects=False)


def _repo():
    """同一个测试库、同一把密钥的另一个句柄 —— 用来查页面背后真正落库的状态。

    ``state`` 是 ``create_app()`` 的闭包变量，从外面够不着，所以这里按
    **同一份 settings**（app 用的就是它）再开一个连接：查的是同一份真相，
    不是另起炉灶的副本。
    """
    from divinepen_server.db import SqliteStore
    from divinepen_server.settings import get_settings
    from divinepen_server.store import Repo, SecretBox

    settings = get_settings()
    return Repo(
        SqliteStore(settings.db_dsn.removeprefix("sqlite://")),
        SecretBox(settings.credential_key),
    )


def _rows(provider=PROVIDER) -> list[dict]:
    return asyncio.run(_repo().credentials(provider))


def _cid(label: str, provider: str = PROVIDER) -> str:
    return next(r["id"] for r in _rows(provider) if r["label"] == label)


def test_page_lists_every_key_of_a_provider(client):
    _add(client, PROVIDER, "页面用", "sk-page-b")
    html = client.get("/credentials").text
    assert "keyline" in html
    assert "页面用" in html


def test_page_offers_a_name_field_and_a_default_toggle(client):
    html = client.get("/credentials").text
    assert 'name="label"' in html
    assert 'name="make_default"' in html


def test_page_marks_the_default_key_with_a_hook(client):
    """整行一个 ``data-default`` 钩子：样式靠它点亮，读屏也靠它念出"这是默认"。"""
    _add(client, PROVIDER, "钩子用", "sk-page-c")
    assert _rows()
    assert 'data-default="1"' in client.get("/credentials").text


def test_chinese_name_survives_the_form(client):
    """页面是 UTF-8，浏览器提交的是 percent-encoded 的 UTF-8 —— 别把它读成 latin-1。"""
    _add(client, PROVIDER, "灰度号", "sk-cjk")
    html = client.get("/credentials").text
    assert "灰度号" in html
    assert "æµ" not in html, "落库的名字不该是乱码"


def test_post_without_a_key_is_rejected(client):
    resp = client.post(f"/credentials/{PROVIDER}", data={"api_key": "  "}, follow_redirects=False)
    assert resp.status_code == 303
    assert "API" in resp.headers["location"]


def test_named_post_keeps_the_previous_default(client):
    _add(client, PROVIDER, "默认", "sk-real-default", make_default=True)
    _add(client, PROVIDER, "再加一把", "sk-page-d")
    assert asyncio.run(_repo().credential(PROVIDER))["label"] == "默认"


def test_default_route_switches_which_key_is_used(client):
    _add(client, PROVIDER, "路由切默认", "sk-r1")
    resp = client.post(
        f"/credentials/{PROVIDER}/default",
        data={"cred_id": _cid("路由切默认")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert asyncio.run(_repo().credential(PROVIDER))["label"] == "路由切默认"
    assert asyncio.run(_repo().raw_api_key(PROVIDER)) == "sk-r1"
    _add(client, PROVIDER, "默认", "sk-real-default", make_default=True)


def test_default_route_refuses_a_key_belonging_to_another_provider(client):
    _add(client, OTHER, "别家的钥匙", "sk-r2")
    resp = client.post(
        f"/credentials/{PROVIDER}/default",
        data={"cred_id": _cid("别家的钥匙", OTHER)},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert asyncio.run(_repo().credential(OTHER))["label"] == "别家的钥匙", "别家的默认不该动"


def test_delete_route_removes_only_that_key(client):
    _add(client, PROVIDER, "路由删", "sk-r3")
    resp = client.post(
        f"/credentials/{PROVIDER}/delete",
        data={"cred_id": _cid("路由删")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    labels = [r["label"] for r in _rows()]
    assert "路由删" not in labels
    assert "默认" in labels, "删备用那把不该波及默认"


def test_page_still_renders_for_a_provider_without_keys(client):
    assert client.get("/credentials").status_code == 200
    assert client.get("/credentials", params={"scope": "all"}).status_code == 200
