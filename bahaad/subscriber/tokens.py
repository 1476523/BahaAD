"""訂閱通知範本 token 參考清單／預覽用範例內容。跟 `bahaad/notify/tokens.py`
（管理通知的 token）完全獨立——描述文字一律用「你」稱呼收件人，見
`bahaad/subscriber/categories.py` 開頭說明。
"""

from __future__ import annotations

PLACEHOLDER_REFERENCE: tuple[tuple[str, str, str], ...] = (
    ("@episode@", "集數", "更新通知適用，例如「第12集」"),
    ("@anime_link@", "番劇連結", "更新通知適用；連到這部番劇的詳細頁"),
    (
        "@episode_cover@", "這一集自己的封面圖片",
        "更新通知適用；有插入才會真的附圖，插入後訊息文字裡看不到網址本身",
    ),
    (
        "@anime_cover@", "整部番劇的封面圖片",
        "更新通知適用；跟 @episode_cover@ 都插入時兩張圖一起附上",
    ),
    ("@announcement_text@", "公告原文", "官方公告類（暫停/延後/同時加更/暫時下架）適用"),
    ("@announcement_time@", "偵測到公告的時間", "官方公告類適用"),
    ("@newanime_message@", "組好的新番異動整句", "新番快訊（新增新番／更改時間／新番移除）適用"),
    ("@digest_date@", "本次每日新番通知的日期", "每日新番通知適用"),
    ("@today_schedule_count@", "你追蹤的番劇中，今日排定更新的部數", "每日新番通知適用"),
    (
        "@today_schedule_list@",
        "你追蹤的番劇中，今日排定更新清單（每行一部，含時間）",
        "每日新番通知適用，沒有項目時顯示「你追蹤的番劇今日沒有排定更新」",
    ),
    ("@anime_title@", "番劇名稱", "除了每日新番通知（一次可能涉及多部）以外都適用"),
    ("@subscriber_mention@", "提及（標註）你自己", "Discord 會 @ 你、Telegram 會附上你的顯示名稱連結；所有類別皆可用"),
    ("@finish_time@", "通知送出的時間", "所有類別皆可用，發送時自動帶入"),
)

# 兩欄排版（使用者 2026-09-12）：前三組排左欄、後兩組排右欄，跟範本編輯區的
# 左右分類對齊，見 notify_subscriber_template.html。
_TOKEN_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("更新通知", ("@episode@", "@anime_link@", "@episode_cover@", "@anime_cover@")),
    ("公告通知", ("@announcement_text@", "@announcement_time@")),
    ("新番快訊", ("@newanime_message@",)),
    ("每日新番通知", ("@digest_date@", "@today_schedule_count@", "@today_schedule_list@")),
    ("共用（多個類別）", ("@anime_title@", "@subscriber_mention@", "@finish_time@")),
)
TOKEN_GROUPS_LEFT_COUNT = 3


def placeholder_reference_grouped() -> list[tuple[str, list[tuple[str, str, str]]]]:
    by_token = {row[0]: row for row in PLACEHOLDER_REFERENCE}
    seen: set[str] = set()
    groups: list[tuple[str, list[tuple[str, str, str]]]] = []
    for label, tokens in _TOKEN_GROUPS:
        rows = [by_token[t] for t in tokens if t in by_token]
        seen.update(t for t in tokens if t in by_token)
        if rows:
            groups.append((label, rows))
    leftover = [row for row in PLACEHOLDER_REFERENCE if row[0] not in seen]
    if leftover:
        groups.append(("其他", leftover))
    return groups


# 「模擬預覽」用的範例內容：涵蓋所有 token，不管範本用到哪些都能替換出看起來合理的結果。
SAMPLE_CONTEXT: dict[str, str] = {
    "anime_title": "葬送的芙莉蓮",
    "episode": "第12集",
    "anime_link": "https://example.com/anime/100",
    # 預覽用提示文字——實際發送時這兩個 token 在訊息文字裡會換成空字串，圖片是走
    # 真正的附圖 API（Discord embed／Telegram sendPhoto），不是把網址貼進內文，
    # 這裡用括號提示使用者「這裡會有一張圖」。
    "episode_cover": "（這一集的封面圖片）",
    "anime_cover": "（番劇封面圖片）",
    "announcement_text": "因版權疑慮，本片將暫停更新，恢復時間另行公告",
    "announcement_time": "2026-07-29 18:00:00",
    "newanime_message": "新增新番 葬送的芙莉蓮 第二季 集數將於 20:30 更新",
    "digest_date": "2026-07-30",
    "today_schedule_count": "2",
    "today_schedule_list": "- 葬送的芙莉蓮 第28集 20:30\n- 鬼滅之刃 第11集 22:00",
    "subscriber_mention": "@你",
    "finish_time": "2026-07-29 20:30:11",
}
