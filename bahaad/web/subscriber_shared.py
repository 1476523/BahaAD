"""`bahaad/web/notify.py`（擁有者的「訂閱設定」管理頁）、
`bahaad/web/subscriber_auth.py`（訂閱者自己的登入/訂閱流程）與
`bahaad/web/__init__.py`（context processor）共用的小工具。
"""

from __future__ import annotations

from flask import flash, g, request


def subscriber_prereqs_met(deps) -> bool:
    """訂閱者功能（登入／通知）僅限公開模式開啟且已填寫域名設定時才可用。"""
    return bool(deps.settings.get("public_mode", False)) and bool(
        deps.settings.get("public_domain", "") or ""
    )


SESSION_COOKIE = "subscriber_session"

# 跟擁有者登入共用同一個隱藏設定（`web/__init__.py` 的 `_DEFAULT_IDLE_TIMEOUT_
# MINUTES`）——使用者 2026-09-17：第三方登入也該有跟管理者一樣的閒置自動登出，
# 沒有理由另外開一個獨立的設定值。
_DEFAULT_IDLE_TIMEOUT_MINUTES = 60


def _idle_timeout_minutes(deps) -> float:
    try:
        return float(deps.settings.get("session_idle_timeout_minutes", _DEFAULT_IDLE_TIMEOUT_MINUTES))
    except (TypeError, ValueError):
        return _DEFAULT_IDLE_TIMEOUT_MINUTES


def _is_activity_request() -> bool:
    """跟 `web/__init__.py._request_is_user_activity()` 同一套判斷（那邊給擁有者
    登入用）：static／封面圖與背景 JSON 輪詢不算動作，開網頁的 GET／任何 POST 算。"""
    if request.endpoint in ("static", "cache.image", "cache.image_batch"):
        return False
    if request.method == "POST":
        return True
    if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
        return True
    return False


def current_subscriber(deps) -> dict | None:
    """回傳目前這個瀏覽器已登入的訂閱者身分；沒登入、session 無效、或閒置超過
    `session_idle_timeout_minutes` 回 `None`（見 `SubscribersStore.resolve_session()`）。
    有效登入時，若這次請求算「使用者活動」就順便刷新閒置計時。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token or deps.subscriber_store is None:
        return None
    identity = deps.subscriber_store.resolve_session(token, idle_timeout_minutes=_idle_timeout_minutes(deps))
    if identity is not None and _is_activity_request():
        deps.subscriber_store.touch_session(token)
    if identity is None:
        _notify_session_expired()
    return identity


SESSION_EXPIRED_MESSAGE = "太久沒有動作，為了安全已自動登出，請重新登入。"


def _notify_session_expired() -> None:
    """帶著登入 cookie 來、但 session 已失效（閒置逾時／固定到期／被清掉）：跟擁有者一樣提示一次
    「已自動登出」（使用者 2026-10-20：第三方登入自動登出沒有任何提示，畫面只是悄悄變成訪客）。

    只在「開網頁」的請求提示（背景 JSON 輪詢拿到的提示會被丟掉、使用者看不到），並在回應時清掉
    失效的 cookie（`web/__init__.py` 的 after_request），之後的請求不再重複提示。同一個請求
    只提示一次（context processor 與 route 都會呼叫 `current_subscriber`）。"""
    if g.get("_subscriber_expired_notified"):
        return
    if not (request.method == "GET" and "text/html" in request.headers.get("Accept", "")):
        return
    if request.endpoint in ("static", "cache.image", "cache.image_batch"):
        return
    g._subscriber_expired_notified = True
    g.clear_subscriber_cookie = True
    flash(SESSION_EXPIRED_MESSAGE)
