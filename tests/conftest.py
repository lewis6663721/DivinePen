"""测试夹具：**先把存储挡在门外**，再谈别的。

事故复盘（2026-10-01）：仓库根的 ``.env`` 把 ``DIVINEPEN_DB_URL`` 指向了真实的
MySQL ``divinepen``。测试走 ``get_settings()`` → ``Settings.from_env()`` →
``load_dotenv()``，于是每一次 ``pytest`` 都在生产库里留下任务记录 ——
任务中心那 38 条 ``media.trim`` 就是这么来的（payload 的 ``source`` 全指向
``pytest-of-xxx`` 临时目录）。

所以这里在**任何 settings 被构造之前**（``conftest`` 早于所有测试模块导入）把 DSN
钉死到一次性目录：

* 默认 SQLite + 临时 media 目录，跑完即弃；
* 真要连 MySQL 验证存储适配器时，显式给 ``DIVINEPEN_TEST_DB_URL=mysql://…``。
  那才是"你明确要的行为"，而不是默认行为。

``load_dotenv`` 默认 ``override=False``，所以这里设进 ``os.environ`` 的值赢过
``.env`` —— 顺序正是关键。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="divinepen-tests-"))
#: 每份测试报告都会写出这一行：它现在往哪儿写，一眼可见。
ISOLATED_DB = os.environ.get("DIVINEPEN_TEST_DB_URL") or f"sqlite://{_TMP / 'test.sqlite3'}"

os.environ["DIVINEPEN_DB_URL"] = ISOLATED_DB
os.environ["DIVINEPEN_MEDIA_ROOT"] = str(_TMP / "media")
os.environ["DIVINEPEN_ALLOW_BILLED_CALLS"] = "false"
os.environ["DIVINEPEN_AUTH_REQUIRED"] = "false"


def pytest_report_header(config) -> str:
    return f"divinepen storage → {ISOLATED_DB}  (media → {_TMP / 'media'})"
