"""動畫瘋首頁／番劇頁 HTML 爬取。規格見 docs/requirements/gamer_client_browse.md。

跟 `catalog.py` 走的 `api.gamer.com.tw/anime/v1/video.php` JSON API 是兩條不同的路——
這裡拿的是首頁本季新番／週期表這類**只有網頁本身有、沒有對外 JSON API** 的資料，只能
直接爬 HTML。訪客身分即可，跟 `catalog.py`／`playlist.py` 一樣不需要登入。

選擇器全部是 2026-08-23 用 Claude Browser 實際觀察 `ani.gamer.com.tw` 真實頁面 DOM
得到的，不是憑截圖猜的，細節見 `gamer_client_browse.md`——之後站方改版導致選擇器失效，
要回這份文件更新。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Protocol
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from bahaad.gamer_client.titles import clean_anime_title

_HOME_URL = "https://ani.gamer.com.tw/"
_ANIME_VIDEO_URL = "https://ani.gamer.com.tw/animeVideo.php"
_ANIME_REF_URL = "https://ani.gamer.com.tw/animeRef.php"
_SEARCH_URL = "https://ani.gamer.com.tw/search.php"
_ANIME_LIST_URL = "https://ani.gamer.com.tw/animeList.php"

# 番劇級標題會帶當前集數的「 [N]」尾巴（例："無職轉生...第三季 [2]"）——
# 這是動畫瘋 <h1> 本身的既有格式，不是我們自己加的，這裡去掉集數尾巴取番劇本身的名稱
# 番劇標題結尾的 [...] 標記統一交給 bahaad.gamer_client.titles.clean_anime_title 處理
# （round 7 第 8、15 項：不只 [數字]，[特別篇]/[電影]/[OVA] 都去掉）

_TYPE_LIST_FIELD_LABELS = {
    "首播日期": "air_date",
    "導演監督": "director",
    "代理廠商": "distributor",
    "製作廠商": "producer",
}

# 週一～週日的星期文字，跟 store/schedule_list.py 的 1=一...7=日編碼對照用
WEEKDAY_ORDER = ["週一", "週二", "週三", "週四", "週五", "週六", "週日"]


class BrowseError(Exception):
    pass


class HttpGetter(Protocol):
    def get(self, url: str, params: dict | None = None): ...


@dataclass(frozen=True)
class AnimeCard:
    video_sn: int
    title: str
    cover_url: str
    time_text: str
    episode_text: str
    watch_count: str
    # 首頁時間軸每個 area 的日期標籤，原字串照抄（例："09/01 (二)"）。站方的
    # `data-date-code` 只是「距今第幾天」的排序索引、不是星期（1=今天、2=昨天…），
    # 真正的日期在每個 area 的 <span class="anime-date-info">。標「其他」的 area
    # （劇場版總集篇等）不會進 get_newanime，所以這裡不會是空字串。
    # （.0 改進.txt 第 23 項：首頁本季新番依這個分日期分組，仿官方時間軸）
    air_date: str = ""


@dataclass(frozen=True)
class ScheduleEntry:
    video_sn: int
    title: str
    time_text: str
    episode_text: str


@dataclass(frozen=True)
class WeeklySchedule:
    day: str
    entries: list[ScheduleEntry]


@dataclass(frozen=True)
class Episode:
    number: str
    video_sn: int


@dataclass(frozen=True)
class EpisodeCategory:
    name: str
    episodes: list[Episode]


@dataclass(frozen=True)
class RelatedAnime:
    ref_sn: int
    title: str
    cover_url: str
    year_text: str
    episode_count_text: str


@dataclass(frozen=True)
class SearchResult:
    """`search.php` 的一張搜尋結果卡片。**連的是 `animeRef.php?sn={ref_sn}` 番劇層級
    代碼，不是某一集的 `video_sn`**——呼叫端要拿 `ref_sn` 走 `resolve_ref_sn()` 才能
    得到真正的 `video_sn`（`scheduler/gossip_watch.py` 的作品比對就是這樣用的）。
    欄位跟 `RelatedAnime` 一樣是「番劇代碼＋顯示用中繼資料」，但兩者是不同來源、不同
    用途（詳細頁側欄 vs 搜尋結果），比照 `AnimeCard`／`ScheduleEntry` 各自獨立的慣例
    分開兩個型別。`year_text`／`episode_count_text` 保留站上原字串（"年份：2026/07"／
    "共9集"），要拆成數字由呼叫端自己處理，這一層只做爬取。"""

    ref_sn: int
    title: str
    cover_url: str
    year_text: str
    episode_count_text: str


@dataclass(frozen=True)
class AnimeListPage:
    """`animeList.php` 的一頁結果（供「近期熱播」＝依月人氣排序、下滑無限捲動用）。
    `results` 是這一頁的卡片、`page` 是第幾頁、`max_page` 是站方分頁列最後一個頁碼
    （會隨時間浮動，不要寫死）。`has_next` 給前端判斷還要不要繼續往下抓。"""

    results: list[SearchResult]
    page: int
    max_page: int

    @property
    def has_next(self) -> bool:
        return self.page < self.max_page


@dataclass(frozen=True)
class AnimeDetail:
    video_sn: int
    title: str
    cover_url: str
    air_date: str = ""
    director: str = ""
    distributor: str = ""
    producer: str = ""
    genres: list[str] = field(default_factory=list)
    description: str = ""
    rating_score: str = ""
    rating_count: str = ""
    episode_categories: list[EpisodeCategory] = field(default_factory=list)
    related_anime: list[RelatedAnime] = field(default_factory=list)


_NEWANIME_DATE_RE = re.compile(r"\d{1,2}/\d{1,2}\s*\([一二三四五六日]\)")


def get_newanime(http: HttpGetter) -> list[AnimeCard]:
    """首頁「本季新番」，依站方順序（最近更新的日期在前）回傳每張卡片。
    見 gamer_client_browse.md 目標 1。

    **只回傳真正「本季新番」的項目**，不是首頁「依更新日排列」時間軸上出現的所有卡片：

    - 排除付費比例區塊（`.newanime-date-area.premium-block`）
    - 排除**沒有 `data-date-code` 屬性**的 area——站方時間軸會把「最近有更新、但不是這一季
      首播」的舊番續季（史萊姆第四季、Re:0 第四季、BLEACH 千年血戰篇…）也列出來，這些沒有
      `data-date-code`；真正的本季新番才會帶 `data-date-code`
    - 排除日期標「其他」的 area——每個 area 的 `<span class="anime-date-info">` 是這一組的
      日期文字（"09/01 (二)"），劇場版總集篇那種跟本季無關的則標「其他」（沒有日期）。
      使用者 2026-08-26 明確要求扣除。

    `data-date-code` 只是「距今第幾天」的排序索引（1=今天、2=昨天…），**不是星期**，
    所以卡片的日期一律從 `anime-date-info` 拿、不從 `data-date-code` 推。
    """
    soup = _fetch_home_soup(http)
    cards: list[AnimeCard] = []
    for area in soup.select(".newanime-date-area"):
        classes = area.get("class") or []
        if "premium-block" in classes:
            continue
        if area.get("data-date-code") is None:
            continue  # 時間軸上的舊番續季，不算本季新番
        date_el = area.select_one(".anime-date-info")
        match = _NEWANIME_DATE_RE.search(date_el.get_text(" ", strip=True)) if date_el else None
        if match is None:
            continue  # 標「其他」的 area（劇場版總集篇等），使用者要求扣除
        air_date = re.sub(r"\s+", " ", match.group(0))
        for card_el in area.select("a.anime-card-block"):
            card = _parse_anime_card(card_el)
            if card is not None:
                cards.append(replace(card, air_date=air_date))
    return cards


def get_weekly_schedule(http: HttpGetter) -> list[WeeklySchedule]:
    """首頁「週期表」，依週一～週日順序回傳。見 gamer_client_browse.md 目標 2——
    已確認純 HTTP GET（不執行 JS）就能拿到完整內容，不需要 headless browser。"""
    soup = _fetch_home_soup(http)
    by_day: dict[str, list[ScheduleEntry]] = {}
    for day_list in soup.select(".day-list"):
        title_el = day_list.select_one("h3.day-title")
        if title_el is None:
            continue
        day = title_el.get_text(strip=True)
        entries = [
            entry
            for a in day_list.select("a.text-anime-info")
            if (entry := _parse_schedule_entry(a)) is not None
        ]
        by_day[day] = entries
    return [WeeklySchedule(day=day, entries=by_day.get(day, [])) for day in WEEKDAY_ORDER]


def get_anime_detail(http: HttpGetter, video_sn: int) -> AnimeDetail:
    """番劇詳細頁：封面／基本資訊／分類／簡介／評分／依類別分組的集數清單／相關動畫。
    見 gamer_client_browse.md 目標 5。"""
    response = http.get(_ANIME_VIDEO_URL, params={"sn": video_sn})
    status_code = getattr(response, "status_code", 200)
    if status_code != 200:
        raise BrowseError(f"番劇詳細頁回應非 200: HTTP {status_code}")
    soup = BeautifulSoup(response.text, "html.parser")

    title_el = soup.select_one(".anime_name h1")
    title = clean_anime_title(title_el.get_text(strip=True)) if title_el else ""

    cover_url = ""
    cover_el = soup.select_one(".data-file img.data-img")
    if cover_el is not None:
        cover_url = cover_el.get("data-src") or cover_el.get("src") or ""

    fields = {"air_date": "", "director": "", "distributor": "", "producer": ""}
    genres: list[str] = []
    for li in soup.select("ul.type-list > li"):
        label_el = li.select_one("span.title")
        if label_el is None:
            continue
        label = label_el.get_text(strip=True)
        if label == "作品分類":
            genres = [tag.get_text(strip=True) for tag in li.select("ul.tag-list > li.tag")]
        elif label in _TYPE_LIST_FIELD_LABELS:
            value = li.get_text(strip=True).removeprefix(label)
            fields[_TYPE_LIST_FIELD_LABELS[label]] = value

    # `.data-intro` 底下除了簡介本文的 <p> 外，還有一個「作品資料」展開按鈕
    # （<div class="link">），只取 <p> 避免把按鈕文字（含 material-icons 的
    # keyboard_arrow_down）一起吃進來
    description = _text(soup, ".data-intro > p")

    rating_score = _text(soup, ".acg-score .score-overall-number")
    rating_count = _text(soup, ".acg-score .score-overall-people")

    return AnimeDetail(
        video_sn=video_sn,
        title=title,
        cover_url=cover_url,
        genres=genres,
        description=description,
        rating_score=rating_score,
        rating_count=rating_count,
        episode_categories=_parse_episode_categories(soup, fallback_video_sn=video_sn, fallback_title=title),
        related_anime=_parse_related_anime(soup),
        **fields,
    )


def search_anime(http: HttpGetter, keyword: str) -> list[SearchResult]:
    """`GET search.php?keyword=`，回傳搜尋結果卡片清單。見 gamer_client_browse.md
    目標 6——**實測（2026-08-26）發現規格原本寫的「卡片版型跟本季新番共用 `a.anime-card-block`」
    是錯的**：真實搜尋結果用的是 `animeList.php` 那套 `a.theme-list-main` 版型，連的是
    `animeRef.php?sn={ref_sn}`（番劇代碼）而不是 `animeVideo.php?sn={video_sn}`。這其實
    正好符合 `scheduler_gossip_watch.md` 作品比對流程的預期（拿 `ref_sn` 走
    `resolve_ref_sn()`）。查無結果時站方回一個 `<div class="notice">沒有搜尋到動畫</div>`、
    沒有任何 `a.theme-list-main`，這裡自然回傳空清單，不需要特別判斷那段文字。"""
    response = http.get(_SEARCH_URL, params={"keyword": keyword})
    status_code = getattr(response, "status_code", 200)
    if status_code != 200:
        raise BrowseError(f"搜尋頁回應非 200: HTTP {status_code}")
    soup = BeautifulSoup(response.text, "html.parser")

    # 只取「搜尋結果」區塊（`div.old_list`），不碰頁面下方站方自己的「願望清單」推薦區塊
    # （`div.animate-theme-list.animate-wish`，在 `.container` 底下、卡片 href 是
    # `javascript:;`）——使用者明確排除，見 gamer_client_browse.md 目標 6
    return _parse_theme_list_block(soup.select_one("div.old_list"))


def get_new_arrivals(http: HttpGetter) -> list[SearchResult]:
    """首頁「新上架」區塊（`#blockAnimeNewArrive`）——代理商新授權上架的番劇，依上架日
    排列。見 gamer_client_browse.md 目標 3。

    **卡片版型是 `a.theme-list-main` + `href="animeRef.php?sn={ref_sn}"`**（跟
    `search.php`／`animeList.php` 同一套，回傳 `SearchResult`），**不是**本季新番的
    `a.anime-card-block` + `video_sn`——目標 3 規格原本寫「跟目標 1 相同」是錯的，
    2026-08-27 用真實首頁 DOM 修正（跟目標 6 當初踩過的同一種錯）。呼叫端要拿 `ref_sn`
    走 `resolve_ref_sn()` 才能得到真正的 `video_sn`。

    這個區塊**穩定**（不隨機、依上架日），觀察當下 21 張，站方**沒有「看更多」頁**
    （只有這 21 筆）。卡片的 `.theme-time` 是「上架日：MM/DD」而不是本季新番的
    「年份：YYYY/MM」——`_parse_search_result()` 照樣塞進 `year_text`，顯示端自己處理。

    （站方首頁另有 `#blockHotAnime`「近期熱播」區塊，但**每次載入隨機洗牌**、不適合
    快取，「近期熱播」頁改用 `list_anime(sort=2)`＝依月人氣排序，見 web_redesign.md。）"""
    soup = _fetch_home_soup(http)
    return _parse_theme_list_block(soup.find(id="blockAnimeNewArrive"))


# animeList.php 篩選側欄的選項——2026-08-27 從真實 `animeList.php` 爬下來。`tags`（屬性）
# 站方可多選、上限 `ANIME_LIST_MAX_TAGS`，用逗號串（`tags=戀愛,校園`，AND 篩選）；
# `category`（類型）／`target`（對象）單選。三者的「全部」＝不篩選，下面的清單不含它。
# 排序：中文二字 → 中文三字 → 中文四字 → 英文（搜尋頁篩選面板依這個順序排、
# 每個字數一組各自一行起頭，見 web_redesign_round3.md 階段 7-2 使用者回饋）。
ANIME_LIST_TAGS: tuple[str, ...] = (
    # 中文二字
    "動作", "冒險", "奇幻", "魔法", "科幻", "機甲", "校園", "喜劇", "戀愛", "青春",
    "勵志", "溫馨", "悠閒", "料理", "親情", "感人", "運動", "競技", "偶像", "音樂",
    "職場", "推理", "懸疑", "歷史", "戰爭", "黑暗", "特攝",
    # 中文三字
    "異世界", "超能力",
    # 中文四字
    "時間穿越", "血腥暴力", "靈異神怪",
    # 英文
    "BL", "GL",
)
ANIME_LIST_CATEGORIES: tuple[str, ...] = ("電影", "OVA", "雙語", "泡麵番", "真人演出")
ANIME_LIST_TARGETS: tuple[str, ...] = ("闔家觀賞", "付費會員", "年齡限制")
ANIME_LIST_MAX_TAGS = 5
_ANIME_LIST_ALL = "全部"


def list_anime(
    http: HttpGetter,
    *,
    sort: int = 1,
    page: int = 1,
    tags: tuple[str, ...] | list[str] = (),
    category: str | None = None,
    target: str | None = None,
) -> AnimeListPage:
    """`GET animeList.php?sort=&page=&tags=&category=&target=`——所有動畫的完整列表，
    可帶篩選。見 gamer_client_browse.md 目標 4。**「近期熱播」頁用 `sort=2`**（依月人氣
    排序，穩定、有意義）＋下滑無限捲動；**「搜尋番劇」頁的篩選面板**帶 `tags`／`category`
    ／`target`（見 web_redesign_round3.md 階段 7-2）。

    - `sort=1`＝依年份新到舊；`sort=2`＝依月人氣（站方只有這兩種、**沒有「依上架日」**）。
    - `tags`：0～`ANIME_LIST_MAX_TAGS` 個屬性（`ANIME_LIST_TAGS` 的值），逗號串；空＝不篩。
    - `category`／`target`：`ANIME_LIST_CATEGORIES`／`ANIME_LIST_TARGETS` 的值，`None`＝不篩。
    - 每頁 28 張 `a.theme-list-main`，連 `animeRef.php?sn={ref_sn}`（同 search.php）。
    - 分頁在 `div.page_number` 裡一排 `a[data-ani-list-page]`，最後一個的值就是總頁數
      （會隨時間浮動，不要寫死）——沒有分頁列時 `max_page` 就等於當頁。
    """
    params = {
        "sort": sort,
        "page": page,
        "tags": ",".join(tags) if tags else _ANIME_LIST_ALL,
        "category": category or _ANIME_LIST_ALL,
        "target": target or _ANIME_LIST_ALL,
    }
    response = http.get(_ANIME_LIST_URL, params=params)
    status_code = getattr(response, "status_code", 200)
    if status_code != 200:
        raise BrowseError(f"animeList 回應非 200: HTTP {status_code}")
    soup = BeautifulSoup(response.text, "html.parser")

    max_page = page
    for a in soup.select("div.page_number a[data-ani-list-page]"):
        try:
            max_page = max(max_page, int(a["data-ani-list-page"]))
        except (ValueError, KeyError, TypeError):
            continue
    return AnimeListPage(results=_parse_theme_list_block(soup), page=page, max_page=max_page)


def _parse_theme_list_block(container) -> list[SearchResult]:
    """`a.theme-list-main` 卡片清單解析（新上架／所有動畫／搜尋結果共用同一套版型）。
    `container` 是 `None`（找不到區塊）時回空清單。"""
    if container is None:
        return []
    return [
        result
        for a in container.select("a.theme-list-main")
        if (result := _parse_search_result(a)) is not None
    ]


def resolve_ref_sn(http: HttpGetter, ref_sn: int) -> int | None:
    """相關動畫卡片給的是 `animeRef.php?sn=` 的關聯代碼，不是真正的 video_sn——這支
    端點會伺服器端 302 導向真正的 `animeVideo.php?sn=`，這裡跟著重導向解析出來，見
    gamer_client_browse.md「animeRef.php 的解析行為」一節。查不到就回傳 None，呼叫端
    自行決定要不要退化成「只能點出去外部連結」。"""
    response = http.get(_ANIME_REF_URL, params={"sn": ref_sn})
    final_url = getattr(response, "url", None)
    if not final_url:
        return None
    return _extract_sn(str(final_url))


def _parse_episode_categories(
    soup: BeautifulSoup, fallback_video_sn: int | None = None, fallback_title: str = ""
) -> list[EpisodeCategory]:
    def _fallback() -> list[EpisodeCategory]:
        # 電影／特別篇這類沒有 section.season 集數清單的作品——用這一頁自己的 video_sn
        # 當唯一一集，「全部下載」才不會誤判成抓不到集數（round 6 第 13 項）
        if fallback_video_sn is None:
            return []
        return [EpisodeCategory(name="本篇", episodes=[Episode(number=fallback_title or "本篇", video_sn=fallback_video_sn)])]

    season = soup.select_one("section.season")
    if season is None:
        return _fallback()

    direct_children = [c for c in season.find_all(["p", "ul"], recursive=False)]
    if not direct_children:
        return _fallback()

    if direct_children[0].name == "ul":
        # 單一分類（例：只有本篇的番劇）：section.season 底下直接是 ul，沒有 <p> 類別
        # 標籤，名稱預設當作「本篇」，見 gamer_client_browse.md 目標 5
        return [EpisodeCategory(name="本篇", episodes=_parse_episodes(direct_children[0]))]

    categories = []
    name = None
    for child in direct_children:
        if child.name == "p":
            name = child.get_text(strip=True)
        elif child.name == "ul" and name is not None:
            categories.append(EpisodeCategory(name=name, episodes=_parse_episodes(child)))
            name = None
    if not any(cat.episodes for cat in categories):
        return _fallback()
    return categories


def _parse_episodes(ul) -> list[Episode]:
    episodes = []
    for a in ul.select("li > a"):
        video_sn_raw = a.get("data-ani-video-sn")
        if video_sn_raw is None:
            continue
        try:
            video_sn = int(video_sn_raw)
        except ValueError:
            continue
        episodes.append(Episode(number=a.get_text(strip=True), video_sn=video_sn))
    return episodes


def _parse_search_result(a) -> SearchResult | None:
    ref_sn = _extract_sn(a.get("href", ""))
    if ref_sn is None:
        return None
    cover_url = ""
    img = a.select_one("img.theme-img")
    if img is not None:
        cover_url = img.get("data-src") or img.get("src") or ""
    return SearchResult(
        ref_sn=ref_sn,
        title=_text(a, ".theme-name"),
        cover_url=cover_url,
        year_text=_text(a, ".theme-time"),
        episode_count_text=_text(a, ".theme-number"),
    )


def _parse_related_anime(soup: BeautifulSoup) -> list[RelatedAnime]:
    related = []
    for a in soup.select("section.old_list a[href*='animeRef.php']"):
        ref_sn = _extract_sn(a.get("href", ""))
        if ref_sn is None:
            continue
        cover_url = ""
        img = a.select_one("img.theme-img")
        if img is not None:
            cover_url = img.get("data-src") or img.get("src") or ""
        related.append(
            RelatedAnime(
                ref_sn=ref_sn,
                title=_text(a, ".theme-name"),
                cover_url=cover_url,
                year_text=_text(a, ".theme-time"),
                episode_count_text=_text(a, ".theme-number"),
            )
        )
    return related


def _fetch_home_soup(http: HttpGetter) -> BeautifulSoup:
    response = http.get(_HOME_URL)
    status_code = getattr(response, "status_code", 200)
    if status_code != 200:
        raise BrowseError(f"首頁回應非 200: HTTP {status_code}")
    return BeautifulSoup(response.text, "html.parser")


def _parse_anime_card(card_el) -> AnimeCard | None:
    video_sn = _extract_sn(card_el.get("href", ""))
    if video_sn is None:
        return None

    cover_url = ""
    for img in card_el.select("img"):
        if "anime-clock-img" in (img.get("class") or []):
            continue
        cover_url = img.get("data-src") or img.get("src") or ""
        break

    return AnimeCard(
        video_sn=video_sn,
        title=_text(card_el, ".anime-name"),
        cover_url=cover_url,
        time_text=_text(card_el, ".anime-hours"),
        episode_text=_text(card_el, ".anime-episode"),
        watch_count=_text(card_el, ".anime-watch-number").replace("remove_red_eye", "").strip(),
    )


def _parse_schedule_entry(a) -> ScheduleEntry | None:
    video_sn = _extract_sn(a.get("href", ""))
    if video_sn is None:
        return None
    return ScheduleEntry(
        video_sn=video_sn,
        title=_text(a, ".text-anime-name"),
        time_text=_text(a, ".text-anime-time"),
        episode_text=_text(a, ".text-anime-number"),
    )


def _text(el, selector: str) -> str:
    found = el.select_one(selector)
    return found.get_text(strip=True) if found is not None else ""


def _extract_sn(href: str) -> int | None:
    values = parse_qs(urlparse(href).query).get("sn")
    if not values:
        return None
    try:
        return int(values[0])
    except ValueError:
        return None
