"""任务"进度"：一个不许编数字的地方。

背景：``_poll_until_done`` 曾经写 ``progress=min(90, polls * 3)`` —— 每轮询
3 次涨 9%，30 个周期（约 3 分钟）爬到 90% 然后停住。那个数字和远端做到哪了
**毫无关系**：远端 5% 和 95% 在本地显示一模一样。用户盯着一个停住的"90%"
等 20 分钟，只会得出"卡住了"的结论 —— 用一个假数字制造出真焦虑。

百炼的轮询接口也只回 ``PENDING / RUNNING / SUCCEEDED``，**根本不返回百分比**。
所以详情页能给的只有两条真事实（跑了多久 / 回过几次话），加一条"在这个意义上
它会动"的条纹。这个文件守的就是这条线，外加自刷新那块的契约：

  页面上看到的和片段里回来的，必须是**同一个宏**画出来的 ——
  两处各写一份渲染，"刷新一下变了样"就成了新的假信息源。
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from divinepen_server import progress
from divinepen_server.settings import get_settings


def _ago(seconds: int) -> str:
    return (datetime.now(UTC).astimezone() - timedelta(seconds=seconds)).isoformat(
        timespec="seconds"
    )


def _task(**over: Any) -> dict[str, Any]:
    """一行 dp_task 的字面量。describe() 是纯函数，所以不需要数据库。"""
    row = {
        "id": "t_" + "0" * 16,
        "capability_id": "video.t2v",
        "provider": "aliyun-bailian",
        "model": "wan2.7-t2v",
        "status": "RUNNING",
        "remote_task_id": "rt-123",
        "progress": 0,
        "polls": 0,
        "created_at": _ago(30),
        "updated_at": _ago(3),
        "finished_at": None,
        "error": None,
    }
    row.update(over)
    return row


# ── 一、不编数字 ────────────────────────────────────────────────


def test_running_async_task_reports_no_percentage():
    """在跑的异步任务：没有百分比就是没有，一格都不许编。

    这条是本文件的**主条款**。旧写法下 polls=30 会给 90 —— 一个看上去
    "快好了"、其实什么也不说明的数。所以这里特意挑一个旧公式会给出 90 的
    轮询次数，确认它现在仍然是"不知道"。
    """
    view = progress.describe(_task(status="RUNNING", polls=30))
    assert view.percent is None
    assert view.bar == "striped", "拿不到百分比就别画确定的格子，画会走的条纹"
    assert view.watching is True


def test_percentage_never_grows_with_poll_count():
    """轮询次数再多，也不能长出百分比来 —— 它只是"回过几次话"。"""
    first = progress.describe(_task(polls=1))
    later = progress.describe(_task(polls=300))
    assert first.percent is None and later.percent is None


def test_succeeded_gets_a_hundred_because_that_one_is_earned():
    view = progress.describe(_task(status="SUCCEEDED"))
    assert view.percent == 100
    assert view.bar == "filmstrip"
    assert view.watching is False


# ── 二、轮询循环自己不写假进度（行为级，不是源码级）────────────────


class _Resp:
    ok = True
    status_code = 200
    request_id = "req-test"

    def __init__(self, body: dict[str, Any]):
        self.body = body


class _FakeHttp:
    """按剧本回远端状态。"""

    def __init__(self, statuses: list[str]):
        self.statuses = statuses
        self.calls = 0

    async def request(self, _request):
        index = min(self.calls, len(self.statuses) - 1)
        self.calls += 1
        return _Resp({"output": {"task_status": self.statuses[index]}})


class _FakeTasks:
    def __init__(self):
        self.updates: list[dict[str, Any]] = []

    async def update(self, _task_id, **fields):
        self.updates.append(fields)
        return None


class _Ctx:
    def __init__(self, statuses: list[str]):
        self.http = _FakeHttp(statuses)
        self.tasks = _FakeTasks()


def _poll_probe():
    """一个只为测轮询循环存在的能力实例。

    ``_poll_until_done`` 不读任何实例状态，所以这里只需要一个能实例化的壳。
    """
    from divinepen.capabilities._model import ModelBackedCapability
    from divinepen.core.manifest import CapabilityManifest, Category

    class _Probe(ModelBackedCapability):
        manifest = CapabilityManifest(id="video.poll", name="轮询探针", category=Category.VIDEO)

    return _Probe()


def _async_model(interval: float = 0.001):
    from divinepen.core.specs import ApiSpec, ModelSpec, PollSpec

    return ModelSpec(
        provider="aliyun-bailian",
        key="wan2.7-t2v",
        capability="video.t2v",
        base_url="https://example.invalid",
        api=ApiSpec(
            path="/api/v1/services/aigc/video-generation/video-synthesis",
            mode="async_task",
            poll=PollSpec(path="/api/v1/tasks/{task_id}", interval_seconds=interval),
        ),
    )


def test_polling_writes_no_progress_field():
    """轮询只许写"回过几次话"和"远端说什么"，不许写百分比。

    这是那条被删掉的 ``progress=min(90, polls * 3)`` 的守门人：
    只要有人在轮询里重新塞一个 progress 进去，这里立刻红。
    """
    ctx = _Ctx(["RUNNING", "RUNNING", "SUCCEEDED"])
    payload, polls = asyncio.run(
        _poll_probe()._poll_until_done(_async_model(), "rt-1", "sk-test", ctx, "t_local")
    )
    assert polls == 3
    assert payload["output"]["task_status"] == "SUCCEEDED"
    assert ctx.tasks.updates, "轮询至少该往台账写点什么，否则页面上一点心跳都没有"
    for fields in ctx.tasks.updates:
        assert "progress" not in fields, f"轮询里又出现了百分比：{fields}"
        assert fields.get("status") in {"RUNNING", "SUCCEEDED"}
    assert [f.get("polls") for f in ctx.tasks.updates] == [1, 2, 3], "次数要一次不落地记下来"


# ── 三、心跳：拿得出证据的三行 ───────────────────────────────────


def test_heartbeat_shows_polls_and_last_echo():
    view = progress.describe(_task(status="RUNNING", polls=14, updated_at=_ago(3)))
    facts = dict(view.facts)
    assert facts["已轮询"] == "14 次"
    assert facts["最后回音"] == "3s 前", "每次轮询都会写 updated_at，所以这是真事实"
    assert "RUNNING · 正在生成" in facts["远端状态"]
    assert view.headline == "正在生成"


def test_queued_task_says_it_is_queuing_not_stuck():
    """排队就是排队 —— 0% 不等于没在跑，这一条要说在明处。"""
    view = progress.describe(_task(status="PENDING", polls=4, updated_at=_ago(2)))
    assert view.headline == "远端排队中"
    assert "排队" in view.note and "没在跑" in view.note
    assert view.stuck is False


def test_sync_task_admits_it_has_no_heartbeat():
    """同步调用没有心跳可看，就直说 —— 不要装出有进度的样子。"""
    view = progress.describe(_task(remote_task_id=None, status="RUNNING", created_at=_ago(20)))
    facts = dict(view.facts)
    assert "已轮询" not in facts
    assert view.is_async is False
    assert view.headline == "同步执行中"
    assert "同步调用" in view.note
    assert view.percent is None


# ── 四、卡住：宁可不说，不可说错 ─────────────────────────────────


def test_stale_heartbeat_is_flagged_as_stuck():
    view = progress.describe(_task(status="RUNNING", polls=9, updated_at=_ago(120)))
    assert view.stuck is True
    assert "没有收到轮询回音" in view.stuck_reason
    assert "供应商控制台" in view.stuck_reason, "要先劝人去看远端，别直接下结论"


def test_fresh_heartbeat_is_not_flagged():
    """刚回过话就说人家卡住了，比漏报更糟 —— 冤枉一个在跑的任务。"""
    view = progress.describe(_task(status="RUNNING", polls=2, updated_at=_ago(15)))
    assert view.stuck is False
    assert view.stuck_reason == ""


def test_sync_task_is_not_called_stuck_for_merely_being_slow():
    """一次两分钟的 ffmpeg 拼接很正常，不能跟"卡住"混为一谈。"""
    view = progress.describe(_task(remote_task_id=None, created_at=_ago(120)))
    assert view.stuck is False


def test_sync_task_stuck_only_after_half_an_hour():
    view = progress.describe(_task(remote_task_id=None, created_at=_ago(2000)))
    assert view.stuck is True
    assert "同步调用" in view.stuck_reason


# ── 五、什么时候不用再刷新 ───────────────────────────────────────


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "CANCELED", "AWAITING_INPUT"])
def test_finished_and_paused_states_stop_watching(status):
    """终态等的是时间（已经没了），AWAITING_INPUT 等的是人 —— 都不该再刷。

    AWAITING_INPUT 尤其重要：页面 refresh 一万次，闸口也不会自己松开，
    刷下去只会让用户以为"它在跑"。
    """
    assert progress.describe(_task(status=status)).watching is False


def test_paused_pipeline_keeps_its_real_percentage():
    """pipeline 的 progress 是"第几步 / 共几步"，是真数，要留着。"""
    view = progress.describe(_task(status="AWAITING_INPUT", progress=60))
    assert view.percent == 60
    assert view.bar == "filmstrip"
    assert "等" in view.headline


def test_failed_task_draws_no_track():
    view = progress.describe(_task(status="FAILED"))
    assert view.bar == "none"
    assert view.percent is None
    assert "error" in view.note, "失败了要把人指到错误原因那一段"


# ── 六、时长写法 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (59, "59s"), (60, "1m00s"), (150, "2m30s"), (3600, "1h00m"), (5400, "1h30m")],
)
def test_human_duration(seconds, expected):
    assert progress.human_duration(seconds) == expected


def test_missing_timestamp_does_not_explode():
    assert progress.human_duration(None) == "—"
    view = progress.describe({"status": "RUNNING"})
    assert view.elapsed_human == "—"
    assert view.stuck is False, "时间都读不出来的时候，没资格说人家卡住了"


# ── 七、路由：详情页与它的片段 ───────────────────────────────────


@pytest.fixture(scope="module")
def client():
    from divinepen_server.web import create_app

    with TestClient(create_app(), raise_server_exceptions=False) as c:
        yield c


def _insert_task(**over) -> str:
    from divinepen_server.db import SqliteStore
    from divinepen_server.store import Repo, SecretBox

    repo = Repo(SqliteStore(get_settings().db_dsn.removeprefix("sqlite://")), SecretBox("k"))
    return asyncio.run(repo.create_task(over))


def _new_running_task() -> str:
    """造一条"正在跑、心跳新鲜"的异步任务。

    时间戳取当下，所以「已运行 0s」在两个请求里都是 0s —— 下面那条
    "页面和片段一字不差"的断言不会被跨秒的抖动打断。
    """
    return _insert_task(
        capability_id="video.t2v",
        provider="aliyun-bailian",
        model="wan2.7-t2v",
        status="RUNNING",
        remote_task_id="rt-live-1",
        payload={"model": "wan2.7-t2v"},
        polls=7,
    )


def _squash(html: str) -> str:
    """折叠空白 + 抹掉时长数字。除了"过去了 1 秒"，两边不该有任何差别。"""
    return re.sub(r"\d+m\d+s|\d+s", "T", " ".join(html.split()))


def test_detail_page_watches_a_running_task(client):
    task_id = _new_running_task()
    html = client.get(f"/tasks/{task_id}").text
    assert 'data-task-watch="1"' in html, "在跑的任务，详情页必须自己盯着"
    assert "自动刷新中" in html
    assert "已轮询" in html and "最后回音" in html


def test_fragment_route_returns_the_same_card_as_the_page(client):
    """片段必须和页面**同一份渲染**。

    这是本文件里最较真的一条：两处各画一份，"刷新一下变了样"就成了新的
    假信息源 —— 而这次骗人的是页面自己。折叠空白后做包含判定，
    等于在说"这一块在两边是同一段 HTML"。
    """
    task_id = _new_running_task()
    page = client.get(f"/tasks/{task_id}").text
    frag = client.get(f"/tasks/{task_id}/progress").text
    assert frag.strip(), "片段不能是空的"
    assert 'class="pcard"' in frag
    assert 'data-task-watch="1"' in frag
    assert _squash(frag) in _squash(page), "片段和页面上的那一块必须一模一样"


def test_fragment_is_not_a_whole_page(client):
    """片段只回一块。整页（侧栏、页脚、导航）走别处 —— 否则每次刷新都在重画整站。"""
    frag = client.get(f"/tasks/{_new_running_task()}/progress").text
    assert 'class="side"' not in frag
    assert "<!doctype" not in frag.lower()


def test_fragment_stops_watching_when_task_is_done(client):
    task_id = _insert_task(
        capability_id="video.t2v", provider="aliyun-bailian", model="wan2.7-t2v", status="SUCCEEDED"
    )
    frag = client.get(f"/tasks/{task_id}/progress").text
    assert "data-task-watch" not in frag, "跑完了还让 JS 继续问，就是白刷"
    assert "100%" in frag


def test_fragment_404s_on_unknown_task(client):
    assert client.get("/tasks/t_deadbeefdeadbeef/progress").status_code == 404


def test_task_list_shows_no_number_for_a_running_task(client):
    """列表也一样：给不出的数就写"—"，不写 0%。"""
    task_id = _new_running_task()
    html = client.get("/tasks").text
    block = html.split(f'href="/tasks/{task_id}"')[1].split("</tr>")[0]
    assert "pbar" in block, "在跑的行要给「活着」的证据"
    assert "已运行" in block
    assert '<td class="num">0%</td>' not in block
    assert "—" in block
