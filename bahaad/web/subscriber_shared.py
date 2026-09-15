"""`bahaad/web/notify.py`（擁有者的「訂閱設定」管理頁）、
`bahaad/web/subscriber_auth.py`（訂閱者自己的登入/訂閱流程）與
`bahaad/web/__init__.py`（context processor）共用的小工具。
"""

from __future__ import annotations

from flask import request


def subscriber_prereqs_met(deps) -> bool:
    """訂閱者功能（登入／通知）僅限公開模式開啟且已填寫域名設定時才可用。"""
    return bool(deps.settings.get("public_mode", False)) and bool(
        deps.settings.get("public_domain", "") or ""
    )


SESSION_COOKIE = "subscriber_session"


def current_subscriber(deps) -> dict | None:
    """回傳目前這個瀏覽器已登入的訂閱者身分；沒登入或 session 無效回 `None`。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token or deps.subscriber_store is None:
        return None
    return deps.subscriber_store.resolve_session(token)
