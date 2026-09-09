"""「套用更新並重新啟動」端點。

原本的「下載狀態儀表板」（`GET /dashboard`）已**退休**——下載狀態改看側欄的「下載
列表」（`GET /browse/downloads`，含「最近失敗」＋重試），更新提示改成 `base.html` 的
全站橫幅（`_inject_update_banners` context processor）。見
docs/requirements/web_redesign_round2.md 階段 3。

這裡只留下橫幅上「套用更新並重新啟動」按鈕的 POST 端點——blueprint 名稱與 URL
（`dashboard.apply_update` ／ `POST /dashboard/apply-update`）刻意不動，避免動到
`web/__init__.py` 的豁免名單跟既有測試。
"""

from __future__ import annotations

from pathlib import Path

from flask import (
    Blueprint,
    after_this_request,
    current_app,
    jsonify,
    redirect,
    request,
    url_for,
)

from bahaad import __version__
from bahaad.runtime_paths import own_exe_path
from bahaad.updater import UPDATE_STAGING_DIRNAME
from bahaad.updater.applier import install_root, launch_apply_and_restart

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/update/pending")
def update_pending():
    """目前待套用的差異更新（`_pending_update`）——給 base.html 的提醒視窗每小時輪詢用
    （`static/update_nag.js`）。沒有就回 `{"policy": null}`。重點更新鎖定時這個端點在
    豁免名單裡，讓被鎖在首頁的 JS 也問得到。"""
    deps = current_app.config["DEPS"]
    return jsonify(deps.settings.get("_pending_update") or {"policy": None})


@dashboard_bp.route("/update/version")
def update_version():
    """目前執行中的版本號。套用更新後 `update_nag.js` 的遮罩會輪詢這個端點——伺服器
    在小幫手覆蓋安裝目錄那幾秒是斷線的，等它回來、而且版本號變成目標版本，就代表
    更新完成、可以收掉遮罩。也在強制更新的豁免名單裡（遮罩在鎖定狀態下也要能輪詢）。"""
    return jsonify({"version": __version__})


def _wants_json() -> bool:
    return (
        request.headers.get("X-Requested-With") == "fetch"
        or request.accept_mimetypes.best == "application/json"
    )


@dashboard_bp.route("/dashboard/apply-update", methods=["POST"])
def apply_update():
    """套用已經下載好的差異更新（`_pending_update` 有值時才有意義）：啟動獨立的
    PowerShell 小幫手行程，接著呼叫 app_shutdown_hook 觸發整個程式優雅關閉——小幫手
    等主程式行程真的結束才會整批覆蓋安裝目錄、重新啟動。

    `update_nag.js` 走 `fetch`（帶 `X-Requested-With: fetch`）→ 回 JSON，前端接著顯示
    「正在重新啟動」遮罩、輪詢 `/update/version`、重啟後自動在**新分頁**開 BahaAD、
    並試著關掉目前這個分頁（使用者 2026-09-08：「關掉再重開」）。沒有 JS 時退回
    傳統表單送出 → `redirect` 回首頁（伺服器隨即關閉，畫面會短暫斷線，屬預期）。"""
    deps = current_app.config["DEPS"]
    pending_update = deps.settings.get("_pending_update")
    if not pending_update:
        if _wants_json():
            return jsonify({"ok": False, "error": "沒有待套用的更新"}), 409
        return redirect(url_for("index"))

    # 重啟後自動開新分頁（使用者 2026-09-08：舊分頁關掉、開新的）。
    deps.settings.update({"_reopen_web_on_start": True})
    base_dir = Path(deps.database.path).parent
    staging_dir = base_dir / UPDATE_STAGING_DIRNAME
    launch_apply_and_restart(
        install_root=install_root(),
        staging_dir=staging_dir,
        exe_path=own_exe_path(),
    )

    if _wants_json():
        # 先把回應送出去，再觸發關閉——不然關閉鉤子可能在 Flask 寫回應之前就把伺服器
        # 收掉，前端 fetch 直接拿到連線中斷、沒辦法區分「已受理」跟「失敗」。
        # `after_this_request` 只掛在這一次請求上，不會變成全域 after_request。
        @after_this_request
        def _shutdown_after(response):  # noqa: ANN001
            if deps.app_shutdown_hook is not None:
                deps.app_shutdown_hook()
            return response

        return jsonify({"ok": True, "target_version": pending_update.get("version")})

    if deps.app_shutdown_hook is not None:
        deps.app_shutdown_hook()
    return redirect(url_for("index"))
