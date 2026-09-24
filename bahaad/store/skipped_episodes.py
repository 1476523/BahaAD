"""「標記為已下載」的集數清單。規格見 docs/requirements/web_redesign_round2.md 階段 4。

「重新檢查排程更新」找到新集數後，使用者可以逐項選「下載」或「標記為已下載」——後者
不會真的下載，但之後的自動排程檢查也不會再把這一集排進去。BahaAD 沒有舊專案那種
`anime` 下載歷史表（已下載判斷是掃檔名裡的 `[video_sn]`，不建資料庫紀錄），所以這個
「不下載但也不要再檢查」的狀態要另外存一張表。

儲存原則（使用者 2026-08-26 定案）：正規化——一個用途一張表、每欄一個明確的值，
不用 JSON blob。`skipped_episodes` 就四個獨立欄位：`sn`（番劇代碼／追蹤項目 sn）、
`video_sn`（那一集的代碼，UNIQUE，標記同一集兩次是冪等）、`anime_title`、`marked_at`。

這張表也是階段 5「資料庫整頓」主要要清的孤兒資料來源——取消訂閱一部番劇後，它的
`skipped_episodes` 列就沒用了（`delete_for_sn()`／`sns_with_records()`）。
"""

from __future__ import annotations

import threading
from datetime import datetime

from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS skipped_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sn INTEGER NOT NULL,
    video_sn INTEGER NOT NULL UNIQUE,
    anime_title TEXT NOT NULL,
    marked_at TEXT NOT NULL
)
"""


class SkippedEpisodeStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)

    def mark(self, sn: int, video_sn: int, anime_title: str) -> None:
        """把 `video_sn` 標記為「已下載（實際上沒下載，但自動排程不要再排入）」。
        標記同一集兩次是冪等（`video_sn UNIQUE` ＋ `INSERT OR IGNORE`），不更新
        `marked_at`——第一次標記的時間才是有意義的。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT OR IGNORE INTO skipped_episodes (sn, video_sn, anime_title, marked_at) "
                    "VALUES (?, ?, ?, ?)",
                    (sn, video_sn, anime_title, datetime.now().isoformat(timespec="seconds")),
                )

    def unmark(self, video_sn: int) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute("DELETE FROM skipped_episodes WHERE video_sn = ?", (video_sn,))

    def is_skipped(self, video_sn: int) -> bool:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM skipped_episodes WHERE video_sn = ? LIMIT 1", (video_sn,)
            ).fetchone()
        return row is not None

    def list_all(self) -> list[dict]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT sn, video_sn, anime_title, marked_at FROM skipped_episodes ORDER BY marked_at"
            ).fetchall()
        return [
            {
                "sn": row["sn"],
                "video_sn": row["video_sn"],
                "anime_title": row["anime_title"],
                "marked_at": row["marked_at"],
            }
            for row in rows
        ]

    def sns_with_records(self) -> set[int]:
        """有幾個不同的番劇 sn 在這張表裡有列——階段 5「資料庫整頓」列出「已不在追蹤
        清單、但 DB 還有紀錄」的 sn 時要用。"""
        with self._database.transaction() as conn:
            rows = conn.execute("SELECT DISTINCT sn FROM skipped_episodes").fetchall()
        return {row["sn"] for row in rows}

    def count_for_sn(self, sn: int) -> int:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM skipped_episodes WHERE sn = ?", (sn,)
            ).fetchone()
        return int(row["n"])

    def delete_for_sn(self, sn: int) -> int:
        """清掉一部番劇的所有標記列（階段 5「資料庫整頓」用）。回傳刪掉幾列。"""
        with self._lock:
            with self._database.transaction() as conn:
                cursor = conn.execute("DELETE FROM skipped_episodes WHERE sn = ?", (sn,))
                return cursor.rowcount
