"""站內「訂閱通知」收件匣。規格見 docs/requirements/completion_detection.md。

跟 `notify/`（Telegram/Discord 站外推播）不同——這是給頂列通知鈕的下拉面板用的站內
訊息（目前只有「番劇完結」一種 kind）。完結時兩邊都會寫一筆。

`resolved`：使用者在面板上對這筆做過操作（重新訂閱／退訂／知道了）之後設 1，面板只列
`resolved = 0` 的；頂列圖示是否顯示「有訊息」的動圖也看未處理數量。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    kind TEXT NOT NULL,
    sn INTEGER,
    anime_title TEXT,
    action_taken TEXT NOT NULL DEFAULT 'none',
    resolved INTEGER NOT NULL DEFAULT 0
)
"""


@dataclass(frozen=True)
class Notification:
    id: int
    created_at: str
    kind: str
    sn: int | None
    anime_title: str | None
    action_taken: str  # 'unsubscribe' | 'mark' | 'none'
    resolved: bool


class NotificationStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)

    def add(
        self,
        *,
        kind: str,
        sn: int | None = None,
        anime_title: str | None = None,
        action_taken: str = "none",
    ) -> int:
        with self._lock, self._database.transaction() as conn:
            cur = conn.execute(
                "INSERT INTO notifications (kind, sn, anime_title, action_taken) VALUES (?, ?, ?, ?)",
                (kind, sn, anime_title, action_taken),
            )
            return int(cur.lastrowid)

    def list_unresolved(self) -> list[Notification]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE resolved = 0 ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [_row(r) for r in rows]

    def unresolved_count(self) -> int:
        with self._database.transaction() as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM notifications WHERE resolved = 0").fetchone()[0]
            )

    def get(self, notification_id: int) -> Notification | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM notifications WHERE id = ?", (notification_id,)
            ).fetchone()
        return _row(row) if row is not None else None

    def resolve(self, notification_id: int) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE notifications SET resolved = 1 WHERE id = ?", (notification_id,)
            )

    def has_unresolved_for_sn(self, sn: int, kind: str) -> bool:
        with self._database.transaction() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM notifications WHERE sn = ? AND kind = ? AND resolved = 0 LIMIT 1",
                    (sn, kind),
                ).fetchone()
                is not None
            )

    def prune(self, retention_days: int) -> int:
        with self._lock, self._database.transaction() as conn:
            cur = conn.execute(
                "DELETE FROM notifications WHERE resolved = 1 "
                "AND created_at < datetime('now', 'localtime', ?)",
                (f"-{int(retention_days)} days",),
            )
            return cur.rowcount


def _row(row) -> Notification:
    return Notification(
        id=row["id"],
        created_at=row["created_at"],
        kind=row["kind"],
        sn=row["sn"],
        anime_title=row["anime_title"],
        action_taken=row["action_taken"],
        resolved=bool(row["resolved"]),
    )
