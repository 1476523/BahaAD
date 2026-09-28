"""`NewAnimeNotifier`——把 `newanime_change_log` 的未發異動變成通知。

規格 docs/requirements/new_anime_bulletin.md §8b：

- **首次掃描完成**：一定發 `newanime_full_list`（即使使用者關掉這個類別，`force=True`）。
- 之後有未發異動時：
  - `newanime_full_list` 類別**開** → 重發完整列表（下方多「新番異動」組），個別三類不發。
  - `newanime_full_list` 類別**關** → 逐則發 `newanime_added` / `newanime_time_changed` /
    `newanime_removed`（各自看類別開關）。
- 兩條路都在最後 `mark_changes_notified`。

「新番偵測」總開關（`newanime_detect_enabled`）關掉時整個 no-op。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable

from bahaad.newanime.format import display_name, format_change_line, format_full_list
from bahaad.newanime.watch import DEFAULT_NEWANIME_DETECT_ENABLED, NEWANIME_DETECT_KEY
from bahaad.notify.dispatch import category_enabled, send_notification
from bahaad.notify.senders import HttpClient
from bahaad.store.newanime_cache import NewAnimeCacheStore
from bahaad.store.notify import NotifyStore
from bahaad.store.settings import SettingsStore

logger = logging.getLogger(__name__)

_KIND_TO_CATEGORY = {
    "added": "newanime_added",
    "re_added": "newanime_added",
    "time_changed": "newanime_time_changed",
    "removed": "newanime_removed",
}


class NewAnimeNotifier:
    def __init__(
        self,
        notify_store: NotifyStore,
        settings: SettingsStore,
        http: HttpClient,
        store: NewAnimeCacheStore,
        *,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._notify_store = notify_store
        self._settings = settings
        self._http = http
        self._store = store
        self._now_fn = now_fn

    def _enabled(self) -> bool:
        return bool(self._settings.get(NEWANIME_DETECT_KEY, DEFAULT_NEWANIME_DETECT_ENABLED))

    def notify_all(self) -> None:
        if not self._enabled():
            return
        for bulletin in self._store.list_bulletins():
            try:
                self._notify_season(bulletin)
            except Exception:  # noqa: BLE001
                logger.exception("新番快訊：%s 季發通知失敗", bulletin.get("season_key"))

    # ------------------------------------------------------------------

    def _notify_season(self, bulletin: dict) -> None:
        season_key = bulletin["season_key"]
        now_iso = self._now_fn().isoformat(timespec="seconds")
        unnotified = self._store.list_changes(season_key, only_unnotified=True)

        first_scan_done = bulletin.get("first_scan_done_at") is not None
        full_list_sent = bulletin.get("full_list_notified_at") is not None

        # 首次掃描完成、但「完整列表」還沒發過 → 一定發（不看類別開關）
        if first_scan_done and not full_list_sent:
            self._send_full_list(season_key, [], is_update=False, force=True)
            self._store.mark_full_list_notified(season_key, now_iso)
            if unnotified:
                self._store.mark_changes_notified([c["id"] for c in unnotified], now_iso)
            return

        if not unnotified:
            return

        if category_enabled(self._settings, "newanime_full_list"):
            self._send_full_list(season_key, unnotified, is_update=True, force=False)
        else:
            for change in unnotified:
                self._send_individual(change)
        self._store.mark_changes_notified([c["id"] for c in unnotified], now_iso)

    def _send_full_list(
        self, season_key: str, changes: list[dict], *, is_update: bool, force: bool
    ) -> None:
        items = self._store.list_items(season_key)
        names = {
            row["virtual_sn"]: display_name(row) or "（未知）"
            for row in items
        }
        html = format_full_list(items, changes, names, is_update=is_update)
        send_notification(
            self._notify_store,
            self._settings,
            self._http,
            "newanime_full_list",
            force=force,
            full_list_html=html,
        )

    def _send_individual(self, change: dict) -> None:
        category = _KIND_TO_CATEGORY.get(change.get("kind"))
        if category is None:
            return
        item = self._store.get_item(change["virtual_sn"]) or {}
        name = display_name(item) or "（未知）"
        send_notification(
            self._notify_store,
            self._settings,
            self._http,
            category,
            newanime_message=format_change_line(change, name),
            animation_name=name,
            newanime_time=change.get("new_time") or "待定",
            newanime_old_time=change.get("old_time") or "待定",
            newanime_new_time=change.get("new_time") or "待定",
        )
