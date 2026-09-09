"""已下載集數的紀錄。規格見 docs/requirements/round6.md 第 11、12、14 項。

原本「這一集下載過沒」是掃輸出目錄的檔名裡有沒有 `[video_sn]`（`scheduler_main_loop.md`
「以檔案系統為準」），刻意不建 DB 表。但 round 6 第 14 項讓使用者自訂檔名模板後，檔名
不再保證含 sn（甚至不含集數），檔名比對做不下去；而且下載頁的封面靠「最小的 video_sn」
去猜番劇，猜錯就顯示成別部番劇（第 12 項）。

改法：一張正規化的表，每欄一個明確的值。**保留「刪掉檔案就會重抓」的原始意圖**——
`is_downloaded()` 查到紀錄後還會確認 `file_path` 真的存在，檔案被使用者刪掉就當沒下載過。

儲存原則（使用者 2026-08-26 定案）：一個用途一張表、不用 JSON blob。這張表也是
「資料庫整頓」（round2 階段 5）要清的孤兒資料來源（`sns_with_records()` /
`delete_for_anime_sn()`）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

from bahaad.store.database import Database

logger = logging.getLogger(__name__)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS downloaded_episodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_sn INTEGER NOT NULL UNIQUE,
    anime_sn INTEGER,
    anime_title TEXT NOT NULL,
    cover_url TEXT,
    file_path TEXT NOT NULL,
    downloaded_at TEXT NOT NULL
)
"""

# round 7 第 7 項：檔案被使用者刪掉後不再硬刪紀錄，改標記 removed_at——番劇詳細頁的
# 集數選取器對這些集數顯示紫框（曾下載過、檔案已不在），「資料庫整頓」頁有「刪除歷史」
# 讓使用者手動清。（store/identity.py 的 PRAGMA table_info + ALTER 做法，idempotent）
# display_name：下載當下的資料夾／檔案名（＝使用者更名 或 乾淨標題）。`anime_title` 一律
# 存原始標題（集數選取器藍框靠它比對，不能動），「已下載的番劇」清單顯示改用 display_name
# ——不然資料夾／檔案是更名後的、清單卻還印原始標題（使用者 2026-09-04）。
_ADDED_COLUMNS = {"removed_at": "TEXT", "display_name": "TEXT"}


