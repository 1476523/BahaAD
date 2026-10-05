"""解析 youranimes.tw 的季度頁 `/bangumi/<YYYYMM>`。規格見 docs/requirements/youranimes.md。

站是 Next.js 但季度頁是**完整 server-side render**——HTML 內就有真的 markup，
BeautifulSoup（`html.parser`，比照 gamer_client/browse.py，不引 lxml）直接吃得到。

**選擇器只錨定穩定的結構／語意 token**（`article[id^=anime-]`、`h3 a[href^="/animes/"]`、
`whitespace-pre-line`、區塊標題文字 + 相鄰 `<ul>`）——youranimes 用 Tailwind arbitrary
class（`text-[13px]`、`grid-cols-[5.5em_1fr]`），改版整批失效，不能拿整串 class 比對。
解析失敗一律安靜退回空值，讓 web 層 fallback 到動畫瘋原資料；整頁 200 卻解析不到任何
一筆時由呼叫端記 WARNING（改版的偵測訊號）。
"""

from __future__ import annotations

import json
import logging
import re

from bs4 import BeautifulSoup

from bahaad.scheduler.gossip_watch import season_number
from bahaad.youranimes.models import (
    YourAnimesCastMember,
    YourAnimesMusic,
    YourAnimesRecord,
    YourAnimesStaff,
)

logger = logging.getLogger(__name__)

_ANIME_ID_RE = re.compile(r"(\d+)")
# 18 禁番劇不會出現在季度頁的 application/ld+json（SEO 用結構化資料，成人內容被排除），
# 只存在於 Next.js 的 RSC streaming payload（`self.__next_f.push(...)`，JS 字串跳脫、
# 不是乾淨的 JSON，不能直接 json.loads）。經過實測比對，`"_id":"<id>","adultContent":true`
# 這組 key 相鄰、沒有巢狀物件插在中間，是這個 payload 裡目前找得到的最穩定錨點——反過來
# 用它整段的 "name" 欄位當標題不穩（同一個物件裡角色／聲優的巢狀 "name" 常排在前面，會
# 抓錯），所以這裡只取 id，標題留給 `/animes/<id>` 個別頁自己解析（一定準）。
_ADULT_RSC_ID_RE = re.compile(r'\\"_id\\":\\"(\d+)\\",\\"adultContent\\":true')
_SECTION_STAFF = "製作"
_SECTION_CAST = "配音"
_SECTION_MUSIC = "音樂"
_KNOWN_SECTIONS = {_SECTION_STAFF, _SECTION_CAST, _SECTION_MUSIC}

# 個別番劇頁（`/animes/<id>`）用的區塊標題——跟季度卡片的 製作/配音/音樂 是不同文字
_PAGE_SECTION_SYNOPSIS = "簡介"
_PAGE_SECTION_STAFF = "製作陣容"
_PAGE_SECTION_CAST = "登場角色 / 演出聲優"
_PAGE_SECTION_MUSIC = "音樂"


def parse_season_page(html: str, *, season_slug: str = "") -> list[YourAnimesRecord]:
    """回傳季度頁上每部番劇的 `YourAnimesRecord`。解析不出任何一筆時回 `[]`。"""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # noqa: BLE001 - 壞掉的 HTML 不該讓整條同步爆掉
        logger.warning("季度頁 HTML 解析失敗", exc_info=True)
        return []

    records: list[YourAnimesRecord] = []
    for article in soup.select("article[id]"):
        article_id = article.get("id", "")
        if not article_id.startswith("anime-"):
            continue
        try:
            record = _parse_article(article, season_slug)
        except Exception:  # noqa: BLE001 - 單一 article 壞掉不影響其他
            logger.debug("youranimes 單一 article 解析失敗（%s）", article_id, exc_info=True)
            continue
        if record is not None:
            records.append(record)
    return records


def _parse_article(article, season_slug: str) -> YourAnimesRecord | None:
    link = article.select_one('h3 a[href^="/animes/"]') or article.select_one('a[href^="/animes/"]')
    if link is None:
        return None
    anime_id = _anime_id_from(article.get("id", ""), link.get("href", ""))
    if anime_id is None:
        return None
    zh_title = link.get_text(strip=True)
    if not zh_title:
        return None

    return YourAnimesRecord(
        anime_id=anime_id,
        zh_title=zh_title,
        jp_title=_jp_title(article, link),
        season_number=season_number(zh_title),
        synopsis=_synopsis(article),
        staff=tuple(_staff_rows(_section_list(article, _SECTION_STAFF))),
        cast=tuple(_cast_rows(_section_list(article, _SECTION_CAST))),
        music=tuple(_music_rows(_section_list(article, _SECTION_MUSIC))),
        season_slug=season_slug,
    )


