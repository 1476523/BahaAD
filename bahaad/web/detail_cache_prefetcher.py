"""背景把「已訂閱番劇」的詳細頁快取排隊補齊／更新（使用者 2026-10-07）。

原本番劇詳細頁快取只有使用者真的點進那一頁才會建立；沒進過就沒有快取，而且快取有效期很長
（預設 90 天），站方新增的集數（例如特別篇 13.5）要等快取過期才看得到。這裡定時掃訂閱清單：
沒有快取、或快取超過 `refresh_hours` 的就排進佇列，**每分鐘一輪、每輪最多 5 部（沒快取的優先、
再來最舊的）、兩次之間隔 3 秒**，不一次全抓（避免觸發風險管控）。已經夠新的略過，不浪費請求
（使用者 2026-10-11：原本 30 分鐘一輪全抓，改每分鐘最多 5 部）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Callable

logger = logging.getLogger(__name__)

_START_DELAY_SECONDS = 30.0
_PASS_INTERVAL_SECONDS = 60.0
_MAX_PER_PASS = 5
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
        max_per_pass: int = _MAX_PER_PASS,
        fetch_fn: Callable[[object, int], object] | None = None,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._deps = web_deps
        self._start_delay = start_delay_seconds
        self._pass_interval = pass_interval_seconds
        self._gap = fetch_gap_seconds
        self._refresh = timedelta(hours=refresh_hours)
        self._max_per_pass = max(1, int(max_per_pass))
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
        todo: list[tuple[int, datetime, int]] = []
        for sn in sorted(schedule_store.get_entries()):
            try:
                hit = cache.get_detail(sn)
            except Exception:  # noqa: BLE001
                hit = None
            if hit is None:
                todo.append((0, datetime.min, sn))  # 沒快取的最優先
            elif now - hit[1] >= self._refresh:
                # 其次最舊的。不用 datetime.timestamp()——Windows 上遇到 1970 年前後（快取被作廢時的
                # 時間戳）會丟 OSError，整條預抓執行緒就停了（使用者 2026-10-12 的未捕捉例外）
                todo.append((1, hit[1], sn))
        todo.sort()
        return [sn for _grp, _at, sn in todo]

    def _fetch(self, sn: int) -> None:
        fetch_fn = self._fetch_fn
        if fetch_fn is None:
            from bahaad.web.anime_data import anime_detail_data

            # ttl_days=0 → 一律視為過期、重新抓並覆寫快取；訪客 session（不帶登入 cookie，免得背景
            # 一直輪換 BAHARUNE 害下載碰 code 1007）、集數比既有快取少就不覆蓋
            fetch_fn = lambda deps, video_sn: anime_detail_data(  # noqa: E731
                deps, video_sn, ttl_days=0, allow_member=False, keep_if_smaller=True
            )
        result = fetch_fn(self._deps, sn)
        # anime_detail_data 失敗時回 (None, 錯誤原因) 而不是丟例外——留一行日誌，不然沒快取的原因看不到
        if isinstance(result, tuple) and len(result) == 2 and result[0] is None and result[1]:
            logger.warning("背景補番劇頁快取沒成功（sn=%s）：%s", sn, result[1])

    def run_pass(self) -> int:
        """跑一輪：最多抓 `max_per_pass` 部、每部之間隔 `fetch_gap`；回傳實際抓的部數。被要求停止就中斷。"""
        fetched = 0
        for sn in self.queue()[: self._max_per_pass]:
            if self._stop_event.is_set():
                break
            try:
                self._fetch(sn)
                fetched += 1
            except Exception:  # noqa: BLE001 - 單一部失敗不影響後面的
                logger.warning("背景補番劇頁快取失敗（sn=%s）", sn, exc_info=True)
            if self._stop_event.wait(self._gap):
                break
        return fetched

    def _run_loop(self) -> None:
        if self._stop_event.wait(self._start_delay):
            return
        while not self._stop_event.is_set():
            try:
                self.run_pass()
            except Exception:  # noqa: BLE001 - 一輪出錯不能讓整條預抓執行緒結束
                logger.warning("背景補番劇頁快取這一輪發生未預期的例外，下一輪再試", exc_info=True)
            if self._stop_event.wait(self._pass_interval):
                return
