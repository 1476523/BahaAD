"""設定讀寫的唯一入口。

設計理由：舊專案裡「整包讀出設定快照、只想改一兩個欄位、卻整包寫回」這個模式反覆出現，
造成好幾類資料被悄悄覆蓋掉的問題（見對話紀錄裡的併發風險審查）。這裡從介面設計上直接
杜絕這個問題——SettingsStore 只提供 update(partial)，故意不提供任何「整包覆寫」的方法，
每次 update() 都是「讀最新值→合併→寫回」，呼叫端沒有機會意外覆蓋自己沒有要改的欄位。
"""

from __future__ import annotations

import json
import threading
from typing import Any, Mapping

from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


class SettingsStore:
    def __init__(self, database: Database, defaults: Mapping[str, Any]) -> None:
        self._database = database
        self._defaults = dict(defaults)
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)

    def get_all(self) -> dict[str, Any]:
        """回傳目前完整設定(預設值 + 已儲存的覆蓋值)。"""
        result = dict(self._defaults)
        with self._database.transaction() as conn:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
        for row in rows:
            result[row["key"]] = json.loads(row["value"])
        return result

    def get(self, key: str, default: Any = None) -> Any:
        settings = self.get_all()
        if key in settings:
            return settings[key]
        return default

    def update(self, partial: Mapping[str, Any]) -> dict[str, Any]:
        """把 partial 合併進目前設定，只動 partial 裡出現的欄位，回傳合併後的完整設定。

        刻意不提供整包覆寫的方法——任何呼叫端想更新設定都只能透過這裡，結構上不可能
        不小心把別的呼叫端剛存的欄位洗掉。
        """
        if not isinstance(partial, Mapping):
            raise TypeError("update() 需要一個 dict/Mapping，逐欄位合併寫入")
        with self._lock:
            with self._database.transaction() as conn:
                for key, value in partial.items():
                    conn.execute(
                        "INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value, ensure_ascii=False)),
                    )
            return self.get_all()

    def increment(self, key: str, delta: int = 1) -> int:
        """把一個整數計數器欄位 +delta（沒有就從 0 起算），回傳新值。整個讀改寫在鎖內
        完成，多執行緒同時 +1 不會漏。給「已回報錯誤的次數」這類純計數用。"""
        with self._lock:
            with self._database.transaction() as conn:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                ).fetchone()
                try:
                    current = int(json.loads(row["value"])) if row is not None else 0
                except (ValueError, TypeError):
                    current = 0
                new_value = current + delta
                conn.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(new_value)),
                )
        return new_value

    def reset(self, keys: list[str]) -> dict[str, Any]:
        """把指定欄位還原成預設值(刪掉覆蓋列，不是寫入預設值本身，未來預設值調整才會自動生效)。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.executemany(
                    "DELETE FROM settings WHERE key = ?",
                    [(key,) for key in keys],
                )
            return self.get_all()
