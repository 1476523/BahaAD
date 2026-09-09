"""「重新檢查排程更新」執行期間暫停背景自動排程檢查。

規格見 docs/requirements/web_redesign_round2.md 階段 4——按下「重新檢查排程更新」後，
`scheduler/main_loop.py`／`scheduler/custom_schedule.py`／`scheduler/gossip_watch.py`
三個背景迴圈這段期間都**暫停觸發新的檢查**（不中斷進行中的下載），避免使用者正在
逐項決定「下載／標記已下載」時，背景排程又自己把同一集下載下去、或狀態被改掉。

只用一個 `SettingsStore` 的內部 key `_recheck_started_at`（ISO 時間字串）：有值＝暫停中。
web 端在開始檢查時 `begin()`、使用者確認完（或按取消）時 `end()`。**30 分鐘 timeout**：
使用者列出結果後放著不管，`is_active()` 發現旗標超過 30 分鐘就自己清掉、恢復排程——
不需要另開背景執行緒盯著，三個迴圈每一輪本來就會呼叫 `is_active()`。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from bahaad.store.settings import SettingsStore

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "_recheck_started_at"
TIMEOUT_MINUTES = 30


def begin(settings: SettingsStore) -> None:
    settings.update({_SETTINGS_KEY: datetime.now().isoformat(timespec="seconds")})


def end(settings: SettingsStore) -> None:
    settings.reset([_SETTINGS_KEY])


def started_at(settings: SettingsStore) -> datetime | None:
    raw = settings.get(_SETTINGS_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None


def is_active(settings: SettingsStore) -> bool:
    """True＝「重新檢查排程更新」正在進行、背景迴圈這一輪要跳過。順手清掉超過
    `TIMEOUT_MINUTES` 沒收尾的殭屍旗標（web 端沒正常 end() 時的保險）。"""
    began = started_at(settings)
    if began is None:
        if settings.get(_SETTINGS_KEY):  # 有值但 parse 不出來
            settings.reset([_SETTINGS_KEY])
        return False
    if datetime.now() - began > timedelta(minutes=TIMEOUT_MINUTES):
        logger.info("_recheck_started_at 超過 %d 分鐘未收尾，自動清除、恢復背景排程", TIMEOUT_MINUTES)
        settings.reset([_SETTINGS_KEY])
        return False
    return True
