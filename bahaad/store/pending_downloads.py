"""進行中／待搬移的下載任務的持久化（round 7 第 11 項）。

`registry.py` 是純記憶體——程式異常關閉時「正在下載到一半」的那幾集，重啟後沒有任何
東西記得它們該被重下。這張表補上這個缺口：

- `stage='downloading'`：`_maybe_download()` 提交下載時寫入。程式啟動時
  `app_shell.resume_pending_downloads()` 掃還留著的 `downloading` 列 → 重新提交整集下載
  （不做 segment 真續傳，見 round7.md 定案）。
- `stage='awaiting_move'`：番劇已經在暫存區下載＋合併完成，但要搬進下載目錄時發現
  下載目錄不存在（網路磁碟／外接裝置沒接）。不刪暫存、不判失敗，改記成這個狀態，
  等使用者改下載目錄或把裝置接回來後按「再試一次」把 `staged_dir` 搬進去。
- `stage='failed'`：整集重試都用完還是失敗（使用者 2026-09-03：「下載失敗的項目在
  更新或重啟後會遺失」）。`last_error`／`failure_count` 一起存下來，程式啟動時
  `resume_pending_downloads()` 把它們灌回 `registry`，下載列表頁的「最近失敗」卡片
  重啟／更新後照樣在。
- `stage='retry_pending'`：動畫瘋站方暫時性錯誤（503 維護中／回應不是 JSON）→ 排進
  延長重試排程（`main_loop._SITE_ERROR_RETRY_SCHEDULE`：3 分 ×10 → 5 分 ×10 →
  10 分 ×10 → 放棄）。`next_retry_at`＝下一次該重試的絕對時間，`retry_attempt`＝
  已自動重試過幾次。`_run_loop` 每輪掃到期的重新提交；`failed` 跟這個的差別是
  「已放棄、等使用者手動」vs「還會自己再試」（使用者 2026-09-06）。

**這一列什麼時候被刪掉——使用者定案**：只有兩種情況，
  (1) 番劇資料夾成功搬進下載目錄（＝「下載任務完成」，`_download_one` 內 `remove()` +
      `downloaded_episodes.record()`），
  (2) 使用者在下載列表頁按「丟棄」（`discard_failed`）。
碰到錯誤時**絕不**自動刪——不然使用者根本發現不了下載失敗過。之後排程週期自動重試
時，`mark_downloading` 只把 `stage` 翻回 `downloading`，`last_error`／`failure_count`
留著（重試又失敗 → `mark_failed` 用新錯誤蓋過；重試成功 → 整列 `remove()`）。
使用者中止（`abort_download`）也刪列，但那是使用者主動操作、不算「碰到錯誤」。
"""

from __future__ import annotations

import threading
from typing import Any

