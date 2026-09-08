"""Flask app 組裝。規格見 docs/requirements/web_overview.md。

`create_app(deps)` 是唯一組裝入口——`deps` 把 Phase 0～2 已經建好的服務物件（`store/`／
`registry.py`／`scheduler/` 等）打包起來，各 blueprint 透過 `current_app.config["DEPS"]`
取得同一份實例，不會各自重新 new 一份、導致跟背景執行緒看到的狀態對不起來。

存取保護（見 web_auth.md）在這裡用一個全域的 `before_request` 鉤子實作，不是要求每個
blueprint 的每支 route 自己記得套 `@login_required`——集中在一個地方，新增 blueprint/route
時不會有人忘記加保護，也不會有兩套「有沒有登入」的判斷邏輯各自為政。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from flask import (
    Flask, current_app, flash, g, redirect, render_template, request, session, url_for,
)

from bahaad import __version__ as _APP_VERSION, version_label as _version_label
from bahaad import is_beta as _app_is_beta

from bahaad.gamer_client.catalog import CatalogClient
from bahaad.registry import DownloadRegistry
from bahaad.scheduler.main_loop import MainLoop
from bahaad.store.access_gate import AccessGateStore
from bahaad.store.database import Database
from bahaad.store.gossip import GossipStore
from bahaad.store.manual_tasks import ManualTaskStore
from bahaad.store.notify import NotifyStore
from bahaad.store.schedule_list import ScheduleListStore
from bahaad.store.settings import SettingsStore
from bahaad.store.skipped_episodes import SkippedEpisodeStore
from bahaad.store.web_auth import WebAuthStore
from bahaad.vault import Vault

_DEFAULT_SECRET_KEY_LEN = 32

# 不需要「已設定帳密」也能進入的路由（首次設定本身——含驗證碼關卡兩步、忘記密碼救援
# 流程，以及首次設定畫面上「查看隱私權說明」的連結——那頁在登入前就看得到）
_SETUP_EXEMPT_ENDPOINTS = {
    "auth.setup", "auth.setup_verify", "auth.send_verify_code",
    "settings.privacy_policy", "static",
}
# 不需要「已登入」也能進入的路由（登入頁本身，以及忘記密碼救援流程——見 web_auth.md，
# 這是刻意不加保護的，不是漏加）
_LOGIN_EXEMPT_ENDPOINTS = {
    "auth.login",
    "auth.setup",
    "auth.setup_verify",
    "auth.forgot_password",
    "auth.reset",
    "auth.reset_everything",
    "auth.send_verify_code",
    "settings.privacy_policy",
    "static",
}
# 「公開模式」（設定 `public_mode`，預設關）開著時，沒登入也能進入的**唯讀**路由：
# 訂閱列表、下載列表、已快取的番劇頁、播放已下載的影片。使用者 2026-09-04：登出後
# 不用重新登入就能直接看已下載的影片。
#
# 白名單刻意壓到最小（2026-09-04 安全檢視）：
#   - 只 GET、只讀本地快取／DB，**任何會打 ani.gamer.com.tw 的都不放行**
#     （`anime_by_ref`／`anime_related` 會用 attacker 給的 ref_sn 觸發對外請求，排除）
#   - 不放行寫入（連 `/ui/theme`、`/ui/sidebar` 這種偏好寫入也不給——未登入者不該改
#     擁有者存的設定）
#   - 不放行只有登入功能才用得到的 API（`downloads_api_status` 會吐 `last_error`
#     內部訊息、`downloads_downloaded_fragment` 只有下載完成時 JS 會抓）
_PUBLIC_MODE_ENDPOINTS = {
    "browse.subscriptions",
    "browse.downloads",
    "browse.anime_detail",
    "browse.anime_episode_states",
    "browse.play_episode",
    "browse.play_episode_hls",
    "browse.play_episode_hls_segment",
    # 新番快訊（使用者 2026-09-08）：列表頁＋詳細頁都只 GET、只讀本地 newanime_cache／
    # youranimes_cache，不打 ani.gamer.com.tw。公開模式下 web/newanime.py 另外把清單
    # 限縮成「擁有者已追蹤」的那幾部。track/untrack/rename（寫入）刻意不放行。
    "newanime.index",
    "newanime.detail",
    # 番劇封面圖：只回本地已快取的圖檔，或 302 導去我們自己 DB 裡登記過的原網址
    # （attacker 湊不出沒登記過的 hash → 404，不能拿來當對外請求跳板）。不放行的話公開
    # 模式的卡片全是破圖（使用者 2026-09-04）。
    "cache.image",
    # 即時匿名使用統計（使用者 2026-09-08）——刻意的例外：這幾個 POST 只累加匿名計數／
    # 更新記憶體時戳，不寫擁有者資料、不打 ani.gamer.com.tw。公開模式訪客的觀看要算
    # 進「番劇觀看次數／公開模式在線人數」（規格明確要求），所以連 view/completion/
    # watching 這種 POST 都放行。summary/anime/leaderboard 是顯示數字用的讀取。
    "stats.summary",
    "stats.anime",
    "stats.leaderboard",
    "stats.leaderboard_page",
    "stats.view",
    "stats.completion",
    "stats.watching",
    "stats.watching_stop",
    "stats.alive",
}
# 重點（強制）更新待套用時，非豁免路由一律**硬回 503**（不是 302 導首頁）——API／外部
# 呼叫拿到明確的 503、動作絕不執行，擋掉「用功能繞過強制更新」。首頁的全站更新橫幅會
# 顯示「必須套用更新」＋按鈕。豁免：首頁本身、按鈕的 POST 端點、提醒視窗輪詢端點、
# static、番劇封面圖（不然使用者被鎖在首頁時，本季新番卡片全是破圖——`cache.image`
# 只回本地已快取的圖／302 導去登記過的原網址，是安全的 GET，跟 `_PUBLIC_MODE_ENDPOINTS`
# 同樣理由，使用者 2026-09-08）。不強制暫停背景排程執行緒（不硬中斷進行中的下載），
# 只鎖網頁 UI 的其他功能，見 docs/requirements/updater.md 與 web_redesign_round2.md
# 階段 3（dashboard 退休）。
_MANDATORY_UPDATE_EXEMPT_ENDPOINTS = {
    "index",
    "dashboard.apply_update",
    "dashboard.update_pending",
    "dashboard.update_version",
    "static",
    "cache.image",
}
_MANDATORY_UPDATE_BLOCK_MESSAGE = (
    "需要先套用更新才能繼續使用 BahaAD——請回主畫面按「套用更新並重新啟動」。"
)
# 同帳號多 IP 防護觸發時封鎖整個程式含網頁介面（比上面的 mandatory 更新鎖定更嚴格，
# 連 /dashboard 都不留），只留下顯示說明的畫面跟「中斷連結」按鈕，見
# docs/requirements/access_gate.md「web/__init__.py 的改動」一節
_ACCESS_GATE_BLOCKED_EXEMPT_ENDPOINTS = {
    "access_gate.blocked",
    "access_gate.disconnect",
    "static",
}

# 登入後閒置自動登出（使用者 2026-09-08，為了安全）。「動作」＝開網頁、送出表單、
# 看番劇（播放器每分鐘 ping 一個 POST）。背景 JSON 輪詢（通知／下載進度／集數狀態／
# 登入態檢查）**不算動作**——不然停在頁面就永遠不會登出。`session_idle_timeout_minutes`
# 是隱藏設定（無 UI），設 0＝關掉自動登出。
_IDLE_LAST_SEEN_KEY = "_last_seen"
_DEFAULT_IDLE_TIMEOUT_MINUTES = 60


@dataclass
class WebDeps:
    database: Database
    schedule_store: ScheduleListStore
    settings: SettingsStore
    registry: DownloadRegistry
    catalog: CatalogClient
    main_loop: MainLoop
    vault: Vault
    web_auth: WebAuthStore
    manual_task_store: ManualTaskStore
    # app_shell.py 組裝完系統匣圖示後才設得到（`icon.stop`），web/ 本來不知道怎麼關閉
    # 整個程式——這是唯一需要 app_shell.py 提供一個回呼給 web_deps 的地方，見
    # docs/requirements/updater.md「誰負責定期檢查、誰負責觸發」一節
    app_shutdown_hook: Callable[[], None] | None = field(default=None)
    # 同上——改「網頁介面連接埠」後觸發「優雅關閉 + 重新啟動」（使用者 2026-08-29 第 12 項）
    app_restart_hook: Callable[[], None] | None = field(default=None)
    # 以下三個是 access_gate/（Phase 6）新增的可選依賴，預設 None／空字串——GitHub
    # 登入完全可選，沒有連結過的情況下這些欄位不會被用到；existing 測試沒有理由
    # 一定要帶這些欄位，用預設值避免所有既有的 WebDeps(...) 建構呼叫都要跟著改
    access_gate_store: AccessGateStore | None = field(default=None)
    access_gate_http: Any = field(default=None)
    access_gate_server_base_url: str = field(default="")
    # gamer_client/browse.py（首頁本季新番／週期表爬取，見 docs/requirements/
    # gamer_client_browse.md）用的 HTTP client——跟上面 access_gate_http 同樣理由，
    # 預設 None，避免所有既有 WebDeps(...) 建構呼叫都要跟著改
    browse_http: Any = field(default=None)
    # notify/（Phase 8）新增，跟上面幾個可選依賴同樣理由：通知完全可選，沒有設定
    # Telegram/Discord 憑證的情況下這兩個欄位不會被用到
    notify_store: NotifyStore | None = field(default=None)
    notify_http: Any = field(default=None)
    # scheduler/gossip_watch.py（Phase 8）新增：監視公告的三個網頁頁面。gossip_watcher
    # 是因為「確認處置」要跟背景自動套用走同一個 apply_disposition()（含 _apply_lock），
    # 不是只有 gossip_store 的 CRUD。都給預設 None，避免既有 WebDeps(...) 建構呼叫全改
    gossip_store: GossipStore | None = field(default=None)
    gossip_watcher: Any = field(default=None)
    # store/skipped_episodes.py（階段 4「重新檢查排程更新」）：使用者把某一集標記為
    # 「已下載」（不下載、自動排程也不再排入）的清單。跟上面幾個一樣可選、預設 None
    skipped_episode_store: SkippedEpisodeStore | None = field(default=None)
    # scheduler/recheck.py 的 RecheckCoordinator（階段 4「重新檢查排程更新」的背景檢查
    # 執行緒＋狀態機）。可選、預設 None
    recheck_coordinator: Any = field(default=None)
    # cache/（番劇資料本地快取，見 docs/requirements/anime_cache.md）：能用快取就不打
    # ani.gamer.com.tw，降低風控機率。兩個都可選、預設 None（測試不用改），None 時
    # web/anime_data.py 的 helper 全部退化成直接爬取。
    anime_cache: Any = field(default=None)
    image_fetcher: Any = field(default=None)
    # youranimes.tw 番劇資料補充（見 docs/requirements/youranimes.md）：詳細頁的簡介
    # 替換 + 製作/配音/音樂右側欄。可選、預設 None（None 時 web/youranimes_view.py
    # 直接回 None，詳細頁維持動畫瘋原資料）。
    youranimes_cache: Any = field(default=None)
    # scheduler/youranimes_sync.py 的背景 job——設定頁「清除快取」清完之後 `wake()`
    # 它立刻重抓一輪季度頁，不用被動等每小時那一輪（使用者 2026-09-08）。可選、預設 None。
    youranimes_sync: Any = field(default=None)
    # net/proxy.py 的 ProxySelector（進階存取／代理）——設定頁存檔後
    # `reload_from_settings()` 就地更新，所有 session 共用同一個物件。可選、預設 None。
    proxy_selector: Any = field(default=None)
    # 動畫瘋登入設定 + 環境偽裝（見 docs/requirements/gamer_login_setup.md）。
    # identity_store 讓設定頁顯示指紋狀態；gamer_login_coordinator 是登入／指紋採集的
    # 背景 job 協調器。都可選、預設 None（既有測試不用改）。
    identity_store: Any = field(default=None)
    gamer_login_coordinator: Any = field(default=None)
    # 番劇完結自動偵測（completion_detection.md）：站內「訂閱通知」收件匣 + 完結狀態表。
    # 可選、預設 None（既有測試不用改）。
    completion_watch_store: Any = field(default=None)
    notification_store: Any = field(default=None)
    # store/logs.py（round 7 第 13 項）：設定頁的「日誌」查詢用。可選、預設 None。
    log_store: Any = field(default=None)
    # 新番快訊（new_anime_bulletin.md §6）：`/newanime` 列表頁 + 右側入口。可選、
    # 預設 None（None 時右側入口不顯示、`/newanime` 導回首頁）。
    newanime_cache: Any = field(default=None)
    # 已下載集數的 HLS 串流快取（web/hls.py）：遠端／網路不穩時用。可選、預設 None
    # （None 時只提供 mp4 直接串）。
    hls_cache: Any = field(default=None)
    # 高風險動作（首次設定／忘記密碼救援）的本機驗證碼關卡（verify_code.py）。可選、
    # 預設 None——公開模式／反向代理曝露在外時，「只接受 127.0.0.1」擋不住透過代理連進
    # 來的遠端訪客，這裡另外要求系統匣通知裡的 6 位數碼才能繼續（使用者 2026-09-06）。
    verify_code_gate: Any = field(default=None)
    # 驗證碼猜錯的漸進式鎖定（同一批功能，見 verify_code.py VerifyCodeLockout）。可選、
    # 預設 None（None 時 auth.py 的路由不鎖定，只靠 VerifyCodeGate 自己的重試上限）。
    verify_code_lockout: Any = field(default=None)
    # `pystray.Icon.notify(message, title)`——app_shell.py 組裝完系統匣圖示後才設得到，
    # 跟 app_shutdown_hook 同樣理由。None 時 verify_code_gate.issue() 會直接失敗（不假裝
    # 發送成功），呼叫端要把這個情況清楚告知使用者。
    tray_notify: Callable[[str, str], None] | None = field(default=None)
    # 即時匿名使用統計（realtime_stats.md）。可選、預設 None。
    # `stats_collector`：訂閱／收藏／播放埋點寫進 pending（`bahaad/stats/collector.py`）。
    # `stats_pending_store`：公開模式活躍時戳、給心跳讀（`bahaad/stats/heartbeat.py`）。
    # `stats_query`：本地代理，把 /stats/* 轉去 access_gate_server 並短快取（P4）。
    stats_collector: Any = field(default=None)
    stats_pending_store: Any = field(default=None)
    stats_query: Any = field(default=None)
    stats_activity: Any = field(default=None)


def create_app(deps: WebDeps) -> Flask:
    app = Flask(__name__)
    app.config["DEPS"] = deps

    # 靜態檔（側欄圖示、CSS、前端 JS）給一小時的瀏覽器快取。Flask 預設 `no-cache`，
    # 每次換頁都會對 ~35 個靜態檔各發一個 conditional GET（回 304）——首頁一次還疊上
    # ~55 張封面圖，短時間湧入 90+ 個請求、瀏覽器連線池反覆開關，偶爾就有連線被
    # 重設 → 圖示變綠色破圖（使用者 2026-09-08 回報「圖示有時候會缺圖」）。自我更新
    # 會換掉整包並重啟程式，快取頂多晚一小時生效，可接受；真的看到舊圖示強制重新
    # 整理即可。封面圖的 `/cache/img` 本來就已經帶 90 天 `max-age`（見 web/cache.py）。
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 3600

    # session cookie 簽章金鑰每次程式啟動都重新隨機產生、不從資料庫還原——關掉 BahaAD
    # 再打開就一定要重新登入，不會拿舊 session cookie 直接進站（使用者 2026-09-01，
    # 推翻 web_auth.md 原本「持久化以免每次重啟都要重登」的決定）。setup／改帳密當下
    # 由那些路由把新金鑰設進 current_app.secret_key，維持該次啟動內不掉線。
    app.secret_key = os.urandom(_DEFAULT_SECRET_KEY_LEN)

    from bahaad.web.access_gate import access_gate_bp
    from bahaad.web.auth import auth_bp
    from bahaad.web.browse import browse_bp
    from bahaad.web.cache import cache_bp, cached_img_url
    from bahaad.web.dashboard import dashboard_bp
    from bahaad.web.gamer_login import gamer_login_bp
    from bahaad.web.gossip import gossip_bp
    from bahaad.web.manual_download import manual_download_bp
    from bahaad.web.newanime import newanime_bp
    from bahaad.web.notifications import notifications_bp
    from bahaad.web.notify import notify_bp
    from bahaad.web.schedule import schedule_bp
    from bahaad.web.settings import settings_bp
    from bahaad.web.stats import stats_bp

    app.register_blueprint(access_gate_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(browse_bp)
    app.register_blueprint(cache_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(gamer_login_bp)
    app.register_blueprint(gossip_bp)
    app.register_blueprint(manual_download_bp)
    app.register_blueprint(newanime_bp)
    app.register_blueprint(notifications_bp)
    app.register_blueprint(notify_bp)
    app.register_blueprint(schedule_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(stats_bp)

    app.jinja_env.filters["cached_img"] = cached_img_url

    @app.before_request
    def _enforce_setup_and_login():
        deps = current_app.config["DEPS"]

        if not deps.web_auth.get_status().configured:
            if request.endpoint not in _SETUP_EXEMPT_ENDPOINTS:
                return redirect(url_for("auth.setup"))
            return None

        if request.endpoint in _LOGIN_EXEMPT_ENDPOINTS:
            return None
        if not session.get("logged_in"):
            # 公開模式：唯讀路由（訂閱／下載／已快取番劇頁／播放）放行，其他導登入。
            # 存取閘鎖住時（擁有者的 BahaAD 使用權被停用）連公開模式也一起擋。
            public_on = deps.settings.get("public_mode", False)
            if public_on and not deps.settings.get("_access_gate_blocked"):
                if request.endpoint in _PUBLIC_MODE_ENDPOINTS:
                    g.public_readonly = True
                    return None
                if request.endpoint == "index":
                    return redirect(url_for("browse.subscriptions"))
            return redirect(url_for("auth.login"))

        # 登入後閒置太久 → 自動登出（使用者 2026-09-08）。
        idle_redirect = _apply_idle_logout(deps)
        if idle_redirect is not None:
            return idle_redirect

        pending_update = deps.settings.get("_pending_update")
        if pending_update and pending_update.get("policy") == "important":
            if request.endpoint not in _MANDATORY_UPDATE_EXEMPT_ENDPOINTS:
                return _MANDATORY_UPDATE_BLOCK_MESSAGE, 503

        if deps.settings.get("_access_gate_blocked"):
            if request.endpoint not in _ACCESS_GATE_BLOCKED_EXEMPT_ENDPOINTS:
                return redirect(url_for("access_gate.blocked"))

        return None

    def _request_is_user_activity() -> bool:
        """這個請求算不算「使用者真的有動作」——會刷新閒置計時。
        算：所有 POST（訂閱／設定儲存／播放器每分鐘 ping…全是使用者觸發的，沒有背景
        定時 POST）、開網頁的 GET（Accept 含 text/html）。
        不算：static／封面圖、以及背景 JSON 輪詢（通知／下載進度／集數狀態／登入態檢查
        ——那些跟著計時器跑，停在頁面也會一直發）。"""
        if request.endpoint in ("static", "cache.image"):
            return False
        if request.method == "POST":
            return True
        if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
            return True
        return False

    def _apply_idle_logout(deps):
        raw = deps.settings.get("session_idle_timeout_minutes", _DEFAULT_IDLE_TIMEOUT_MINUTES)
        try:
            minutes = float(raw)
        except (TypeError, ValueError):
            minutes = _DEFAULT_IDLE_TIMEOUT_MINUTES
        if minutes <= 0:
            return None  # 0＝關掉自動登出（隱藏逃生口）

        now = time.time()
        last = session.get(_IDLE_LAST_SEEN_KEY)
        if last is not None and now - last > minutes * 60:
            session.clear()
            flash("太久沒有動作，為了安全已自動登出，請重新登入。")
            return redirect(url_for("auth.login"))
        if _request_is_user_activity():
            session[_IDLE_LAST_SEEN_KEY] = now
        return None

    @app.before_request
    def _stats_touch_activity():
        """即時統計「當前在線」——記一次「有人在操作頁面」的時戳（公開模式閒置超過
        10 分鐘就不計入在線人數，使用者 2026-09-08）。只認**開網頁**（Accept 含
        text/html）的 GET，排除 static／封面圖／JSON 輪詢（那些跟著計時器跑、不代表
        使用者真的在用）。"""
        act = getattr(current_app.config["DEPS"], "stats_activity", None)
        if act is None or request.method != "GET":
            return None
        if request.endpoint in ("static", "cache.image"):
            return None
        if "text/html" in request.headers.get("Accept", ""):
            try:
                act.touch()
            except Exception:  # noqa: BLE001
                pass
        return None

    _CLIENT_ID_COOKIE = "bahaad_client_id"

    @app.after_request
    def _ensure_client_id_cookie(response):
        # 純本機、匿名的隨機識別碼——只用來把「同一個瀏覽器」的驗證碼猜錯次數串在一起
        # 算漸進式鎖定（見 verify_code.py VerifyCodeLockout、auth.py 的驗證碼路由），
        # 不送到任何伺服器、不進診斷回報、不做任何其他追蹤用途。沒有才補設，httponly
        # （前端不需要讀）。
        if _CLIENT_ID_COOKIE not in request.cookies:
            import secrets as _secrets

            response.set_cookie(
                _CLIENT_ID_COOKIE, _secrets.token_hex(16),
                max_age=365 * 86400, httponly=True, samesite="Lax",
            )
        return response

    def _can_read() -> bool:
        """已登入，或「公開模式」放行的唯讀頁面——這兩種情況 context processor 才去碰
        SettingsStore／DB（其他情況維持原本「不碰、避免 auth.reset 意外重建 DB」）。"""
        return bool(session.get("logged_in") or getattr(g, "public_readonly", False))

    @app.context_processor
    def _inject_public_mode():
        # 每頁都要知道自己是不是「公開模式唯讀」——樣板據此收側欄、藏下載/改名/退訂鈕。
        return {"public_readonly": bool(getattr(g, "public_readonly", False))}

    @app.context_processor
    def _inject_idle_timeout():
        # 登入後閒置自動登出的分鐘數，給 idle_logout.js 用（0＝關）。只有登入頁面需要。
        if not session.get("logged_in"):
            return {"idle_timeout_minutes": 0}
        try:
            m = float(
                current_app.config["DEPS"].settings.get(
                    "session_idle_timeout_minutes", _DEFAULT_IDLE_TIMEOUT_MINUTES
                )
            )
        except (TypeError, ValueError):
            m = _DEFAULT_IDLE_TIMEOUT_MINUTES
        return {"idle_timeout_minutes": max(0, m)}

    @app.context_processor
    def _inject_access_gate_star_reminder():
        # 只在已登入 BahaAD 的頁面查——setup/login/忘記密碼這幾個登入前就能看到的頁面
        # 不需要這個提示，也刻意避免在 auth.reset 剛把資料庫檔案刪掉、行程準備結束的
        # 那個請求裡碰 SettingsStore：sqlite3.connect() 對一個不存在的路徑會直接建立
        # 一個新的空檔案，等於讓「重置 BahaAD」這個動作被這裡的查詢意外抵銷掉
        if not session.get("logged_in"):
            return {
                "access_gate_show_star_reminder": False,
                "access_gate_connected": False,
            }

        deps = current_app.config["DEPS"]
        connected = deps.access_gate_store is not None and deps.access_gate_store.get_status()["connected"]
        # 「未連結 GitHub 帳號」視同「還沒 star」——一樣要提示（使用者定案）。只有心跳
        # 明確回報 `_access_gate_starred is True` 時才收起提示。收起的頻率控制（每 10
        # 分鐘、「下次再說」）在 base.html 的行內 JS 用 localStorage 做，伺服器端只負責
        # 「該不該有這條提示」。
        starred = deps.settings.get("_access_gate_starred")
        return {
            "access_gate_show_star_reminder": starred is not True,
            "access_gate_connected": bool(connected),
        }

    @app.context_processor
    def _inject_gamer_login_stale():
        # 背景檢查偵測到動畫瘋登入態失效時掛的橫幅（app_shell 的 _note_gamer_login_state
        # 設 `_gamer_login_stale` 旗標）。背景**不會**自己開登入視窗，改由使用者看到
        # 橫幅後自己按「重新登入」（使用者 2026-09-01）。跟其他 context processor 一樣，
        # 未登入頁面不碰 SettingsStore（避免 auth.reset 那個請求意外重建 DB）。
        if not session.get("logged_in"):
            return {"gamer_login_stale": False}
        deps = current_app.config["DEPS"]
        vault = getattr(deps, "vault", None)
        configured = vault is not None and vault.get_status().get("configured")
        return {"gamer_login_stale": bool(configured and deps.settings.get("_gamer_login_stale"))}

    @app.context_processor
    def _inject_diagnostics_reminder():
        # 跟其他幾個 context processor 一樣理由，不在未登入頁面碰 SettingsStore，
        # 避免 auth.reset 那個請求被意外重新建立資料庫檔案，見
        # docs/requirements/diagnostics.md「使用者可見的部分」第 3 點
        if not session.get("logged_in"):
            return {"diagnostics_show_reminder": False}

        deps = current_app.config["DEPS"]
        return {"diagnostics_show_reminder": bool(deps.settings.get("_diagnostics_reminder_pending"))}

    @app.context_processor
    def _inject_newanime_nav():
        # 側欄「官方公告」下方的「新番快訊」入口——只在有「新番節目資訊」公告、且還在
        # 時效內時顯示（new_anime_bulletin.md §6a）。跟其他 context processor 一樣不在
        # 未登入頁面碰 store。公開模式（未登入唯讀）也給這個入口，但要有「擁有者已追蹤」
        # 的新番才顯示（使用者 2026-09-08）。
        if not _can_read():
            return {"newanime_nav_visible": False}
        from datetime import datetime as _dt

        from bahaad.newanime.visibility import first_visible

        deps = current_app.config["DEPS"]
        store = getattr(deps, "newanime_cache", None)
        if store is None:
            return {"newanime_nav_visible": False}
        try:
            bulletin = first_visible(store.list_bulletins(), _dt.now())
            if bulletin is None:
                visible = False
            elif getattr(g, "public_readonly", False):
                tracked = store.list_tracked()
                visible = any(
                    it["virtual_sn"] in tracked
                    for it in store.list_items(bulletin["season_key"])
                )
            else:
                visible = True
        except Exception:  # noqa: BLE001
            visible = False
        return {"newanime_nav_visible": visible}

    @app.context_processor
    def _inject_subscribed_sns():
        # 給訂閱鈴鐺（Phase 6）用，跟上面兩個 context processor 一樣理由：不在未登入
        # 頁面碰 store，避免 auth.reset 那個請求被意外重新建立資料庫檔案
        if not _can_read():
            return {"subscribed_sns": set(), "subscribed_renames": {}}

        deps = current_app.config["DEPS"]
        entries = deps.schedule_store.get_entries()
        subscribed = {sn: entry for sn, entry in entries.items() if entry.schedule_weekday is not None}

        # 使用者 2026-08-31：訂閱／改名按番劇不按單一集數——把訂閱的 sn 展開成「這些
        # 番劇的全部集數 sn」，這樣進同一部番劇的任一集都看得到金鈴鐺＋套用改的名字。
        expanded_sns: set[int] = set(subscribed)
        renames: dict[int, str] = {sn: e.rename for sn, e in subscribed.items() if e.rename}
        cache = getattr(deps, "anime_cache", None)
        if cache is not None and subscribed:
            try:
                members, origin = cache.resolve_subscription_expansion(list(subscribed))
                expanded_sns = members
                for member_sn, origin_sn in origin.items():
                    rename = subscribed[origin_sn].rename if origin_sn in subscribed else None
                    if rename:
                        renames.setdefault(member_sn, rename)
            except Exception:  # noqa: BLE001
                pass

        return {
            "subscribed_sns": expanded_sns,
            "subscribed_renames": renames,
        }

    @app.context_processor
    def _inject_ui_shell_prefs():
        # 跟上面的 star 提醒同樣理由，不在未登入頁面（setup/login/忘記密碼）碰
        # SettingsStore——避免 auth.reset 那個請求被意外重新建立資料庫檔案的問題，見
        # docs/requirements/web_redesign.md「整體殼層」
        if getattr(g, "public_readonly", False):
            # 公開模式（未登入唯讀）：主題／側欄收合是每個訪客自己的偏好——存在訪客瀏覽器
            # 的 cookie（shell.js 寫），伺服器這裡讀出來 server-render，換頁不會閃一下
            # （使用者 2026-09-06：localStorage 版本換頁會先展開再收起）。不套擁有者存的值。
            theme = request.cookies.get("bahaad_pub_theme")
            return {
                "ui_theme": theme if theme in ("dark", "light") else "dark",
                "ui_sidebar_collapsed": request.cookies.get("bahaad_pub_sidebar") == "1",
                "subscribe_intro_seen": True,
            }
        if not _can_read():
            return {"ui_theme": "dark", "ui_sidebar_collapsed": False, "subscribe_intro_seen": True}

        deps = current_app.config["DEPS"]
        return {
            "ui_theme": deps.settings.get("web_theme", "dark"),
            "ui_sidebar_collapsed": bool(deps.settings.get("web_sidebar_collapsed", False)),
            # 首次訂閱說明對話框看過一次就不再跳（見 web_redesign_round2.md 階段 2-2）。
            # 未登入頁面不會有鈴鐺，給 True 直接跳過。
            "subscribe_intro_seen": bool(deps.settings.get("_subscribe_intro_seen", False)),
        }

    @app.context_processor
    def _inject_app_version():
        # 側欄底部顯示「v<版本> [BETA]」。主版本為 0 就顯示 BETA（見 bahaad/_version.py
        # 的 is_beta()／docs/decisions/0001「Beta 期間版本方案」）；1.0.0 起自己消失。
        # `app_version` 打包版帶 commit 短雜湊（`0.0.1 (504e31c)`），方便回報問題對得上
        # 是哪一版 build（使用者 2026-09-09）。
        return {"app_version": _version_label(), "app_is_beta": _app_is_beta(_APP_VERSION)}

    @app.context_processor
    def _inject_update_banners():
        # 更新提示（GitHub Release 有新版本／差異更新下載完待套用）改成全站橫幅，
        # 在 base.html 顯示——原本只在已退休的 /dashboard 頁。跟其他 context processor
        # 一樣不在未登入頁面碰 SettingsStore（避免 auth.reset 重建資料庫檔案）。
        # 見 docs/requirements/web_redesign_round2.md 階段 3。
        if not session.get("logged_in"):
            return {"update_available": None, "pending_update": None, "firewall_setup_needed": False}

        deps = current_app.config["DEPS"]
        return {
            "update_available": deps.settings.get("_update_available"),
            "pending_update": deps.settings.get("_pending_update"),
            "github_releases_url": "https://github.com/1476523/BahaAD/releases/latest",
            # 第一次啟動自動跳的防火牆 UAC 被按了「否」（`app_shell._ensure_firewall_rule`）
            # → 首頁橫幅留一顆手動補設定的按鈕
            "firewall_setup_needed": bool(deps.settings.get("_firewall_setup_needed")),
        }

    @app.route("/")
    def index():
        # 首頁「本季新番／週期表」走快取優先——能用快取就不打 ani.gamer.com.tw，
        # 決策見 docs/requirements/anime_cache.md「觸發 3」。web/anime_data.py 也負責
        # 把封面 URL 批次登記進 image_cache＋排進背景抓取佇列。
        from bahaad.web.anime_data import group_newanime_by_date, home_bell_sns, home_data

        deps = current_app.config["DEPS"]
        newanime, weekly_schedule, browse_error = home_data(deps)
        return render_template(
            "home.html",
            username=session.get("username"),
            newanime=newanime,
            newanime_by_day=group_newanime_by_date(newanime),
            weekly_schedule=weekly_schedule,
            browse_error=browse_error,
            # 卡片＋週期表側板的鈴鐺該掛哪個 sn（見 anime_data.home_bell_sns）：已訂閱的
            # 重掛訂閱項目的 sn（番劇更新後週期表項目 sn 會變、不重掛會顯示未訂閱＋建重複
            # 訂閱，使用者 2026-09-05 回報）；沒訂閱的卡片重掛週期表同名項目的 sn。
            card_bell_sns=home_bell_sns(deps, newanime, weekly_schedule),
        )

    return app
