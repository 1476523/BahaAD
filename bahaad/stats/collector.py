"""接收使用統計事件、累加進 `StatsPendingStore`。規格見
docs/requirements/realtime_stats.md。

各整合點（下載完成、訂閱／收藏變動、播放開始／看完）呼叫這裡的方法——只是一次
DB upsert，很快、執行緒安全、不打網路。實際送出到伺服器是 `StatsReporter` 的事。

開關 `realtime_stats_enabled`（預設開）關掉時所有方法直接 no-op——每次呼叫即時檢查
設定、不快取（比照 `DiagnosticsReporter`）。呼叫端埋點時完全不用擔心被拖慢或被
例外波及：這裡任何錯誤都吞掉、只記 debug。

**番劇 key ＝首集 sn**（`resolve_first_ep_sn()`）：訂閱記的常是「訂閱當下最新一集」的
sn，用 `anime_cache.episode_group` 的 `group_key`（＝該番劇集數集合最小值＝首集）
統一，讓「同一部番劇不同集」的事件記在同一個 key 上。
"""

from __future__ import annotations

import logging

from bahaad.store.stats_pending import StatsPendingStore
from bahaad.store.settings import SettingsStore

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "realtime_stats_enabled"

# stats_pending_counters 的 key
COUNTER_DOWNLOADS = "downloads"


def _expand_to_group(deps, sn: int) -> set[int]:
    """把 `sn` 展開成「同一部番劇（episode_group）已知的全部集數 sn」——擁有者排程
    清單記的 sn 跟訂閱者追蹤記的 sn 是在不同時間點各自正規化出來的，即使是同一部
    番劇，兩邊存的未必是同一個 sn（使用者 2026-09-17 回報：54 部訂閱番劇，排行榜
    「番劇訂閱數」卻大多只顯示 1，根因就是這裡沒展開——owner 用 sn A、訂閱者用
    sn B，`subscription_count()` 各自只查自己那個 sn，永遠看不到對方那一份）。查不到
    群組（這個 sn 自己的詳細頁從沒被瀏覽過）就退回只有自己這一個 sn，維持原本行為。"""
    anime_cache = getattr(deps, "anime_cache", None)
    if anime_cache is None:
        return {sn}
    try:
        expanded, _origin = anime_cache.resolve_subscription_expansion([sn])
    except Exception:  # noqa: BLE001
        return {sn}
    return expanded or {sn}


def subscription_count(deps, first_ep_sn: int) -> int:
    """這台裝置對某部番劇（`first_ep_sn` 應已是首集 sn，但不要求跟擁有者排程／
    訂閱者追蹤存的 sn 剛好一致——見 `_expand_to_group()`）目前算出來的訂閱人數：
    擁有者本人在排程清單有訂閱算 1，再加上每個還在追蹤的訂閱者身分各算 1（使用者
    2026-09-16：「番劇訂閱數」要能反映擁有者跟各訂閱者身分分別累加，不是這台裝置
    只回報「有沒有人在追蹤」的 0/1）。呼叫端（owner 訂閱／退訂鈴鐺、訂閱者
    follow／unfollow）都是「異動排程清單或追蹤清單之後，當場重新算一次完整的
    人數」，不是各自累加各自的那一份——這樣不管哪一邊先變動，兩邊看到的都是同一個
    當下最新的總數，不會因為誰後寫入就把另一邊蓋掉。"""
    sns = _expand_to_group(deps, first_ep_sn)
    schedule_store = getattr(deps, "schedule_store", None)
    entries = schedule_store.get_entries() if schedule_store is not None else {}
    owner_subscribed = any(
        (entry := entries.get(sn)) is not None and entry.schedule_weekday is not None for sn in sns
    )
    subscriber_store = getattr(deps, "subscriber_store", None)
    follower_ids: set[int] = set()
    if subscriber_store is not None:
        for sn in sns:
            for identity in subscriber_store.identities_following(sn):
                follower_ids.add(identity["id"])
    return (1 if owner_subscribed else 0) + len(follower_ids)


