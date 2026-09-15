"""訂閱者登入——Discord OAuth／Telegram Login Widget，兩者都經中繼完成驗證後導回
這裡。規格見「設計修正」筆記：訂閱者的身分綁定一律由訂閱者自己在這裡自助完成，
不經過擁有者；跟 `bahaad/web/auth.py`（擁有者登入，`web_auth`／Flask `session`）
是完全獨立的第二套身分系統——訂閱者 session 是獨立的 opaque token cookie
（`subscriber_sessions` 表），不寫進 Flask `session`，也不受擁有者登入閘
（`_enforce_setup_and_login`）管制。

Discord／Telegram 兩條登入路徑刻意設計成**同一種形狀**：`<channel>_start` 導去
中繼上對應的 OAuth-like 交握入口，中繼驗證完成後一律導回
`<channel>_callback`，帶著同一組查詢字串（`channel`／`external_id`／`ts`／
`signature`／`state`，`display_name` 可選）——`_complete_channel_login()` 因此
能兩邊共用，不用各自維護一份驗簽＋建 session 的邏輯。Telegram 使用者
2026-09-15 改用官方 Login Widget（在中繼那頁直接完成登入），取代先前「跳出深
連結叫 Telegram App 傳 `/start <code>`、網頁輪詢等結果」的做法——舊的配對碼
機制使用者反映體驗不直覺，且看起來像沒反應（其實是叫出了 App，不是網頁登入）。
"""

from __future__ import annotations

import secrets
import time
from urllib.parse import urlencode

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from bahaad.stats.collector import resolve_first_ep_sn
from bahaad.subscriber import relay_client
from bahaad.subscriber.signing import verify_signature
from bahaad.web.responses import form_result
from bahaad.web.subscriber_shared import SESSION_COOKIE, current_subscriber, subscriber_prereqs_met

subscriber_auth_bp = Blueprint("subscriber_auth", __name__)

_STATE_SESSION_KEY = "subscriber_oauth_state"
_SESSION_COOKIE_MAX_AGE = 30 * 86400  # 30 天
# callback 的簽章 payload 帶時間戳——超過這個秒數一律拒絕，避免導轉網址被截下來
# 重放（見「設計修正」筆記的安全考量）
_SIGNATURE_MAX_AGE_SECONDS = 300
# 帳號連結（使用者 2026-09-16）：這兩把鑰匙存在 Flask session（signed cookie，
# 只有這個瀏覽器讀得到），不是放在網址查詢字串／表單欄位——要連結的「另一個
# 身分」是從已經驗證過簽章的中繼回呼裡查到的，絕對不能讓使用者自己用網址參數
# 指定要併入哪個身分 id（不然就是任意帳號都能被拿去跟別人的帳號連結的漏洞）。
_LINK_MODE_SESSION_KEY = "subscriber_link_mode"
_LINK_PENDING_OTHER_ID_KEY = "subscriber_link_pending_other_id"
_CHANNELS = ("discord", "telegram")


def _installation_credentials(deps) -> tuple[str, str] | None:
    """回傳 `(installation_id, secret)`；訂閱功能尚未開放或尚未完成安裝綁定回 `None`。"""
    if not subscriber_prereqs_met(deps) or deps.subscriber_relay_store is None:
        return None
    return deps.subscriber_relay_store.get_credentials()


@subscriber_auth_bp.route("/subscriber/login")
def login():
    deps = current_app.config["DEPS"]
    if _installation_credentials(deps) is None:
        return render_template("subscriber_login.html", available=False)
    if current_subscriber(deps) is not None:
        return redirect(url_for("subscriber_auth.me"))
    return render_template("subscriber_login.html", available=True, error=None)


