"""追蹤 → 訂閱 轉換。規格 docs/requirements/new_anime_bulletin.md §7、§7c。

番劇正式上架後（出現在動畫瘋週期表）→ 用原始名稱比對出真 `video_sn` → 有追蹤的自動
寫進正式 `schedule_entries`（新番版的鈴鐺），並立刻補抓已上架的集數；沒追蹤的只是把
item 標成 `aired`（從新番快訊清單淡出）。

`first_ep_count > 1`（首集一次上多集）或 `backfill_from_episode`（首播是第 N 話、前面
要補）→ 轉訂閱後進 `catching_up`，每輪 `check_and_download(force_all_episodes=True)`
直到目標話數都抓到（或超過 `_CATCH_UP_MAX_DAYS` 上限）。

**掃描停止（GNN 文章定稿）後這個仍要繼續跑**——所以掛在 `NewAnimeWatcher` 的定時
tick 上，不是只在偵測到公告時。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable

from bahaad.newanime.watch import DEFAULT_NEWANIME_DETECT_ENABLED, NEWANIME_DETECT_KEY
from bahaad.scheduler.gossip_watch import build_search_title, season_compatible, season_number
from bahaad.store.youranimes_cache import title_base_key
from bahaad.subscription_ops import subscribe_sn

logger = logging.getLogger(__name__)

_CATCH_UP_MAX_DAYS = 14
# item.stage 已經「上架後」、不用再解析真 sn / 轉訂閱的
_POST_AIR_STAGES = ("aired", "converted", "catching_up", "done", "removed")


def resolve_from_weekly_schedule(source_name: str, schedule) -> int | None:
    """在週期表裡用番名找真 `video_sn`。番劇有在更新＝一定在週期表裡（那就是週期表的
    定義），所以這是主要路徑。名稱正規化沿用 gossip 的 `title_base_key` + 季別相容判斷
    （擋掉《相反的你和我》配到「第二季」）。"""
    if not schedule:
        return None
    want = title_base_key(source_name)
    _base, season = build_search_title(source_name or "")
    for weekly in schedule:
        for entry in weekly.entries or ():
            if title_base_key(entry.title) == want and season_compatible(
                season, season_number(entry.title)
            ):
                return entry.video_sn
    return None


def catch_up_target(item) -> int | None:
    """§7c 的「目標話數」。回 None＝不用捕齊（首集就一集、也沒有 backfill）。"""
    backfill = item.get("backfill_from_episode")
    if backfill:
        return int(backfill)
    if int(item.get("first_ep_count") or 1) > 1:
        return int(item.get("first_ep_number") or 1) + int(item["first_ep_count"]) - 1
    return None


def _hhmm(value: str | None) -> tuple[int, int] | None:
    try:
        h, m = str(value).split(":")
        return int(h), int(m)
    except (TypeError, ValueError):
        return None


class NewAnimeConverter:
    def __init__(
        self,
        settings,
        store,
        schedule_store,
        catalog,
        *,
        weekly_schedule_fn: Callable[[], list] | None = None,
        main_loop=None,
        now_fn: Callable[[], datetime] = datetime.now,
        stats_collector=None,
        anime_cache=None,
    ) -> None:
        self._settings = settings
        self._store = store
        self._schedule_store = schedule_store
        self._catalog = catalog
        self._weekly_schedule_fn = weekly_schedule_fn
        self._main_loop = main_loop
        self._now_fn = now_fn
        # 即時匿名使用統計（使用者 2026-09-08）：收藏轉訂閱時把 favorite 狀態從虛構 sn
        # 搬到真首集 sn（favorite 不歸零、subscription 設 1）。
        self._stats_collector = stats_collector
        self._anime_cache = anime_cache

    def set_main_loop(self, main_loop) -> None:
        self._main_loop = main_loop

    def _enabled(self) -> bool:
        return bool(self._settings.get(NEWANIME_DETECT_KEY, DEFAULT_NEWANIME_DETECT_ENABLED))

    # ------------------------------------------------------------------

    def run_once(self) -> None:
        if not self._enabled():
            return
        rows = [
            row
            for bulletin in self._store.list_bulletins()
            for row in self._store.list_items(bulletin["season_key"])
        ]
        need_resolve = [
            r for r in rows if r["real_video_sn"] is None and r["stage"] not in _POST_AIR_STAGES
        ]
        need_convert = [
            r for r in rows if r["real_video_sn"] is not None and r["stage"] not in _POST_AIR_STAGES
        ]
        catching = [r for r in rows if r["stage"] == "catching_up"]
        if not (need_resolve or need_convert or catching):
            return

        # 提早給 need_convert 用：不能只在 need_resolve 非空時才抓——real_video_sn 上一輪
        # 就解出來了、但排不出訂閱時段而卡住的項目（seasonal.php 提早偵測到、還沒等到
        # GNN 補時間），這一輪不會落進 need_resolve，也需要這份週期表當 _convert() 的
        # 時段備援來源（見 _subscription_slot()，使用者 2026-09-30 回報「追蹤的不會有
        # 任何動作」）。
        schedule = self._weekly_schedule() if (need_resolve or need_convert) else None

        if need_resolve:
            for row in need_resolve:
                video_sn = resolve_from_weekly_schedule(row["source_name"], schedule)
                if video_sn is None:
                    continue
                self._store.set_item_enrichment(row["virtual_sn"], real_video_sn=video_sn)
                row["real_video_sn"] = video_sn
                need_convert.append(row)

        tracked = self._store.list_tracked()
        for row in need_convert:
            try:
                self._convert(row, tracked, schedule)
            except Exception:  # noqa: BLE001
                logger.exception("新番快訊：轉訂閱失敗（virtual_sn=%s）", row["virtual_sn"])

        for row in catching:
            try:
                self._catch_up_tick(row)
            except Exception:  # noqa: BLE001
                logger.exception("新番快訊：集數捕齊失敗（virtual_sn=%s）", row["virtual_sn"])

    def _weekly_schedule(self):
        if self._weekly_schedule_fn is not None:
            try:
                return self._weekly_schedule_fn()
            except Exception:  # noqa: BLE001
                logger.warning("新番快訊：抓週期表失敗", exc_info=True)
        return None

    def _convert(self, row: dict, tracked: set[int], schedule=None) -> None:
        virtual_sn = row["virtual_sn"]
        real_sn = row["real_video_sn"]
        now_iso = self._now_fn().isoformat(timespec="seconds")

        if virtual_sn not in tracked:
            # 沒追蹤 → 只從清單淡出，不建立訂閱（§7.2）
            self._store.set_item_stage(virtual_sn, "aired")
            return

        weekday, time_hhmm = self._subscription_slot(row, schedule)
        parsed = _hhmm(time_hhmm)
        if weekday is None or parsed is None:
            # 排不出時段就先不轉（下輪再試）——比照 web 鈴鐺「查不到時段不建立殭屍訂閱」
            logger.info("新番快訊：virtual_sn=%s 排不出訂閱時段，這輪先不轉", virtual_sn)
            return
        display_name = row.get("display_name") or None
        subscribe_sn(self._schedule_store, real_sn, weekday, parsed[0], parsed[1], rename=display_name)
        self._store.untrack(virtual_sn)
        if self._stats_collector is not None:
            try:
                from bahaad.stats.collector import resolve_first_ep_sn

                first_sn, cached_title = resolve_first_ep_sn(self._anime_cache, real_sn)
                name = display_name or row.get("source_name") or cached_title
                self._stats_collector.migrate_favorite(virtual_sn, first_sn, name)
            except Exception:  # noqa: BLE001
                logger.debug("新番快訊：stats migrate_favorite 失敗", exc_info=True)

        target = catch_up_target(row)
        if target is not None and self._main_loop is not None:
            self._store.set_item_stage(virtual_sn, "catching_up")
            self._store.set_item_enrichment(
                virtual_sn,
                converted_at=now_iso,
                catch_up_target=target,
                catch_up_deadline=(self._now_fn() + timedelta(days=_CATCH_UP_MAX_DAYS)).isoformat(
                    timespec="seconds"
                ),
            )
        else:
            self._store.set_item_stage(virtual_sn, "converted")
            self._store.set_item_enrichment(virtual_sn, converted_at=now_iso)

        # 上架時可能已經有好幾集了——立刻補抓一次，不用等排定的星期
        if self._main_loop is not None:
            try:
                self._main_loop.trigger_manual_download(real_sn, display_name)
            except Exception:  # noqa: BLE001
                logger.exception("新番快訊：轉訂閱後立即補抓失敗（sn=%s）", real_sn)

    def _subscription_slot(self, row: dict, schedule=None) -> tuple[int | None, str | None]:
        """訂閱時段：有後續每週時段就用它，否則用首播時段；兩個都沒有（seasonal.php
        提早偵測到、GNN 公告還沒補上確切時間，`is_undetermined=True`）就直接查它現在
        在週期表上的真實時段當備援——`real_video_sn` 本來就是靠週期表比對出來的，那個
        時段就是它「現在真的在用」的時段，比等公告來填欄位更直接（使用者 2026-09-30
        回報：這種項目排不出時段，永遠卡在「這輪先不轉」，新番快訊追蹤形同沒有動作）。
        跟 `web/browse.py` 的 `_weekly_schedule_time()` 同一套 enumerate 對照法。"""
        if row.get("ongoing_weekday") and row.get("ongoing_time"):
            return int(row["ongoing_weekday"]), row["ongoing_time"]
        if row.get("first_air_weekday") and row.get("first_air_time"):
            return int(row["first_air_weekday"]), row["first_air_time"]
        real_sn = row.get("real_video_sn")
        if schedule and real_sn is not None:
            for weekday, weekly in enumerate(schedule, start=1):
                for entry in weekly.entries or ():
                    if entry.video_sn == real_sn:
                        parsed = _hhmm(entry.time_text)
                        if parsed is None:
                            return None, None
                        return weekday, f"{parsed[0]:02d}:{parsed[1]:02d}"
        return None, None

    def _catch_up_tick(self, row: dict) -> None:
        virtual_sn = row["virtual_sn"]
        real_sn = row["real_video_sn"]
        entry = self._schedule_store.get_entries().get(real_sn)
        deadline = row.get("catch_up_deadline")
        over_deadline = False
        if deadline:
            try:
                over_deadline = self._now_fn() > datetime.fromisoformat(deadline)
            except ValueError:
                over_deadline = False
        if entry is None or self._main_loop is None or over_deadline:
            self._store.set_item_stage(virtual_sn, "converted")
            return

        submitted = self._main_loop.check_and_download(entry, force_all_episodes=True)
        target = int(row.get("catch_up_target") or 1)
        latest = None
        try:
            latest = self._catalog.latest_main_episode_number(real_sn)
        except Exception:  # noqa: BLE001
            logger.debug("新番快訊：查最新集數失敗（sn=%s）", real_sn, exc_info=True)
        if submitted == 0 and latest is not None and latest >= target:
            self._store.set_item_stage(virtual_sn, "converted")
