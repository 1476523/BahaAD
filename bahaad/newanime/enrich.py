"""補新番快訊的欄位——`seasonal.php`（製作廠商／標籤／授權範圍／訂閱人數）＋ youranimes
季度頁（主視覺圖／導演監督）。規格 docs/requirements/new_anime_bulletin.md §1b、§1c。

跟 GNN 一樣：站方改版一律安靜回空結果，詳細頁那格顯示「待確認」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup

from bahaad.store.youranimes_cache import title_base_key
from bahaad.youranimes.parse import parse_season_page

_DIRECTOR_JOBS = ("導演監督", "導演", "監督", "總導演")


@dataclass(frozen=True)
class SeasonalCard:
    name: str
    studio: str = ""
    tags: tuple[str, ...] = ()
    subscribe_count: str = ""  # 例 '7.1萬' / '1,234'
    range_text: str = ""       # 授權範圍，例 '台灣、港澳' / '台灣'


# 三個來源對同一部番的標點／空白常有出入（GNN「無職轉生～…～第三季」vs youranimes
# 「無職轉生 ～…～ 第三季」）——比對鍵再統一掉全形標點與所有空白（2026-09-05 用 7 月
# 新番實測：不做這步 youranimes 命中率只有 ~70%）。
_PUNCT_NORMALIZE = str.maketrans(
    {
        "～": "~", "〜": "~", "－": "-", "—": "-", "―": "-", "–": "-",
        "！": "!", "？": "?", "：": ":", "・": "·", "，": ",",
    }
)


# 三個來源對「續作」的寫法各異：GNN／seasonal.php 用「碧藍之海 3」「第 2 季」「Clevatess Ⅱ」，
# youranimes 用「第三季」「第二季度」——`title_base_key`（＝gossip 的 build_search_title）
# 其實**不脫季別尾綴**，只把 S2/2nd Season 正規化成「第二季」。這裡把尾端的季別標記
# （「第N季」「第N季度」「第N期」／裸數字 2–19／羅馬 ⅱ–ⅹ）整個脫掉，讓三邊都收斂到
# 基礎名。單一季度頁不會同時收錄同系列的前後兩季，脫過頭配錯季的風險可忽略
# （2026-09-05 用 7 月新番實測補：不做這步 youranimes 命中率只有 ~70%）。
_TRAILING_SEASON_RE = re.compile(
    r"(?:"
    r"第[0-9零一二三四五六七八九十]{1,3}(?:季度|季|期)"
    r"|1[0-9]|[2-9]"
    r"|ⅱ|ⅲ|ⅳ|ⅴ|ⅵ|ⅶ|ⅷ|ⅸ|ⅹ"
    r"|i{2,3}|iv|vi{0,3}"
    r")$"
)


def match_key(name: str) -> str:
    """三個來源（GNN 番名 / seasonal.php 標題 / youranimes alt）共用的比對鍵。"""
    base = title_base_key(name or "").translate(_PUNCT_NORMALIZE)
    base = re.sub(r"\s+", "", base)
    for _ in range(2):  # 「基礎名 II 第二季」這種疊寫的脫兩層
        stripped = _TRAILING_SEASON_RE.sub("", base)
        if stripped == base or not stripped:
            break
        base = stripped
    return base


# ---- seasonal.php ---------------------------------------------------------


def parse_seasonal_page(html: str) -> list[SeasonalCard]:
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # noqa: BLE001
        return []
    cards: list[SeasonalCard] = []
    for card in soup.select("div.program-card"):
        title_el = card.select_one("h5.title")
        name = title_el.get_text(strip=True) if title_el else ""
        if not name:
            continue
        studio_el = card.select_one("div.company p")
        range_el = card.select_one("div.range p")
        tags = tuple(
            t.get_text(strip=True)
            for t in card.select("div.tag-row div.tag")
            if t.get_text(strip=True)
        )
        sub_el = card.select_one("div.btn-subscribe[data-gather-number]")
        subscribe = ""
        if sub_el is not None:
            subscribe = re.sub(
                r"^[\s(（]+|[\s)）]+$", "", sub_el.get("data-gather-number") or ""
            )
        cards.append(
            SeasonalCard(
                name=name,
                studio=studio_el.get_text(strip=True) if studio_el else "",
                tags=tags,
                subscribe_count=subscribe,
                range_text=range_el.get_text(strip=True) if range_el else "",
            )
        )
    return cards


def seasonal_by_key(html: str) -> dict[str, SeasonalCard]:
    return {match_key(c.name): c for c in parse_seasonal_page(html)}


# ---- youranimes 季度頁 ---------------------------------------------------


def _to_origin(url: str) -> str:
    """`.../XXX.webp` → `.../XXX_origin.webp`（最大尺寸）。"""
    m = re.match(r"^(.*)(\.[A-Za-z0-9]+)$", url or "")
    return f"{m.group(1)}_origin{m.group(2)}" if m else (url or "")


def parse_youranimes_covers(html: str) -> dict[str, str]:
    """`{比對鍵: 主視覺圖 _origin 網址}`。番名取 `<img alt>` 去掉「 主視覺圖」。"""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, str] = {}
    for art in soup.select('article[id^="anime-"]'):
        img = art.select_one('img[alt$="主視覺圖"]') or art.select_one("img[src]")
        if img is None:
            continue
        alt = re.sub(r"\s*主視覺圖\s*$", "", img.get("alt") or "").strip()
        src = (img.get("src") or "").strip()
        if alt and src:
            out[match_key(alt)] = _to_origin(src)
    return out


def parse_youranimes_directors(html: str) -> dict[str, str]:
    """`{比對鍵: 導演監督}`——沿用既有 `youranimes.parse.parse_season_page` 的製作陣容解析。"""
    out: dict[str, str] = {}
    for record in parse_season_page(html):
        director = _director_from_staff(record.staff)
        if director:
            out[match_key(record.zh_title)] = director
    return out


def _director_from_staff(staff) -> str:
    for row in staff or ():
        if any(job in (row.job or "") for job in _DIRECTOR_JOBS):
            return "、".join(row.names) if row.names else ""
    return ""
