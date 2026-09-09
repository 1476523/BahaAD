"""動畫瘋登入設定 + 環境偽裝的網頁流程。規格見 docs/requirements/gamer_login_setup.md。

兩個進入點共用同一組頁面：
1. 首次設定 BahaAD 帳密之後 —— `auth.setup()` 導到 `/setup/gamer-login` 提示頁
2. 之後在設定頁 —— 「動畫瘋登入」收納區塊的按鈕

登入流程（開瀏覽器、代填、輪詢成功網址）與環境偽裝指紋採集都會阻塞好幾十秒到幾分鐘，
不能卡在請求裡：交給 `GamerLoginCoordinator` 背景 job，頁面導到進度頁後用
`/gamer-login/status` 輪詢。
"""

from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

from bahaad.gamer_client.gamer_login_coordinator import GamerLoginCoordinatorError

gamer_login_bp = Blueprint("gamer_login", __name__)

# 進度頁完成後可以導回的白名單——不接受任意 next，避免開放導向
_ALLOWED_NEXT = {
    "index": "/",
    "settings": "/settings",
}


def _coordinator():
    return current_app.config["DEPS"].gamer_login_coordinator


def _resolve_next(raw: str | None) -> str:
    return _ALLOWED_NEXT.get(raw or "index", "/")


@gamer_login_bp.route("/setup/gamer-login")
def setup_prompt():
    """首次設定帳密後的提示頁：要不要現在登入動畫瘋。順便在背景開始採集環境偽裝指紋
    （不擋頁面、失敗也只是指紋維持罐頭值）。"""
    coordinator = _coordinator()
    if coordinator is not None:
        coordinator.start_fingerprint_fetch()
    return render_template("setup_gamer_login.html")


@gamer_login_bp.route("/setup/gamer-login/skip", methods=["POST"])
def setup_skip():
    flash("之後可以到「設定 › 動畫瘋登入」隨時登入動畫瘋。")
    return redirect(url_for("index"))


@gamer_login_bp.route("/gamer-login/creds", methods=["GET", "POST"])
def creds():
    """動畫瘋帳號／密碼／兩步驟驗證密鑰（選填）輸入。存進 vault 後起登入 job。"""
    deps = current_app.config["DEPS"]
    next_key = request.args.get("next") or request.form.get("next") or "index"

    if request.method == "POST":
        account = (request.form.get("account") or "").strip()
        password = request.form.get("password") or ""
        totp_secret = (request.form.get("totp_secret") or "").strip() or None

        if not account or not password:
            flash("動畫瘋帳號與密碼為必填")
            return render_template("gamer_login_creds.html", next_key=next_key)

        deps.vault.save_credentials(account, password, totp_secret)

        coordinator = _coordinator()
        if coordinator is not None:
            try:
                coordinator.start_login()
            except GamerLoginCoordinatorError as exc:
                flash(str(exc))
        return redirect(url_for("gamer_login.progress", next=next_key))

    return render_template("gamer_login_creds.html", next_key=next_key)


@gamer_login_bp.route("/gamer-login/progress")
def progress():
    next_key = request.args.get("next") or "index"
    return render_template(
        "gamer_login_progress.html",
        next_url=_resolve_next(next_key),
        next_key=next_key,
    )


@gamer_login_bp.route("/gamer-login/status")
def status():
    coordinator = _coordinator()
    if coordinator is None:
        return jsonify({"state": "idle", "kind": None, "message": ""})
    return jsonify(coordinator.status())
