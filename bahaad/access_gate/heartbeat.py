"""同帳號多 IP 心跳背景執行緒。規格見 docs/requirements/access_gate.md。

跟 `scheduler/version_check.py` 的 `VersionChecker`、`updater/policy.py` 的
`UpdateCoordinator` 同樣的背景執行緒慣例：`check_once()`／`start()`／`stop()`。

**實作時比規格文件多補的細節**：同一輪 `check_once()` 順便也把 star 狀態刷新進
`SettingsStore`（`_access_gate_starred`），不是另外開一個獨立背景執行緒——`access_
gate_server` 本身已經把 star 查詢快取 1 小時（見 `access_gate_server.md`），這裡即使
跟著心跳週期一起查（預設 5 分鐘一次）也不會真的每次都打 GitHub API，不值得為了這個
輕量查詢另外維護一條背景執行緒生命週期。
"""

from __future__ import annotations

import logging
import threading

from bahaad.access_gate import oauth
from bahaad.store.access_gate import AccessGateStore
from bahaad.store.settings import SettingsStore

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_MINUTES = 5
# 前綴底線標記「執行期快取結果」，跟 `_pending_update`／`_update_available` 同樣的
# 命名慣例
_BLOCKED_SETTINGS_KEY = "_access_gate_blocked"
_STARRED_SETTINGS_KEY = "_access_gate_starred"


class AccessGateHeartbeat:
    def __init__(
        self,
        settings: SettingsStore,
        access_gate_store: AccessGateStore,
        http: oauth.HttpClient,
        access_gate_server_base_url: str,
    ) -> None:
        self._settings = settings
        self._access_gate_store = access_gate_store
        self._http = http
        self._access_gate_server_base_url = access_gate_server_base_url
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def check_once(self) -> None:
        bahaad_token = self._access_gate_store.get_token()
        if bahaad_token is None:
            # 沒有連結 GitHub 帳號：不送任何請求，確保不會殘留舊的封鎖/star 狀態——
            # 使用者中斷連結之後不該還被鎖住或還看得到舊的提示
            self._settings.reset([_BLOCKED_SETTINGS_KEY, _STARRED_SETTINGS_KEY])
            return

        blocked = oauth.heartbeat(self._http, self._access_gate_server_base_url, bahaad_token)
        if blocked is not None:
            if blocked:
                self._settings.update({_BLOCKED_SETTINGS_KEY: True})
            else:
                self._settings.reset([_BLOCKED_SETTINGS_KEY])
        # blocked is None：連不上伺服器，維持現狀，不改變目前的封鎖狀態

        starred = oauth.star_status(self._http, self._access_gate_server_base_url, bahaad_token)
        if starred is not None:
            self._settings.update({_STARRED_SETTINGS_KEY: starred})

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
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
                logger.exception("access_gate 心跳這一輪檢查發生未預期的例外")
            interval_seconds = (
                self._settings.get("access_gate_heartbeat_interval_minutes", _DEFAULT_INTERVAL_MINUTES)
                * 60
            )
            self._stop_event.wait(interval_seconds)
