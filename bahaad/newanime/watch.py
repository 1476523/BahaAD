"""`NewAnimeWatcher` 背景執行緒——新番快訊的掃描生命週期。

規格 docs/requirements/new_anime_bulletin.md §2。

- `GossipWatcher.check_once()` 跑完 → 呼叫 `notify_gossip_scan(snapshot)`。
- 偵測到「新番節目資訊」公告（`detect_bulletin_gnn_sn`）→ 排一次掃描。
- 掃描：睡 `_gnn_scan_delay`（+5 秒，公告掃完才動）→ 對每個「授權流程備註還沒消失」的
  bulletin 抓 GNN 文章、`parse_and_sync_gnn`、diff → `newanime_change_log`。
- 備註消失 → 那一季不再抓 GNN。

**這一階段不發通知、沒有頁面**——階段 5 讀 `newanime_change_log` 發通知、階段 6/7 做頁面。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from time import monotonic
from typing import Callable

from bahaad.newanime.detect import detect_bulletin_gnn_sn
from bahaad.newanime.enrich import (
    match_key,
    parse_youranimes_covers,
    parse_youranimes_directors,
    seasonal_by_key,
)
from bahaad.newanime.fetch import NewAnimeFetcher, NewAnimeFetchError
from bahaad.newanime.ingest import GnnSyncResult, parse_and_sync_gnn
from bahaad.newanime.parse import parse_gnn_article
from bahaad.scheduler.gossip_watch import GossipSnapshot
from bahaad.store.newanime_cache import NewAnimeCacheStore
from bahaad.store.settings import SettingsStore

logger = logging.getLogger(__name__)

NEWANIME_DETECT_KEY = "newanime_detect_enabled"
DEFAULT_NEWANIME_DETECT_ENABLED = True

_DEFAULT_GNN_SCAN_DELAY_SECONDS = 5.0
_DEFAULT_SEASONAL_EXTRA_DELAY_SECONDS = 5.0  # GNN +5 秒後再等 5 秒 = seasonal.php +10 秒
_DEFAULT_MIN_SCAN_INTERVAL_SECONDS = 120.0
_DEFAULT_CONVERT_INTERVAL_SECONDS = 120.0  # 追蹤→訂閱轉換 / 集數捕齊的定時 tick
# 詳細頁「待確認」的欄位——都補齊了就不用再抓 seasonal.php / youranimes
_ENRICH_FIELDS = ("studio", "tags", "subscribe_count", "seasonal_range", "director", "cover_url")
# 某部番從文章消失多久才算「被下架」（記進 change_log 給通知用）。使用者提示「不再追蹤」
# 的門檻是 24 小時（階段 9），那個更保守；這裡是通知用、可以短一點。
_REMOVAL_GRACE_SECONDS = 6 * 3600
# 有追蹤的番從新番表消失多久 → 站內通知問使用者「要不要不再追蹤」（§9）
_DISAPPEAR_PROMPT_HOURS = 24
_DISAPPEAR_KEEP_DAYS = 7  # 使用者選「繼續追蹤」後，這麼久不再提示；過了還沒回來 → 再問一次


class NewAnimeWatcher:
    def __init__(
        self,
        settings: SettingsStore,
        store: NewAnimeCacheStore,
        fetcher: NewAnimeFetcher,
        *,
        now_fn: Callable[[], datetime] = datetime.now,
        gnn_scan_delay_seconds: float = _DEFAULT_GNN_SCAN_DELAY_SECONDS,
        seasonal_extra_delay_seconds: float = _DEFAULT_SEASONAL_EXTRA_DELAY_SECONDS,
        min_scan_interval_seconds: float = _DEFAULT_MIN_SCAN_INTERVAL_SECONDS,
        notifier: "object | None" = None,
        converter: "object | None" = None,
        notification_store: "object | None" = None,
        convert_interval_seconds: float | None = _DEFAULT_CONVERT_INTERVAL_SECONDS,
    ) -> None:
        self._settings = settings
        self._store = store
        self._fetcher = fetcher
        self._now_fn = now_fn
        # 站內「訂閱通知」收件匣——追蹤的番從新番表消失時問使用者要不要不再追蹤（§9）
        self._notification_store = notification_store
        # `NewAnimeNotifier`（Any 型別避免 import 迴圈：notify.py 反過來 import 本模組的
        # NEWANIME_DETECT_KEY）。None＝不發通知（測試/組裝順序）。
        self._notifier = notifier
        # `NewAnimeConverter`——追蹤 → 訂閱轉換 + 集數捕齊。GNN 掃描停止後仍要跑，所以
        # `_run_loop` 每 `_convert_interval` 秒定時 tick 一次呼叫它（None＝不定時 tick）。
        self._converter = converter
        self._convert_interval = convert_interval_seconds
        self._gnn_scan_delay = float(gnn_scan_delay_seconds)
        self._seasonal_extra_delay = float(seasonal_extra_delay_seconds)
        self._min_scan_interval = float(min_scan_interval_seconds)
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_gnn_sn: int | None = None
        self._last_scan_at = 0.0  # monotonic

    def _enabled(self) -> bool:
        return bool(self._settings.get(NEWANIME_DETECT_KEY, DEFAULT_NEWANIME_DETECT_ENABLED))

    # ---- 生命週期 -----------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def notify_gossip_scan(self, snapshot: GossipSnapshot) -> None:
        """`GossipWatcher.check_once()` 跑完呼叫（非阻塞）。偵測新番快訊公告 → 叫醒掃描。"""
        if not self._enabled():
            return
        gnn_sn = detect_bulletin_gnn_sn(snapshot.text, snapshot.links)
        if gnn_sn is not None:
            self._pending_gnn_sn = gnn_sn
            self._wake.set()
        elif self._has_active_bulletin():
            self._wake.set()  # 沒新公告，但有還在追的季 → 繼續複掃

    def _has_active_bulletin(self) -> bool:
        return any(b["pending_note_gone_at"] is None for b in self._store.list_bulletins())

    def _has_periodic_work(self) -> bool:
        return self._converter is not None or self._notification_store is not None

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            woke = self._wake.wait(self._convert_interval if self._has_periodic_work() else None)
            self._wake.clear()
            if self._stop_event.is_set():
                return
            if woke:
                # 偵測到公告 / 有還在追的季 → 公告掃完才動（+5 秒），跑掃描 + 通知 + 補欄位
                if self._stop_event.wait(self._gnn_scan_delay):
                    return
                self._scan_notify_enrich()
            # 追蹤 → 訂閱轉換 / 集數捕齊：每次（不管是被叫醒還是定時 tick）都跑，
            # GNN 掃描停止後仍要繼續（§7「掃描停止後也繼續」）
            if self._converter is not None:
                try:
                    self._converter.run_once()
                except Exception:  # noqa: BLE001
                    logger.exception("新番快訊轉訂閱發生未預期的例外")
            # 追蹤的番從新番表消失 → 定時（文章定稿後也繼續）檢查要不要提示使用者
            try:
                self._check_all_disappearances()
            except Exception:  # noqa: BLE001
                logger.exception("新番快訊消失提示檢查發生未預期的例外")

    def _scan_notify_enrich(self) -> None:
        try:
            ran = self.scan_once()
        except Exception:  # noqa: BLE001
            logger.exception("新番快訊掃描發生未預期的例外")
            ran = False
        if not ran:
            return  # 這次被節流／關掉了 → 也不用去抓 seasonal/youranimes
        # 掃描 + diff 完 → 讀 newanime_change_log 發通知（首次一定發完整列表）
        if self._notifier is not None:
            try:
                self._notifier.notify_all()
            except Exception:  # noqa: BLE001
                logger.exception("新番快訊發通知發生未預期的例外")
        # seasonal.php / youranimes 再等 5 秒（＝公告 +10 秒）
        if self._stop_event.wait(self._seasonal_extra_delay):
            return
        try:
            self.enrich_once()
        except Exception:  # noqa: BLE001
            logger.exception("新番快訊補欄位發生未預期的例外")

    # ---- 掃描 --------------------------------------------------------

    def scan_once(self) -> bool:
        """回 True＝這次真的掃了；False＝被節流／功能關掉。"""
        if not self._enabled():
            return False
        now_mono = monotonic()
        if self._last_scan_at and now_mono - self._last_scan_at < self._min_scan_interval:
            return False  # 節流（避免一連串 gossip poke 把 GNN 打爆）
        self._last_scan_at = now_mono

        if self._pending_gnn_sn is not None:
            self._ensure_bulletin(self._pending_gnn_sn)
            self._pending_gnn_sn = None

        for bulletin in self._store.list_bulletins():
            if bulletin["pending_note_gone_at"] is not None:
                continue  # 那一季文章定稿了，不再抓 GNN
            self._scan_bulletin(bulletin)
        return True

    def _ensure_bulletin(self, gnn_sn: int) -> None:
        if self._store.get_bulletin_by_gnn_sn(gnn_sn) is not None:
            return
        html = self._fetch_gnn(gnn_sn)
        if html is None:
            return
        parsed = parse_gnn_article(html, fallback_year=self._now_fn().year)
        if not parsed.season_key:
            logger.warning("新番快訊：GNN 文章 sn=%s 解析不出季度（標題：%s）", gnn_sn, parsed.title)
            return
        self._store.upsert_bulletin(
            parsed.season_key, gnn_sn, article_published_at=parsed.published_at
        )
        logger.info(
            "偵測到新番節目資訊公告：%s 季（GNN 文章 sn=%s）", parsed.season_key, gnn_sn
        )
        self._apply_sync(parsed.season_key, gnn_sn, html)

    def _scan_bulletin(self, bulletin: dict) -> None:
        html = self._fetch_gnn(bulletin["gnn_sn"])
        if html is None:
            return
        self._apply_sync(bulletin["season_key"], bulletin["gnn_sn"], html)

    def _apply_sync(self, season_key: str, gnn_sn: int, html: str) -> None:
        now_iso = self._now_fn().isoformat(timespec="seconds")
        first_scan = self._store.get_bulletin(season_key)["first_scan_done_at"] is None
        try:
            result = parse_and_sync_gnn(self._store, html, season_key=season_key, now_iso=now_iso)
        except Exception:  # noqa: BLE001
            logger.exception("新番快訊：%s 季 GNN 同步失敗", season_key)
            return
        self._diff_and_log(season_key, result, now_iso, first_scan=first_scan)
        # 只有真的解析到番劇才算「首次掃描完成」——第一次抓就 parse 失敗（站方改版／
        # 暫時性錯誤）不能白白消耗掉首次，不然下輪就變成逐部發「新增」而不是完整列表
        if first_scan and result.seen_source_names:
            self._store.mark_first_scan_done(season_key, now_iso)
        if not result.pending_note_present:
            logger.info("新番快訊：%s 季文章已定稿（授權流程備註消失），停止複掃", season_key)

    def _diff_and_log(
        self, season_key: str, result: GnnSyncResult, now_iso: str, *, first_scan: bool
    ) -> None:
        # 首次掃描：不記個別「新增」（階段 5 首次一定發完整列表）
        if not first_scan:
            for virtual_sn in result.added_virtual_sns:
                item = self._store.get_item(virtual_sn) or {}
                self._store.log_change(
                    season_key, virtual_sn, "added", detected_at=now_iso,
                    new_weekday=item.get("first_air_weekday"), new_time=item.get("first_air_time"),
                )
        for change in result.time_changes:
            self._store.log_change(
                season_key, change.virtual_sn, "time_changed", detected_at=now_iso,
                old_weekday=change.old_weekday, old_time=change.old_time,
                new_weekday=change.new_weekday, new_time=change.new_time,
            )

        # 從文章消失夠久 → 記「下架」
        now_dt = self._now_fn()
        for row in self._store.list_active_items_missing_from(season_key, result.seen_source_names):
            last_seen = _parse_iso(row.get("last_seen_at"))
            if last_seen is None or (now_dt - last_seen).total_seconds() < _REMOVAL_GRACE_SECONDS:
                continue
            self._store.set_item_stage(row["virtual_sn"], "removed")
            self._store.log_change(
                season_key, row["virtual_sn"], "removed", detected_at=now_iso,
                old_weekday=row.get("first_air_weekday"), old_time=row.get("first_air_time"),
            )

        # 之前記為 removed、這次又出現 → 復活（sync_from_gnn 只更新欄位、不動 stage）
        for row in self._store.list_items(season_key):
            if row["stage"] == "removed" and row["source_name"] in result.seen_source_names:
                self._store.set_item_stage(row["virtual_sn"], "upcoming")
                self._store.log_change(
                    season_key, row["virtual_sn"], "re_added", detected_at=now_iso,
                    new_weekday=row.get("first_air_weekday"), new_time=row.get("first_air_time"),
                )

        # §9：有追蹤的番從文章消失 → 站內通知問要不要不再追蹤（用這次掃描實際看到的名單）
        self._check_disappearance_prompts(
            season_key, now_dt, present_names=result.seen_source_names
        )

    # ---- §9 消失提示 --------------------------------------------------

    def _check_all_disappearances(self) -> None:
        """定時 tick 用（文章定稿後也繼續）——沒有本次掃描名單，`stage='removed'` ＝已消失。"""
        if self._notification_store is None or not self._enabled():
            return
        now_dt = self._now_fn()
        for bulletin in self._store.list_bulletins():
            self._check_disappearance_prompts(bulletin["season_key"], now_dt, present_names=None)

    def _check_disappearance_prompts(self, season_key, now_dt, *, present_names) -> None:
        if self._notification_store is None:
            return
        tracked = self._store.list_tracked()
        if not tracked:
            return
        for row in self._store.list_items(season_key):
            virtual_sn = row["virtual_sn"]
            if virtual_sn not in tracked:
                continue
            if present_names is not None:
                gone = row["source_name"] not in present_names
            else:
                gone = row["stage"] == "removed"
            if not gone:
                continue
            last_seen = _parse_iso(row.get("last_seen_at"))
            if last_seen is None:
                continue
            tracked_row = self._store.get_tracked(virtual_sn) or {}
            keep_until = _parse_iso(tracked_row.get("keep_after_disappear_until"))
            prompted = _parse_iso(row.get("disappeared_prompt_at"))
            name = row.get("display_name") or row["source_name"]

            if keep_until is not None:
                # 「繼續追蹤」的一週保護期還沒過 → 不提示
                if now_dt < keep_until:
                    continue
                # 一週過了、還是沒回來、而且上次提示是在保護期開始前 → 再問一次
                if prompted is None or prompted < keep_until:
                    self._add_gone_notification(virtual_sn, name, week=True)
                    self._store.set_disappeared_prompt_at(
                        virtual_sn, now_dt.isoformat(timespec="seconds")
                    )
                continue

            if prompted is None and (now_dt - last_seen) >= timedelta(hours=_DISAPPEAR_PROMPT_HOURS):
                self._add_gone_notification(virtual_sn, name, week=False)
                self._store.set_disappeared_prompt_at(
                    virtual_sn, now_dt.isoformat(timespec="seconds")
                )

    def _add_gone_notification(self, virtual_sn: int, name: str, *, week: bool) -> None:
        if self._notification_store.has_unresolved_for_sn(virtual_sn, "newanime_gone"):
            return
        self._notification_store.add(
            kind="newanime_gone",
            sn=virtual_sn,
            anime_title=name,
            action_taken="gone_week" if week else "gone",
        )
        logger.info("新番快訊：《%s》從新番表消失，已發站內提示（week=%s）", name, week)

    def _fetch_gnn(self, gnn_sn: int) -> str | None:
        try:
            return self._fetcher.fetch_gnn_article(gnn_sn)
        except NewAnimeFetchError as exc:
            logger.warning("新番快訊：抓 GNN 文章失敗（sn=%s）：%s", gnn_sn, exc)
            return None

    # ---- seasonal.php / youranimes 補欄位 --------------------------------

    def enrich_once(self) -> None:
        """補 seasonal.php（製作廠商／標籤／授權範圍／訂閱人數）＋ youranimes（主視覺圖／
        導演監督）。所有 item 的 `_ENRICH_FIELDS` 都補齊就不再抓。"""
        if not self._enabled():
            return
        needy: dict[str, list[dict]] = {}
        for bulletin in self._store.list_bulletins():
            missing = [
                row
                for row in self._store.list_items(bulletin["season_key"])
                if row["stage"] not in ("removed", "done")
                and any(row.get(field) in (None, "") for field in _ENRICH_FIELDS)
            ]
            if missing:
                needy[bulletin["season_key"]] = missing
        if not needy:
            return

        seasonal = self._safe_fetch(self._fetcher.fetch_seasonal_page, "seasonal.php")
        seasonal_cards = seasonal_by_key(seasonal) if seasonal else {}

        for season_key, rows in needy.items():
            youranimes_html = self._safe_fetch(
                lambda: self._fetcher.fetch_youranimes_season(season_key),
                f"youranimes/{season_key}",
            )
            covers = parse_youranimes_covers(youranimes_html) if youranimes_html else {}
            directors = parse_youranimes_directors(youranimes_html) if youranimes_html else {}
            for row in rows:
                key = match_key(row["source_name"])
                updates: dict = {}
                card = seasonal_cards.get(key)
                if card is not None:
                    if not row.get("studio") and card.studio:
                        updates["studio"] = card.studio
                    if not row.get("tags") and card.tags:
                        updates["tags"] = "\n".join(card.tags)
                    if not row.get("subscribe_count") and card.subscribe_count:
                        updates["subscribe_count"] = card.subscribe_count
                    if not row.get("seasonal_range") and card.range_text:
                        updates["seasonal_range"] = card.range_text
                if not row.get("cover_url") and key in covers:
                    updates["cover_url"] = covers[key]
                if not row.get("director") and key in directors:
                    updates["director"] = directors[key]
                if updates:
                    self._store.set_item_enrichment(row["virtual_sn"], **updates)

    def _safe_fetch(self, fn: Callable[[], str], label: str) -> str | None:
        try:
            return fn()
        except NewAnimeFetchError as exc:
            logger.warning("新番快訊：抓 %s 失敗：%s", label, exc)
            return None


def _parse_iso(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
