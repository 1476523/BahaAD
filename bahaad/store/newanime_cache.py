"""新番快訊的本地快取。規格見 docs/requirements/new_anime_bulletin.md §4。

跟 `AnimeCacheStore` / `YourAnimesCacheStore` 都分開——不同來源（GNN 文章 + seasonal.php +
youranimes）、不同生命週期（一季一次、備註消失就停）、獨立的 route。慣例比照
`store/youranimes_cache.py`：模組級建表 SQL、`threading.Lock`、`self._database.transaction()`、
`_ADDED_COLUMNS` 冪等 ALTER。

**階段 1–4：`newanime_bulletin` ＋ `newanime_item` ＋ `newanime_change_log`。**
`newanime_tracked` 在後面的階段加進 `_TABLES_SQL`。
"""

from __future__ import annotations

import threading
from typing import Any

from bahaad.store.database import Database

_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS newanime_bulletin (
    season_key TEXT PRIMARY KEY,          -- 'YYYYQQ'，例 '202610'
    gnn_sn INTEGER NOT NULL,              -- GNN 文章 sn
    article_published_at TEXT,            -- 文章發佈時間（'YYYY-MM-DD HH:MM:SS'）
    pending_note_gone_at TEXT,            -- 「授權流程」備註消失的時間（NULL＝還在）
    first_scan_done_at TEXT,              -- 首次掃描完成的時間
    full_list_notified_at TEXT,           -- 已發過「完整列表」通知的時間（首次一定發）
    next_seq INTEGER NOT NULL DEFAULT 1,  -- 下一個要配的虛構 sn 序號
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS newanime_item (
    virtual_sn INTEGER PRIMARY KEY,       -- 'YYYYQQNNN'，例 202610001
    season_key TEXT NOT NULL,
    source_name TEXT NOT NULL,            -- GNN 文章原始番名（比對真 sn 用，不可變）
    display_name TEXT,                    -- 使用者改的顯示名（NULL＝用 source_name）
    first_air_date TEXT,                  -- 'YYYY-MM-DD'
    first_air_time TEXT,                  -- 'HH:MM'（NULL＝待定）
    first_air_weekday INTEGER,            -- 1=一 .. 7=日
    first_ep_count INTEGER NOT NULL DEFAULT 1,
    first_ep_number INTEGER NOT NULL DEFAULT 1,
    backfill_from_episode INTEGER,
    ongoing_weekday INTEGER,
    ongoing_time TEXT,
    is_vip INTEGER NOT NULL DEFAULT 0,
    region_locked INTEGER NOT NULL DEFAULT 0,
    is_undetermined INTEGER NOT NULL DEFAULT 0,
    article_order INTEGER NOT NULL DEFAULT 0,
    studio TEXT,                          -- seasonal.php（NULL → 頁面顯示「待確認」）
    tags TEXT,                            -- seasonal.php
    subscribe_count TEXT,                 -- seasonal.php，例 '7.1萬'
    seasonal_range TEXT,                  -- seasonal.php 授權範圍，例 '台灣、港澳'
    director TEXT,                        -- youranimes 右欄
    cover_url TEXT,                       -- youranimes 主視覺圖 _origin
    real_video_sn INTEGER,               -- 比對成功後的真 video_sn
    stage TEXT NOT NULL DEFAULT 'upcoming',
    converted_at TEXT,                    -- 追蹤 → 訂閱 轉換完成的時間
    catch_up_target INTEGER,              -- 集數捕齊目標話數（§7c）
    catch_up_deadline TEXT,               -- 捕齊上限時間（過了就放棄）
    first_seen_at TEXT,
    last_seen_at TEXT,
    disappeared_prompt_at TEXT,
    UNIQUE (season_key, source_name)
);
CREATE INDEX IF NOT EXISTS idx_newanime_item_season ON newanime_item(season_key);
CREATE TABLE IF NOT EXISTS newanime_change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    season_key TEXT NOT NULL,
    virtual_sn INTEGER NOT NULL,
    kind TEXT NOT NULL,          -- 'added' | 'time_changed' | 'removed' | 're_added'
    old_weekday INTEGER,
    old_time TEXT,               -- 'HH:MM'，NULL＝待定
    new_weekday INTEGER,
    new_time TEXT,
    detected_at TEXT NOT NULL,
    notified_at TEXT             -- 階段 5 標記已發通知
);
CREATE INDEX IF NOT EXISTS idx_newanime_change_season ON newanime_change_log(season_key);
CREATE TABLE IF NOT EXISTS newanime_tracked (
    virtual_sn INTEGER PRIMARY KEY,      -- FK newanime_item.virtual_sn（新番版的「訂閱」）
    tracked_at TEXT NOT NULL,
    keep_after_disappear_until TEXT      -- 使用者選「繼續追蹤」後，一週內不再提示的截止（階段 9）
);
"""

# 舊資料庫加欄（比照 store/pending_downloads.py 的 PRAGMA + ALTER）
_ADDED_COLUMNS: dict[str, str] = {
    "full_list_notified_at": "TEXT",
}
_ADDED_ITEM_COLUMNS: dict[str, str] = {
    "converted_at": "TEXT",
    "catch_up_target": "INTEGER",
    "catch_up_deadline": "TEXT",
}

# GNN 文章來源的欄位（重掃時會更新這些，seasonal/youranimes/stage/real_video_sn 不動）
_GNN_FIELDS = (
    "first_air_date", "first_air_time", "first_air_weekday", "first_ep_count",
    "first_ep_number", "backfill_from_episode", "ongoing_weekday", "ongoing_time",
    "is_vip", "region_locked", "is_undetermined", "article_order",
)
_ITEM_COLUMNS = (
    "virtual_sn, season_key, source_name, display_name, first_air_date, first_air_time, "
    "first_air_weekday, first_ep_count, first_ep_number, backfill_from_episode, "
    "ongoing_weekday, ongoing_time, is_vip, region_locked, is_undetermined, article_order, "
    "studio, tags, subscribe_count, seasonal_range, director, cover_url, real_video_sn, stage, "
    "converted_at, catch_up_target, catch_up_deadline, "
    "first_seen_at, last_seen_at, disappeared_prompt_at"
)

_COLUMNS = (
    "season_key, gnn_sn, article_published_at, pending_note_gone_at, "
    "first_scan_done_at, full_list_notified_at, next_seq, created_at"
)


class NewAnimeCacheStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.executescript(_TABLES_SQL)
            existing = {
                row["name"] for row in conn.execute("PRAGMA table_info(newanime_bulletin)")
            }
            for name, decl in _ADDED_COLUMNS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE newanime_bulletin ADD COLUMN {name} {decl}")
            existing_item = {
                row["name"] for row in conn.execute("PRAGMA table_info(newanime_item)")
            }
            for name, decl in _ADDED_ITEM_COLUMNS.items():
                if name not in existing_item:
                    conn.execute(f"ALTER TABLE newanime_item ADD COLUMN {name} {decl}")

    # ---- newanime_bulletin ------------------------------------------------

    def get_bulletin(self, season_key: str) -> dict[str, Any] | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM newanime_bulletin WHERE season_key = ?",
                (season_key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_bulletin_by_gnn_sn(self, gnn_sn: int) -> dict[str, Any] | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM newanime_bulletin WHERE gnn_sn = ?", (gnn_sn,)
            ).fetchone()
        return dict(row) if row is not None else None

    def list_bulletins(self) -> list[dict[str, Any]]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM newanime_bulletin ORDER BY season_key DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_bulletin(
        self, season_key: str, gnn_sn: int, *, article_published_at: str | None = None
    ) -> None:
        """第一次偵測到某季的新番快訊公告時建立；同一季重跑更新 `gnn_sn` /（有帶的話）
        發佈時間。`next_seq` / 備註消失時間 / 首次掃描時間**不覆蓋**。"""
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "INSERT INTO newanime_bulletin (season_key, gnn_sn, article_published_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(season_key) DO UPDATE SET "
                "gnn_sn = excluded.gnn_sn, "
                "article_published_at = COALESCE(excluded.article_published_at, "
                "newanime_bulletin.article_published_at)",
                (season_key, gnn_sn, article_published_at),
            )

    def set_published_at(self, season_key: str, published_at: str) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE newanime_bulletin SET article_published_at = ? WHERE season_key = ?",
                (published_at, season_key),
            )

    def mark_pending_note_gone(self, season_key: str, at: str) -> None:
        """GNN 文章的「授權流程」備註消失了——只寫第一次（之後不覆蓋）。"""
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE newanime_bulletin SET pending_note_gone_at = ? "
                "WHERE season_key = ? AND pending_note_gone_at IS NULL",
                (at, season_key),
            )

    def mark_first_scan_done(self, season_key: str, at: str) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE newanime_bulletin SET first_scan_done_at = ? "
                "WHERE season_key = ? AND first_scan_done_at IS NULL",
                (at, season_key),
            )

    def mark_full_list_notified(self, season_key: str, at: str) -> None:
        """已發過「完整列表」通知——只寫第一次（之後不覆蓋），判「首次一定發」用。"""
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE newanime_bulletin SET full_list_notified_at = ? "
                "WHERE season_key = ? AND full_list_notified_at IS NULL",
                (at, season_key),
            )

    def take_virtual_seq(self, season_key: str, count: int = 1) -> int:
        """原子性地配 `count` 個虛構 sn 序號，回傳**起始**序號（含）。
        例：`next_seq` 是 5、`take_virtual_seq(k, 3)` → 回 5、`next_seq` 變 8。"""
        count = max(1, int(count))
        with self._lock, self._database.transaction() as conn:
            row = conn.execute(
                "SELECT next_seq FROM newanime_bulletin WHERE season_key = ?", (season_key,)
            ).fetchone()
            if row is None:
                raise KeyError(f"newanime_bulletin 沒有這一季：{season_key}")
            start = int(row["next_seq"])
            conn.execute(
                "UPDATE newanime_bulletin SET next_seq = ? WHERE season_key = ?",
                (start + count, season_key),
            )
        return start

    # ---- newanime_item --------------------------------------------------

    def get_item(self, virtual_sn: int) -> dict[str, Any] | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM newanime_item WHERE virtual_sn = ?", (virtual_sn,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_item_by_source(self, season_key: str, source_name: str) -> dict[str, Any] | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM newanime_item "
                "WHERE season_key = ? AND source_name = ?",
                (season_key, source_name),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_items(self, season_key: str) -> list[dict[str, Any]]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM newanime_item WHERE season_key = ? "
                "ORDER BY virtual_sn",
                (season_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_items(self, season_key: str) -> bool:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM newanime_item WHERE season_key = ? LIMIT 1", (season_key,)
            ).fetchone()
        return row is not None

    def add_item(self, virtual_sn: int, season_key: str, fields: dict[str, Any], *, seen_at: str) -> None:
        """新的一部新番。`fields` 只放 GNN 來源的欄位（見 `_GNN_FIELDS` + `source_name`）。"""
        cols = ["virtual_sn", "season_key", "source_name", "first_seen_at", "last_seen_at", *_GNN_FIELDS]
        values = [
            virtual_sn, season_key, fields["source_name"], seen_at, seen_at,
            *[fields.get(k) for k in _GNN_FIELDS],
        ]
        placeholders = ", ".join("?" for _ in cols)
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                f"INSERT INTO newanime_item ({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )

    def update_item_gnn_fields(self, virtual_sn: int, fields: dict[str, Any], *, seen_at: str) -> None:
        """重掃時更新 GNN 來源的欄位 + `last_seen_at`。seasonal/youranimes/stage/real_sn 不動。"""
        assignments = ", ".join(f"{k} = ?" for k in _GNN_FIELDS) + ", last_seen_at = ?"
        values = [fields.get(k) for k in _GNN_FIELDS] + [seen_at, virtual_sn]
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                f"UPDATE newanime_item SET {assignments} WHERE virtual_sn = ?", values
            )

    def touch_seen(self, virtual_sn: int, at: str) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE newanime_item SET last_seen_at = ?, "
                "first_seen_at = COALESCE(first_seen_at, ?) WHERE virtual_sn = ?",
                (at, at, virtual_sn),
            )

    def set_item_stage(self, virtual_sn: int, stage: str) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE newanime_item SET stage = ? WHERE virtual_sn = ?", (stage, virtual_sn)
            )

    def set_item_enrichment(self, virtual_sn: int, **fields: Any) -> None:
        """補 seasonal.php / youranimes 的欄位（`studio`/`tags`/`subscribe_count`/`director`/
        `cover_url`）——只更新有帶進來、且目前是 NULL 或值不同的欄位。"""
        allowed = {
            "studio", "tags", "subscribe_count", "seasonal_range", "director",
            "cover_url", "real_video_sn", "display_name",
            "converted_at", "catch_up_target", "catch_up_deadline",
        }
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return
        assignments = ", ".join(f"{k} = ?" for k in sets)
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                f"UPDATE newanime_item SET {assignments} WHERE virtual_sn = ?",
                [*sets.values(), virtual_sn],
            )

    def list_active_items_missing_from(
        self, season_key: str, seen_source_names: set[str]
    ) -> list[dict[str, Any]]:
        """這一季裡、`stage` 還在跑（不是 removed/done）、但這次掃描沒出現的 item。"""
        return [
            row
            for row in self.list_items(season_key)
            if row["source_name"] not in seen_source_names
            and row["stage"] not in ("removed", "done")
        ]

    # ---- newanime_change_log ------------------------------------------

    def log_change(
        self,
        season_key: str,
        virtual_sn: int,
        kind: str,
        *,
        detected_at: str,
        old_weekday: int | None = None,
        old_time: str | None = None,
        new_weekday: int | None = None,
        new_time: str | None = None,
    ) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "INSERT INTO newanime_change_log "
                "(season_key, virtual_sn, kind, old_weekday, old_time, new_weekday, new_time, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (season_key, virtual_sn, kind, old_weekday, old_time, new_weekday, new_time, detected_at),
            )

    def list_changes(self, season_key: str, *, only_unnotified: bool = False) -> list[dict[str, Any]]:
        clause = "AND notified_at IS NULL" if only_unnotified else ""
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT id, season_key, virtual_sn, kind, old_weekday, old_time, "
                "new_weekday, new_time, detected_at, notified_at "
                f"FROM newanime_change_log WHERE season_key = ? {clause} ORDER BY id",
                (season_key,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_changes_notified(self, change_ids: list[int], at: str) -> None:
        if not change_ids:
            return
        placeholders = ", ".join("?" for _ in change_ids)
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                f"UPDATE newanime_change_log SET notified_at = ? WHERE id IN ({placeholders})",
                (at, *change_ids),
            )

    # ---- newanime_tracked --------------------------------------------

    def track(self, virtual_sn: int, at: str) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "INSERT INTO newanime_tracked (virtual_sn, tracked_at) VALUES (?, ?) "
                "ON CONFLICT(virtual_sn) DO NOTHING",
                (virtual_sn, at),
            )

    def untrack(self, virtual_sn: int) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute("DELETE FROM newanime_tracked WHERE virtual_sn = ?", (virtual_sn,))

    def is_tracked(self, virtual_sn: int) -> bool:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM newanime_tracked WHERE virtual_sn = ?", (virtual_sn,)
            ).fetchone()
        return row is not None

    def list_tracked(self) -> set[int]:
        with self._database.transaction() as conn:
            rows = conn.execute("SELECT virtual_sn FROM newanime_tracked").fetchall()
        return {row["virtual_sn"] for row in rows}

    def get_tracked(self, virtual_sn: int) -> dict[str, Any] | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT virtual_sn, tracked_at, keep_after_disappear_until "
                "FROM newanime_tracked WHERE virtual_sn = ?",
                (virtual_sn,),
            ).fetchone()
        return dict(row) if row is not None else None

    def set_disappeared_prompt_at(self, virtual_sn: int, at: str) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE newanime_item SET disappeared_prompt_at = ? WHERE virtual_sn = ?",
                (at, virtual_sn),
            )

    def set_keep_after_disappear(self, virtual_sn: int, until: str | None) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE newanime_tracked SET keep_after_disappear_until = ? WHERE virtual_sn = ?",
                (until, virtual_sn),
            )
