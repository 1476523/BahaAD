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
# sendPhoto 的 caption 上限是 1024 字（sendMessage 的純文字訊息是 4096）——附圖但文字
# 放不進 caption 時，寧可整則退化成純文字（不裁切內容），見 send_telegram()。
_TELEGRAM_PHOTO_CAPTION_MAX_CHARS = 1024


class HttpClient(Protocol):
    def post(self, url: str, json: dict | None = None, timeout: float | None = None) -> "_ResponseLike": ...


class _ResponseLike(Protocol):
    status_code: int

    def json(self) -> dict: ...


def is_valid_discord_webhook_url(url: str) -> bool:
    return bool(url) and bool(_DISCORD_WEBHOOK_URL_RE.match(url.strip()))


def _telegram_send_once(
    http: HttpClient, bot_token: str, chat_id: str, text: str, image_urls: list[str] | None = None
) -> tuple[bool, str]:
    # 附圖但文字放不進 caption 時退化成純文字（不裁切內容），見 send_telegram()。
    fits_caption = bool(image_urls) and len(text) <= _TELEGRAM_PHOTO_CAPTION_MAX_CHARS
    if fits_caption and len(image_urls) == 1:
        url = f"{_TELEGRAM_API_BASE}/bot{bot_token}/sendPhoto"
        payload = {"chat_id": chat_id, "photo": image_urls[0], "caption": text, "parse_mode": "HTML"}
    elif fits_caption:
        # 兩張圖（集數封面＋番劇封面）一起送——sendMediaGroup 組成一則相簿訊息，
        # caption 只掛在第一張，Telegram 會顯示成整組相簿的說明文字。
        url = f"{_TELEGRAM_API_BASE}/bot{bot_token}/sendMediaGroup"
        media = [{"type": "photo", "media": image_urls[0], "caption": text, "parse_mode": "HTML"}]
        media += [{"type": "photo", "media": u} for u in image_urls[1:]]
        payload = {"chat_id": chat_id, "media": media}
    else:
        url = f"{_TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
        # parse_mode=HTML：讓可自訂範本裡的 <b>/<i>/<code> 等 Telegram HTML 標籤真的
        # 被渲染成格式（round3 階段 5-4）。帶入的動態內容已在 render.render_message
        # (escape_html=True) 先做過 HTML 跳脫，不會被誤判成標籤。
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        response = http.post(url, json=payload, timeout=10)
        payload_resp = response.json()
    except Exception as exc:
        return False, f"Exception: {exc}"
    if payload_resp.get("ok"):
        return True, ""
    return False, f"Telegram 回應: {payload_resp.get('description', '')}"


def send_telegram(
    http: HttpClient, bot_token: str, chat_id: str, text: str, sleep=time.sleep,
    image_urls: list[str] | None = None,
) -> tuple[bool, str]:
    """回傳 `(ok, error_message)`。`sleep` 參數方便測試不用真的等待重試延遲。
    `image_urls`：範本插入了 `@episode_cover@`／`@anime_cover@` 時呼叫端會給，一張圖
    走 `sendPhoto`、兩張圖（兩個 token 都插入）走 `sendMediaGroup` 一起送；沒有就跟
    以前一樣 `sendMessage`。"""
    retry_count = 0
    while True:
        ok, error = _telegram_send_once(http, bot_token, chat_id, text, image_urls=image_urls)
        if ok or "Bad Gateway" not in error or retry_count >= _TELEGRAM_BAD_GATEWAY_MAX_RETRY:
            if not ok and retry_count >= _TELEGRAM_BAD_GATEWAY_MAX_RETRY and "Bad Gateway" in error:
                error = f"{error}（已自動重試 {_TELEGRAM_BAD_GATEWAY_MAX_RETRY} 次仍失敗）"
            return ok, error
        retry_count += 1
        sleep(_TELEGRAM_BAD_GATEWAY_RETRY_DELAY_SECONDS)


def send_discord(
    http: HttpClient, webhook_url: str, title: str, description: str, image_urls: list[str] | None = None,
) -> tuple[bool, str]:
    """`image_urls`：0、1 或 2 張圖。Discord 一個 embed 只能放一張圖，第二張圖用第二個
    只有圖片沒有文字的 embed 附加——同一則訊息可以有多個 embed，會顯示成一組畫廊。"""
    if not is_valid_discord_webhook_url(webhook_url):
        return False, "Discord Webhook URL 格式不正確，需為 https://discord.com/api/webhooks/... 開頭的網址"
    embeds = [{"title": title, "description": description, "color": _DISCORD_EMBED_COLOR}]
    if image_urls:
        embeds[0]["image"] = {"url": image_urls[0]}
        embeds += [{"image": {"url": u}} for u in image_urls[1:]]
    try:
        response = http.post(webhook_url, json={"embeds": embeds}, timeout=10)
    except Exception as exc:
        return False, f"Exception: {exc}"
    if response.status_code == 204:
        return True, ""
    return False, f"Discord 回應: HTTP {response.status_code}"
