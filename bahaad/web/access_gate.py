"""GitHub 連結／中斷連結／多 IP 封鎖畫面。規格見 docs/requirements/access_gate.md。

登入完全可選：這裡的路由只負責「使用者主動選擇連結／中斷連結 GitHub 帳號」，跟
`web/auth.py`（BahaAD 本機帳密，首次啟動強制設定）完全無關。
"""

from __future__ import annotations

import logging
import secrets
import time

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from bahaad.access_gate import oauth
from bahaad.web.settings import _DEFAULT_WEB_PORT

logger = logging.getLogger(__name__)

access_gate_bp = Blueprint("access_gate", __name__)

_STATE_SESSION_KEY = "access_gate_oauth_state"
# 大約一天一次的節流間隔——見 star_reminder_blocked() 的說明
_STAR_REMINDER_FALLBACK_MIN_INTERVAL_SECONDS = 20 * 60 * 60


@access_gate_bp.route("/access_gate/connect")
def connect():
    deps = current_app.config["DEPS"]
    port = deps.settings.get("web_port", _DEFAULT_WEB_PORT)
    client_callback = f"http://127.0.0.1:{port}{url_for('access_gate.callback')}"

    state = secrets.token_urlsafe(24)
    session[_STATE_SESSION_KEY] = state

    connect_url = oauth.build_connect_url(deps.access_gate_server_base_url, client_callback, state)
    return redirect(connect_url)


@access_gate_bp.route("/access_gate/callback")
def callback():
    deps = current_app.config["DEPS"]
    bahaad_token = request.args.get("bahaad_token")
    github_login = request.args.get("github_login")
    returned_state = request.args.get("state")
    expected_state = session.pop(_STATE_SESSION_KEY, None)

    if not bahaad_token or not github_login or not returned_state or returned_state != expected_state:
        return render_template(
            "access_gate_error.html", message="連結失敗：驗證資訊不符，請重新嘗試連結。"
        )

    deps.access_gate_store.save_connection(github_login, bahaad_token)
    return redirect(url_for("settings.index"))


@access_gate_bp.route("/access_gate/disconnect", methods=["POST"])
def disconnect():
    deps = current_app.config["DEPS"]
    bahaad_token = deps.access_gate_store.get_token()
    if bahaad_token is not None:
        oauth.disconnect(deps.access_gate_http, deps.access_gate_server_base_url, bahaad_token)
    deps.access_gate_store.disconnect()
    deps.settings.reset(["_access_gate_blocked", "_access_gate_starred"])

    next_endpoint = request.form.get("next") or "settings.index"
    return redirect(url_for(next_endpoint))


@access_gate_bp.route("/access_gate/blocked")
def blocked():
    deps = current_app.config["DEPS"]
    status = deps.access_gate_store.get_status()
    return render_template("access_gate_blocked.html", github_login=status["github_login"])


@access_gate_bp.route("/access_gate/star_reminder_blocked", methods=["POST"])
def star_reminder_blocked():
    """網頁上的 star 提醒（`base.html` 的 `#star-reminder-modal`）偵測到自己被瀏覽器端
    工具（廣告／元件封鎖套件的「封鎖此元素」、Tampermonkey 腳本……）隱藏或移除時
    回報，見 `base.html` 行內腳本的偵測邏輯——使用者 2026-09-17：「需要防止使用者
    規避提示或網站的相關元素」。

    這裡是這類手法真正碰不到的管道：系統匣通知完全在瀏覽器 DOM／CSS 的範圍之外，
    任何網頁端的隱藏或移除都影響不到它。**但不是無懈可擊**——瀏覽器端的偵測終究有
    極限，一個夠深入、在瀏覽器 DOM 就緒前就先接管 `fetch`／`MutationObserver` 的
    Tampermonkey 腳本，理論上可以連這支偵測邏輯一起繞過。這裡能確保涵蓋到的是
    最常見的情境——單純用廣告封鎖套件的元件選取工具「封鎖此元素」——一旦偵測到就
    補上系統匣通知這個逃生口。

    節流成大約一天一次：偵測腳本每次載入頁面都可能重新觸發回報，不節流的話系統匣
    通知會洗版。已經 star 過就直接忽略（有可能是 star 之後、背景心跳下一輪還沒
    把 `_access_gate_starred` 更新前，瀏覽器裡這段空窗期送出的過期回報）。"""
    deps = current_app.config["DEPS"]
    if deps.settings.get("_access_gate_starred") is True:
        return jsonify({"ok": True})
    last = deps.settings.get("_star_reminder_fallback_notified_at") or 0
    now = time.time()
    if now - last < _STAR_REMINDER_FALLBACK_MIN_INTERVAL_SECONDS:
        return jsonify({"ok": True})
    deps.settings.update({"_star_reminder_fallback_notified_at": now})
    tray_notify = getattr(deps, "tray_notify", None)
    if tray_notify is not None:
        try:
            tray_notify(
                "網頁上的 star 提醒似乎被瀏覽器端的封鎖工具擋掉了——喜歡 BahaAD 的話，"
                "可以到 github.com/1476523/BahaAD 幫忙點個 star 支持這個專案。",
                "支持一下 BahaAD",
            )
        except Exception:  # noqa: BLE001 - 系統匣通知失敗不該讓這個端點回錯
            logger.debug("star 提醒系統匣通知失敗", exc_info=True)
    return jsonify({"ok": True})
