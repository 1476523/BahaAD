"""本機端「訂閱者中繼綁定」狀態的持久化。規格見 docs/requirements/subscriber_notify.md
（Phase 1：中繼綁定基礎建設）。

存的是跟 `access_gate_server` 換到的 installation_id ＋共用密鑰——跟 `store/access_gate.py`
的 `bahaad_token` 是類似性質的不透明憑證，同樣只用 DPAPI 一層加密（見 `bahaad/dpapi.py`），
不需要 `vault.py` 那種可選 PIN 分層：外洩風險遠低於動畫瘋帳密，且中繼隨時能讓一組密鑰
失效重發，不值得為了它多一層 PIN 摩擦使用者體驗。
"""

from __future__ import annotations

import threading
from typing import Any

from bahaad import dpapi
from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS subscriber_relay (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    installation_id TEXT NOT NULL,
    secret_enc BLOB NOT NULL,
    registered_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""


class SubscriberRelayStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)

    def get_status(self) -> dict[str, Any]:
        """絕不含共用密鑰本身——比照 `access_gate.py`／`vault.py` 的 `get_status()`
        同樣的理由，避免任何呼叫端不小心把這個結果直接回傳給網頁前端而外洩憑證。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT installation_id, registered_at FROM subscriber_relay WHERE id = 1"
            ).fetchone()
        if row is None:
            return {"registered": False, "installation_id": None, "registered_at": None}
        return {
            "registered": True,
            "installation_id": row["installation_id"],
            "registered_at": row["registered_at"],
        }

    def save_registration(self, installation_id: str, secret: str) -> None:
        if not installation_id or not secret:
            raise ValueError("installation_id 與 secret 為必填")
        secret_enc = dpapi.protect(secret.encode("utf-8"))
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO subscriber_relay (id, installation_id, secret_enc) "
                    "VALUES (1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET installation_id=excluded.installation_id, "
                    "secret_enc=excluded.secret_enc, registered_at=datetime('now','localtime')",
                    (installation_id, secret_enc),
                )

    def get_credentials(self) -> tuple[str, str] | None:
        """回傳 `(installation_id, secret)`，供 `subscriber/relay_client.py` 簽章
        請求前的極短暫記憶體使用；尚未註冊回 `None`。不能把回傳值放進任何會回給
        前端的回應或 log，比照 `vault.py` 的 `unlock_credentials()` 使用限制。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT installation_id, secret_enc FROM subscriber_relay WHERE id = 1"
            ).fetchone()
        if row is None:
            return None
        return row["installation_id"], dpapi.unprotect(row["secret_enc"]).decode("utf-8")

    def clear(self) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute("DELETE FROM subscriber_relay WHERE id = 1")
