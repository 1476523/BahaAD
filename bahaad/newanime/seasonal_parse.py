"""解析 `ani.gamer.com.tw/seasonal.php?c=<季別>`（新番授權情報頁）。

使用者 2026-09-29 提議：這頁比 GNN「新番節目資訊」公告更早就有內容（首頁右下角
`fab-seasonal-promotion` 浮動按鈕連過去），可以當「提早觸發」的**備選**來源——公告
真的出現後，仍然以公告的資料為準（`ingest.sync_from_gnn()` 既有的「已存在 → 用新資料
更新」邏輯，靠 `source_name` 比對，兩邊標題字面一致就會正確接上，不需要另外寫合併
邏輯）。

跟 GNN 文章比，這頁**沒有確切播出時間／星期**（只有「M月D日公開」這種揭露日期，不是
保證等於首播時間），所以這裡解析出來的 `ParsedBulletinItem` 一律
`first_air_time=None`／`first_air_weekday=None`／`is_undetermined=True`，讓後續
GNN 公告出現時能正常判定「時間有變化」並更新（`ingest.sync_from_gnn()` 看
`first_air_weekday`／`first_air_time` 是否跟既有紀錄不同）。

結構（2026-09-29 用瀏覽器實測）：`div.date-title-block`（日期分組標題，「M月D日公開」）
後面接一個 sibling `div.program-card-block`，裡面是多個 `div.program-card`（一部番一個，
`h5.title` 標題／`.company p` 製作廠商／`.range p` 授權範圍／`.tag-row .tag` 標籤／
`.intro-text` 簡介）。站方改版、解析失敗一律安靜回空清單，比照 `newanime/parse.py`
既有的容錯風格。
"""

from __future__ import annotations

import re
from datetime import date

from bs4 import BeautifulSoup

from bahaad.newanime.parse import ParsedBulletin, ParsedBulletinItem

_DATE_TITLE_RE = re.compile(r"(\d{1,2})月(\d{1,2})日公開")


def parse_seasonal_page(html: str, *, season_key: str) -> ParsedBulletin:
    """`season_key` 一定要帶（呼叫端已經知道要查哪一季，不像 GNN 文章需要從標題反推）。
    解析不到任何 `program-card` → 空 `items`（呼叫端據此判斷這一季還沒有內容，不建立
    追蹤記錄）。"""
    soup = BeautifulSoup(html, "html.parser")
    year = int(season_key[:4])
    quarter_month = int(season_key[4:6])

    items: list[ParsedBulletinItem] = []
    order = 0
    for date_block in soup.select("div.date-title-block"):
        card_block = date_block.find_next_sibling("div", class_="program-card-block")
        if card_block is None:
            continue
        first_air_date = _parse_reveal_date(date_block.get_text(), year, quarter_month)
        for card in card_block.select("div.program-card"):
            title_el = card.select_one("h5.title")
            if title_el is None:
                continue
            name = title_el.get_text(strip=True)
            if not name:
                continue
            order += 1
            items.append(
                ParsedBulletinItem(
                    source_name=name,
                    article_order=order,
                    first_air_date=first_air_date,
                    first_air_time=None,
                    first_air_weekday=None,
                    is_undetermined=True,
                    region_locked=_is_region_locked(card),
                )
            )

    return ParsedBulletin(
        title=f"{season_key} 新番授權情報", published_at=None,
        pending_note_present=True, season_key=season_key, items=tuple(items),
    )


def _parse_reveal_date(text: str, season_year: int, quarter_month: int) -> str | None:
    match = _DATE_TITLE_RE.search(text)
    if not match:
        return None
    mm, dd = int(match.group(1)), int(match.group(2))
    year = season_year
    if quarter_month == 1 and mm >= 10:
        year -= 1  # 冬番（01）頁面常見前一年 12 月就揭露，比照 newanime/parse.py 的既有規則
    try:
        return date(year, mm, dd).isoformat()
    except ValueError:
        return None


def _is_region_locked(card) -> bool:
    range_el = card.select_one(".range p")
    if range_el is None:
        return False
    return "港澳" not in range_el.get_text()
