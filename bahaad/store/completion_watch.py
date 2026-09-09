"""番劇完結偵測的狀態表。規格見 docs/requirements/completion_detection.md。

每個「已訂閱」的 sn 一列，記錄它在週期表上的出現狀況：
- `anime_title`：在週期表上看過的標題（比對「還在不在週期表」用——週期表項目連的是
  「最新一集」的 video_sn、每週會變，只能靠標題比對，不能靠 sn）
- `last_seen_at`：最後一次在週期表上看到它的時間（NULL＝從沒看過它在週期表上）
- `gone_since`：從週期表上消失的時間（NULL＝目前在週期表上，或從沒看過）
- `status`：`watching`（監視中）／`completed`（已判定完結）
- `completed_at`：判定完結的時間

只對「曾經出現在週期表上」（`last_seen_at` 有值）的訂閱番劇做完結偵測——使用者手動加進
追蹤、本來就不在週期表的舊番／劇場版不套用。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Iterable

from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS completion_watch (
    sn INTEGER PRIMARY KEY,
    anime_title TEXT,
    last_seen_at TEXT,
    gone_since TEXT,
    status TEXT NOT NULL DEFAULT 'watching',
    completed_at TEXT,
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""


@dataclass(frozen=True)
class CompletionWatchRow:
    sn: int
    anime_title: str | None
    last_seen_at: str | None
    gone_since: str | None
    status: str
    completed_at: str | None


class CompletionWatchStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)

    def get(self, sn: int) -> CompletionWatchRow | None:
        with self._database.transaction() as conn:
            row = conn.execute("SELECT * FROM completion_watch WHERE sn = ?", (sn,)).fetchone()
        return _row(row) if row is not None else None

    def all_active(self) -> list[CompletionWatchRow]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM completion_watch WHERE status = 'watching'"
            ).fetchall()
        return [_row(r) for r in rows]

    def completed_sns(self) -> set[int]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT sn FROM completion_watch WHERE status = 'completed'"
            ).fetchall()
        return {r["sn"] for r in rows}

    def mark_seen(self, sn: int, title: str | None, now_iso: str) -> None:
        """這個 sn 這輪在週期表上看到了：更新 last_seen、清 gone_since、記標題。"""
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "INSERT INTO completion_watch (sn, anime_title, last_seen_at, gone_since, updated_at) "
                "VALUES (?, ?, ?, NULL, datetime('now','localtime')) "
                "ON CONFLICT(sn) DO UPDATE SET "
                "  anime_title = COALESCE(excluded.anime_title, anime_title), "
                "  last_seen_at = excluded.last_seen_at, gone_since = NULL, "
                "  updated_at = datetime('now','localtime')",
                (sn, title, now_iso),
            )

    def learn_title(self, sn: int, title: str) -> None:
        """只補標題（不動 last_seen／gone_since）——從 anime_cache 之類拿到標題時用。"""
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "INSERT INTO completion_watch (sn, anime_title, updated_at) "
                "VALUES (?, ?, datetime('now','localtime')) "
                "ON CONFLICT(sn) DO UPDATE SET anime_title = excluded.anime_title, "
                "  updated_at = datetime('now','localtime')",
                (sn, title),
            )

    def mark_gone(self, sn: int, now_iso: str) -> None:
        """這個 sn 這輪不在週期表上，且之前是在的：記下開始消失的時間（只在 gone_since
        還是 NULL 時寫，之後不覆蓋）。"""
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE completion_watch SET gone_since = ?, updated_at = datetime('now','localtime') "
                "WHERE sn = ? AND gone_since IS NULL",
                (now_iso, sn),
            )

    def set_completed(self, sn: int, now_iso: str) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE completion_watch SET status = 'completed', completed_at = ?, "
                "updated_at = datetime('now','localtime') WHERE sn = ?",
                (now_iso, sn),
            )

    def delete(self, sn: int) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute("DELETE FROM completion_watch WHERE sn = ?", (sn,))

    def prune(self, keep_sns: Iterable[int]) -> int:
        """刪掉已經不在訂閱清單裡的 sn 的列（退訂了就不用再監視）。回刪掉幾列。"""
        keep = set(keep_sns)
        with self._lock, self._database.transaction() as conn:
            existing = {r["sn"] for r in conn.execute("SELECT sn FROM completion_watch")}
            stale = existing - keep
            for sn in stale:
                conn.execute("DELETE FROM completion_watch WHERE sn = ?", (sn,))
        return len(stale)


def _row(row) -> CompletionWatchRow:
    return CompletionWatchRow(
        sn=row["sn"],
        anime_title=row["anime_title"],
        last_seen_at=row["last_seen_at"],
        gone_since=row["gone_since"],
        status=row["status"],
        completed_at=row["completed_at"],
    )
