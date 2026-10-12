"""應用程式日誌的儲存（round 7 第 13 項；2026-08-29 定案：DB 就是唯一的日誌儲存）。

`logging_db.SqliteLogHandler` 把 `bahaad` logger 的紀錄（含 `exc_info` 的多行例外堆疊）
直接寫進這張表。不再另外寫 `logs/bahaad.log` 檔案——見 `logging_db.py` 開頭的理由。
設定頁的「日誌」可以查／篩／清。

列數上限自己控制：超過 `_MAX_ROWS` 就從最舊的刪，每 `_PRUNE_EVERY` 筆才掃一次。

儲存原則（使用者 2026-08-26 定案）：一個用途一張表、每欄一個明確的值。
"""

from __future__ import annotations

import threading
from datetime import datetime

from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    logger TEXT NOT NULL,
    message TEXT NOT NULL
)
"""

# 表最多保留這麼多列，超過就從最舊的刪。
_MAX_ROWS = 10_000
# 每寫這麼多筆才檢查一次上限，不用每次寫都掃。
_PRUNE_EVERY = 200

_LEVEL_ORDER = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class LogStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        self._writes_since_prune = 0
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)

    def add(self, level: str, logger: str, message: str, ts: str | None = None) -> None:
        self.add_many([(ts or datetime.now().isoformat(timespec="seconds"), level, logger, message)])

    def add_many(self, entries: list[tuple[str, str, str, str]]) -> None:
        """一次寫入多筆 (ts, level, logger, message)，最後修剪一次列數上限。"""
        if not entries:
            return
        with self._lock:
            with self._database.transaction() as conn:
                conn.executemany(
                    "INSERT INTO logs (ts, level, logger, message) VALUES (?, ?, ?, ?)",
                    entries,
                )
                self._writes_since_prune += len(entries)
                if self._writes_since_prune >= _PRUNE_EVERY:
                    self._writes_since_prune = 0
                    conn.execute(
                        "DELETE FROM logs WHERE id NOT IN "
                        "(SELECT id FROM logs ORDER BY id DESC LIMIT ?)",
                        (_MAX_ROWS,),
                    )

    def list(self, *, min_level: str | None = None, limit: int = 500, offset: int = 0) -> list[dict]:
        """最新的在前。`min_level` 給定時只回那個等級（含）以上的。"""
        query = "SELECT id, ts, level, logger, message FROM logs"
        args: list = []
        if min_level and min_level.upper() in _LEVEL_ORDER:
            allowed = [name for name, order in _LEVEL_ORDER.items() if order >= _LEVEL_ORDER[min_level.upper()]]
            query += " WHERE level IN (%s)" % ",".join("?" * len(allowed))
            args.extend(allowed)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        args.extend([max(1, limit), max(0, offset)])
        with self._database.transaction() as conn:
            rows = conn.execute(query, args).fetchall()
        return [
            {"id": r["id"], "ts": r["ts"], "level": r["level"], "logger": r["logger"], "message": r["message"]}
            for r in rows
        ]

    def count(self, *, min_level: str | None = None) -> int:
        query = "SELECT COUNT(*) AS n FROM logs"
        args: list = []
        if min_level and min_level.upper() in _LEVEL_ORDER:
            allowed = [name for name, order in _LEVEL_ORDER.items() if order >= _LEVEL_ORDER[min_level.upper()]]
            query += " WHERE level IN (%s)" % ",".join("?" * len(allowed))
            args.extend(allowed)
        with self._database.transaction() as conn:
            row = conn.execute(query, args).fetchone()
        return int(row["n"])

    def clear(self) -> int:
        with self._lock:
            with self._database.transaction() as conn:
                return conn.execute("DELETE FROM logs").rowcount
