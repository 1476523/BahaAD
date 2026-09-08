"""解析 GNN「新番節目資訊」文章。規格 docs/requirements/new_anime_bulletin.md §1a。

文章是**扁平 `<div>`**（無表格）：`<h3>新作部分</h3>` → `<b>MM/DD （週X）</b>` 日期組 →
`HH:MM　《名》（附註）` 每部一行 → `（時間待定）` 組 → `<h3>其他續播節目</h3>`（不要）。

站方會改版，解析失敗一律**安靜回空清單**（呼叫端記 WARNING，詳細頁自動退回既有資料）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from bs4 import BeautifulSoup

from bahaad.newanime.detect import parse_season_key

_ZH_WEEKDAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7}

_PENDING_NOTE_MARKERS = ("授權流程", "陸續公佈", "陸續公布")
_SECTION_NEW = "新作部分"
_SECTION_CONTINUING = "其他續播節目"
_UNDETERMINED_MARKERS = ("（時間待定）", "(時間待定)", "時間待定")

_DATE_HEADER_RE = re.compile(r"^(\d{1,2})/(\d{1,2})\s*（週([一二三四五六日])）")
_PUBLISH_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})\s+\d{2}:\d{2}:\d{2}")
# HH:MM　《名》（附註）——時間、附註都可選（待定組沒時間）
_ITEM_RE = re.compile(r"^(?:(\d{1,2}:\d{2})\s*)?《(.+?)》(?:\s*（(.+)）)?\s*$")

_NOTE_FIRST_EP_COUNT_RE = re.compile(r"首播更新\s*(\d+)\s*集")
_NOTE_FIRST_EP_NUMBER_RE = re.compile(r"首播為第\s*(\d+)\s*話")
_NOTE_BACKFILL_RE = re.compile(r"前\s*(\d+)\s*話")
_NOTE_ONGOING_RE = re.compile(r"每週([一二三四五六日])\s*(\d{1,2}:\d{2})")


@dataclass(frozen=True)
class ParsedBulletinItem:
    source_name: str
    article_order: int
    first_air_date: str | None  # 'YYYY-MM-DD'
    first_air_time: str | None  # 'HH:MM'
    first_air_weekday: int | None  # 1=一 .. 7=日
    is_undetermined: bool
    first_ep_count: int = 1
    first_ep_number: int = 1
    backfill_from_episode: int | None = None
    ongoing_weekday: int | None = None
    ongoing_time: str | None = None
    is_vip: bool = False
    region_locked: bool = False


@dataclass(frozen=True)
class ParsedBulletin:
    title: str
    published_at: str | None  # 'YYYY-MM-DD HH:MM:SS'
    pending_note_present: bool
    season_key: str | None = None  # 'YYYYQQ'，從標題解析
    items: tuple[ParsedBulletinItem, ...] = field(default_factory=tuple)


def parse_gnn_article(
    html: str, *, season_key: str | None = None, fallback_year: int | None = None
) -> ParsedBulletin:
    """`season_key` 沒帶就從 `<h1>` 標題解析（「2026 夏季」/「7月新番」）；解析不出 →
    `season_key=None`、集數的日期也算不出（`first_air_date=None`），但標題／發佈時間／
    備註旗標照樣回。站方改版 → 空 `items`。"""
    soup = BeautifulSoup(html, "html.parser")

    title_el = soup.find("h1")
    title = title_el.get_text(strip=True) if title_el else ""

    full_text = soup.get_text(" ")
    pending = any(marker in full_text for marker in _PENDING_NOTE_MARKERS)
    published_at = _extract_published_at(full_text)

    if season_key is None:
        year = fallback_year
        if year is None:
            year = int(published_at[:4]) if published_at else date.today().year
        season_key = parse_season_key(title, fallback_year=year)

    body = _find_article_body(soup)
    items = _parse_items(body, season_key) if (body is not None and season_key) else ()

    return ParsedBulletin(
        title=title, published_at=published_at, pending_note_present=pending,
        season_key=season_key, items=items,
    )


def _extract_published_at(text: str) -> str | None:
    m = _PUBLISH_RE.search(text)
    if not m:
        return None
    return m.group(0).replace("  ", " ").strip()


def _find_article_body(soup: BeautifulSoup):
    candidates = [
        el
        for el in soup.find_all("div")
        if _SECTION_NEW in el.get_text()
    ]
    if not candidates:
        return None
    # 同時含「其他續播節目」的最小容器優先；沒有的話（某些季沒續播）取含「新作部分」的最小
    with_both = [el for el in candidates if _SECTION_CONTINUING in el.get_text()]
    pool = with_both or candidates
    return min(pool, key=lambda el: len(el.get_text()))


def _flatten_lines(body) -> list[str]:
    # `<a class="acglink">名</a><a> 第 2 季</a>` 拆成多個文字節點，get_text("\n") 會把名字
    # 切斷 → 先 unwrap 掉所有 <a> 再 smooth() 合併相鄰文字節點，名字就回到同一行。
    clone = BeautifulSoup(str(body), "html.parser")
    for anchor in clone.find_all("a"):
        anchor.unwrap()
    clone.smooth()
    lines = []
    for raw in clone.get_text("\n").split("\n"):
        line = re.sub(r"\s+", " ", raw).strip()
        if line and line != "\xa0":
            lines.append(line)
    return lines


def _parse_items(body, season_key: str) -> tuple[ParsedBulletinItem, ...]:
    year = int(season_key[:4])
    quarter_month = int(season_key[4:6])

    mode = "pre"  # pre → new → undetermined → done
    weekday: int | None = None
    md: tuple[int, int] | None = None
    order = 0
    out: list[ParsedBulletinItem] = []

    for line in _flatten_lines(body):
        if mode == "pre":
            if _SECTION_NEW in line:
                mode = "new"
            continue
        if _SECTION_CONTINUING in line:
            break
        if any(marker in line for marker in _UNDETERMINED_MARKERS):
            mode, weekday, md = "undetermined", None, None
            continue

        header = _DATE_HEADER_RE.match(line)
        if header:
            mode = "new"
            md = (int(header.group(1)), int(header.group(2)))
            weekday = _ZH_WEEKDAY[header.group(3)]
            continue

        item = _ITEM_RE.match(line)
        if item is None:
            continue
        time_str, name, notes = item.group(1), item.group(2).strip(), item.group(3)
        if not name:
            continue
        order += 1
        notes_fields = _parse_notes(notes or "")
        first_air_date = (
            _iso_date(year, quarter_month, md[0], md[1]) if md is not None else None
        )
        out.append(
            ParsedBulletinItem(
                source_name=name,
                article_order=order,
                first_air_date=first_air_date,
                first_air_time=time_str,
                first_air_weekday=weekday,
                is_undetermined=(mode == "undetermined"),
                **notes_fields,
            )
        )
    return tuple(out)


def _parse_notes(notes: str) -> dict:
    out: dict = {
        "first_ep_count": 1,
        "first_ep_number": 1,
        "backfill_from_episode": None,
        "ongoing_weekday": None,
        "ongoing_time": None,
        "is_vip": "限付費會員" in notes,
        "region_locked": "無港澳" in notes,
    }
    if (m := _NOTE_FIRST_EP_COUNT_RE.search(notes)):
        out["first_ep_count"] = max(1, int(m.group(1)))
    if (m := _NOTE_FIRST_EP_NUMBER_RE.search(notes)):
        out["first_ep_number"] = max(1, int(m.group(1)))
    if (m := _NOTE_BACKFILL_RE.search(notes)):
        out["backfill_from_episode"] = int(m.group(1))
    if (m := _NOTE_ONGOING_RE.search(notes)):
        out["ongoing_weekday"] = _ZH_WEEKDAY[m.group(1)]
        out["ongoing_time"] = m.group(2)
    return out


def _iso_date(season_year: int, quarter_month: int, mm: int, dd: int) -> str | None:
    """`MM/DD` + 季度年 → `YYYY-MM-DD`。冬番（QQ=01）文章 12 月底發、首集日期有 12/xx 的
    → 算前一年。日期本身壞掉（2/30 之類）回 None。"""
    year = season_year
    if quarter_month == 1 and mm >= 10:
        year -= 1
    try:
        return date(year, mm, dd).isoformat()
    except ValueError:
        return None
