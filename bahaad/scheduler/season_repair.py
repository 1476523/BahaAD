"""版本更新後自動把「掉在季別資料夾外」的番劇資料夾搬進季別資料夾（使用者 2026-10-06）。

背景：新番轉換出問題、手動下載剛首播的番劇時，舊版把 `totalEpisode=1` 的連續劇誤判成電影，
下載成 `下載目錄/<番劇名>/…`（沒有季別資料夾）。修好之後，這支當成資料庫版本檢查
（`store/maintenance.py`）的一個「更改」步驟，把這些既有資料夾搬到正確位置。

保守原則：只動「直接放在下載目錄底下」的資料夾；排程項目有設分類（tag）的不動；
從本機快取的番劇頁資料才能算出季別（沒快取就跳過，不打網路）；真正的老電影／單集特別篇
維持原位；目標已有同名檔案就不覆蓋；每個檔案各自搬、更新資料庫路徑。
"""

from __future__ import annotations

import logging
import shutil
from datetime import date
from pathlib import Path

from bahaad.store.database import Database
from bahaad.store.maintenance import Step

logger = logging.getLogger(__name__)


def _entry_has_tag(schedule_store, anime_cache, video_sn: int) -> bool:
    try:
        entries = schedule_store.get_entries()
        group = anime_cache.group_key_for(video_sn)
        members = anime_cache.group_members(group) if group is not None else {video_sn}
    except Exception:  # noqa: BLE001
        return True  # 查不出來就不動，寧可不搬
    members = set(members) | {video_sn}
    return any(entry.tag for sn, entry in entries.items() if sn in members)


def _target_folder(settings, anime_cache, video_sn: int, today: date) -> str:
    from bahaad.scheduler.main_loop import DEFAULT_AUTO_SEASON_FOLDER, DEFAULT_AUTO_SEASON_YEAR
    from bahaad.scheduler.seasons import season_folder

    if not settings.get("auto_season_folder", DEFAULT_AUTO_SEASON_FOLDER):
        return ""
    found = anime_cache.get_detail_with_sibling_fallback(video_sn)
    if not found:
        return ""
    detail = found[0]
    # 只修「首播不久」的：首播很久以前的（例如去年首播、今年接著播的舊番）無法只憑首播日
    # 判斷檔案屬於哪一季，搬錯比不搬更糟，維持原位
    from bahaad.scheduler.seasons import _CARRYOVER_DAYS, _parse_date

    start = _parse_date(detail.air_date or "")
    if start is None or (today - start).days > _CARRYOVER_DAYS:
        return ""
    total = sum(len(category.episodes) for category in (detail.episode_categories or []))
    return season_folder(
        detail.air_date or "",
        total_episode=total or None,
        include_year=bool(settings.get("auto_season_year_folder", DEFAULT_AUTO_SEASON_YEAR)),
        today=today,
    )


def move_misplaced_folders(
    database: Database, settings, anime_cache, schedule_store, *, today: date | None = None
) -> int:
    from bahaad.scheduler.main_loop import _DEFAULT_DOWNLOAD_DIR, _parent_folder_path

    root = Path(settings.get("download_dir", _DEFAULT_DOWNLOAD_DIR))
    if not root.is_dir() or anime_cache is None:
        return 0
    today = today or date.today()
    with database.transaction() as conn:
        rows = conn.execute(
            "SELECT video_sn, file_path FROM downloaded_episodes WHERE removed_at IS NULL"
        ).fetchall()

    by_folder: dict[str, list] = {}
    for row in rows:
        try:
            rel = Path(row["file_path"]).relative_to(root)
        except ValueError:
            continue
        if len(rel.parts) == 2:  # 下載目錄/<番劇資料夾>/<檔案>：沒有季別／分類父資料夾
            by_folder.setdefault(rel.parts[0], []).append(row)

    moved = 0
    for folder_name, folder_rows in by_folder.items():
        try:
            sample_sn = folder_rows[0]["video_sn"]
            if any(_entry_has_tag(schedule_store, anime_cache, r["video_sn"]) for r in folder_rows[:1]):
                continue
            parent = _target_folder(settings, anime_cache, sample_sn, today)
            if not parent:
                continue
            source = root / folder_name
            target = root / _parent_folder_path(parent) / folder_name
            if source == target or not source.is_dir():
                continue
            target.mkdir(parents=True, exist_ok=True)
            for item in list(source.iterdir()):
                destination = target / item.name
                if destination.exists():
                    continue
                shutil.move(str(item), str(destination))
                with database.transaction() as conn:
                    conn.execute(
                        "UPDATE downloaded_episodes SET file_path = ? WHERE file_path = ?",
                        (str(destination), str(item)),
                    )
            if not any(source.iterdir()):
                source.rmdir()
            moved += 1
            logger.info("已把《%s》搬進季別資料夾 %s", folder_name, parent)
        except Exception:  # noqa: BLE001 - 單一番劇搬失敗不影響其他
            logger.exception("搬動資料夾失敗：%s", folder_name)
    return moved


