"""通知憑證／範本／發送歷史的持久化。規格見 docs/requirements/notify.md「資料模型」
一節。

跟 `store/access_gate.py` 同樣的 DPAPI 加密慣例（`bahaad/dpapi.py`）——Telegram Bot
Token／Discord Webhook URL 外洩風險遠低於動畫瘋帳密（vault.py 那種可逆＋可選 PIN
分層），且使用者都能自己在 Telegram/Discord 後台重新產生撤銷舊的，不需要 vault.py
的分層設計，只用 DPAPI 這一層就夠。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from bahaad import dpapi
from bahaad.notify.categories import CUSTOMIZABLE_CATEGORIES, DEFAULT_TEMPLATES
from bahaad.store.database import Database

_TABLE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS notify_credentials (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        telegram_bot_token_enc BLOB,
        telegram_chat_id TEXT,
        discord_webhook_url_enc BLOB
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notify_templates (
        category TEXT PRIMARY KEY,
        template TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 1
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notify_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL,
        category TEXT NOT NULL,
        message TEXT NOT NULL,
        success INTEGER NOT NULL,
        error TEXT,
        sent_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    )
    """,
]


@dataclass(frozen=True)
class NotifyCredentials:
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    discord_webhook_url: str | None


class NotifyStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            for statement in _TABLE_STATEMENTS:
                conn.execute(statement)
            for category in CUSTOMIZABLE_CATEGORIES:
                conn.execute(
                    "INSERT OR IGNORE INTO notify_templates (category, template, is_default) VALUES (?, ?, 1)",
                    (category, DEFAULT_TEMPLATES[category]),
                )

    # --- notify_credentials ---

    def get_credentials(self) -> NotifyCredentials:
        """絕不外洩加密後的原始 blob——回傳前一律先解密。呼叫端（`notify/dispatch.py`）
        只在準備發請求前極短暫使用，不能把回傳值放進任何會回給前端的回應或 log，
        比照 `store/access_gate.py` 的 `get_token()` 同樣的使用限制。"""
        with self._database.transaction() as conn:
            row = conn.execute("SELECT * FROM notify_credentials WHERE id = 1").fetchone()
        if row is None:
            return NotifyCredentials(None, None, None)
        token = dpapi.unprotect(row["telegram_bot_token_enc"]).decode("utf-8") if row["telegram_bot_token_enc"] else None
        webhook = dpapi.unprotect(row["discord_webhook_url_enc"]).decode("utf-8") if row["discord_webhook_url_enc"] else None
        return NotifyCredentials(token, row["telegram_chat_id"], webhook)

    def set_telegram_credentials(self, bot_token: str, chat_id: str) -> None:
        if not bot_token or not chat_id:
            raise ValueError("bot_token 與 chat_id 為必填")
        token_enc = dpapi.protect(bot_token.encode("utf-8"))
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO notify_credentials (id, telegram_bot_token_enc, telegram_chat_id) "
                    "VALUES (1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET telegram_bot_token_enc=excluded.telegram_bot_token_enc, "
                    "telegram_chat_id=excluded.telegram_chat_id",
                    (token_enc, chat_id),
                )

    def clear_telegram_credentials(self) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO notify_credentials (id, telegram_bot_token_enc, telegram_chat_id) "
                    "VALUES (1, NULL, NULL) "
                    "ON CONFLICT(id) DO UPDATE SET telegram_bot_token_enc=NULL, telegram_chat_id=NULL"
                )

    def set_discord_credentials(self, webhook_url: str) -> None:
        if not webhook_url:
            raise ValueError("webhook_url 為必填")
        webhook_enc = dpapi.protect(webhook_url.encode("utf-8"))
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO notify_credentials (id, discord_webhook_url_enc) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET discord_webhook_url_enc=excluded.discord_webhook_url_enc",
                    (webhook_enc,),
                )

    def clear_discord_credentials(self) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO notify_credentials (id, discord_webhook_url_enc) VALUES (1, NULL) "
                    "ON CONFLICT(id) DO UPDATE SET discord_webhook_url_enc=NULL"
                )

    # --- notify_templates ---

    def get_template(self, category: str) -> str:
        if category not in CUSTOMIZABLE_CATEGORIES:
            raise ValueError(f"{category} 不是可自訂範本的類別")
        with self._database.transaction() as conn:
            row = conn.execute("SELECT template FROM notify_templates WHERE category = ?", (category,)).fetchone()
        return row["template"] if row is not None else DEFAULT_TEMPLATES[category]

    def get_all_templates(self) -> dict[str, dict]:
        with self._database.transaction() as conn:
            rows = conn.execute("SELECT category, template, is_default FROM notify_templates").fetchall()
        return {row["category"]: {"template": row["template"], "is_default": bool(row["is_default"])} for row in rows}

    def set_template(self, category: str, text: str) -> None:
        if category not in CUSTOMIZABLE_CATEGORIES:
            raise ValueError(f"{category} 不是可自訂範本的類別")
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE notify_templates SET template = ?, is_default = 0 WHERE category = ?", (text, category)
                )

    def reset_template(self, category: str) -> str:
        if category not in CUSTOMIZABLE_CATEGORIES:
            raise ValueError(f"{category} 不是可自訂範本的類別")
        default_text = DEFAULT_TEMPLATES[category]
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE notify_templates SET template = ?, is_default = 1 WHERE category = ?",
                    (default_text, category),
                )
        return default_text

    # --- notify_history ---

    def log_history(self, channel: str, category: str, message: str, success: bool, error: str | None = None) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO notify_history (channel, category, message, success, error) VALUES (?, ?, ?, ?, ?)",
                    (channel, category, message, int(success), error),
                )

    def _history_where(self, channel, category):
        conditions, params = [], []
        if channel:
            conditions.append("channel = ?")
            params.append(channel)
        if category:
            conditions.append("category = ?")
            params.append(category)
        return (" WHERE " + " AND ".join(conditions)) if conditions else "", params

    def count_history(self, channel: str | None = None, category: str | None = None) -> int:
        where, params = self._history_where(channel, category)
        with self._database.transaction() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM notify_history{where}", params).fetchone()[0]

    def get_history(
        self, channel: str | None = None, category: str | None = None,
        limit: int = 200, offset: int = 0,
    ) -> list[dict]:
        where, params = self._history_where(channel, category)
        query = (
            "SELECT id, channel, category, message, success, error, sent_at FROM notify_history"
            f"{where} ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        params = [*params, limit, offset]
        with self._database.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [
            {
                "id": row["id"],
                "channel": row["channel"],
                "category": row["category"],
                "message": row["message"],
                "success": bool(row["success"]),
                "error": row["error"],
                "sent_at": row["sent_at"],
            }
            for row in rows
        ]

    def get_history_row(self, history_id: int) -> dict | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT id, channel, category, message, success, error, sent_at FROM notify_history WHERE id = ?",
                (history_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "channel": row["channel"],
            "category": row["category"],
            "message": row["message"],
            "success": bool(row["success"]),
            "error": row["error"],
            "sent_at": row["sent_at"],
        }

    def clear_history(self) -> int:
        """清空發送歷史，回傳刪了幾筆（使用者 2026-09-05：比照日誌頁的「清空」）。"""
        with self._database.transaction() as conn:
            cur = conn.execute("DELETE FROM notify_history")
            return cur.rowcount
