"""登入／首次設定／忘記密碼。規格見 docs/requirements/web_auth.md。

`before_request` 的全域保護（見 `web/__init__.py`）已經處理「哪些路由不用登入/設定就能
進」，這裡的 route 本身不用再各自檢查一次——除了幾個高風險動作另外多加保護：

- `/forgot-password/reset`／`/reset-everything`：「只接受 127.0.0.1」（見下方）＋
  未登入時要求系統匣驗證碼（見下一點）。
- **本機驗證碼關卡**（`verify_code.py`，使用者 2026-09-06）：公開模式／透過反向代理
  （例如 Cloudflare Tunnel）曝露在外時，「只接受 127.0.0.1」擋不住透過代理連進來的
  遠端訪客——Flask 看到的 `request.remote_addr` 永遠是代理本機的位址。首次設定
  （會先洩漏下載目錄路徑）跟忘記密碼救援（清憑證／整個重置）都是「還沒登入就打得到」
  的高風險動作，額外要求系統匣通知裡的 6 位數碼才能繼續：
    - 首次設定：先進「驗證身分」頁（`/setup` 沒驗證過就導去這頁），驗證過
      （`session["setup_verified"]`）才看得到「建立帳號密碼」表單本身，連下載目錄
      路徑都不會先曝露出去。
    - 忘記密碼救援：兩個表單各自要求對應用途（`forgot_password_reset`／
      `reset_everything`）的驗證碼，**已登入**時略過（設定頁「完整重置」入口本來就
      要先登入才進得去，不用疊加）。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from bahaad.scheduler.main_loop import _DEFAULT_DOWNLOAD_DIR
from bahaad.store.web_auth import AlreadyConfiguredError
from bahaad.verify_code import VerifyCodeError, format_lockout_duration

auth_bp = Blueprint("auth", __name__)

_RESET_SHUTDOWN_DELAY_SECONDS = 0.5
_VERIFY_CODE_PURPOSES = {"setup", "forgot_password_reset", "reset_everything"}
# 跟 web/__init__.py 的 after_request（`_ensure_client_id_cookie`）設的是同一個 cookie
# 名——純本機、匿名，只用來把「同一個瀏覽器」的驗證碼猜錯次數串起來算漸進式鎖定。
_CLIENT_ID_COOKIE = "bahaad_client_id"


def _lockout_key() -> str:
    return f"{request.remote_addr}:{request.cookies.get(_CLIENT_ID_COOKIE, '')}"


def _verify_code_ok(deps, purpose: str, code: str) -> bool:
    gate = getattr(deps, "verify_code_gate", None)
    if gate is None:
        return False  # 沒接驗證碼機制（例如舊組裝方式的測試）就不能通過，不是繞過
    return gate.verify(purpose, (code or "").strip())


def _verify_code_or_error(deps, purpose: str, code: str) -> str | None:
    """驗證碼 + 漸進式鎖定（`verify_code.py` VerifyCodeLockout）合併檢查。
    回傳 `None`＝通過；否則回傳要顯示給使用者的錯誤訊息（鎖定中／碼錯誤）。"""
    lockout = getattr(deps, "verify_code_lockout", None)
    key = _lockout_key()
    if lockout is not None:
        remaining = lockout.remaining_seconds(key)
        if remaining > 0:
            return f"驗證碼錯誤次數過多，已鎖定，還要等 {format_lockout_duration(remaining)}"
    if _verify_code_ok(deps, purpose, code):
        if lockout is not None:
            lockout.record_success(key)
        return None
    if lockout is not None:
        locked_for = lockout.record_failure(key)
        return f"驗證碼錯誤，已鎖定 {format_lockout_duration(locked_for)}"
    return "驗證碼錯誤或已過期，請重新發送"


def _shutdown_process() -> None:
    """獨立成一個模組層級函式，方便測試用 monkeypatch 替換掉，不會真的終止測試行程本身。"""
    os._exit(0)


def _delayed_shutdown() -> None:
    def _run() -> None:
        time.sleep(_RESET_SHUTDOWN_DELAY_SECONDS)
        _shutdown_process()

    threading.Thread(target=_run, daemon=True).start()


def _clear_all_credentials(deps) -> None:
    """「忘記密碼」重設：清掉所有用密碼／DPAPI 保護的東西，變回「還沒設定」的狀態
    ——BahaAD 登入密碼、瀏覽器偽裝指紋、動畫瘋帳密與二步驟驗證、動畫瘋 cookie、
    Telegram Bot Token、Discord Webhook。訂閱清單、一般設定、各種快取都保留
    （那些沒有用登入密碼加密，備份也救不回登入密碼，所以清掉沒意義）。使用者 2026-09-05。"""
    deps.web_auth.clear()
    for clear in (
        lambda: deps.vault.delete_credentials() if deps.vault is not None else None,
        lambda: deps.identity_store.clear_cookies() if getattr(deps, "identity_store", None) else None,
        lambda: deps.identity_store.clear_fingerprint() if getattr(deps, "identity_store", None) else None,
        lambda: deps.notify_store.clear_telegram_credentials() if getattr(deps, "notify_store", None) else None,
        lambda: deps.notify_store.clear_discord_credentials() if getattr(deps, "notify_store", None) else None,
    ):
        try:
            clear()
        except Exception:  # noqa: BLE001 - 盡力清乾淨，個別失敗不擋整個重設
            pass


@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    deps = current_app.config["DEPS"]
    if deps.web_auth.get_status().configured:
        return redirect(url_for("index"))

    # 驗證碼關卡：還沒證明「操作的人真的坐在這台電腦前面」之前，連帳號密碼表單都不給看
    # ——表單裡的下載目錄路徑會先洩漏出去（使用者 2026-09-06）。GET／POST 都擋，不能
    # 靠直接 POST 繞過只顯示 GET 的關卡。
    if not session.get("setup_verified"):
        if request.method == "POST":
            flash("請先完成驗證")
            return redirect(url_for("auth.setup"))
        lockout = getattr(deps, "verify_code_lockout", None)
        remaining = lockout.remaining_seconds(_lockout_key()) if lockout is not None else 0
        lockout_message = (
            f"目前鎖定中，還要等 {format_lockout_duration(remaining)}才能再次嘗試"
            if remaining > 0 else None
        )
        return render_template("setup_verify.html", lockout_message=lockout_message)

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""
        download_dir = (request.form.get("download_dir") or "").strip()

        if not username or not password:
            flash("帳號與密碼為必填")
        elif password != confirm_password:
            flash("兩次輸入的密碼不一致")
        else:
            try:
                secret_key = deps.web_auth.setup(username, password)
            except AlreadyConfiguredError:
                return redirect(url_for("index"))
            # 下載位置：使用者 2026-09-05 要在首次設定就能選。留白＝用預設值（不寫入
            # 覆蓋列，之後預設值調整才會自動生效）。
            if download_dir and download_dir != _DEFAULT_DOWNLOAD_DIR:
                deps.settings.update({"download_dir": download_dir})
            # 設定完成當下直接生效，不用重啟——跟其他「改設定要重開程式」的情況不同
            current_app.secret_key = secret_key
            session.pop("setup_verified", None)
            session["logged_in"] = True
            session["username"] = username
            # 帳密設定完成後，先問要不要現在登入動畫瘋（見 gamer_login_setup.md）。
            # 沒有 vault（部分精簡測試組裝）就維持原本直接進首頁。
            if deps.vault is not None:
                return redirect(url_for("gamer_login.setup_prompt"))
            return redirect(url_for("index"))

    return render_template("setup.html", default_download_dir=_DEFAULT_DOWNLOAD_DIR)


@auth_bp.route("/setup/verify", methods=["POST"])
def setup_verify():
    """首次設定的驗證碼關卡——驗證通過才把 `session["setup_verified"]` 設起來，回
    `/setup` 才看得到「建立帳號密碼」表單本身。"""
    deps = current_app.config["DEPS"]
    if deps.web_auth.get_status().configured:
        return redirect(url_for("index"))

    error = _verify_code_or_error(deps, "setup", request.form.get("verify_code") or "")
    if error:
        flash(error)
        return redirect(url_for("auth.setup"))
    session["setup_verified"] = True
    return redirect(url_for("auth.setup"))


@auth_bp.route("/verify-code/send", methods=["POST"])
def send_verify_code():
    """發送驗證碼——`purpose` 決定要不要送（首次設定／忘記密碼救援兩種表單各自的按鈕）。
    只送系統匣通知（`WebDeps.tray_notify`），絕不把碼放進這支的回應。"""
    deps = current_app.config["DEPS"]
    purpose = request.form.get("purpose") or ""
    if purpose not in _VERIFY_CODE_PURPOSES:
        return jsonify({"ok": False, "message": "不明的用途"}), 400

    lockout = getattr(deps, "verify_code_lockout", None)
    if lockout is not None:
        remaining = lockout.remaining_seconds(_lockout_key())
        if remaining > 0:
            return jsonify({
                "ok": False,
                "message": f"驗證碼錯誤次數過多，已鎖定，還要等 {format_lockout_duration(remaining)}",
            }), 429

    gate = getattr(deps, "verify_code_gate", None)
    if gate is None:
        return jsonify({"ok": False, "message": "這個環境沒有驗證碼功能"}), 503
    try:
        gate.issue(purpose, getattr(deps, "tray_notify", None))
    except VerifyCodeError as exc:
        return jsonify({"ok": False, "message": str(exc)}), 429
    return jsonify({"ok": True, "message": "驗證碼已送到這台電腦的系統匣通知，5 分鐘內有效"})


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    deps = current_app.config["DEPS"]

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if deps.web_auth.verify_password(username, password):
            session["logged_in"] = True
            session["username"] = username
            return redirect(url_for("index"))
        # 刻意不區分「帳號不存在」跟「密碼錯誤」，見 web_auth.md
        flash("帳號或密碼錯誤")

    return render_template("login.html")


@auth_bp.route("/logout")
def logout():
    session.clear()
    # 公開模式開著時，登出後直接回首頁（before_request 會再導到訂閱列表）——訪客本來就
    # 能唯讀瀏覽，停在登入頁沒意義（使用者 2026-09-04）。
    deps = current_app.config["DEPS"]
    if deps.settings.get("public_mode", False) and not deps.settings.get("_access_gate_blocked"):
        return redirect(url_for("index"))
    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot-password")
def forgot_password():
    return render_template("forgot_password.html")


@auth_bp.route("/forgot-password/reset", methods=["POST"])
def reset():
    """忘記密碼＝清掉所有用密碼／DPAPI 保護的憑證（登入密碼、指紋、動畫瘋帳密＋二步驟、
    動畫瘋 cookie、Telegram/Discord），變回「還沒設定」的狀態。訂閱清單、一般設定、
    快取全部保留。重開後跳首次設定頁重設一組新密碼、重新登入動畫瘋、重填通知憑證。
    使用者 2026-09-05：不再匯出檔案／不再整個 bahaad.db 刪掉。

    只接受 127.0.0.1（不是這台機器的區網位址）——區網內其他裝置不該能觸發重設。
    **未登入時**還要求系統匣驗證碼（使用者 2026-09-06：反向代理曝露在外時，
    `remote_addr` 這層保護對透過代理連進來的遠端訪客沒用）；已登入（例如從設定頁的
    連結點進來，本來就要先登入才進得去）就不疊加這一層。"""
    if request.remote_addr != "127.0.0.1":
        abort(403)

    deps = current_app.config["DEPS"]
    if not session.get("logged_in"):
        error = _verify_code_or_error(
            deps, "forgot_password_reset", request.form.get("verify_code") or ""
        )
        if error:
            flash(error)
            return redirect(url_for("auth.forgot_password"))

    _clear_all_credentials(deps)
    _delayed_shutdown()  # 給瀏覽器收下回應再結束行程，使用者手動重新啟動 BahaAD
    return render_template("reset_done.html")


@auth_bp.route("/reset-everything", methods=["POST"])
def reset_everything():
    """完整重置：整個 bahaad.db 刪掉（訂閱清單、動畫瘋登入、所有設定、快取全部清空）。
    設定頁「更改帳號與密碼」下方的危險選項，忘記密碼頁也有一個入口。同樣只接受
    127.0.0.1；未登入時同樣要求系統匣驗證碼（見 `reset()` 的說明）。"""
    if request.remote_addr != "127.0.0.1":
        abort(403)

    deps = current_app.config["DEPS"]
    if not session.get("logged_in"):
        error = _verify_code_or_error(
            deps, "reset_everything", request.form.get("verify_code") or ""
        )
        if error:
            flash(error)
            return redirect(url_for("auth.forgot_password"))

    # 只刪 bahaad.db*——**刻意不動 activation.db**（匿名啟動識別碼，使用者 2026-09-08：
    # 完全重置也不重置識別碼，只有刪掉整個 %LOCALAPPDATA%\BahaAD 才重產）。
    db_path = Path(deps.database.path)
    for suffix in ("", "-wal", "-shm"):
        side_car = db_path.parent / (db_path.name + suffix)
        if side_car.exists():
            side_car.unlink()

    # 從設定頁（已登入）點進來時 session 還是 logged_in——資料庫檔案都刪了，這個 session
    # 已經沒有對應的帳號。清掉，不然渲染 reset_done.html 時其他 context processor
    # （例如 access_gate 星星提醒）會在 `session.get("logged_in")` 誤判成「還登入著」，
    # 跑去碰已經刪除的資料庫檔案而炸掉（`sqlite3.OperationalError: no such table`）。
    session.clear()
    _delayed_shutdown()
    return render_template("reset_done.html")
