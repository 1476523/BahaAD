"""「新番快訊」入口／`/newanime*` route 的時效判斷。規格 §6a、§9。

- `pending_note_gone_at` 還沒設（GNN 文章「授權流程」備註還在、還在掃描）→ 永遠可見。
- 設了 → 那天起 **+6 天的 23:59:59** 為最後可見（「實際天數 +6，不用算太精」）。
  過了 → 右側入口不顯示、`/newanime*` 一律導回首頁。**但背景追蹤照舊跑**（那不看這個）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Mapping, Sequence

_VISIBLE_DAYS_AFTER_PENDING_GONE = 6


def visible_until(bulletin: Mapping) -> datetime | None:
    """最後可見時刻；`None` ＝沒有上限（文章還在更新）。"""
    gone = bulletin.get("pending_note_gone_at")
    if not gone:
        return None
    try:
        base = datetime.fromisoformat(gone)
    except (TypeError, ValueError):
        return None
    last_day = (base + timedelta(days=_VISIBLE_DAYS_AFTER_PENDING_GONE)).date()
    return datetime.combine(last_day, datetime.max.time()).replace(microsecond=0)


def is_visible(bulletin: Mapping, now: datetime) -> bool:
    until = visible_until(bulletin)
    return until is None or now <= until


def first_visible(bulletins: Sequence[Mapping], now: datetime) -> Mapping | None:
    """`store.list_bulletins()` 已按 season_key 由新到舊排序——回第一個還在時效內的。"""
    for bulletin in bulletins:
        if is_visible(bulletin, now):
            return bulletin
    return None
