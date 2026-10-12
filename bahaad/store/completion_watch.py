"""番劇完結偵測的狀態表。規格見 docs/requirements/completion_detection.md。

每個「已訂閱」的 sn 一列，記錄它在週期表上的出現狀況：
- `anime_title`：在週期表上看過的標題（比對「還在不在週期表」用——週期表項目連的是
  「最新一集」的 video_sn、每週會變，只能靠標題比對，不能靠 sn）
- `last_seen_at`：最後一次在週期表上看到它的時間（NULL＝從沒看過它在週期表上）
- `gone_since`：從週期表上消失的時間（NULL＝目前在週期表上，或從沒看過）
- `absence_count`：連續幾輪檢查都沒出現在週期表上（每輪 `mark_gone()` 真的執行到就
  +1；`mark_seen()` 重新出現就歸零）。2026-09-28 使用者定案改成看這個數字判斷完結，
  不再等一整個日曆週——見 `scheduler/completion_watch.py` 的 `_DEFAULT_COMPLETION_
  ABSENCE_ROUNDS`。
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
    absence_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'watching',
    completed_at TEXT,
    updated_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""


# 從舊版升級上來的資料庫要補這個欄位（比照 store/identity.py 既有的 idempotent 做法：
# PRAGMA table_info 檢查、缺了才 ALTER TABLE ADD COLUMN）。
_ADDED_COLUMNS = {
    "absence_count": "INTEGER NOT NULL DEFAULT 0",
}


@dataclass(frozen=True)
class CompletionWatchRow:
    sn: int
    anime_title: str | None
    last_seen_at: str | None
    gone_since: str | None
    absence_count: int
    status: str
    completed_at: str | None


class CompletionWatchStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(completion_watch)")}
            for name, decl in _ADDED_COLUMNS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE completion_watch ADD COLUMN {name} {decl}")

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
        """這個 sn 這輪在週期表上看到了：更新 last_seen、清 gone_since、記標題、
        `absence_count` 歸零（重新出現 → 之前累積的缺席輪數不算數，要重新連續累積）。"""
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "INSERT INTO completion_watch "
                "  (sn, anime_title, last_seen_at, gone_since, absence_count, updated_at) "
                "VALUES (?, ?, ?, NULL, 0, datetime('now','localtime')) "
                "ON CONFLICT(sn) DO UPDATE SET "
                "  anime_title = COALESCE(excluded.anime_title, anime_title), "
                "  last_seen_at = excluded.last_seen_at, gone_since = NULL, absence_count = 0, "
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
        """這個 sn 這輪不在週期表上，且之前是在的：`absence_count` +1（每輪真的檢查到
        都會走到這裡，累積連續缺席輪數）；`gone_since` 記下第一次消失的時間，只在還是
        NULL 時寫，之後不覆蓋——之後每輪繼續缺席也不會變。"""
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE completion_watch SET "
                "  gone_since = COALESCE(gone_since, ?), "
                "  absence_count = absence_count + 1, "
                "  updated_at = datetime('now','localtime') "
                "WHERE sn = ?",
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
        absence_count=row["absence_count"],
        status=row["status"],
        completed_at=row["completed_at"],
    )
