"""`/newanime` 列表頁的分組／顯示字串。規格 §6b。

純函式（不碰 DB／HTTP）。`build_listing()` 把 `newanime_item` 的 row 整理成：
- `groups`：`週一`…`週日` 各一組（按更新時間排）、然後 `待定時間`（按文章原始順序）。
- `cards`：卡片格用的平面清單（依首播日期／時間／文章順序）。

每張卡／每列的顯示規則（§6b「番劇在清單裡待多久」）：
- 只有首播時間：`首播 <時間>`；首集確認播出後那部就 `stage` 轉走、不再出現。
- 有後續時間：`首播 <首播時間>；後續第 n 集為 <後續時間>`，掛在**首播星期**組；首集播出後
  （`stage='airing'`）改掛**後續星期**組、只顯示 `後續第 n 集為 <後續時間>`。
- 待定：`待定`。
`n` = `first_ep_number + first_ep_count`（見 `format.ongoing_episode_number`）。
"""

from __future__ import annotations

from datetime import date
from typing import Iterable, Mapping

from bahaad.newanime.format import (
    air_date,
    date_label,
    display_name,
    ongoing_episode_number,
    short_date_label,
)

_WEEKDAY_NAMES = {1: "週一", 2: "週二", 3: "週三", 4: "週四", 5: "週五", 6: "週六", 7: "週日"}
# 上架後（已轉訂閱／已淡出／下架／結束）就不在新番快訊清單上顯示
_HIDDEN_STAGES = ("removed", "done", "aired", "converted", "catching_up")


def update_label(item: Mapping) -> str:
    if item.get("is_continuing") and item.get("first_air_weekday") and item.get("first_air_time"):
        return f"每週{_WEEKDAY_NAMES[int(item['first_air_weekday'])][1]} {item['first_air_time']}"
    if item.get("is_undetermined") or not item.get("first_air_time"):
        return "待定"
    n = ongoing_episode_number(item)
    if item.get("stage") == "airing" and item.get("ongoing_time"):
        return f"後續第 {n} 集為 {item['ongoing_time']}"
    d = air_date(item)
    # 有首播日期就帶上（使用者 2026-10-05：有些是下下週才開播，只寫時間看不出是哪一天）
    when = f"{short_date_label(d)}{item['first_air_time']}" if d is not None else item["first_air_time"]
    label = f"首播 {when}"
    if item.get("ongoing_time"):
        label += f"；後續第 {n} 集為 {item['ongoing_time']}"
    return label


def _group_slot(item: Mapping) -> tuple[str, int] | None:
    """(分組鍵, 星期)：首播日期已知且還沒進入每週固定時段 → 按**日期**歸組；否則按星期。
    回 None＝待定。"""
    if item.get("is_undetermined") or not item.get("first_air_time") or not item.get("first_air_weekday"):
        return None
    if item.get("is_continuing"):
        wd = int(item["ongoing_weekday"] or item["first_air_weekday"])
        return ("continuing", wd)
    if item.get("stage") == "airing" and item.get("ongoing_weekday"):
        wd = int(item["ongoing_weekday"])
        return (f"w{wd}", wd)
    d = air_date(item)
    if d is not None:
        return (d.isoformat(), d.isoweekday())
    wd = int(item["first_air_weekday"])
    return (f"w{wd}", wd)


def _row(item: Mapping, tracked_sns) -> dict:
    return {
        **dict(item),
        "display_name": display_name(item),
        "update_label": update_label(item),
        "tracked": item["virtual_sn"] in tracked_sns,
    }


def build_listing(items: Iterable[Mapping], tracked_sns: Iterable[int], *, today: date | None = None) -> dict:
    """`today`：給側欄「剛更新」高亮用——只有首播日期在今天前後一天內的項目才輸出
    `data-weekday`/`data-time`（前端用星期＋時間判斷「剛過」，遠在下下週的首播不能被
    誤標成剛更新）。沒帶 `today`（測試）＝一律輸出，維持舊行為。"""
    tracked = set(tracked_sns)
    visible = [it for it in items if it.get("stage") not in _HIDDEN_STAGES]

    slot_groups: dict[str, dict] = {}
    undetermined: list[dict] = []
    for item in visible:
        row = _row(item, tracked)
        slot = _group_slot(item)
        if slot is None:
            undetermined.append(row)
            continue
        key, weekday = slot
        d = air_date(item) if key not in ("continuing",) and not key.startswith("w") else None
        row["highlight_slot"] = (
            today is None or d is None or abs((d - today).days) <= 1
        )
        row["slot_weekday"] = weekday
        if key == "continuing":
            row["slot_time"] = f"{_WEEKDAY_NAMES[weekday]} {row.get('ongoing_time') or row.get('first_air_time')}"
        elif item.get("stage") == "airing" and item.get("ongoing_time"):
            row["slot_time"] = item["ongoing_time"]
        else:
            row["slot_time"] = item.get("first_air_time")
        group = slot_groups.setdefault(key, {"key": weekday, "sort": key, "date": d, "rows": []})
        group["rows"].append(row)

    groups: list[dict] = []
    # 日期組（時間序）在前，之後固定每週時段的星期組（週一…週日），再來是跨季播出
    for sort_key in sorted(k for k in slot_groups if k not in ("continuing",) and not k.startswith("w")):
        g = slot_groups[sort_key]
        rows = sorted(g["rows"], key=lambda r: (r.get("first_air_time") or "99:99", r.get("article_order") or 0))
        groups.append({"key": g["key"], "label": date_label(g["date"]), "rows": rows})
    for sort_key in sorted(k for k in slot_groups if k.startswith("w")):
        g = slot_groups[sort_key]
        rows = sorted(g["rows"], key=lambda r: (r.get("ongoing_time") or r.get("first_air_time") or "99:99", r.get("article_order") or 0))
        groups.append({"key": g["key"], "label": _WEEKDAY_NAMES[g["key"]], "rows": rows})
    if "continuing" in slot_groups:
        rows = sorted(
            slot_groups["continuing"]["rows"],
            key=lambda r: (r["slot_weekday"], r.get("ongoing_time") or r.get("first_air_time") or "99:99", r.get("article_order") or 0),
        )
        groups.append({"key": "continuing", "label": "跨季播出", "rows": rows})
    if undetermined:
        for row in undetermined:
            row["slot_time"] = "待定"
        undetermined.sort(key=lambda r: r.get("article_order") or 0)
        groups.append({"key": "undetermined", "label": "待定時間", "rows": undetermined})

    def _card_rank(r):
        # 首播日期已知的先（日期＋時間）→ 跨季播出（星期＋時間）→ 待定
        if r.get("is_continuing") and r.get("first_air_weekday"):
            return (1, f"{int(r['first_air_weekday'])}", r.get("ongoing_time") or r.get("first_air_time") or "99:99", r.get("article_order") or 0)
        if r.get("first_air_date"):
            return (0, r["first_air_date"], r.get("first_air_time") or "99:99", r.get("article_order") or 0)
        return (2, "", "99:99", r.get("article_order") or 0)

    cards = sorted((_row(it, tracked) for it in visible), key=_card_rank)
    return {"groups": groups, "cards": cards, "count": len(cards)}
