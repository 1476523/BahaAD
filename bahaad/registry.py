"""唯一「哪些 sn 正在下載中／連續失敗幾次」權威結構。規格見 docs/requirements/registry.md。

刻意不持久化：「正在下載中」的定義就是「這個行程還活著、正在做這件事」，程式重啟後
不管資料庫裡記了什麼，事實上沒有任何東西真的在下載。也刻意不記錄「已下載完成的集數」
——那是 scheduler/main_loop.py 的責任（依檔案系統判斷），是完全不同的問題。

例外：**失敗紀錄**（`_failure_counts`/`_last_errors`）2026-09-03 起會在程式啟動時由
`main_loop.resume_pending_downloads()` 從 `pending_downloads`（stage='failed'）灌回來
（`restore_failure()`）——使用者回報「下載失敗的項目在更新或重啟後會遺失」。權威來源
仍是這裡的記憶體字典；持久化只負責「重啟後補回」，寫入時機在 `main_loop` 那邊。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from bahaad.downloader.segment import DownloadProgress


@dataclass(frozen=True)
class RegistryEntry:
    sn: int
    started_at: float
    progress: DownloadProgress | None = None
    # 「前置準備」階段（progress 還是 None）的細部狀態文字——遊客／非 VIP 看廣告下載時
    # 的「正在交握」「等待廣告播放（約 N 秒）」等，讓下載列表卡片不會一直只顯示「前置
    # 準備」（使用者 2026-09-01 回饋）。progress 開始回報後這個欄位就沒意義了。
    phase: str | None = None
    # 使用者已更改的番劇名（`entry.rename` 或乾淨標題）——下載列表卡片直接顯示這個，
    # 不用再靠 catalog 反查、也不會顯示成原始標題或 sn（使用者 2026-09-03）。
    display_name: str | None = None


@dataclass(frozen=True)
class FailureInfo:
    sn: int
    failure_count: int
    last_error: str | None


class DownloadRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[int, RegistryEntry] = {}
        self._failure_counts: dict[int, int] = {}
        self._last_errors: dict[int, str] = {}
        # .0 改進.txt 第 12 項：下載完一集後的冷卻期間（worker slot 仍被佔著），記下
        # 「還要冷卻到哪個時間點」，下載列表頁才能顯示「冷卻中，約 N 秒後開始下一部」，
        # 不會看起來像卡住了。value = time.monotonic() 的到期時間點。
        self._cooldown_until: dict[int, float] = {}

    def try_start(self, sn: int, display_name: str | None = None) -> bool:
        """原子性地嘗試把 sn 標記為「正在下載中」。已經在下載中回傳 False。"""
        with self._lock:
            if sn in self._active:
                return False
            self._active[sn] = RegistryEntry(
                sn=sn, started_at=time.time(), display_name=display_name
            )
            return True

    def update_progress(self, sn: int, progress: DownloadProgress) -> None:
        with self._lock:
            entry = self._active[sn]
            self._active[sn] = RegistryEntry(
                sn=sn, started_at=entry.started_at, progress=progress,
                display_name=entry.display_name,
            )

    def set_phase(self, sn: int, phase: str | None) -> None:
        """更新「前置準備」階段的細部狀態文字（遊客看廣告下載用）。sn 不在 active 時是
        安全的空操作。progress 已經開始回報後呼叫這個沒有意義（前端只在 preparing 顯示）。"""
        with self._lock:
            entry = self._active.get(sn)
            if entry is None:
                return
            self._active[sn] = RegistryEntry(
                sn=sn, started_at=entry.started_at, progress=entry.progress, phase=phase,
                display_name=entry.display_name,
            )

    def finish(self, sn: int, success: bool, error: str | None = None) -> None:
        with self._lock:
            # 一般是「正在下載中的 sn 結束了」；`main_loop._process_site_error_retries`
            # 放棄延長重試時，sn 早就不在 active 裡（那次連下載 worker 都沒起）——用
            # pop 不用 del，讓那條路也能借這裡記失敗訊息（使用者 2026-09-06）。
            self._active.pop(sn, None)
            if success:
                self._failure_counts[sn] = 0
                self._last_errors.pop(sn, None)
            else:
                self._failure_counts[sn] = self._failure_counts.get(sn, 0) + 1
                if error is not None:
                    self._last_errors[sn] = error

    def cancel(self, sn: int) -> None:
        """使用者主動「中止」這次下載（round 7 第 18 項）——從 active 移除，但**不**
        累加失敗次數、不記 last_error（跟 `finish(success=False)` 不同：那會讓它出現在
        「最近失敗」清單、被當成需要重試的失敗）。對不在 active 的 sn 是安全的空操作
        （排隊中、還沒真的開始跑的任務也可能被中止）。"""
        with self._lock:
            self._active.pop(sn, None)

    def clear_failure(self, sn: int) -> None:
        """使用者「丟棄」一個失敗任務（round 7 第 18 項）——清掉它的失敗紀錄，讓它不再
        出現在 `failing_snapshot()`（下載列表頁的「最近失敗」卡片）。"""
        with self._lock:
            self._failure_counts.pop(sn, None)
            self._last_errors.pop(sn, None)

    def restore_failure(self, sn: int, failure_count: int, error: str | None) -> None:
        """程式啟動時把上次跑到失敗、還沒被使用者處理掉的下載灌回來（來源＝
        `pending_downloads` stage='failed'）。已經 active／已有更新的失敗紀錄就不覆蓋。"""
        with self._lock:
            if sn in self._active or sn in self._failure_counts:
                return
            self._failure_counts[sn] = max(1, int(failure_count or 1))
            if error:
                self._last_errors[sn] = error

    def is_active(self, sn: int) -> bool:
        with self._lock:
            return sn in self._active

    def failure_count(self, sn: int) -> int:
        with self._lock:
            return self._failure_counts.get(sn, 0)

    def last_error(self, sn: int) -> str | None:
        with self._lock:
            return self._last_errors.get(sn)

    def mark_cooldown(self, sn: int, seconds: float) -> None:
        """標記這個 sn 進入「下載完成後的冷卻」——`snapshot()` 已經不含它了（已 finish），
        這是給下載列表頁顯示「冷卻中」用的獨立狀態。"""
        if seconds <= 0:
            return
        with self._lock:
            self._cooldown_until[sn] = time.monotonic() + seconds

    def clear_cooldown(self, sn: int) -> None:
        with self._lock:
            self._cooldown_until.pop(sn, None)

    def cooldown_remaining(self) -> float:
        """目前所有冷卻中的 sn 裡，最久還要等幾秒（沒有就 0）。順便清掉已到期的。"""
        now = time.monotonic()
        with self._lock:
            self._cooldown_until = {
                sn: until for sn, until in self._cooldown_until.items() if until > now
            }
            if not self._cooldown_until:
                return 0.0
            return max(self._cooldown_until.values()) - now

    def snapshot(self) -> dict[int, RegistryEntry]:
        with self._lock:
            return dict(self._active)

    def failing_snapshot(self) -> dict[int, FailureInfo]:
        """回傳目前所有「連續失敗次數 > 0」的 sn，給 web/dashboard.py 顯示用。跟
        snapshot() 一樣只是既有內部狀態（_failure_counts／_last_errors）的唯讀
        快照，不是新增的持久化狀態——這兩份字典本來就存在，只是原本沒有整批列舉
        的介面，只能一次查一個 sn。"""
        with self._lock:
            return {
                sn: FailureInfo(sn=sn, failure_count=count, last_error=self._last_errors.get(sn))
                for sn, count in self._failure_counts.items()
                if count > 0
            }