def _begin_login(deps, *, callback_path: str) -> str | None:
    """`discord_start()`／`telegram_start()` 共用：檢查安裝綁定、算好 state、決定
    這次是「登入」還是「連結到目前身分」（`?link=1` 且目前真的已經登入才算），
    回傳中繼那邊要導去的查詢字串；沒接好安裝就回 `None`。"""
    ready = _installation_credentials(deps)
    if ready is None:
        return None
    installation_id, _secret = ready

    state = secrets.token_urlsafe(24)
    session[_STATE_SESSION_KEY] = state
    current = current_subscriber(deps)
    if request.args.get("link") == "1" and current is not None:
        session[_LINK_MODE_SESSION_KEY] = True
    else:
        session.pop(_LINK_MODE_SESSION_KEY, None)
    public_domain = deps.settings.get("public_domain", "") or ""
    client_callback = f"{public_domain.rstrip('/')}{callback_path}"
    return urlencode({"installation_id": installation_id, "client_callback": client_callback, "state": state})


@subscriber_auth_bp.route("/subscriber/login/discord/start")
def discord_start():
    deps = current_app.config["DEPS"]
    query = _begin_login(deps, callback_path=url_for("subscriber_auth.discord_callback"))
    if query is None:
        return redirect(url_for("subscriber_auth.login"))
    return redirect(f"{deps.subscriber_relay_server_base_url.rstrip('/')}/subscriber/oauth/discord/start?{query}")


def _apply_link(deps, primary: dict, channel: str, external_id: str, display_name: str | None):
    """`link_mode=True` 時的收尾——`primary` 是連結發起當下已經登入的那個身分，
    `channel`／`external_id` 是剛驗證過簽章的「另一個」身分。兩邊都已經有訂閱
    清單才需要問使用者要怎麼處理衝突（見 `link_confirm`／`link_resolve`）；
    只有一邊有資料（或都沒有）就直接合併，不用多問一次。"""
    other_id = deps.subscriber_store.upsert_identity(channel, external_id, display_name)
    if other_id == primary["id"]:
        # 連結到自己目前登入的這個身分——沒有意義，當作沒事發生。
        return redirect(url_for("subscriber_auth.me"))

    other_follows = deps.subscriber_store.follows_for(other_id)
    primary_follows = deps.subscriber_store.follows_for(primary["id"])
    if other_follows and primary_follows:
        session[_LINK_PENDING_OTHER_ID_KEY] = other_id
        return redirect(url_for("subscriber_auth.link_confirm"))

    deps.subscriber_store.link_identity(primary["id"], other_id, keep="merge")
    return redirect(url_for("subscriber_auth.me"))


def _complete_channel_login(secret: str, *, link_mode: bool = False):
    """Discord／Telegram 走完各自在中繼那邊的交握後，導回這裡共用的收尾：驗證
    HMAC 簽章＋時效，然後依 `link_mode` 分兩條路——一般登入是建立/更新訂閱者
    身分＋session；`link_mode=True`（使用者在「我的訂閱」按了「連結
    Discord/Telegram 帳號」）則是把這個剛驗證過的身分併入**目前已經登入**的那
    個身分底下（見 `_apply_link()`）。兩個管道中繼導回來的查詢字串是同一套
    形狀（見檔頭說明），這裡完全共用，不用 Discord／Telegram 各寫一份。

    使用者 2026-09-13 回報：「登入成功後頁面需要刷新，不然會錯誤顯示登入失敗」。
    根因：瀏覽器有時會對 callback 網址重複發出請求（例如導轉過程中的重試），第
    一次請求成功後就已經把 state 從 session 彈出、設好 cookie；緊接著的第二次
    請求驗證會因為 state 已經被彈掉而判定失敗，即便使用者其實已經登入成功。
    呼叫端要先確認目前還沒登入（`current_subscriber(deps) is None`）才呼叫這支
    （`link_mode=False` 時），已經登入就直接導去我的訂閱，不要誤報登入失敗。"""
    channel = request.args.get("channel")
    external_id = request.args.get("external_id")
    ts = request.args.get("ts")
    signature = request.args.get("signature")
    display_name = request.args.get("display_name") or None
    returned_state = request.args.get("state")
    expected_state = session.pop(_STATE_SESSION_KEY, None)

    def _fail(message: str):
        return render_template("subscriber_login.html", available=True, error=message)

    if not all([channel, external_id, ts, signature]) or not returned_state or returned_state != expected_state:
        return _fail("登入失敗：驗證資訊不符，請重新嘗試。")

    payload = f"{channel}:{external_id}:{ts}".encode("utf-8")
    if not verify_signature(secret, payload, signature):
        return _fail("登入失敗：簽章驗證不通過。")
    try:
        if abs(time.time() - int(ts)) > _SIGNATURE_MAX_AGE_SECONDS:
            return _fail("登入逾時，請重新嘗試。")
    except ValueError:
        return _fail("登入失敗：資料格式不正確。")

    deps = current_app.config["DEPS"]

    if link_mode:
        primary = current_subscriber(deps)
        if primary is None:
            # session 中途過期之類的邊界狀況——退化成一般登入，總比整個失敗好。
            link_mode = False
        else:
            return _apply_link(deps, primary, channel, external_id, display_name)

    identity_id = deps.subscriber_store.upsert_identity(channel, external_id, display_name)
    token = deps.subscriber_store.create_session(identity_id)
    response = redirect(url_for("subscriber_auth.me"))
    response.set_cookie(
        SESSION_COOKIE, token, max_age=_SESSION_COOKIE_MAX_AGE, httponly=True, samesite="Lax"
    )
    return response


