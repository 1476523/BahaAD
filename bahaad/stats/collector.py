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

    def record_subscription(self, first_ep_sn, title, *, subscribed: bool) -> None:
        """訂閱列表的訂閱／退訂。`first_ep_sn` 應已是首集 sn（呼叫端先過
        `resolve_first_ep_sn`）。"""
        self._set_state(first_ep_sn, title, subscription=1 if subscribed else 0)

    def record_favorite(self, first_ep_sn, title, *, favorited: bool) -> None:
        """新番快訊的收藏／取消收藏。收藏轉訂閱時 **不呼叫這裡的 unfavorite**——
        `favorite` 狀態留 1（人數不歸零，使用者：「類似期待該番劇播出的人數值」），
        只另外 `record_subscription(subscribed=True)`。"""
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
