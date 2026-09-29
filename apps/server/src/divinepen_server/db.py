"""存储层：MySQL（默认）与 SQLite（零配置回退）两个方言，同一套仓储代码。

**为什么不是 Tortoise + Aerich**：一期要验证的是"页面参数与接口文档一一对应"，
不是 ORM 选型。所有 SQL 关在 ``Store`` 里，方言差异只有三处：

1. 占位符 ``?`` ⇄ ``%s``；
2. 建表 DDL 两套（AUTOINCREMENT ⇄ AUTO_INCREMENT、TEXT ⇄ LONGTEXT…）；
3. upsert 语法（``ON CONFLICT … DO UPDATE`` ⇄ ``ON DUPLICATE KEY UPDATE``）。

换成 Tortoise 只动这一个文件；仓储（``store.py``）一行不用改。

连接串：``DIVINEPEN_DB_URL=mysql://user:pass@host:port/db``，
不配则回退 ``sqlite://<repo>/var/divinepen.sqlite3``（本地一把梭、测试用）。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from anyio import to_thread

# 列名 `key` 在 MySQL 里是保留字。SQLite 也吃反引号（MySQL 兼容语法），
# 所以两边统一写反引号，仓储里就不用再想方言。
SCHEMA_SQLITE = """
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

CREATE TABLE IF NOT EXISTS dp_model (
  id          TEXT PRIMARY KEY,
  provider    TEXT NOT NULL,
  `key`       TEXT NOT NULL,
  capability  TEXT NOT NULL,
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
  verified    INTEGER NOT NULL DEFAULT 0,
  priority    INTEGER NOT NULL DEFAULT 100,
  created_at  TEXT NOT NULL DEFAULT '',
  updated_at  TEXT NOT NULL DEFAULT '',
  UNIQUE(provider, `key`)
);
CREATE INDEX IF NOT EXISTS ix_model_capability ON dp_model(capability, priority);

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

CREATE TABLE IF NOT EXISTS dp_user (
  id            TEXT PRIMARY KEY,
  username      TEXT NOT NULL UNIQUE,
  display_name  TEXT NOT NULL DEFAULT '',
  role          TEXT NOT NULL DEFAULT 'user',
  password_hash TEXT NOT NULL DEFAULT '',
  enabled       INTEGER NOT NULL DEFAULT 1,
  created_at    TEXT NOT NULL DEFAULT ''
);

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
  raw           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_usage_at ON dp_usage(at);

