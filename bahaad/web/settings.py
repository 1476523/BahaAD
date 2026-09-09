"""設定頁。規格見 docs/requirements/web_settings.md。

只暴露目前程式碼裡實際會讀取的設定 key——欄位的預設值直接從各消費模組 import 對應的
模組常數，不在這裡重新寫一次數字（那樣兩邊之後容易改一邊忘了改另一邊，變成「一個問題
兩個答案」）。`web_port` 是 Phase 3 新定案、目前只有這裡在用的 key，預設值就地定義。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

from flask import (
    Blueprint,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from bahaad import __version__ as _APP_VERSION, is_beta as _app_is_beta
from bahaad.diagnostics.reminder import dismiss_reminder
from bahaad.store.web_auth import WebAuthError
from bahaad.web.responses import form_result, wants_json
from bahaad.downloader.naming import (
    DEFAULT_PAD_WIDTH as _DEFAULT_FILENAME_PAD_WIDTH,
    DEFAULT_TEMPLATE as _DEFAULT_FILENAME_TEMPLATE,
    PLACEHOLDER_REFERENCE as _FILENAME_TOKENS,
    render_basename as _render_basename,
    sample_context as _filename_sample_context,
)
from bahaad.scheduler.seasons import (
    DEFAULT_AUTO_SEASON_FOLDER as _DEFAULT_AUTO_SEASON_FOLDER,
    DEFAULT_AUTO_SEASON_YEAR as _DEFAULT_AUTO_SEASON_YEAR,
)
from bahaad.downloader.segment import (
    _DEFAULT_MAX_CONCURRENT_SEGMENTS,
    _DEFAULT_MAX_RETRIES,
    _MAX_ALLOWED_CONCURRENT_SEGMENTS,
    DEFAULT_PREFERRED_QUALITY as _DEFAULT_PREFERRED_QUALITY,
)
from bahaad.scheduler.cookie_warmup import _DEFAULT_INTERVAL_HOURS as _DEFAULT_COOKIE_WARMUP_HOURS
from bahaad.scheduler.download_pool import (
    _DEFAULT_MAX_CONCURRENT_DOWNLOADS,
    _MAX_ALLOWED_CONCURRENT_DOWNLOADS,
)
from bahaad.scheduler.main_loop import (
    _DEFAULT_CHECK_INTERVAL_MINUTES,
    _DEFAULT_DOWNLOAD_COOLDOWN_SECONDS,
    _DEFAULT_DOWNLOAD_DIR,
    _DEFAULT_RETRY_BACKOFF_SECONDS,
    _DEFAULT_SN_QUERY_COOLDOWN_SECONDS,
)
from bahaad.web.db_cleanup import delete_records, list_orphans
from bahaad.net.proxy import (
    SETTINGS_ENABLED_KEY as _AA_ENABLED_KEY,
    SETTINGS_PROXIES_KEY as _AA_PROXIES_KEY,
    VALID_SCHEMES as _AA_SCHEMES,
    parse_proxies as _parse_proxies,
    validate_proxy_dict as _validate_proxy_dict,
)

settings_bp = Blueprint("settings", __name__)

_DEFAULT_WEB_PORT = 5996
# 登入態檢查週期（小時）的上限（使用者 2026-08-29：最多 6 小時）
_MAX_COOKIE_WARMUP_HOURS = 6
# 番劇資料本地快取的 TTL（見 docs/requirements/anime_cache.md）。這裡就地定義，
# web/anime_data.py／web/browse.py 從這裡 import，不重複寫數字。
_DEFAULT_ANIME_CACHE_TTL_DAYS = 90


@dataclass(frozen=True)
class _SettingField:
    key: str
    label: str
    kind: str  # "text" | "int" | "float" | "choice"
    default: object
    frontend_min: float | None = None
    frontend_max: float | None = None
    help_text: str | None = None
    requires_restart: bool = False
    # kind == "choice"：按鈕組的 (存的值, 顯示文字)。存的值要對得上
    # gamer_client/playlist.py 的 QualityVariant.label（小寫，如 "720p"）。
    choices: tuple[tuple[str, str], ...] | None = None
    # 這個選項值需要「已設定動畫瘋登入」才可選（樣板把它 disabled）。
    login_gated_choices: tuple[str, ...] = ()
    # 有值時：按鈕用圖示 `static/icons/{prefix}{value}.png`（使用者自製的畫質牌），
    # 文字轉成螢幕報讀用的隱藏標籤。
    choice_icon_prefix: str | None = None


# 偏好畫質：站方主清單目前就這四級（使用者 2026-09-05 實測）。預設 720P；1080P 需要
# 登入動畫瘋、且帳號為 VIP——非 VIP 時站方主清單根本不會有 1080p，下載時
# segment.select_quality 會自動退回可取得的最高畫質並記日誌。
_QUALITY_CHOICES = (
    ("360p", "360P"),
    ("540p", "540P"),
    ("720p", "720P"),
    ("1080p", "1080P（需動畫瘋 VIP）"),
)

_FIELDS: list[_SettingField] = [
    _SettingField(
        "preferred_quality", "偏好畫質", "choice", _DEFAULT_PREFERRED_QUALITY,
        choices=_QUALITY_CHOICES, login_gated_choices=("1080p",), choice_icon_prefix="quality-",
        help_text="下載時挑這個畫質。1080P 需要登入有 VIP 的動畫瘋帳號；拿不到偏好畫質時"
        "（非 VIP／站方這集只提供到 720P）會自動改用可取得的最高畫質，並記在日誌。",
    ),
    _SettingField("download_dir", "下載目錄", "text", _DEFAULT_DOWNLOAD_DIR),
    _SettingField(
        "check_interval_minutes", "全域檢查週期（分鐘）", "int", _DEFAULT_CHECK_INTERVAL_MINUTES,
        frontend_min=1,
    ),
    _SettingField(
        "max_concurrent_downloads", "最大一次下載數", "int", _DEFAULT_MAX_CONCURRENT_DOWNLOADS,
        frontend_min=1, frontend_max=_MAX_ALLOWED_CONCURRENT_DOWNLOADS,
        help_text=f"最多同時下載幾部番劇（上限 {_MAX_ALLOWED_CONCURRENT_DOWNLOADS}）。改了即時生效。",
    ),
    _SettingField(
        "max_concurrent_segments", "最大下載線程數", "int", _DEFAULT_MAX_CONCURRENT_SEGMENTS,
        frontend_min=1, frontend_max=_MAX_ALLOWED_CONCURRENT_SEGMENTS,
        help_text=f"單一番劇用幾條線程同時抓片段（上限 {_MAX_ALLOWED_CONCURRENT_SEGMENTS}）。改了即時生效。",
    ),
    _SettingField(
        "download_cooldown_seconds", "下載冷卻（秒）", "int", _DEFAULT_DOWNLOAD_COOLDOWN_SECONDS,
        frontend_min=10,
        help_text="每成功下載完一集之後的等待秒數，放慢對站方的請求節奏、降低風控機率。下載失敗不會冷卻（要能盡快重試）。最少 10 秒。",
    ),
    _SettingField(
        "sn_query_cooldown_seconds", "sn 解析冷卻（秒）", "int", _DEFAULT_SN_QUERY_COOLDOWN_SECONDS,
        frontend_min=2,
        help_text="排程檢查時，查詢每部番劇資訊之間的等待秒數。最少 2 秒。",
    ),
    _SettingField(
        "max_retries", "分段下載重試次數", "int", _DEFAULT_MAX_RETRIES, frontend_min=1,
        help_text="單一 .ts 片段下載失敗時自動重試幾次",
    ),
    _SettingField(
        "episode_retry_count", "整集重試次數", "int", 1, frontend_min=0,
        help_text="整集下載失敗後自動重跑幾次才放棄（0＝不重試，等下一輪排程檢查）",
    ),
    _SettingField(
        "episode_retry_backoff_seconds", "整集重試間隔（秒）", "int",
        _DEFAULT_RETRY_BACKOFF_SECONDS, frontend_min=0,
        help_text="整集重試之間的等待秒數（第 N 次重試等這個值的 N 倍），避免短時間內"
        "連續打好幾次同一支 API 被站方判定異常（code 1007 等）。0＝不等待、立刻重試。",
    ),
    _SettingField(
        "web_port", "網頁介面連接埠", "int", _DEFAULT_WEB_PORT,
        frontend_min=1, frontend_max=65535,
        help_text="需要重新啟動 BahaAD 才會套用新的連接埠",
        requires_restart=True,
    ),
]
_FIELDS_BY_KEY = {field.key: field for field in _FIELDS}


def _parse_value(field: _SettingField, raw: str):
    if field.kind == "text":
        return raw.strip()
    if field.kind == "choice":
        val = raw.strip()
        valid = {v for v, _ in (field.choices or ())}
        return val if val in valid else field.default
    if field.kind == "int":
        return int(raw)
    return float(raw)


def _display_value(field: _SettingField, stored):
    """render 用：choice 欄位存到不認得的舊值（例如以前 preferred_quality 存空字串
    ＝自動最高）就退回預設，讓 <select> 有東西可選。"""
    if field.kind == "choice":
        valid = {v for v, _ in (field.choices or ())}
        return stored if stored in valid else field.default
    if field.kind == "int" and isinstance(stored, (int, float)):
        return int(stored)
    return stored


def _format_bytes(n: float) -> str:
    if n < 1024:
        return f"{int(n)} B"
    for unit in ("KB", "MB", "GB"):
        n /= 1024
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
    return f"{n:.1f} GB"


def _anime_cache_size(deps) -> str:
    fetcher = getattr(deps, "image_fetcher", None)
    if fetcher is None or not hasattr(fetcher, "disk_usage_bytes"):
        return "0 B"
    return _format_bytes(fetcher.disk_usage_bytes())


def _port_change_response(deps, message: str):
    """改了「網頁介面連接埠」——重新啟動 BahaAD 套用新埠，重啟後自動開新分頁
    （使用者 2026-08-29 第 12 項）。沒有 app_restart_hook（dev server／測試）時退回
    純提示。"""
    import threading

    restart_hook = getattr(deps, "app_restart_hook", None)
    if restart_hook is None:
        return form_result(
            message + "，需要重新啟動 BahaAD 才會套用新的連接埠", endpoint="settings.index"
        )
    deps.settings.update({"_reopen_web_on_start": True})
    # 延遲一下再重啟，讓這個回應先送達瀏覽器
    threading.Timer(2.0, restart_hook).start()
    return form_result(
        "已儲存新的連接埠。BahaAD 正在重新啟動，重啟後會自動開啟新分頁——這個分頁可以關閉了。",
        endpoint="settings.index",
        json_extra={"restart": True},
    )


@settings_bp.route("/settings", methods=["GET", "POST"])
def index():
    deps = current_app.config["DEPS"]

    access_gate_status = (
        deps.access_gate_store.get_status()
        if deps.access_gate_store is not None
        else {"connected": False, "github_login": None, "connected_at": None}
    )

    if request.method == "POST":
        partial = {}
        port_changed = False
        try:
            for field in _FIELDS:
                if field.key not in request.form:
                    continue  # 這次請求根本沒帶這個欄位，維持原值，不是「使用者想清空」

                raw = (request.form.get(field.key) or "").strip()
                if field.key == "download_dir" and not raw:
                    raise ValueError("下載目錄不能是空字串")

                value = _parse_value(field, raw)
                current_value = deps.settings.get(field.key, field.default)
                if value != current_value:
                    partial[field.key] = value
                    if field.key == "web_port":
                        port_changed = True
        except ValueError:
            return form_result("格式不正確，請檢查輸入的數值", endpoint="settings.index", ok=False)
        if partial:
            deps.settings.update(partial)
        message = "設定已儲存"
        # round 7 第 11 項：改了下載目錄 → 試著把「等待搬移」的集數搬進新目錄
        if "download_dir" in partial and deps.main_loop is not None:
            moved = deps.main_loop.retry_all_pending_moves()
            if moved:
                message += f"；已把 {moved} 集待搬移的下載搬進新的下載目錄"
        if port_changed:
            return _port_change_response(deps, message)
        return form_result(message, endpoint="settings.index")

    # int 欄位若舊資料存成 float（例如冷卻秒數以前是 float，2.0）→ 顯示成整數；
    # choice 欄位舊值不在選項內就退回預設（見 _display_value）
    values = {
        field.key: _display_value(field, deps.settings.get(field.key, field.default))
        for field in _FIELDS
    }
    vault_status = deps.vault.get_status() if deps.vault is not None else {"configured": False, "account": None}
    fingerprint_status = (
        deps.identity_store.get_fingerprint_status()
        if deps.identity_store is not None
        else {"configured": False, "updated_at": None}
    )
    return render_template(
        "settings.html",
        fields=_FIELDS,
        values=values,
        anime_cache_size=_anime_cache_size(deps),
        access_gate_status=access_gate_status,
        diagnostics_enabled=deps.settings.get("diagnostics_enabled", True),
        diagnostics_reported_count=int(deps.settings.get("diagnostics_reported_count", 0) or 0),
        realtime_stats_enabled=deps.settings.get("realtime_stats_enabled", True),
        public_mode=bool(deps.settings.get("public_mode", False)),
        public_hidden_count=len(
            [t for t in (deps.settings.get("public_hidden", []) or []) if t and t.strip()]
        ),
        gamer_login_available=deps.gamer_login_coordinator is not None,
        vault_status=vault_status,
        fingerprint_status=fingerprint_status,
        show_dub_episodes=deps.settings.get("show_dub_episodes", False),
        completion_action=deps.settings.get("completion_action", "unsubscribe"),
        newanime_detect_enabled=bool(deps.settings.get("newanime_detect_enabled", True)),
        include_prereleases=bool(
            deps.settings.get("version_check_include_prereleases", _app_is_beta(_APP_VERSION))
        ),
        app_is_beta=_app_is_beta(_APP_VERSION),
        filename_template=deps.settings.get("filename_template", _DEFAULT_FILENAME_TEMPLATE),
        filename_pad_width=int(deps.settings.get("filename_pad_width", _DEFAULT_FILENAME_PAD_WIDTH)),
        download_danmu=bool(deps.settings.get("download_danmu", True)),
        auto_season_folder=bool(deps.settings.get("auto_season_folder", _DEFAULT_AUTO_SEASON_FOLDER)),
        auto_season_year_folder=bool(
            deps.settings.get("auto_season_year_folder", _DEFAULT_AUTO_SEASON_YEAR)
        ),
        filename_tokens=_FILENAME_TOKENS,
        filename_preview=_filename_preview(deps.settings),
        cookie_warmup_interval_hours=int(
            deps.settings.get("cookie_warmup_interval_hours", _DEFAULT_COOKIE_WARMUP_HOURS)
        ),
        max_cookie_warmup_hours=_MAX_COOKIE_WARMUP_HOURS,
        anime_cache_ttl_days=int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS)),
        web_username=deps.web_auth.get_status().username or "",
        advanced_access_enabled=bool(deps.settings.get(_AA_ENABLED_KEY, False)),
        advanced_access_proxies=_parse_proxies(deps.settings.get(_AA_PROXIES_KEY, [])),
        advanced_access_schemes=_AA_SCHEMES,
        advanced_access_status=(
            deps.proxy_selector.status() if getattr(deps, "proxy_selector", None) is not None else None
        ),
    )


@settings_bp.route("/settings/account", methods=["POST"])
def change_account():
    """更改網頁介面的帳號或密碼。兩種模式（`mode=username` / `mode=password`）都**必須**
    帶對的「目前密碼」才會套用（`WebAuthStore.change_credentials` 驗證），沒有「只送新值
    就生效」的路徑。成功後 session 金鑰已輪替 → 所有既有 session 失效，回應帶
    `relogin` 讓前端提示重新登入（其他開著的分頁靠 storage 事件一起提示）。"""
    deps = current_app.config["DEPS"]
    mode = request.form.get("mode")
    current_password = request.form.get("current_password") or ""

    try:
        if mode == "username":
            new_username = (request.form.get("new_username") or "").strip()
            if not new_username:
                return form_result("新的帳號名稱不能空白", endpoint="settings.index", ok=False)
            new_secret = deps.web_auth.change_credentials(current_password, new_username=new_username)
            done = "帳號已更新"
        elif mode == "password":
            new_password = request.form.get("new_password") or ""
            confirm = request.form.get("confirm_new_password") or ""
            if not new_password:
                return form_result("新密碼不能空白", endpoint="settings.index", ok=False)
            if new_password != confirm:
                return form_result("兩次輸入的新密碼不一致", endpoint="settings.index", ok=False)
            new_secret = deps.web_auth.change_credentials(current_password, new_password=new_password)
            done = "密碼已更新"
        else:
            return form_result("不明的操作", endpoint="settings.index", ok=False)
    except WebAuthError as exc:
        return form_result(str(exc), endpoint="settings.index", ok=False)
    except ValueError as exc:
        return form_result(str(exc), endpoint="settings.index", ok=False)

    # 金鑰已輪替：目前這個行程立刻換上新金鑰、清掉自己的 session。所有既有 session
    # cookie（含其他分頁）簽章驗不過 → 下次請求就會被 before_request 導去 /login。
    current_app.secret_key = new_secret
    session.clear()

    if wants_json():
        return jsonify({"ok": True, "message": done + "，請重新登入", "relogin": True})
    flash(done + "，請重新登入")
    return redirect(url_for("auth.login"))


def _filename_preview(settings) -> str:
    ctx = _filename_sample_context()
    return _render_basename(
        settings.get("filename_template", _DEFAULT_FILENAME_TEMPLATE),
        settings.get("filename_pad_width", _DEFAULT_FILENAME_PAD_WIDTH),
        anime_title=ctx["anime_title"],
        sn=ctx["sn"],
        episode_number=ctx["episode_number"],
        media=ctx.get("media"),
        finished_at=ctx.get("finished_at"),
    ) + ".mp4"


_COMPLETION_ACTIONS = ("unsubscribe", "notify", "mark")


@settings_bp.route("/settings/completion-action", methods=["POST"])
def set_completion_action():
    """番劇完結自動偵測的處置方式（見 docs/requirements/completion_detection.md）——
    下拉三選一，比照 diagnostics 開關的獨立表單。"""
    deps = current_app.config["DEPS"]
    value = (request.form.get("completion_action") or "").strip()
    if value not in _COMPLETION_ACTIONS:
        return form_result("未知的選項", endpoint="settings.index", ok=False)
    deps.settings.update({"completion_action": value})
    return form_result("已更新番劇完結偵測的處置方式", endpoint="settings.index")


def _gamer_coordinator(deps):
    return deps.gamer_login_coordinator


@settings_bp.route("/settings/gamer-login/start", methods=["POST"])
def gamer_login_start():
    """設定頁「動畫瘋登入」區塊：（重新）登入。導到共用進度頁。"""
    deps = current_app.config["DEPS"]
    coordinator = _gamer_coordinator(deps)
    if coordinator is None:
        return redirect(url_for("settings.index"))
    try:
        coordinator.start_login()
    except Exception as exc:  # noqa: BLE001 - GamerLoginCoordinatorError 等，訊息回給使用者
        flash(str(exc))
        return redirect(url_for("settings.index"))
    return redirect(url_for("gamer_login.progress", next="settings"))


@settings_bp.route("/settings/gamer-login/delete", methods=["POST"])
def gamer_login_delete():
    deps = current_app.config["DEPS"]
    coordinator = _gamer_coordinator(deps)
    if coordinator is not None:
        coordinator.logout()
    elif deps.vault is not None:
        deps.vault.delete_credentials()
    return form_result("已刪除動畫瘋登入資訊（帳號、密碼、Cookie）", endpoint="settings.index")


@settings_bp.route("/settings/fingerprint/refresh", methods=["POST"])
def fingerprint_refresh():
    deps = current_app.config["DEPS"]
    coordinator = _gamer_coordinator(deps)
    if coordinator is None:
        return redirect(url_for("settings.index"))
    coordinator.start_fingerprint_fetch()
    return redirect(url_for("gamer_login.progress", next="settings"))


@settings_bp.route("/settings/fingerprint/clear", methods=["POST"])
def fingerprint_clear():
    deps = current_app.config["DEPS"]
    coordinator = _gamer_coordinator(deps)
    if coordinator is not None:
        coordinator.clear_fingerprint()
    return form_result("已清除環境偽裝指紋，改用內建預設值", endpoint="settings.index")


@settings_bp.route("/settings/privacy-policy")
def privacy_policy():
    """自動錯誤回報的隱私權說明——每次按都重新讀 `docs/PRIVACY_POLICY.md` 原始檔案
    （不快取、永遠最新），轉成 HTML 顯示（使用者 2026-09-03）。dev 從 repo 根目錄讀；
    打包版靠 build.py 的 `--include-data-file` 把這份 md 一起打進 dist 的 docs/。

    `?embed=1`：只回內容片段（不套 base.html），給設定頁「查看隱私權說明」按鈕疊浮層用
    （使用者 2026-09-03：要疊在設定上、不是換頁）。"""
    from bahaad.web.mini_markdown import render as _md_render

    md_path = Path(__file__).resolve().parents[2] / "docs" / "PRIVACY_POLICY.md"
    try:
        md_text = md_path.read_text(encoding="utf-8")
    except OSError:
        md_text = "# BahaAD 隱私權說明\n\n找不到隱私權說明檔案（`docs/PRIVACY_POLICY.md`）。"
    policy_html = _md_render(md_text)
    if request.args.get("embed"):
        return policy_html
    return render_template("privacy_policy.html", policy_html=policy_html)


@settings_bp.route("/settings/newanime-detect", methods=["POST"])
def set_newanime_detect():
    """新番偵測總開關（見 docs/requirements/new_anime_bulletin.md §10）——關掉 → 整個
    停：不掃描、右側不顯示入口、不發新番通知；已追蹤的項目保留但不再自動轉訂閱。
    比照 diagnostics 開關的獨立 checkbox 表單。"""
    deps = current_app.config["DEPS"]
    deps.settings.update(
        {"newanime_detect_enabled": request.form.get("newanime_detect_enabled") == "on"}
    )
    return form_result("已更新新番偵測設定", endpoint="settings.index")


@settings_bp.route("/settings/diagnostics", methods=["POST"])
def set_diagnostics_enabled():
    """錯誤自動回報開關。見 docs/requirements/diagnostics.md「使用者可見的部分」第 2
    點——不是布林開關塞進上面通用的 `_FIELDS` 迴圈（那是給文字/數字設定用的），
    這裡是獨立的 checkbox 表單，比照 GitHub 帳號連結區塊的做法另外處理。"""
    deps = current_app.config["DEPS"]
    deps.settings.update({"diagnostics_enabled": request.form.get("diagnostics_enabled") == "on"})
    return form_result("已更新錯誤自動回報設定", endpoint="settings.index")


@settings_bp.route("/settings/realtime-stats", methods=["POST"])
def set_realtime_stats_enabled():
    """即時匿名使用統計開關（使用者 2026-09-08，預設開啟）。跟「錯誤自動回報」
    **完全獨立運作**——各自的執行緒／佇列／開關。關掉＝立刻停止所有回報與心跳，
    已暫存的 pending 保留不送、重新打開就繼續（不丟資料）。匿名啟動識別碼不刪。
    見 docs/requirements/realtime_stats.md。"""
    deps = current_app.config["DEPS"]
    deps.settings.update(
        {"realtime_stats_enabled": request.form.get("realtime_stats_enabled") == "on"}
    )
    return form_result("已更新即時統計設定", endpoint="settings.index")


@settings_bp.route("/settings/public-mode", methods=["POST"])
def set_public_mode():
    """公開模式開關（使用者 2026-09-04，預設關）。開啟後：沒登入也能看訂閱／已下載的
    番劇（純唯讀、只查本地快取／DB、不打動畫瘋、不顯示未下載集數與下載/改名/退訂鈕）。

    公開模式下**預設**公開全部訂閱／已下載的番劇（含之後新增的）；要藏個別番劇是在
    訂閱列表／下載列表的卡片上取消勾選「公開」（`browse.set_public_anime` →
    `public_hidden` denylist）。"""
    deps = current_app.config["DEPS"]
    deps.settings.update({"public_mode": request.form.get("public_mode") == "on"})
    return form_result("已更新公開模式設定", endpoint="settings.index")


# ---- 進階存取（代理 / proxy，見 docs/requirements/advanced_access.md）----

def _reload_proxy_selector(deps) -> None:
    sel = getattr(deps, "proxy_selector", None)
    if sel is not None:
        try:
            sel.reload_from_settings(deps.settings)
        except Exception:  # noqa: BLE001
            logger.debug("reload proxy selector 失敗", exc_info=True)


@settings_bp.route("/settings/advanced-access", methods=["POST"])
def set_advanced_access_enabled():
    """進階存取總開關。關掉＝所有連線直連；打開＝依設定組（主要→備用→其他）走代理，
    某組連不上自動切下一組。"""
    deps = current_app.config["DEPS"]
    deps.settings.update({_AA_ENABLED_KEY: request.form.get("advanced_access_enabled") == "on"})
    _reload_proxy_selector(deps)
    return form_result("已更新進階存取設定", endpoint="settings.index")


@settings_bp.route("/settings/advanced-access/add", methods=["POST"])
def add_advanced_access_proxy():
    deps = current_app.config["DEPS"]
    cfg, err = _validate_proxy_dict(
        {
            "role": request.form.get("role"),
            "label": request.form.get("label"),
            "scheme": request.form.get("scheme"),
            "host": request.form.get("host"),
            "port": request.form.get("port"),
        }
    )
    if cfg is None:
        return form_result(err or "設定格式不正確", endpoint="settings.index", ok=False)
    existing = [p for p in (deps.settings.get(_AA_PROXIES_KEY, []) or []) if isinstance(p, dict)]
    # 主要／備用各只留一組——同角色再加就取代舊的；其他可以多組
    if cfg.role in ("primary", "backup"):
        existing = [p for p in existing if p.get("role") != cfg.role]
    existing.append(cfg.as_dict())
    deps.settings.update({_AA_PROXIES_KEY: existing})
    _reload_proxy_selector(deps)
    return form_result(f"已新增{cfg.role_label}：{cfg.scheme_label} {cfg.host}:{cfg.port}", endpoint="settings.index")


@settings_bp.route("/settings/advanced-access/delete", methods=["POST"])
def delete_advanced_access_proxy():
    """用 scheme/host/port 指名刪除（顯示清單有排序過，不能用 index）。"""
    deps = current_app.config["DEPS"]
    want = (
        (request.form.get("scheme") or "").strip().lower(),
        (request.form.get("host") or "").strip(),
        (request.form.get("port") or "").strip(),
    )
    existing = [p for p in (deps.settings.get(_AA_PROXIES_KEY, []) or []) if isinstance(p, dict)]
    kept, removed = [], None
    for p in existing:
        key = (str(p.get("scheme", "")).lower(), str(p.get("host", "")), str(p.get("port", "")))
        if removed is None and key == want:
            removed = p
        else:
            kept.append(p)
    if removed is None:
        return form_result("找不到要刪除的設定組", endpoint="settings.index", ok=False)
    deps.settings.update({_AA_PROXIES_KEY: kept})
    _reload_proxy_selector(deps)
    return form_result(f"已刪除代理設定組（{removed.get('host', '')}）", endpoint="settings.index")


@settings_bp.route("/settings/firewall-allow", methods=["POST"])
def firewall_allow():
    """（手動補按，只在首頁橫幅出現）在 Windows 防火牆建「依映像路徑放行」的規則——
    跳一次 UAC 並等它做完（`bahaad/firewall.py`）。第一次啟動已經自動跳過一次；這顆
    按鈕是給「當時按了否、後來改變主意」或「搬過安裝資料夾」用的。監聽範圍
    （`web_lan_access`）沒有 UI——預設就是開放區網，只想僅本機的極少數人自己改 DB。"""
    deps = current_app.config["DEPS"]
    from bahaad import firewall
    from bahaad.runtime_paths import is_compiled, own_exe_path

    if not is_compiled():
        return form_result(
            "從原始碼執行時不需要（也不會）設定防火牆規則", endpoint="settings.index", ok=False
        )
    exe = own_exe_path()
    if firewall.ensure_rule(exe):
        deps.settings.update({"_firewall_rule_ok": str(exe)})
        deps.settings.reset(["_firewall_setup_needed"])
        return form_result(
            "防火牆規則已設定好——之後 BahaAD 更新換掉程式都不會再被防火牆詢問。",
            endpoint="settings.index",
        )
    deps.settings.update({"_firewall_setup_needed": True})
    return form_result(
        "沒能設定防火牆規則（可能在系統權限確認視窗按了「否」）。可以稍後再試一次。",
        endpoint="settings.index", ok=False,
    )


@settings_bp.route("/settings/cookie-warmup-interval", methods=["POST"])
def set_cookie_warmup_interval():
    """登入態檢查週期（小時）——2026-08-29 從「一般設定」搬到「動畫瘋登入」區塊，
    上限 6 小時。獨立數字欄位表單。"""
    deps = current_app.config["DEPS"]
    raw = (request.form.get("cookie_warmup_interval_hours") or "").strip()
    try:
        hours = int(raw)
        if not 1 <= hours <= _MAX_COOKIE_WARMUP_HOURS:
            raise ValueError
    except ValueError:
        return form_result(
            f"請填 1～{_MAX_COOKIE_WARMUP_HOURS} 的整數", endpoint="settings.index", ok=False
        )
    deps.settings.update({"cookie_warmup_interval_hours": hours})
    return form_result("已更新登入態檢查週期", endpoint="settings.index")


@settings_bp.route("/settings/anime-cache-ttl", methods=["POST"])
def set_anime_cache_ttl():
    """番劇資料快取保留天數——2026-08-29 從「一般設定」搬到「番劇資料快取」區塊。"""
    deps = current_app.config["DEPS"]
    raw = (request.form.get("anime_cache_ttl_days") or "").strip()
    try:
        days = int(raw)
        if days < 1:
            raise ValueError
    except ValueError:
        return form_result("請填 1 以上的整數", endpoint="settings.index", ok=False)
    deps.settings.update({"anime_cache_ttl_days": days})
    return form_result("已更新番劇資料快取保留天數", endpoint="settings.index")


@settings_bp.route("/settings/dub-episodes", methods=["POST"])
def set_show_dub_episodes():
    """「顯示中文配音集數」開關（custom-features #13）——比照 diagnostics 開關，獨立
    checkbox 表單，不塞進通用的 `_FIELDS` 迴圈。預設不勾。"""
    deps = current_app.config["DEPS"]
    deps.settings.update({"show_dub_episodes": request.form.get("show_dub_episodes") == "on"})
    return form_result("已更新「顯示中文配音集數」設定", endpoint="settings.index")


@settings_bp.route("/settings/update-channel", methods=["POST"])
def set_update_channel():
    """Beta channel 開關（`version_check_include_prereleases`）——獨立 checkbox 表單，
    不塞進通用的 `_FIELDS` 迴圈。勾＝也接收標 prerelease 的版本更新提示；預設值依這個
    build 本身是不是 Beta（`bahaad._version.is_beta`），見 docs/decisions/0001。"""
    deps = current_app.config["DEPS"]
    deps.settings.update(
        {"version_check_include_prereleases": request.form.get("version_check_include_prereleases") == "on"}
    )
    return form_result("已更新 Beta 版更新提示設定", endpoint="settings.index")


@settings_bp.route("/settings/filename", methods=["POST"])
def set_filename_settings():
    """檔案命名：模板 + 補齊長度 + 季別資料夾兩個開關 + 是否下載彈幕。獨立表單，不塞
    `_FIELDS`（模板是自由字串、其餘是 checkbox）。一次存整組，沒勾的 checkbox 就存 `False`。"""
    deps = current_app.config["DEPS"]
    template = (request.form.get("filename_template") or "").strip() or _DEFAULT_FILENAME_TEMPLATE
    try:
        pad = max(1, min(int(request.form.get("filename_pad_width", _DEFAULT_FILENAME_PAD_WIDTH)), 6))
    except (TypeError, ValueError):
        pad = _DEFAULT_FILENAME_PAD_WIDTH
    deps.settings.update(
        {
            "filename_template": template,
            "filename_pad_width": pad,
            "auto_season_folder": request.form.get("auto_season_folder") == "on",
            "auto_season_year_folder": request.form.get("auto_season_year_folder") == "on",
            "download_danmu": request.form.get("download_danmu") == "on",
        }
    )
    return form_result("已更新檔案命名設定", endpoint="settings.index")


@settings_bp.route("/settings/diagnostics/dismiss-reminder", methods=["POST"])
def dismiss_diagnostics_reminder():
    deps = current_app.config["DEPS"]
    dismiss_reminder(deps.settings)
    return ("", 204)


@settings_bp.route("/settings/db-cleanup", methods=["GET", "POST"])
def db_cleanup():
    """資料庫整頓（web_redesign_round2.md 階段 5）：列出「已不在追蹤清單、但 DB 還有
    紀錄」的番劇，讓使用者勾選清除。涵蓋不是透過退訂、而是直接編輯排程清單移除的
    情況（退訂鈴鐺本身已經會連帶清）。只動資料庫紀錄，不碰已下載的檔案。"""
    deps = current_app.config["DEPS"]

    if request.method == "POST":
        # round 7 第 7 項：「刪除歷史」——清掉「曾下載過、檔案已被移除」的已下載紀錄
        if request.form.get("action") == "clear_removed_history":
            one = request.form.get("video_sn")
            video_sn = int(one) if one and one.isdigit() else None
            n = deps.main_loop.clear_removed_download_history(video_sn) if deps.main_loop is not None else 0
            flash(f"已清除 {n} 筆刪除歷史" if n else "沒有清除任何刪除歷史")
            return redirect(url_for("settings.db_cleanup"))

        selected = {int(sn) for sn in request.form.getlist("sn") if sn.isdigit()}
        orphan_sns = {item["sn"] for item in _list_orphans(deps)}
        removed_records = 0
        removed_anime = 0
        for sn in selected & orphan_sns:  # 只清真的是孤兒的，防表單被竄改
            counts = delete_records(
                sn,
                gossip_store=deps.gossip_store,
                manual_task_store=deps.manual_task_store,
                skipped_episode_store=deps.skipped_episode_store,
            )
            n = sum(counts.values())
            if n:
                removed_records += n
                removed_anime += 1
        if removed_anime:
            flash(f"已清除 {removed_anime} 部番劇、共 {removed_records} 筆孤兒紀錄")
        else:
            flash("沒有清除任何紀錄")
        return redirect(url_for("settings.db_cleanup"))

    removed_history = (
        deps.main_loop.removed_download_history() if deps.main_loop is not None else []
    )
    return render_template(
        "db_cleanup.html", orphans=_list_orphans(deps), removed_history=removed_history
    )


def _list_orphans(deps):
    return list_orphans(
        deps.schedule_store,
        gossip_store=deps.gossip_store,
        manual_task_store=deps.manual_task_store,
        skipped_episode_store=deps.skipped_episode_store,
    )


@settings_bp.route("/settings/clear-anime-cache", methods=["POST"])
def clear_anime_cache():
    """清空番劇資料本地快取（文字＋圖片檔）。下次造訪會重新抓、重新快取。
    見 docs/requirements/anime_cache.md。"""
    deps = current_app.config["DEPS"]
    if getattr(deps, "anime_cache", None) is not None:
        deps.anime_cache.clear()
    if getattr(deps, "youranimes_cache", None) is not None:
        deps.youranimes_cache.clear()
    removed = 0
    if getattr(deps, "image_fetcher", None) is not None:
        removed = deps.image_fetcher.clear_disk()
    # 清完快取後主動叫醒季度頁背景 job 立刻重抓，不要被動等每小時那一輪（使用者
    # 2026-09-08：「清除快取後應該要喚醒而不是處於被動」）。
    if getattr(deps, "youranimes_sync", None) is not None:
        try:
            deps.youranimes_sync.wake()
        except Exception:  # noqa: BLE001 - 叫醒失敗不影響「快取已清空」這個結果
            logger.debug("清除快取後叫醒 youranimes_sync 失敗", exc_info=True)
    return form_result(
        f"已清空番劇資料快取（含 {removed} 個圖片檔）",
        endpoint="settings.index",
        json_extra={"update": {"#anime-cache-size": f"目前使用約 {_anime_cache_size(deps)}"}},
    )


_LOG_LEVELS = ("ALL", "INFO", "WARNING", "ERROR")
_LOG_LEVEL_LABELS = {
    "ALL": "全部",
    "DEBUG": "除錯",
    "INFO": "資訊",
    "WARNING": "警告",
    "ERROR": "錯誤",
    "CRITICAL": "嚴重",
}
_LOG_PAGE_SIZE = 50  # 使用者 2026-09-04：單頁 50 筆


@settings_bp.route("/settings/logs", methods=["GET", "POST"])
def logs():
    """應用程式日誌（round 7 第 13 項）：`bahaad` logger 的紀錄寫進 bahaad.db，可依
    等級篩、分頁看、一鍵清空。2026-08-29 起 DB 是唯一的日誌儲存。"""
    deps = current_app.config["DEPS"]
    log_store = getattr(deps, "log_store", None)

    if request.method == "POST":
        if log_store is not None and request.form.get("action") == "clear":
            n = log_store.clear()
            flash(f"已清空 {n} 筆日誌")
        return redirect(url_for("settings.logs"))

    level = (request.args.get("level") or "ALL").upper()
    if level not in _LOG_LEVELS:
        level = "ALL"
    min_level = None if level == "ALL" else level
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    entries: list = []
    total = 0
    if log_store is not None:
        total = log_store.count(min_level=min_level)
        entries = log_store.list(
            min_level=min_level, limit=_LOG_PAGE_SIZE, offset=(page - 1) * _LOG_PAGE_SIZE
        )
    has_next = page * _LOG_PAGE_SIZE < total
    return render_template(
        "logs.html",
        entries=entries,
        level=level,
        levels=_LOG_LEVELS,
        level_labels=_LOG_LEVEL_LABELS,
        page=page,
        has_next=has_next,
        total=total,
        available=log_store is not None,
    )


@settings_bp.route("/settings/reset/<key>", methods=["POST"])
def reset_field(key: str):
    if key not in _FIELDS_BY_KEY:
        return redirect(url_for("settings.index"))
    deps = current_app.config["DEPS"]
    deps.settings.reset([key])
    flash(f"「{_FIELDS_BY_KEY[key].label}」已還原成預設值")
    return redirect(url_for("settings.index"))


@settings_bp.route("/settings/save/<key>", methods=["POST"])
def save_field(key: str):
    """單欄儲存（使用者 2026-08-29 回饋：每欄「還原」旁邊自己一顆「儲存」）。
    表單會把整組欄位一起送來，這裡只取 URL 指定的那一欄存。"""
    field = _FIELDS_BY_KEY.get(key)
    if field is None:
        return form_result("未知的設定欄位", endpoint="settings.index", ok=False)
    deps = current_app.config["DEPS"]

    raw = (request.form.get(key) or "").strip()
    if key == "download_dir" and not raw:
        return form_result("下載目錄不能是空字串", endpoint="settings.index", ok=False)
    try:
        value = _parse_value(field, raw)
    except ValueError:
        return form_result("格式不正確，請檢查輸入的數值", endpoint="settings.index", ok=False)

    changed = value != deps.settings.get(key, field.default)
    if changed:
        deps.settings.update({key: value})

    message = f"「{field.label}」已儲存"
    if key == "download_dir" and deps.main_loop is not None:
        moved = deps.main_loop.retry_all_pending_moves()
        if moved:
            message += f"；已把 {moved} 集待搬移的下載搬進新的下載目錄"
    if key == "web_port" and changed:
        return _port_change_response(deps, message)
    return form_result(message, endpoint="settings.index")
