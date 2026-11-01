"""「新番快訊」入口／`/newanime*` route 的時效判斷。規格 §6a、§9。

- `pending_note_gone_at` 還沒設（GNN 文章「授權流程」備註還在、還在掃描）→ 永遠可見。
- 設了 → 那天起 **+6 天的 23:59:59** 為最後可見（「實際天數 +6，不用算太精」）。
  過了 → 右側入口不顯示、`/newanime*` 一律導回首頁。**但背景追蹤照舊跑**（那不看這個）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Callable, Mapping, Sequence

_VISIBLE_DAYS_AFTER_PENDING_GONE = 6
# 時間待定的新番（沒有首播日）剛被偵測到多久內，入口還是留著（使用者 2026-10-20：晚公布的《赤河戀影》
# 時間待定，公告時效已過，入口就消失了、看不到新番快訊）。過了這段時間還定不下來才不再撐著入口。
_UNDETERMINED_GRACE_DAYS = 14


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


def _recently_seen(item: Mapping, today: date) -> bool:
    seen = item.get("first_seen_at")
    if not seen:
        return False
    try:
        return (today - date.fromisoformat(str(seen)[:10])).days <= _UNDETERMINED_GRACE_DAYS
    except ValueError:
        return False


def has_unfinished_items(items: Sequence[Mapping], today: date) -> bool:
    """這一季還有「還沒開播」的新番（`upcoming` 且首播日是今天或之後）。時間待定（沒有首播日）的只算
    「剛被偵測到 `_UNDETERMINED_GRACE_DAYS` 天內」的，不然一個永遠定不下來的項目會讓入口一直留著。"""
    for item in items:
        if item.get("stage") != "upcoming":
            continue
        first = item.get("first_air_date")
        if not first:
            if _recently_seen(item, today):
                return True
            continue
        try:
            if date.fromisoformat(str(first)[:10]) >= today:
                return True
        except ValueError:
            continue
    return False


def first_visible(
    bulletins: Sequence[Mapping],
    now: datetime,
    *,
    items_for: Callable[[Mapping], Sequence[Mapping]] | None = None,
) -> Mapping | None:
    """只看**最新**的那一季公告（`store.list_bulletins()` 已按 season_key 由新到舊排序）。

    使用者 2026-10-12：十月季過了時效、還剩最後一部沒播，新番快訊卻退回顯示更舊的七月季（舊季度
    提早偵測留下的 `gnn_sn=0` 假公告永遠可見），剩下那部也跟著消失。所以：
    - 最新一季還在時效內 → 回它。
    - 過了時效，但這一季還有沒開播的新番（給了 `items_for` 才判斷）→ 繼續回它，等全部開播完才隱藏。
    - 其他情況 → `None`（入口隱藏），**不退回更舊的季度**。"""
    if not bulletins:
        return None
    newest = bulletins[0]
    if is_visible(newest, now):
        return newest
    if items_for is not None and has_unfinished_items(items_for(newest), now.date()):
        return newest
    return None
