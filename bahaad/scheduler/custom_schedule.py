"""個別項目自訂時段（星期＋時分）背景執行緒。規格見 docs/requirements/scheduler_custom_schedule.md。

`store/schedule_list.py` 裡設了 `schedule_weekday` 的項目由這裡負責，`scheduler/main_loop.py`
的全域輪詢明確排除這些項目。兩者共用同一套「檢查單一項目、決定要不要下載」的邏輯
（`MainLoop.check_and_download()`），這裡不重新實作一份——符合「一個問題一個唯一答案」原則。

不是每分鐘醒來檢查「現在是不是該檢查的時間」，而是算出下一個該醒來的絕對時間點、直接睡到
那個時間點，避免精確到分鐘的使用者設定因為輪詢間隔而被跳過。

**監視公告的覆蓋排程（Phase 8）**：`store/gossip.py` 的 `gossip_pending` 裡 `override_check_time`／
`override_check_window` 兩種「改時間／改時段」覆蓋也由這裡處理——它們本質上就是「在某個
（非平常的）時間點觸發一次檢查」，跟自訂時段是同一件事，差在還帶「目標時間到了但影片還沒
上架」的重試視窗（單一時刻型：目標時間後 2 小時內每 30 分鐘複查；範圍型：範圍內每分鐘
複查，見 scheduler_gossip_watch.md）。「本週暫停／下架」的 `skip_check` 覆蓋不在這裡，是
`MainLoop.check_and_download()` 自己查 `gossip_store.is_skipped()` 擋下。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Callable

from bahaad.scheduler import recheck_pause
from bahaad.scheduler.main_loop import MainLoop
from bahaad.store.gossip import GossipStore
from bahaad.store.schedule_list import ScheduleEntry, ScheduleListStore
from bahaad.store.settings import SettingsStore

logger = logging.getLogger(__name__)

# 追蹤清單裡沒有任何自訂時段項目、也沒有生效中的公告覆蓋時，多久重新檢查一次清單有沒有
# 變化——不能無限期睡死，使用者之後在網頁上新增自訂時段項目、或監視公告新建了覆蓋，要能
# 在有限時間內生效
_EMPTY_RECHECK_SECONDS = 3600.0
# 醒來時間跟預定觸發時間之間允許的誤差（OS 排程/浮點數計算的微小落差）
_TRIGGER_TOLERANCE_SECONDS = 1.0
# 「重新檢查排程更新」進行中時，這個迴圈每隔多久回頭看一次它結束了沒
_RECHECK_PAUSE_POLL_SECONDS = 15.0
# 排程時間到了、但站方新集數還沒真的上架（動畫瘋通常慢幾秒，偶爾慢一兩分鐘）——不能只
# 在那一刻查一次就放到下週，不然永遠慢一集（使用者 2026-09-04 實測：BahaAD 抓到第 8 集
# 時 aniGamerPlus 已經抓第 9 集）。觸發後在 `_EPISODE_RETRY_WINDOW` 時窗內持續複查，
# 抓到新集數（或時窗到）就停。
#
# 複查間隔採遞增曲線（使用者 2026-09-09 定）——越接近觸發時刻查越密（最可能剛好在這幾
# 分鐘內上架），之後拉長：
#   觸發後 0–3 分：每 2 秒
#   觸發後 3–6 分：每 5 秒
#   觸發後 6–10 分：每 10 秒
#   超過 10 分：放棄，等下一次排程時間
# 注意：複查打的是 `video.php`（公開的集數清單端點，無 cookie）——比 `video_src.php` /
# `getdeviceid`（code 1007 真正咬過的地方）輕。且 `check_and_download()` 內部本來就有
# `sn_query_cooldown_seconds`（預設 2s）的 sleep，實際間隔會再加那個值。若日後 `video.php`
# 也開始回 1007，把下面的秒數往上調（或延後第一段）。
_EPISODE_RETRY_WINDOW = timedelta(minutes=10)
_EPISODE_RETRY_TIERS: tuple[tuple[timedelta, timedelta], ...] = (
    (timedelta(minutes=3), timedelta(seconds=2)),
    (timedelta(minutes=6), timedelta(seconds=5)),
    (timedelta(minutes=10), timedelta(seconds=10)),
)


def _episode_retry_interval(elapsed: timedelta) -> timedelta | None:
    """觸發後過了 `elapsed`，下一次複查該等多久（`None`＝已超過時窗、該放棄）。"""
    for upto, interval in _EPISODE_RETRY_TIERS:
        if elapsed < upto:
            return interval
    return None

_OVERRIDE_TYPES = ("override_check_time", "override_check_window")


def _entry_label(entry: ScheduleEntry) -> str:
    """日誌用的作品名（使用者 2026-09-05：日誌顯示番劇名不顯示 sn）。這裡只拿得到
    排程項目，用使用者更名，沒有就退回 `sn N`（更完整的番劇名日誌在 main_loop 那邊，
    它有 video 物件＋快取）。"""
    return f"《{entry.rename}》" if entry.rename else f"sn {entry.sn}"


def _next_occurrence(weekday: int, hour: int, minute: int, now: datetime) -> datetime:
    """算出下一個符合「這個星期幾的這個時分」的未來時間點。如果現在剛好就是那個時間點
    （或已經過了），算下一週，不是現在——避免同一分鐘內醒來兩次都判定為同一次觸發。

    `weekday` 沿用 store/schedule_list.py 的編碼：1=一...7=日；Python 的 datetime.weekday()
    是 0=一...6=日，兩者差 1。
    """
    target_python_weekday = weekday - 1
    days_ahead = (target_python_weekday - now.weekday()) % 7
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days_ahead)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


def _parse_dt(date_str: str | None, time_str: str | None) -> datetime | None:
    if not date_str or not time_str:
        return None
    try:
        return datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_window(
    date_str: str | None, start_str: str | None, end_str: str | None
) -> tuple[datetime, datetime] | tuple[None, None]:
    if not date_str or not start_str or not end_str:
        return None, None
    try:
        start_dt = datetime.strptime(f"{date_str} {start_str}", "%Y-%m-%d %H:%M")
        if end_str == "24:00":
            end_dt = start_dt.replace(hour=0, minute=0) + timedelta(days=1)
        else:
            end_dt = datetime.strptime(f"{date_str} {end_str}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None, None
    if end_dt <= start_dt:
        end_dt += timedelta(days=1)  # 跨午夜時段（例如「深夜」23:00~06:00）
    return start_dt, end_dt


def _override_next_check(pending: dict, now: datetime) -> datetime | None:
    """回傳這筆覆蓋「下一次該被看一眼」的時間點（可能是過去，代表現在就該處理）。資料
    壞掉一律回 `now` 讓 `_process_one_override()` 有機會把它標記成 expired，不留殭屍。"""
    if pending["type"] == "override_check_time":
        check_dt = _parse_dt(pending["check_date"], pending["check_time"])
        if check_dt is None:
            return now
        deadline = _parse_iso(pending["retry_deadline"])
        if deadline is not None and now > deadline:
            return now
        if pending["next_retry_at"] is None:
            return max(check_dt, now)
        return _parse_iso(pending["next_retry_at"]) or now

    start_dt, end_dt = _parse_window(
        pending["check_date"], pending["check_time_start"], pending["check_time_end"]
    )
    if start_dt is None:
        return now
    if now > end_dt:
        return now
    if now < start_dt:
        return start_dt
    return _parse_iso(pending["next_retry_at"]) or now


class CustomScheduleRunner:
    def __init__(
        self,
        schedule_store: ScheduleListStore,
        main_loop: MainLoop,
        gossip_store: GossipStore | None = None,
        settings: SettingsStore | None = None,
        completion_watch_store: Any = None,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._schedule_store = schedule_store
        self._main_loop = main_loop
        self._gossip_store = gossip_store
        # 番劇完結偵測（completion_detection.md）判定為完結的 sn 不再自動檢查更新
        # （處置＝mark 時；unsubscribe 處置本來就把排程項目移掉了）。None＝沒接、不過濾。
        self._completion_watch_store = completion_watch_store
        # 「重新檢查排程更新」執行期間暫停觸發用（階段 4）。None＝沒接（測試用），視為不暫停。
        self._settings = settings
        self._now_fn = now_fn
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # {sn: (下次複查時間, 複查截止時間)}——排程時間到了但新集數還沒上架時，在時窗內
        # 持續複查用（見 _EPISODE_RETRY_WINDOW）。
        self._episode_retries: dict[int, tuple[datetime, datetime]] = {}

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
            # 「重新檢查排程更新」執行期間暫停觸發（不中斷進行中的下載）。錯過的自訂
            # 時段這一週就不補了——「重新檢查」本來就會把漏的都找出來，見階段 4。
            # 放在計算 wake 時間之前：到期的監視公告覆蓋會讓 wake 時間變成 0，如果在
            # 下面才 `continue` 會變成滿速空轉（wait(0) → continue → wait(0)…）。
            if self._settings is not None and recheck_pause.is_active(self._settings):
                self._stop_event.wait(_RECHECK_PAUSE_POLL_SECONDS)
                continue

            entries = self._custom_schedule_entries()
            now = self._now_fn()
            entry_sns = {entry.sn for entry in entries}
            # 追蹤項目被移除／被判完結 → 連帶清掉它殘留的複查
            self._episode_retries = {
                sn: v for sn, v in self._episode_retries.items() if sn in entry_sns
            }
            occurrences = {
                entry.sn: _next_occurrence(
                    entry.schedule_weekday, entry.schedule_hour, entry.schedule_minute, now
                )
                for entry in entries
            }
            override_points = [
                point
                for pending in self._active_overrides()
                if (point := _override_next_check(pending, now)) is not None
            ]

            wake_times = (
                list(occurrences.values())
                + override_points
                + [next_at for next_at, _deadline in self._episode_retries.values()]
            )
            if not wake_times:
                self._stop_event.wait(_EMPTY_RECHECK_SECONDS)
                continue

            # 上限 _EMPTY_RECHECK_SECONDS：新加的自訂時段項目 / 監視公告覆蓋要能在有限
            # 時間內被下一輪看到，不能因為最近一個 wake 在很久以後就睡死那麼久
            wait_seconds = min(
                _EMPTY_RECHECK_SECONDS, max(0.0, (min(wake_times) - now).total_seconds())
            )
            self._stop_event.wait(wait_seconds)
            if self._stop_event.is_set():
                break

            self._trigger_due_entries(entries, occurrences)
            self._process_due_episode_retries(entries)
            self._process_due_overrides()

    def _custom_schedule_entries(self) -> list[ScheduleEntry]:
        completed = (
            self._completion_watch_store.completed_sns()
            if self._completion_watch_store is not None
            else set()
        )
        return [
            entry
            for entry in self._schedule_store.get_entries().values()
            if entry.schedule_weekday is not None and entry.sn not in completed
        ]

    def _trigger_due_entries(
        self, entries: list[ScheduleEntry], occurrences: dict[int, datetime]
    ) -> None:
        now = self._now_fn()
        deadline = now + timedelta(seconds=_TRIGGER_TOLERANCE_SECONDS)
        for entry in entries:
            if occurrences[entry.sn] > deadline:
                continue
            submitted = 0
            try:
                submitted = self._main_loop.check_and_download(entry)
            except Exception:
                logger.exception("%s 自訂時段觸發時發生未預期的例外", _entry_label(entry))
            if submitted:
                self._episode_retries.pop(entry.sn, None)
            else:
                # 這次沒抓到新集數——動畫瘋常常比「更新時間」晚幾分鐘才真的上架，開一個
                # 複查時窗，別放到下週（不然永遠慢一集）。
                self._episode_retries[entry.sn] = (
                    now + _EPISODE_RETRY_TIERS[0][1], now + _EPISODE_RETRY_WINDOW,
                )

    def _process_due_episode_retries(self, entries: list[ScheduleEntry]) -> None:
        now = self._now_fn()
        deadline = now + timedelta(seconds=_TRIGGER_TOLERANCE_SECONDS)
        by_sn = {entry.sn: entry for entry in entries}
        for sn, (next_at, window_end) in list(self._episode_retries.items()):
            if next_at > deadline:
                continue
            elapsed = now - (window_end - _EPISODE_RETRY_WINDOW)
            interval = _episode_retry_interval(elapsed)
            if interval is None or now > window_end or sn not in by_sn:
                self._episode_retries.pop(sn, None)
                if sn in by_sn:
                    # 使用者 2026-09-05：複查 10 分鐘仍沒有新集數 → 明確記在日誌（不是靜靜放掉）
                    logger.info(
                        "%s 更新時間到後複查 10 分鐘仍沒有新集數，這次略過（等下一次排程時間）",
                        _entry_label(by_sn[sn]),
                    )
                continue
            submitted = 0
            try:
                submitted = self._main_loop.check_and_download(by_sn[sn])
            except Exception:
                logger.exception("%s 排程複查時發生未預期的例外", _entry_label(by_sn[sn]))
            if submitted:
                self._episode_retries.pop(sn, None)  # 抓到了，收工，等下週
            else:
                self._episode_retries[sn] = (now + interval, window_end)

    # ------------------------------------------------------------------
    # 監視公告的覆蓋排程（override_check_time / override_check_window）
    # ------------------------------------------------------------------

    def _active_overrides(self) -> list[dict]:
        if self._gossip_store is None:
            return []
        return [
            pending
            for pending in self._gossip_store.list_pending("pending")
            if pending["type"] in _OVERRIDE_TYPES
        ]

    def _process_due_overrides(self) -> None:
        now = self._now_fn()
        deadline = now + timedelta(seconds=_TRIGGER_TOLERANCE_SECONDS)
        for pending in self._active_overrides():
            point = _override_next_check(pending, now)
            if point is None or point > deadline:
                continue
            try:
                self._process_one_override(pending, now)
            except Exception:
                logger.exception("處理監視公告覆蓋排程 id=%s 時發生未預期的例外", pending["id"])

    def _process_one_override(self, pending: dict, now: datetime) -> None:
        assert self._gossip_store is not None  # _active_overrides() 已保證
        expect = max(1, int(pending["expect_episode_count"]))
        entry = self._entry_for(pending["sn"])

        if pending["type"] == "override_check_time":
            check_dt = _parse_dt(pending["check_date"], pending["check_time"])
            if check_dt is None:
                self._gossip_store.update_pending(pending["id"], status="expired")
                return
            deadline = _parse_iso(pending["retry_deadline"])
            if deadline is not None and now > deadline:
                self._finish(pending, pending["found_episode_count"])
                return
            if now < check_dt:
                return
            interval = pending["retry_interval_minutes"] or 30
        else:  # override_check_window
            start_dt, end_dt = _parse_window(
                pending["check_date"], pending["check_time_start"], pending["check_time_end"]
            )
            if start_dt is None:
                self._gossip_store.update_pending(pending["id"], status="expired")
                return
            if now < start_dt:
                return
            if now > end_dt:
                self._finish(pending, pending["found_episode_count"])
                return
            interval = pending["retry_interval_minutes"] or 1

        submitted = self._safe_check(entry, all_episodes=expect >= 2)
        found = pending["found_episode_count"] + submitted
        if found >= expect:
            self._gossip_store.update_pending(pending["id"], status="done", found_episode_count=found)
        else:
            self._gossip_store.update_pending(
                pending["id"],
                found_episode_count=found,
                next_retry_at=(now + timedelta(minutes=interval)).isoformat(),
            )

    def _finish(self, pending: dict, found: int) -> None:
        assert self._gossip_store is not None
        self._gossip_store.update_pending(
            pending["id"], status="done" if found > 0 else "expired", found_episode_count=found
        )

    def _entry_for(self, sn: int) -> ScheduleEntry:
        # 覆蓋排程的 sn 未必還在追蹤清單裡（使用者可能事後移除），查不到就用最小的
        # ScheduleEntry（mode 預設 → 抓最新一集），expect>=2 時 check_and_download 會
        # 被要求 force_all_episodes 補足
        return self._schedule_store.get_entries().get(sn) or ScheduleEntry(sn=sn)

    def _safe_check(self, entry: ScheduleEntry, *, all_episodes: bool) -> int:
        try:
            return self._main_loop.check_and_download(entry, force_all_episodes=all_episodes)
        except Exception:
            logger.exception("sn=%s 監視公告覆蓋觸發檢查時發生未預期的例外", entry.sn)
            return 0
