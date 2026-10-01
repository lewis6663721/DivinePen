"""一次性清理：把测试跑进生产库的任务记录备份成 JSON 再删掉。

用法：``uv run python scripts/purge_test_tasks.py --apply``
不加 ``--apply`` 只打印将要删除的行。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "server" / "src"))
sys.path.insert(0, str(ROOT / "src"))

import aiomysql  # noqa: E402

from divinepen_server.settings import Settings  # noqa: E402


def _parts(dsn: str) -> dict:
    rest = dsn.split("://", 1)[1]
    creds, hostpart = rest.rsplit("@", 1)
    user, _, password = creds.partition(":")
    hostport, _, dbname = hostpart.partition("/")
    host, _, port = hostport.partition(":")
    return {
        "user": user,
        "password": password,
        "host": host or "127.0.0.1",
        "port": int(port or 3306),
        "db": dbname,
    }


def is_test_artifact(row: dict) -> bool:
    """测试残留的判据：payload 里的路径指向临时目录或仓库里的测试样例。"""
    payload = row.get("payload") or "{}"
    markers = ("pytest", "/private/var/folders", "var/sample", "/tmp/x.")
    return any(m in payload for m in markers)


async def main() -> None:
    dsn = Settings.from_env().db_dsn
    if not dsn.startswith("mysql://"):
        print(f"当前不是 MySQL（{dsn}），无需清理。")
        return
    conn = await aiomysql.connect(**_parts(dsn), autocommit=True)
    async with conn.cursor(aiomysql.DictCursor) as cur:
        await cur.execute("SELECT * FROM dp_task ORDER BY created_at")
        rows = await cur.fetchall()
        junk = [r for r in rows if is_test_artifact(r)]
        print(f"dp_task 共 {len(rows)} 行，其中测试残留 {len(junk)} 行：")
        for r in junk:
            print(
                f"  {r['id']}  {r['capability_id']:26} {r['status']:10} {(r.get('payload') or '')[:70]}"
            )
        if "--apply" not in sys.argv:
            print("\n（dry-run，没有改动。加 --apply 才真删。）")
            return
        if not junk:
            print("没有要删的行。")
            return
        backup = ROOT / "var" / "backup" / f"dp_task-{datetime.now():%Y%m%d-%H%M%S}.json"
        backup.parent.mkdir(parents=True, exist_ok=True)
        backup.write_text(
            json.dumps([dict(r) for r in junk], ensure_ascii=False, indent=2, default=str)
        )
        ids = [r["id"] for r in junk]
        placeholders = ",".join(["%s"] * len(ids))
        await cur.execute(f"DELETE FROM dp_task WHERE id IN ({placeholders})", ids)
        print(f"\n已删除 {cur.rowcount} 行；备份在 {backup.relative_to(ROOT)}")
    conn.close()


if __name__ == "__main__":
    asyncio.run(main())