@subscriber_auth_bp.route("/subscriber/login/discord/callback")
def discord_callback():
    deps = current_app.config["DEPS"]
    ready = _installation_credentials(deps)
    if ready is None:
        return redirect(url_for("subscriber_auth.login"))
    _installation_id, secret = ready
    link_mode = session.pop(_LINK_MODE_SESSION_KEY, False)
    if not link_mode and current_subscriber(deps) is not None:
        return redirect(url_for("subscriber_auth.me"))
    return _complete_channel_login(secret, link_mode=link_mode)


@subscriber_auth_bp.route("/subscriber/login/telegram/start")
def telegram_start():
    """使用者 2026-09-15：Telegram 改用官方 Login Widget，跟 Discord 一樣是單純的
    GET 導轉——中繼那邊會渲染一頁內嵌 Telegram 官方登入按鈕的網頁（Bot 需要先在
    BotFather 用 `/setdomain` 指到中繼網域，Login Widget 才允許在那個網域上運作），
    使用者在那頁完成登入後，中繼驗證 Telegram 回傳的簽章沒問題，才導回下面的
    `telegram_callback`。"""
    deps = current_app.config["DEPS"]
    query = _begin_login(deps, callback_path=url_for("subscriber_auth.telegram_callback"))
    if query is None:
        return redirect(url_for("subscriber_auth.login"))
    return redirect(f"{deps.subscriber_relay_server_base_url.rstrip('/')}/subscriber/oauth/telegram/widget?{query}")


@subscriber_auth_bp.route("/subscriber/login/telegram/callback")
def telegram_callback():
    deps = current_app.config["DEPS"]
    ready = _installation_credentials(deps)
    if ready is None:
        return redirect(url_for("subscriber_auth.login"))
    _installation_id, secret = ready
    link_mode = session.pop(_LINK_MODE_SESSION_KEY, False)
    if not link_mode and current_subscriber(deps) is not None:
        return redirect(url_for("subscriber_auth.me"))
    return _complete_channel_login(secret, link_mode=link_mode)


@subscriber_auth_bp.route("/subscriber/link/confirm")
def link_confirm():
    """兩邊身分都已經有各自的訂閱清單時的衝突確認頁——`other_id` 完全從 Flask
    session 讀（見檔頭 `_LINK_PENDING_OTHER_ID_KEY` 的說明，不接受網址參數）。"""
    deps = current_app.config["DEPS"]
    primary = current_subscriber(deps)
    other_id = session.get(_LINK_PENDING_OTHER_ID_KEY)
    if primary is None or other_id is None:
        return redirect(url_for("subscriber_auth.me"))
    other = deps.subscriber_store.get_identity(other_id)
    if other is None:
        session.pop(_LINK_PENDING_OTHER_ID_KEY, None)
        return redirect(url_for("subscriber_auth.me"))
    return render_template(
        "subscriber_link_confirm.html",
        primary=primary,
        other=other,
        primary_count=len(deps.subscriber_store.follows_for(primary["id"])),
        other_count=len(deps.subscriber_store.follows_for(other_id)),
    )


