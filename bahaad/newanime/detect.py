"""從首頁公告偵測「新番節目資訊」＋把「X月／季」解析成季度代碼。

規格 docs/requirements/new_anime_bulletin.md §3。純函式，好單元測試。
"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable

# 公告裡的 GNN 新聞連結：`gnn.gamer.com.tw/detail.php?sn=<數字>`（有沒有 scheme、`www.`
# 前綴都接受）。sn 是 GNN 文章編號。
_GNN_DETAIL_RE = re.compile(
    r"(?:https?:)?(?://)?(?:www\.)?gnn\.gamer\.com\.tw/detail\.php\?sn=(\d+)",
    re.IGNORECASE,
)

# 除了連結，公告文字還要像「新番節目資訊」才算數（GNN 連結也會出現在別種新聞公告裡）。
_KEYWORD_PRIMARY = "新番"
_KEYWORD_SECONDARY = ("節目資訊", "新番表", "新番節目", "節目表")

# 「X月」→ 季度代碼；月份對季度：1-3 冬(01)、4-6 春(04)、7-9 夏(07)、10-12 秋(10)。
_MONTH_TO_SEASON = {
    1: "01", 2: "01", 3: "01",
    4: "04", 5: "04", 6: "04",
    7: "07", 8: "07", 9: "07",
    10: "10", 11: "10", 12: "10",
}
_SEASON_WORD_TO_KEY = {"冬": "01", "春": "04", "夏": "07", "秋": "10"}

_MONTH_RE = re.compile(r"(?<!\d)(1[0-2]|[1-9])\s*月")
_SEASON_WORD_RE = re.compile(r"([冬春夏秋])季")
_YEAR_RE = re.compile(r"(20\d{2})\s*年?")


def is_bulletin_text(text: str) -> bool:
    """公告文字像不像「新番節目資訊」（含「新番」＋節目資訊／新番表／新番節目／節目表之一）。
    不看連結——給 `gossip_watch` 把這類公告歸成「新番快訊」、不要落到「無法判斷」用
    （使用者 2026-10-05）。"""
    text = text or ""
    return _KEYWORD_PRIMARY in text and any(kw in text for kw in _KEYWORD_SECONDARY)


def detect_bulletin_gnn_sn(text: str, links: Iterable[str]) -> int | None:
    """公告文字 + 連結清單 → 這是不是「新番節目資訊」公告；是的話回 GNN 文章 sn，否則 None。

    判斷：**連結（或文字）裡有 `gnn.gamer.com.tw/detail.php?sn=`** 且 **文字含「新番」+
    （「節目資訊」／「新番表」／「新番節目」／「節目表」之一）**。
    """
    text = text or ""
    if not is_bulletin_text(text):
        return None
    for candidate in (*links, text):
        match = _GNN_DETAIL_RE.search(candidate or "")
        if match:
            return int(match.group(1))
    return None


def parse_season_key(source: str, *, fallback_year: int) -> str | None:
    """把「7月新番」「2026 夏季」這類字串解析成 `YYYYQQ`（例 `202607`）。

    - 季度：先看「X月」，沒有再看「冬/春/夏/秋季」。都沒有 → None。
    - 年份：字串裡有「20XX」就用它，否則用 `fallback_year`（通常是文章發佈年）。
    """
    source = source or ""
    season_code: str | None = None

    month_match = _MONTH_RE.search(source)
    if month_match:
        season_code = _MONTH_TO_SEASON[int(month_match.group(1))]
    else:
        word_match = _SEASON_WORD_RE.search(source)
        if word_match:
            season_code = _SEASON_WORD_TO_KEY[word_match.group(1)]
    if season_code is None:
        return None

    year_match = _YEAR_RE.search(source)
    year = int(year_match.group(1)) if year_match else int(fallback_year)
    return f"{year}{season_code}"


# 使用者 2026-09-29 提議：seasonal.php（新番授權情報頁）比 GNN「新番節目資訊」公告更早
# 就有內容，可以當提早觸發的備選來源（公告出現後仍以公告為準，見
# `newanime/seasonal_parse.py`）。這裡是「日期 → 季度代碼」方向的純函式，給
# `NewAnimeWatcher` 算「現在／下一季的 season_key 是什麼」用，跟上面「標題文字 → 季度
# 代碼」的 `parse_season_key` 是同一份 `_MONTH_TO_SEASON` 對照表、不同輸入。
_QUARTER_START_MONTHS = (1, 4, 7, 10)
_SEASON_CODE_TO_SEASONAL_PARAM = {"01": "S1", "04": "S2", "07": "S3", "10": "S4"}


def season_key_for_date(d: date) -> str:
    """`d` 落在哪一季 → 那一季的 `season_key`（`YYYYQQ`）。"""
    return f"{d.year}{_MONTH_TO_SEASON[d.month]}"


def next_season_key(d: date) -> str:
    """`d` 之後**下一個**還沒開始的季度的 `season_key`——`d` 剛好是某季開播月份時，
    當季已經開始，回的是再下一季（不是當季自己）。"""
    for month in _QUARTER_START_MONTHS:
        if d.month < month:
            return f"{d.year}{month:02d}"
    return f"{d.year + 1}01"


def season_key_to_seasonal_param(season_key: str) -> str:
    """`YYYYQQ` → `seasonal.php?c=` 要的參數格式（`YYYY_S[1-4]`，例
    `202610` → `2026_S4`，2026-09-29 用瀏覽器實測 `fab-seasonal-promotion` 浮動按鈕
    連結抓到的真實格式）。"""
    year, code = season_key[:4], season_key[4:6]
    return f"{year}_{_SEASON_CODE_TO_SEASONAL_PARAM[code]}"
