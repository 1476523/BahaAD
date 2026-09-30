"""「新番快訊」的網頁介面。規格 docs/requirements/new_anime_bulletin.md §6。

- `GET /newanime`：列表頁（卡片格 + 右側「星期／待定」面板）。
- `GET /newanime/<virtual_sn>`：詳細頁（§6c）。
- `POST /newanime/<virtual_sn>/track` / `/untrack`：加入／移除追蹤（新番版的訂閱鈴鐺）。
- `POST /newanime/<virtual_sn>/rename`：改顯示名。

時效（§6a／§9）：`newanime_bulletin.pending_note_gone_at` + 6 天的 23:59 之後，右側入口
不顯示、`/newanime*` 一律導回首頁——`before_request` 統一擋。**背景追蹤（掃描／通知）
不受這個影響**，那是 `NewAnimeWatcher` 的事。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from flask import (
    Blueprint,
    current_app,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from bahaad.newanime.detail import build_detail
from bahaad.newanime.listing import build_listing
from bahaad.newanime.visibility import first_visible
from bahaad.web.responses import form_result, wants_json

logger = logging.getLogger(__name__)

newanime_bp = Blueprint("newanime", __name__)

# 跟 web/browse.py 的番劇更名一致：Windows 檔名不允許的符號 + 控制字元擋掉
_ILLEGAL_RENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
_MAX_RENAME_LEN = 120


def _deps():
    return current_app.config["DEPS"]


def _public_readonly() -> bool:
    return bool(getattr(g, "public_readonly", False))


def _visible_bulletin(deps) -> dict | None:
    store = getattr(deps, "newanime_cache", None)
    if store is None:
        return None
    bulletin = first_visible(store.list_bulletins(), datetime.now())
    return dict(bulletin) if bulletin is not None else None


@newanime_bp.before_request
def _guard_visibility():
    deps = _deps()
    bulletin = _visible_bulletin(deps)
    if bulletin is None:
        return redirect(url_for("index"))
    # 公開模式：擁有者一部新番都沒追蹤 → 這個入口對訪客整個不存在，導回公開首頁
    if _public_readonly() and not _has_tracked_in_bulletin(deps, bulletin):
        return redirect(url_for("browse.subscriptions"))
    return None


def _has_tracked_in_bulletin(deps, bulletin) -> bool:
    store = deps.newanime_cache
    tracked = store.list_tracked()
    return any(it["virtual_sn"] in tracked for it in store.list_items(bulletin["season_key"]))


def _listing_items(deps, bulletin, tracked):
    """列表頁要餵給 `build_listing` 的 `newanime_item` row。公開模式（未登入唯讀）下
    只留擁有者「已追蹤」的那幾部——訪客不需要看到擁有者沒在追的整季新番清單
    （使用者 2026-09-08）。"""
    items = deps.newanime_cache.list_items(bulletin["season_key"])
    if _public_readonly():
        items = [it for it in items if it["virtual_sn"] in tracked]
    return items


@newanime_bp.route("/newanime")
def index():
    deps = _deps()
    bulletin = _visible_bulletin(deps)
    store = deps.newanime_cache
    tracked = store.list_tracked()
    listing = build_listing(_listing_items(deps, bulletin, tracked), tracked)
    return render_template("newanime_list.html", bulletin=bulletin, listing=listing)


def _item_in_bulletin(deps, virtual_sn: int):
    """回 (item, bulletin)；查不到／不是這一季的 → (None, bulletin)。"""
    store = deps.newanime_cache
    bulletin = _visible_bulletin(deps)
    item = store.get_item(virtual_sn)
    if item is None or bulletin is None or item["season_key"] != bulletin["season_key"]:
        return None, bulletin
    return item, bulletin


@newanime_bp.route("/newanime/<int:virtual_sn>")
def detail(virtual_sn: int):
    deps = _deps()
    item, bulletin = _item_in_bulletin(deps, virtual_sn)
    if item is None:
        return redirect(url_for("newanime.index"))
    tracked = deps.newanime_cache.is_tracked(virtual_sn)
    # 公開模式：只有擁有者追蹤中的新番才給訪客看詳細頁（跟列表頁一致）
    if _public_readonly() and not tracked:
        return redirect(url_for("newanime.index"))
    view = build_detail(item, tracked=tracked)
    youranimes = _youranimes_right_column(deps, item["source_name"])
    return render_template(
        "newanime_detail.html", item=view, youranimes=youranimes, bulletin=bulletin
    )


def _youranimes_right_column(deps, source_name: str):
    """youranimes 季度頁的「製作／配音／音樂」——沿用番劇詳細頁的 `YourAnimesPanel`
    ＋ `match_record`。`youranimes_sync` 只抓「當季 + 前 2 季」，新番快訊多半是**下一季**、
    這時 `youranimes_cache` 還沒那一季 → 回 None，右欄先不出現（等季度變「當季」後就有了）。"""
    store = getattr(deps, "youranimes_cache", None)
    if store is None:
        return None
    try:
        from bahaad.web.youranimes_view import YourAnimesPanel
        from bahaad.youranimes.match import match_record

        rec = match_record([source_name], store.find_by_base)
    except Exception:  # noqa: BLE001 - 補充資料失敗絕不能讓詳細頁掛掉
        logger.debug("新番快訊：youranimes 右欄配對失敗（%s）", source_name, exc_info=True)
        return None
    if rec is None:
        return None
    return YourAnimesPanel(
        anime_id=rec.anime_id, synopsis=rec.synopsis,
        staff=rec.staff, cast=rec.cast, music=rec.music,
    )


@newanime_bp.route("/newanime/<int:virtual_sn>/rename", methods=["POST"])
def rename(virtual_sn: int):
    """更名——跟一般番劇詳細頁的更名圖示（`rename.js`）走同一套：POST JSON `{name}`、
    回 `{ok}` / `{error}`。改的是 `newanime_item.display_name`（追蹤的番正式上架轉訂閱後
    沿用當下載資料夾名）。前端也檢查過，但不信任前端。"""
    deps = _deps()
    item, _bulletin = _item_in_bulletin(deps, virtual_sn)
    if item is None:
        return jsonify({"ok": False, "error": "找不到這部新番"}), 404
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name") or request.form.get("display_name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "名稱不能是空白"}), 400
    if _ILLEGAL_RENAME_CHARS_RE.search(name) or any(ord(ch) < 0x20 for ch in name):
        return jsonify({"ok": False, "error": '名稱不能包含 < > : " / \\ | ? * 等系統不允許的符號'}), 400
    if len(name) > _MAX_RENAME_LEN:
        return jsonify({"ok": False, "error": f"名稱太長（最多 {_MAX_RENAME_LEN} 個字）"}), 400
    # 跟原名一樣 → 清掉自訂顯示名（回到 source_name）
    display_name = None if name == item["source_name"] else name
    deps.newanime_cache.set_item_enrichment(virtual_sn, display_name=display_name)
    if request.is_json or wants_json():
        return jsonify({"ok": True, "name": name})
    return redirect(url_for("newanime.detail", virtual_sn=virtual_sn))


def _toggle(virtual_sn: int, *, track: bool):
    deps = _deps()
    store = deps.newanime_cache
    item, _bulletin = _item_in_bulletin(deps, virtual_sn)
    if item is None:
        return form_result("找不到這部新番", endpoint="newanime.index", ok=False)
    name = item["display_name"] or item["source_name"]
    if track:
        store.track(virtual_sn, datetime.now().isoformat(timespec="seconds"))
        message = f"已追蹤《{name}》"
    else:
        store.untrack(virtual_sn)
        message = f"已取消追蹤《{name}》"
    # 即時匿名使用統計——新番收藏用虛構 sn 當 key，正式上架轉訂閱時 newanime/convert.py
    # 會 migrate 到真首集 sn（favorite 不歸零，使用者 2026-09-08）。
    collector = getattr(deps, "stats_collector", None)
    if collector is not None:
        try:
            collector.record_favorite(virtual_sn, name, favorited=track)
        except Exception:  # noqa: BLE001
            logger.debug("stats: 記收藏事件失敗", exc_info=True)
    # 一般表單送出：靜靜導回原頁，愛心圖示變化本身就是回饋，不用再彈對話框
    # （跟訂閱鈴鐺一致）。有 JS 的呼叫端才回 JSON 讓它彈 toast。
    if wants_json():
        return jsonify({"ok": True, "message": message})
    back = request.form.get("back")
    if back == "detail":
        return redirect(url_for("newanime.detail", virtual_sn=virtual_sn))
    return redirect(url_for("newanime.index"))


@newanime_bp.route("/newanime/<int:virtual_sn>/track", methods=["POST"])
def track(virtual_sn: int):
    return _toggle(virtual_sn, track=True)


@newanime_bp.route("/newanime/<int:virtual_sn>/untrack", methods=["POST"])
def untrack(virtual_sn: int):
    return _toggle(virtual_sn, track=False)