CREATE TABLE IF NOT EXISTS dp_task (
  id            TEXT PRIMARY KEY,
  capability_id TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  status        TEXT NOT NULL DEFAULT 'PENDING',
  remote_task_id TEXT,
  progress      INTEGER NOT NULL DEFAULT 0,
  polls         INTEGER NOT NULL DEFAULT 0,
  payload       TEXT NOT NULL,
  result        TEXT NOT NULL,
  error         TEXT,
  user_id       TEXT,
  created_at    TEXT NOT NULL DEFAULT '',
  updated_at    TEXT NOT NULL DEFAULT '',
  finished_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_task_status ON dp_task(status, created_at);

CREATE TABLE IF NOT EXISTS dp_setting (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL DEFAULT ''
);
"""

SCHEMA_MYSQL = """
CREATE TABLE IF NOT EXISTS dp_provider (
  code        VARCHAR(64) PRIMARY KEY,
  name        VARCHAR(191) NOT NULL,
  base_url    VARCHAR(500) NOT NULL DEFAULT '',
  region      VARCHAR(64) NULL,
  auth_type   VARCHAR(32) NOT NULL DEFAULT 'bearer',
  docs_url    VARCHAR(500) NOT NULL DEFAULT '',
  notes       TEXT NULL,
  enabled     TINYINT(1) NOT NULL DEFAULT 1,
  sort        INT NOT NULL DEFAULT 100
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dp_model (
  id          VARCHAR(191) PRIMARY KEY,
  provider    VARCHAR(64) NOT NULL,
  `key`       VARCHAR(128) NOT NULL,
  capability  VARCHAR(64) NOT NULL,
  name        VARCHAR(191) NOT NULL DEFAULT '',
  mode        VARCHAR(16) NOT NULL DEFAULT 'sync',
  transport   VARCHAR(16) NOT NULL DEFAULT 'http',
  enabled     TINYINT(1) NOT NULL DEFAULT 1,
  path        VARCHAR(500) NOT NULL DEFAULT '',
  method      VARCHAR(16) NOT NULL DEFAULT 'POST',
  headers     TEXT NULL,
  poll        TEXT NULL,
  params      LONGTEXT NOT NULL,
  defaults    TEXT NULL,
  pricing     TEXT NULL,
  notes       TEXT NULL,
  doc_url     VARCHAR(500) NOT NULL DEFAULT '',
  source_doc  VARCHAR(191) NOT NULL DEFAULT '',
  verified    TINYINT(1) NOT NULL DEFAULT 0,
  priority    INT NOT NULL DEFAULT 100,
  created_at  VARCHAR(40) NOT NULL DEFAULT '',
  updated_at  VARCHAR(40) NOT NULL DEFAULT '',
  UNIQUE KEY uq_model_provider_key (provider, `key`),
  KEY ix_model_capability (capability, priority)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dp_credential (
  id            VARCHAR(191) PRIMARY KEY,
  provider      VARCHAR(64) NOT NULL,
  workspace     VARCHAR(64) NOT NULL DEFAULT '',
  api_key       TEXT NOT NULL,
  encrypted     TINYINT(1) NOT NULL DEFAULT 0,
  last_test_at  VARCHAR(40) NULL,
  last_test_ok  TINYINT(1) NULL,
  last_test_note VARCHAR(500) NOT NULL DEFAULT '',
  created_at    VARCHAR(40) NOT NULL DEFAULT '',
  updated_at    VARCHAR(40) NOT NULL DEFAULT '',
  UNIQUE KEY uq_cred_provider_ws (provider, workspace)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dp_user (
  id            VARCHAR(64) PRIMARY KEY,
  username      VARCHAR(191) NOT NULL UNIQUE,
  display_name  VARCHAR(191) NOT NULL DEFAULT '',
  role          VARCHAR(32) NOT NULL DEFAULT 'user',
  password_hash VARCHAR(255) NOT NULL DEFAULT '',
  enabled       TINYINT(1) NOT NULL DEFAULT 1,
  created_at    VARCHAR(40) NOT NULL DEFAULT ''
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dp_usage (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  capability_id VARCHAR(64) NOT NULL,
  provider      VARCHAR(64) NOT NULL,
  model         VARCHAR(128) NOT NULL,
  unit          VARCHAR(32) NOT NULL DEFAULT 'free',
  quantity      DOUBLE NOT NULL DEFAULT 0,
  amount        DECIMAL(18, 4) NULL,
  currency      VARCHAR(8) NOT NULL DEFAULT 'CNY',
  user_id       VARCHAR(64) NULL,
  task_id       VARCHAR(64) NULL,
  request_id    VARCHAR(128) NULL,
  at            VARCHAR(40) NOT NULL,
  raw           LONGTEXT NOT NULL,
  KEY ix_usage_at (at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dp_task (
  id            VARCHAR(64) PRIMARY KEY,
  capability_id VARCHAR(64) NOT NULL,
  provider      VARCHAR(64) NOT NULL,
  model         VARCHAR(128) NOT NULL,
  status        VARCHAR(24) NOT NULL DEFAULT 'PENDING',
  remote_task_id VARCHAR(128) NULL,
  progress      INT NOT NULL DEFAULT 0,
  polls         INT NOT NULL DEFAULT 0,
  payload       LONGTEXT NOT NULL,
  result        LONGTEXT NOT NULL,
  error         TEXT NULL,
  user_id       VARCHAR(64) NULL,
  created_at    VARCHAR(40) NOT NULL DEFAULT '',
  updated_at    VARCHAR(40) NOT NULL DEFAULT '',
  finished_at   VARCHAR(40) NULL,
  KEY ix_task_status (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS dp_setting (
  k VARCHAR(191) PRIMARY KEY,
  v TEXT NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""

_PLACEHOLDER = re.compile(r"\?")


def _to_mysql_params(sql: str) -> str:
    return _PLACEHOLDER.sub("%s", sql)


class Store:
    """异步存储门面。仓储只认这几个方法，方言差异全部沉在这里。"""

    dialect = "abstract"

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:  # pragma: no cover
        raise NotImplementedError

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:  # pragma: no cover
        raise NotImplementedError

    async def query(
        self, sql: str, params: Sequence[Any] = ()
    ) -> list[dict[str, Any]]:  # pragma: no cover
        raise NotImplementedError

    async def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
        rows = await self.query(sql, params)
        return rows[0] if rows else None

    async def upsert(
        self,
        table: str,
        keys: dict[str, Any],
        data: dict[str, Any],
        update: Sequence[str] | None = None,
    ) -> int:
        """方言无关的 upsert。``keys`` 是唯一键列，``update`` 缺省为 data 的全部列。"""
        columns = list(keys) + [c for c in data if c not in keys]
        values = [keys[c] for c in keys] + [data[c] for c in data if c not in keys]
        update_cols = list(update) if update is not None else [c for c in data if c not in keys]
        cols_sql = ", ".join(f"`{c}`" if c == "key" else c for c in columns)
        marks = ", ".join(["?"] * len(columns))
        if self.dialect == "mysql":
            sets = ", ".join(f"{_q(c)} = VALUES({_q(c)})" for c in update_cols)
            sql = (
                f"INSERT INTO {table} ({cols_sql}) VALUES ({marks}) ON DUPLICATE KEY UPDATE {sets}"
            )
        else:
            conflict = ", ".join(_q(c) for c in keys)
            sets = ", ".join(f"{_q(c)} = excluded.{_q(c)}" for c in update_cols)
            sql = (
                f"INSERT INTO {table} ({cols_sql}) VALUES ({marks}) "
                f"ON CONFLICT({conflict}) DO UPDATE SET {sets}"
            )
        return await self.execute(sql, values)

    async def aclose(self) -> None:  # pragma: no cover
        raise NotImplementedError

    @property
    def description(self) -> str:  # pragma: no cover
        raise NotImplementedError


def _q(column: str) -> str:
    return f"`{column}`" if column == "key" else column


class SqliteStore(Store):
    dialect = "sqlite"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA_SQLITE)
        self._conn.commit()

    @property
    def description(self) -> str:
        return f"sqlite://{self.path}"

    def _execute(self, sql: str, params: Sequence[Any]) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.rowcount

    def _executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        with self._lock:
            cur = self._conn.executemany(sql, list(rows))
            self._conn.commit()
            return cur.rowcount

    def _query(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return await to_thread.run_sync(self._execute, sql, params)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        return await to_thread.run_sync(self._executemany, sql, rows)

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return await to_thread.run_sync(self._query, sql, params)

    async def aclose(self) -> None:
        with self._lock:
            self._conn.close()


class MySQLStore(Store):
    dialect = "mysql"

    def __init__(self, pool: Any, dsn: str) -> None:
        self._pool = pool
        self._dsn = dsn

    @property
    def description(self) -> str:
        return re.sub(r"://[^@]*@", "://***:***@", self._dsn)

    async def _run(self, sql: str, params: Sequence[Any], many: Iterable[Sequence[Any]] | None):
        translated = _to_mysql_params(sql)
        async with self._pool.acquire() as conn, conn.cursor() as cur:
            if many is not None:
                await cur.executemany(translated, list(many))
            else:
                await cur.execute(translated, tuple(params))
            await conn.commit()
            return cur.rowcount

    async def _fetch(self, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
        import aiomysql

        translated = _to_mysql_params(sql)
        async with self._pool.acquire() as conn, conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute(translated, tuple(params))
            rows = await cur.fetchall()
            return [_normalize(row) for row in rows]

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return await self._run(sql, params, None)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> int:
        return await self._run(sql, (), rows)

    async def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        return await self._fetch(sql, params)

    async def aclose(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()


def _normalize(row: dict[str, Any]) -> dict[str, Any]:
    """MySQL 会把 DECIMAL 读成 Decimal、TINYINT 读成 int —— 统一成仓储期望的形状。"""
    from decimal import Decimal

    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            # COUNT/SUM 回来的是整数 Decimal，金额才是小数 —— 整数就还原成 int，
            # 否则模板里 "26" 会变成 "26.0"。
            out[key] = int(value) if value == value.to_integral_value() else float(value)
        elif isinstance(value, (bytes, bytearray)):
            out[key] = value.decode("utf-8", "replace")
        else:
            out[key] = value
    return out


async def connect_store(dsn: str) -> Store:
    """按连接串建存储。mysql:// 走连接池；其它一律当 SQLite 文件路径。"""
    if dsn.startswith("mysql://") or dsn.startswith("mysql+aiomysql://"):
        from urllib.parse import urlparse

        import aiomysql

        parsed = urlparse(dsn.replace("mysql+aiomysql://", "mysql://", 1))
        pool = await aiomysql.create_pool(
            host=parsed.hostname or "127.0.0.1",
            port=parsed.port or 3306,
            user=parsed.username or "root",
            password=parsed.password or "",
            db=(parsed.path or "/divinepen").lstrip("/"),
            charset="utf8mb4",
            autocommit=False,
            minsize=1,
            maxsize=8,
            pool_recycle=1800,
        )
        store = MySQLStore(pool, dsn)
        for statement in [s.strip() for s in SCHEMA_MYSQL.split(";") if s.strip()]:
            await store.execute(statement)
        return store
    path = dsn.removeprefix("sqlite://")
    return SqliteStore(path)


def loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


__all__ = [
    "SCHEMA_MYSQL",
    "SCHEMA_SQLITE",
    "MySQLStore",
    "SqliteStore",
    "Store",
    "connect_store",
    "dumps",
    "loads",
]
