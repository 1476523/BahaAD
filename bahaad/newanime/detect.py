"""從首頁公告偵測「新番節目資訊」＋把「X月／季」解析成季度代碼。

規格 docs/requirements/new_anime_bulletin.md §3。純函式，好單元測試。
"""

from __future__ import annotations

import re
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


def detect_bulletin_gnn_sn(text: str, links: Iterable[str]) -> int | None:
    """公告文字 + 連結清單 → 這是不是「新番節目資訊」公告；是的話回 GNN 文章 sn，否則 None。

    判斷：**連結（或文字）裡有 `gnn.gamer.com.tw/detail.php?sn=`** 且 **文字含「新番」+
    （「節目資訊」／「新番表」／「新番節目」／「節目表」之一）**。
    """
    text = text or ""
    if _KEYWORD_PRIMARY not in text:
        return None
    if not any(kw in text for kw in _KEYWORD_SECONDARY):
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
