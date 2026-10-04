"""存储会话新鲜度：读到的必须是"现在"，不是连接挂上那一刻的快照。

守的是一个真实事故（2026-10-04）。MySQL 连接池建的时候 ``autocommit=False``，
而读路径（``MySQLStore._fetch``）查询完**既不 commit 也不 rollback** ——
连接就一直挂着一个没结束的事务；MySQL 默认 REPEATABLE READ 下，
它此后永远读**那条事务开始时的快照**，别的会话怎么写都看不见。

后果比"页面偶尔旧"严重得多：

* 改完目录，重启前后表现不一致，最难查（现象诡异到会让人怀疑记忆）；
* 更致命的是**异步任务的进度写进了库，页面却刷不出来** ——
  页面看到的任务状态会一直停在提交那一刻，看起来像"卡住了没跑"。

异步任务一多，这个 bug 就越显形。所以它和"统一走异步"是同一条线上的事。

SQLite 走单文件单连接，天生不满足这个失败条件，所以在非 MySQL 上跳过。
要真验，显式给 ``DIVINEPEN_TEST_DB_URL=mysql://…``（**别用生产库**）。
"""

from __future__ import annotations

import pytest

from divinepen_server.db import connect_store
from divinepen_server.settings import get_settings

_PROBE = "session-freshness-probe"


async def test_reader_sees_another_sessions_commit() -> None:
    dsn = get_settings().db_dsn
    if not dsn.startswith("mysql"):
        pytest.skip(
            "这条不变量只在 MySQL 上会被违反（REPEATABLE READ + 未结束的事务）；"
            "SQLite 单连接天然满足，跳过"
        )

    reader = await connect_store(dsn)
    writer = await connect_store(dsn)
    try:
        await writer.execute(f"DELETE FROM dp_setting WHERE k = '{_PROBE}'")

        # 先让 reader 读一次：旧行为下，正是这一步把它的会话钉在了一个事务上。
        before = await reader.query_one(f"SELECT v FROM dp_setting WHERE k = '{_PROBE}'")
        assert before is None

        # 另一个会话写入并提交。
        await writer.execute(f"INSERT INTO dp_setting (k, v) VALUES ('{_PROBE}', 'v1')")

        # reader 必须看得见 —— 这正是旧代码会失败的地方。
        after = await reader.query_one(f"SELECT v FROM dp_setting WHERE k = '{_PROBE}'")
        assert after is not None and after["v"] == "v1", (
            "reader 会话读到的还是旧快照：连接池的 autocommit 又变回 False 了？"
        )

        # 再改一次，仍要看得见（不只是"第一次读之后恰好刷新"）。
        await writer.execute(f"UPDATE dp_setting SET v = 'v2' WHERE k = '{_PROBE}'")
        again = await reader.query_one(f"SELECT v FROM dp_setting WHERE k = '{_PROBE}'")
        assert again is not None and again["v"] == "v2"
    finally:
        await writer.execute(f"DELETE FROM dp_setting WHERE k = '{_PROBE}'")
        await reader.aclose()
        await writer.aclose()
