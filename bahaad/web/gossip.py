"""監視公告的網頁介面：歷史公告／操作處置／機動調整三個頁面，以及設定頁的三個開關。

規格見 docs/requirements/scheduler_gossip_watch.md「網頁 UI」一節，建議實作階段第 6 階段。

比照排程清單／下載狀態（舊版）／手動下載的既有慣例（見 `notify.md` 第 3 階段實作後
補充）：這三頁**不放進主側欄導覽**，從 `home.html` 底部連結進來、頁面之間互相連結。

「確認處置」跟 `GossipWatcher` 背景執行緒自動套用建議走**同一個 `apply_disposition()`**
（`_apply_lock` 序列化「清舊覆蓋 + 寫新覆蓋」，防兩邊並發疊出矛盾的臨時排程）——所以
這裡需要 `deps.gossip_watcher`，不是只有 `deps.gossip_store`。
"""

from __future__ import annotations

from datetime import date

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for

from bahaad.store.gossip import DISPOSITION_OPTIONS, RESCHEDULE_DISPOSITIONS
from bahaad.web.responses import form_result

gossip_bp = Blueprint("gossip", __name__)

_HISTORY_PAGE_SIZE = 50

# 生效中覆蓋的 type 顯示成中文
_PENDING_TYPE_LABELS = {
    "skip_check": "本週暫停／下架",
    "override_check_time": "改為指定時刻檢查",
    "override_check_window": "改為時間範圍內檢查",
}


def _deps():
    return current_app.config["DEPS"]


def _gossip_display_name(sn, title_in_gossip, tracked, cache) -> str:
    """公告相關頁面「作品」欄一律顯示番劇名、不顯示 sn（使用者 2026-09-03）：
    使用者更名 → 公告原文名稱 → 番劇快取標題 → 真的都查不到才退回 sn。"""
    if sn is not None:
        entry = tracked.get(sn)
        if entry is not None and entry.rename:
            return entry.rename
    if title_in_gossip:
        return title_in_gossip
    if sn is not None and cache is not None:
        try:
            title = cache.anime_title_for(sn)
        except Exception:  # noqa: BLE001
            title = None
        if title:
            return title
    return f"sn {sn}" if sn is not None else "未比對到作品"


