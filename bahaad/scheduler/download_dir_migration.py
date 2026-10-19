"""更改下載目錄時，把已下載的檔案搬到新目錄。規格見
docs/requirements/download_dir_migration.md。

核心設計是「複製到新目錄 → 驗證 → 才切換 `downloaded_episodes.file_path`」，不是直接
搬移（`os.rename`／`shutil.move`）——複製完的新檔案先驗證過雜湊、資料庫也切過去了，
舊檔案才在最後一輪整批驗證通過後刪除。整個流程刻意設計成**無狀態、可重複執行**：
`SettingsStore` 只記 `old_dir`／`new_dir`／`phase` 三個字串，其餘進度（哪些搬了、哪些
還沒、哪些失敗）都是「重新掃一次 `downloaded_episodes` 現況 + 檢查磁碟上的檔案」當場
算出來的，不是另外存一份清單——程式中途被關掉重開，下一次呼叫 `resume_if_pending()`
接著跑，會自動跳過已經搬完的、重試還沒搬完的，不會重複複製、也不會漏掉。
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from bahaad.store.downloaded_episodes import DownloadedEpisodeStore
from bahaad.store.settings import SettingsStore

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "_download_dir_migration"
_POLL_INTERVAL_SECONDS = 5.0


def _resolve(path: str) -> Path | None:
    try:
        return Path(path).resolve()
    except OSError:
        return None


def _under_dir(path: str, directory: str) -> bool:
    """`path` 是不是在 `directory` 底下（含子資料夾）。用 `Path.resolve()` 正規化比較，
    避免 Windows 大小寫／短路徑／斜線方向造成誤判。"""
    p, d = _resolve(path), _resolve(directory)
    if p is None or d is None:
        return False
    return p == d or d in p.parents


def _compute_new_path(old_file_path: str, old_dir: str, new_dir: str) -> str | None:
    """把 `old_file_path` 相對 `old_dir` 的部分原封不動搬到 `new_dir` 底下（維持目錄
    結構／檔名，只換根目錄）。不在 `old_dir` 底下的路徑回傳 `None`。"""
    old_p, old_d = _resolve(old_file_path), _resolve(old_dir)
    if old_p is None or old_d is None:
        return None
    try:
        rel = old_p.relative_to(old_d)
    except ValueError:
        return None
    return str(Path(new_dir) / rel)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class MigrationPreview:
    count: int
    total_size_bytes: int


def scan_preview(episode_store: DownloadedEpisodeStore, old_dir: str, new_dir: str) -> MigrationPreview:
    """觸發前的確認視窗用：目前有幾筆、大約多少容量需要搬。只讀不動任何東西。"""
    count = 0
    total = 0
    for row in episode_store.list_for_migration():
        if not _under_dir(row["file_path"], old_dir) or _under_dir(row["file_path"], new_dir):
            continue
        count += 1
        try:
            total += Path(row["file_path"]).stat().st_size
        except OSError:
            pass  # 檔案量測失敗不影響計數，只是總容量估得少一點
    return MigrationPreview(count=count, total_size_bytes=total)


@dataclass
class MigrationStatus:
    active: bool
    phase: str | None = None  # "copying" | "final_verify" | None（沒有進行中的遷移）
    old_dir: str | None = None
    new_dir: str | None = None
    migrated_count: int = 0
    pending_count: int = 0
    failed_video_sns: list[int] = field(default_factory=list)
    finished: bool = False  # 這一輪剛結束（給前端顯示一次結果用，讀過一次就消失）


def _migrate_one_file(
    old_path_str: str, new_path_str: str, video_sn: int, episode_store: DownloadedEpisodeStore
) -> bool:
    """複製＋驗證＋切路徑單筆流程。已經複製過且驗證得過的（上一輪中斷留下的）直接
    跳過複製、只確保資料庫真的切過去；驗證不過的殘留檔案清掉重來。任何一步失敗都回
    `False`，不留半個檔案、不切路徑。"""
    old_p, new_p = Path(old_path_str), Path(new_path_str)
    if not old_p.exists():
        return False  # 來源在遷移過程中被移走／刪掉了，交給既有 is_downloaded() 邏輯處理

    if new_p.exists():
        try:
            same_size = old_p.stat().st_size == new_p.stat().st_size
            same_hash = same_size and sha256_file(old_p) == sha256_file(new_p)
        except OSError:
            same_hash = False
        if same_hash:
            if not episode_store.update_file_path(video_sn, str(new_p)):
                return False  # 資料庫那筆已經被動過（見下方完整流程說明），新檔案留著沒意義
            return True
        try:
            new_p.unlink()
        except OSError:
            logger.warning("download_dir_migration：清掉損壞的殘留檔案失敗（%s）", new_p, exc_info=True)
            return False

    try:
        new_p.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(old_p, new_p)  # copy2 保留 mtime，HLS 快取的 _stamp() 才不會被誤判成來源變了
        if old_p.stat().st_size != new_p.stat().st_size or sha256_file(old_p) != sha256_file(new_p):
            raise ValueError("複製後檔案大小/雜湊對不上")
    except (OSError, ValueError):
        logger.warning("download_dir_migration：複製/驗證失敗（video_sn=%s）", video_sn, exc_info=True)
        try:
            new_p.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    if not episode_store.update_file_path(video_sn, str(new_p)):
        # 複製這段期間，這筆紀錄被別的流程改掉了（例如使用者手動觸發重新下載、或
        # 檔案被刪除後標記了 removed_at）——資料庫已經不指向這份新複製，留著是孤兒
        # 檔案，直接清掉；舊檔案不動，交給原本那個改動它的流程自己負責。
        try:
            new_p.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


def _final_verify_and_cleanup(
    episode_store: DownloadedEpisodeStore, old_dir: str, new_dir: str
) -> list[int]:
    """收尾：對每一筆「新路徑底下、舊目錄還留著對應原始檔案」的紀錄，重新核對
    （1）資料庫**當下**真的指向新路徑（不是遷移過程中被誰改掉）、（2）新舊兩份檔案
    雜湊仍然一致，兩項都過才刪舊檔案；任一項對不上算這筆失敗，舊檔案保留、不刪。
    回傳失敗的 video_sn 清單。"""
    failed: list[int] = []
    for row in episode_store.list_for_migration():
        video_sn, current_path = row["video_sn"], row["file_path"]
        if not _under_dir(current_path, new_dir):
            continue  # 不是這輪遷移搬過去的（本來就在新目錄，或跟這次遷移無關）
        old_path = _compute_new_path(current_path, new_dir, old_dir)
        if old_path is None or not Path(old_path).exists():
            continue  # 舊檔案已經不在了（可能上一輪就刪過），沒有東西要清

        fresh_path = episode_store.get_file_path_raw(video_sn)
        if fresh_path != current_path:
            failed.append(video_sn)
            continue
        new_p = Path(current_path)
        if not new_p.exists():
            failed.append(video_sn)
            continue
        try:
            if sha256_file(Path(old_path)) != sha256_file(new_p):
                failed.append(video_sn)
                continue
        except OSError:
            failed.append(video_sn)
            continue

        try:
            Path(old_path).unlink()
        except OSError:
            logger.warning("download_dir_migration：刪除舊檔案失敗（%s）", old_path, exc_info=True)
            failed.append(video_sn)
    return failed


class DownloadDirMigration:
    def __init__(
        self,
        episode_store: DownloadedEpisodeStore,
        settings: SettingsStore,
        registry,
        *,
        on_progress: Callable[[MigrationStatus], None] | None = None,
        on_finished: Callable[[MigrationStatus], None] | None = None,
    ) -> None:
        self._episode_store = episode_store
        self._settings = settings
        self._registry = registry
        self._on_progress = on_progress
        self._on_finished = on_finished
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._status_lock = threading.Lock()
        self._last_status = MigrationStatus(active=False)

    def preview(self, old_dir: str, new_dir: str) -> MigrationPreview:
        return scan_preview(self._episode_store, old_dir, new_dir)

    def status(self) -> MigrationStatus:
        with self._status_lock:
            return self._last_status

    def _set_status(self, status: MigrationStatus, *, notify_finished: bool = False) -> None:
        with self._status_lock:
            self._last_status = status
        if notify_finished and self._on_finished is not None:
            try:
                self._on_finished(status)
            except Exception:  # noqa: BLE001 - 回呼失敗不能讓遷移流程掛掉
                logger.debug("download_dir_migration on_finished 回呼發生例外", exc_info=True)
        elif self._on_progress is not None:
            try:
                self._on_progress(status)
            except Exception:  # noqa: BLE001
                logger.debug("download_dir_migration on_progress 回呼發生例外", exc_info=True)

    def is_active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, old_dir: str, new_dir: str) -> bool:
        """觸發一輪新的遷移。已經有一輪在跑就拒絕（規格：不接受疊加/排隊）。"""
        with self._lock:
            if self.is_active():
                return False
            self._settings.update({_SETTINGS_KEY: {"old_dir": old_dir, "new_dir": new_dir, "phase": "copying"}})
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run, args=(old_dir, new_dir), daemon=True, name="download-dir-migration"
            )
            self._thread.start()
            return True

    def resume_if_pending(self) -> bool:
        """程式啟動時呼叫一次：上次沒跑完的遷移（程式中途被關掉）接著跑。"""
        pending = self._settings.get(_SETTINGS_KEY)
        if not pending or not isinstance(pending, dict):
            return False
        old_dir, new_dir = pending.get("old_dir"), pending.get("new_dir")
        if not old_dir or not new_dir:
            self._settings.reset([_SETTINGS_KEY])
            return False
        logger.info("download_dir_migration：偵測到上次未完成的遷移，接續執行（%s → %s）", old_dir, new_dir)
        return self.start(old_dir, new_dir)

    def stop(self) -> None:
        """程式關閉時呼叫——只是讓背景執行緒盡快結束當前這一輪迴圈，不會中斷正在
        進行的單筆複製（複製本身不長，讓它自然做完比硬中斷安全）。下次啟動
        `resume_if_pending()` 會接著跑。"""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None

    def _run(self, old_dir: str, new_dir: str) -> None:
        try:
            migrated_count = self._run_copy_phase(old_dir, new_dir)
            if self._stop_event.is_set():
                return
            self._settings.update({_SETTINGS_KEY: {"old_dir": old_dir, "new_dir": new_dir, "phase": "final_verify"}})
            failed = _final_verify_and_cleanup(self._episode_store, old_dir, new_dir)
            self._settings.reset([_SETTINGS_KEY])
            self._set_status(
                MigrationStatus(active=False, phase=None, old_dir=old_dir, new_dir=new_dir,
                                 migrated_count=migrated_count, failed_video_sns=failed, finished=True),
                notify_finished=True,
            )
        except Exception:  # noqa: BLE001 - 背景執行緒任何未預期例外都不能讓它悄悄死掉
            logger.exception("download_dir_migration 執行時發生未預期的例外")
            self._set_status(MigrationStatus(active=False, phase=None, finished=True), notify_finished=True)

    def _run_copy_phase(self, old_dir: str, new_dir: str) -> int:
        """回傳這輪真的搬成功的筆數——`_run()` 組收尾狀態時要用，不能讓「複製階段」
        自己內部的計數器就這樣消失，不然結果畫面會永遠顯示「成功搬移 0 部」，即使
        實際上全部都搬成功了（2026-09-24 用瀏覽器實測才抓到：檔案跟資料庫都正確
        搬過去、舊檔案也正確刪除，只有回報給使用者看的數字是錯的）。"""
        failed_this_run: set[int] = set()
        migrated_count = 0
        while not self._stop_event.is_set():
            rows = self._episode_store.list_for_migration()
            pending = [
                r for r in rows
                if _under_dir(r["file_path"], old_dir) and not _under_dir(r["file_path"], new_dir)
                and r["video_sn"] not in failed_this_run
            ]
            if not pending:
                break
            made_progress = False
            for row in pending:
                if self._stop_event.is_set():
                    return migrated_count
                video_sn = row["video_sn"]
                if self._registry is not None and self._registry.is_active(video_sn):
                    continue  # 還在下載中，等它完成後下一輪再處理
                made_progress = True
                new_path = _compute_new_path(row["file_path"], old_dir, new_dir)
                if new_path is None:
                    failed_this_run.add(video_sn)
                    continue
                if _migrate_one_file(row["file_path"], new_path, video_sn, self._episode_store):
                    migrated_count += 1
                else:
                    failed_this_run.add(video_sn)
                self._set_status(MigrationStatus(
                    active=True, phase="copying", old_dir=old_dir, new_dir=new_dir,
                    migrated_count=migrated_count, pending_count=len(pending) - migrated_count,
                    failed_video_sns=sorted(failed_this_run),
                ))
            if not made_progress:
                self._stop_event.wait(_POLL_INTERVAL_SECONDS)
        return migrated_count
