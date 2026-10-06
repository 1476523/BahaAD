"""背景把「已訂閱番劇」的詳細頁快取排隊補齊／更新（使用者 2026-10-07）。

原本番劇詳細頁快取只有使用者真的點進那一頁才會建立；沒進過就沒有快取，而且快取有效期很長
（預設 90 天），站方新增的集數（例如特別篇 13.5）要等快取過期才看得到。這裡定時掃訂閱清單：
沒有快取、或快取超過 `refresh_hours` 的就排進佇列，**一次只抓一部、兩次之間隔 3 秒**，不一次全抓
（避免觸發風險管控）。已經夠新的略過，不浪費請求。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

logger = logging.getLogger(__name__)

_START_DELAY_SECONDS = 30.0
_PASS_INTERVAL_SECONDS = 30 * 60
_FETCH_GAP_SECONDS = 3.0
_REFRESH_HOURS = 3


class DetailCachePrefetcher:
    def __init__(
        self,
        web_deps,
        *,
        start_delay_seconds: float = _START_DELAY_SECONDS,
        pass_interval_seconds: float = _PASS_INTERVAL_SECONDS,
        fetch_gap_seconds: float = _FETCH_GAP_SECONDS,
        refresh_hours: float = _REFRESH_HOURS,
        fetch_fn: Callable[[object, int], object] | None = None,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._deps = web_deps
        self._start_delay = start_delay_seconds
        self._pass_interval = pass_interval_seconds
        self._gap = fetch_gap_seconds
        self._refresh = timedelta(hours=refresh_hours)
        self._fetch_fn = fetch_fn
        self._now = now_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="detail-cache-prefetch")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def queue(self) -> list[int]:
        """這一輪要補／更新的訂閱 sn（沒快取、或快取已經超過 `refresh_hours`）。"""
        cache = getattr(self._deps, "anime_cache", None)
        schedule_store = getattr(self._deps, "schedule_store", None)
        if cache is None or schedule_store is None:
            return []
        now = self._now()
        todo = []
        for sn in sorted(schedule_store.get_entries()):
            try:
                hit = cache.get_detail(sn)
            except Exception:  # noqa: BLE001
                hit = None
            if hit is None or now - hit[1] >= self._refresh:
                todo.append(sn)
        return todo

    def _fetch(self, sn: int) -> None:
        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            from bahaad.web.anime_data import anime_detail_data

            # ttl_days=0 → 一律視為過期、重新抓並覆寫快取
            fetch_fn = lambda deps, video_sn: anime_detail_data(deps, video_sn, ttl_days=0)  # noqa: E731
        fetch_fn(self._deps, sn)

    def run_pass(self) -> int:
        """跑一輪：依序抓、每部之間隔 `fetch_gap`；回傳實際抓的部數。被要求停止就中斷。"""
        fetched = 0
        for sn in self.queue():
            if self._stop_event.is_set():
                break
            try:
                self._fetch(sn)
                fetched += 1
            except Exception:  # noqa: BLE001 - 單一部失敗不影響後面的
                logger.debug("背景補番劇頁快取失敗（sn=%s）", sn, exc_info=True)
            if self._stop_event.wait(self._gap):
                break
        return fetched

    def _run_loop(self) -> None:
        if self._stop_event.wait(self._start_delay):
            return
        while not self._stop_event.is_set():
            self.run_pass()
            if self._stop_event.wait(self._pass_interval):
                return
