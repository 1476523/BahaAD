"""GitHub 帳號連結狀態的持久化。規格見 docs/requirements/access_gate.md。

存的是 `access_gate_server` 核發的不透明 `bahaad_token`（不是真正的 GitHub access
token——那個只存在 `access_gate_server`，`main.exe` 從來拿不到，見 access_gate.md
「重要架構決策」一節），跟 `gamer_client/`（動畫瘋登入身份，`store/identity.py`）／
`vault.py`（動畫瘋帳密）是完全不同用途、不同外洩風險等級的身份，不共用任何儲存或
加密邏輯——這裡只用 DPAPI 一層（見 `bahaad/dpapi.py`），不需要 `vault.py` 那種可選
PIN 分層：`bahaad_token` 本來就是可以被伺服器隨時撤銷的不透明憑證，外洩風險遠低於
動畫瘋帳密，不值得為了它多一層 PIN 摩擦使用者體驗。
"""

from __future__ import annotations

import threading
from typing import Any

from bahaad import dpapi
from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS access_gate (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    github_login TEXT NOT NULL,
    bahaad_token_enc BLOB NOT NULL,
    connected_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""


class AccessGateStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)

    def get_status(self) -> dict[str, Any]:
        """絕不含 bahaad_token：不只是設成 None，是根本不放進 dict——比照 vault.py
        的 get_status() 同樣的理由，避免任何呼叫端不小心把這個結果直接回傳給網頁
        前端而外洩憑證。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT github_login, connected_at FROM access_gate WHERE id = 1"
            ).fetchone()
        if row is None:
            return {"connected": False, "github_login": None, "connected_at": None}
        return {
            "connected": True,
            "github_login": row["github_login"],
            "connected_at": row["connected_at"],
        }

    def save_connection(self, github_login: str, bahaad_token: str) -> None:
        if not github_login or not bahaad_token:
            raise ValueError("github_login 與 bahaad_token 為必填")

        token_enc = dpapi.protect(bahaad_token.encode("utf-8"))
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO access_gate (id, github_login, bahaad_token_enc) "
                    "VALUES (1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET github_login=excluded.github_login, "
                    "bahaad_token_enc=excluded.bahaad_token_enc, "
                    "connected_at=datetime('now','localtime')",
                    (github_login, token_enc),
                )

    def disconnect(self) -> None:
        """只刪本機這一列。不負責通知 access_gate_server 撤銷 token——那是網路呼叫，
        呼叫端（web/access_gate.py）自己先呼叫 access_gate/oauth.py 的 disconnect()
        再呼叫這裡，兩者職責分開；遠端呼叫失敗也要能刪本機列，見 access_gate.md
        邊界案例「登入是可選的，隨時能斷開連結」。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute("DELETE FROM access_gate WHERE id = 1")

    def get_token(self) -> str | None:
        """解密回傳 bahaad_token，只給 access_gate/oauth.py／heartbeat.py 在準備發
        請求前的極短暫記憶體使用，比照 vault.py 的 unlock_credentials() 同樣的使用
        限制——不能把回傳值放進任何會回給前端的回應或 log。"""
        with self._database.transaction() as conn:
            row = conn.execute("SELECT bahaad_token_enc FROM access_gate WHERE id = 1").fetchone()
        if row is None:
            return None
        return dpapi.unprotect(row["bahaad_token_enc"]).decode("utf-8")
