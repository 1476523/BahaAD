"""新番快訊通知的純文字組字串。規格 docs/requirements/new_anime_bulletin.md §8。

- `format_change_line()`：一則異動 → 一行文字（§8d 那張表）。
- `format_full_list()`：整份「完整列表」訊息（Telegram HTML，每個星期／待定組各自
  一個 `<blockquote expandable>`；`完整列表` 類別開時下方多一個「新番異動」組）。

這裡只組字串、不碰 DB／不發送。`format_full_list` 的輸出是安全的 Telegram HTML
（動態的番名已 `html.escape`），`notify/dispatch.py` 走 `_send_prebuilt_html` 直接送、
不再過 `render_message` 的 token 跳脫（不然 `<blockquote>` 會被跳成純文字）。
"""

from __future__ import annotations

import html
from datetime import date
from typing import Iterable, Mapping

_WEEKDAY_NAMES = {1: "週一", 2: "週二", 3: "週三", 4: "週四", 5: "週五", 6: "週六", 7: "週日"}

# 完整列表裡「不再顯示」的 item（已下架／已結束顯示／已上架轉訂閱）
_HIDDEN_STAGES = ("removed", "done", "aired", "converted", "catching_up")


def display_name(item: Mapping) -> str:
    return (item.get("display_name") or item.get("source_name") or "").strip()


def ongoing_episode_number(item: Mapping) -> int:
    """後續第 n 集：n = 首播話數 + 首集集數（首播第 1 話 + 首集 1 集 → 後續第 2 集）。"""
    return int(item.get("first_ep_number") or 1) + int(item.get("first_ep_count") or 1)


