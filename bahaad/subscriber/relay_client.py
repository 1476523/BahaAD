"""跟 access_gate_server 的訂閱者中繼對話：註冊這台安裝實例、請中繼代發通知。規格見
docs/requirements/subscriber_notify.md（Phase 1：中繼綁定基礎建設）。Telegram/Discord
登入本身（OAuth／Login Widget）是瀏覽器導轉去中繼完成，不是這裡的 server-to-server
呼叫，見 `bahaad/web/subscriber_auth.py`。

跟 `bahaad/access_gate/oauth.py` 同樣的 `HttpClient` Protocol 依賴注入慣例、同樣的
「失敗記警告、回傳 `None`、呼叫端維持現狀」原則——連不上中繼不代表帳號有問題，維持
現狀最不會誤傷使用者。
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

from bahaad.subscriber.signing import sign_payload

logger = logging.getLogger(__name__)


class HttpClient(Protocol):
    def get(self, url: str) -> "_ResponseLike": ...
    def post(self, url: str, data: bytes | None = None, headers: dict | None = None) -> "_ResponseLike": ...


class _ResponseLike(Protocol):
    status_code: int

    def json(self) -> dict: ...


def register_installation(http: HttpClient, base_url: str, callback_base_url: str) -> dict[str, str] | None:
    """回傳 `{"installation_id", "secret"}`；失敗回 `None`。`secret` 只有這次回傳，
    呼叫端要立刻存起來（`store/subscriber_relay.py`）。"""
    base = base_url.rstrip("/")
    body = json.dumps({"callback_base_url": callback_base_url}).encode("utf-8")
    try:
        response = http.post(
            f"{base}/subscriber/register", data=body, headers={"Content-Type": "application/json"}
        )
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        payload = response.json()
        return {"installation_id": payload["installation_id"], "secret": payload["secret"]}
    except Exception as exc:
        logger.warning("訂閱中繼註冊失敗：%s", exc)
        return None


def get_telegram_bot_username(http: HttpClient, base_url: str) -> str | None:
    """Bot 的公開使用者名稱——訂閱者「我的訂閱」頁提示「請傳送 /start 開啟通知」
    要用來組 `t.me/<username>` 深連結（見「使用者 2026-09-16」筆記：Telegram
    Login Widget 登入不會讓 Bot 跟訂閱者開啟對話，還是要訂閱者自己傳一次
    `/start`，Bot 才發得出通知）。連不上中繼、或中繼沒設定 Bot Token 時回
    `None`，呼叫端就不顯示這段提示，不強迫使用者一定要看到。"""
    base = base_url.rstrip("/")
    try:
        response = http.get(f"{base}/subscriber/telegram/bot-info")
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        return response.json().get("username")
    except Exception as exc:
        logger.warning("查詢訂閱中繼 Telegram Bot 資訊失敗：%s", exc)
        return None


def send_notification(
    http: HttpClient, base_url: str, installation_id: str, secret: str,
    channel: str, external_id: str, message: str, image_urls: list[str] | None = None,
) -> dict | None:
    """請中繼代發一則已經渲染好的通知內容給某個訂閱者。`image_urls` 可選（0～2 張）
    ——附上時中繼改走 Telegram `sendPhoto`／`sendMediaGroup`、Discord embed image
    （見 access_gate_server 的 `telegram_client.py`/`discord_client.py`），舊版中繼
    （還沒部署這次更新）會直接忽略這個欄位、退化成純文字，不會出錯。回傳
    `{"ok", "info"}`；連不上中繼時回 `None`（呼叫端視為失敗，寫進發送歷史時用通用
    錯誤訊息）。"""
    base = base_url.rstrip("/")
    payload: dict[str, object] = {"channel": channel, "external_id": external_id, "message": message}
    if image_urls:
        payload["image_urls"] = image_urls
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-BahaAD-Installation-Id": installation_id,
        "X-BahaAD-Signature": sign_payload(secret, body),
    }
    try:
        response = http.post(f"{base}/subscriber/send", data=body, headers=headers)
        if response.status_code != 200:
            raise ValueError(f"HTTP {response.status_code}")
        return response.json()
    except Exception as exc:
        logger.warning("訂閱中繼代發通知失敗：%s", exc)
        return None
