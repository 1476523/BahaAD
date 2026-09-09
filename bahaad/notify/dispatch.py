"""發送入口。規格見 docs/requirements/notify.md「發送流程」一節。

`send_notification()` 是唯一對外入口，供 `scheduler/main_loop.py`（下載完成/失敗）、
`scheduler/version_check.py`（新版本可用）、`scheduler/gossip_watch.py`（公告/每日
彙整，Phase 8 後續階段）等各處共用，避免同樣的「類別開關/憑證檢查/組字串/寫歷史」
邏輯散落多處重複維護。
"""

from __future__ import annotations

import logging
from datetime import datetime

from bahaad import __version__
from bahaad.notify import senders
from bahaad.notify.categories import (
    CUSTOMIZABLE_CATEGORIES,
    DEFAULT_TEMPLATES,
    SYSTEM_NEW_VERSION_TEMPLATE,
)
from bahaad.notify.render import render_message, to_discord_markdown
from bahaad.notify.senders import HttpClient
from bahaad.store.notify import NotifyStore
from bahaad.store.settings import SettingsStore

logger = logging.getLogger(__name__)

_SETTINGS_KEY = "notify_categories"


def notification_header() -> str:
    """所有通知（每個類別、Telegram 與 Discord 都一樣）的固定第一行
    （round 6 第 7、8 項）。"""
    return f"【BahaAD v{__version__} 通知】"


def _category_enabled(settings: SettingsStore, category: str) -> bool:
    categories = settings.get(_SETTINGS_KEY, {})
    return categories.get(category, True)


def category_enabled(settings: SettingsStore, category: str) -> bool:
    """給 `notify/` 以外的呼叫端（新番快訊要先知道「完整列表」類別開沒開才能決定
    發完整列表還是逐則發）用的公開包裝。"""
    return _category_enabled(settings, category)


def _template_for(store: NotifyStore, category: str) -> str:
    if category in CUSTOMIZABLE_CATEGORIES:
        return store.get_template(category)
    if category == "system_new_version":
        return SYSTEM_NEW_VERSION_TEMPLATE
    return DEFAULT_TEMPLATES.get(category, "")


def send_notification(
    store: NotifyStore,
    settings: SettingsStore,
    http: HttpClient,
    category: str,
    *,
    force: bool = False,
    **context: object,
) -> bool:
    """類別關閉時直接跳過，兩個管道都不送、不寫入歷史（使用者主動關掉的類別，連
    「沒送」這件事都不需要留紀錄）。管道沒有設定憑證的同樣直接跳過、不算失敗、
    不寫歷史——兩個管道都可選，可以只設一個，見 notify.md「發送流程」「邊界案例」。

    `force=True`：略過類別開關（新番快訊「首次掃描」一定發完整列表，即使使用者關掉
    了 `newanime_full_list` 類別，見 new_anime_bulletin.md §8b）。

    回傳是否真的過了「類別開著＋至少一個管道有憑證」這兩關、有嘗試發送——**不是**
    「有沒有送成功」（每個管道各自的成功/失敗已經個別寫進 `store.log_history`）。
    這個回傳值只給呼叫端做「今天已經送過（嘗試過）」這種去重判斷用（見
    `gossip_watch.py` 每日彙整——使用者 2026-09-05 回報：重置後、通知憑證都還沒
    設定好時送的那一次不該被當成「今天已經送過」，不然之後補上憑證也不會再送）。
    """
    if not force and not _category_enabled(settings, category):
        return False

    credentials = store.get_credentials()
    if credentials.telegram_bot_token is None and credentials.discord_webhook_url is None:
        return False

    # `@finish_time@`＝這則通知**送出當下**的時間，任何類別都能用（「預覽」的範例內容
    # 一定有這個 token，實際發送卻沒帶就會原樣印出 `@finish_time@`——使用者 2026-09-04
    # 回報）。呼叫端自己有更精確的時間就自己帶（gossip/彙整），沒帶就用現在時間補。
    context.setdefault("finish_time", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    header = notification_header()

    # 新番快訊「完整列表」：內容是「按星期分組＋每組可折疊引用」的固定 HTML（呼叫端
    # 用 `full_list_html` 帶進來，本身就是安全的 Telegram HTML），不走 render_message
    # 的 token 跳脫路徑——不然 `<blockquote>` 會被跳脫成純文字（見 new_anime_bulletin.md §8c）。
    if category == "newanime_full_list":
        _send_prebuilt_html(store, http, credentials, category, header, str(context.get("full_list_html", "")))
        return True

    template = _template_for(store, category)

    if credentials.telegram_bot_token and credentials.telegram_chat_id:
        # Telegram 走 parse_mode=HTML：帶入值先跳脫，範本自己的標籤保留（round3 階段 5-4）
        text = f"{header}\n{render_message(template, escape_html=True, **context)}"
        ok, error = senders.send_telegram(http, credentials.telegram_bot_token, credentials.telegram_chat_id, text)
        store.log_history("telegram", category, text, ok, error or None)

    if credentials.discord_webhook_url:
        # Discord 沒有 HTML：先把標籤轉 markdown，再替換 token（帶入值不跳脫）。
        # 標頭放進內文第一行（不只 embed 標題），跟 Telegram 完全一致（round 6 第 7 項）
        body = f"{header}\n{render_message(to_discord_markdown(template), **context)}"
        ok, error = senders.send_discord(http, credentials.discord_webhook_url, "BahaAD 通知", body)
        store.log_history("discord", category, body, ok, error or None)

    return True


def _send_prebuilt_html(store, http, credentials, category, header, telegram_html: str) -> None:
    """已經組好的 Telegram HTML 直接送（不跳脫）；Discord 版把 `<blockquote>`/`<b>` 轉 markdown。"""
    if credentials.telegram_bot_token and credentials.telegram_chat_id:
        text = f"{header}\n{telegram_html}"
        ok, error = senders.send_telegram(
            http, credentials.telegram_bot_token, credentials.telegram_chat_id, text
        )
        store.log_history("telegram", category, text, ok, error or None)
    if credentials.discord_webhook_url:
        body = f"{header}\n{to_discord_markdown(telegram_html)}"
        ok, error = senders.send_discord(http, credentials.discord_webhook_url, "BahaAD 通知", body)
        store.log_history("discord", category, body, ok, error or None)