from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pending_downloads (
    video_sn INTEGER PRIMARY KEY,
    anime_sn INTEGER,
    anime_title TEXT NOT NULL DEFAULT '',
    display_name TEXT NOT NULL DEFAULT '',
    staged_dir TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'downloading',
    parent_folder TEXT NOT NULL DEFAULT '',
    created_at TEXT DEFAULT (datetime('now', 'localtime'))
)
"""

# 舊資料庫加欄（比照 store/downloaded_episodes.py 的 PRAGMA + ALTER）
_ADDED_COLUMNS = {
    "parent_folder": "TEXT NOT NULL DEFAULT ''",
    # stage='failed' 用：重啟後「最近失敗」卡片要能還原（使用者 2026-09-03）
    "last_error": "TEXT NOT NULL DEFAULT ''",
    "failure_count": "INTEGER NOT NULL DEFAULT 0",
    # stage='retry_pending' 用：動畫瘋 503 等站方暫時性錯誤時排進延長重試排程
    # （使用者 2026-09-06）。next_retry_at＝下一次該重試的絕對時間（ISO），
    # retry_attempt＝目前是第幾次（0 起算）。
    "next_retry_at": "TEXT NOT NULL DEFAULT ''",
    "retry_attempt": "INTEGER NOT NULL DEFAULT 0",
}

_STAGE_DOWNLOADING = "downloading"
_STAGE_AWAITING_MOVE = "awaiting_move"
_STAGE_FAILED = "failed"
_STAGE_RETRY_PENDING = "retry_pending"

_COLUMNS = (
    "video_sn, anime_sn, anime_title, display_name, staged_dir, stage, "
    "parent_folder, last_error, failure_count, next_retry_at, retry_attempt"
)


class PendingDownloadStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(pending_downloads)")}
            for name, decl in _ADDED_COLUMNS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE pending_downloads ADD COLUMN {name} {decl}")

    def mark_downloading(
        self,
        video_sn: int,
        *,
        anime_sn: int | None,
        anime_title: str,
        display_name: str,
        parent_folder: str = "",
    ) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "INSERT INTO pending_downloads "
                "(video_sn, anime_sn, anime_title, display_name, staged_dir, stage, parent_folder) "
                "VALUES (?, ?, ?, ?, '', ?, ?) "
                "ON CONFLICT(video_sn) DO UPDATE SET "
                "anime_sn=excluded.anime_sn, anime_title=excluded.anime_title, "
                "display_name=excluded.display_name, stage=excluded.stage, "
                "parent_folder=excluded.parent_folder",
                # 重試時只把 stage 翻回 downloading，**不清** last_error / failure_count——
                # 那份失敗資訊要留著（使用者：只有「下載成功」或使用者「丟棄」才該讓失敗
                # 紀錄消失，碰到錯誤時不能自動抹掉，不然使用者發現不了）。重試又失敗時
                # mark_failed() 會用最新的錯誤蓋過去；重試成功時整列會被 remove()。
                (
                    video_sn, anime_sn, anime_title or "", display_name or "",
                    _STAGE_DOWNLOADING, parent_folder or "",
                ),
            )

    def mark_awaiting_move(self, video_sn: int, staged_dir: str) -> None:
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "UPDATE pending_downloads SET stage = ?, staged_dir = ? WHERE video_sn = ?",
                (_STAGE_AWAITING_MOVE, staged_dir, video_sn),
            )

    def mark_failed(
        self,
        video_sn: int,
        *,
        error: str,
        failure_count: int,
        anime_sn: int | None = None,
        anime_title: str = "",
        display_name: str = "",
        parent_folder: str = "",
    ) -> None:
        """整集重試都失敗 → 標成 `failed`（而不是刪掉列）。列一定已經有（`mark_downloading`
        在提交下載時寫過），但還是走 upsert 保險——重啟續傳那條路萬一沒先 mark 也不會漏。"""
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "INSERT INTO pending_downloads "
                "(video_sn, anime_sn, anime_title, display_name, staged_dir, stage, "
                "parent_folder, last_error, failure_count) "
                "VALUES (?, ?, ?, ?, '', ?, ?, ?, ?) "
                "ON CONFLICT(video_sn) DO UPDATE SET "
                "stage=excluded.stage, last_error=excluded.last_error, "
                "failure_count=excluded.failure_count",
                (
                    video_sn, anime_sn, anime_title or "", display_name or "",
                    _STAGE_FAILED, parent_folder or "", error or "", max(1, int(failure_count or 1)),
                ),
            )

    def mark_retry_pending(
        self,
        video_sn: int,
        *,
        error: str,
        retry_attempt: int,
        next_retry_at: str,
        anime_sn: int | None = None,
        anime_title: str = "",
        display_name: str = "",
        parent_folder: str = "",
    ) -> None:
        """動畫瘋站方暫時性錯誤（503…）→ 排進延長重試排程（使用者 2026-09-06）。
        跟 `failed` 不一樣：`failed` 是「放棄了、等使用者手動」，這個是「還會自己再試」。
        `retry_attempt`＝目前已經自動重試過幾次（呼叫端傳確切值，這裡不再自己加）；
        `failure_count` 直接等於它，下載列表卡片才看得出試過幾次。"""
        n = max(1, int(retry_attempt))
        with self._lock, self._database.transaction() as conn:
            conn.execute(
                "INSERT INTO pending_downloads "
                "(video_sn, anime_sn, anime_title, display_name, staged_dir, stage, "
                "parent_folder, last_error, failure_count, next_retry_at, retry_attempt) "
                "VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(video_sn) DO UPDATE SET "
                "stage=excluded.stage, last_error=excluded.last_error, "
                "failure_count=excluded.failure_count, next_retry_at=excluded.next_retry_at, "
                "retry_attempt=excluded.retry_attempt",
                (
                    video_sn, anime_sn, anime_title or "", display_name or "",
                    _STAGE_RETRY_PENDING, parent_folder or "", error or "",
                    n, next_retry_at, n,
                ),
            )

    def list_retry_pending(self) -> list[dict[str, Any]]:
        return self._list_by_stage(_STAGE_RETRY_PENDING)

    def remove(self, video_sn: int) -> int:
        with self._lock, self._database.transaction() as conn:
            return conn.execute(
                "DELETE FROM pending_downloads WHERE video_sn = ?", (video_sn,)
            ).rowcount

    def get(self, video_sn: int) -> dict[str, Any] | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM pending_downloads WHERE video_sn = ?",
                (video_sn,),
            ).fetchone()
        return dict(row) if row is not None else None

    def list_downloading(self) -> list[dict[str, Any]]:
        return self._list_by_stage(_STAGE_DOWNLOADING)

    def list_awaiting_move(self) -> list[dict[str, Any]]:
        return self._list_by_stage(_STAGE_AWAITING_MOVE)

    def list_failed(self) -> list[dict[str, Any]]:
        return self._list_by_stage(_STAGE_FAILED)

    def _list_by_stage(self, stage: str) -> list[dict[str, Any]]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM pending_downloads WHERE stage = ? ORDER BY created_at",
                (stage,),
            ).fetchall()
        return [dict(row) for row in rows]
