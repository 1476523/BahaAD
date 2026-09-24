"""訂閱通知（給訂閱者的 Discord/Telegram 通知）的類別定義／預設範本。

跟 `bahaad/notify/categories.py`（給 BahaAD 擁有者自己的管理通知）是完全獨立的
兩套類別/範本——收件人不同、口吻也不同：這裡的範本一律用「你」稱呼收件人（訂閱者
自己），不是管理通知那種對擁有者講話的語氣。共用的只有 Telegram HTML 標籤子集
（`TELEGRAM_HTML_TAGS`，直接從 `notify/categories.py` 匯入，不重複定義）。

使用者 2026-09-12 定案的十個類別：更新通知（既有，唯一目前有實際推播邏輯的類別，
見 `bahaad/subscriber/dispatch.py`）＋四種公告類別＋每日新番通知＋番劇完結＋三種
新番快訊。後五種（公告／每日新番通知／番劇完結／新番快訊）目前**只有範本可以編輯
與預覽**，還沒有接上實際的推播判斷邏輯（那需要在公告偵測／每日彙整／新番快訊各自
的既有觸發點，另外接一段「篩出有追蹤的訂閱者」邏輯，是下一輪要做的事）——不要
因為範本頁上看得到就以為訂閱者現在真的會收到這些通知。
"""

from __future__ import annotations

from bahaad.notify.categories import TELEGRAM_HTML_TAGS  # noqa: F401  (範本編輯頁工具列共用)

CATEGORIES: tuple[str, ...] = (
    "episode_update",
    "gossip_pause",
    "gossip_delay",
    "gossip_extra_episode",
    "gossip_takedown",
    "daily_digest",
    "anime_completed",
    "newanime_added",
    "newanime_time_changed",
    "newanime_removed",
)

CATEGORY_LABELS: dict[str, str] = {
    "episode_update": "更新通知",
    "gossip_pause": "官方公告：暫停更新",
    "gossip_delay": "官方公告：延後更新",
    "gossip_extra_episode": "官方公告：同時/加更",
    "gossip_takedown": "官方公告：暫時下架",
    "daily_digest": "每日新番通知",
    "anime_completed": "番劇完結",
    "newanime_added": "新番快訊：新增新番",
    "newanime_time_changed": "新番快訊：更改時間",
    "newanime_removed": "新番快訊：新番移除",
}

# 範本編輯頁兩欄排版（使用者 2026-09-12）。
COLUMN_LEFT: tuple[str, ...] = (
    "episode_update",
    "gossip_pause",
    "gossip_delay",
    "gossip_extra_episode",
    "gossip_takedown",
)
COLUMN_RIGHT: tuple[str, ...] = (
    "daily_digest",
    "anime_completed",
    "newanime_added",
    "newanime_time_changed",
    "newanime_removed",
)

# 預設範本——一律用「你」稱呼收件人（訂閱者本人）。
DEFAULT_TEMPLATES: dict[str, str] = {
    "episode_update": "《@anime_title@》更新了 @episode@\n@anime_link@",
    "gossip_pause": "你追蹤的《@anime_title@》本週停止更新\n@announcement_text@",
    "gossip_delay": "你追蹤的《@anime_title@》延後更新\n@announcement_text@",
    "gossip_extra_episode": "你追蹤的《@anime_title@》臨時加更\n@announcement_text@",
    "gossip_takedown": "你追蹤的《@anime_title@》暫時下架\n@announcement_text@",
    "daily_digest": (
        "<b>@digest_date@ 每日新番通知</b>\n\n"
        "你追蹤的番劇中，今日排定更新共 @today_schedule_count@ 部：\n"
        "<blockquote expandable>@today_schedule_list@</blockquote>"
    ),
    "anime_completed": "你追蹤的《@anime_title@》可能已經完結了\n（已連續一週沒出現在動畫瘋每日新番時刻表上）",
    "newanime_added": "你關注的新番快訊有更新：@newanime_message@",
    "newanime_time_changed": "你關注的新番快訊有更新：@newanime_message@",
    "newanime_removed": "你關注的新番快訊有更新：@newanime_message@",
}
