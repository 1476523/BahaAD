"""排程清單管理。規格見 docs/requirements/web_schedule.md。

排程清單只以「訂閱列表頁（`/browse/subscriptions`）的右側浮動面板」這一種形態存在
（round 7 第 4 項），面板內容是 `schedule_embed.html` 的 iframe（`/schedule?embed=1`）。
直接開 `/schedule`（沒有 `embed`）會 302 導回訂閱列表頁——沒有獨立的整頁版了
（使用者 2026-08-31 第 8 項：「排程清單是訂閱列表的右側欄位」）。

只有結構化列表這一種編輯方式（改單一項目的分類／資料夾名稱／自訂檢查時段）。加入
追蹤、取消追蹤一律走卡片上的鈴鐺（`web/browse.py` 的 subscribe/unsubscribe）——面板
**不再有**「刪除」鈕、也沒有「文字模式」，避免使用者搞不清「刪除」跟「取消訂閱」
差在哪（使用者 2026-08-31 第 8 項）。

寫入路徑：`parse_text()` 解析 → 在記憶體裡改一行 → `render_text()` 組回文字 →
`replace_from_text()` 整批寫回——`ScheduleListStore` 只有 `replace_from_text()` 一個
寫入方法，沒有細粒度的「改單一欄位」介面，這裡不繞過去直接碰資料庫，維持
store/schedule_list.py 的權威地位。

分類（`@tag` 行）在文字/資料表層級不是 entry 自己的欄位，是「這一行前面最近一個
`@tag` 行是誰」這種位置關係決定的（見 `get_entries()` 的 `current_tag` 追蹤邏輯）。
所以「改變一個項目的分類」實際上是把這一行搬到對應 `@tag` 群組底下，不是單純改一個
欄位值——`_move_entry_into_tag_group()` 處理這件事。
"""

from __future__ import annotations

import logging
from datetime import date

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

# _tag_before / _move_entry_into_tag_group / _upsert_entry 2026-08-27 搬到
# store/schedule_list.py（scheduler 也要用），這裡 re-export 讓既有 import 路徑不變。
from bahaad.scheduler.seasons import (
    DEFAULT_AUTO_SEASON_FOLDER,
    DEFAULT_AUTO_SEASON_YEAR,
    season_folder,
)
from bahaad.store.schedule_list import (  # noqa: F401
    _Line,
    _move_entry_into_tag_group,
    _tag_before,
    _upsert_entry,
    parse_text,
    render_text,
    schedule_sort_key,
)

schedule_bp = Blueprint("schedule", __name__)

logger = logging.getLogger(__name__)

_WEEKDAY_LABELS = [
    (1, "一"), (2, "二"), (3, "三"), (4, "四"), (5, "五"), (6, "六"), (7, "日"),
]


def _parse_schedule_fields(form) -> tuple[int | None, int | None, int | None]:
    weekday_raw = (form.get("schedule_weekday") or "").strip()
    hour_raw = (form.get("schedule_hour") or "").strip()
    minute_raw = (form.get("schedule_minute") or "").strip()
    if not weekday_raw and not hour_raw and not minute_raw:
        return None, None, None
    if not (weekday_raw and hour_raw and minute_raw):
        raise ValueError("自訂時段要嘛星期/時/分都填，要嘛都留空")
    return int(weekday_raw), int(hour_raw), int(minute_raw)


