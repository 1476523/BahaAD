"""手動任務（單集／批次）的持久化，供程式意外中斷後重啟時還原未完成的手動任務。

需求：使用者手動送出的下載任務(不是排程自動觸發的)如果在完成前程式意外關閉，
重啟後應該要能自動接續，而不是悄悄遺失。提交時寫入、任務終結(成功或確定放棄重試)時移除。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS manual_tasks (
    sn INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,      -- 'single' or 'batch'
    params TEXT NOT NULL,    -- JSON: 下載參數(解析度/是否分類/重新命名等)
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""


class ManualTaskStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)

    def save_task(self, sn: int, kind: str, params: dict[str, Any]) -> None:
        if kind not in ("single", "batch"):
            raise ValueError("kind 必須是 'single' 或 'batch'")
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO manual_tasks (sn, kind, params) VALUES (?, ?, ?) "
                    "ON CONFLICT(sn) DO UPDATE SET kind=excluded.kind, params=excluded.params",
                    (sn, kind, json.dumps(params, ensure_ascii=False)),
                )

    def remove_task(self, sn: int) -> int:
        """回傳刪掉幾筆（0 或 1，`sn` 是 PK）——`scheduler/main_loop.py` 的呼叫端忽略
        回傳值，階段 5「資料庫整頓」要用來統計清了幾筆。"""
        with self._lock:
            with self._database.transaction() as conn:
                return conn.execute("DELETE FROM manual_tasks WHERE sn = ?", (sn,)).rowcount

    def list_tasks(self, kind: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT sn, kind, params FROM manual_tasks"
        args: tuple = ()
        if kind is not None:
            query += " WHERE kind = ?"
            args = (kind,)
        with self._database.transaction() as conn:
            rows = conn.execute(query, args).fetchall()
        return [
            {"sn": row["sn"], "kind": row["kind"], "params": json.loads(row["params"])}
            for row in rows
        ]
