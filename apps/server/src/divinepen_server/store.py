"""仓储与领域逻辑：把 ``db.Store`` 的裸 SQL 收成有语义的方法。

这一层是"服务端实现端口"的落点：``ARCHITECTURE.md`` 里的
Credentials / Catalog / Usage / Tasks / Artifacts 五个端口，在服务端就是这几组仓储。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from divinepen.core.specs import ModelSpec

from .catalog import MODELS, PROVIDERS
from .db import Store, dumps, loads

PBKDF2_ROUNDS = 200_000

# ── 小工具 ─────────────────────────────────────────────────────────


def now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 10:
        return value[:2] + "·" * max(len(value) - 2, 1)
    return f"{value[:6]}····{value[-4:]}（共 {len(value)} 位）"


def hash_password(password: str, *, salt: str | None = None, rounds: int = PBKDF2_ROUNDS) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), rounds)
    return f"pbkdf2_sha256${rounds}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds))
    return hmac.compare_digest(candidate.hex(), digest)


class SecretBox:
    """凭据加解密。

    有 ``cryptography`` 且配了 ``DIVINEPEN_CREDENTIAL_KEY`` → 真加密（Fernet）；
    否则**明文存库但在页面上显式告警**。不搞"看起来加密了其实没加密"的把戏。
    """

    def __init__(self, key: str) -> None:
        self.key = key
        self._fernet = None
        self.backend = "plain"
        if not key:
            return
        try:  # pragma: no cover - 取决于运行环境
            from cryptography.fernet import Fernet
        except ImportError:
            self.backend = "plain"
            return
        material = hashlib.sha256(key.encode()).digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(material))
        self.backend = "fernet"

    @property
    def encrypted(self) -> bool:
        return self._fernet is not None

    def seal(self, value: str) -> str:
        if not self._fernet or not value:
            return value
        return self._fernet.encrypt(value.encode()).decode()

    def open(self, value: str) -> str:
        if not self._fernet or not value:
            return value
        try:
            return self._fernet.decrypt(value.encode()).decode()
        except Exception:
            return ""


# ── 会话 ───────────────────────────────────────────────────────────


class SessionSigner:
    def __init__(self, secret: str, max_age_hours: int = 6) -> None:
        self.secret = secret.encode()
        self.max_age = max_age_hours * 3600

    def sign(self, payload: dict[str, Any]) -> str:
        payload = {**payload, "exp": int(time.time()) + self.max_age}
        raw = base64.urlsafe_b64encode(dumps(payload).encode()).decode().rstrip("=")
        mac = hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()[:32]
        return f"{raw}.{mac}"

    def verify(self, token: str | None) -> dict[str, Any] | None:
        if not token or "." not in token:
            return None
        raw, _, mac = token.rpartition(".")
        expected = hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(mac, expected):
            return None
        try:
            padded = raw + "=" * (-len(raw) % 4)
            payload = json.loads(base64.urlsafe_b64decode(padded))
        except Exception:
            return None
        if int(payload.get("exp", 0)) < time.time():
            return None
        return payload


# ── 仓储 ───────────────────────────────────────────────────────────


class Repo:
    """所有查询的唯一入口。**表名与字段名只在这里出现一次。**"""

    def __init__(self, store: Store, secrets_box: SecretBox) -> None:
        self.db = store
        self.box = secrets_box

    # 供应商 ------------------------------------------------------
    async def providers(self) -> list[dict[str, Any]]:
        return await self.db.query("SELECT * FROM dp_provider ORDER BY sort, code")

    async def provider(self, code: str) -> dict[str, Any] | None:
        return await self.db.query_one("SELECT * FROM dp_provider WHERE code = ?", (code,))

    async def upsert_provider(self, row: dict[str, Any], *, force: bool = False) -> bool:
        existing = await self.provider(row["code"])
        if existing and not force:
            return False
        await self.db.upsert(
            "dp_provider",
            keys={"code": row["code"]},
            data={
                "name": row["name"],
                "base_url": row.get("base_url", ""),
                "region": row.get("region"),
                "auth_type": row.get("auth_type", "bearer"),
                "docs_url": row.get("docs_url", ""),
                "notes": row.get("notes", ""),
                "enabled": int(row.get("enabled", 1)),
                "sort": int(row.get("sort", 100)),
            },
        )
        return True

    async def set_provider_enabled(self, code: str, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE dp_provider SET enabled = ? WHERE code = ?", (int(enabled), code)
        )

    # 模型 --------------------------------------------------------
    @staticmethod
    def _model_row(spec: ModelSpec) -> tuple:
        meta = spec.meta or {}
        return (
            f"{spec.provider}:{spec.key}",
            spec.provider,
            spec.key,
            spec.capability,
            spec.name or spec.key,
            spec.api.mode,
            str(meta.get("transport", "http")),
            int(spec.enabled),
            spec.api.path,
            spec.api.method,
            dumps(spec.api.headers),
            dumps(spec.api.poll.model_dump(mode="json")) if spec.api.poll else None,
            dumps(spec.params.model_dump(mode="json")),
            dumps(spec.defaults),
            dumps(spec.pricing.model_dump(mode="json")),
            spec.notes,
            spec.doc_url,
            str(meta.get("source_doc", "")),
            int(bool(meta.get("verified", False))),
            int(meta.get("priority", 100)),
            now_iso(),
            now_iso(),
        )

    MODEL_COLUMNS = (
        "id, provider, `key`, capability, name, mode, transport, enabled, path, method, "
        "headers, poll, params, defaults, pricing, notes, doc_url, source_doc, verified, "
        "priority, created_at, updated_at"
    )

    async def upsert_model(self, spec: ModelSpec, *, force: bool = False) -> str:
        """返回 'inserted' / 'skipped' / 'updated'。

        默认 ``skip``：**绝不覆盖用户在页面上改过的参数**；``force=True``
        才按目录重写（用于文档更新后的重新导入）。
        """
        row = self._model_row(spec)
        existing = await self.db.query_one("SELECT id FROM dp_model WHERE id = ?", (row[0],))
        placeholders = ", ".join(["?"] * len(row))
        if existing and not force:
            return "skipped"
        if existing:
            sets = ", ".join(
                f"{col.strip()} = ?" for col in self.MODEL_COLUMNS.split(",") if col.strip() != "id"
            )
            await self.db.execute(f"UPDATE dp_model SET {sets} WHERE id = ?", (*row[1:], row[0]))
            return "updated"
        await self.db.execute(
            f"INSERT INTO dp_model ({self.MODEL_COLUMNS}) VALUES ({placeholders})", row
        )
        return "inserted"

    async def models(
        self,
        *,
        capability: str | None = None,
        provider: str | None = None,
        include_disabled: bool = True,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dp_model WHERE 1=1"
        args: list[Any] = []
        if capability:
            sql += " AND capability = ?"
            args.append(capability)
        if provider:
            sql += " AND provider = ?"
            args.append(provider)
        if not include_disabled:
            sql += " AND enabled = 1"
        sql += " ORDER BY capability, priority, `key`"
        return await self.db.query(sql, args)

    async def model(self, model_id: str) -> dict[str, Any] | None:
        row = await self.db.query_one("SELECT * FROM dp_model WHERE id = ?", (model_id,))
        return self.hydrate_model(row) if row else None

    @staticmethod
    def hydrate_model(row: dict[str, Any]) -> dict[str, Any]:
        row["headers_obj"] = loads(row.get("headers"), {})
        row["poll_obj"] = loads(row.get("poll"), None)
        row["params_obj"] = loads(row.get("params"), {"groups": []})
        row["defaults_obj"] = loads(row.get("defaults"), {})
        row["pricing_obj"] = loads(row.get("pricing"), {})
        row["fields"] = [
            field
            for group in row["params_obj"].get("groups", [])
            for field in group.get("fields", [])
        ]
        row["billed_fields"] = [f for f in row["fields"] if f.get("billed")]
        return row

    async def set_model_enabled(self, model_id: str, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE dp_model SET enabled = ?, updated_at = ? WHERE id = ?",
            (int(enabled), now_iso(), model_id),
        )

    async def update_model_field_default(self, model_id: str, key: str, value: Any) -> bool:
        row = await self.model(model_id)
        if not row:
            return False
        params = row["params_obj"]
        for group in params.get("groups", []):
            for field in group.get("fields", []):
                if field.get("key") == key:
                    field["default"] = value
                    await self.db.execute(
                        "UPDATE dp_model SET params = ?, updated_at = ? WHERE id = ?",
                        (dumps(params), now_iso(), model_id),
                    )
                    return True
        return False

    async def model_in_use(self, model_id: str) -> int:
        """有多少条任务记录指向这个模型。

        这是删除前的闸门：任务记录里存的是模型名（不是外键），删掉模型之后
        历史任务就会显示一个库里查不到的名字 —— 账单对不上账，往往就是从
        这种"少了一行"开始的。有引用就只让停用，不让删。
        """
        provider, _, key = model_id.partition(":")
        row = await self.db.query_one(
            "SELECT COUNT(*) AS n FROM dp_task WHERE provider = ? AND model = ?", (provider, key)
        )
        return int((row or {}).get("n") or 0)

    async def set_model_verified(self, model_id: str, verified: bool) -> None:
        """核对印记是**人**给的：参数与文档逐字对过，才配挂「已核对」。

        页面一改参数就自动退回待核对，改完由人在这里重新盖章；
        没有这一下，改过一次的模型就永远挂着「待核对」，这个标记也就废了。
        """
        await self.db.execute(
            "UPDATE dp_model SET verified = ?, updated_at = ? WHERE id = ?",
            (int(verified), now_iso(), model_id),
        )

    async def delete_model(self, model_id: str) -> None:
        await self.db.execute("DELETE FROM dp_model WHERE id = ?", (model_id,))

    async def capabilities(self) -> list[str]:
        """库里出现过的能力分类，给新增模型时的输入建议用。"""
        rows = await self.db.query("SELECT DISTINCT capability FROM dp_model")
        return sorted(r["capability"] for r in rows if r.get("capability"))

    # 凭据 --------------------------------------------------------
    # 一个供应商可以配多把钥匙（额度分开的账号、正式号与测试号并存），
    # 但**调用时只能挑一把**：is_default 就是那一把。所有"拿钥匙"的路径
    # 都走 default 优先，页面按 is_default DESC 排 —— 顺序本身就是答案。
    @staticmethod
    def shape_credential(row: dict[str, Any]) -> dict[str, Any]:
        """把一行钥匙整理成对外的形状（掩码、布尔化）。

        三个取数方法（列表 / 单取 / 按 id）都得过这一道 —— 否则同一个字段
        会因为调用路径不同而时而是 ``0/1`` 时而是 ``True/False``，
        调用方就得记住"从哪儿拿的"，那种约定迟早会漏。
        """
        row["api_key_masked"] = mask_secret(row.get("api_key", ""))
        row["has_key"] = bool(row.get("api_key"))
        row["is_default"] = bool(row.get("is_default"))
        # last_test_ok 允许是 None（从没测过）—— 那与"测了但失败"是两回事，
        # 别让"没测过"看起来像"挂了"。
        if row.get("last_test_ok") is not None:
            row["last_test_ok"] = bool(row["last_test_ok"])
        return row

    async def credentials(self, provider: str | None = None) -> list[dict[str, Any]]:
        """某供应商（或全部）的钥匙清单，默认的那把排最前。"""
        sql = "SELECT * FROM dp_credential"
        params: tuple[Any, ...] = ()
        if provider:
            sql += " WHERE provider = ?"
            params = (provider,)
        sql += " ORDER BY provider, workspace, is_default DESC, created_at"
        return [self.shape_credential(row) for row in await self.db.query(sql, params)]

    async def default_credentials(self) -> dict[str, dict[str, Any]]:
        """``{供应商: 默认钥匙}``。

        给"这个供应商配了吗"这类判断用。注意它**不是** ``credentials()`` 的
        简单索引 —— 多把钥匙时按 provider 建字典只会留下最后一行，
        默认不默认全看插入顺序，那是 bug 不是设计。
        """
        out: dict[str, dict[str, Any]] = {}
        for row in await self.credentials():
            current = out.get(row["provider"])
            if current is None or (row["is_default"] and not current["is_default"]):
                out[row["provider"]] = row
        return out

    async def credential(
        self, provider: str, workspace: str = "", label: str | None = None
    ) -> dict[str, Any] | None:
        """按名字取一把；不点名就等于"取默认那把"。"""
        if label is None:
            row = await self.db.query_one(
                "SELECT * FROM dp_credential WHERE provider = ? AND workspace = ? "
                "ORDER BY is_default DESC, created_at LIMIT 1",
                (provider, workspace),
            )
        else:
            row = await self.db.query_one(
                "SELECT * FROM dp_credential WHERE provider = ? AND workspace = ? AND label = ?",
                (provider, workspace, label),
            )
        return self.shape_credential(row) if row else None

    async def credential_by_id(self, cred_id: str) -> dict[str, Any] | None:
        row = await self.db.query_one("SELECT * FROM dp_credential WHERE id = ?", (cred_id,))
        return self.shape_credential(row) if row else None

    async def raw_api_key(
        self, provider: str, workspace: str = "", label: str | None = None
    ) -> str:
        row = await self.credential(provider, workspace, label)
        if not row:
            return ""
        return self.box.open(row.get("api_key", ""))

    async def save_credential(
        self,
        provider: str,
        api_key: str,
        *,
        label: str = "默认",
        workspace: str = "",
        make_default: bool | None = None,
    ) -> str:
        """新增或覆盖「同一供应商 + 同一工作区 + 同一名字」下的那把钥匙。

        两条规矩，都是为了不出现"配了却用不上"的状态：

        * **第一把自动成为默认** —— 否则配完钥匙，调用仍然发不出去。
        * 覆盖一把已经存在的钥匙时，**不改它的默认身份** ——
          除非用户明确要求。改个密钥不该顺手把默认换掉。
        """
        label = (label or "默认").strip() or "默认"
        existing = await self.credential(provider, workspace, label)
        has_any = await self.db.query_one(
            "SELECT id FROM dp_credential WHERE provider = ? AND workspace = ? LIMIT 1",
            (provider, workspace),
        )
        if make_default is None:
            make_default = not has_any
        cred_id = existing["id"] if existing else f"{provider}:{workspace or '-'}:{label}"
        update = ["api_key", "encrypted", "updated_at"]
        if make_default:
            update.append("is_default")
        await self.db.upsert(
            "dp_credential",
            keys={"provider": provider, "workspace": workspace, "label": label},
            data={
                "id": cred_id,
                "api_key": self.box.seal(api_key.strip()),
                "encrypted": int(self.box.encrypted),
                "is_default": int(bool(make_default)),
                "created_at": now_iso(),
                "updated_at": now_iso(),
            },
            update=update,
        )
        if make_default:
            await self.set_default_credential(provider, cred_id, workspace)
        return cred_id

    async def set_default_credential(
        self, provider: str, cred_id: str, workspace: str = ""
    ) -> None:
        """把某把钥匙设为默认。**先全部清零再置一**，不留两个默认。"""
        row = await self.credential_by_id(cred_id)
        if row:
            workspace = row.get("workspace", workspace) or ""
        await self.db.execute(
            "UPDATE dp_credential SET is_default = 0 WHERE provider = ? AND workspace = ?",
            (provider, workspace),
        )
        await self.db.execute(
            "UPDATE dp_credential SET is_default = 1, updated_at = ? WHERE id = ?",
            (now_iso(), cred_id),
        )

    async def delete_credential(self, cred_id: str) -> dict[str, Any] | None:
        """删一把。删掉的若正是默认，**自动把剩下最早的一把顶上去** ——
        供应商不该出现"有钥匙但没有默认"的状态，那等于没钥匙。
        """
        row = await self.credential_by_id(cred_id)
        if not row:
            return None
        await self.db.execute("DELETE FROM dp_credential WHERE id = ?", (cred_id,))
        if row.get("is_default"):
            nxt = await self.credential(row["provider"], row.get("workspace", "") or "")
            if nxt and nxt["id"] != cred_id:
                await self.set_default_credential(row["provider"], nxt["id"])
        return row

    async def record_credential_test(self, cred_id: str, ok: bool, note: str) -> None:
        """记录某把钥匙的连通性测试结果。

        按 **id** 记，不按供应商记：多把钥匙的场景下"这个供应商通不通"
        本身就没有意义 —— 不通的永远是具体那一把。
        """
        await self.db.execute(
            "UPDATE dp_credential SET last_test_at = ?, last_test_ok = ?, last_test_note = ? "
            "WHERE id = ?",
            (now_iso(), int(ok), note, cred_id),
        )

    # 用户 --------------------------------------------------------
    async def users(self) -> list[dict[str, Any]]:
        return await self.db.query("SELECT * FROM dp_user ORDER BY created_at")

    async def user_by_name(self, username: str) -> dict[str, Any] | None:
        return await self.db.query_one("SELECT * FROM dp_user WHERE username = ?", (username,))

    async def user(self, user_id: str) -> dict[str, Any] | None:
        return await self.db.query_one("SELECT * FROM dp_user WHERE id = ?", (user_id,))

    async def create_user(
        self,
        username: str,
        password: str,
        *,
        display_name: str = "",
        role: str = "user",
    ) -> str:
        user_id = f"u_{secrets.token_hex(6)}"
        await self.db.execute(
            """INSERT INTO dp_user (id, username, display_name, role, password_hash, enabled, created_at)
               VALUES (?, ?, ?, ?, ?, 1, ?)""",
            (user_id, username, display_name or username, role, hash_password(password), now_iso()),
        )
        return user_id

    async def set_user_enabled(self, user_id: str, enabled: bool) -> None:
        await self.db.execute(
            "UPDATE dp_user SET enabled = ? WHERE id = ?", (int(enabled), user_id)
        )

    async def set_user_role(self, user_id: str, role: str) -> None:
        await self.db.execute("UPDATE dp_user SET role = ? WHERE id = ?", (role, user_id))

    async def set_user_password(self, user_id: str, password: str) -> None:
        await self.db.execute(
            "UPDATE dp_user SET password_hash = ? WHERE id = ?", (hash_password(password), user_id)
        )

    # 用量 --------------------------------------------------------
    async def record_usage(self, entry: dict[str, Any]) -> None:
        await self.db.execute(
            """INSERT INTO dp_usage
                 (capability_id, provider, model, unit, quantity, amount, currency,
                  user_id, task_id, request_id, at, raw)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry.get("capability_id", ""),
                entry.get("provider", ""),
                entry.get("model", ""),
                entry.get("unit", "free"),
                float(entry.get("quantity") or 0),
                entry.get("amount"),
                entry.get("currency", "CNY"),
                entry.get("user_id"),
                entry.get("task_id"),
                entry.get("request_id"),
                entry.get("at") or now_iso(),
                dumps(entry.get("raw", {})),
            ),
        )

    async def usage(self, *, limit: int = 200) -> list[dict[str, Any]]:
        return await self.db.query(
            "SELECT * FROM dp_usage ORDER BY at DESC, id DESC LIMIT ?", (limit,)
        )

    async def usage_summary(self) -> dict[str, Any]:
        today = datetime.now().astimezone().date().isoformat()
        totals = await self.db.query_one(
            """SELECT COUNT(*) AS calls,
                      COALESCE(SUM(amount), 0) AS amount,
                      COALESCE(SUM(CASE WHEN at LIKE ? THEN amount ELSE 0 END), 0) AS today_amount,
                      COALESCE(SUM(CASE WHEN at LIKE ? THEN 1 ELSE 0 END), 0) AS today_calls
               FROM dp_usage""",
            (f"{today}%", f"{today}%"),
        )
        by_model = await self.db.query(
            """SELECT model, COUNT(*) AS calls, COALESCE(SUM(amount), 0) AS amount
               FROM dp_usage GROUP BY model ORDER BY amount DESC, calls DESC LIMIT 8"""
        )
        return {"totals": totals or {}, "by_model": by_model}

    async def pending_billed_count(self) -> int:
        row = await self.db.query_one(
            "SELECT COUNT(*) AS n FROM dp_usage WHERE amount IS NULL AND unit != 'free'"
        )
        return int((row or {}).get("n", 0))

    # 任务 --------------------------------------------------------
    async def create_task(self, entry: dict[str, Any]) -> str:
        task_id = entry.get("id") or f"t_{secrets.token_hex(8)}"
        await self.db.execute(
            """INSERT INTO dp_task
                 (id, capability_id, provider, model, status, remote_task_id, progress, polls,
                  payload, result, error, user_id, created_at, updated_at, finished_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id,
                entry.get("capability_id", ""),
                entry.get("provider", ""),
                entry.get("model", ""),
                entry.get("status", "PENDING"),
                entry.get("remote_task_id"),
                int(entry.get("progress") or 0),
                int(entry.get("polls") or 0),
                dumps(entry.get("payload", {})),
                dumps(entry.get("result", {})),
                entry.get("error"),
                entry.get("user_id"),
                entry.get("created_at") or now_iso(),
                now_iso(),
                entry.get("finished_at"),
            ),
        )
        return task_id

    async def tasks(self, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT * FROM dp_task"
        args: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        rows = await self.db.query(sql, args)
        for row in rows:
            row["payload_obj"] = loads(row.get("payload"), {})
            row["result_obj"] = loads(row.get("result"), {})
        return rows

    async def task(self, task_id: str) -> dict[str, Any] | None:
        row = await self.db.query_one("SELECT * FROM dp_task WHERE id = ?", (task_id,))
        if row:
            row["payload_obj"] = loads(row.get("payload"), {})
            row["result_obj"] = loads(row.get("result"), {})
        return row

    async def task_counts(self) -> dict[str, int]:
        rows = await self.db.query("SELECT status, COUNT(*) AS n FROM dp_task GROUP BY status")
        return {row["status"]: int(row["n"]) for row in rows}

    async def update_task(self, task_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        sets = ", ".join(f"{key} = ?" for key in fields)
        await self.db.execute(
            f"UPDATE dp_task SET {sets} WHERE id = ?", (*fields.values(), task_id)
        )

    async def delete_task(self, task_id: str) -> bool:
        """删一条任务。返回是否真的删掉了（用于区分"不存在"和"删了"）。"""
        row = await self.db.query_one("SELECT id FROM dp_task WHERE id = ?", (task_id,))
        if not row:
            return False
        await self.db.execute("DELETE FROM dp_task WHERE id = ?", (task_id,))
        return True

    async def purge_tasks(self, statuses: list[str]) -> int:
        """按状态批量清账。调用方保证只传终态 —— 跑着的任务不是"垃圾"。"""
        if not statuses:
            return 0
        marks = ", ".join("?" for _ in statuses)
        row = await self.db.query_one(
            f"SELECT COUNT(*) AS n FROM dp_task WHERE status IN ({marks})", tuple(statuses)
        )
        await self.db.execute(f"DELETE FROM dp_task WHERE status IN ({marks})", tuple(statuses))
        return int((row or {}).get("n", 0))

    # 设置 --------------------------------------------------------
    async def settings(self) -> dict[str, str]:
        rows = await self.db.query("SELECT k, v FROM dp_setting")
        return {row["k"]: row["v"] for row in rows}

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.upsert("dp_setting", keys={"k": key}, data={"v": value})

    # 概览 --------------------------------------------------------
    async def overview(self) -> dict[str, Any]:
        models = await self.db.query_one(
            "SELECT COUNT(*) AS total, SUM(enabled) AS enabled, SUM(verified) AS verified "
            "FROM dp_model"
        )
        creds = await self.db.query_one("SELECT COUNT(*) AS total FROM dp_credential")
        users = await self.db.query_one("SELECT COUNT(*) AS total FROM dp_user")
        categories = await self.db.query(
            "SELECT capability, COUNT(*) AS total, SUM(enabled) AS enabled "
            "FROM dp_model GROUP BY capability"
        )
        return {
            "models": models or {},
            "credentials": creds or {},
            "users": users or {},
            "categories": categories,
        }


# ── seed ───────────────────────────────────────────────────────────


async def seed(repo: Repo, *, force: bool = False) -> dict[str, Any]:
    """把目录灌进库。**默认不覆盖已有记录**（用户的改动优先于目录）。"""
    result = {"providers": 0, "models": {"inserted": 0, "updated": 0, "skipped": 0}}
    for row in PROVIDERS:
        if await repo.upsert_provider(row, force=force):
            result["providers"] += 1
    seen: set[tuple[str, str, str]] = set()
    for spec in MODELS:
        identity = (spec.provider, spec.key, spec.capability)
        if identity in seen:
            msg = f"目录里出现重复模型 key：{spec.provider}:{spec.key}（{spec.capability}）"
            raise ValueError(msg)
        seen.add(identity)
        outcome = await repo.upsert_model(spec, force=force)
        result["models"][outcome] += 1
    if not await repo.user_by_name("admin"):
        await repo.create_user("admin", "divinepen", display_name="管理员", role="admin")
        result["admin_created"] = True
    defaults = await repo.settings()
    for key, value in (("connectivity_model", "qwen3.7-plus"), ("cost_alert_threshold", "50")):
        if key not in defaults:
            await repo.set_setting(key, value)
    return result


def model_specs(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return list(rows)
