"""任务中心的操作面：取消 / 删除 / 重跑 / 复制改参。

任务中心原来是只读账本，加上这些动作之后最危险的都不是"能不能点"，
而是**边界**：跑着的不许删、清账不许碰在跑的、复制得把参数原样带回表单。
所以这里全是边界用例。

一条纪律：这些测试**不碰任何付费接口**。需要一条任务时，就提交一个源文件
不存在的本地裁剪（``media.trim``）——它立刻失败、不花一分钱，但会在台账里
留下一行真记录。需要"跑着的"任务时，直接往库里写一行 RUNNING，不去真跑。
"""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from divinepen_server.settings import get_settings


@pytest.fixture(scope="module")
def client():
    from divinepen_server.web import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _flash(response) -> str:
    """重定向里的中文是百分号编码的，不解码永远断言不到。"""
    return unquote(response.headers.get("location", ""))


def _store_repo():
    """直连同一个库（conftest 已把 DSN 钉到临时 SQLite）。

    用它来造"跑着的任务"这种**没法从 UI 造出来**的状态。
    """
    from divinepen_server.db import SqliteStore
    from divinepen_server.store import Repo, SecretBox

    url = get_settings().db_dsn
    path = url.removeprefix("sqlite://")
    return Repo(SqliteStore(path), SecretBox("test-key-for-task-ops"))


def _create_failed_task(client) -> str:
    """提交一次注定失败的本地任务，返回它的 task_id。"""
    resp = client.post(
        "/capabilities/media.trim/run",
        data={"source": "var/definitely-not-here.mp4", "start": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    match = re.search(r"/tasks/(t_[0-9a-f]+)", resp.headers["location"])
    assert match, resp.headers["location"]
    task_id = match.group(1)
    _wait_terminal(client, task_id)
    return task_id


def _wait_terminal(client, task_id: str, timeout: float = 10.0) -> str:
    """等后台那一跑落地。测试线程看不到 app 的事件循环，只能轮询页面。"""
    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        page = client.get(f"/tasks/{task_id}")
        match = re.search(r"data-status=\"([A-Z_]+)\"", page.text)
        status = match.group(1) if match else ""
        if status in {"SUCCEEDED", "FAILED", "CANCELED"}:
            return status
        time.sleep(0.05)
    raise AssertionError(f"任务 {task_id} 没有在 {timeout}s 内到终态（当前 {status}）")


def _insert_task(*, status: str, capability_id: str = "video.t2v", **extra) -> str:
    row = {"capability_id": capability_id, "status": status, **extra}
    return asyncio.run(_store_repo().create_task(row))


# ── 查：列表能筛，filmstrip 不再空转 ──────────────────────────────


def test_terminal_task_has_no_filmstrip(client):
    """已结束的任务不再画进度条。

    进度条对终态是纯噪声：SUCCEEDED 恒满格、同时旁边还写着 SUCCEEDED 和 100%，
    一个格子说三遍。跑着的才需要它。
    """
    task_id = _create_failed_task(client)
    html = client.get("/tasks?status=FAILED").text
    assert task_id in html
    block = html.split(f'href="/tasks/{task_id}"')[1].split("</tr>")[0]
    assert "filmstrip" not in block


def test_status_filter_hides_other_statuses(client):
    task_id = _create_failed_task(client)
    assert task_id not in client.get("/tasks?status=SUCCEEDED").text


# ── 删 ────────────────────────────────────────────────────────


def test_finished_task_can_be_deleted(client):
    task_id = _create_failed_task(client)
    resp = client.post(f"/tasks/{task_id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert "已删除任务" in _flash(resp)
    assert client.get(f"/tasks/{task_id}").status_code == 404
    assert task_id not in client.get("/tasks").text


def test_running_task_cannot_be_deleted(client):
    """跑着的任务是**正在花的钱**，删掉它就查无此人了 —— 必须拦住。"""
    task_id = _insert_task(status="RUNNING", provider="pytest-ghost", model="")
    resp = client.post(f"/tasks/{task_id}/delete", follow_redirects=False)
    assert "先取消再删" in _flash(resp)
    assert client.get(f"/tasks/{task_id}").status_code == 200
    client.post(f"/tasks/{task_id}/cancel", follow_redirects=False)
    client.post(f"/tasks/{task_id}/delete", follow_redirects=False)


def test_purge_clears_terminal_but_keeps_running(client):
    failed = _create_failed_task(client)
    running = _insert_task(status="RUNNING", provider="pytest-ghost", model="")
    resp = client.post("/tasks/purge", data={"scope": "terminal"}, follow_redirects=False)
    assert resp.status_code == 303
    assert client.get(f"/tasks/{failed}").status_code == 404
    assert client.get(f"/tasks/{running}").status_code == 200, "在跑的任务不该被清掉"
    client.post(f"/tasks/{running}/cancel", follow_redirects=False)


def test_purge_failed_scope_leaves_successes_alone(client):
    failed = _create_failed_task(client)
    done = _insert_task(status="SUCCEEDED", provider="pytest-ghost", model="")
    client.post("/tasks/purge", data={"scope": "failed"}, follow_redirects=False)
    assert client.get(f"/tasks/{failed}").status_code == 404
    assert client.get(f"/tasks/{done}").status_code == 200
    client.post(f"/tasks/{done}/delete", follow_redirects=False)


# ── 取消 ──────────────────────────────────────────────────────


def test_pending_task_can_be_canceled(client):
    task_id = _insert_task(status="PENDING", provider="pytest-ghost", model="")
    resp = client.post(f"/tasks/{task_id}/cancel", follow_redirects=False)
    assert resp.status_code == 303
    assert "已取消" in _flash(resp)
    assert 'data-status="CANCELED"' in client.get(f"/tasks/{task_id}").text
    client.post(f"/tasks/{task_id}/delete", follow_redirects=False)


def test_canceling_a_finished_task_is_a_noop(client):
    task_id = _create_failed_task(client)
    resp = client.post(f"/tasks/{task_id}/cancel", follow_redirects=False)
    assert "已经结束了" in _flash(resp)
    assert 'data-status="FAILED"' in client.get(f"/tasks/{task_id}").text
    client.post(f"/tasks/{task_id}/delete", follow_redirects=False)


def test_cancel_survives_a_provider_that_cannot_be_canceled(client):
    """远端 id 存在但供应商查不到（或没配 Key）时，本地取消必须照样成立。

    不能因为"远端取消没成功"就把整条取消操作回滚 —— 用户点的是"停，
    别再花我钱了"，本地停住是底线；远端结果如实回报即可。
    """
    task_id = _insert_task(
        status="RUNNING", provider="pytest-ghost", model="ghost-model",
        remote_task_id="remote-abc",
    )
    resp = client.post(f"/tasks/{task_id}/cancel", follow_redirects=False)
    flash = _flash(resp)
    assert "已取消" in flash
    assert 'data-status="CANCELED"' in client.get(f"/tasks/{task_id}").text
    client.post(f"/tasks/{task_id}/delete", follow_redirects=False)


# ── 重跑 ──────────────────────────────────────────────────────


def test_retry_creates_a_new_task_with_the_same_payload(client):
    old = _create_failed_task(client)
    old_payload = client.get(f"/tasks/{old}").text

    resp = client.post(f"/tasks/{old}/retry", follow_redirects=False)
    assert resp.status_code == 303
    match = re.search(r"/tasks/(t_[0-9a-f]+)", resp.headers["location"])
    assert match
    new = match.group(1)
    assert new != old
    assert "重跑" in _flash(resp)

    old_page = client.get(f"/tasks/{old}").text
    assert old in old_page, "重跑后原任务必须还在（失败原因、耗时是证据）"
    assert old_payload  # 原页面本来就读得到

    client.post(f"/tasks/{old}/delete", follow_redirects=False)
    client.post(f"/tasks/{new}/delete", follow_redirects=False)


def test_retry_refuses_while_the_task_is_still_running(client):
    task_id = _insert_task(status="RUNNING", provider="pytest-ghost", model="")
    resp = client.post(f"/tasks/{task_id}/retry", follow_redirects=False)
    assert "还在跑" in _flash(resp)
    client.post(f"/tasks/{task_id}/cancel", follow_redirects=False)
    client.post(f"/tasks/{task_id}/delete", follow_redirects=False)


# ── 复制改参 ──────────────────────────────────────────────────


def test_clone_carries_the_payload_back_into_the_capability_form(client):
    """这是"复制完小改一个参数再快速生成"的地基：参数必须真的回到表单里。"""
    task_id = _insert_task(
        status="SUCCEEDED",
        capability_id="video.t2v",
        provider="aliyun-bailian",
        model="wan2.7-t2v",
        payload={
            "provider": "aliyun-bailian",
            "model": "wan2.7-t2v",
            "policy": "explicit",
            "params": {"input.prompt": "一只猫在沙滩上奔跑", "parameters.duration": 5},
        },
    )
    resp = client.get(f"/tasks/{task_id}/clone", follow_redirects=False)
    assert resp.status_code == 303
    location = unquote(resp.headers["location"])
    assert location.startswith("/capabilities/video.t2v?")
    assert "provider=aliyun-bailian" in location
    assert "model=wan2.7-t2v" in location
    assert "input.prompt=一只猫在沙滩上奔跑" in location

    # 跟过去看：表单真的回填了，而且模型没跑偏
    page = client.get(location)
    assert page.status_code == 200
    assert "一只猫在沙滩上奔跑" in page.text
    assert "wan2.7-t2v" in page.text

    client.post(f"/tasks/{task_id}/delete", follow_redirects=False)


def test_clone_of_a_local_editing_task_keeps_its_flat_fields(client):
    task_id = _create_failed_task(client)
    resp = client.get(f"/tasks/{task_id}/clone", follow_redirects=False)
    location = unquote(resp.headers["location"])
    assert location.startswith("/capabilities/media.trim?")
    assert "source=var/definitely-not-here.mp4" in location
    assert client.get(location).status_code == 200
    client.post(f"/tasks/{task_id}/delete", follow_redirects=False)


def test_clone_of_a_missing_task_lands_back_on_the_list(client):
    resp = client.get("/tasks/t_ffffffffffffffff/clone", follow_redirects=False)
    assert resp.status_code == 303
    assert "任务不存在" in _flash(resp)


# ── 存储层：清理只认状态 ───────────────────────────────────────


def test_purge_tasks_only_touches_the_given_statuses(tmp_path):
    from divinepen_server.db import SqliteStore
    from divinepen_server.store import Repo, SecretBox

    repo = Repo(SqliteStore(tmp_path / "purge.sqlite3"), SecretBox("k"))

    async def scenario():
        await repo.create_task({"capability_id": "video.t2v", "status": "SUCCEEDED"})
        await repo.create_task({"capability_id": "video.t2v", "status": "FAILED"})
        await repo.create_task({"capability_id": "video.t2v", "status": "RUNNING"})
        removed = await repo.purge_tasks(["SUCCEEDED", "FAILED", "CANCELED"])
        assert removed == 2
        remaining = await repo.tasks()
        assert [r["status"] for r in remaining] == ["RUNNING"]

    asyncio.run(scenario())


def test_delete_task_reports_whether_it_existed(tmp_path):
    from divinepen_server.db import SqliteStore
    from divinepen_server.store import Repo, SecretBox

    repo = Repo(SqliteStore(tmp_path / "del.sqlite3"), SecretBox("k"))

    async def scenario():
        task_id = await repo.create_task({"capability_id": "video.t2v", "status": "FAILED"})
        assert await repo.delete_task(task_id) is True
        assert await repo.delete_task(task_id) is False

    asyncio.run(scenario())
