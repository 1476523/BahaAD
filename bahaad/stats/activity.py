"""「這個客戶端現在有沒有在用」的記憶體狀態——`StatsHeartbeat` 讀、網頁 before_request
與播放器 ping 寫。規格見 docs/requirements/realtime_stats.md「當前在線人數」。

- `touch()`：任何一次「真的在操作頁面」的請求（不含 static／輪詢端點）。
- `touch_watching(first_ep_sn)`：播放器每分鐘 ping 一次「我正在看這部」（只有**真的在播**
  ——本機或投放——才 ping；暫停時播放器改送 `/ui/stats/alive`，不 touch 這裡）。
- `snapshot()`：回 `(active, watching_sn)`——`active`＝10 分鐘內有動作；
  `watching_sn`＝**最後一次「真的在播」的 ping 之後 5 分鐘內**的番劇首集 sn（沒有回
  None）。5 分鐘＝使用者 2026-09-08 定的「暫停寬限」：按暫停後 5 分鐘內仍算「正在收看」
  （拿東西、換集、短暫暫停），超過才掉；按 ✕ 關播放器則 `stop_watching()` 立刻掉。

純記憶體、程式重啟歸零——心跳資料本來就只反映「當下」，不需要持久化。
"""

from __future__ import annotations

import threading
import time

_ACTIVE_WINDOW_SECONDS = 10 * 60
_WATCHING_WINDOW_SECONDS = 5 * 60


class StatsActivity:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_activity = 0.0
        self._watching_sn: int | None = None
        self._watching_at = 0.0

    def touch(self, now: float | None = None) -> None:
        with self._lock:
            self._last_activity = now if now is not None else time.time()

    def touch_watching(self, first_ep_sn: int, now: float | None = None) -> None:
        try:
            sn = int(first_ep_sn)
        except (TypeError, ValueError):
            return
        ts = now if now is not None else time.time()
        with self._lock:
            self._watching_sn = sn if sn > 0 else None
            self._watching_at = ts
            self._last_activity = ts  # 看影片也算「有在用」

    def stop_watching(self) -> None:
        with self._lock:
            self._watching_sn = None
            self._watching_at = 0.0

    def snapshot(self, now: float | None = None) -> tuple[bool, int | None]:
        ts = now if now is not None else time.time()
        with self._lock:
            active = (ts - self._last_activity) < _ACTIVE_WINDOW_SECONDS
            watching = (
                self._watching_sn
                if self._watching_sn and (ts - self._watching_at) < _WATCHING_WINDOW_SECONDS
                else None
            )
        return active, watching
