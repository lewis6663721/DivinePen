"""DivinePen 控制台服务 + 模型底座的后台实现。

分层（与 ``docs/ARCHITECTURE.md`` 对齐）：

* ``divinepen``（根包）：契约 + 能力 + 适配器，**零 Web、零 ORM**。
* ``divinepen_server``（本包）：把端口落到 SQLite/本地磁盘，再在上面盖一层
  Jinja2 服务端渲染的控制台。

    * ``settings.py`` 配置
    * ``db.py``       SQLite 适配器（唯一出现裸 SQL 的地方）
    * ``store.py``    仓储 + 领域规则（脱敏、加密、会话、seed）
    * ``catalog.py``  阿里系模型目录（一期 seed 的唯一真相源）
    * ``runtime.py``  端口实现 + 请求组装/计费估算 + 连通性测试
    * ``web.py``      路由与模板
"""

from .settings import Settings, get_settings
from .web import create_app

__all__ = ["Settings", "create_app", "get_settings"]
__version__ = "0.1.0.dev0"
