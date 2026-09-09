r"""匿名啟動識別碼——即時匿名使用統計的客戶端身分。規格見
docs/requirements/realtime_stats.md。

**一輩子只產生一次**：

- 存在 `%LOCALAPPDATA%\BahaAD\activation.db`（**刻意不放進 `bahaad.db`**），DPAPI 加密。
- 用獨立檔案就是為了「`/reset-everything` 刪掉整個 `bahaad.db` 也不會弄丟識別碼」
  ——`web/auth.py` 的 `reset_everything()` 只刪 `bahaad.db*`。程式自我更新換 exe、
  重新下載安裝，識別碼都在 `%LOCALAPPDATA%`、不在安裝目錄，一樣不受影響。
- **只有使用者手動刪掉整個 `%LOCALAPPDATA%\BahaAD` 資料夾**，下次啟動才會重新產生。

16 字元、字元集 `A-Za-z0-9-_`（URL/JSON/日誌全安全、不需跳脫；使用者 2026-09-08：
「以不影響傳輸與安全為主要」）。
"""

from __future__ import annotations

import secrets
import string
import threading

from bahaad import dpapi
from bahaad.store.database import Database

_ALPHABET = string.ascii_letters + string.digits + "-_"
_ID_LENGTH = 16

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS activation (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    activation_id_enc BLOB NOT NULL,
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""


def _generate() -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(_ID_LENGTH))


class ActivationStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)

    def peek(self) -> str | None:
        """已經產生過就回識別碼，沒有回 `None`——**不會**順手產生一組。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT activation_id_enc FROM activation WHERE id = 1"
            ).fetchone()
        if row is None or row["activation_id_enc"] is None:
            return None
        try:
            return dpapi.unprotect(row["activation_id_enc"]).decode("utf-8")
        except Exception:  # noqa: BLE001 - 解不開（換機器／DPAPI 壞了）＝視同還沒有
            return None

    def get_or_create(self) -> str:
        """回目前的識別碼；還沒有就產生一組、存起來、回傳。產生只會發生這一次。"""
        existing = self.peek()
        if existing is not None:
            return existing
        with self._lock:
            # 搶鎖期間可能已被另一條執行緒建好
            again = self.peek()
            if again is not None:
                return again
            new_id = _generate()
            blob = dpapi.protect(new_id.encode("utf-8"))
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO activation (id, activation_id_enc) VALUES (1, ?) "
                    "ON CONFLICT(id) DO NOTHING",
                    (blob,),
                )
                row = conn.execute(
                    "SELECT activation_id_enc FROM activation WHERE id = 1"
                ).fetchone()
            # 若剛好撞上 ON CONFLICT DO NOTHING（另一條執行緒先寫入），讀回實際存的那組
            try:
                return dpapi.unprotect(row["activation_id_enc"]).decode("utf-8")
            except Exception:  # noqa: BLE001
                return new_id
