"""九個通知類別的定義／預設範本。規格見 docs/requirements/notify.md「通知類別」
「範本 token」兩節。刻意精簡自舊專案的 14 類——只選有明確 BahaAD 既有訊號可以掛
的類別，其餘（`system_cookie_invalid`／`system_device_error`／`system_path_
unreachable`）目前沒有對應訊號，不是遺漏。

`CATEGORIES` 是九個類別 id 的完整清單（含 `system_new_version`）；`CUSTOMIZABLE_
CATEGORIES` 少一個 `system_new_version`——那則通知內容是系統產生的版本資訊，刻意
不開放自訂範本（被使用者範本改壞的風險比讓它可自訂更值得避免，沿用舊專案定案），
`store/notify.py` 的 `notify_templates` 表只需要儲存這八個。
"""

from __future__ import annotations

CATEGORIES: tuple[str, ...] = (
    "download_success",
    "download_failed",
    "gossip_pause",
    "gossip_delay",
    "gossip_extra_episode",
    "gossip_takedown",
    "gossip_other",
    "anime_completed",
    "newanime_full_list",
    "newanime_added",
    "newanime_time_changed",
    "newanime_removed",
    "system_new_version",
    "system_daily_digest",
)

# `newanime_full_list` 跟 `system_new_version` 一樣不開放自訂範本——內容是「按星期分組
# ＋每組可折疊引用」的固定結構，被使用者範本改壞的風險比讓它可自訂更值得避免
# （見 new_anime_bulletin.md §8c、notify/dispatch.py 的特別處理）。
_NON_CUSTOMIZABLE = {"system_new_version", "newanime_full_list"}
CUSTOMIZABLE_CATEGORIES: tuple[str, ...] = tuple(c for c in CATEGORIES if c not in _NON_CUSTOMIZABLE)

# 設定頁類別開關／範本編輯頁顯示用的中文標籤
CATEGORY_LABELS: dict[str, str] = {
    "download_success": "下載完成",
    "download_failed": "下載失敗",
    "gossip_pause": "官方公告：暫停更新",
    "gossip_delay": "官方公告：延後更新",
    "gossip_extra_episode": "官方公告：同時/加更",
    "gossip_takedown": "官方公告：暫時下架",
    "gossip_other": "官方公告：其他",
    "anime_completed": "番劇完結",
    "newanime_full_list": "新番快訊：完整列表",
    "newanime_added": "新番快訊：新增新番",
    "newanime_time_changed": "新番快訊：更改時間",
    "newanime_removed": "新番快訊：新番移除",
    "system_new_version": "有新版本可用",
    "system_daily_digest": "每日新番彙整通知",
}

# 通知標題行的後綴，組出「【BahaAD OOO】」這行標題，跟舊專案 NOTIFY_CATEGORY_TITLE_SUFFIX 對應
TITLE_SUFFIX: dict[str, str] = {
    "download_success": "消息",
    "download_failed": "消息",
    "gossip_pause": "公告通知",
    "gossip_delay": "公告通知",
    "gossip_extra_episode": "公告通知",
    "gossip_takedown": "公告通知",
    "gossip_other": "公告通知",
    "anime_completed": "消息",
    "newanime_full_list": "新番快訊",
    "newanime_added": "新番快訊",
    "newanime_time_changed": "新番快訊",
    "newanime_removed": "新番快訊",
    "system_new_version": "系統通知",
    "system_daily_digest": "系統通知",
}

# Telegram Bot 支援的 HTML 標籤子集（sendMessage 搭配 parse_mode=HTML）——範本編輯頁的
# 格式工具列／說明用。(插入用範本, 功能, 說明)。轉 Discord markdown 見 notify/render.py。
TELEGRAM_HTML_TAGS: tuple[tuple[str, str, str], ...] = (
    ("<b>文字</b>", "粗體", "Discord → **文字**"),
    ("<i>文字</i>", "斜體", "Discord → *文字*"),
    ("<u>文字</u>", "底線", "Discord → __文字__"),
    ("<s>文字</s>", "刪除線", "Discord → ~~文字~~"),
    ("<code>文字</code>", "單行程式碼", "適合短片段（指令／變數名）"),
    ("<pre>文字</pre>", "多行程式碼區塊", "保留換行與空白"),
    ("<blockquote>文字</blockquote>", "引用", "整段內縮"),
    ("<blockquote expandable>文字</blockquote>", "可摺疊引用", "Telegram 專屬；Discord 退化成一般引用"),
    ('<a href="網址">文字</a>', "超連結", "把「網址」換成實際連結"),
    ("<tg-spoiler>文字</tg-spoiler>", "防劇透", "Telegram 點擊後才顯示；Discord → ||文字||"),
)

# 預設範本文字，唯一的預設值來源：建表 seed 與「還原預設值」都讀這裡。v1.1 起支援
# Telegram HTML 標籤子集（見 notify/render.py），預設範本用一點 <b> 讓工具列有東西可玩。
DEFAULT_TEMPLATES: dict[str, str] = {
    "download_success": "<b>@animation_name@</b> @episode@\n下載完成 @file_size@ MB",
    "download_failed": "<b>@animation_name@</b> @episode@\n下載失敗: @fail_reason@",
    "gossip_pause": "《<b>@animation_name@</b>》本週停止更新\n@announcement_text@",
    "gossip_delay": "《<b>@animation_name@</b>》延後更新\n@announcement_text@",
    "gossip_extra_episode": "《<b>@animation_name@</b>》臨時加更\n@announcement_text@",
    "gossip_takedown": "《<b>@animation_name@</b>》暫時下架\n@announcement_text@",
    "gossip_other": "《<b>@animation_name@</b>》其他公告\n@announcement_text@",
    "anime_completed": "《<b>@animation_name@</b>》可能已經完結了\n（已連續一週沒出現在動畫瘋每日新番時刻表上）",
    # 新番快訊的個別異動（完整列表類別關掉時才用）。`@newanime_message@` 是照 §8d 組好的
    # 整句；也可以自己用 `@animation_name@` / `@newanime_time@` 等 token 重寫。
    "newanime_added": "@newanime_message@",
    "newanime_time_changed": "@newanime_message@",
    "newanime_removed": "@newanime_message@",
    # 使用者 2026-09-05：希望比照舊版 aniGamerPlus 的「引用摺疊」設計——兩個區塊各自用
    # 可摺疊引用包起來，訊息預設收合、點開才展開清單，長串排程不會把整則訊息撐得很長。
    "system_daily_digest": (
        "<b>@digest_date@ 每日新番通知</b>@digest_update_note@\n\n"
        "本日排程更新，共 @today_schedule_count@ 部：\n"
        "<blockquote expandable>@today_schedule_list@</blockquote>\n\n"
        "機動調整異動，共 @dynamic_adjustment_count@ 部：\n"
        "<blockquote expandable>@dynamic_adjustment_list@</blockquote>"
    ),
}

# system_new_version 的固定格式，刻意不放進 DEFAULT_TEMPLATES/CUSTOMIZABLE_CATEGORIES——
# 這則通知內容（GitHub Release 版本資訊）完全是系統產生、使用者沒有自行填寫的欄位可
# 自訂，開放範本只會徒增「內容被改壞導致看不出真正版本資訊」的風險，見 notify.md
# 「範本 token」一節。`notify/dispatch.py` 對這個類別特別處理，不走 store 的可自訂
# 範本查詢路徑。
SYSTEM_NEW_VERSION_TEMPLATE = "發現 GitHub 上有新版本: @version_tag@\n更新內容:\n@version_body@"
