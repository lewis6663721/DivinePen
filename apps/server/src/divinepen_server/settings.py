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
    db_path: Path = field(default_factory=lambda: ROOT / "var" / "divinepen.sqlite3")
    media_root: Path = field(default_factory=lambda: ROOT / "media")
    secret_key: str = ""
    session_max_age_hours: int = 6
    allow_register: bool = True
    #: 默认关闭登录（本地开发直接看页面）。上线前置 true。
    auth_required: bool = False
    #: 默认关闭真实计费调用。
    allow_billed_calls: bool = False
    #: 连通性测试用的默认文本模型（只测文本，绝不测多模态）。
    connectivity_model: str = "qwen-plus"
    credential_key: str = ""
    oss_enabled: bool = False
    oss_endpoint: str = ""
    oss_bucket: str = ""

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(ROOT / ".env")
        db_url = _env("DB_URL")
        if db_url.startswith("sqlite://"):
            db_path = Path(db_url.removeprefix("sqlite://")).expanduser()
        else:
            db_path = Path(_env("DB_FILE") or (ROOT / "var" / "divinepen.sqlite3")).expanduser()
        settings = cls(
            host=_env("HOST", "127.0.0.1"),
            port=int(_env("PORT", "8801") or 8801),
            db_path=db_path,
            media_root=Path(_env("MEDIA_ROOT") or (ROOT / "media")).expanduser(),
            secret_key=_env("SECRET_KEY"),
            session_max_age_hours=int(_env("SESSION_MAX_AGE_HOURS", "6") or 6),
            allow_register=_flag("ALLOW_REGISTER", True),
            auth_required=_flag("AUTH_REQUIRED", False),
            allow_billed_calls=_flag("ALLOW_BILLED_CALLS", False),
            connectivity_model=_env("CONNECTIVITY_MODEL", "qwen-plus"),
            credential_key=_env("CREDENTIAL_KEY"),
            oss_enabled=_flag("OSS_ENABLED"),
            oss_endpoint=_env("OSS_ENDPOINT"),
            oss_bucket=_env("OSS_BUCKET"),
        )
        if not settings.secret_key:
            settings.secret_key = secrets.token_hex(32)
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        settings.media_root.mkdir(parents=True, exist_ok=True)
        return settings


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings
