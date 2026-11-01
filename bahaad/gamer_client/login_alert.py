"""動畫瘋登入失效時發站外通知（Telegram／Discord）。

使用者 2026-10-10：登入態失效（token.php 回 login=false、BAHARUNE=deleted、code 1007…）時，
下載會悄悄退成看廣告、畫質掉到 360P，原本只有網頁橫幅，人不在電腦前完全不知道。

`_gamer_login_stale` 旗標有好幾個地方會設（`GamerSession`、`guest_access`、`main_loop`、
網頁路由…），這裡不去改每一處，改成定時看旗標：有旗標、而且還沒為這一次失效通知過，就送一則
`system_login_lost` 通知。旗標被清掉（重新登入）後，下一次失效會再通知。
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

logger = logging.getLogger(__name__)

_STALE_KEY = "_gamer_login_stale"
_NOTIFIED_KEY = "_gamer_login_stale_notified"
_CATEGORY = "system_login_lost"
_DEFAULT_INTERVAL_SECONDS = 30.0


class GamerLoginAlert:
    def __init__(
        self,
        settings,
        notify_store,
        http,
        *,
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
        send_fn: Callable[..., object] | None = None,
    ) -> None:
        self._settings = settings
        self._notify_store = notify_store
        self._http = http
        self._interval = float(interval_seconds)
        self._send_fn = send_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def check_once(self) -> bool:
        """回傳這次有沒有送出通知。"""
        stale = self._settings.get(_STALE_KEY)
        if not stale:
            if self._settings.get(_NOTIFIED_KEY):
                self._settings.reset([_NOTIFIED_KEY])  # 已恢復，下次失效要能再通知
            return False
        marker = stale.get("detected_at") if isinstance(stale, dict) else str(stale)
        if self._settings.get(_NOTIFIED_KEY) == marker:
            return False
        send = self._send_fn
        if send is None:
            from bahaad.notify.dispatch import send_notification as send
        try:
            send(self._notify_store, self._settings, self._http, _CATEGORY)
        except Exception:  # noqa: BLE001 - 通知失敗不能影響其他背景工作，下一輪再試
            logger.warning("動畫瘋登入失效通知發送失敗", exc_info=True)
            return False
        self._settings.update({_NOTIFIED_KEY: marker})
        return True

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="gamer-login-alert")
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
            except Exception:  # noqa: BLE001
                logger.debug("登入失效通知檢查失敗", exc_info=True)
            if self._stop_event.wait(self._interval):
                return
