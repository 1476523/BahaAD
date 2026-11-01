"""帳號保險箱：加密儲存動畫瘋帳號密碼（可選：兩步驟驗證用的 TOTP 密鑰）。

規格見 docs/requirements/vault.md。只負責存取與加解密，不碰瀏覽器自動化（那是
gamer_client/cookie_rotation.py 的事）。

加密：明文 -> DPAPI（綁定目前 Windows 使用者帳戶）。**只有 DPAPI 這一層**——
2026-08-27 使用者定案移除原本可選的 PIN 分層：BahaAD 網頁登入密碼已是不可逆雜湊
（web_auth.md）、DPAPI 又綁 Windows 帳戶，再要使用者記一組 PIN 是多餘的、且忘記無法
救援。跟 store/access_gate.py 同樣的「只用 DPAPI」決策。

本專案目前只支援 Windows（見 docs/decisions/0000-architecture-overview.md），DPAPI 一律
視為可用，不需要額外的平台檢查介面。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct
import threading
import time
from typing import Any

from bahaad import dpapi
from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS vault (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    account TEXT NOT NULL,
    password_enc BLOB NOT NULL,
    totp_secret_enc BLOB,
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""

# 從舊版（有 PIN 分層）升級上來的資料庫：把不再使用的 PIN 欄位丟掉。SQLite 3.35+
# 支援 DROP COLUMN；失敗（欄位不存在 / 更舊的 SQLite）就算了，留著空欄位也無害。
_DROPPED_COLUMNS = ("pin_enabled", "pin_verifier_enc", "pin_kdf_salt")


class VaultError(Exception):
    pass


class NotConfiguredError(VaultError):
    pass


def _encrypt_secret(plaintext: str) -> bytes:
    return dpapi.protect(plaintext.encode("utf-8"))


def _decrypt_secret(blob: bytes) -> str:
    return dpapi.unprotect(blob).decode("utf-8")


class Vault:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(vault)")}
            for name in _DROPPED_COLUMNS:
                if name in existing:
                    try:
                        conn.execute(f"ALTER TABLE vault DROP COLUMN {name}")
                    except Exception:  # noqa: BLE001 - 丟不掉就留著，空欄位無害
                        pass

    def get_status(self) -> dict[str, Any]:
        """絕不含 password/totp_secret：不只是設成 None，是根本不放進 dict。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT account, totp_secret_enc, updated_at FROM vault WHERE id = 1"
            ).fetchone()
        if row is None:
            return {"configured": False, "account": None, "has_totp": False, "updated_at": None}
        return {
            "configured": True,
            "account": row["account"],
            "has_totp": row["totp_secret_enc"] is not None,
            "updated_at": row["updated_at"],
        }

    def save_credentials(
        self, account: str, password: str, totp_secret: str | None = None
    ) -> None:
        if not account or not password:
            raise ValueError("帳號與密碼為必填")

        password_enc = _encrypt_secret(password)
        totp_secret_enc = _encrypt_secret(totp_secret) if totp_secret else None

        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO vault (id, account, password_enc, totp_secret_enc) "
                    "VALUES (1, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET account=excluded.account, "
                    "password_enc=excluded.password_enc, totp_secret_enc=excluded.totp_secret_enc, "
                    "updated_at=datetime('now','localtime')",
                    (account, password_enc, totp_secret_enc),
                )

    def delete_credentials(self) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute("DELETE FROM vault WHERE id = 1")

    def unlock_credentials(self) -> dict[str, str | None]:
        """回傳明文 {account, password, totp_secret}，僅供呼叫端在準備自動代填登入表單前的
        極短暫記憶體使用——呼叫端不能把這個回傳值放進任何會回給前端的回應或 log。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT account, password_enc, totp_secret_enc FROM vault WHERE id = 1"
            ).fetchone()
        if row is None:
            raise NotConfiguredError("尚未設定動畫瘋登入資訊")

        totp_secret_enc = row["totp_secret_enc"]
        return {
            "account": row["account"],
            "password": _decrypt_secret(row["password_enc"]),
            "totp_secret": _decrypt_secret(totp_secret_enc) if totp_secret_enc else None,
        }


def generate_totp(secret_b32: str, when: float | None = None) -> str:
    """標準 TOTP（RFC 6238，HMAC-SHA1，6 碼，30 秒週期），跟一般 Authenticator App 相容。

    使用者貼上密鑰時可能夾帶空白（常見於分段顯示），先去除空白再處理。
    """
    normalized = secret_b32.strip().replace(" ", "").upper()
    padding = "=" * (-len(normalized) % 8)
    key = base64.b32decode(normalized + padding)

    counter = int((when if when is not None else time.time()) // 30)
    counter_bytes = struct.pack(">Q", counter)
    digest = hmac.new(key, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    truncated = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return "{:06d}".format(truncated % 1_000_000)
