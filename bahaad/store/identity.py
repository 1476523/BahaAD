"""動畫瘋登入身份的持久化：Cookie、裝置 ID、環境偽裝指紋（UA/JA3/Akamai）。

注意：這裡存的是「跟動畫瘋網站互動用」的身份(gamer_client/ 在 Phase 1 會用到)，跟
access_gate/(Phase 6，GitHub OAuth 登入、用於 star 檢查與多 IP 防護)是完全不同的兩種身份，
不要混在同一個 store 模組裡——兩者的用途、生命週期、外洩風險等級都不一樣。

## 加密

Cookie 與環境偽裝指紋（UA/JA3/Akamai）都用 DPAPI 加密後才落地（`cookies_enc`／
`ua_enc`／`ja3_enc`／`akamai_enc` 欄位），比照 `store/access_gate.py` 的決策——只用
DPAPI 這一層，不需要 `vault.py` 的可選 PIN 分層（BahaAD 網頁登入密碼已是不可逆雜湊、
DPAPI 又綁定 Windows 使用者帳戶，PIN 是多餘的）。`device_id` 本身不是機密（伺服器
核發的裝置識別碼），維持明文。

早期版本 Cookie 是明文 JSON 存在 `cookies` 欄位，`get_cookies()` 讀到舊資料會就地
遷移成加密欄位、把舊欄位清成 NULL。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from bahaad import dpapi
from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS identity (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    cookies TEXT,
    cookies_enc BLOB,
    device_id TEXT,
    device_fingerprint TEXT,
    ua_enc BLOB,
    ja3_enc BLOB,
    akamai_enc BLOB,
    fingerprint_updated_at TEXT,
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""

# 針對「表已存在、但欄位是舊版」的資料庫補欄位——store/ 沒有正式的 migration 框架，
# 各模組自己用 CREATE TABLE IF NOT EXISTS；這裡多一步 idempotent 的補欄位，讓從舊版
# 升級上來的資料庫也拿得到新欄位。
_ADDED_COLUMNS = {
    "cookies_enc": "BLOB",
    "ua_enc": "BLOB",
    "ja3_enc": "BLOB",
    "akamai_enc": "BLOB",
    "fingerprint_updated_at": "TEXT",
}


def _enc(value: str) -> bytes:
    return dpapi.protect(value.encode("utf-8"))


def _dec(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    return dpapi.unprotect(blob).decode("utf-8")


class IdentityStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(identity)")}
            for name, decl in _ADDED_COLUMNS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE identity ADD COLUMN {name} {decl}")

    # ---- Cookie ----------------------------------------------------------

    def get_cookies(self) -> dict[str, Any]:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT cookies, cookies_enc FROM identity WHERE id = 1"
            ).fetchone()
        if row is None:
            return {}
        if row["cookies_enc"] is not None:
            return json.loads(_dec(row["cookies_enc"]))
        if row["cookies"]:
            # 舊版明文資料：就地遷移成加密欄位，之後不會再走這條路
            legacy = json.loads(row["cookies"])
            self.set_cookies(legacy)
            return legacy
        return {}

    def set_cookies(self, cookies: dict[str, Any]) -> None:
        blob = _enc(json.dumps(cookies, ensure_ascii=False))
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO identity (id, cookies, cookies_enc) VALUES (1, NULL, ?) "
                    "ON CONFLICT(id) DO UPDATE SET cookies=NULL, cookies_enc=excluded.cookies_enc, "
                    "updated_at=datetime('now','localtime')",
                    (blob,),
                )

    def clear_cookies(self) -> None:
        self.set_cookies({})

    # ---- 裝置 ID -------------------------------------------------------

    def get_device_id(self) -> tuple[str, str] | None:
        """回傳 (device_id, fingerprint)，尚未申請過則回傳 None。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT device_id, device_fingerprint FROM identity WHERE id = 1"
            ).fetchone()
        if row is None or row["device_id"] is None:
            return None
        return row["device_id"], row["device_fingerprint"]

    def set_device_id(self, device_id: str, fingerprint: str) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO identity (id, device_id, device_fingerprint) VALUES (1, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET device_id=excluded.device_id, "
                    "device_fingerprint=excluded.device_fingerprint, "
                    "updated_at=datetime('now','localtime')",
                    (device_id, fingerprint),
                )

    def invalidate_device_id(self) -> None:
        """清掉已核發的裝置 ID——Cookie 重新登入、或環境偽裝指紋換過之後呼叫。舊的
        device_id 是在「另一組」cookie／瀏覽器指紋底下核發的，沿用會跟目前的身份對不上，
        容易被站方判成「裝置驗證異常」(code 1007)，下次 DeviceIdManager 會自然重新申請。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE identity SET device_id=NULL, device_fingerprint=NULL, "
                    "updated_at=datetime('now','localtime') WHERE id = 1"
                )

    # ---- 環境偽裝指紋（UA/JA3/Akamai）--------------------------------

    def get_fingerprint(self) -> dict[str, str | None]:
        """解密後的 {ua, ja3, akamai}——**只給 GamerSession 建構請求用**，呼叫端不能
        把這個回傳值放進任何會回給網頁前端的回應或 log。前端要顯示狀態用
        get_fingerprint_status()。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT ua_enc, ja3_enc, akamai_enc FROM identity WHERE id = 1"
            ).fetchone()
        if row is None:
            return {"ua": None, "ja3": None, "akamai": None}
        return {
            "ua": _dec(row["ua_enc"]),
            "ja3": _dec(row["ja3_enc"]),
            "akamai": _dec(row["akamai_enc"]),
        }

    def get_fingerprint_status(self) -> dict[str, Any]:
        """絕不含解密後的 ua/ja3/akamai：只回「有沒有設定過」「什麼時候更新的」。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT ua_enc, fingerprint_updated_at FROM identity WHERE id = 1"
            ).fetchone()
        if row is None or row["ua_enc"] is None:
            return {"configured": False, "updated_at": None}
        return {"configured": True, "updated_at": row["fingerprint_updated_at"]}

    def set_fingerprint(self, ua: str, ja3: str, akamai: str) -> None:
        ua_enc, ja3_enc, akamai_enc = _enc(ua), _enc(ja3), _enc(akamai)
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO identity (id, ua_enc, ja3_enc, akamai_enc, fingerprint_updated_at) "
                    "VALUES (1, ?, ?, ?, datetime('now','localtime')) "
                    "ON CONFLICT(id) DO UPDATE SET ua_enc=excluded.ua_enc, ja3_enc=excluded.ja3_enc, "
                    "akamai_enc=excluded.akamai_enc, "
                    "fingerprint_updated_at=datetime('now','localtime'), "
                    "updated_at=datetime('now','localtime')",
                    (ua_enc, ja3_enc, akamai_enc),
                )

    def clear_fingerprint(self) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE identity SET ua_enc=NULL, ja3_enc=NULL, akamai_enc=NULL, "
                    "fingerprint_updated_at=NULL, updated_at=datetime('now','localtime') WHERE id = 1"
                )
