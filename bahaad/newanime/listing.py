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

from typing import Iterable, Mapping

from bahaad.newanime.format import display_name, ongoing_episode_number

_WEEKDAY_NAMES = {1: "週一", 2: "週二", 3: "週三", 4: "週四", 5: "週五", 6: "週六", 7: "週日"}
# 上架後（已轉訂閱／已淡出／下架／結束）就不在新番快訊清單上顯示
_HIDDEN_STAGES = ("removed", "done", "aired", "converted", "catching_up")


def update_label(item: Mapping) -> str:
    if item.get("is_undetermined") or not item.get("first_air_time"):
        return "待定"
    n = ongoing_episode_number(item)
    if item.get("stage") == "airing" and item.get("ongoing_time"):
        return f"後續第 {n} 集為 {item['ongoing_time']}"
    label = f"首播 {item['first_air_time']}"
    if item.get("ongoing_time"):
        label += f"；後續第 {n} 集為 {item['ongoing_time']}"
    return label


def _group_weekday(item: Mapping) -> int | None:
    if item.get("is_undetermined") or not item.get("first_air_time") or not item.get("first_air_weekday"):
        return None
    if item.get("stage") == "airing" and item.get("ongoing_weekday"):
        return int(item["ongoing_weekday"])
    return int(item["first_air_weekday"])


def _row(item: Mapping, tracked_sns) -> dict:
    return {
        **dict(item),
        "display_name": display_name(item),
        "update_label": update_label(item),
        "tracked": item["virtual_sn"] in tracked_sns,
    }


def build_listing(items: Iterable[Mapping], tracked_sns: Iterable[int]) -> dict:
    tracked = set(tracked_sns)
    visible = [it for it in items if it.get("stage") not in _HIDDEN_STAGES]

    weekday_groups: dict[int, list[dict]] = {}
    undetermined: list[dict] = []
    for item in visible:
        row = _row(item, tracked)
        weekday = _group_weekday(item)
        if weekday is None:
            undetermined.append(row)
        else:
            weekday_groups.setdefault(weekday, []).append(row)

    groups: list[dict] = []
    for weekday in sorted(weekday_groups):
        rows = sorted(
            weekday_groups[weekday],
            key=lambda r: (r.get("first_air_time") or "99:99", r.get("article_order") or 0),
        )
        groups.append({"key": weekday, "label": _WEEKDAY_NAMES[weekday], "rows": rows})
    if undetermined:
        undetermined.sort(key=lambda r: r.get("article_order") or 0)
        groups.append({"key": "undetermined", "label": "待定時間", "rows": undetermined})

    cards = sorted(
        (_row(it, tracked) for it in visible),
        key=lambda r: (
            r.get("first_air_date") or "9999-99-99",
            r.get("first_air_time") or "99:99",
            r.get("article_order") or 0,
        ),
    )
    return {"groups": groups, "cards": cards, "count": len(cards)}
