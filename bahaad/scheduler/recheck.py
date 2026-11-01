"""「重新檢查排程更新」的協調器。規格見 docs/requirements/web_redesign_round2.md 階段 4。

使用者按下「重新檢查排程更新」→ 這個協調器起一條背景執行緒，對所有訂閱項目（或指定
的單一部）跑 `MainLoop.list_new_episodes()`（只檢查、不下載），把找到的新集數收集起來，
交給前端讓使用者**逐項**選「下載」或「標記為已下載」。確認後對「下載」的呼叫
`trigger_manual_download()`、對「標記已下載」的寫進 `SkippedEpisodeStore`。

為什麼要背景執行緒＋輪詢，而不是一個同步的長請求：`app_shell.py` 用
`werkzeug.serving.make_server()` 是**單執行緒**的，一個跑好幾分鐘的同步 recheck 請求會
把整個網頁介面卡住。

狀態機（`status()` 回給前端輪詢）：
  idle → checking → results_ready → downloading → idle
                          ↓（沒找到新集數）        ↑（提交的下載都完成）
                        idle                       idle
`results_ready` 停留超過 30 分鐘沒確認 → `recheck_pause` 的 timeout 會清掉暫停旗標，
下一次 `status()` 發現就把狀態退回 idle（＝自動取消）。
"""

from __future__ import annotations

import logging
import threading

from bahaad.registry import DownloadRegistry
from bahaad.scheduler import recheck_pause
from bahaad.scheduler.main_loop import DownloadTrigger, MainLoop
from bahaad.store.schedule_list import ScheduleListStore
from bahaad.store.settings import SettingsStore
from bahaad.store.skipped_episodes import SkippedEpisodeStore

logger = logging.getLogger(__name__)

STATE_IDLE = "idle"
STATE_CHECKING = "checking"
STATE_RESULTS_READY = "results_ready"
STATE_DOWNLOADING = "downloading"
STATE_ERROR = "error"