def resync_all_subscriptions(deps) -> None:
    """把目前所有訂閱狀態（擁有者排程清單 + 各訂閱者身分的追蹤清單）一次全部重新
    同步進即時統計（使用者 2026-09-16 實測發現：排行榜「番劇訂閱數」完全是空的，
    即使本機確實有幾十部訂閱中的番劇）。

    根因：`record_subscription()` 只在「使用者當下按鈴鐺／訂閱者當下 follow」那個
    事件發生的瞬間才會被呼叫——這次上線之前就已經存在的訂閱、或透過排程清單文字
    編輯器（`schedule.py` 的 `replace_from_text()`，完全繞過鈴鐺路由）整批修改的
    項目，從來沒有機會觸發那個事件，於是永遠不會出現在統計裡。跟單一 sn 的
    `_stats_subscription()`/`_stats_sync_subscription()` 用同一份 `subscription_
    count()` 計算邏輯，只是這裡一次掃過「目前所有」有訂閱狀態的 sn，不管訂閱是
    怎麼建立的都能自我修復，不用去追殺每一個修改路徑。呼叫端（`app_shell.py`）
    在啟動時呼叫一次即可；重複呼叫也安全（純粹是冪等的目標狀態覆蓋）。"""
    collector = getattr(deps, "stats_collector", None)
    if collector is None:
        return
    schedule_store = getattr(deps, "schedule_store", None)
    subscriber_store = getattr(deps, "subscriber_store", None)
    anime_cache = getattr(deps, "anime_cache", None)

    raw_sns: set[int] = set()
    try:
        entries = schedule_store.get_entries() if schedule_store is not None else {}
    except Exception:  # noqa: BLE001
        entries = {}
    for sn, entry in entries.items():
        if entry.schedule_weekday is not None:
            raw_sns.add(sn)
    if subscriber_store is not None:
        try:
            raw_sns.update(subscriber_store.all_followed_sns())
        except Exception:  # noqa: BLE001
            logger.debug("stats: resync 讀取訂閱者追蹤清單失敗", exc_info=True)

    # 同一部番劇的 sn，擁有者排程跟訂閱者追蹤未必存的是同一個（見 `_expand_to_group()`
    # 的說明）——先用 episode_group 併成一部番劇一個代表 sn 再統計，不然同一部番劇會
    # 在排行榜上以兩個不同 sn 各自出現、人數也各自被拆開只算到一半（使用者 2026-09-17
    # 回報：54 部訂閱番劇，排行榜卻大多只顯示 1）。查不到群組的 sn（沒瀏覽過詳細頁）
    # 就用它自己當代表，維持原本行為。
    seen_groups: set[int] = set()
    reporting_sns: set[int] = set()
    for sn in raw_sns:
        group_key = None
        if anime_cache is not None:
            try:
                group_key = anime_cache.group_key_for(sn)
            except Exception:  # noqa: BLE001
                group_key = None
        if group_key is None:
            reporting_sns.add(sn)
            continue
        if group_key in seen_groups:
            continue
        seen_groups.add(group_key)
        reporting_sns.add(group_key)

    for sn in reporting_sns:
        try:
            entry = entries.get(sn)
            title = getattr(entry, "rename", None) or (
                anime_cache.anime_title_for(sn) if anime_cache is not None else None
            )
            collector.record_subscription(sn, title, count=subscription_count(deps, sn))
        except Exception:  # noqa: BLE001 - 埋點不能拖垮啟動流程，單一 sn 失敗跳過即可
            logger.debug("stats: resync sn=%s 失敗", sn, exc_info=True)


def resolve_first_ep_sn(anime_cache, video_sn: int) -> tuple[int, str | None]:
    """把任一集的 video_sn 換成該番劇首集 sn ＋ 標題。快取查不到就原樣回傳、標題 None。"""
    try:
        sn = int(video_sn)
    except (TypeError, ValueError):
        return video_sn, None
    if anime_cache is None:
        return sn, None
    try:
        group_key = anime_cache.group_key_for(sn)
        title = anime_cache.anime_title_for(sn)
    except Exception:  # noqa: BLE001
        return sn, None
    return (group_key or sn), title


