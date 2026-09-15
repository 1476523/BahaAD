"""GitHub 連結／中斷連結／多 IP 封鎖畫面。規格見 docs/requirements/access_gate.md。

登入完全可選：這裡的路由只負責「使用者主動選擇連結／中斷連結 GitHub 帳號」，跟
`web/auth.py`（BahaAD 本機帳密，首次啟動強制設定）完全無關。
"""

from __future__ import annotations

import secrets

from flask import Blueprint, current_app, redirect, render_template, request, session, url_for

from bahaad.access_gate import oauth
from bahaad.web.settings import _DEFAULT_WEB_PORT

access_gate_bp = Blueprint("access_gate", __name__)

_STATE_SESSION_KEY = "access_gate_oauth_state"


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