def _anime_id_from(article_id: str, href: str) -> int | None:
    # `id="anime-6330"` 跟 `href="/animes/6330"` 應該一致；以 article id 為準，
    # 抓不到再退 href。
    for source in (article_id, href):
        m = _ANIME_ID_RE.search(source or "")
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                continue
    return None


def _jp_title(article, link) -> str:
    """日文原名：標題 `<h3>` 上方那個小灰字 `<div>`。當次要比對用的鍵，抓不到不影響。"""
    header = link.find_parent("h3")
    if header is None:
        return ""
    block = header.find_parent("div")
    sibling = block.find_previous_sibling("div") if block is not None else None
    if sibling is None:
        return ""
    # 前一個 sibling 若本身還包著連結／標題就不是日文名那格
    if sibling.find(["a", "h1", "h2", "h3", "button", "img"]) is not None:
        return ""
    text = sibling.get_text(strip=True)
    return text if 0 < len(text) <= 120 else ""


def _synopsis(article) -> str:
    p = article.select_one("p.whitespace-pre-line")
    if p is None:
        return ""
    return p.get_text("\n", strip=True)


def _section_list(article, section_name: str):
    """找到區塊標題（純文字 == section_name）後面緊鄰的 `<ul>`。找不到回 None。"""
    for div in article.find_all("div"):
        classes = div.get("class") or []
        if "font-bold" not in classes:
            continue
        if div.get_text(strip=True) != section_name:
            continue
        return div.find_next_sibling("ul")
    return None


def _direct_spans(row):
    return row.find_all("span", recursive=False)


def _staff_row(row) -> YourAnimesStaff | None:
    spans = _direct_spans(row)
    if len(spans) < 2:
        return None
    job = spans[0].get_text(strip=True)
    if not job:
        return None
    names: list[str] = []
    inner = spans[1].find_all("span")
    raw = [s.get_text(strip=True) for s in inner] if inner else [spans[1].get_text(strip=True)]
    for name in raw:
        name = name.lstrip("、").strip()
        if name and name != "、":
            names.append(name)
    return YourAnimesStaff(job=job, names=tuple(names))


def _cast_row(row) -> YourAnimesCastMember | None:
    spans = _direct_spans(row)
    if len(spans) < 2:
        return None
    character = spans[0].get_text(strip=True)
    actor = spans[1].get_text(strip=True)
    if not character and not actor:
        return None
    return YourAnimesCastMember(character=character, actor=actor)


def _music_row(row) -> YourAnimesMusic | None:
    spans = _direct_spans(row)
    if len(spans) < 2:
        return None
    slot = spans[0].get_text(strip=True)
    value = spans[1]
    # 曲名 = value 底下的直接文字節點（「曲名」）；演唱者 = 內層 <span>（／演唱者）。
    # 個別番劇頁的分隔號「／」是自己獨立的直接文字節點（季度卡片則黏在演唱者 span
    # 裡面）——兩種情況都要濾掉，不然會混進曲名。
    song_parts = [t.strip() for t in value.find_all(string=True, recursive=False)]
    song = "".join(p for p in song_parts if p and p not in ("／", "/")).strip()
    inner = value.find("span")
    artist = inner.get_text(strip=True).lstrip("／／/").strip() if inner is not None else ""
    if not song and inner is not None:
        song = value.get_text(strip=True)
    if not slot and not song:
        return None
    return YourAnimesMusic(slot=slot, song=song, artist=artist)


def _staff_rows(ul):
    if ul is None:
        return
    for li in ul.find_all("li", recursive=False):
        row = _staff_row(li)
        if row is not None:
            yield row


def _cast_rows(ul):
    if ul is None:
        return
    for li in ul.find_all("li", recursive=False):
        row = _cast_row(li)
        if row is not None:
            yield row


def _music_rows(ul):
    if ul is None:
        return
    for li in ul.find_all("li", recursive=False):
        row = _music_row(li)
        if row is not None:
            yield row


