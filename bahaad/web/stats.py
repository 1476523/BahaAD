"""即時匿名使用統計的網頁端點。規格見 docs/requirements/realtime_stats.md。

兩類：

- **讀**（`GET /ui/stats/summary`、`POST /ui/stats/anime`、`GET /ui/stats/leaderboard`）：
  轉呼叫 `deps.stats_query`（打 access_gate_server、短快取），回 JSON 給前端 `stats.js`。
  前端不直接打 access_gate_server——不讓前端知道後端網址、也集中做快取。
- **寫**（`POST /ui/stats/view/<sn>`、`/completion/<sn>`、`/watching/<sn>`、
  `/watching/stop`）：播放器呼叫。把事件寫進 `stats_collector`（之後由 reporter 補送）
  ＋更新 `stats_activity`（「正在收看」給心跳讀）。**公開模式訪客也能打**——公開模式
  的觀看要算實際使用人數、番劇觀看次數公開模式關也算（使用者 2026-09-08）。

番劇 key 一律轉成首集 sn（`resolve_first_ep_sn`），跟訂閱／收藏對齊。
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify, render_template, request, url_for

from bahaad.stats.collector import resolve_first_ep_sn

logger = logging.getLogger(__name__)

stats_bp = Blueprint("stats", __name__)

# 新番快訊的虛構 sn 是 YYYYQQNNN（9 位數，>= 2 億）；動畫瘋真的 video_sn 目前 5-6 位數。
_VIRTUAL_SN_FLOOR = 100_000_000


def _deps():
    return current_app.config["DEPS"]


def _first_ep(deps, video_sn: int) -> tuple[int, str | None]:
    return resolve_first_ep_sn(getattr(deps, "anime_cache", None), video_sn)


def _cover_and_url(deps, sn: int) -> tuple[str | None, str | None]:
    """排行榜每一列的番劇封面圖 URL ＋ 點擊前往的頁面。封面一律用**番劇圖**
    （使用者 2026-09-08：不分集數、全部用番劇圖）；查本機快取，查不到就沒有圖。"""
    try:
        sn = int(sn)
    except (TypeError, ValueError):
        return None, None
    if sn >= _VIRTUAL_SN_FLOOR:
        nc = getattr(deps, "newanime_cache", None)
        item = nc.get_item(sn) if nc is not None else None
        cover = item.get("cover_url") if item else None
        return _cached(cover), (url_for("newanime.detail", virtual_sn=sn) if item else None)
    cache = getattr(deps, "anime_cache", None)
    cover = None
    if cache is not None:
        hit = cache.get_detail(sn)
        if hit is None:
            gk = cache.group_key_for(sn)
            for member in sorted(cache.group_members(gk)) if gk else ():
                hit = cache.get_detail(member)
                if hit is not None:
                    break
        if hit is not None:
            cover = hit[0].cover_url
    return _cached(cover), url_for("browse.anime_detail", video_sn=sn)


def _cached(cover: str | None) -> str | None:
    if not cover:
        return None
    from bahaad.web.cache import cached_img_url

    return cached_img_url(cover)


def _enrich_leaderboard(deps, data: dict) -> dict:
    for rows in (data or {}).values():
        if not isinstance(rows, list):
            continue
        for row in rows:
            cover, link = _cover_and_url(deps, row.get("sn"))
            if cover:
                row["cover"] = cover
            if link:
                row["url"] = link
    return data


# ---- 頁面 -----------------------------------------------------------


@stats_bp.route("/leaderboard")
def leaderboard_page():
    """「BahaAD 排行榜」頁——側欄「訂閱列表」下方的入口（使用者 2026-09-08）。
    公開模式也看得到。榜單資料由 stats.js 打 /ui/stats/leaderboard 即時填。"""
    return render_template("leaderboard.html")


# ---- 讀 ---------------------------------------------------------------


@stats_bp.route("/ui/stats/summary")
def summary():
    q = getattr(_deps(), "stats_query", None)
    return jsonify(q.summary() if q is not None else {})


@stats_bp.route("/ui/stats/anime", methods=["POST"])
def anime():
    q = getattr(_deps(), "stats_query", None)
    if q is None:
        return jsonify({})
    body = request.get_json(silent=True) or {}
    sns = body.get("sns") or []
    try:
        sns = [int(s) for s in list(sns)[:200]]
    except (TypeError, ValueError):
        sns = []
    return jsonify(q.anime(sns))


@stats_bp.route("/ui/stats/leaderboard")
def leaderboard():
    deps = _deps()
    q = getattr(deps, "stats_query", None)
    data = q.leaderboard() if q is not None else {}
    return jsonify(_enrich_leaderboard(deps, data))


# ---- 寫（播放器）----------------------------------------------------


@stats_bp.route("/ui/stats/view/<int:video_sn>", methods=["POST"])
def view(video_sn: int):
    deps = _deps()
    first_sn, title = _first_ep(deps, video_sn)
    collector = getattr(deps, "stats_collector", None)
    activity = getattr(deps, "stats_activity", None)
    if collector is not None:
        try:
            collector.record_view(first_sn, title)
        except Exception:  # noqa: BLE001
            logger.debug("stats: record_view 失敗", exc_info=True)
    if activity is not None:
        activity.touch_watching(first_sn)
    return jsonify({"ok": True})


@stats_bp.route("/ui/stats/completion/<int:video_sn>", methods=["POST"])
def completion(video_sn: int):
    deps = _deps()
    first_sn, title = _first_ep(deps, video_sn)
    collector = getattr(deps, "stats_collector", None)
    if collector is not None:
        try:
            collector.record_completion(first_sn, title)
        except Exception:  # noqa: BLE001
            logger.debug("stats: record_completion 失敗", exc_info=True)
    return jsonify({"ok": True})


@stats_bp.route("/ui/stats/watching/<int:video_sn>", methods=["POST"])
def watching(video_sn: int):
    deps = _deps()
    activity = getattr(deps, "stats_activity", None)
    if activity is not None:
        first_sn, _title = _first_ep(deps, video_sn)
        activity.touch_watching(first_sn)
    return jsonify({"ok": True})


@stats_bp.route("/ui/stats/watching/stop", methods=["POST"])
def watching_stop():
    activity = getattr(_deps(), "stats_activity", None)
    if activity is not None:
        activity.stop_watching()
    return jsonify({"ok": True})


@stats_bp.route("/ui/stats/alive", methods=["POST"])
def alive():
    """播放器開著但影片暫停時的「我還在」keepalive——使用者 2026-09-08：播放器開著
    就算動作、不要閒置登出。這個 POST 本身就會刷新 `session["_last_seen"]`
    （見 web/__init__.py `_request_is_user_activity`）；不碰任何統計數字。
    ✕ 關閉播放器後 `episode_picker.js` 就不再送。"""
    return jsonify({"ok": True})