@subscriber_auth_bp.route("/subscriber/link/resolve", methods=["POST"])
def link_resolve():
    deps = current_app.config["DEPS"]
    primary = current_subscriber(deps)
    other_id = session.pop(_LINK_PENDING_OTHER_ID_KEY, None)
    if primary is not None and other_id is not None:
        choice = request.form.get("choice")
        if choice in ("merge", "primary", "other"):
            deps.subscriber_store.link_identity(primary["id"], other_id, keep=choice)
    return redirect(url_for("subscriber_auth.me"))


@subscriber_auth_bp.route("/subscriber/notify-channels", methods=["POST"])
def notify_channels():
    """使用者 2026-09-16：連結兩個管道後，訂閱者可以隨時切換想通知的管道
    （選擇「合併」或問答時挑的那份清單以外，通知要送去哪裡是可以隨時調整的
    偏好，不是連結當下就定死）。至少要留一個管道開著，不然直接忽略這次送出。"""
    deps = current_app.config["DEPS"]
    identity = current_subscriber(deps)
    if identity is None:
        return jsonify({"ok": False, "message": "請先登入"}), 401
    linked = deps.subscriber_store.linked_identities_for(identity["id"])
    valid_channels = {i["channel"] for i in linked} or {identity["channel"]}
    chosen = {c for c in request.form.getlist("channels") if c in valid_channels}
    if not chosen:
        return form_result("至少要保留一個通知管道", endpoint="subscriber_auth.me", ok=False)
    deps.subscriber_store.set_notify_channels(identity["id"], chosen)
    return form_result("通知設定已儲存", endpoint="subscriber_auth.me")


@subscriber_auth_bp.route("/subscriber/me")
def me():
    from bahaad.web.anime_data import anime_cards_for_sns
    from bahaad.web.settings import _DEFAULT_ANIME_CACHE_TTL_DAYS

    deps = current_app.config["DEPS"]
    if not subscriber_prereqs_met(deps):
        # 擁有者關掉公開模式／清掉域名設定後，訂閱者不該還能用這頁——導去登入頁，
        # 該頁本來就會顯示「此功能目前尚未開放」。
        return redirect(url_for("subscriber_auth.login"))
    identity = current_subscriber(deps)
    if identity is None:
        return redirect(url_for("subscriber_auth.login"))
    follows = deps.subscriber_store.follows_for(identity["id"])
    ttl_days = int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS))
    # 「番劇的顯示需與訂閱列表顯示相同」（使用者 2026-09-12）——跟 browse.subscriptions
    # 共用同一套卡片資料組法（封面／集數），訂閱者這裡一律 cache_only（跟公開模式
    # 訪客同一套規則，不主動幫訂閱者觸發爬蟲）。
    cards = anime_cards_for_sns(
        deps, [(f["sn"], f["title"]) for f in follows], ttl_days=ttl_days, cache_only=True
    )
    # 使用者 2026-09-16：帳號可以連結 Discord／Telegram 成同一個人——`linked` 一定
    # 至少含自己這一筆，`notify_channels` 是目前開著通知的管道集合，
    # `linkable_channel` 是「還沒連結、可以按下去連結」的另一個管道（目前只有兩個
    # 管道，最多連一個）。
    linked = deps.subscriber_store.linked_identities_for(identity["id"])
    linked_channels = {i["channel"] for i in linked}
    notify_channels_enabled = deps.subscriber_store.get_notify_channels(identity["id"])
    linkable_channel = next((c for c in _CHANNELS if c not in linked_channels), None)
    # Telegram Login Widget 登入不會讓 Bot 跟訂閱者開啟對話，Bot 沒辦法對「從沒
    # 傳過訊息給它」的使用者發送第一則訊息——訂閱者還是要自己傳一次 /start 給
    # Bot，之後的通知才發得出去。只要 Telegram 是已連結管道之一就要提醒（不限
    # 於目前登入用的那個管道），查不到 Bot 資訊（中繼連不上／沒設定）就不顯示。
    telegram_bot_username = None
    if "telegram" in linked_channels:
        telegram_bot_username = relay_client.get_telegram_bot_username(
            deps.subscriber_relay_http, deps.subscriber_relay_server_base_url
        )
    return render_template(
        "subscriber_me.html", identity=identity, follows=cards,
        telegram_bot_username=telegram_bot_username,
        linked=linked, notify_channels_enabled=notify_channels_enabled, linkable_channel=linkable_channel,
    )


