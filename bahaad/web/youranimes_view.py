"""番劇詳細頁的 youranimes 補充資料——route 面向的 view-model。

在 request 內執行，純讀本地 DB（背景 job `scheduler/youranimes_sync.py` 已經把季度頁
抓好解析好了），不打網路、不動 `anime_data.anime_detail_data()`。

配對：週期表的原始中文標題優先、再退 `AnimeDetail.title`，用 `youranimes.match_record`
（重用 `gossip_watch` 的標題正規化）。配不到回 None → 詳細頁維持動畫瘋原簡介、不出右欄。
缺欄位就那一欄空著——18 禁／跨季延續播出的番劇季度卡片格常常沒有，但
`scheduler/youranimes_sync.py` 背景 job 已經會逐一補抓 `/animes/<id>` 個別頁把資料填進
快取（2026-09-05 起，見 docs/requirements/youranimes.md），這裡完全不用管來源，一律讀
本地快取。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bahaad.gamer_client.browse import AnimeDetail
from bahaad.scheduler.gossip_watch import build_search_title
from bahaad.store.youranimes_cache import title_base_key
from bahaad.youranimes.match import match_record
from bahaad.youranimes.models import YourAnimesCastMember, YourAnimesMusic, YourAnimesStaff

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class YourAnimesPanel:
    anime_id: int
    synopsis: str
    staff: tuple[YourAnimesStaff, ...]
    cast: tuple[YourAnimesCastMember, ...]
    music: tuple[YourAnimesMusic, ...]

    @property
    def has_right_column(self) -> bool:
        return bool(self.staff or self.cast or self.music)


def youranimes_panel(deps, detail: AnimeDetail) -> YourAnimesPanel | None:
    store = getattr(deps, "youranimes_cache", None)
    if store is None:
        return None

    titles = [t for t in (_weekly_schedule_title(deps, detail), detail.title) if t]
    try:
        rec = match_record(titles, store.find_by_base, store.find_by_base_prefix)
    except Exception:  # noqa: BLE001 - 補充資料失敗絕不能讓詳細頁掛掉
        logger.debug("youranimes 配對失敗（sn=%s）", getattr(detail, "video_sn", "?"), exc_info=True)
        return None
    if rec is None:
        return None
    logger.debug("youranimes 配到 sn=%s → anime_id=%s（%s）", detail.video_sn, rec.anime_id, rec.zh_title)
    return YourAnimesPanel(
        anime_id=rec.anime_id,
        synopsis=rec.synopsis,
        staff=rec.staff,
        cast=rec.cast,
        music=rec.music,
    )


def _weekly_schedule_title(deps, detail: AnimeDetail) -> str | None:
    """週期表裡這部番劇的原始中文標題（使用者要求用這個當配對鍵）。從首頁快取拿，
    不打網路——比照 `gossip_watch._weekly_schedule_match`。"""
    cache = getattr(deps, "anime_cache", None)
    if cache is None:
        return None
    try:
        home = cache.get_home()
    except Exception:  # noqa: BLE001
        return None
    if not home:
        return None
    _cards, schedule = home
    want = title_base_key(detail.title)
    for day in schedule:
        for entry in day.entries:
            if entry.video_sn == detail.video_sn:
                return entry.title
            if want and title_base_key(entry.title) == want:
                return entry.title
    return None
