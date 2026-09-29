"""存储层：SQLite 适配器（默认），一个文件、零配置、开箱即跑。

**为什么不是先上 Tortoise + MySQL**：一期要验证的是"页面参数与接口文档一一对应"，
不是 ORM 选型。``ARCHITECTURE.md`` 里定的是 MySQL + Tortoise，这里先用同期最省的
实现把页面跑起来，所有 SQL 都关在 ``Store`` 类里 —— 换成 Tortoise 只动这一个文件。

事务性：SQLite 单文件 + 一把进程内锁。服务端是单进程单事件循环，够用；
真要并发上 MySQL 时这一层整个换掉。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import anyio
from anyio import to_thread

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- 供应商
CREATE TABLE IF NOT EXISTS dp_provider (
  code        TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  base_url    TEXT NOT NULL DEFAULT '',
  region      TEXT,
  auth_type   TEXT NOT NULL DEFAULT 'bearer',
  docs_url    TEXT NOT NULL DEFAULT '',
  notes       TEXT NOT NULL DEFAULT '',
  enabled     INTEGER NOT NULL DEFAULT 1,
  sort        INTEGER NOT NULL DEFAULT 100
);

-- 模型（一个可被 Router 选中的具体模型）
CREATE TABLE IF NOT EXISTS dp_model (
  id          TEXT PRIMARY KEY,          -- provider:key
  provider    TEXT NOT NULL,
  key         TEXT NOT NULL,
  capability  TEXT NOT NULL,             -- 功能分类，如 video.t2v
  name        TEXT NOT NULL DEFAULT '',
  mode        TEXT NOT NULL DEFAULT 'sync',
  transport   TEXT NOT NULL DEFAULT 'http',
  enabled     INTEGER NOT NULL DEFAULT 1,
  path        TEXT NOT NULL DEFAULT '',
  method      TEXT NOT NULL DEFAULT 'POST',
  headers     TEXT NOT NULL DEFAULT '{}',
  poll        TEXT,
  params      TEXT NOT NULL DEFAULT '{"groups":[]}',
  defaults    TEXT NOT NULL DEFAULT '{}',
  pricing     TEXT NOT NULL DEFAULT '{}',
  notes       TEXT NOT NULL DEFAULT '',
  doc_url     TEXT NOT NULL DEFAULT '',
  source_doc  TEXT NOT NULL DEFAULT '',
  verified    INTEGER NOT NULL DEFAULT 0,  -- 参数是否已逐条核对文档
  priority    INTEGER NOT NULL DEFAULT 100,
  created_at  TEXT NOT NULL DEFAULT '',
  updated_at  TEXT NOT NULL DEFAULT '',
  UNIQUE(provider, key)
);
CREATE INDEX IF NOT EXISTS ix_model_capability ON dp_model(capability, priority);

-- 凭据（每个供应商一条；workspace 暂无使用，留给业务空间维度）
CREATE TABLE IF NOT EXISTS dp_credential (
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

-- 用户
CREATE TABLE IF NOT EXISTS dp_user (
  id            TEXT PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  display_name  TEXT NOT NULL DEFAULT '',
  role          TEXT NOT NULL DEFAULT 'user',
  password_hash TEXT NOT NULL DEFAULT '',
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL DEFAULT ''
);

-- 用量台账（只有成功的真实调用才记账）
CREATE TABLE IF NOT EXISTS dp_usage (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  capability_id TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  unit          TEXT NOT NULL DEFAULT 'free',
  quantity      REAL NOT NULL DEFAULT 0,
  amount        REAL,
  currency      TEXT NOT NULL DEFAULT 'CNY',
  user_id       TEXT,
  task_id       TEXT,
  request_id    TEXT,
  at            TEXT NOT NULL,
  raw           TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS ix_usage_at ON dp_usage(at);

-- 任务台账（异步任务；远端 task_id 必须落库）
CREATE TABLE IF NOT EXISTS dp_task (
  id            TEXT PRIMARY KEY,
  capability_id TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'PENDING',
  remote_task_id TEXT,
  progress      INTEGER NOT NULL DEFAULT 0,
  polls         INTEGER NOT NULL DEFAULT 0,
  payload       TEXT NOT NULL DEFAULT '{}',
  result        TEXT NOT NULL DEFAULT '{}',
  error         TEXT,
  user_id       TEXT,
  created_at    TEXT NOT NULL DEFAULT '',
  updated_at    TEXT NOT NULL DEFAULT '',
  finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_task_status ON dp_task(status, created_at);

-- 杂项键值（默认文本模型名、告警阈值等）
CREATE TABLE IF NOT EXISTS dp_setting (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL DEFAULT ''
);
"""


class Store:
    """同步 SQLite 句柄；对外只暴露 ``execute`` / ``query`` / ``query_one``。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ── 同步原语（线程内调用）────────────────────────────────────

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        with self._lock:
            cur = self._conn.executemany(sql, rows)
            self._conn.commit()
            return cur.rowcount

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 异步包装（事件循环里调用；不阻塞其他请求）────────────────

    async def aexecute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return await to_thread.run_sync(self.execute, sql, params)

    async def aquery(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return await to_thread.run_sync(self.query, sql, params)

    async def aquery_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        return await to_thread.run_sync(self.query_one, sql, params)


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


__all__ = ["SCHEMA", "Store", "anyio", "dumps", "loads"]