# ----------------------------------------------------------------------
# 個別番劇頁（`/animes/<id>`）——季度頁 `<article>` 抓不到的 18 禁／跨季番劇補洞用。
# 完全不同的模板：沒有 `<article>`，區塊靠 `<h2>` 文字（簡介/製作陣容/登場角色 / 演出
# 聲優/音樂）定位，一列是 `<div>`（不是 `<li>`）兩個直接 `<span>` 子元素——內層 span 的
# 巢狀結構（多人名用 `、` 相連、音樂曲名＋演唱者）跟季度卡片一致，沿用同一組列解析。
# ----------------------------------------------------------------------


def parse_anime_page(html: str, anime_id: int, *, season_slug: str = "") -> YourAnimesRecord | None:
    """解析個別番劇頁，回傳這部番劇的 `YourAnimesRecord`。解析不到標題就回 `None`。"""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # noqa: BLE001 - 壞掉的 HTML 不該讓整條同步爆掉
        logger.warning("個別番劇頁 HTML 解析失敗（%s）", anime_id, exc_info=True)
        return None

    h1 = soup.find("h1")
    zh_title = h1.get_text(strip=True) if h1 is not None else ""
    if not zh_title:
        return None

    return YourAnimesRecord(
        anime_id=anime_id,
        zh_title=zh_title,
        jp_title="",  # 個別頁沒有像季度卡片那樣獨立標出的日文原名欄位
        season_number=season_number(zh_title),
        synopsis=_page_synopsis(soup),
        staff=tuple(_page_rows(soup, _PAGE_SECTION_STAFF, _staff_row)),
        cast=tuple(_page_rows(soup, _PAGE_SECTION_CAST, _cast_row)),
        music=tuple(_page_rows(soup, _PAGE_SECTION_MUSIC, _music_row)),
        season_slug=season_slug,
    )


def _page_section(soup, heading_text: str):
    """找 `<h2>` 純文字等於 `heading_text` 的區塊（`<section>`），找不到回 `None`。"""
    for h2 in soup.find_all("h2"):
        if h2.get_text(strip=True) == heading_text:
            return h2.find_parent("section") or h2.parent
    return None


def _page_synopsis(soup) -> str:
    section = _page_section(soup, _PAGE_SECTION_SYNOPSIS)
    if section is None:
        return ""
    h2 = section.find("h2")
    div = h2.find_next_sibling("div") if h2 is not None else None
    if div is None:
        return ""
    return div.get_text("\n", strip=True)


def _page_rows(soup, heading_text: str, row_parser):
    section = _page_section(soup, heading_text)
    if section is None:
        return
    h2 = section.find("h2")
    wrapper = h2.find_next_sibling("div") if h2 is not None else None
    if wrapper is None:
        return
    for row in wrapper.find_all("div", recursive=False):
        parsed = row_parser(row)
        if parsed is not None:
            yield parsed


def season_page_all_anime_ids(html: str) -> dict[int, str]:
    """從季度頁取出**全部**番劇（含季度卡片抓不到的跨季番劇 **以及** 18 禁番劇）的
    `{anime_id: 標題}`（18 禁的標題欄位是空字串——見下方）。用來讓呼叫端知道「季度卡片
    漏了哪些」，再視需要逐一去抓個別番劇頁補洞。解析失敗回空 dict。

    兩個獨立來源合併：
    - `application/ld+json`（schema.org `ItemList`）：SEO 用結構化資料，涵蓋一般分級
      跨季番劇，欄位齊全（標題／連結都有），但**不含 18 禁內容**（成人內容被排除在
      SEO 資料外，實測 2026-09-05 確認：從後面來的神威先生等 18 禁番劇完全不在這裡）。
    - Next.js RSC streaming payload：18 禁番劇的 id **只**在這裡（`_ADULT_RSC_ID_RE`），
      標題不穩定不取，一律空字串——呼叫端不需要這裡的標題，個別番劇頁自己的 `<h1>`
      才是準的。"""
    try:
        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # noqa: BLE001
        return {}

    result: dict[int, str] = {}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or script.get_text() or "")
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict) or data.get("@type") != "ItemList":
            continue
        for entry in data.get("itemListElement") or []:
            item = entry.get("item") if isinstance(entry, dict) else None
            if not isinstance(item, dict):
                continue
            url = item.get("url") or ""
            name = (item.get("name") or "").strip()
            m = _ANIME_ID_RE.search(url)
            if not m or not name:
                continue
            try:
                anime_id = int(m.group(1))
            except ValueError:
                continue
            result[anime_id] = name

    for m in _ADULT_RSC_ID_RE.finditer(html or ""):
        try:
            anime_id = int(m.group(1))
        except ValueError:
            continue
        result.setdefault(anime_id, "")

    return result