@schedule_bp.route("/schedule")
def index():
    """排程清單——唯讀列表，點「編輯」才展開該項目的編輯表單（round3 階段 6-1）。
    加入／取消追蹤靠卡片鈴鐺，這頁只調整分類／資料夾名稱／自訂時段。

    只有訂閱列表頁右側面板 iframe 會帶 `?embed=1` 打進來；直接開 `/schedule`
    導回訂閱列表頁（排程清單沒有獨立整頁版，使用者 2026-08-31 第 8 項）。"""
    if not request.args.get("embed"):
        return redirect(url_for("browse.subscriptions"))

    deps = current_app.config["DEPS"]
    entries = sorted(deps.schedule_store.get_entries().values(), key=schedule_sort_key)

    # 標題：快取有就顯示（不打網路），沒有就顯示 sn。順便算「自動季別」提示——只在
    # 「分類」欄空著、且設定開著時給，用快取的首播日期（沒快取就沒提示），不寫進排程檔。
    titles: dict[int, str] = {}
    auto_seasons: dict[int, str] = {}
    season_on = bool(deps.settings.get("auto_season_folder", DEFAULT_AUTO_SEASON_FOLDER))
    year_on = bool(deps.settings.get("auto_season_year_folder", DEFAULT_AUTO_SEASON_YEAR))
    cache = getattr(deps, "anime_cache", None)
    if cache is not None:
        for entry in entries:
            # 訂閱的可能是某一集的 sn、該集的詳細頁不一定有快取——先用 episode_group
            # 記下的番劇標題（使用者 2026-08-31 第 4/18 項：別顯示 sn 號碼）
            try:
                group_title = cache.anime_title_for(entry.sn)
            except Exception:  # noqa: BLE001
                group_title = None
            if group_title:
                titles[entry.sn] = group_title
            try:
                hit = cache.get_detail(entry.sn)
            except Exception:  # noqa: BLE001
                hit = None
            if hit is None:
                continue
            titles[entry.sn] = hit[0].title
            if season_on and not entry.tag:
                ep_total = sum(len(c.episodes) for c in hit[0].episode_categories) or None
                label = season_folder(hit[0].air_date, total_episode=ep_total, include_year=year_on)
                if label:
                    auto_seasons[entry.sn] = label

    return render_template(
        "schedule_embed.html",
        entries=entries,
        titles=titles,
        auto_seasons=auto_seasons,
        gossip_notes=_gossip_notes(deps, {e.sn for e in entries}),
        weekday_labels=_WEEKDAY_LABELS,
        weekday_names=dict(_WEEKDAY_LABELS),
        embed=True,
    )


def _gossip_notes(deps, entry_sns: set[int]) -> dict[int, str]:
    """{sn: 「將延後至 01:25 播出（預計 2026-09-03）」} —— 公告造成的臨時時間異動，
    顯示在排程清單該番劇下方（使用者 2026-09-03）。`reschedule`（每週時間永久變更）
    不算臨時異動、排除；一部番只取一筆。"""
    store = getattr(deps, "gossip_store", None)
    if store is None:
        return {}
    from bahaad.web.notifications import _gossip_adjustment_text

    notes: dict[int, str] = {}
    try:
        events = store.get_actionable(date.today().isoformat())
    except Exception:  # noqa: BLE001 - 面板盡力而為，讀不到就當沒有
        logger.debug("排程清單讀取公告臨時異動失敗", exc_info=True)
        return {}
    for ev in events:
        sn = ev.get("sn")
        if sn not in entry_sns or sn in notes or ev.get("action") == "reschedule":
            continue
        parts = _gossip_adjustment_text(ev).split("\n")
        notes[sn] = parts[0] + (f"（{parts[1]}）" if len(parts) > 1 else "")
    return notes


def _back_to_index():
    """儲存後回哪：一律回訂閱列表頁右側面板的無殼版（`embed` 欄；沒帶就導回訂閱列表頁）。"""
    if request.form.get("embed"):
        return redirect(url_for("schedule.index", embed=1))
    return redirect(url_for("browse.subscriptions"))


@schedule_bp.route("/schedule/save", methods=["POST"])
def save_entry():
    """編輯既有排程項目（分類／下載資料夾名稱／自訂時段）。「模式」欄位已廢除
    （round3 階段 6-3，一律抓最新一集）——這裡不讀 `mode`，但沿用既有那一行原本的
    `mode` 值（相容既有排程檔裡手動／匯入設過的舊資料，比照 `web/browse.py` 的鈴鐺
    訂閱／改名路徑）。"""
    deps = current_app.config["DEPS"]

    try:
        sn = int(request.form.get("sn", ""))
    except (TypeError, ValueError):
        flash("番劇 sn 必須是數字")
        return _back_to_index()

    tag = (request.form.get("tag") or "").strip() or None
    rename = (request.form.get("rename") or "").strip() or None

    try:
        schedule_weekday, schedule_hour, schedule_minute = _parse_schedule_fields(request.form)
    except ValueError as exc:
        flash(str(exc))
        return _back_to_index()

    lines = parse_text(deps.schedule_store.get_raw_text())
    existing = next((ln for ln in lines if ln.line_type == "entry" and ln.sn == sn), None)
    mode = existing.mode if existing is not None else None
    lines = _upsert_entry(lines, sn, tag, mode, rename, schedule_weekday, schedule_hour, schedule_minute)
    deps.schedule_store.replace_from_text(render_text(lines))
    flash("已儲存")
    return _back_to_index()

