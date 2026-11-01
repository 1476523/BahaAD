"""每分鐘回報「這個客戶端還活著」的心跳。規格見
docs/requirements/realtime_stats.md「當前在線人數」。

比照 `access_gate/heartbeat.py` 的背景執行緒慣例（`check_once`／`start`／`stop`）。
每輪 POST `/stats/heartbeat`：

- `online`：一律 True（背景執行緒還在跑＝程式活著）。
- `public_active`：公開模式開著 **且** 10 分鐘內有頁面操作／看影片（`StatsActivity`）。
  ——「公開模式 總在線人數」要算實際使用，閒置超過 10 分鐘不計（使用者 2026-09-08）。
- `watching_sn`：目前正在播放的番劇首集 sn（`StatsActivity`，播放器每分鐘 ping），沒有 None。

心跳送不出去就這輪算了——過期的心跳沒有補送的意義（不進 stats_pending）。
開關 `realtime_stats_enabled` 關掉 → 整輪 no-op。
"""

from __future__ import annotations

import logging
import threading
from typing import Protocol

from bahaad.stats.activity import StatsActivity
from bahaad.store.activation import ActivationStore
from bahaad.store.settings import SettingsStore

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "realtime_stats_enabled"
_DEFAULT_INTERVAL_SECONDS = 60
_DEFAULT_TIMEOUT = 5.0


class HttpPoster(Protocol):
    def post(self, url: str, **kwargs) -> object: ...


class StatsHeartbeat:
    def __init__(
        self,
        settings: SettingsStore,
        activation_store: ActivationStore,
        activity: StatsActivity,
        http: HttpPoster,
        base_url: str,
        *,
        interval_seconds: int = _DEFAULT_INTERVAL_SECONDS,
        request_timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._settings = settings
        self._activation_store = activation_store
        self._activity = activity
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._interval = interval_seconds
        self._timeout = request_timeout
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def check_once(self) -> None:
        if not self._settings.get(_SETTINGS_KEY, True):
            return
        active, watching_sn = self._activity.snapshot()
        public_active = active and bool(self._settings.get("public_mode", False))
        payload = {
            "activation_id": self._activation_store.get_or_create(),
            "online": True,
            "public_active": public_active,
            "watching_sn": watching_sn,
        }
        try:
            self._http.post(
                f"{self._base_url}/stats/heartbeat", json=payload, timeout=self._timeout
            )
        except Exception as exc:  # noqa: BLE001 - 心跳漏一次沒差
            logger.debug("stats 心跳送出失敗（下輪再試）：%s", exc)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="stats-heartbeat")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("stats 心跳這一輪發生未預期的例外")
            self._stop_event.wait(self._interval)
