"""訂閱者身分與登入 session 的本機持久化。規格見「設計修正」筆記——訂閱者透過
Discord OAuth／Telegram 深連結登入後，身分與訂閱清單一律留在該安裝實例本機
`bahaad.db`，中繼只負責驗證身分、不保存訂閱內容。

`subscriber_sessions` 是獨立於擁有者 `web_auth`／Flask `session` 的第二套身分系統：
opaque token（隨機值），資料庫只存雜湊＋`compare_digest` 比對，比照 `bahaad_token`／
`VerifyCodeLockout` 的既有模式。**刻意不跟擁有者的 Flask `session`／`app.secret_key`
共用**——那把金鑰每次啟動都重新隨機產生（見 `bahaad/web/__init__.py`），訂閱者的
登入不該被擁有者重啟連坐登出。
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from typing import Any

from bahaad.store.database import Database
from bahaad.subscriber.categories import CATEGORIES as SUBSCRIBER_TEMPLATE_CATEGORIES
from bahaad.subscriber.categories import DEFAULT_TEMPLATES as SUBSCRIBER_DEFAULT_TEMPLATES

_TABLE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS subscriber_identities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        channel TEXT NOT NULL,
        external_id TEXT NOT NULL,
        display_name TEXT,
        created_at TEXT DEFAULT (datetime('now', 'localtime')),
        last_login_at TEXT DEFAULT (datetime('now', 'localtime')),
        UNIQUE (channel, external_id)
    )
    """,
    # 帳號連結（使用者 2026-09-16：同一個人的 Discord／Telegram 帳號可以連結成
    # 「同一個人」，訂閱清單互通）。`linked_to` 非 NULL＝這個身分已經併入
    # `linked_to` 指的那個「主要」身分底下——併入之後，訂閱清單／通知範本狀態
    # 一律只保留在主要身分那邊（見 `link_identity()`），這個身分自己的
    # `subscriber_follows` 列會被清空，之後這個身分登入一律被當成主要身分。
    # `notify_channels` 只有「主要」身分（沒有被併入別人）才有意義：逗號分隔的
    # 已啟用通知管道清單（例如 "discord,telegram"），NULL 表示「只有自己這個
    # 管道」（還沒連結過任何帳號時的預設狀態，不用另外補值）。
    """
    CREATE TABLE IF NOT EXISTS subscriber_sessions (
        token_hash TEXT PRIMARY KEY,
        subscriber_identity_id INTEGER NOT NULL,
        created_at REAL NOT NULL,
        expires_at REAL NOT NULL
    )
    """,
    # 訂閱者選擇要追蹤的番劇——sn 一律正規化成該番劇的首集 sn（比照擁有者訂閱系統的
    # 慣例，見 browse.py 的 `_canonical_subscription_sn`），不然同一部番劇會因為從
    # 不同集數頁面訂閱而產生好幾筆紀錄。
    """
    CREATE TABLE IF NOT EXISTS subscriber_follows (
        subscriber_identity_id INTEGER NOT NULL,
        sn INTEGER NOT NULL,
        title TEXT,
        followed_at TEXT DEFAULT (datetime('now', 'localtime')),
        PRIMARY KEY (subscriber_identity_id, sn)
    )
    """,
    # 訂閱通知的發送歷史——跟擁有者的 notify_history（store/notify.py）是完全分開
    # 的兩張表／兩套統計，見「真正的推播發送」筆記。
    """
    CREATE TABLE IF NOT EXISTS subscriber_notify_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subscriber_identity_id INTEGER NOT NULL,
        sn INTEGER,
        message TEXT NOT NULL,
        success INTEGER NOT NULL,
        error TEXT,
        sent_at TEXT DEFAULT (datetime('now', 'localtime'))
    )
    """,
    # 訂閱通知範本——跟擁有者的 notify_templates（store/notify.py）同樣的表結構／
    # 慣例，但完全獨立的一張表：類別清單、預設範本、口吻都不同（見
    # subscriber/categories.py 開頭說明），不能共用同一張表。
    """
    CREATE TABLE IF NOT EXISTS subscriber_notify_templates (
        category TEXT PRIMARY KEY,
        template TEXT NOT NULL,
        is_default INTEGER NOT NULL DEFAULT 1
    )
    """,
    # 每日新番通知（個人化版）的日期去重——每個訂閱者一天只送一次，見
    # subscriber/dispatch.py 的 notify_daily_digest()。
    """
    CREATE TABLE IF NOT EXISTS subscriber_digest_state (
        subscriber_identity_id INTEGER PRIMARY KEY,
        last_date TEXT NOT NULL
    )
    """,
]