class DownloadedEpisodeStore:
    def __init__(
        self, database: Database, *, on_removed: Callable[[int], None] | None = None
    ) -> None:
        self._database = database
        self._lock = threading.Lock()
        # 偵測到某一集的檔案已被使用者刪掉、標記 removed_at 時呼叫（`app_shell` 接
        # `hls_cache.discard`——原始 mp4 沒了就一併清掉那一集的 HLS 快取）。
        self._on_removed = on_removed
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(downloaded_episodes)")}
            for name, decl in _ADDED_COLUMNS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE downloaded_episodes ADD COLUMN {name} {decl}")

    def record(
        self,
        video_sn: int,
        anime_title: str,
        file_path: str,
        anime_sn: int | None = None,
        cover_url: str | None = None,
        display_name: str | None = None,
    ) -> None:
        """下載成功後記一筆。同一 `video_sn` 重下就更新（`ON CONFLICT` 覆蓋路徑/時間，
        並清掉 `removed_at`——重下＝不再是「已移除」狀態）。`display_name`＝下載當下的
        資料夾／檔案名（更名或標題），沒帶就退回 `anime_title`。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO downloaded_episodes "
                    "(video_sn, anime_sn, anime_title, display_name, cover_url, file_path, downloaded_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(video_sn) DO UPDATE SET "
                    "anime_sn=excluded.anime_sn, anime_title=excluded.anime_title, "
                    "display_name=excluded.display_name, "
                    "cover_url=excluded.cover_url, file_path=excluded.file_path, "
                    "downloaded_at=excluded.downloaded_at, removed_at=NULL",
                    (
                        video_sn,
                        anime_sn,
                        anime_title,
                        display_name or anime_title,
                        cover_url,
                        file_path,
                        datetime.now().isoformat(timespec="seconds"),
                    ),
                )

    def is_downloaded(self, video_sn: int) -> bool:
        """查到紀錄、且 `file_path` 真的還在磁碟上、且沒被標記為已移除，才算已下載——
        檔案被刪掉就當沒下載過（下次排程檢查會重抓），並把該筆標記 `removed_at`
        （不硬刪，留給集數選取器顯示紫框、「資料庫整頓」的刪除歷史）。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT file_path, removed_at FROM downloaded_episodes WHERE video_sn = ?", (video_sn,)
            ).fetchone()
        if row is None or row["removed_at"] is not None:
            return False
        if Path(row["file_path"]).exists():
            return True
        self._mark_removed(video_sn)
        return False

    def anime_title_for_episode(self, video_sn: int) -> str | None:
        """這一集屬於的番劇原始標題（公開模式的「可見番劇」白名單用）。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT anime_title FROM downloaded_episodes WHERE video_sn = ?", (video_sn,)
            ).fetchone()
        return row["anime_title"] if row is not None else None

    def playable_path(self, video_sn: int) -> str | None:
        """已下載、沒被移除、且檔案還在磁碟上 → 回傳本地檔案路徑（瀏覽器內建播放器用）；
        否則回 None（檔案不在的話順手標記 removed）。使用者 2026-09-04。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT file_path, removed_at FROM downloaded_episodes WHERE video_sn = ?", (video_sn,)
            ).fetchone()
        if row is None or row["removed_at"] is not None:
            return None
        if Path(row["file_path"]).exists():
            return row["file_path"]
        self._mark_removed(video_sn)
        return None

    def _mark_removed(self, video_sn: int) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                cursor = conn.execute(
                    "UPDATE downloaded_episodes SET removed_at = ? "
                    "WHERE video_sn = ? AND removed_at IS NULL",
                    (datetime.now().isoformat(timespec="seconds"), video_sn),
                )
                newly_removed = cursor.rowcount > 0
        # 真的是「這次才從有變無」才通知（鎖外呼叫，回呼慢／出錯都不影響這裡）
        if newly_removed and self._on_removed is not None:
            try:
                self._on_removed(video_sn)
            except Exception:  # noqa: BLE001 - 回呼失敗不能讓「標記已移除」這件事失敗
                logger.debug("downloaded_episodes on_removed 回呼發生例外（sn=%s）", video_sn, exc_info=True)

    def downloaded_video_sns_for_title(self, anime_title: str) -> set[int]:
        """這部番劇底下、檔案還在的已下載 video_sn（集數選取器藍框用）。用 `anime_title`
        欄位查、不靠檔案路徑前綴——季別資料夾功能讓路徑多了一層，而且 `record()` 存的路徑
        是 `entry.rename or 標題`、跟前綴用的標題本來就對不上（舊 bug）。

        檔案已被使用者刪掉的順手標記 `removed_at`——這樣 `removed_video_sns_for_title()`
        下一次（同一輪 `_episode_states` 就會呼叫）就能把那一格從「無色」變成紫框，不用
        離開頁面重進（使用者 2026-09-04）。"""
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT video_sn, file_path FROM downloaded_episodes "
                "WHERE anime_title = ? AND removed_at IS NULL",
                (anime_title,),
            ).fetchall()
        present: set[int] = set()
        for row in rows:
            if Path(row["file_path"]).exists():
                present.add(row["video_sn"])
            else:
                self._mark_removed(row["video_sn"])
        return present

    def removed_video_sns_for_title(self, anime_title: str) -> set[int]:
        """這部番劇「曾下載過、但檔案已被移除」的 video_sn（集數選取器紫框用，round 7 第 7 項）。"""
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT video_sn FROM downloaded_episodes "
                "WHERE anime_title = ? AND removed_at IS NOT NULL",
                (anime_title,),
            ).fetchall()
        return {row["video_sn"] for row in rows}

    def list_by_anime(self) -> list[dict]:
        """下載頁「已下載的番劇」用：一部番劇一列（用 anime_sn 聚合，沒有 anime_sn 的
        退回用 anime_title），帶集數與一個代表 video_sn（呼叫端拿去解封面/連詳細頁）。
        只算檔案還在、沒被標記移除的；檔案已被刪的順手標記 removed_at（不硬刪）。"""
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT video_sn, anime_sn, anime_title, display_name, cover_url, file_path, downloaded_at "
                "FROM downloaded_episodes WHERE removed_at IS NULL ORDER BY downloaded_at DESC"
            ).fetchall()
        groups: dict[object, dict] = {}
        stale: list[int] = []
        for row in rows:
            if not Path(row["file_path"]).exists():
                stale.append(row["video_sn"])
                continue
            key = row["anime_sn"] if row["anime_sn"] is not None else row["anime_title"]
            g = groups.setdefault(
                key,
                {
                    "anime_sn": row["anime_sn"],
                    # 資料夾／檔案是更名後的名字，清單也顯示這個（使用者 2026-09-04）
                    "title": row["display_name"] or row["anime_title"],
                    # 原始站方標題——公開模式的「可見番劇」白名單用它當 key（穩定、
                    # 跟訂閱／番劇頁對得上）
                    "anime_title": row["anime_title"],
                    "cover_url": row["cover_url"] or "",
                    "sample_video_sn": row["video_sn"],
                    "episode_count": 0,
                    "latest_at": row["downloaded_at"],
                },
            )
            g["episode_count"] += 1
            if not g["cover_url"] and row["cover_url"]:
                g["cover_url"] = row["cover_url"]
        for video_sn in stale:
            self._mark_removed(video_sn)
        return sorted(groups.values(), key=lambda g: g["latest_at"], reverse=True)

    def list_removed(self) -> list[dict]:
        """「資料庫整頓」的「刪除歷史」區塊：曾下載過、但檔案已被移除的集數。"""
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT video_sn, anime_sn, anime_title, file_path, downloaded_at, removed_at "
                "FROM downloaded_episodes WHERE removed_at IS NOT NULL ORDER BY removed_at DESC"
            ).fetchall()
        return [
            {
                "video_sn": row["video_sn"],
                "anime_sn": row["anime_sn"],
                "anime_title": row["anime_title"],
                "file_path": row["file_path"],
                "downloaded_at": row["downloaded_at"],
                "removed_at": row["removed_at"],
            }
            for row in rows
        ]

    def clear_removed(self, video_sn: int | None = None) -> int:
        """硬刪「已移除」的紀錄——使用者在「資料庫整頓」的刪除歷史手動清。`video_sn`
        給定就只清那一筆，否則清全部。回傳刪掉幾列。"""
        with self._lock:
            with self._database.transaction() as conn:
                if video_sn is None:
                    cursor = conn.execute(
                        "DELETE FROM downloaded_episodes WHERE removed_at IS NOT NULL"
                    )
                else:
                    cursor = conn.execute(
                        "DELETE FROM downloaded_episodes WHERE video_sn = ? AND removed_at IS NOT NULL",
                        (video_sn,),
                    )
                return cursor.rowcount

    def sns_with_records(self) -> set[int]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT DISTINCT anime_sn FROM downloaded_episodes WHERE anime_sn IS NOT NULL"
            ).fetchall()
        return {row["anime_sn"] for row in rows}

    def count_for_anime_sn(self, anime_sn: int) -> int:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM downloaded_episodes WHERE anime_sn = ?", (anime_sn,)
            ).fetchone()
        return int(row["n"])

    def delete_for_anime_sn(self, anime_sn: int) -> int:
        """清掉一部番劇的所有紀錄（「資料庫整頓」用）。不動實際檔案。回傳刪掉幾列。"""
        with self._lock:
            with self._database.transaction() as conn:
                cursor = conn.execute(
                    "DELETE FROM downloaded_episodes WHERE anime_sn = ?", (anime_sn,)
                )
                return cursor.rowcount