def air_date(item: Mapping) -> date | None:
    """`first_air_date`（`YYYY-MM-DD`）→ `date`；沒有或壞掉回 None。"""
    raw = item.get("first_air_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw)[:10])
    except ValueError:
        return None


def date_label(d: date) -> str:
    """`10/05（週日）`——通知／清單標題用，星期由日期本身算，不信另外存的星期欄位。"""
    return f"{d.month:02d}/{d.day:02d}（{_WEEKDAY_NAMES[d.isoweekday()]}）"


def short_date_label(d: date) -> str:
    """`10/05（日）`——卡片單行標籤用。"""
    return f"{d.month:02d}/{d.day:02d}（{_WEEKDAY_NAMES[d.isoweekday()][1]}）"


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def _group_of(item: Mapping) -> tuple[str, int | str | None]:
    """回 ('date', 'YYYY-MM-DD')、('weekday', 1..7) 或 ('undetermined', None)。

    首播日期已知就按**日期**歸組（使用者 2026-10-05：要依更新日期與時間排序，不能只看
    星期——有些是下下週才開播，混在同一個星期組裡看不出來）；首集確認播出後
    （stage='airing'）改按後續星期歸組（之後每週固定時段，沒有特定日期）。"""
    if item.get("is_undetermined") or not item.get("first_air_time") or not item.get("first_air_weekday"):
        return ("undetermined", None)
    if item.get("is_continuing"):
        return ("continuing", int(item.get("ongoing_weekday") or item["first_air_weekday"]))
    if item.get("stage") == "airing" and item.get("ongoing_weekday"):
        return ("weekday", int(item["ongoing_weekday"]))
    d = air_date(item)
    if d is not None:
        return ("date", d.isoformat())
    return ("weekday", int(item["first_air_weekday"]))


def _line_of(item: Mapping) -> str:
    """星期組裡的一行：`<預計時間> #<番名>[（後續第 n 集為 <後續時間>）]`。"""
    name = display_name(item)
    if item.get("stage") == "airing" and item.get("ongoing_time"):
        return f"{item['ongoing_time']} #{name}"
    line = f"{item['first_air_time']} #{name}"
    if item.get("ongoing_time"):
        line += f"（後續第 {ongoing_episode_number(item)} 集為 {item['ongoing_time']}）"
    return line


def _blockquote(header: str, body_lines: list[str]) -> str:
    inner = _esc("\n".join([header, *body_lines]))
    return f"<blockquote expandable>{inner}</blockquote>"


def format_change_line(change: Mapping, name: str, *, air: date | None = None) -> str:
    """一則 `newanime_change_log` → §8d 的一行文字。

    `air`：這部番目前的首播日期（呼叫端用 `air_date(item)` 查好傳進來）——有的話「新的
    更新時間」前面帶上日期（使用者 2026-10-05：通知沒有日期，有些是下下週才開播，只
    寫時間看不出是哪一天）。異動記錄本身只存時間不存日期，所以舊時間那邊不帶日期。"""
    kind = change.get("kind")
    old_t = change.get("old_time")
    new_t = change.get("new_time")
    new_when = f"{short_date_label(air)}{new_t}" if (air is not None and new_t) else new_t
    if kind == "added":
        return (
            f"新增新番 {name} 集數將於 {new_when} 更新"
            if new_t
            else f"新增新番 {name} 集數更新時間待定"
        )
    if kind == "re_added":
        return f"新番 {name} 重新回到新番表"
    if kind == "time_changed":
        if new_t:
            if old_t:
                return f"新番 {name} 將從 {old_t} 變更為 {new_when}"
            return f"新番 {name} 更新時間確定為 {new_when}"
        return f"新番 {name} 時間改為待定"
    if kind == "removed":
        return f"新番 {name} 目前暫時被下架"
    return f"新番 {name} 有異動"


def format_full_list(
    items: Iterable[Mapping],
    changes: Iterable[Mapping],
    names_by_virtual_sn: Mapping[int, str],
    *,
    is_update: bool,
) -> str:
    """整份「完整列表」訊息（Telegram HTML）。

    `items`：這一季所有 item（本函式自己濾掉 removed/done）。
    `changes`：這次要一起帶的異動（`is_update=True` 且非空時才附「新番異動」組）。
    `names_by_virtual_sn`：異動行要用的番名（呼叫端先 resolve 好）。
    """
    item_list = list(items)
    date_groups: dict[str, list[Mapping]] = {}
    weekday_groups: dict[int, list[Mapping]] = {}
    continuing: list[tuple[int, Mapping]] = []
    undetermined: list[Mapping] = []
    for item in item_list:
        if item.get("stage") in _HIDDEN_STAGES:
            continue
        kind, key = _group_of(item)
        if kind == "undetermined":
            undetermined.append(item)
        elif kind == "date":
            date_groups.setdefault(key, []).append(item)
        elif kind == "continuing":
            continuing.append((key, item))
        else:
            weekday_groups.setdefault(key, []).append(item)

    def _sorted(rows):
        return sorted(rows, key=lambda it: (it.get("first_air_time") or "99:99", it.get("article_order") or 0))

    blocks: list[str] = []
    for iso in sorted(date_groups):
        rows = _sorted(date_groups[iso])
        header = f"{date_label(date.fromisoformat(iso))} （共 {len(rows)} 部）"
        blocks.append(_blockquote(header, [_line_of(it) for it in rows]))
    for weekday in sorted(weekday_groups):
        rows = _sorted(weekday_groups[weekday])
        header = f"{_WEEKDAY_NAMES.get(weekday, '週?')} （共 {len(rows)} 部）"
        blocks.append(_blockquote(header, [_line_of(it) for it in rows]))

    if continuing:
        continuing.sort(
            key=lambda pair: (pair[0], pair[1].get("ongoing_time") or pair[1].get("first_air_time") or "99:99",
                              pair[1].get("article_order") or 0)
        )
        lines = [
            f"{_WEEKDAY_NAMES.get(wd, '週?')} {it.get('ongoing_time') or it.get('first_air_time')} #{display_name(it)}"
            for wd, it in continuing
        ]
        blocks.append(_blockquote(f"跨季播出 （共 {len(continuing)} 部）", lines))

    if undetermined:
        undetermined.sort(key=lambda it: it.get("article_order") or 0)
        header = f"待定時間 （共 {len(undetermined)} 部）"
        blocks.append(_blockquote(header, [f"#{display_name(it)}" for it in undetermined]))

    change_list = list(changes)
    if is_update and change_list:
        air_by_sn = {it.get("virtual_sn"): air_date(it) for it in item_list}
        lines = [
            format_change_line(
                ch, names_by_virtual_sn.get(ch.get("virtual_sn"), "（未知）"),
                air=air_by_sn.get(ch.get("virtual_sn")),
            )
            for ch in change_list
        ]
        blocks.append(_blockquote("新番異動", lines))

    return "\n".join(blocks)
