"""訂閱／退訂的共用邏輯——`web/browse.py` 的鈴鐺路由跟 `scheduler/completion_watch.py`
的自動退訂共用同一套「怎麼寫排程清單」。

訂閱＝在 `schedule_entries` 裡建一筆帶 `schedule_weekday` 的項目（星期＋時分取自週期表）；
退訂＝移除排程時段。退訂有個細節：使用者若在排程清單頁另外設過分類／模式／改名，只清
掉排程時段、讓它退化成一般全域輪詢項目，不整條刪——不然點一下鈴鐺會意外清掉使用者手動
設定的東西。純粹由鈴鐺建立、沒有其他設定的項目才整條移除（呼叫端據此決定要不要清孤兒
資料庫紀錄）。

孤兒資料庫紀錄的清理（`gossip_events`／`manual_tasks`／`skipped_episodes`）**不在這裡**——
交給呼叫端（`web/db_cleanup.delete_records`），避免這個底層模組相依到那幾個 store。
"""

from __future__ import annotations

from bahaad.store.schedule_list import (
    ScheduleListStore,
    _tag_before,
    _upsert_entry,
    parse_text,
    render_text,
)


def subscribe_sn(
    schedule_store: ScheduleListStore,
    sn: int,
    weekday: int,
    hour: int,
    minute: int,
    *,
    rename: str | None = None,
) -> None:
    """把 sn 寫成一筆帶排程時段的項目。已存在的項目沿用它原本的分類／模式／改名。
    `rename` 有帶就用它當下載資料夾名（新番快訊轉訂閱時把使用者改的顯示名帶進來）；
    沒帶則沿用既有項目原本的改名。"""
    lines = parse_text(schedule_store.get_raw_text())
    idx = next(
        (i for i, line in enumerate(lines) if line.line_type == "entry" and line.sn == sn), None
    )
    tag = _tag_before(lines, idx) if idx is not None else None
    mode = lines[idx].mode if idx is not None else None
    existing_rename = lines[idx].rename if idx is not None else None
    lines = _upsert_entry(lines, sn, tag, mode, rename or existing_rename, weekday, hour, minute)
    schedule_store.replace_from_text(render_text(lines))


def unsubscribe_sn(schedule_store: ScheduleListStore, sn: int) -> dict:
    """移除訂閱。回 `{"removed": bool}`——`removed=True` 代表整條刪掉了（呼叫端該連帶
    清孤兒紀錄）；`False` 代表只退化成全域輪詢項目（還在追蹤、不是孤兒），或本來就不在。"""
    lines = parse_text(schedule_store.get_raw_text())
    idx = next(
        (i for i, line in enumerate(lines) if line.line_type == "entry" and line.sn == sn), None
    )
    if idx is None:
        return {"removed": False}

    line = lines[idx]
    tag = _tag_before(lines, idx)
    if tag or line.mode or line.rename:
        lines = _upsert_entry(lines, sn, tag, line.mode, line.rename, None, None, None)
        removed = False
    else:
        lines = [entry_line for i, entry_line in enumerate(lines) if i != idx]
        removed = True
    schedule_store.replace_from_text(render_text(lines))
    return {"removed": removed}
