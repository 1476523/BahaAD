"""網頁登入帳密＋session 簽章密鑰。規格見 docs/requirements/web_auth.md。

密碼用不可逆 scrypt 雜湊，不是 vault.py 的可逆 DPAPI 加密——網頁登入密碼從來不需要
還原成明文，只需要驗證「輸入的密碼對不對」，所以用業界標準的不可逆雜湊，即使資料庫
外洩密碼本身也還原不出來。KDF 參數沿用 vault.py 已經跑過官方向量驗證的同一組設定，
不是重新設計一套；這不是「參考舊專案 aniGamerPlus」的例外——查證過舊專案
`Dashboard/Server.py` 的 dashboard 密碼其實是明文存在 config.json，完全沒有加密，
這裡刻意不照抄那個做法。
"""

from __future__ import annotations

import hmac
import os
import threading
from dataclasses import dataclass

from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS web_auth (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    username TEXT NOT NULL,
    password_hash BLOB NOT NULL,
    password_salt BLOB NOT NULL,
    session_secret_key BLOB NOT NULL,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN = 2**14, 8, 1, 32
_PASSWORD_SALT_LEN = 16
_SESSION_SECRET_KEY_LEN = 32


class WebAuthError(Exception):
    pass


class AlreadyConfiguredError(WebAuthError):
    pass


@dataclass(frozen=True)
class WebAuthStatus:
    configured: bool
    username: str | None = None


def _hash_password(password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=_SCRYPT_DKLEN, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return kdf.derive(password.encode("utf-8"))


class WebAuthStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)

    def get_status(self) -> WebAuthStatus:
        with self._database.transaction() as conn:
            row = conn.execute("SELECT username FROM web_auth WHERE id = 1").fetchone()
        if row is None:
            return WebAuthStatus(configured=False)
        return WebAuthStatus(configured=True, username=row["username"])

    def setup(self, username: str, password: str) -> bytes:
        """首次建立帳密，回傳 session_secret_key（Flask app.secret_key 用）。已經設定過
        的話丟 AlreadyConfiguredError，不能悄悄覆蓋既有帳密——覆蓋是「重置流程」的責任
        （見 web_auth.md），不是這個方法該做的事。"""
        if not username or not password:
            raise ValueError("帳號與密碼為必填")
        with self._lock:
            with self._database.transaction() as conn:
                existing = conn.execute("SELECT 1 FROM web_auth WHERE id = 1").fetchone()
                if existing is not None:
                    raise AlreadyConfiguredError("已經設定過帳號密碼，不能重複設定")

                salt = os.urandom(_PASSWORD_SALT_LEN)
                password_hash = _hash_password(password, salt)
                session_secret_key = os.urandom(_SESSION_SECRET_KEY_LEN)
                conn.execute(
                    "INSERT INTO web_auth (id, username, password_hash, password_salt, "
                    "session_secret_key) VALUES (1, ?, ?, ?, ?)",
                    (username, password_hash, salt, session_secret_key),
                )
        return session_secret_key

    def change_credentials(
        self,
        current_password: str,
        *,
        new_username: str | None = None,
        new_password: str | None = None,
    ) -> bytes:
        """改帳號／改密碼。**一定要先驗證 `current_password`**（對現有的密碼雜湊）才放行——
        這是改帳密的唯一入口，不接受「只給新值、不驗證舊密碼」。`new_username` / `new_password`
        任一為 `None` 代表該項不改；兩者可同時給。

        一律輪替 `session_secret_key` 並回傳新值——改帳密後所有既有 session（含目前操作
        的這個）全部失效、強制重新登入，避免舊 session 在未重新登入的情況下繼續能用。
        呼叫端要把回傳值設進 `current_app.secret_key`。
        """
        if not current_password:
            raise WebAuthError("需要輸入目前的密碼")
        if new_username is None and new_password is None:
            raise WebAuthError("沒有要變更的項目")

        with self._lock:
            with self._database.transaction() as conn:
                row = conn.execute(
                    "SELECT username, password_hash, password_salt FROM web_auth WHERE id = 1"
                ).fetchone()
                if row is None:
                    raise WebAuthError("尚未設定帳號密碼")

                candidate = _hash_password(current_password, row["password_salt"])
                if not hmac.compare_digest(candidate, row["password_hash"]):
                    raise WebAuthError("目前的密碼不正確")

                columns: list[str] = []
                params: list[object] = []
                if new_username is not None:
                    cleaned = new_username.strip()
                    if not cleaned:
                        raise WebAuthError("新的帳號名稱不能空白")
                    if cleaned == row["username"]:
                        raise WebAuthError("新的帳號名稱跟目前的一樣，沒有變更")
                    columns.append("username = ?")
                    params.append(cleaned)
                if new_password is not None:
                    if not new_password:
                        raise WebAuthError("新密碼不能空白")
                    if hmac.compare_digest(
                        _hash_password(new_password, row["password_salt"]), row["password_hash"]
                    ):
                        raise WebAuthError("新密碼跟目前的一樣，沒有變更")
                    new_salt = os.urandom(_PASSWORD_SALT_LEN)
                    columns.append("password_salt = ?")
                    params.append(new_salt)
                    columns.append("password_hash = ?")
                    params.append(_hash_password(new_password, new_salt))

                new_secret = os.urandom(_SESSION_SECRET_KEY_LEN)
                columns.append("session_secret_key = ?")
                params.append(new_secret)

                conn.execute(
                    f"UPDATE web_auth SET {', '.join(columns)} WHERE id = 1", tuple(params)
                )
        return new_secret

    def verify_password(self, username: str, password: str) -> bool:
        """刻意不區分「帳號不存在」跟「密碼錯誤」——兩種情況都回傳 False，且都會實際
        跑一次 scrypt 雜湊運算（即使帳號打錯也一樣），避免回應時間差洩漏帳號是否存在。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT username, password_hash, password_salt FROM web_auth WHERE id = 1"
            ).fetchone()
        if row is None:
            _hash_password(password, os.urandom(_PASSWORD_SALT_LEN))  # 維持一致的運算成本
            return False

        username_matches = hmac.compare_digest(
            row["username"].encode("utf-8"), username.encode("utf-8")
        )
        candidate_hash = _hash_password(password, row["password_salt"])
        password_matches = hmac.compare_digest(candidate_hash, row["password_hash"])
        return username_matches and password_matches

    def clear(self) -> None:
        """把帳密整列刪掉——「忘記密碼」重設用（使用者 2026-09-05）。刪掉後
        `get_status().configured` 是 False，`before_request` 會把使用者導到首次設定頁
        重設一組新密碼。訂閱清單／一般設定／快取都不動（那些不是用登入密碼加密的）。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute("DELETE FROM web_auth WHERE id = 1")

    def get_session_secret_key(self) -> bytes | None:
        with self._database.transaction() as conn:
            row = conn.execute("SELECT session_secret_key FROM web_auth WHERE id = 1").fetchone()
        return row["session_secret_key"] if row is not None else None
