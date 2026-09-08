"""Telegram／Discord 實際發送。規格見 docs/requirements/notify.md「發送實作」一節。

跟專案其他對外 HTTP 呼叫（`access_gate/oauth.py`）一樣的 `HttpClient` Protocol 依賴
注入慣例，不直接 import `requests`/`curl_cffi`，方便測試用假物件。
"""

from __future__ import annotations

import re
import time
from typing import Protocol

_TELEGRAM_API_BASE = "https://api.telegram.org"
# Bad Gateway 是 Telegram 伺服器端暫時性問題（502），稍等幾秒重試通常就會成功；其他
# 失敗原因（Token/聊天室ID設定錯誤、訊息格式錯誤等）重試沒有意義，只會延遲使用者
# 發現設定錯誤的時間——沿用舊專案已經驗證過的行為，見 notify.md「發送實作」一節
_TELEGRAM_BAD_GATEWAY_MAX_RETRY = 3
_TELEGRAM_BAD_GATEWAY_RETRY_DELAY_SECONDS = 3.0

# 白名單格式檢查：避免使用者（或惡意輸入）把這個值填成內網/雲端 metadata 網址，讓
# 伺服器代為對任意主機發出請求（SSRF）——沿用舊專案已經修過的真實風險
_DISCORD_WEBHOOK_URL_RE = re.compile(r"^https://(discord\.com|discordapp\.com)/api/webhooks/\d+/[\w-]+/?$")
_DISCORD_EMBED_COLOR = 5814783


class HttpClient(Protocol):
    def post(self, url: str, json: dict | None = None, timeout: float | None = None) -> "_ResponseLike": ...


class _ResponseLike(Protocol):
    status_code: int

    def json(self) -> dict: ...


def is_valid_discord_webhook_url(url: str) -> bool:
    return bool(url) and bool(_DISCORD_WEBHOOK_URL_RE.match(url.strip()))


def _telegram_send_once(http: HttpClient, bot_token: str, chat_id: str, text: str) -> tuple[bool, str]:
    try:
        response = http.post(
            f"{_TELEGRAM_API_BASE}/bot{bot_token}/sendMessage",
            # parse_mode=HTML：讓可自訂範本裡的 <b>/<i>/<code> 等 Telegram HTML 標籤真的
            # 被渲染成格式（round3 階段 5-4）。帶入的動態內容已在 render.render_message
            # (escape_html=True) 先做過 HTML 跳脫，不會被誤判成標籤。
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        payload = response.json()
    except Exception as exc:
        return False, f"Exception: {exc}"
    if payload.get("ok"):
        return True, ""
    return False, f"Telegram 回應: {payload.get('description', '')}"


def send_telegram(
    http: HttpClient, bot_token: str, chat_id: str, text: str, sleep=time.sleep
) -> tuple[bool, str]:
    """回傳 `(ok, error_message)`。`sleep` 參數方便測試不用真的等待重試延遲。"""
    retry_count = 0
    while True:
        ok, error = _telegram_send_once(http, bot_token, chat_id, text)
        if ok or "Bad Gateway" not in error or retry_count >= _TELEGRAM_BAD_GATEWAY_MAX_RETRY:
            if not ok and retry_count >= _TELEGRAM_BAD_GATEWAY_MAX_RETRY and "Bad Gateway" in error:
                error = f"{error}（已自動重試 {_TELEGRAM_BAD_GATEWAY_MAX_RETRY} 次仍失敗）"
            return ok, error
        retry_count += 1
        sleep(_TELEGRAM_BAD_GATEWAY_RETRY_DELAY_SECONDS)


def send_discord(http: HttpClient, webhook_url: str, title: str, description: str) -> tuple[bool, str]:
    if not is_valid_discord_webhook_url(webhook_url):
        return False, "Discord Webhook URL 格式不正確，需為 https://discord.com/api/webhooks/... 開頭的網址"
    try:
        response = http.post(
            webhook_url,
            json={"embeds": [{"title": title, "description": description, "color": _DISCORD_EMBED_COLOR}]},
            timeout=10,
        )
    except Exception as exc:
        return False, f"Exception: {exc}"
    if response.status_code == 204:
        return True, ""
    return False, f"Discord 回應: HTTP {response.status_code}"
