"""範本 token 參考清單。規格見 docs/requirements/notify.md「範本 token」一節——純參
考用資料，供之後的範本編輯頁（Phase 8 建議實作階段第 3 階段）顯示說明表，以及「預覽」
功能的範例內容，使用者不能編輯這份清單本身。
"""

from __future__ import annotations

# (token, 說明, 適用類別/備註)——跟舊專案 PLACEHOLDER_REFERENCE 同樣的用途，這裡不分
# 三張表，單一清單即可
PLACEHOLDER_REFERENCE: tuple[tuple[str, str, str], ...] = (
    ("@animation_name@", "番劇名稱", "下載通知、公告通知、番劇完結通知適用；系統通知沒有對應的番劇，不適用"),
    ("@episode@", "集數", "下載通知適用，例如「第12集」（數字依「補齊長度」補零；番劇內特別篇為「特別篇 N」）"),
    ("@file_size@", "檔案大小（MB）", "下載完成通知適用"),
    ("@fail_reason@", "下載失敗原因", "下載失敗通知適用"),
    ("@download_time@", "下載完成時間", "下載完成／失敗通知適用；完整日期時間，例如「2026-07-29 20:30:05」。比 @finish_time@ 早幾秒"),
    ("@resolution@", "影片解析度（720p、1080p…）", "下載完成通知適用"),
    ("@fps@", "影格率（24、23.98…）", "下載完成通知適用"),
    ("@vcodec@", "影像編碼器（H.264、H.265…）", "下載完成通知適用"),
    ("@acodec@", "音訊編碼器（AAC、AC-3…）", "下載完成通知適用"),
    ("@container@", "檔案格式（mp4）", "下載完成通知適用"),
    ("@mux_date@", "壓縮完畢的日期", "下載完成通知適用；＝ @download_time@ 的日期部分"),
    ("@mux_time@", "壓縮完畢的時間", "下載完成通知適用；時:分:秒，＝ @download_time@ 的時間部分"),
    ("@announcement_text@", "公告原文", "公告通知適用"),
    ("@announcement_time@", "偵測到公告的時間", "公告通知適用"),
    ("@postpone_time@", "延後後的新排程時間", "只有延後更新通知在有解析出新時間時才有值"),
    ("@version_tag@", "GitHub Release 版本標籤", "「新版本可用」系統通知固定格式使用，不開放自訂範本"),
    ("@version_body@", "GitHub Release 內容", "同上"),
    ("@digest_date@", "本次每日新番通知彙整的日期", "每日新番通知適用"),
    ("@today_schedule_count@", "本日排程更新的部數", "每日新番通知適用"),
    ("@today_schedule_list@", "本日排程更新清單（每行一部，含時間）", "每日新番通知適用，沒有排定項目時顯示「今日無排定更新」"),
    ("@dynamic_adjustment_count@", "機動調整生效中的部數", "每日新番通知適用，需開啟官方公告的機動調整功能才會有內容"),
    ("@dynamic_adjustment_list@", "機動調整清單（每行一部，含暫停/延後等異動說明）", "同上，沒有異動時顯示「今日無機動調整異動」"),
    ("@digest_update_note@", "本次是否為更新版的提示文字", "每日新番通知適用，首次發送時為空字串"),
    ("@newanime_message@", "組好的新番異動整句", "新番快訊「新增新番／更改時間／新番移除」適用（見設定頁說明）"),
    ("@newanime_time@", "新番的預計更新時間", "新番快訊「新增新番」適用，待定時為「待定」"),
    ("@newanime_old_time@", "新番原訂的更新時間", "新番快訊「更改時間」適用"),
    ("@newanime_new_time@", "新番變更後的更新時間", "新番快訊「更改時間」適用，待定時為「待定」"),
    ("@finish_time@", "通知送出的時間", "所有類別皆可用，發送時自動帶入。比 @download_time@（壓縮完成時間）晚幾秒"),
)

# 範本 token 參考頁「依類別收納」用（使用者 2026-09-05：不要整條攤成一張大表）。
# 每個 token 掛在它「主要」屬於的類別；跨多類別的 token（例如番劇名稱）放「共用」。
# 顯示順序＝這裡的順序。
_TOKEN_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("下載通知", (
        "@episode@", "@file_size@", "@fail_reason@", "@download_time@",
        "@resolution@", "@fps@", "@vcodec@", "@acodec@", "@container@",
        "@mux_date@", "@mux_time@",
    )),
    ("公告通知", ("@announcement_text@", "@announcement_time@", "@postpone_time@")),
    ("每日新番彙整", (
        "@digest_date@", "@today_schedule_count@", "@today_schedule_list@",
        "@dynamic_adjustment_count@", "@dynamic_adjustment_list@", "@digest_update_note@",
    )),
    ("新番快訊", (
        "@newanime_message@", "@newanime_time@", "@newanime_old_time@", "@newanime_new_time@",
    )),
    ("系統通知（新版本可用，固定格式）", ("@version_tag@", "@version_body@")),
    ("共用（多個類別）", ("@animation_name@", "@finish_time@")),
)


def placeholder_reference_grouped() -> list[tuple[str, list[tuple[str, str, str]]]]:
    """`[(類別標題, [(token, 說明, 適用), ...]), ...]`——範本 token 參考頁用。
    `_TOKEN_GROUPS` 沒列到的 token 落到最後的「其他」組（新增 token 忘了分類時不會消失）。"""
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


# 「預覽」功能用的範例內容：涵蓋所有 token，不管範本用到哪些都能替換出看起來合理的結果
SAMPLE_CONTEXT: dict[str, str] = {
    "animation_name": "葬送的芙莉蓮",
    "episode": "第12集",
    "file_size": "350",
    "fail_reason": "網路連線逾時",
    "download_time": "2026-07-29 20:30:05",
    "resolution": "1080p",
    "fps": "24",
    "vcodec": "H.264",
    "acodec": "AAC",
    "container": "mp4",
    "mux_date": "2026-07-29",
    "mux_time": "20:30:05",
    "announcement_text": "因版權疑慮，本片將暫停更新，恢復時間另行公告",
    "announcement_time": "2026-07-29 18:00:00",
    "postpone_time": "2026-08-05 20:30",
    "version_tag": "v0.3.0",
    "version_body": "修正下載失敗時的重試邏輯",
    "digest_date": "2026-07-30",
    "digest_update_note": "（機動調整異動更新，重新發送）",
    "today_schedule_count": "4",
    "today_schedule_list": "- 葬送的芙莉蓮 第28集 20:30\n- 進擊的巨人 第09集 21:00\n- 鬼滅之刃 第11集 22:00\n- 咒術迴戰 第05集 23:00",
    "dynamic_adjustment_count": "2",
    "dynamic_adjustment_list": "- 葬送的芙莉蓮 本週暫停更新\n- 咒術迴戰 本週延後更新，預計播出第06集",
    "newanime_message": "新增新番 葬送的芙莉蓮 第二季 集數將於 20:30 更新",
    "newanime_time": "20:30",
    "newanime_old_time": "20:00",
    "newanime_new_time": "22:30",
    "finish_time": "2026-07-29 20:30:11",
}