def build_step(settings, anime_cache, schedule_store) -> Step:
    return Step(
        "change_move_misplaced_season_folders", "change", "把掉在季別資料夾外的番劇資料夾搬進去",
        lambda database, _settings: move_misplaced_folders(database, settings, anime_cache, schedule_store),
    )


# ---------------------------------------------------------------------------
# 設定頁「季度位置設定」：手動指定訂閱番劇的季別／年分資料夾，並把已下載的檔案搬過去
# ---------------------------------------------------------------------------

SEASON_LABELS = ("一月新番", "四月新番", "七月新番", "十月新番")


def tag_for(year: str, season: str) -> str | None:
    """年分＋季別 → 排程分類（資料夾）字串；沒選季別＝None（回到自動）。"""
    if season not in SEASON_LABELS:
        return None
    year = (year or "").strip()
    return f"{year}/{season}" if year.isdigit() and len(year) == 4 else season


def split_tag(tag: str | None) -> tuple[str, str]:
    """分類字串 → (年分, 季別)；不是「年/季別」或「季別」格式（自訂分類）回 ("", "")。"""
    if not tag:
        return "", ""
    parts = tag.split("/")
    if parts[-1] in SEASON_LABELS and (len(parts) == 1 or (len(parts) == 2 and parts[0].isdigit())):
        return (parts[0] if len(parts) == 2 else ""), parts[-1]
    return "", ""


def anime_download_folders(database: Database, anime_cache, root: Path, sn: int) -> set[Path]:
    """這部番劇（訂閱的 sn 所屬的整部番劇）已下載檔案目前所在的資料夾集合。"""
    try:
        group = anime_cache.group_key_for(sn) if anime_cache is not None else None
        members = set(anime_cache.group_members(group)) if group is not None else set()
    except Exception:  # noqa: BLE001
        members = set()
    members.add(sn)
    marks = ",".join("?" for _ in members)
    with database.transaction() as conn:
        rows = conn.execute(
            f"SELECT file_path FROM downloaded_episodes WHERE removed_at IS NULL "
            f"AND video_sn IN ({marks})", tuple(members),
        ).fetchall()
    folders = set()
    for row in rows:
        path = Path(row["file_path"])
        try:
            path.relative_to(root)
        except ValueError:
            continue
        folders.add(path.parent)
    return folders


def relocate_folders(database: Database, root: Path, folders: set[Path], parent: str) -> int:
    """把 `folders`（番劇資料夾）整個搬到 `root/<parent>/<資料夾名>`；回傳搬動的資料夾數。"""
    from bahaad.scheduler.main_loop import _parent_folder_path

    moved = 0
    for source in sorted(folders):
        target = root / _parent_folder_path(parent) / source.name
        if source == target or not source.is_dir():
            continue
        target.mkdir(parents=True, exist_ok=True)
        for item in list(source.iterdir()):
            destination = target / item.name
            if destination.exists():
                continue
            shutil.move(str(item), str(destination))
            with database.transaction() as conn:
                conn.execute(
                    "UPDATE downloaded_episodes SET file_path = ? WHERE file_path = ?",
                    (str(destination), str(item)),
                )
        if not any(source.iterdir()):
            source.rmdir()
        moved += 1
    return moved


def first_downloaded_at(database: Database, anime_cache, sn: int) -> str:
    """這部番劇（整部）最早一筆下載紀錄的時間（當作「首次新增日期」）；沒有回空字串。"""
    try:
        group = anime_cache.group_key_for(sn) if anime_cache is not None else None
        members = set(anime_cache.group_members(group)) if group is not None else set()
    except Exception:  # noqa: BLE001
        members = set()
    members.add(sn)
    marks = ",".join("?" for _ in members)
    with database.transaction() as conn:
        row = conn.execute(
            f"SELECT MIN(downloaded_at) AS first FROM downloaded_episodes WHERE video_sn IN ({marks})",
            tuple(members),
        ).fetchone()
    return (row["first"] or "") if row else ""
