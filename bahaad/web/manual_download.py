"""手動下載／單集補抓。規格見 docs/requirements/web_manual_download.md。

使用者輸入番劇 sn，頁面列出集數清單讓使用者勾選要抓哪幾集——不要求使用者自己知道每一集
確切的 video_sn。送出時先寫 `manual_tasks`（意外中斷後才能接續）再觸發
`MainLoop.trigger_manual_download()`，跟 `scheduler/main_loop.py` 共用同一套「要不要
下載」的判斷邏輯，不自己另開一條下載路徑。
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, request

from bahaad.gamer_client.catalog import CatalogError
from bahaad.scheduler.main_loop import DownloadTrigger

manual_download_bp = Blueprint("manual_download", __name__)

_STATUS_LABELS = {
    DownloadTrigger.SUBMITTED: "submitted",
    DownloadTrigger.ALREADY_DOWNLOADED: "already_downloaded",
    DownloadTrigger.ALREADY_ACTIVE: "already_active",
    # 理論上碰不到：這個 blueprint 的所有路由在 _access_gate_blocked 為真時，
    # web/__init__.py 的 before_request 鉤子早就把整個網頁介面鎖住了，走不到這裡；
    # 補上這個 label 只是避免 _STATUS_LABELS[trigger_result] 在某種意外情境下
    # 丟 KeyError，不是預期會被觸發的路徑
    DownloadTrigger.BLOCKED_BY_ACCESS_GATE: "blocked_by_access_gate",
}


@manual_download_bp.route("/manual_download")
def index():
    return render_template("manual_download.html")


@manual_download_bp.route("/manual_download/episodes", methods=["POST"])
def episodes():
    deps = current_app.config["DEPS"]
    try:
        sn = int(request.form.get("sn", ""))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "番劇 sn 必須是數字"})

    try:
        videos = deps.catalog.get_all_episodes(sn)
    except CatalogError as exc:
        return jsonify({"ok": False, "error": str(exc)})

    return jsonify(
        {
            "ok": True,
            "episodes": [
                {
                    "video_sn": video.video_sn,
                    "episode": video.episode_number,
                    "anime_title": video.anime_title,
                }
                for video in videos
            ],
        }
    )


@manual_download_bp.route("/manual_download/submit", methods=["POST"])
def submit():
    deps = current_app.config["DEPS"]
    rename = (request.form.get("rename") or "").strip() or None

    results = []
    for raw_sn in request.form.getlist("video_sn"):
        try:
            video_sn = int(raw_sn)
        except (TypeError, ValueError):
            results.append({"video_sn": raw_sn, "status": "invalid"})
            continue

        # 先寫紀錄再觸發，避免「觸發了但程式在寫紀錄前就意外關閉」的窗口
        if deps.manual_task_store is not None:
            deps.manual_task_store.save_task(video_sn, "single", {"rename": rename})

        try:
            trigger_result = deps.main_loop.trigger_manual_download(video_sn, rename)
        except CatalogError as exc:
            results.append({"video_sn": video_sn, "status": "error", "error": str(exc)})
            continue

        results.append({"video_sn": video_sn, "status": _STATUS_LABELS[trigger_result]})

    return jsonify({"results": results})
