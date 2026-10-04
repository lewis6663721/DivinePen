"""任务进度：把一行 ``dp_task`` 翻译成"人怎么判断它还活着"。

一条规矩：**只报拿得出证据的东西。**

百炼的异步任务只回 ``PENDING / RUNNING / SUCCEEDED``，**从来不报百分比**。
所以这里不构造百分比。拿不到就明说拿不到，改用三条真事实回答那个真正的
问题 ——「它是不是还在跑？」：

    · 已运行多久（created_at 差）
    · 回过几次话（polls）
    · 最后一次回音在几秒前（updated_at 差 —— 每次轮询都写这一行）

这三条都是可核对的。相比一个"看着在动"的假百分比，它们才担得起"进度"两个字。

由来：``_poll_until_done`` 曾经写 ``progress=min(90, polls * 3)`` —— 每轮询
3 次涨 9%，30 个周期（约 3 分钟）爬到 90% 然后停住。**那个数字和远端做到哪了
毫无关系**：远端 5% 和 95% 在本地的显示完全一样。用户盯着"90%"等 20 分钟，
只会得出"卡住了"的结论 —— 用一个假数字制造出真焦虑。已删。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from divinepen.core.specs import TaskStatus

#: 会自动刷新、会去看心跳的状态。AWAITING_INPUT 不在里面 ——
#: 它等的不是时间，是人；页面 refresh 一万次也不会自己往前走。
WATCHING = {TaskStatus.PENDING, TaskStatus.RUNNING}

#: 异步任务：轮询间隔 6 秒，90 秒 ≈ 15 个周期没有回音，才配叫"可能卡住"。
ASYNC_STUCK_AFTER = 90
#: 同步任务：没有心跳可看，只能拿"超出常理"来推断。半小时。
SYNC_STUCK_AFTER = 1800

#: 远端状态 → 中文。这三个词是供应商的原话，别翻译成"进行中"那种谁也说不清的词。
REMOTE_LABEL = {
    TaskStatus.PENDING.value: "远端排队中",
    TaskStatus.RUNNING.value: "正在生成",
    TaskStatus.SUCCEEDED.value: "远端已完成",
    TaskStatus.FAILED.value: "远端失败",
    TaskStatus.CANCELED.value: "远端已取消",
}

REMOTE_NOTE = {
    TaskStatus.PENDING.value: "任务已交给供应商，还在排队等资源。排队期间进度就是 0，"
    "这不代表没在跑 —— 下面那行心跳才是它在动的证据。",
    TaskStatus.RUNNING.value: "供应商正在生成。它不报百分比，所以这一格填不出实数 —— "
    "宁可留空，不编一个。",
}

SYNC_NOTE = (
    "同步调用：一次请求发出去，等到结果才回来，中间没有任何可探测的中间态。"
    "所以这里只能报「跑了多久」，报不了「跑到哪了」。"
)


def _parse(iso: Any) -> datetime | None:
    if not isinstance(iso, str) or not iso:
        return None
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _seconds_since(iso: Any) -> int | None:
    moment = _parse(iso)
    if moment is None:
        return None
    return max(0, int((datetime.now(moment.tzinfo) - moment).total_seconds()))


def human_duration(seconds: int | None) -> str:
    """``90`` → ``1m30s``。粗到分钟就够了 —— 这里量的是"有没有在动"。"""
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


@dataclass(frozen=True)
class TaskProgress:
    """进度卡片的全部数据。模板只读这里，不做任何推断。"""

    status: str
    #: 还要不要继续刷新。片段每次返回都会带上它 —— 什么时候停由服务端说，
    #: JS 只管照做，不自己判断状态机。
    watching: bool
    #: none / filmstrip（有真百分比）/ striped（在跑但没有百分比）
    bar: str
    #: 只有拿得出证据才给数：供应商报的、pipeline 的步数比例、SUCCEEDED 的 100
    percent: int | None
    headline: str
    facts: list[tuple[str, str]] = field(default_factory=list)
    note: str = ""
    stuck: bool = False
    stuck_reason: str = ""
    remote_status: str | None = None
    #: 异步 or 同步 —— 这决定了"卡住"能不能被判出来
    is_async: bool = False
    #: 已运行多久。列表页那行和进度卡都要，放顶上省得两处各算一遍、再算出两个样。
    elapsed_seconds: int | None = None
    elapsed_human: str = "—"


def describe(task: dict[str, Any]) -> TaskProgress:
    """一行 ``dp_task`` → 一张进度卡片。纯函数，不碰数据库，可离线测。"""
    status = str(task.get("status") or TaskStatus.PENDING.value).upper()
    try:
        parsed_status = TaskStatus(status)
    except ValueError:
        parsed_status = TaskStatus.UNKNOWN

    # 异步 ⟺ 有远端 task_id。两者在同一处写下（_open_task），同步任务永远没有。
    # 用这个判据就不用为了一个 UI 判断去 join 模型目录。
    remote_task_id = task.get("remote_task_id")
    is_async = bool(remote_task_id)
    watching = parsed_status in WATCHING

    elapsed = _seconds_since(task.get("created_at"))
    since_update = _seconds_since(task.get("updated_at"))
    polls = int(task.get("polls") or 0)
    progress = int(task.get("progress") or 0)

    # 每一张卡片都带的公共项。放在一处算，省得六个分支各写一遍、
    # 写着写着就不一样了（"已运行"在两个页面显示成两个数的最常见原因）。
    base: dict[str, Any] = {
        "status": status,
        "is_async": is_async,
        "remote_status": status if is_async else None,
        "elapsed_seconds": elapsed,
        "elapsed_human": human_duration(elapsed),
    }

    if parsed_status is TaskStatus.SUCCEEDED:
        return TaskProgress(
            watching=False,
            bar="filmstrip",
            percent=100,
            headline="已完成",
            **base,
        )

    if parsed_status is TaskStatus.FAILED:
        return TaskProgress(
            watching=False,
            bar="none",
            percent=None,
            headline="失败",
            note="失败原因见下面「结果 / 暂停态」里的 error。重跑会重新计费。",
            **base,
        )

    if parsed_status is TaskStatus.CANCELED:
        return TaskProgress(
            watching=False,
            bar="none",
            percent=None,
            headline="已取消",
            **base,
        )

    if parsed_status is TaskStatus.AWAITING_INPUT:
        # 这里的 progress 是真数：pipeline 报"第几步 / 共几步"。
        return TaskProgress(
            watching=False,
            bar="filmstrip" if progress else "none",
            percent=progress or None,
            headline="停在人工闸口，等你确认",
            note="确认之后才继续往下跑，所以这一页不用刷新 —— 等的是你，不是时间。",
            **base,
        )

    # ── 在跑：PENDING / RUNNING / UNKNOWN ─────────────────────────
    facts: list[tuple[str, str]] = [("已运行", human_duration(elapsed))]
    note = SYNC_NOTE
    if is_async:
        facts.append(("已轮询", f"{polls} 次"))
        facts.append(
            ("最后回音", f"{human_duration(since_update)} 前" if since_update is not None else "—")
        )
        facts.append(("远端状态", f"{status} · {REMOTE_LABEL.get(status, status)}"))
        note = REMOTE_NOTE.get(status, f"远端状态 {status}。")

    stuck, reason = _stuck(
        watching=watching, is_async=is_async, since_update=since_update, elapsed=elapsed
    )
    return TaskProgress(
        watching=watching,
        bar="striped" if watching else "none",
        percent=None,  # ← 编不出来就不编。这里以前是 min(90, polls*3)。
        headline=REMOTE_LABEL.get(status, "执行中") if is_async else "同步执行中",
        facts=facts,
        note=note,
        stuck=stuck,
        stuck_reason=reason,
        **base,
    )


def _stuck(
    *,
    watching: bool,
    is_async: bool,
    since_update: int | None,
    elapsed: int | None,
) -> tuple[bool, str]:
    """卡住判定。**宁可不说，不可说错** —— 冤枉一个在跑的任务比漏报更糟。

    异步有心跳（每次轮询都会写 updated_at），所以能直接判：
    15 个轮询周期没回音，基本就是轮询它的那个进程不在了。
    同步没有心跳，只能拿常理推断，阈值因此放得很宽。
    """
    if not watching:
        return False, ""
    if is_async:
        if since_update is not None and since_update > ASYNC_STUCK_AFTER:
            return True, (
                f"已经 {human_duration(since_update)} 没有收到轮询回音（轮询间隔约 6 秒）。"
                "这一单多半是停了 —— 轮询它的那个进程可能已经不在了。"
                "远端任务可能还在跑，建议先去供应商控制台看一眼，再决定要不要取消。"
            )
        return False, ""
    if elapsed is not None and elapsed > SYNC_STUCK_AFTER:
        return True, (
            f"同步调用已经跑了 {human_duration(elapsed)}。正常一次几十秒就该回来，"
            "这么久多半是这次请求挂住了。"
        )
    return False, ""