class RecheckCoordinator:
    def __init__(
        self,
        schedule_store: ScheduleListStore,
        main_loop: MainLoop,
        registry: DownloadRegistry,
        settings: SettingsStore,
        skipped_episode_store: SkippedEpisodeStore,
    ) -> None:
        self._schedule_store = schedule_store
        self._main_loop = main_loop
        self._registry = registry
        self._settings = settings
        self._skipped = skipped_episode_store

        self._lock = threading.Lock()
        self._state = STATE_IDLE
        self._thread: threading.Thread | None = None
        self._checked = 0
        self._total = 0
        # [{"sn", "anime_title", "episodes": [{"video_sn", "episode_label"}]}]
        self._found: list[dict] = []
        self._error: str | None = None
        self._downloading_sns: set[int] = set()
        # confirm() 的「逐集 trigger_manual_download()」在背景執行緒跑（每一集一次
        # get_video() 網路請求，選很多集時會卡住單執行緒的 web）——這段期間為 True，
        # status() 不要因為「還沒有任何 sn 在 registry 裡」就誤判下載已全部完成。
        self._submitting = False

    # ------------------------------------------------------------------
    # 使用者觸發的動作
    # ------------------------------------------------------------------

    def start_check(self, video_sn: int | None = None) -> bool:
        """啟動一輪檢查。`video_sn=None` → 檢查所有訂閱項目；否則只檢查那一部。
        已經有檢查／下載在進行中就回傳 False（前端顯示提示）。"""
        with self._lock:
            if self._state in (STATE_CHECKING, STATE_DOWNLOADING):
                return False
            self._state = STATE_CHECKING
            self._checked = 0
            self._total = 0
            self._found = []
            self._error = None
            self._downloading_sns = set()
            self._submitting = False
            # begin()／end() 一律在 self._lock 內呼叫，跟狀態轉換綁在一起——不然
            # cancel() 可能在「設好狀態」跟「動 recheck_pause」之間插進來、把剛清掉的
            # 暫停旗標又設回去（反之亦然）。
            recheck_pause.begin(self._settings)
        self._thread = threading.Thread(target=self._run_check, args=(video_sn,), daemon=True)
        self._thread.start()
        return True

    def confirm(self, choices: dict[int, str]) -> bool:
        """`choices`: {video_sn: "download" | "skip"}。"skip" 的直接寫進
        `skipped_episodes`（純本地 DB、很快）；"download" 的丟進背景執行緒逐集
        `trigger_manual_download()`——每一集一次 `get_video()` 網路請求，選很多集時同步
        跑會把單執行緒的 web 卡住好幾分鐘。回傳 True 代表狀態對、有受理；False 代表
        現在不是 results_ready（前端狀態過期）。"""
        with self._lock:
            if self._state != STATE_RESULTS_READY:
                return False
            meta = {
                ep["video_sn"]: (group["sn"], group["anime_title"])
                for group in self._found
                for ep in group["episodes"]
            }

        downloads: list[int] = []
        for video_sn, choice in choices.items():
            info = meta.get(video_sn)
            if info is None:
                continue
            sn, anime_title = info
            if choice == "skip":
                self._skipped.mark(sn, video_sn, anime_title)
            elif choice == "download":
                downloads.append(video_sn)

        with self._lock:
            if self._state != STATE_RESULTS_READY:
                return True  # cancel() 在標記 skip 期間插進來——skip 已寫入，下載就不送了
            if not downloads:
                self._state = STATE_IDLE
                self._found = []
                recheck_pause.end(self._settings)
                return True
            self._state = STATE_DOWNLOADING
            self._downloading_sns = set(downloads)
            self._submitting = True
            recheck_pause.begin(self._settings)  # 送出／下載期間持續暫停、重設 timeout
        self._thread = threading.Thread(
            target=self._run_confirm, args=(list(downloads),), daemon=True
        )
        self._thread.start()
        return True

    def _run_confirm(self, downloads: list[int]) -> None:
        """背景送出使用者選了「下載」的集數。比照 `_run_check`：整段包 try/except，不讓
        未預期例外把執行緒悄悄殺掉、把暫停旗標留著沒清。"""
        submitted: set[int] = set()
        try:
            for video_sn in downloads:
                with self._lock:
                    if self._state != STATE_DOWNLOADING:  # 被 cancel() 了
                        break
                try:
                    if self._main_loop.trigger_manual_download(video_sn) is DownloadTrigger.SUBMITTED:
                        submitted.add(video_sn)
                except Exception:
                    logger.exception("重新檢查：提交 video_sn=%s 下載時發生未預期的例外", video_sn)
        except Exception:
            logger.exception("重新檢查：送出下載的執行緒發生未預期的例外")
        finally:
            with self._lock:
                self._submitting = False
                if self._state == STATE_DOWNLOADING:
                    if submitted:
                        self._downloading_sns = submitted
                        recheck_pause.begin(self._settings)
                    else:
                        self._state = STATE_IDLE
                        self._found = []
                        self._downloading_sns = set()
                        recheck_pause.end(self._settings)

    def cancel(self) -> None:
        # 檢查／送出執行緒還在跑時：它們每一輪都會查 self._state，發現不是自己預期的
        # 狀態就自己收手（不會再動 recheck_pause）。begin()／end() 一律在 self._lock 內。
        with self._lock:
            self._state = STATE_IDLE
            self._found = []
            self._downloading_sns = set()
            self._submitting = False
            recheck_pause.end(self._settings)

    # ------------------------------------------------------------------
    # 前端輪詢
    # ------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            state = self._state
            submitting = self._submitting
            downloading_sns = set(self._downloading_sns)
        # downloading：送出階段結束後，提交的下載都不在 registry 裡了＝都完成（或失敗）
        # → 恢復排程。送出階段（submitting）_downloading_sns 還沒填完，先別判斷。
        if state == STATE_DOWNLOADING and not submitting:
            remaining = {sn for sn in downloading_sns if self._registry.is_active(sn)}
            if not remaining:
                with self._lock:
                    if self._state == STATE_DOWNLOADING:
                        self._state = STATE_IDLE
                        self._found = []
                        self._downloading_sns = set()
                        recheck_pause.end(self._settings)
        # results_ready：暫停旗標被 timeout 清掉了（超過 30 分鐘沒確認）→ 自動取消
        elif state == STATE_RESULTS_READY and not recheck_pause.is_active(self._settings):
            with self._lock:
                if self._state == STATE_RESULTS_READY:
                    self._state = STATE_IDLE
                    self._found = []

        with self._lock:
            remaining_downloads = sum(
                1 for sn in self._downloading_sns if self._registry.is_active(sn)
            )
            return {
                "state": self._state,
                "checked": self._checked,
                "total": self._total,
                "found": self._found,
                "error": self._error,
                "submitting": self._submitting,
                "downloading_remaining": remaining_downloads,
                "downloading_total": len(self._downloading_sns),
            }

    # ------------------------------------------------------------------
    # 背景檢查執行緒
    # ------------------------------------------------------------------

    def _run_check(self, video_sn: int | None) -> None:
        try:
            entries = [
                entry
                for entry in self._schedule_store.get_entries().values()
                if entry.schedule_weekday is not None
            ]
            if video_sn is not None:
                entries = [entry for entry in entries if entry.sn == video_sn]

            with self._lock:
                self._total = len(entries)

            found: list[dict] = []
            for entry in entries:
                with self._lock:
                    if self._state != STATE_CHECKING:  # 被 cancel() 了
                        return
                try:
                    new_episodes = self._main_loop.list_new_episodes(entry)
                except Exception:
                    logger.exception("重新檢查：sn=%s 檢查時發生未預期的例外", entry.sn)
                    new_episodes = []
                if new_episodes:
                    found.append(
                        {
                            "sn": entry.sn,
                            "anime_title": new_episodes[0].anime_title,
                            "episodes": [
                                {"video_sn": e.video_sn, "episode_label": e.episode_label}
                                for e in new_episodes
                            ],
                        }
                    )
                with self._lock:
                    self._checked += 1

            with self._lock:
                if self._state != STATE_CHECKING:  # cancel() 在最後一刻插進來
                    return
                self._found = found
                if found:
                    self._state = STATE_RESULTS_READY
                    recheck_pause.begin(self._settings)  # 重設 timeout（從「列出結果」起算）
                else:
                    self._state = STATE_IDLE
                    recheck_pause.end(self._settings)
        except Exception as exc:
            logger.exception("重新檢查：檢查執行緒發生未預期的例外")
            with self._lock:
                self._error = str(exc)
                self._state = STATE_ERROR
                recheck_pause.end(self._settings)
