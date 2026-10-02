"""服务端配置。全部来自环境变量，前缀 ``DIVINEPEN_``，可选 ``.env``。

设计取舍：**SQLite 是默认存储**，开箱即可 ``make run`` 看到页面；把
``DIVINEPEN_DB_URL`` 指向 MySQL 是后续替换存储适配器的事（见 ``store.py`` 顶部的说明）。
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def _env(name: str, default: str = "") -> str:
    return os.getenv(f"DIVINEPEN_{name}", default).strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = _env(name, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "on"}


def load_dotenv(path: Path) -> None:
    """极简 .env 读取：只填空，不覆盖已有环境变量。"""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


@dataclass(slots=True)
class Settings:
    root: Path = ROOT
    host: str = "127.0.0.1"
    port: int = 8801
    #: ``mysql://user:pass@host:port/db`` 或 ``sqlite:///path``。默认 SQLite 零配置。
    db_dsn: str = ""
    media_root: Path = field(default_factory=lambda: ROOT / "media")
    secret_key: str = ""
    session_max_age_hours: int = 6
    allow_register: bool = True
    #: 默认关闭登录（本地开发直接看页面）。上线前置 true。
    auth_required: bool = False
    #: 连通性测试用的默认文本模型。
    connectivity_model: str = "qwen-plus"
    credential_key: str = ""
    oss_enabled: bool = False
    oss_endpoint: str = ""
    oss_bucket: str = ""
    oss_access_key_id: str = ""
    oss_access_key_secret: str = ""

    @property
    def db_label(self) -> str:
        """给人看的连接描述：密码一律打码。"""
        import re

        return re.sub(r"://[^@/:]+:[^@]+@", "://***:***@", self.db_dsn)

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(ROOT / ".env")
        db_dsn = _env("DB_URL") or _env("DATABASE_URL")
        if not db_dsn:
            db_file = Path(_env("DB_FILE") or (ROOT / "var" / "divinepen.sqlite3")).expanduser()
            db_dsn = f"sqlite://{db_file}"
        settings = cls(
            host=_env("HOST", "127.0.0.1"),
            port=int(_env("PORT", "8801") or 8801),
            db_dsn=db_dsn,
            media_root=Path(_env("MEDIA_ROOT") or (ROOT / "media")).expanduser(),
            secret_key=_env("SECRET_KEY"),
            session_max_age_hours=int(_env("SESSION_MAX_AGE_HOURS", "6") or 6),
            allow_register=_flag("ALLOW_REGISTER", True),
            auth_required=_flag("AUTH_REQUIRED", False),

            connectivity_model=_env("CONNECTIVITY_MODEL", "qwen-plus"),
            credential_key=_env("CREDENTIAL_KEY"),
            oss_enabled=_flag("OSS_ENABLED"),
            oss_endpoint=_env("OSS_ENDPOINT"),
            oss_bucket=_env("OSS_BUCKET"),
            oss_access_key_id=_env("OSS_ACCESS_KEY_ID"),
            oss_access_key_secret=_env("OSS_ACCESS_KEY_SECRET"),
        )
        if not settings.secret_key:
            settings.secret_key = secrets.token_hex(32)
        if settings.db_dsn.startswith("sqlite://"):
            Path(settings.db_dsn.removeprefix("sqlite://")).parent.mkdir(
                parents=True, exist_ok=True
            )
        settings.media_root.mkdir(parents=True, exist_ok=True)
        return settings


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings
