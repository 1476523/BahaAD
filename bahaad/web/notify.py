"""通知設定／範本編輯／發送歷史。規格見 docs/requirements/notify.md「使用者可見的
部分」一節，建議實作階段第 3 階段。

憑證欄位（Telegram Bot Token／Discord Webhook URL）**從不把已存的值送回瀏覽器**——
比照 `vault.py`／`store/access_gate.py` 一貫的「絕不外洩憑證本身」原則。表單留空
代表「這次不改，沿用舊值」（比照 `access_gate_server/tray.py`「Client Secret 留空
表示沿用舊值」的既有慣例），不是「使用者想清空」——真的要清空要按專屬的「清除」
按鈕。
"""

from __future__ import annotations

from flask import Blueprint, abort, current_app, jsonify, render_template, request

from bahaad.web.responses import form_result

from bahaad import __version__
from bahaad.notify import senders
from bahaad.notify.categories import (
    CATEGORIES,
    CATEGORY_LABELS,
    CUSTOMIZABLE_CATEGORIES,
    TELEGRAM_HTML_TAGS,
)
from bahaad.notify.dispatch import notification_header
from bahaad.notify.render import render_message, to_discord_markdown, to_discord_preview_html, to_telegram_preview_html
from bahaad.notify.senders import is_valid_discord_webhook_url
from bahaad.notify.tokens import (
    PLACEHOLDER_REFERENCE,
    SAMPLE_CONTEXT,
    placeholder_reference_grouped,
)
from bahaad.store.notify import NotifyCredentials

notify_bp = Blueprint("notify", __name__)

_SETTINGS_KEY = "notify_categories"


@notify_bp.route("/notify")
def index():
    """通知中心首頁＝「通知設定」分頁（Telegram／Discord 憑證＋通知類別開關）。
    範本編輯／發送歷史是另外兩個分頁。round3 階段 5-2：這些從設定頁搬過來。"""
    deps = current_app.config["DEPS"]
    credentials = (
        deps.notify_store.get_credentials()
        if deps.notify_store is not None
        else NotifyCredentials(None, None, None)
    )
    return render_template(
        "notify_center.html",
        notify_credentials=credentials,
        notify_categories=CATEGORIES,
        notify_category_labels=CATEGORY_LABELS,
        notify_category_settings=deps.settings.get(_SETTINGS_KEY, {}),
    )


def _telegram_state(deps) -> dict:
    """通知設定頁的 Telegram 狀態摘要——存檔／清除後前端就地更新徽章＋遮罩過的
    Chat ID，不用整頁重載（使用者 2026-09-01）。**不回傳憑證本身**。"""
    cred = deps.notify_store.get_credentials()
    cid = cred.telegram_chat_id or ""
    masked = (cid[:3] + "…" + cid[-3:]) if len(cid) > 6 else cid
    return {
        "channel": "telegram",
        "enabled": bool(cred.telegram_bot_token),
        "chat_id_masked": masked,
    }


def _discord_state(deps) -> dict:
    cred = deps.notify_store.get_credentials()
    return {"channel": "discord", "enabled": bool(cred.discord_webhook_url)}


@notify_bp.route("/notify/credentials/telegram", methods=["POST"])
def save_telegram_credentials():
    deps = current_app.config["DEPS"]
    current = deps.notify_store.get_credentials()
    bot_token = (request.form.get("telegram_bot_token") or "").strip() or current.telegram_bot_token
    chat_id = (request.form.get("telegram_chat_id") or "").strip() or current.telegram_chat_id
    if not bot_token or not chat_id:
        return form_result("Bot Token 與 Chat ID 都需要填寫", endpoint="notify.index", ok=False)
    deps.notify_store.set_telegram_credentials(bot_token, chat_id)
    return form_result("Telegram 設定已儲存", endpoint="notify.index", json_extra=_telegram_state(deps))


@notify_bp.route("/notify/credentials/telegram/clear", methods=["POST"])
def clear_telegram_credentials():
    deps = current_app.config["DEPS"]
    deps.notify_store.clear_telegram_credentials()
    return form_result("已清除 Telegram 設定", endpoint="notify.index", json_extra=_telegram_state(deps))


@notify_bp.route("/notify/credentials/discord", methods=["POST"])
def save_discord_credentials():
    deps = current_app.config["DEPS"]
    current = deps.notify_store.get_credentials()
    webhook_url = (request.form.get("discord_webhook_url") or "").strip() or current.discord_webhook_url
    if not webhook_url:
        return form_result("Webhook URL 為必填", endpoint="notify.index", ok=False)
    if not is_valid_discord_webhook_url(webhook_url):
        return form_result(
            "Discord Webhook URL 格式不正確，需為 https://discord.com/api/webhooks/... 開頭的網址",
            endpoint="notify.index", ok=False,
        )
    deps.notify_store.set_discord_credentials(webhook_url)
    return form_result("Discord 設定已儲存", endpoint="notify.index", json_extra=_discord_state(deps))


@notify_bp.route("/notify/credentials/discord/clear", methods=["POST"])
def clear_discord_credentials():
    deps = current_app.config["DEPS"]
    deps.notify_store.clear_discord_credentials()
    return form_result("已清除 Discord 設定", endpoint="notify.index", json_extra=_discord_state(deps))


@notify_bp.route("/notify/categories", methods=["POST"])
def save_categories():
    deps = current_app.config["DEPS"]
    categories = {category: (request.form.get(category) == "on") for category in CATEGORIES}
    deps.settings.update({_SETTINGS_KEY: categories})
    return form_result("通知類別設定已儲存", endpoint="notify.index")


@notify_bp.route("/notify/test/<channel>", methods=["POST"])
def send_test(channel: str):
    deps = current_app.config["DEPS"]
    if channel not in ("telegram", "discord"):
        return jsonify({"ok": False, "info": "不明的管道"}), 400

    credentials = deps.notify_store.get_credentials()
    # 跟正式通知同一個標頭（round 6 第 7、8 項）：第一行 【BahaAD v<版本> 通知】
    message = f"{notification_header()}\n此為 BahaAD v{__version__} 發送的測試訊息"

    if channel == "telegram":
        if not credentials.telegram_bot_token or not credentials.telegram_chat_id:
            return jsonify({"ok": False, "info": "尚未設定 Telegram Bot Token / Chat ID"})
        ok, info = senders.send_telegram(
            deps.notify_http, credentials.telegram_bot_token, credentials.telegram_chat_id, message
        )
    else:
        if not credentials.discord_webhook_url:
            return jsonify({"ok": False, "info": "尚未設定 Discord Webhook URL"})
        ok, info = senders.send_discord(deps.notify_http, credentials.discord_webhook_url, "BahaAD 通知", message)

    return jsonify({"ok": ok, "info": info or "測試訊息已送出，請確認是否收到"})


@notify_bp.route("/notify/templates")
def templates_index():
    deps = current_app.config["DEPS"]
    all_templates = deps.notify_store.get_all_templates()
    return render_template(
        "notify_templates.html",
        categories=CUSTOMIZABLE_CATEGORIES,
        templates=all_templates,
        placeholder_reference=PLACEHOLDER_REFERENCE,
        placeholder_reference_groups=placeholder_reference_grouped(),
        notify_category_labels=CATEGORY_LABELS,
        telegram_html_tags=TELEGRAM_HTML_TAGS,
    )


@notify_bp.route("/notify/templates/<category>", methods=["POST"])
def save_template(category: str):
    if category not in CUSTOMIZABLE_CATEGORIES:
        abort(404)
    deps = current_app.config["DEPS"]
    deps.notify_store.set_template(category, request.form.get("template") or "")
    return form_result("範本已儲存", endpoint="notify.templates_index")


@notify_bp.route("/notify/templates/<category>/reset", methods=["POST"])
def reset_template(category: str):
    if category not in CUSTOMIZABLE_CATEGORIES:
        abort(404)
    deps = current_app.config["DEPS"]
    deps.notify_store.reset_template(category)
    return form_result("已還原成預設範本", endpoint="notify.templates_index")


@notify_bp.route("/notify/templates/<category>/preview", methods=["POST"])
def preview_template(category: str):
    """預覽功能：`template` 是使用者「目前輸入框裡」還沒存檔的文字，不是資料庫裡已存
    的版本，讓使用者編輯過程中隨時能看到目前寫的內容大概會長什麼樣子，不用先按儲存。
    回傳 Telegram 氣泡 HTML ＋ Discord 卡片 HTML ＋ Discord 原始 markdown（唯讀框用）。"""
    if category not in CUSTOMIZABLE_CATEGORIES:
        return jsonify({"error": "不明的類別"}), 400
    text = request.form.get("template") or ""
    return jsonify(
        {
            "telegram_html": f"{notification_header()}<br>"
            + to_telegram_preview_html(text, SAMPLE_CONTEXT).replace("\n", "<br>"),
            "discord_html": to_discord_preview_html(text, SAMPLE_CONTEXT).replace("\n", "<br>"),
            "discord_markdown": render_message(to_discord_markdown(text), **SAMPLE_CONTEXT),
        }
    )


@notify_bp.route("/notify/templates/<category>/test", methods=["POST"])
def test_template(category: str):
    """「模板測試」：拿目前**已儲存**的範本＋範例內容，實際發一次到已設定的管道。"""
    if category not in CUSTOMIZABLE_CATEGORIES:
        return jsonify({"ok": False, "info": "不明的類別"}), 400
    deps = current_app.config["DEPS"]
    credentials = deps.notify_store.get_credentials()
    if not (credentials.telegram_bot_token or credentials.discord_webhook_url):
        return jsonify({"ok": False, "info": "尚未設定任何通知管道，請先到「通知設定」設定"})

    template = deps.notify_store.get_template(category)
    header = f"{notification_header()}｜模板測試"
    results = []
    if credentials.telegram_bot_token and credentials.telegram_chat_id:
        text = f"{header}\n{render_message(template, escape_html=True, **SAMPLE_CONTEXT)}"
        ok, info = senders.send_telegram(
            deps.notify_http, credentials.telegram_bot_token, credentials.telegram_chat_id, text
        )
        results.append("Telegram：成功" if ok else f"Telegram：{info}")
    if credentials.discord_webhook_url:
        body = f"{header}\n{render_message(to_discord_markdown(template), **SAMPLE_CONTEXT)}"
        ok, info = senders.send_discord(
            deps.notify_http, credentials.discord_webhook_url, "BahaAD 通知", body
        )
        results.append("Discord：成功" if ok else f"Discord：{info}")
    return jsonify({"ok": True, "info": "；".join(results)})


_HISTORY_PAGE_SIZE = 50  # 使用者 2026-09-04：單頁 50 筆（比照日誌）


@notify_bp.route("/notify/history", methods=["GET", "POST"])
def history_index():
    deps = current_app.config["DEPS"]
    if request.method == "POST" and request.form.get("action") == "clear":
        removed = deps.notify_store.clear_history()
        return form_result(f"已清空發送歷史（{removed} 筆）", endpoint="notify.history_index")
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    total = deps.notify_store.count_history()
    history = deps.notify_store.get_history(
        limit=_HISTORY_PAGE_SIZE, offset=(page - 1) * _HISTORY_PAGE_SIZE
    )
    return render_template(
        "notify_history.html",
        history=history,
        page=page,
        total=total,
        has_next=page * _HISTORY_PAGE_SIZE < total,
    )


@notify_bp.route("/notify/history/<int:history_id>/retry", methods=["POST"])
def retry_history(history_id: int):
    deps = current_app.config["DEPS"]
    row = deps.notify_store.get_history_row(history_id)
    if row is None:
        return jsonify({"ok": False, "info": "找不到這筆歷史紀錄"}), 404

    credentials = deps.notify_store.get_credentials()
    if row["channel"] == "telegram":
        if not credentials.telegram_bot_token or not credentials.telegram_chat_id:
            return jsonify({"ok": False, "info": "尚未設定 Telegram Bot Token / Chat ID"})
        ok, info = senders.send_telegram(
            deps.notify_http, credentials.telegram_bot_token, credentials.telegram_chat_id, row["message"]
        )
    else:
        if not credentials.discord_webhook_url:
            return jsonify({"ok": False, "info": "尚未設定 Discord Webhook URL"})
        # row["message"] 已含標頭第一行（發送時就存進去了），重送不用再加
        ok, info = senders.send_discord(
            deps.notify_http, credentials.discord_webhook_url, "BahaAD 通知", row["message"]
        )

    # 成功/失敗都另外記一筆新的歷史紀錄，保留原始那筆失敗紀錄不覆寫，沿用舊專案定案
    deps.notify_store.log_history(row["channel"], row["category"], row["message"], ok, info or None)
    return jsonify({"ok": ok, "info": info or "手動重試成功"})
