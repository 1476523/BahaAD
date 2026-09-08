"""番劇完結自動偵測。規格見 docs/requirements/completion_detection.md。

信號：一部**已訂閱**番劇從**週期表**上消失、跨過一個完整的週一～週日日曆週都沒再出現
→ 視為完結。例外：這段期間有對得上的「下架」官方公告 → 是下架不是完結。

「還在不在週期表」靠**標題比對**，不是 sn——週期表項目連的是「最新一集」的 video_sn、
每週新集數上架就變。`completion_watch` 表存每個訂閱 sn 在週期表看過的標題。

處置由設定 `completion_action` 決定（`unsubscribe` 預設 / `notify` / `mark`）。不管哪種，
都會寫一筆站內通知（`NotificationStore`）＋發一則站外通知（`notify/` `anime_completed`）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable

from bahaad.gamer_client.activation import skip_if_busy
from bahaad.gamer_client.browse import get_weekly_schedule
from bahaad.notify.dispatch import send_notification
from bahaad.store.completion_watch import CompletionWatchStore
from bahaad.store.gossip import GossipStore
from bahaad.store.notifications import NotificationStore
from bahaad.store.schedule_list import ScheduleListStore
from bahaad.store.settings import SettingsStore
from bahaad.subscription_ops import unsubscribe_sn

logger = logging.getLogger(__name__)

_DEFAULT_CHECK_INTERVAL_MINUTES = 30  # 沿用 gossip_check_interval_minutes 的預設量級
_COMPLETION_ACTIONS = ("unsubscribe", "notify", "mark")
_DEFAULT_COMPLETION_ACTION = "unsubscribe"


def _monday_after(t: datetime) -> datetime:
    """t 之後第一個週一 00:00（t 本身是週一時也往後跳到下一個週一）。"""
    days_ahead = (7 - t.weekday()) % 7  # Mon=0
    if days_ahead == 0:
        days_ahead = 7
    return (t + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)


def _is_complete(last_seen: datetime, now: datetime) -> bool:
    """last_seen 之後，`now` 已經走過至少一個完整的週一～週日（見規格的時間門檻）。"""
    return now >= _monday_after(last_seen) + timedelta(days=7)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class CompletionWatcher:
    def __init__(
        self,
        schedule_store: ScheduleListStore,
        completion_watch_store: CompletionWatchStore,
        notification_store: NotificationStore,
        gossip_store: GossipStore,
        notify_store: Any,
        settings: SettingsStore,
        http: Any,
        anime_cache: Any = None,
        orphan_cleanup: Callable[..., Any] | None = None,
        now_fn: Callable[[], datetime] = datetime.now,
        on_gamer_activity: Callable[[], None] | None = None,
        activation_lock: Any = None,
    ) -> None:
        self._schedule_store = schedule_store
        self._watch = completion_watch_store
        self._notifications = notification_store
        self._gossip_store = gossip_store
        self._notify_store = notify_store
        self._settings = settings
        self._http = http
        # 抓週期表 / 查集數會讓伺服器輪換 BAHARUNE——下載交握進行中就這輪跳過
        # （跟 playlist / guest_access / gossip_watch / cookie_warmup 共用同一把鎖，
        # 見 gamer_client/activation.py）
        self._activation_lock = activation_lock
        self._anime_cache = anime_cache
        self._orphan_cleanup = orphan_cleanup
        # 每輪都抓動畫瘋週期表 → 順便叫 gossip_watcher 查一次公告（使用者 2026-09-04）
        self._on_gamer_activity = on_gamer_activity
        self._now_fn = now_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 對外 ------------------------------------------------------------

    def check_once(self) -> dict[str, int]:
        """跑一次檢查。回一個小 summary（給測試看）。"""
        subscribed = {
            sn: entry
            for sn, entry in self._schedule_store.get_entries().items()
            if entry.schedule_weekday is not None
        }
        self._watch.prune(subscribed.keys())
        if not subscribed:
            return {"present": 0, "gone": 0, "completed": 0}

        # 抓週期表會讓伺服器輪換 BAHARUNE——下載交握進行中就這輪跳過（下輪再來），
        # 避免把下載的 cookie 換掉觸發 code 1007。
        with skip_if_busy(self._activation_lock) as free:
            if not free:
                logger.debug("完結偵測：下載交握進行中，這輪跳過")
                return {"present": 0, "gone": 0, "completed": 0, "skipped": 1}
            return self._run_check(subscribed)

    def _run_check(self, subscribed: dict) -> dict[str, int]:
        schedule = self._fetch_weekly_schedule()
        if schedule is None:
            # 抓不到週期表——這輪什麼都不推進（不能因為自己沒抓到就把所有訂閱當消失）
            return {"present": 0, "gone": 0, "completed": 0, "skipped": 1}

        present_titles = {e.title.strip() for w in schedule for e in w.entries if e.title}
        present_sns = {e.video_sn for w in schedule for e in w.entries}

        now = self._now_fn()
        now_iso = now.strftime("%Y-%m-%d %H:%M:%S")
        summary = {"present": 0, "gone": 0, "completed": 0}
        completed_sns = self._watch.completed_sns()

        for sn in subscribed:
            if sn in completed_sns:
                continue
            row = self._watch.get(sn)
            title = self._resolve_title(sn, row, present_sns, schedule)

            is_present = (title in present_titles) if title else (sn in present_sns)
            if is_present:
                self._watch.mark_seen(sn, title, now_iso)
                summary["present"] += 1
                continue

            # 不在週期表上
            row = self._watch.get(sn)  # mark_seen 可能剛建過列
            if row is None or row.last_seen_at is None:
                # 從沒看過它在週期表上 → 不套用完結偵測（可能是舊番/劇場版）
                continue
            self._watch.mark_gone(sn, now_iso)
            summary["gone"] += 1

            last_seen = _parse_dt(row.last_seen_at)
            if last_seen is None or not _is_complete(last_seen, now):
                continue
            if self._gossip_store.has_takedown_since(sn, row.last_seen_at):
                continue  # 這段期間有「下架」公告 → 是下架不是完結

            self._complete(sn, title or row.anime_title, now_iso)
            summary["completed"] += 1

        return summary

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="completion-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    # ---- 內部 ------------------------------------------------------------

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once()
                if self._on_gamer_activity is not None:
                    self._on_gamer_activity()  # 剛抓過週期表 → 順便查公告
            except Exception:  # noqa: BLE001
                logger.exception("completion_watch 這一輪檢查發生未預期的例外")
            interval_minutes = self._settings.get(
                "gossip_check_interval_minutes", _DEFAULT_CHECK_INTERVAL_MINUTES
            )
            self._stop_event.wait(interval_minutes * 60)

    def _fetch_weekly_schedule(self):
        if self._anime_cache is not None:
            cached = self._anime_cache.get_home()
            if cached is not None:
                return cached[1]
        try:
            return get_weekly_schedule(self._http)
        except Exception as exc:  # noqa: BLE001
            logger.warning("completion_watch 抓週期表失敗：%s", exc)
            return None

    def _resolve_title(self, sn: int, row, present_sns: set[int], schedule) -> str | None:
        # 使用者更名優先——完結通知也要用改後的名稱（使用者 2026-09-03）
        try:
            entry = self._schedule_store.get_entries().get(sn)
        except Exception:  # noqa: BLE001
            entry = None
        if entry is not None and entry.rename:
            return entry.rename
        if row is not None and row.anime_title:
            return row.anime_title
        if sn in present_sns:
            for w in schedule:
                for e in w.entries:
                    if e.video_sn == sn and e.title:
                        return e.title.strip()
        if self._anime_cache is not None:
            hit = self._anime_cache.get_detail(sn)
            if hit is not None and hit[0].title:
                title = hit[0].title.strip()
                self._watch.learn_title(sn, title)
                return title
        return None

    def _complete(self, sn: int, title: str | None, now_iso: str) -> None:
        action = self._settings.get("completion_action", _DEFAULT_COMPLETION_ACTION)
        if action not in _COMPLETION_ACTIONS:
            action = _DEFAULT_COMPLETION_ACTION

        self._watch.set_completed(sn, now_iso)

        if action == "unsubscribe":
            result = unsubscribe_sn(self._schedule_store, sn)
            if result["removed"] and self._orphan_cleanup is not None:
                try:
                    self._orphan_cleanup(sn)
                except Exception:  # noqa: BLE001
                    logger.exception("completion_watch 自動退訂後清孤兒紀錄失敗（sn=%s）", sn)
            action_taken = "unsubscribe"
        elif action == "mark":
            action_taken = "mark"  # 靠 completed_sns() 讓 custom_schedule 跳過，不動排程
        else:
            action_taken = "none"

        if not self._notifications.has_unresolved_for_sn(sn, "completion"):
            self._notifications.add(
                kind="completion", sn=sn, anime_title=title, action_taken=action_taken
            )

        try:
            send_notification(
                self._notify_store,
                self._settings,
                self._http,
                "anime_completed",
                animation_name=title or f"sn {sn}",
            )
        except Exception:  # noqa: BLE001
            logger.exception("completion_watch 發送完結通知失敗（sn=%s）", sn)

        logger.info("番劇完結偵測：sn=%s（%s）處置=%s", sn, title, action)
