"""定期背景重新評估首頁快取（本季新番卡片＋週期表）要不要重抓。

使用者 2026-09-30 回報：沒人開首頁時新番快訊追蹤（`newanime/convert.py`）跟完結偵測
（`scheduler/completion_watch.py`）都靠 `anime_cache.get_home()` 讀週期表比對，但這個
快取原本只有在有人真的用瀏覽器開 `GET /` 時才會被檢查／重抓（`web/anime_data.py` 的
`home_data()`）——啟動時的 `_warm_home_cache()` 只跑一次，之後就完全依賴頁面瀏覽。沒人
開首頁（例如純粹靠公開模式／API 使用、或使用者很久沒開網頁）→ 快取停在啟動當下的
快照，追蹤/完結偵測永遠比對到過期資料、形同「不會有任何動作」。

這裡直接重用 `anime_data.maybe_refresh_home()`（`home_data()` 同一套節流／重抓邏輯）（`cache/home_policy.py` 的
`home_refresh_decision`：30 分鐘最短間隔、時段前後避開），不重寫一份——只是補上
「沒人開首頁時，也要有人定時幫忙戳一下」這個一直沒有的定時觸發。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_DEFAULT_CHECK_INTERVAL_SECONDS = 30.0  # 只做輕量判斷，實際重抓與否仍由 home_refresh_decision 節流


class HomeCacheRefresher:
    def __init__(
        self,
        web_deps,
        *,
        check_interval_seconds: float = _DEFAULT_CHECK_INTERVAL_SECONDS,
        refresh_fn: Callable[[object], object] | None = None,
    ) -> None:
        self._web_deps = web_deps
        self._interval = float(check_interval_seconds)
        # 測試注入用；正式環境延遲 import，避免 web/ 套件在 app_shell 組裝階段就被拉進來
        self._refresh_fn = refresh_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="home-cache-refresher"
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def check_once(self) -> None:
        refresh_fn = self._refresh_fn
        if refresh_fn is None:
            from bahaad.web.anime_data import maybe_refresh_home as refresh_fn
        try:
            refresh_fn(self._web_deps)
        except Exception:  # noqa: BLE001 - 純背景保養，失敗下一輪再試
            logger.debug("定期首頁快取檢查失敗", exc_info=True)

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self.check_once()
            if self._stop_event.wait(self._interval):
                return