@subscriber_auth_bp.route("/subscriber/logout", methods=["POST"])
def logout():
    deps = current_app.config["DEPS"]
    token = request.cookies.get(SESSION_COOKIE)
    if token and deps.subscriber_store is not None:
        deps.subscriber_store.delete_session(token)
    response = redirect(url_for("subscriber_auth.login"))
    response.delete_cookie(SESSION_COOKIE)
    return response


# --- 訂閱者選擇要追蹤的番劇 ---


def _stats_sync_subscription(deps, canonical_sn: int, title: str | None) -> None:
    """訂閱者的通知訂閱／取消也要跟即時匿名使用統計的「番劇訂閱數」同步（使用者
    2026-09-13：「統計數字都需要與伺服器同步，而不是獨立顯示」）——跟擁有者排程
    清單共用同一個 `stats_collector.record_subscription()`。**不能單純用這次
    follow/unfollow 的結果覆蓋**：這個 sn 可能同時也在擁有者的排程清單裡，取消
    通知訂閱不該把擁有者真正的排程訂閱狀態一起洗成 0——狀態一律用「排程清單有
    這個 sn，或至少一個訂閱者還在追蹤」重新算過。"""
    collector = getattr(deps, "stats_collector", None)
    if collector is None:
        return
    try:
        entries = deps.schedule_store.get_entries() if deps.schedule_store is not None else {}
        entry = entries.get(canonical_sn)
        owner_subscribed = entry is not None and entry.schedule_weekday is not None
        followers = deps.subscriber_store.identities_following(canonical_sn) if deps.subscriber_store else []
        collector.record_subscription(canonical_sn, title, subscribed=owner_subscribed or bool(followers))
    except Exception:  # noqa: BLE001 - 埋點不影響訂閱功能本身
        pass


@subscriber_auth_bp.route("/subscriber/follow/<int:sn>", methods=["POST"])
def follow(sn: int):
    deps = current_app.config["DEPS"]
    if not subscriber_prereqs_met(deps):
        return jsonify({"ok": False, "info": "此功能尚未開放"})
    identity = current_subscriber(deps)
    if identity is None:
        return jsonify({"ok": False, "info": "請先登入"}), 401

    # 正規化成該番劇的首集 sn，跟擁有者訂閱系統同一套慣例——不然同一部番劇會因為
    # 從不同集數頁面訂閱而在 subscriber_follows 裡產生好幾筆紀錄。
    anime_cache = getattr(deps, "anime_cache", None)
    canonical_sn, title = resolve_first_ep_sn(anime_cache, sn)
    if not title and anime_cache is not None:
        title = anime_cache.anime_title_for(sn)

    deps.subscriber_store.add_follow(identity["id"], canonical_sn, title)
    _stats_sync_subscription(deps, canonical_sn, title)
    return jsonify({"ok": True, "sn": canonical_sn})


@subscriber_auth_bp.route("/subscriber/unfollow/<int:sn>", methods=["POST"])
def unfollow(sn: int):
    deps = current_app.config["DEPS"]
    if not subscriber_prereqs_met(deps):
        return jsonify({"ok": False, "info": "此功能尚未開放"})
    identity = current_subscriber(deps)
    if identity is None:
        return jsonify({"ok": False, "info": "請先登入"}), 401

    # 正規化成該番劇的首集 sn，跟 follow() 同一套慣例——不然從非首集的頁面（例如
    # 番劇詳細頁停在最新一集）按「取消訂閱通知」，這裡收到的 sn 跟
    # subscriber_follows 裡實際存的首集 sn 對不上，remove_follow 找不到列可刪，
    # 變成按了沒反應、退訂永遠失敗（使用者 2026-09-16 回報的連帶問題）。
    anime_cache = getattr(deps, "anime_cache", None)
    canonical_sn, _title = resolve_first_ep_sn(anime_cache, sn)

    deps.subscriber_store.remove_follow(identity["id"], canonical_sn)
    _stats_sync_subscription(deps, canonical_sn, None)
    return jsonify({"ok": True})
