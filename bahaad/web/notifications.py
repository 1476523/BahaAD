"""站內「訂閱通知」——頂列通知鈕的下拉面板。規格見 docs/requirements/completion_detection.md
與 web_redesign_round4.md。

兩種來源：
- **番劇完結**（`kind="completion"`，`store/notifications.py` 的 `NotificationStore`）：
  `action_taken` 記錄完結當下自動做了什麼，面板據此顯示文案與按鈕。
- **官方公告**（`kind="gossip"`，即時從 `GossipStore.get_actionable()` 算，沒有獨立列）：
  需要人工操作的（`gossip_class="manual"`）與系統已自動調整的（`"auto"`）。每筆顯示
  番劇名稱／機動調整結果／原始公告文字，帶操作按鈕（立即操作·忽略公告／更改操作·了解）。

跟 `web/notify.py`（Telegram/Discord 通知中心）不同——那是站外推播的設定與歷史，這裡是
站內的收件匣。
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from flask import Blueprint, current_app, jsonify, request

from bahaad.scheduler.gossip_watch import _EPISODE_PATTERN, _RESUME_PATTERN
from bahaad.subscription_ops import subscribe_sn, unsubscribe_sn

notifications_bp = Blueprint("notifications", __name__)

logger = logging.getLogger(__name__)

# action_taken → (標題後的敘述, 可用的操作按鈕清單)。標題（含《》）由前端組成可點擊
# 的連結（連到 /anime/<sn>），敘述接在標題後面。
_PRESENTATION = {
    "unsubscribe": (
        "可能已經完結，已自動退訂。",
        [("resubscribe", "重新訂閱"), ("dismiss", "知道了")],
    ),
    "mark": (
        "可能已經完結，已標記完結、不再自動檢查更新（訂閱保留）。",
        [("unsubscribe", "退訂"), ("dismiss", "知道了")],
    ),
    "none": (
        "可能已經完結了。",
        [("unsubscribe", "退訂"), ("dismiss", "保留追蹤")],
    ),
}

_GOSSIP_IGNORE = "忽略此公告"


@notifications_bp.route("/notifications")
def list_notifications():
    deps = current_app.config["DEPS"]

    items = _completion_items(deps) + _newanime_items(deps) + _gossip_items(deps)
    items.sort(key=lambda it: it.get("created_at") or "", reverse=True)
    return jsonify({"items": items, "unresolved_count": len(items)})


def _completion_items(deps) -> list[dict]:
    store = deps.notification_store
    if store is None:
        return []
    renames = {}
    if getattr(deps, "schedule_store", None) is not None:
        renames = {sn: e.rename for sn, e in deps.schedule_store.get_entries().items() if e.rename}
    items = []
    for n in store.list_unresolved():
        if n.kind != "completion":
            continue
        detail_text, actions = _PRESENTATION.get(n.action_taken, _PRESENTATION["none"])
        items.append(
            {
                "id": str(n.id),
                "kind": "completion",
                "sn": n.sn,
                # 使用者更名優先（現在改的名字），沒有再退回通知建立當下存的標題
                "anime_title": renames.get(n.sn) or n.anime_title or (f"sn {n.sn}" if n.sn else "這部番劇"),
                "anime_url": f"/anime/{n.sn}" if n.sn else None,
                "detail_text": detail_text,
                "created_at": n.created_at,
                "actions": [{"key": k, "label": label} for k, label in actions],
            }
        )
    return items


def _newanime_items(deps) -> list[dict]:
    """新番快訊：有追蹤的番從新番表消失（§9）——問使用者要不要不再追蹤。
    跟完結通知一樣共用 `NotificationStore`（`kind="newanime_gone"`）。"""
    store = getattr(deps, "notification_store", None)
    if store is None:
        return []
    items = []
    for n in store.list_unresolved():
        if n.kind != "newanime_gone":
            continue
        name = n.anime_title or (f"新番 {n.sn}" if n.sn else "這部新番")
        if n.action_taken == "gone_week":
            detail_text = f"《{name}》已經一週沒出現在新番表上了，可能不會在這一季播出——要繼續追蹤嗎？"
        else:
            detail_text = f"《{name}》從新番表上消失了（超過 24 小時）——要繼續追蹤嗎？"
        items.append(
            {
                "id": f"newanime:{n.id}",
                "kind": "newanime",
                "sn": n.sn,
                "anime_title": name,
                "anime_url": f"/newanime/{n.sn}" if n.sn else None,
                "detail_text": detail_text,
                "created_at": n.created_at,
                "actions": [
                    {"key": "keep_tracking", "label": "繼續追蹤"},
                    {"key": "stop_tracking", "label": "不再追蹤"},
                ],
            }
        )
    return items


def _gossip_items(deps) -> list[dict]:
    store = getattr(deps, "gossip_store", None)
    if store is None:
        return []
    try:
        events = store.get_actionable(date.today().isoformat())
    except Exception:  # noqa: BLE001 — 面板盡力而為，讀不到就當沒有
        logger.exception("讀取官方公告待處理事件失敗")
        return []

    renames = {}
    if getattr(deps, "schedule_store", None) is not None:
        renames = {sn: e.rename for sn, e in deps.schedule_store.get_entries().items() if e.rename}
    cache = getattr(deps, "anime_cache", None)

    def _cache_title(sn):
        if sn is None or cache is None:
            return None
        try:
            return cache.anime_title_for(sn)
        except Exception:  # noqa: BLE001
            return None

    # 「系統已自動調整」＝這個子事件有生效中的臨時排程覆蓋（`gossip_dynamic_schedule`
    # 開著時系統套用建議會建一筆）；沒有覆蓋的就是還要人工操作。
    auto_event_ids = {p["event_id"] for p in store.list_pending("pending") if p["event_id"]}

    items = []
    for event in events:
        if event["disposition_locked"] or event["acknowledged_at"]:
            continue
        if event["disposition"] == _GOSSIP_IGNORE:
            continue
        # 「auto」＝系統已自動套用建議：一般公告看有沒有生效中的臨時排程覆蓋；
        # 每週更新時間異動（reschedule）是永久改 schedule_entries、不建 pending，
        # 改看 disposition_updated_at 有沒有值（apply_disposition 跑過就有）。
        applied = event["id"] in auto_event_ids or (
            event["action"] == "reschedule" and event["disposition_updated_at"]
        )
        if applied:
            gossip_class = "auto"
            actions = [("gossip_change", "更改操作"), ("gossip_ack", "了解")]
        else:
            gossip_class = "manual"
            actions = [("gossip_operate", "立即操作"), ("gossip_ignore", "忽略公告")]

        sn = event["sn"]
        # 使用者更名優先，再來才是公告原文名稱／番劇快取標題（使用者 2026-09-03：
        # 通知要用改過的名字，不是原始標題）
        title = (
            renames.get(sn)
            or event["title_in_gossip"]
            or _cache_title(sn)
            or (f"sn {sn}" if sn else "未比對到作品")
        )
        items.append(
            {
                "id": f"gossip:{event['id']}",
                "kind": "gossip",
                "gossip_class": gossip_class,
                "sn": sn,
                "anime_title": title,
                "anime_url": f"/anime/{sn}" if sn else None,
                "adjustment_text": _gossip_adjustment_text(event),
                "raw_clause": event["raw_clause"],
                "created_at": event["created_at"],
                "actions": [{"key": k, "label": label} for k, label in actions],
            }
        )
    return items


def _gossip_episode_phrase(raw_clause: str) -> str:
    """從公告原文抓集數：0 個→空字串、1 個→「第 N 集」、2+ 個→「第 N 集 與 第 M 集」
    （同時更新最多列兩集）。"""
    eps = _EPISODE_PATTERN.findall(raw_clause or "")
    if not eps:
        return ""
    if len(eps) == 1:
        return f"第 {eps[0]} 集"
    return f"第 {eps[0]} 集 與 第 {eps[1]} 集"


def _gossip_time_text(event: dict) -> str:
    if event["target_time"]:
        return event["target_time"]
    if event["target_time_start"]:
        return f"{event['target_time_start']}~{event['target_time_end']}"
    return ""


def _gossip_resume_suffix(raw_clause: str) -> str:
    m = _RESUME_PATTERN.search(raw_clause or "")
    return f"，預計 {int(m.group(1))}/{int(m.group(2))} 恢復更新" if m else ""


def _gossip_adjustment_text(event: dict) -> str:
    """機動調整結果的說明句子（見 web_redesign_round5.md 項目 2）。依 `status_label`
    產生「第 N 集 將延後至 20:00 播出」這種人話，日期換行放第二行（`\\n` 分隔，前端拆）。"""
    label = event["status_label"]
    raw = event["raw_clause"] or ""
    ep = _gossip_episode_phrase(raw)
    prefix = f"{ep} " if ep else ""
    t = _gossip_time_text(event)

    if label == "暫停更新":
        line = f"{prefix}本週暫停更新{_gossip_resume_suffix(raw)}"
    elif label == "暫時下架":
        line = f"{prefix}本週暫時下架{_gossip_resume_suffix(raw)}"
    elif label == "提前更新":
        line = f"{prefix}將提前至 {t} 播出" if t else f"{prefix}本週提前更新"
    elif label == "延後更新":
        line = f"{prefix}將延後至 {t} 播出" if t else f"{prefix}本週延後更新"
    elif label == "同時更新":
        eps_part = ep or "本週的集數"
        line = f"本週將於 {t} 同時更新 {eps_part}" if t else f"本週將同時更新 {eps_part}"
    elif label == "更新時間變更":
        # target_time 存 "W/HH:MM" 或 "W/同時段"
        wd, _, rest = (event["target_time"] or "").partition("/")
        slot = ""
        if wd.isdigit() and 1 <= int(wd) <= 7:
            slot = f"每週{'一二三四五六日'[int(wd) - 1]}"
            if rest and rest != "同時段":
                slot += f" {rest}"
        if event["disposition_updated_at"] and event["disposition"] in (
            "調整更新時段", "自訂更新時段"
        ):
            line = f"已依公告把更新檢查時段調整為 {slot}".rstrip() if slot else "已依公告調整更新檢查時段"
        else:
            line = f"官方公告：更新時段將調整為 {slot}，請選擇處置".rstrip() if slot else "官方公告：更新時段有變動，請選擇處置"
    else:  # 無法判斷
        line = "系統判斷：無法判斷"
        if event["sn"] is None:
            line += "（尚未比對到追蹤作品）"

    if label in ("提前更新", "延後更新", "同時更新") and event["target_date"]:
        line += f"\n預計 {event['target_date']}"
    return line


@notifications_bp.route("/notifications/<notification_id>/action", methods=["POST"])
def notification_action(notification_id: str):
    deps = current_app.config["DEPS"]
    action = (request.form.get("action") or "").strip()

    if notification_id.startswith("gossip:"):
        return _gossip_action(deps, notification_id[len("gossip:"):], action)

    if notification_id.startswith("newanime:"):
        return _newanime_action(deps, notification_id[len("newanime:"):], action)

    store = deps.notification_store
    if store is None:
        return jsonify({"ok": False, "error": "no_store"}), 404

    try:
        notif = store.get(int(notification_id))
    except ValueError:
        return jsonify({"ok": False, "error": "not_found"}), 404
    if notif is None:
        return jsonify({"ok": False, "error": "not_found"}), 404

    if action == "unsubscribe" and notif.sn is not None:
        if unsubscribe_sn(deps.schedule_store, notif.sn)["removed"]:
            _cleanup_orphans(deps, notif.sn)
    elif action == "resubscribe" and notif.sn is not None:
        from bahaad.web.browse import _weekly_schedule_time

        try:
            found = _weekly_schedule_time(deps, notif.sn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("重新訂閱查週期表失敗（sn=%s）：%s", notif.sn, exc)
            return jsonify({"ok": False, "error": "schedule_lookup_failed"})
        if found is None:
            return jsonify({"ok": False, "error": "no_schedule_time"})
        subscribe_sn(deps.schedule_store, notif.sn, *found)
        if deps.completion_watch_store is not None:
            deps.completion_watch_store.delete(notif.sn)  # 重新監視
    elif action not in ("dismiss", "keep"):
        return jsonify({"ok": False, "error": "unknown_action"}), 400

    store.resolve(int(notification_id))
    return jsonify({"ok": True})


def _newanime_action(deps, raw_id: str, action: str):
    """§9 消失提示的按鈕：繼續追蹤（一週內不再提示）／不再追蹤（移出追蹤清單）。"""
    store = getattr(deps, "notification_store", None)
    nastore = getattr(deps, "newanime_cache", None)
    try:
        nid = int(raw_id)
    except ValueError:
        return jsonify({"ok": False, "error": "not_found"}), 404
    notif = store.get(nid) if store is not None else None
    if notif is None:
        return jsonify({"ok": False, "error": "not_found"}), 404

    if action == "stop_tracking":
        if nastore is not None and notif.sn is not None:
            nastore.untrack(notif.sn)
    elif action == "keep_tracking":
        if nastore is not None and notif.sn is not None:
            nastore.set_keep_after_disappear(
                notif.sn,
                (datetime.now() + timedelta(days=7)).isoformat(timespec="seconds"),
            )
    elif action != "dismiss":
        return jsonify({"ok": False, "error": "unknown_action"}), 400

    store.resolve(nid)
    return jsonify({"ok": True})


def _gossip_action(deps, raw_event_id: str, action: str):
    """頂列鈴鐺的公告通知按鈕。「立即操作／更改操作」由前端開處置視窗，不打這裡。"""
    try:
        event_id = int(raw_event_id)
    except ValueError:
        return jsonify({"ok": False, "error": "not_found"}), 404
    store = getattr(deps, "gossip_store", None)
    watcher = getattr(deps, "gossip_watcher", None)
    if store is None or store.get_event(event_id) is None:
        return jsonify({"ok": False, "error": "not_found"}), 404

    if action == "gossip_ack":
        store.acknowledge_event(event_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        return jsonify({"ok": True})
    if action == "gossip_ignore":
        if watcher is None:
            return jsonify({"ok": False, "error": "no_store"}), 404
        try:
            watcher.apply_disposition(event_id, _GOSSIP_IGNORE, locked=True)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)})
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "unknown_action"}), 400


def _cleanup_orphans(deps, sn: int) -> None:
    if deps.gossip_store is None:
        return
    from bahaad.web.db_cleanup import delete_records

    try:
        delete_records(
            sn,
            gossip_store=deps.gossip_store,
            manual_task_store=deps.manual_task_store,
            skipped_episode_store=deps.skipped_episode_store,
        )
    except Exception:  # noqa: BLE001
        logger.exception("通知面板退訂後清孤兒紀錄失敗（sn=%s）", sn)
