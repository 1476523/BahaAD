"""跟 `access_gate_server` 對話：組連結網址、查 star 狀態、送心跳、中斷連結。
規格見 docs/requirements/access_gate.md。

所有查詢失敗（連不上 `access_gate_server`）都記警告、回傳 `None`，呼叫端維持目前
已快取的狀態——連不上伺服器不代表沒事，也不代表被封鎖/沒 star，維持現狀最不會
誤傷使用者，見 access_gate.md 邊界案例。
"""

from __future__ import annotations

import logging
from typing import Protocol
from urllib.parse import urlencode

logger = logging.getLogger(__name__)


class HttpClient(Protocol):
    def get(self, url: str, headers: dict | None = None) -> "_ResponseLike": ...
    def post(self, url: str, headers: dict | None = None) -> "_ResponseLike": ...


class _ResponseLike(Protocol):
    status_code: int

    def json(self) -> dict: ...


def build_connect_url(access_gate_server_base_url: str, client_callback: str, state: str) -> str:
    params = {"client_callback": client_callback, "state": state}
    base = access_gate_server_base_url.rstrip("/")
    return f"{base}/oauth/start?{urlencode(params)}"


def star_status(http: HttpClient, access_gate_server_base_url: str, bahaad_token: str) -> bool | None:
    base = access_gate_server_base_url.rstrip("/")
    try:
        response = http.get(f"{base}/star-status", headers={"Authorization": f"Bearer {bahaad_token}"})
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        return bool(response.json()["starred"])
    except Exception as exc:
        logger.warning("access_gate 查詢 star 狀態失敗：%s", exc)
        return None


def heartbeat(http: HttpClient, access_gate_server_base_url: str, bahaad_token: str) -> bool | None:
    """送一次心跳，回傳這個帳號是否被判定同時多個 IP 使用（`True`＝要封鎖）。"""
    base = access_gate_server_base_url.rstrip("/")
    try:
        response = http.post(f"{base}/heartbeat", headers={"Authorization": f"Bearer {bahaad_token}"})
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        return bool(response.json()["blocked"])
    except Exception as exc:
        logger.warning("access_gate 心跳失敗：%s", exc)
        return None


def disconnect(http: HttpClient, access_gate_server_base_url: str, bahaad_token: str) -> None:
    """通知伺服器撤銷這組 token——盡力而為，失敗不擋。本機刪除 token（`store/
    access_gate.py` 的 `disconnect()`）才是真正決定「還算不算連結」的地方，這裡失敗
    了也要能繼續完成本機斷開連結，見 access_gate.md「登入是可選的」邊界案例。"""
    base = access_gate_server_base_url.rstrip("/")
    try:
        http.post(f"{base}/disconnect", headers={"Authorization": f"Bearer {bahaad_token}"})
    except Exception as exc:
        logger.warning("access_gate 通知伺服器撤銷 token 失敗（本機仍會斷開連結）：%s", exc)
