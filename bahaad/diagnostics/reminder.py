"""更新後如果診斷回報是關閉狀態，提醒一次。規格見 docs/requirements/diagnostics.md
「使用者可見的部分」第 3 點。純函式＋`SettingsStore` 副作用，方便測試，不用真的啟動
整個 `app_shell.py` 才能驗證這段邏輯。
"""

from __future__ import annotations

from bahaad.store.settings import SettingsStore

_ENABLED_KEY = "diagnostics_enabled"
_LAST_SEEN_VERSION_KEY = "_diagnostics_last_seen_version"
_REMINDER_PENDING_KEY = "_diagnostics_reminder_pending"


def check_update_reminder(settings: SettingsStore, current_version: str) -> None:
    """程式啟動時呼叫一次。跟上次啟動記錄的版本號不一樣（含第一次啟動，`get()` 回傳
    `None` 一定跟任何真實版本號不同）代表剛經歷一次版本更新；這時候如果診斷回報是
    關閉的，設一次性的提醒旗標。版本沒變就不用做任何事，維持旗標原狀（可能還是上次
    設定、還沒被使用者看到並清掉的），不要每次啟動都覆蓋掉使用者還沒來得及看的提醒。
    """
    last_seen_version = settings.get(_LAST_SEEN_VERSION_KEY)
    if last_seen_version == current_version:
        return

    settings.update({_LAST_SEEN_VERSION_KEY: current_version})
    if not settings.get(_ENABLED_KEY, True):
        settings.update({_REMINDER_PENDING_KEY: True})


def dismiss_reminder(settings: SettingsStore) -> None:
    settings.reset([_REMINDER_PENDING_KEY])