_SESSION_TTL_SECONDS = 30 * 86400  # 30 天

_IDENTITY_ADDED_COLUMNS = {
    "linked_to": "INTEGER",
    "notify_channels": "TEXT",
}

# 閒置自動登出（使用者 2026-09-17：第三方登入也該跟擁有者登入一樣，閒置過久要
# 自動登出，不是只有 30 天的固定到期時間）。`last_seen` 沒有預設值——既有安裝
# 升級後舊的 session 列這欄是 NULL，`resolve_session()` 讀到 NULL 時退回
# `created_at` 當作最後一次活動時間，不需要另外跑一次性 backfill。
_SESSION_ADDED_COLUMNS = {
    "last_seen": "REAL",
}


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class SubscribersStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            for statement in _TABLE_STATEMENTS:
                conn.execute(statement)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(subscriber_identities)")}
            for name, decl in _IDENTITY_ADDED_COLUMNS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE subscriber_identities ADD COLUMN {name} {decl}")
            existing_session_cols = {row["name"] for row in conn.execute("PRAGMA table_info(subscriber_sessions)")}
            for name, decl in _SESSION_ADDED_COLUMNS.items():
                if name not in existing_session_cols:
                    conn.execute(f"ALTER TABLE subscriber_sessions ADD COLUMN {name} {decl}")
            for category in SUBSCRIBER_TEMPLATE_CATEGORIES:
                conn.execute(
                    "INSERT OR IGNORE INTO subscriber_notify_templates (category, template, is_default) "
                    "VALUES (?, ?, 1)",
                    (category, SUBSCRIBER_DEFAULT_TEMPLATES[category]),
                )

    def upsert_identity(self, channel: str, external_id: str, display_name: str | None) -> int:
        """登入成功時呼叫——第一次見到這個 (channel, external_id) 就建立新身分，
        之後每次登入更新顯示名稱／最後登入時間，回傳 `subscriber_identities.id`。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO subscriber_identities (channel, external_id, display_name) "
                    "VALUES (?, ?, ?) "
                    "ON CONFLICT(channel, external_id) DO UPDATE SET "
                    "display_name = excluded.display_name, "
                    "last_login_at = datetime('now','localtime')",
                    (channel, external_id, display_name),
                )
                row = conn.execute(
                    "SELECT id FROM subscriber_identities WHERE channel = ? AND external_id = ?",
                    (channel, external_id),
                ).fetchone()
        return row["id"]

    def get_identity(self, identity_id: int) -> dict[str, Any] | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT id, channel, external_id, display_name, linked_to, notify_channels "
                "FROM subscriber_identities WHERE id = ?",
                (identity_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def resolve_primary_identity(self, identity_id: int) -> dict[str, Any] | None:
        """把「可能已經被連結併入別的帳號」的身分，解析成實際生效的「主要」身分——
        併入之後訂閱清單／通知偏好一律只存在主要身分底下，任何要讀寫訂閱資料的
        地方都該先過這支，不要直接拿登入當下查到的身分 id。串接深度目前最多 1
        層（只有兩個管道可以連），但還是照鏈走以防萬一，並防呆擋掉自我循環。"""
        seen: set[int] = set()
        current = self.get_identity(identity_id)
        while current is not None and current.get("linked_to") is not None and current["id"] not in seen:
            seen.add(current["id"])
            nxt = self.get_identity(current["linked_to"])
            if nxt is None:
                break
            current = nxt
        return current

    def create_session(self, identity_id: int, ttl_seconds: float = _SESSION_TTL_SECONDS) -> str:
        """回傳明碼 token（只有這次回傳，資料庫只存雜湊）——比照
        `verify_code.py`／`bahaad_token` 的既有原則。"""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._database.transaction() as conn:
            conn.execute(
                "INSERT INTO subscriber_sessions "
                "(token_hash, subscriber_identity_id, created_at, expires_at, last_seen) "
                "VALUES (?, ?, ?, ?, ?)",
                (_hash_token(token), identity_id, now, now + ttl_seconds, now),
            )
        return token

    def resolve_session(self, token: str, idle_timeout_minutes: float | None = None) -> dict[str, Any] | None:
        """回傳對應的訂閱者身分——**已經解析過帳號連結**，`channel`／`display_name`
        等欄位是一律是主要身分（見 `resolve_primary_identity()`）。這樣不管訂閱者
        是從哪個當初登入時的管道重新整理頁面，看到的都是連結後同一份訂閱清單／
        通知偏好，不用另外處理「session 指到已經被併入別人的舊身分」這種情況。
        額外附上 `login_channel`／`login_display_name`／`login_external_id`——這次
        登入 session 實際綁的那個管道身分（可能跟上面解析出來的主要身分不同管道），
        給畫面顯示「歡迎回來」用（使用者 2026-09-17 回報：連結 Discord/Telegram
        後用 Telegram 登入，畫面卻顯示 Discord 的名稱——根因是原本只回傳解析過的
        主要身分，這次登入實際用哪個管道的資訊就這樣遺失了）。

        `idle_timeout_minutes`：跟擁有者登入一樣的閒置自動登出（使用者 2026-09-17：
        第三方登入不該只有 30 天固定到期，也要有跟管理者一樣的閒置逾時）。
        `None`／`<= 0` 表示不檢查閒置，只看 `expires_at` 這個固定到期時間。
        `last_seen` 由 `touch_session()` 更新，呼叫端（`web/subscriber_shared.py`
        的 `current_subscriber()`）只在「算作使用者活動」的請求才會呼叫，判斷方式
        跟 `web/__init__.py._request_is_user_activity()` 同一套邏輯。閒置逾時的
        session 直接刪除，效果等同於「已登出」——所有呼叫端本來就把 `resolve_session
        回 None` 當成未登入處理，不需要額外的登出導轉邏輯。

        token 不存在／過期／閒置逾時回 `None`。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT subscriber_identity_id, created_at, expires_at, last_seen "
                "FROM subscriber_sessions WHERE token_hash = ?",
                (_hash_token(token),),
            ).fetchone()
        if row is None or row["expires_at"] < time.time():
            return None
        if idle_timeout_minutes and idle_timeout_minutes > 0:
            last_seen = row["last_seen"] if row["last_seen"] is not None else row["created_at"]
            if time.time() - last_seen > idle_timeout_minutes * 60:
                self.delete_session(token)
                return None
        login_identity = self.get_identity(row["subscriber_identity_id"])
        primary = self.resolve_primary_identity(row["subscriber_identity_id"])
        if primary is None:
            return None
        result = dict(primary)
        if login_identity is not None:
            result["login_channel"] = login_identity["channel"]
            result["login_display_name"] = login_identity.get("display_name")
            result["login_external_id"] = login_identity.get("external_id")
        return result

    def touch_session(self, token: str) -> None:
        """更新這個 session 的最後活動時間——給閒置自動登出的計時用（見
        `resolve_session()` 的 `idle_timeout_minutes` 說明）。token 不存在就悄悄
        不做事，不報錯。"""
        with self._database.transaction() as conn:
            conn.execute(
                "UPDATE subscriber_sessions SET last_seen = ? WHERE token_hash = ?",
                (time.time(), _hash_token(token)),
            )

    def delete_session(self, token: str) -> None:
        with self._database.transaction() as conn:
            conn.execute("DELETE FROM subscriber_sessions WHERE token_hash = ?", (_hash_token(token),))

    def prune_expired_sessions(self) -> None:
        with self._database.transaction() as conn:
            conn.execute("DELETE FROM subscriber_sessions WHERE expires_at < ?", (time.time(),))

    # --- 帳號連結（使用者 2026-09-16：Discord／Telegram 帳號連結成同一個人）---

    def get_notify_channels(self, primary_id: int) -> set[str]:
        """這個主要身分目前啟用通知的管道集合。還沒連結過任何帳號（`notify_
        channels` 是 NULL）就只有自己這個管道——不用另外補值，省得每個既有身分
        都要跑一次資料庫寫入。"""
        identity = self.get_identity(primary_id)
        if identity is None:
            return set()
        raw = identity.get("notify_channels")
        if not raw:
            return {identity["channel"]}
        return {c for c in raw.split(",") if c}

    def set_notify_channels(self, primary_id: int, channels: set[str]) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE subscriber_identities SET notify_channels = ? WHERE id = ?",
                    (",".join(sorted(channels)), primary_id),
                )

    def linked_identities_for(self, primary_id: int) -> list[dict[str, Any]]:
        """這個主要身分自己＋所有併入它底下的身分（目前最多再多一個，因為只有
        Discord／Telegram 兩個管道）。推播發送時要用這份清單，依 `notify_
        channels` 篩出真正要送的管道。"""
        primary = self.get_identity(primary_id)
        if primary is None:
            return []
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT id, channel, external_id, display_name FROM subscriber_identities "
                "WHERE linked_to = ?",
                (primary_id,),
            ).fetchall()
        return [primary] + [dict(row) for row in rows]

    def link_identity(self, primary_id: int, other_id: int, *, keep: str) -> None:
        """把 `other_id` 併入 `primary_id` 底下，變成同一個人的兩個登入管道。
        `keep`：`"merge"`（兩邊訂閱清單取聯集，使用者 2026-09-16 選定的預設做法）／
        `"primary"`（保留 `primary_id` 原本的清單，丟棄 `other_id` 的）／
        `"other"`（保留 `other_id` 原本的清單，`primary_id` 原本的清單被取代）。
        連結完成後兩個管道預設都開啟通知（`notify_channels` 設成兩者的聯集），
        訂閱者之後可以在「我的訂閱」自己調整。"""
        if primary_id == other_id:
            return
        primary = self.get_identity(primary_id)
        other = self.get_identity(other_id)
        if primary is None or other is None:
            return
        if keep == "primary":
            self.remove_all_follows(other_id)
        elif keep == "other":
            self.remove_all_follows(primary_id)
            for follow in self.follows_for(other_id):
                self.add_follow(primary_id, follow["sn"], follow["title"])
            self.remove_all_follows(other_id)
        else:  # "merge"（含未知值時的安全預設）
            for follow in self.follows_for(other_id):
                self.add_follow(primary_id, follow["sn"], follow["title"])
            self.remove_all_follows(other_id)

        channels = self.get_notify_channels(primary_id) | {other["channel"]}
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE subscriber_identities SET linked_to = ?, notify_channels = NULL WHERE id = ?",
                    (primary_id, other_id),
                )
                conn.execute(
                    "UPDATE subscriber_identities SET notify_channels = ? WHERE id = ?",
                    (",".join(sorted(channels)), primary_id),
                )

    # --- subscriber_follows：訂閱者選擇要追蹤的番劇 ---

    def add_follow(self, identity_id: int, sn: int, title: str | None) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO subscriber_follows (subscriber_identity_id, sn, title, followed_at) "
                    "VALUES (?, ?, ?, datetime('now','localtime')) "
                    "ON CONFLICT(subscriber_identity_id, sn) DO UPDATE SET "
                    "title = COALESCE(excluded.title, subscriber_follows.title)",
                    (identity_id, sn, title),
                )

    def remove_follow(self, identity_id: int, sn: int) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "DELETE FROM subscriber_follows WHERE subscriber_identity_id = ? AND sn = ?",
                    (identity_id, sn),
                )

    def remove_all_follows(self, identity_id: int) -> None:
        """帳號連結（`link_identity()`）用——併入別人底下的身分自己不再保留一份
        訂閱清單，一律只看主要身分那邊，避免兩邊各自一份資料之後對不起來。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "DELETE FROM subscriber_follows WHERE subscriber_identity_id = ?", (identity_id,)
                )

    def follows_for(self, identity_id: int) -> list[dict[str, Any]]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT sn, title, followed_at FROM subscriber_follows "
                "WHERE subscriber_identity_id = ? ORDER BY followed_at DESC",
                (identity_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def identities_following(self, sn: int) -> list[dict[str, Any]]:
        """推播發送用——找出所有追蹤這個（已正規化的）sn 的訂閱者身分。"""
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT i.id, i.channel, i.external_id, i.display_name "
                "FROM subscriber_follows f JOIN subscriber_identities i "
                "ON i.id = f.subscriber_identity_id WHERE f.sn = ?",
                (sn,),
            ).fetchall()
        return [dict(row) for row in rows]

    def all_followed_sns(self) -> set[int]:
        """即時統計全量重算用（`stats.collector.resync_all_subscriptions`）——所有
        還有至少一位訂閱者身分在追蹤的 sn，不分身分、不分頻道。"""
        with self._database.transaction() as conn:
            rows = conn.execute("SELECT DISTINCT sn FROM subscriber_follows").fetchall()
        return {row["sn"] for row in rows}

    def all_identities_with_follows(self) -> list[dict[str, Any]]:
        """每日新番通知（個人化版）用——所有至少追蹤一部番劇的訂閱者身分，
        附上他追蹤的 sn 集合（`"sns"` 鍵），見 subscriber/dispatch.py 的
        `notify_daily_digest()`。"""
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT i.id, i.channel, i.external_id, i.display_name, f.sn "
                "FROM subscriber_follows f JOIN subscriber_identities i "
                "ON i.id = f.subscriber_identity_id"
            ).fetchall()
        by_identity: dict[int, dict[str, Any]] = {}
        for row in rows:
            identity = by_identity.setdefault(
                row["id"],
                {
                    "id": row["id"],
                    "channel": row["channel"],
                    "external_id": row["external_id"],
                    "display_name": row["display_name"],
                    "sns": set(),
                },
            )
            identity["sns"].add(row["sn"])
        return list(by_identity.values())

    # --- subscriber_notify_history：訂閱通知的發送歷史 ---

    def log_notify_history(
        self, identity_id: int, sn: int | None, message: str, success: bool, error: str | None = None
    ) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO subscriber_notify_history "
                    "(subscriber_identity_id, sn, message, success, error) VALUES (?, ?, ?, ?, ?)",
                    (identity_id, sn, message, int(success), error),
                )

    def has_successful_notify(self, identity_id: int) -> bool:
        """這個（單一管道的）身分 id 有沒有至少成功送出過一次通知——用來判斷
        「Telegram 還沒 /start 過 Bot」這類一次性提醒還要不要繼續顯示（使用者
        2026-09-21）。傳的是 `linked_identities_for()` 展開後、單一管道自己的
        id（不是連結後的「主要」身分 id）——`log_notify_history()` 寫的就是
        實際送信那個管道身分自己的 id，兩邊要對得上，不然 Telegram 成功過也會
        因為查到的是主要（可能是 Discord）身分的 id 而查不到。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM subscriber_notify_history WHERE subscriber_identity_id = ? "
                "AND success = 1 LIMIT 1",
                (identity_id,),
            ).fetchone()
        return row is not None

    def count_notify_history(self) -> int:
        with self._database.transaction() as conn:
            return conn.execute("SELECT COUNT(*) FROM subscriber_notify_history").fetchone()[0]

    def get_notify_history(self, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT h.id, h.sn, h.message, h.success, h.error, h.sent_at, "
                "i.channel, i.external_id, i.display_name "
                "FROM subscriber_notify_history h "
                "JOIN subscriber_identities i ON i.id = h.subscriber_identity_id "
                "ORDER BY h.id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "sn": row["sn"],
                "message": row["message"],
                "success": bool(row["success"]),
                "error": row["error"],
                "sent_at": row["sent_at"],
                "channel": row["channel"],
                "external_id": row["external_id"],
                "display_name": row["display_name"],
            }
            for row in rows
        ]

    def clear_notify_history(self) -> int:
        with self._database.transaction() as conn:
            cur = conn.execute("DELETE FROM subscriber_notify_history")
            return cur.rowcount

    # --- subscriber_notify_templates：訂閱通知範本（比照 store/notify.py 的
    # notify_templates 方法簽名，但完全獨立的一張表） ---

    def get_notify_template(self, category: str) -> str:
        if category not in SUBSCRIBER_TEMPLATE_CATEGORIES:
            raise ValueError(f"{category} 不是訂閱通知的類別")
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT template FROM subscriber_notify_templates WHERE category = ?", (category,)
            ).fetchone()
        return row["template"] if row is not None else SUBSCRIBER_DEFAULT_TEMPLATES[category]

    def get_all_notify_templates(self) -> dict[str, dict[str, Any]]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT category, template, is_default FROM subscriber_notify_templates"
            ).fetchall()
        return {row["category"]: {"template": row["template"], "is_default": bool(row["is_default"])} for row in rows}

    def set_notify_template(self, category: str, text: str) -> None:
        if category not in SUBSCRIBER_TEMPLATE_CATEGORIES:
            raise ValueError(f"{category} 不是訂閱通知的類別")
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE subscriber_notify_templates SET template = ?, is_default = 0 WHERE category = ?",
                    (text, category),
                )

    def reset_notify_template(self, category: str) -> str:
        if category not in SUBSCRIBER_TEMPLATE_CATEGORIES:
            raise ValueError(f"{category} 不是訂閱通知的類別")
        default_text = SUBSCRIBER_DEFAULT_TEMPLATES[category]
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE subscriber_notify_templates SET template = ?, is_default = 1 WHERE category = ?",
                    (default_text, category),
                )
        return default_text

    # --- subscriber_digest_state：每日新番通知（個人化版）的日期去重 ---

    def get_last_digest_date(self, identity_id: int) -> str | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT last_date FROM subscriber_digest_state WHERE subscriber_identity_id = ?",
                (identity_id,),
            ).fetchone()
        return row["last_date"] if row is not None else None

    def set_last_digest_date(self, identity_id: int, date_str: str) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO subscriber_digest_state (subscriber_identity_id, last_date) VALUES (?, ?) "
                    "ON CONFLICT(subscriber_identity_id) DO UPDATE SET last_date = excluded.last_date",
                    (identity_id, date_str),
                )
