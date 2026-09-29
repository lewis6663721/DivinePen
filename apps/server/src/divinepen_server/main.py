"""开发服务器入口：``python -m divinepen_server.main``。

生产建议 ``uvicorn divinepen_server.web:app`` + 反向代理，本文件只负责本地起服务。
"""

from __future__ import annotations

import uvicorn

from .settings import get_settings
from .web import create_app

#: 供 ``uvicorn divinepen_server.main:app`` 直接使用
app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "divinepen_server.web:app", host=settings.host, port=settings.port, log_level="info"
    )


if __name__ == "__main__":
    run()