class StatsCollector:
    def __init__(self, settings: SettingsStore, pending: StatsPendingStore) -> None:
        self._settings = settings
        self._pending = pending

    def _enabled(self) -> bool:
        return bool(self._settings.get(_SETTINGS_KEY, True))

    def record_download(self, count: int = 1) -> None:
        """成功下載一集就 +1（`count` 給補記用）。"""
        if not self._enabled() or count <= 0:
            return
        try:
            self._pending.add_counter(COUNTER_DOWNLOADS, count)
        except Exception:  # noqa: BLE001 - 埋點不能拖垮下載流程
            logger.debug("stats: record_download 失敗（不影響下載）", exc_info=True)

    def record_subscription(self, first_ep_sn, title, *, count: int) -> None:
        """訂閱列表的訂閱／退訂。`first_ep_sn` 應已是首集 sn（呼叫端先過
        `resolve_first_ep_sn`）；`count` 是呼叫端用 `subscription_count()` 當場重新
        算出來的完整人數（擁有者 + 各訂閱者身分），不是單純 0/1。"""
        self._set_state(first_ep_sn, title, subscription=max(0, int(count)))

    def record_favorite(self, first_ep_sn, title, *, favorited: bool) -> None:
        """新番快訊的收藏／取消收藏。收藏轉訂閱時 **不呼叫這裡的 unfavorite**——
        `favorite` 狀態留 1（人數不歸零，使用者：「類似期待該番劇播出的人數值」），
        只另外 `record_subscription(count=...)`。"""
        self._set_state(first_ep_sn, title, favorite=1 if favorited else 0)

    def migrate_favorite(self, from_sn, to_sn, title) -> None:
        """新番收藏（虛構 sn）轉成正式訂閱（真首集 sn）時：把收藏狀態從虛構 sn 搬到
        真 sn（虛構 sn 設 0、真 sn 設 favorite=1 + subscription=1）。"""
        if not self._enabled():
            return
        try:
            fs = int(from_sn)
            ts = int(to_sn)
        except (TypeError, ValueError):
            return
        try:
            if fs > 0 and fs != ts:
                self._pending.set_anime_state(fs, favorite=0)
            if ts > 0:
                self._pending.set_anime_state(ts, title=title or None, favorite=1, subscription=1)
        except Exception:  # noqa: BLE001
            logger.debug("stats: migrate_favorite 失敗", exc_info=True)

    def record_view(self, first_ep_sn, title) -> None:
        """開始播放一集 → 該番劇總觀看次數 +1。公開模式關掉也算。"""
        self._add_count(first_ep_sn, title, views=1)

    def record_completion(self, first_ep_sn, title) -> None:
        """播放進度達門檻（播放器「看完」判定）→ 看完次數 +1。"""
        self._add_count(first_ep_sn, title, completions=1)

    # ---- 內部 -----------------------------------------------------------

    @staticmethod
    def _clean_sn(first_ep_sn) -> int | None:
        try:
            sn = int(first_ep_sn) if first_ep_sn is not None else None
        except (TypeError, ValueError):
            return None
        return sn if sn and sn > 0 else None

    def _set_state(self, first_ep_sn, title, **state) -> None:
        if not self._enabled():
            return
        sn = self._clean_sn(first_ep_sn)
        if sn is None:
            return
        try:
            self._pending.set_anime_state(sn, title=title or None, **state)
        except Exception:  # noqa: BLE001
            logger.debug("stats: 記番劇狀態失敗（不影響主流程）", exc_info=True)

    def _add_count(self, first_ep_sn, title, **counts) -> None:
        if not self._enabled():
            return
        sn = self._clean_sn(first_ep_sn)
        if sn is None:
            return
        try:
            self._pending.add_anime_count(sn, title=title or None, **counts)
        except Exception:  # noqa: BLE001
            logger.debug("stats: 記番劇次數失敗（不影響主流程）", exc_info=True)