@gossip_bp.route("/gossip/history")
def history_index():
    deps = _deps()
    page = max(1, request.args.get("page", 1, type=int))
    rows, total = deps.gossip_store.list_events(
        limit=_HISTORY_PAGE_SIZE, offset=(page - 1) * _HISTORY_PAGE_SIZE
    )
    total_pages = max(1, (total + _HISTORY_PAGE_SIZE - 1) // _HISTORY_PAGE_SIZE)
    tracked = deps.schedule_store.get_entries()
    cache = getattr(deps, "anime_cache", None)
    # 「目前仍掛在動畫瘋首頁」的公告用金框框住（比照週期表）：`last_seen_at` 等於最後
    # 一輪掃描時間就是還在生效；已從站上撤下的 `last_seen_at` 會停在較舊的時間。
    last_scan = deps.settings.get("_gossip_last_scan_at")
    for row in rows:
        row["is_current"] = bool(last_scan) and row.get("last_seen_at") == last_scan
        row["display_name"] = _gossip_display_name(
            row.get("sn"), row.get("title_in_gossip"), tracked, cache
        )
    return render_template(
        "gossip_history.html",
        rows=rows,
        page=page,
        total=total,
        total_pages=total_pages,
    )


def _actionable_context() -> dict:
    """操作處置頁／頂列鈴鐺快速處置視窗共用的樣板變數。"""
    deps = _deps()
    tracked = deps.schedule_store.get_entries()
    cache = getattr(deps, "anime_cache", None)

    def _label(sn: int, entry) -> str:
        if entry.rename:
            return entry.rename
        if cache is not None:
            try:
                title = cache.anime_title_for(sn)
            except Exception:  # noqa: BLE001
                title = None
            if title:
                return title
        return f"sn {sn}"

    tracked_options = sorted(
        ((sn, _label(sn, entry)) for sn, entry in tracked.items()),
        key=lambda item: item[0],
    )
    return dict(
        disposition_options=DISPOSITION_OPTIONS,
        reschedule_dispositions=RESCHEDULE_DISPOSITIONS,
        weekday_choices=[(i, f"每週{'一二三四五六日'[i - 1]}") for i in range(1, 8)],
        tracked_options=tracked_options,
        # 「對應追蹤項目」一律顯示番劇名（使用者更名 → 快取標題 → sn），不顯示 sn
        # （C1 見 web_redesign_round3.md；2026-09-03 擴大成「所有 sn 顯示都換成名稱」）
        tracked_titles=dict(tracked_options),
    )


@gossip_bp.route("/gossip/actionable")
def actionable_index():
    deps = _deps()
    events = deps.gossip_store.get_actionable(date.today().isoformat())
    return render_template("gossip_actionable.html", events=events, **_actionable_context())


@gossip_bp.route("/gossip/actionable/<int:event_id>/form")
def disposition_form(event_id: int):
    """單一子事件的處置表單片段——頂列鈴鐺「立即操作／更改操作」用 fetch 拿這段塞進
    視窗，不用整頁跳到操作處置頁。跟操作處置頁走同一個 `_gossip_disposition_form.html`
    partial 與同一組 POST 端點。"""
    deps = _deps()
    event = deps.gossip_store.get_event(event_id)
    if event is None:
        abort(404)
    return render_template(
        "_gossip_disposition_form.html", event=event, in_modal=True, **_actionable_context()
    )


@gossip_bp.route("/gossip/actionable/<int:event_id>/disposition", methods=["POST"])
def apply_disposition(event_id: int):
    deps = _deps()
    if deps.gossip_store.get_event(event_id) is None:
        abort(404)
    disposition = request.form.get("disposition", "")
    kwargs = dict(
        custom_check_date=(request.form.get("custom_check_date") or "").strip() or None,
        custom_check_time=(request.form.get("custom_check_time") or "").strip() or None,
        custom_check_time_start=(request.form.get("custom_check_time_start") or "").strip() or None,
        custom_check_time_end=(request.form.get("custom_check_time_end") or "").strip() or None,
    )
    raw_weekday = (request.form.get("custom_weekday") or "").strip()
    if raw_weekday:
        try:
            kwargs["custom_weekday"] = int(raw_weekday)
        except ValueError:
            flash("星期需要填數字")
            return redirect(url_for("gossip.actionable_index"))
    raw_count = (request.form.get("custom_episode_count") or "").strip()
    if raw_count:
        try:
            kwargs["custom_episode_count"] = int(raw_count)
        except ValueError:
            flash("集數需要填數字")
            return redirect(url_for("gossip.actionable_index"))

    try:
        deps.gossip_watcher.apply_disposition(event_id, disposition, locked=True, **kwargs)
    except ValueError as exc:
        flash(str(exc))
        return redirect(url_for("gossip.actionable_index"))
    flash("已套用處置")
    return redirect(url_for("gossip.actionable_index"))


@gossip_bp.route("/gossip/actionable/<int:event_id>/assign-sn", methods=["POST"])
def assign_sn(event_id: int):
    deps = _deps()
    event = deps.gossip_store.get_event(event_id)
    if event is None:
        abort(404)
    if event["sn"] is not None:
        flash("這則子事件已經比對到追蹤中的作品，不需要手動指定")
        return redirect(url_for("gossip.actionable_index"))

    sn = request.form.get("sn", type=int)
    if sn is None or sn not in deps.schedule_store.get_entries():
        flash("指定的排程項目不存在，可能已從追蹤清單移除")
        return redirect(url_for("gossip.actionable_index"))

    deps.gossip_store.set_manual_sn(event_id, sn)
    flash("已指定追蹤項目，接著可以選擇要套用的處置方式")
    return redirect(url_for("gossip.actionable_index"))


@gossip_bp.route("/gossip/pending")
def pending_index():
    deps = _deps()
    rows = deps.gossip_store.list_pending("pending")
    return render_template(
        "gossip_pending.html",
        rows=rows,
        type_labels=_PENDING_TYPE_LABELS,
    )


@gossip_bp.route("/gossip/settings")
def settings_index():
    """官方公告頁第四個分頁「設定」——兩個開關＋保留天數，round3 階段 5-3 從設定頁搬過來。"""
    from bahaad.scheduler.gossip_watch import _DEFAULT_CHECK_INTERVAL_MINUTES, _DEFAULT_RETENTION_DAYS

    deps = _deps()
    return render_template(
        "gossip_settings.html",
        gossip_monitor=deps.settings.get("gossip_monitor", True),
        gossip_dynamic_schedule=deps.settings.get("gossip_dynamic_schedule", True),
        gossip_retention_days=deps.settings.get("gossip_retention_days", _DEFAULT_RETENTION_DAYS),
        gossip_check_interval_minutes=int(
            deps.settings.get("gossip_check_interval_minutes", _DEFAULT_CHECK_INTERVAL_MINUTES)
        ),
    )


@gossip_bp.route("/gossip/settings", methods=["POST"])
def save_settings():
    """「設定」分頁的表單：兩個開關 + 保留天數。checkbox 沒勾就是不在 form 裡，
    比照 `web/notify.py` 的 `save_categories()`／`web/settings.py` 的
    `set_diagnostics_enabled()` 做法。"""
    deps = _deps()
    partial = {
        "gossip_monitor": request.form.get("gossip_monitor") == "on",
        "gossip_dynamic_schedule": request.form.get("gossip_dynamic_schedule") == "on",
    }
    raw_days = (request.form.get("gossip_retention_days") or "").strip()
    if raw_days:
        try:
            days = int(raw_days)
            if days < 1:
                raise ValueError
            partial["gossip_retention_days"] = days
        except ValueError:
            return form_result("保留天數需要填 1 以上的整數", endpoint="gossip.settings_index", ok=False)
    raw_interval = (request.form.get("gossip_check_interval_minutes") or "").strip()
    if raw_interval:
        try:
            minutes = int(raw_interval)
            if minutes < 1:  # 使用者 2026-09-05：比舊版慢，開放到最少 1 分鐘
                raise ValueError
            partial["gossip_check_interval_minutes"] = minutes
        except ValueError:
            return form_result(
                "檢查週期需要填 1 以上的整數（分鐘）", endpoint="gossip.settings_index", ok=False
            )
    deps.settings.update(partial)
    return form_result("已更新官方公告設定", endpoint="gossip.settings_index")


@gossip_bp.route("/gossip/recheck", methods=["POST"])
def recheck():
    """「重新檢查」：立刻重抓公告 + 重跑分類與機動調整（`GossipWatcher.force_recheck()`），
    無視「這則公告先前處理過」的去重。手動處置過（`disposition_locked=1`）的項目不覆蓋。"""
    deps = _deps()
    watcher = getattr(deps, "gossip_watcher", None)
    if watcher is None:
        return form_result("目前無法重新檢查（公告監視器未啟用）", endpoint="gossip.settings_index", ok=False)
    try:
        watcher.force_recheck()
    except Exception:  # noqa: BLE001
        current_app.logger.exception("手動重新檢查公告失敗")
        return form_result("重新檢查時發生錯誤，請看日誌", endpoint="gossip.settings_index", ok=False)
    return form_result("已重新檢查官方公告", endpoint="gossip.settings_index")
