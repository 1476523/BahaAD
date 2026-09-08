"""監視公告（gossip）：抓取／解析／比對／分類 + `GossipWatcher` 背景執行緒。

規格見 docs/requirements/scheduler_gossip_watch.md。

- 第 3 階段（純函式）：`fetch_gossip_text()`／季別標記／`parse_gossip_text()`／
  `classify_status()`／`match_title_to_sn()`——依賴全注入，能完全用假物件單元測試
- 第 4 階段（`GossipWatcher`）：背景執行緒本體（`check_once()`／`start()`／`stop()`
  慣例）、`notify/` 五個類別整合、`apply_disposition()`（建 `gossip_pending` 臨時排程
  覆蓋）、每日新番彙整通知

「gossip」是動畫瘋首頁 `class="gossip"` 公告欄位的站方 CSS class 命名，不是本專案取的
名字。站方在那裡貼「《作品》因版權因素暫停更新」「《作品》延後至 20:00」這類臨時異動。

**參考舊專案的例外聲明**：本模組的解析／比對／分類演算法拿使用者自己在「客製強化版」
`Gossip.py`（v25.2.0/v25.2.3/v25.2.6 修過好幾輪 bug）的規則當基礎，依 BahaAD 架構重新
實作（依賴注入、frozen dataclass、代碼表常數來自 `store/gossip.py`），不是逐行搬程式碼。
使用者已同意（2026-08-25，見規格文件開頭聲明）。

**作品比對規則**：一律以 sn 是否有交集判斷是不是同一部作品，**不比對名稱字面**——公告／
搜尋結果／`schedule_entries` 三邊的名稱經常不一致（加了年齡限制標記、用「2nd Season」
代替「第二季」等）。只在「從搜尋結果挑候選」這一步才用季別標記做粗篩。
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from time import monotonic
from typing import Callable, Iterable, Protocol

from bs4 import BeautifulSoup

from bahaad.downloader.naming import DEFAULT_PAD_WIDTH as _DEFAULT_PAD_WIDTH, episode_zh
from bahaad.gamer_client.activation import skip_if_busy
from bahaad.gamer_client.browse import SearchResult, resolve_ref_sn, search_anime
from bahaad.notify.dispatch import send_notification
from bahaad.scheduler import recheck_pause
from bahaad.store.gossip import (
    ACTIONS,
    DISPOSITION_OPTIONS,
    STATUS_LABELS,
    STATUS_TO_SUGGESTION,
    GossipStore,
    compute_source_hash,
)
from bahaad.store.notify import NotifyStore
from bahaad.store.schedule_list import ScheduleListStore
from bahaad.store.settings import SettingsStore
from bahaad.subscription_ops import subscribe_sn

logger = logging.getLogger(__name__)

_HOME_URL = "https://ani.gamer.com.tw/"


class HttpGetter(Protocol):
    def get(self, url: str, params: dict | None = None): ...


# ===========================================================================
# 1. 抓取首頁 gossip 公告
# ===========================================================================


# 首頁公告欄裡的裸網址（不一定包在 <a> 裡）——新番快訊偵測要抓 gnn.gamer.com.tw 連結
_GOSSIP_URL_RE = re.compile(r"https?://[^\s，。）)]+")


@dataclass(frozen=True)
class GossipSnapshot:
    """首頁 `.gossip` 的一次快照：公告文字 + 裡面出現的連結（`<a href>` ＋ 裸網址）。"""

    text: str  # `""`＝頁面正常但沒公告
    links: tuple[str, ...]


def fetch_gossip(http: HttpGetter) -> GossipSnapshot | None:
    """抓首頁 `class="gossip"` 欄位：文字 ＋ 連結。

    - **`None`＝這次抓不到**（非 200、逾時、連線失敗、解析例外）。呼叫端要把這當「不知道
      現在有沒有公告」，**不能**當成「公告欄空了」——不然一次暫時性的網路失敗會讓
      `_expire_vanished_events` 把使用者所有生效中的處置都清掉並鎖定（見 check_once）。
    - **`text=""`＝頁面正常載入、但沒有公告**（站方目前沒掛任何 `.gossip`）。這才是「公告欄空了」。
    - `text` 非空＝公告原文；`links` 是公告裡的連結（新番快訊偵測用）。"""
    try:
        response = http.get(_HOME_URL)
        if getattr(response, "status_code", 200) != 200:
            return None
        return _parse_gossip(response.text)
    except Exception as exc:  # noqa: BLE001 — 背景盡力而為，任何失敗都只記警告
        logger.warning("抓取首頁公告失敗：%s", exc)
        return None


def _parse_gossip(html: str) -> GossipSnapshot:
    soup = BeautifulSoup(html, "html.parser")
    element = soup.find(class_="gossip")
    if element is None:
        return GossipSnapshot(text="", links=())  # 頁面 OK、就是沒公告
    inner = element.find("span")
    text = (inner.get_text() if inner is not None else element.get_text()).strip()
    links: list[str] = []
    for anchor in element.find_all("a"):
        href = (anchor.get("href") or "").strip()
        if href:
            links.append(href[2:] if href.startswith("//") else href)  # `//gnn…` → `gnn…`
    links.extend(_GOSSIP_URL_RE.findall(text))
    return GossipSnapshot(text=text, links=tuple(dict.fromkeys(links)))


def fetch_gossip_text(http: HttpGetter) -> str | None:
    """向後相容的薄包裝——只要公告文字（`None`／`""`／原文的語意同 `fetch_gossip`）。"""
    snapshot = fetch_gossip(http)
    return None if snapshot is None else snapshot.text


# ===========================================================================
# 2. 季別標記（用於從搜尋結果篩選候選，不用於最終比對）
# ===========================================================================

_ZH_DIGIT = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_ZH_DIGITS = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九")

_SEASON_ZH_PATTERN = re.compile(r"第([一二三四五六七八九十]{1,3})季")
# 英文季別兩種寫法：「2nd Season」（公告愛用）與「Season 2」（動畫瘋站上作品名愛用，
# 例：SPY×FAMILY 間諜家家酒 Season 2）——兩種都要認得，不然站上「Season N」的候選會被
# season_number() 當成沒季別（=第一季）而被 same_tier 粗篩誤刪。
_SEASON_EN_PATTERN = re.compile(
    r"(?:(\d{1,2})(?:st|nd|rd|th)\s*Season|Season\s*(\d{1,2}))", re.IGNORECASE
)
# 不能用 \bS(\d{1,2})\b——Python 的 \w 在 Unicode 字串裡含中文字，「史萊姆S4」的「姆」跟
# 「S」兩邊都是 \w、\b 找不到邊界，「中文名稱直接接 S4 無空格」這種公告常見寫法會偵測不到
_SEASON_S_PATTERN = re.compile(r"(?<![A-Za-z0-9])S(\d{1,2})(?![A-Za-z0-9])")
# 標題尾端的季別標記，供 build_search_title() 去掉季別、只留基礎名稱；錨定在字串尾端，
# 避免誤刪標題中間本來就含這些字樣的部分
_SEASON_SUFFIX_PATTERN = re.compile(
    r"\s*(?:第[一二三四五六七八九十]{1,3}季|\d{1,2}(?:st|nd|rd|th)\s*Season|S\d{1,2})\s*$",
    re.IGNORECASE,
)


def _zh_num_to_int(s: str) -> int | None:
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        head, _, tail = s.partition("十")
        tens = _ZH_DIGIT.get(head, 1) if head else 1
        ones = _ZH_DIGIT.get(tail, 0) if tail else 0
        return tens * 10 + ones
    return _ZH_DIGIT.get(s)


def _int_to_zh_num(n: int | None) -> str | None:
    if n is None or n <= 0:
        return None
    if n < 10:
        return _ZH_DIGITS[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _ZH_DIGITS[n - 10]
    tens, ones = divmod(n, 10)
    return _ZH_DIGITS[tens] + "十" + (_ZH_DIGITS[ones] if ones else "")


def season_number(title: str) -> int | None:
    """回傳標題「明確寫出」的季別數字（中文「第N季」／英文「Nth Season」／「SN」縮寫），
    沒寫季別回傳 `None`（視為第一季／無季別）。"""
    zh = _SEASON_ZH_PATTERN.search(title)
    if zh:
        return _zh_num_to_int(zh.group(1))
    en = _SEASON_EN_PATTERN.search(title)
    if en:
        return int(en.group(1) or en.group(2))
    s = _SEASON_S_PATTERN.search(title)
    if s:
        return int(s.group(1))
    return None


def season_compatible(a: int | None, b: int | None) -> bool:
    """沒寫季別（`None`）一律視為第一季；只有雙方都「明確寫出」且數字不同才算不相容。
    這步是為了避免沒寫季別的公告標題被誤配到追蹤清單裡帶季別的項目（例如《相反的你和我》
    被誤配到「相反的你和我 第二季」）。"""
    return (1 if a is None else a) == (1 if b is None else b)


def strip_season_suffix(title: str) -> str:
    """去掉標題尾端的季別標記（「第N季」／「Nth Season」／「SN」），**不**重組回中文
    季別——純去除，給只想要「不管有沒有季別都比對得到」的呼叫端用（見
    `youranimes/match.py`，使用者 2026-09-05：標題比對容錯）。"""
    return _SEASON_SUFFIX_PATTERN.sub("", title).strip() or title


def season_number_to_zh(n: int | None) -> str | None:
    """`season_number()` 偵測到的數字 → 中文數字（`_int_to_zh_num` 的公開包一層，給
    `youranimes/match.py` 這種模組外呼叫端用，不用伸手拿底線開頭的內部函式）。"""
    return _int_to_zh_num(n)


def build_search_title(title: str) -> tuple[str, int | None]:
    """回傳 `(搜尋關鍵字, 公告季別)`。公告標題常用英數字季別縮寫（S2／2nd Season），但
    動畫瘋站上正式作品名一律用中文「第N季」，`schedule_entries` 也照這個慣例——縮寫直接
    拿去搜尋幾乎搜不到結果（舊專案實測發現）。所以先去縮寫取基礎名稱，再依偵測到的季別
    數字重新組出「基礎名稱 第N季」。第一季／沒有季別標記則不附加（多數作品第一季在站上
    不會特別標「第一季」）。"""
    gossip_season = season_number(title)
    base_title = strip_season_suffix(title)
    if gossip_season is None or gossip_season <= 1:
        return base_title, gossip_season
    season_zh = _int_to_zh_num(gossip_season)
    return (f"{base_title} 第{season_zh}季" if season_zh else base_title), gossip_season


def _search_title_variants(title: str) -> list[str]:
    """`match_title_to_sn()` 依序嘗試的搜尋關鍵字（前一個回空才試下一個，見階段 8-1，
    使用者 2026-08-27：先單純搜名稱、有結果再靠季別粗篩挑對季）：

    1. **去季別的基礎名稱**——最穩，站上這個系列存在就撈得到（然後靠 `same_tier`
       的 `season_compatible` 粗篩＋集數集合交集挑出對的那一季）
    2. 重組的「基礎名稱 第N季」（縮寫 S2／2nd Season → 中文「第二季」，`build_search_title`）
    3. 每個字加空格（站方的模糊查法，實測能撈到前兩段撈不到的）

    重複的段落自動略過（沒有季別時 1、2 相同）。"""
    search_title, _ = build_search_title(title)
    base = strip_season_suffix(title)
    spaced = " ".join(ch for ch in search_title if not ch.isspace())
    variants: list[str] = []
    for candidate in (base, search_title, spaced):
        if candidate and candidate not in variants:
            variants.append(candidate)
    return variants


_YEAR_PATTERN = re.compile(r"(\d{4})/(\d{2})")


def _year_key(year_text: str) -> tuple[int, int]:
    """把 `SearchResult.year_text`（"年份：2026/07"）拆成 `(2026, 7)` 供排序；拆不出來
    回傳 `(0, 0)`（排最後）。"""
    match = _YEAR_PATTERN.search(year_text or "")
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


# ===========================================================================
# 3. 搜尋 + 解析成 sn，與追蹤清單做 sn 交集比對（不比對名稱）
# ===========================================================================


@dataclass(frozen=True)
class MatchResult:
    sn: int | None
    method: str  # "sn_overlap" / "unmatched" / "search_failed"
    candidates: tuple[SearchResult, ...]


def match_title_to_sn(
    title: str,
    tracked_sns: Iterable[int],
    *,
    search: Callable[[str], list[SearchResult]],
    resolve_ref_sn: Callable[[int], int | None],
    episode_set_for: Callable[[int], frozenset[int]],
) -> MatchResult:
    """把公告標題比對到一個追蹤中的 sn。流程（沿用舊專案定案，理由是實測驗證過的真實問題）：

    1. `build_search_title()` 重組搜尋關鍵字（縮寫 → 「基礎名稱 第N季」）
    2. `search()` 拿候選清單，用季別粗篩（沒寫季別視為第一季）
    3. 依年份新到舊逐一嘗試：候選的 `ref_sn` 走 `resolve_ref_sn()` 拿真正的 video_sn，
       `episode_set_for()` 拿該季全部集數的 sn 集合，跟每個追蹤項目的集數集合比對交集
    4. 找到交集就是同一部作品；逐一試完都沒交集就是 `unmatched`

    依賴（`search`／`resolve_ref_sn`／`episode_set_for`）都用注入的方式傳進來，讓這支
    函式能完全用假物件單元測試；第 4 階段再接上 `browse.search_anime`／`browse.resolve_ref_sn`／
    帶 1 小時快取的 `catalog.get_video` 集數查詢。
    """
    _, gossip_season = build_search_title(title)
    # 多段 fallback（實測 2026-08-27，見 web_redesign_round3.md 階段 8-1）：先單純搜
    # 去季別的名稱（最穩），回空才退到「基礎名稱 第N季」、再空退到「每個字加空格」。
    # 對的那一季由下面的 season_compatible 粗篩＋集數集合交集挑出來。
    candidates: tuple[SearchResult, ...] = ()
    for query in _search_title_variants(title):
        try:
            candidates = tuple(search(query))
        except Exception as exc:  # noqa: BLE001
            logger.warning("搜尋《%s》失敗：%s", query, exc)
            return MatchResult(None, "search_failed", ())
        if candidates:
            break

    if gossip_season is None:
        # 公告沒寫季別（「《作品名》本週停播」這種最常見的寫法）→ 不做季別粗篩，全部
        # 候選都交給下面的「集數集合交集」判斷（那是精確訊號：不同季別的集數 video_sn
        # 不會交集，不會誤配到別部或別季）。用使用者 2026-08-27 回饋定案的做法。
        same_tier = list(candidates)
    else:
        # 公告有明確季別 → 用它粗篩掉季別不合的候選（例：公告寫「第二季」、候選只有
        # 第一季 → 直接判 unmatched，不去試集數，避免慢＋避免萬一集數資料有問題誤配）
        same_tier = [
            c for c in candidates if season_compatible(gossip_season, season_number(c.title))
        ]
    if not same_tier:
        return MatchResult(None, "unmatched", candidates)

    tracked = list(dict.fromkeys(tracked_sns))
    # 依年份新到舊逐一嘗試，不是只看排名最前面那個——同一季別可能同時列出好幾筆
    # （重製版／合輯／其他同名作品），最新那筆不一定是使用者實際在追蹤的
    for chosen in sorted(same_tier, key=lambda c: _year_key(c.year_text), reverse=True):
        try:
            episode_sn = resolve_ref_sn(chosen.ref_sn)
            if episode_sn is None:
                continue
            candidate_set = episode_set_for(episode_sn)
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析候選《%s》(ref_sn=%s) 集數失敗：%s", chosen.title, chosen.ref_sn, exc)
            continue
        for tracked_sn in tracked:
            try:
                if candidate_set & episode_set_for(tracked_sn):
                    return MatchResult(tracked_sn, "sn_overlap", candidates)
            except Exception as exc:  # noqa: BLE001
                logger.warning("解析追蹤項目 sn=%s 集數失敗：%s", tracked_sn, exc)
                continue
    return MatchResult(None, "unmatched", candidates)


# ===========================================================================
# 4. 公告文字解析（規則式，覆蓋常見措辭；無法辨識的一律 manual_review 交給人工）
# ===========================================================================

_TITLE_PATTERN = re.compile(r"《([^《》]+)》")
_TIME_PATTERN = re.compile(r"(\d{1,2}):(\d{2})")
_DATE_PATTERN = re.compile(r"(\d{1,2})/(\d{1,2})")
# 集數：日式「話」跟台式「集」都收（動畫瘋公告兩種都出現過）
_EPISODE_PATTERN = re.compile(r"第\s*(\d+)\s*[話集]")
_RESUME_PATTERN = re.compile(r"(?:將於|回復|恢復)\D{0,6}(\d{1,2})/(\d{1,2})")

_PAUSE_WORDS = ("暫停", "停播", "停止更新")
_TAKEDOWN_WORDS = ("下架",)
_MERGE_WORDS = ("同時", "一併", "合併")
_EXTRA_EP_WORDS = ("更新兩集", "更新2集", "加更兩集", "更新兩話", "加更兩話")
_EARLY_WORDS = ("提前", "提早")

# 公告常見沒給明確 HH:MM，只用「明日白天」「今晚」這種相對日期詞 + 模糊時段詞交代更新
# 時間。抓出這兩種詞、算出一個（粗略但夠用的）日期 + 時間範圍，讓這類公告也能走「延後
# 更新」判斷、直接套用建議處置，不用整個掉進 manual_review 讓使用者自己換算
_RELATIVE_DATE_WORDS = {
    "明日": 1, "明天": 1, "後天": 2, "後日": 2, "大後天": 3,
    "今日": 0, "今天": 0, "今晚": 0,
}
_DAYPART_WORDS = {
    "凌晨": ("00:00", "06:00"),
    "早上": ("06:00", "09:00"),
    "上午": ("06:00", "12:00"),
    "中午": ("11:00", "13:00"),
    "下午": ("13:00", "18:00"),
    "傍晚": ("17:00", "19:00"),
    "晚上": ("18:00", "24:00"),
    "深夜": ("23:00", "06:00"),  # 跨午夜，消費端自行處理 end <= start
    "白天": ("08:00", "18:00"),
}


@dataclass(frozen=True)
class GossipSubEvent:
    """一則公告拆出的一個子事件（一句可能提到好幾部作品 → 好幾個子事件）。`title` 為
    `None` 代表這句沒有 `《》` 作品名（`action='log_only'`，純紀錄不處理）。"""

    raw_clause: str
    action: str
    title: str | None = None
    time_text: str | None = None
    explicit_date: tuple[int, int] | None = None
    fuzzy_window: tuple[str, str] | None = None
    fuzzy_relative_days: int | None = None
    episode_labels: tuple[str, ...] = ()
    expect_episode_count: int = 1
    early_hint: bool = False
    tentative: bool = False
    resume_date: tuple[int, int] | None = None
    confidence: str = "medium"  # "medium" / "low" / "n/a"


def split_clauses(text: str) -> list[str]:
    """用全形／半形句號、分號、換行拆句——公告常見「暫停一件事；延後另一件事」這種用
    分號並列不同動作的寫法，不拆開的話兩件事會被套用同一組關鍵字判斷、互相污染分類。"""
    return [part.strip() for part in re.split(r"[。；;\n]", text) if part.strip()]


def _title_time_pairs(clause: str) -> list[tuple[str, str | None]]:
    titles = [(m.start(), m.group(1)) for m in _TITLE_PATTERN.finditer(clause)]
    times = [(m.start(), m.group(0)) for m in _TIME_PATTERN.finditer(clause)]
    if len(titles) == 1:
        # 整句只有一部作品時，時間不論離標題多遠都算它的（公告常見「《作品》因為 XXX
        # 原因，預計延至 HH:MM」這種長敘述）
        return [(titles[0][1], times[0][1] if times else None)]
    pairs: list[tuple[str, str | None]] = []
    for pos, title in titles:
        best, best_dist = None, None
        for tpos, tstr in times:
            dist = abs(tpos - pos)
            if dist <= 30 and (best_dist is None or dist < best_dist):
                best, best_dist = tstr, dist
        pairs.append((title, best))
    return pairs


def _nearby_date(clause: str, title: str, single_title: bool) -> tuple[int, int] | None:
    date_matches = list(_DATE_PATTERN.finditer(clause))
    if not date_matches:
        return None
    if single_title:
        return (int(date_matches[0].group(1)), int(date_matches[0].group(2)))
    title_pos = clause.find(f"《{title}》")
    best, best_dist = None, None
    for dm in date_matches:
        dist = abs(dm.start() - title_pos)
        if dist <= 30 and (best_dist is None or dist < best_dist):
            best, best_dist = (int(dm.group(1)), int(dm.group(2))), dist
    return best


def parse_gossip_text(text: str) -> list[GossipSubEvent]:
    """把整份公告文字拆解成子事件清單。不做 NLP／AI，用關鍵詞／正則規則覆蓋常見措辭，
    **無法辨識的一律 `action='manual_review'`、`confidence='low'`**，交給操作處置頁人工
    判斷，不猜測。狀態關鍵詞的判斷優先序：下架 → 暫停 → 有明確時間/日期（再依「同時/
    加更/提前」細分）→ 只有相對日期詞 + 模糊時段詞 → 都沒有就 manual_review。"""
    events: list[GossipSubEvent] = []
    for clause in split_clauses(text):
        titles_in_clause = _TITLE_PATTERN.findall(clause)
        if not titles_in_clause:
            events.append(GossipSubEvent(raw_clause=clause, action="log_only", confidence="n/a"))
            continue

        has_pause = any(w in clause for w in _PAUSE_WORDS)
        has_takedown = any(w in clause for w in _TAKEDOWN_WORDS)
        # 「同時段／同一時段更新」是「更新日改了、時刻不變」（detect_reschedule 的活兒），
        # 不是「本週同時更新兩集」——別讓 "同時" 子字串把它誤判成加更
        _merge_clause = clause.replace("同時段", "").replace("同一時段", "").replace("同時間", "").replace("同一時間", "")
        has_merge = any(w in _merge_clause for w in _MERGE_WORDS)
        has_extra_ep = any(w in clause for w in _EXTRA_EP_WORDS)
        has_early = any(w in clause for w in _EARLY_WORDS)
        has_tentative = "暫定" in clause
        episodes = _EPISODE_PATTERN.findall(clause)
        multi_episode = has_merge or has_extra_ep or len(episodes) >= 2
        single_title = len(titles_in_clause) == 1
        # 用最長匹配而非字典順序：「大後天」含「後天」子字串，照迭代順序會先被「後天」
        # 命中（少算一天），取最長才會正確選到「大後天」
        relative_word = max((w for w in _RELATIVE_DATE_WORDS if w in clause), key=len, default=None)
        daypart_word = next((w for w in _DAYPART_WORDS if w in clause), None)
        resume_match = _RESUME_PATTERN.search(clause)
        resume_date = (int(resume_match.group(1)), int(resume_match.group(2))) if resume_match else None
        episode_labels = tuple(f"第{e}話" for e in episodes)

        for title, time_str in _title_time_pairs(clause):
            explicit_date = _nearby_date(clause, title, single_title)
            common = dict(
                raw_clause=clause,
                title=title,
                time_text=time_str,
                explicit_date=explicit_date,
                early_hint=has_early,
                tentative=has_tentative,
                episode_labels=episode_labels,
            )

            if has_takedown:
                events.append(GossipSubEvent(action="takedown", expect_episode_count=1, **common))
            elif has_pause:
                events.append(
                    GossipSubEvent(action="pause", expect_episode_count=1, resume_date=resume_date, **common)
                )
            elif time_str or explicit_date:
                events.append(
                    GossipSubEvent(
                        action="delay",
                        expect_episode_count=2 if multi_episode else 1,
                        **common,
                    )
                )
            elif daypart_word:
                # 沒有明確 HH:MM／MM:DD，但抓到「明日」「白天」這類相對日期詞 + 模糊時段
                # 詞：當延後更新處理，目標時間是一段範圍而非單一時刻
                events.append(
                    GossipSubEvent(
                        action="delay",
                        fuzzy_window=_DAYPART_WORDS[daypart_word],
                        fuzzy_relative_days=_RELATIVE_DATE_WORDS.get(relative_word),
                        expect_episode_count=2 if multi_episode else 1,
                        **common,
                    )
                )
            elif multi_episode:
                # 沒給明確時間，但有「同時／合併／加更兩集」或一次列出兩集以上：視為「本週
                # 同時更新」（照常規時間檢查，只是這次不只一集），不需要額外時間資訊
                events.append(GossipSubEvent(action="extra_episode", expect_episode_count=2, **common))
            else:
                events.append(GossipSubEvent(action="manual_review", confidence="low", **common))
    return events


# ---------------------------------------------------------------------------
# .0 改進.txt 第 25 項例外：每週更新時間「永久」變更公告
# ---------------------------------------------------------------------------

_ZH_WEEKDAY = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}
_RESCHEDULE_WEEKDAY_RE = re.compile(r"每週([一二三四五六日天])")
# 「20:30」「晚間20:30」「晚上8點30分」「8點」——先抓 HH:MM，再退而求其次抓「N點M分」
_RESCHEDULE_HHMM_RE = re.compile(r"(\d{1,2})[:：](\d{2})")
_RESCHEDULE_ZH_TIME_RE = re.compile(r"(\d{1,2})\s*點\s*(\d{1,2})?\s*分?")
_RESCHEDULE_DATE_RE = re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日")
# 變更動詞：「調整／改為／變更／異動…至/為 每週N」。一次性延後也會用這些詞，所以還要
# 搭配「固定/永久」或「起」或「同時段」才算永久變更（見 _is_reschedule_detail）。
_RESCHEDULE_CHANGE_WORDS = (
    "調整至", "調整為", "調整成", "改為", "改成", "更改為", "變更為",
    "變更至", "異動至", "異動為", "移至", "挪至", "調至", "調動至", "調整",
)
# 「同時段／原時段更新」＝星期改了、時刻不變（沿用目前排程的時刻，不用公告寫出時間）。
# 注意：「同時段」含「同時」子字串，不能被 _MERGE_WORDS 誤判成「本週同時更新兩集」。
_RESCHEDULE_SAME_SLOT_WORDS = (
    "同時段", "同一時段", "原時段", "相同時段", "時段不變", "維持原時段",
    "同一時間", "同時間", "原時間", "時間不變",
)
# 一次性、不是永久變更的否定詞——出現就不當 reschedule
_RESCHEDULE_ONE_OFF_WORDS = ("僅本週", "只有本週", "僅此一次", "僅限本週", "本週限定", "僅這週", "這一集", "這一話")


@dataclass(frozen=True)
class RescheduleNotice:
    title: str
    weekday: int  # 1=週一…7=週日
    hour: int | None = None  # None ＝「同時段」，沿用目前排程的時刻、只換星期
    minute: int | None = None
    effective_date: str | None = None  # ISO，供顯示用

    @property
    def same_slot(self) -> bool:
        return self.hour is None


@dataclass(frozen=True)
class _RescheduleNotifySub:
    """`_notify_gossip()` 只讀 `action`／`raw_clause`／`title`——更新時間變更通知用這個
    輕量物件餵它，不必硬湊一個完整的 `GossipSubEvent`。"""

    title: str
    raw_clause: str
    action: str = "reschedule"


def _pm_hint(text: str, time_pos: int) -> bool:
    window = text[max(0, time_pos - 8): time_pos]
    return any(w in window for w in ("晚間", "晚上", "下午", "傍晚", "深夜", "PM", "pm"))


def _reschedule_time(clause: str) -> tuple[int, int] | None:
    hhmm = _RESCHEDULE_HHMM_RE.search(clause)
    if hhmm is not None:
        hour, minute = int(hhmm.group(1)), int(hhmm.group(2))
    else:
        zh = _RESCHEDULE_ZH_TIME_RE.search(clause)
        if zh is None:
            return None
        hour = int(zh.group(1))
        minute = int(zh.group(2)) if zh.group(2) else 0
        if hour < 12 and _pm_hint(clause, zh.start()):
            hour += 12
    return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None


def _parse_reschedule_target(target_time: str | None) -> tuple[int | None, int | None, int | None]:
    """`gossip_events.target_time` 對 reschedule 事件存的是 `"W/HH:MM"` 或 `"W/同時段"`
    （W=1..7）。拆回 `(weekday, hour, minute)`；「同時段」的 hour/minute 是 None
    （呼叫端沿用目前排程時刻）。拆不出來回 `(None, None, None)`。"""
    raw = (target_time or "").strip()
    weekday_str, _, rest = raw.partition("/")
    try:
        weekday = int(weekday_str)
    except ValueError:
        return None, None, None
    if not 1 <= weekday <= 7:
        return None, None, None
    if rest in ("", "同時段"):
        return weekday, None, None
    hhmm = _parse_hhmm(rest)
    if hhmm is None:
        return weekday, None, None
    return weekday, hhmm[0], hhmm[1]


def _is_reschedule_detail(clause: str) -> bool:
    """這句是不是「每週更新時間永久變更」的細節句。要件：
      - 有「每週N」（新的固定更新日；「本週」一次性延後不會用「每週」）
      - 沒有「僅本週／這一集」這種一次性否定詞
      - 有「永久性」訊號：`固定`／`永久`，或（變更動詞 ＋ `起`），或（變更動詞 ＋ `同時段`）
      - 有一個新時間，**或**寫「同時段／原時段」（星期改、時刻不變）
    """
    if not _RESCHEDULE_WEEKDAY_RE.search(clause):
        return False
    if any(w in clause for w in _RESCHEDULE_ONE_OFF_WORDS):
        return False
    has_change_verb = any(w in clause for w in _RESCHEDULE_CHANGE_WORDS)
    has_same_slot = any(w in clause for w in _RESCHEDULE_SAME_SLOT_WORDS)
    permanent = (
        "固定" in clause
        or "永久" in clause
        or (has_change_verb and "起" in clause)
        or (has_change_verb and has_same_slot)
    )
    if not permanent:
        return False
    return _reschedule_time(clause) is not None or has_same_slot


def detect_reschedule(text: str | None) -> RescheduleNotice | None:
    """每週更新時間「永久」變更公告。常見兩種寫法：
      - 「《作品》上架時間調整通知」＋「自 X 起固定於每週四 20:30 上架」（標題／細節分兩句）
      - 「《作品》本週起調整至 每週四 同時段更新」（一句講完，時刻不變）

    做法（**逐句**，避免不同公告的片段被 `.search()` 混在一起，見 code review）：
    1. 找符合 `_is_reschedule_detail()` 的那句 → 這是「變更細節」那句。
    2. 標題：細節那句自己有一個 `《》` 就用它；否則往前找最近一句「剛好一個 `《》`」的。
       往回撞到「多個 `《》`」的句子 → 放棄（無法確定講哪一部）。
    3. 星期只從細節那句取；時間細節那句有寫就取、寫「同時段」就留 None（沿用目前排程時刻）；
       生效日期跨細節句＋標題句找。

    少關鍵要件就回 None，交給一般逐句解析（寧可漏抓、不誤判成永久變更去動使用者的排程）。"""
    if not text:
        return None
    clauses = split_clauses(text)
    detail_idx = next((i for i, c in enumerate(clauses) if _is_reschedule_detail(c)), None)
    if detail_idx is None:
        return None
    detail = clauses[detail_idx]

    title = None
    title_clause = detail
    for c in [detail, *reversed(clauses[:detail_idx])]:
        found = _TITLE_PATTERN.findall(c)
        if len(found) > 1:
            return None
        if len(found) == 1:
            title, title_clause = found[0], c
            break
    if title is None:
        return None

    weekday = _ZH_WEEKDAY[_RESCHEDULE_WEEKDAY_RE.search(detail).group(1)]
    parsed_time = _reschedule_time(detail)
    hour, minute = parsed_time if parsed_time is not None else (None, None)

    effective = None
    dm = _RESCHEDULE_DATE_RE.search(detail) or _RESCHEDULE_DATE_RE.search(title_clause)
    if dm is not None:
        y, mo, d = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            effective = f"{y:04d}-{mo:02d}-{d:02d}"

    return RescheduleNotice(
        title=title, weekday=weekday, hour=hour, minute=minute, effective_date=effective
    )


# ===========================================================================
# 5. 狀態判斷（六種狀態 + 目標日期／時間）
# ===========================================================================


@dataclass(frozen=True)
class ClassifiedStatus:
    status_label: str  # STATUS_LABELS
    target_date: date | None
    target_time: str | None
    target_window: tuple[str, str] | None = None


def baseline_from_schedule(
    schedule_weekday: int | None,
    schedule_hour: int | None,
    schedule_minute: int | None,
    now: datetime,
) -> datetime | None:
    """從 `schedule_entries` 的自訂時段算出「這部作品平常大概什麼時候更新」，當提前／延後
    判斷的基準時間。沒有自訂時段（三個欄位任一為 `None`）回傳 `None`——v1 就到此為止，
    不做舊專案那種「查最近一次下載集數的網頁上架時間」的爬蟲備援（見規格「刻意縮小的
    範圍」），沒有基準時間時 `classify_status()` 一律預設當延後。"""
    if schedule_weekday is None or schedule_hour is None or schedule_minute is None:
        return None
    days_ahead = (schedule_weekday - now.isoweekday()) % 7
    return (now + timedelta(days=days_ahead)).replace(
        hour=schedule_hour, minute=schedule_minute, second=0, microsecond=0
    )


def resolve_date(
    mm_dd: tuple[int, int] | None, reference: datetime, time_str: str | None = None
) -> date:
    """把公告裡的日期線索解析成一個具體日期。

    - 沒有 MM/DD：用 `reference` 當天；若同時給了 `time_str` 且「今天這個時刻」已經是
      過去式（常見於「今晚...凌晨 X 點更新」——「今晚」中文語感涵蓋隔天凌晨，但時鐘已
      跨午夜），順延一天，否則算出的目標時間落在過去、覆蓋排程一到點就誤判過期
    - 有 MM/DD：用當前年份組日期；組出來若已是過去式（不論差幾天），唯一合理解釋是指
      明年同一天（年初讀到「12/28」這種上個月殘留字串，或解析時間點稍晚）——不修正的話
      `get_actionable()` 的 `target_date >= today` 會直接把這筆濾掉，使用者永遠看不到也修不了
    """
    if mm_dd is None:
        d = reference.date()
        if time_str:
            parsed = _parse_hhmm(time_str)
            if parsed is not None and datetime.combine(d, time(*parsed)) < reference:
                d = d + timedelta(days=1)
        return d
    month, day = mm_dd
    try:
        d = date(reference.year, month, day)
    except ValueError:
        return reference.date()
    if d < reference.date():
        try:
            d = date(reference.year + 1, month, day)
        except ValueError:
            return reference.date()
    return d


def resolve_fuzzy_date(
    relative_days: int | None, window: tuple[str, str], reference: datetime
) -> date:
    """相對日期詞（明日／今天等）有寫出時直接用它算；沒寫（公告只寫「白天更新」沒說哪天）
    的話用時段結束時刻判斷「今天」這個時段是否已過，過去式就順延一天。跨午夜時段（例如
    「深夜」23:00~06:00）一律錨定在今天，讓消費端自己對結束時刻順延，這裡不能再多順延
    一次否則整整晚一天。"""
    if relative_days is not None:
        return (reference + timedelta(days=relative_days)).date()
    start_hm = _parse_hhmm(window[0]) or (0, 0)
    end_hm = (23, 59) if window[1] == "24:00" else (_parse_hhmm(window[1]) or (23, 59))
    if end_hm <= start_hm:
        return reference.date()  # 跨午夜時段：一律錨定今天
    if datetime.combine(reference.date(), time(*end_hm)) < reference:
        return (reference + timedelta(days=1)).date()
    return reference.date()


def classify_status(
    event: GossipSubEvent, *, now: datetime, baseline_time: datetime | None = None
) -> ClassifiedStatus:
    """把一個子事件判斷成六種狀態之一，並算出目標日期／時刻／時段。`baseline_time` 是
    這部作品平常的更新時間（由 `baseline_from_schedule()` 算好注入），只用在「有明確時刻
    但沒有其他線索」時判斷提前 vs 延後；沒有基準時間時一律預設當延後（公告絕大多數情況
    是延後，使用者仍可在操作處置頁自行改）。"""
    action = event.action

    if action == "takedown":
        return ClassifiedStatus("暫時下架", resolve_date(event.explicit_date, now), None)
    if action == "pause":
        return ClassifiedStatus("暫停更新", resolve_date(event.explicit_date, now), None)
    if action == "extra_episode":
        d = resolve_date(event.explicit_date, now, event.time_text)
        return ClassifiedStatus("同時更新", d, event.time_text)
    if action != "delay":
        return ClassifiedStatus("無法判斷", None, None)

    # --- action == "delay" ---
    if event.fuzzy_window is not None:
        d = resolve_fuzzy_date(event.fuzzy_relative_days, event.fuzzy_window, now)
        if event.expect_episode_count >= 2:
            return ClassifiedStatus("同時更新", d, None, event.fuzzy_window)
        if event.early_hint:
            return ClassifiedStatus("提前更新", d, None, event.fuzzy_window)
        return ClassifiedStatus("延後更新", d, None, event.fuzzy_window)

    d = resolve_date(event.explicit_date, now, event.time_text)
    if event.expect_episode_count >= 2:
        return ClassifiedStatus("同時更新", d, event.time_text)
    if event.early_hint:
        return ClassifiedStatus("提前更新", d, event.time_text)
    if baseline_time is not None and event.time_text:
        parsed = _parse_hhmm(event.time_text)
        if parsed is not None:
            target_dt = datetime.combine(d, time(*parsed))
            return ClassifiedStatus(
                "提前更新" if target_dt < baseline_time else "延後更新", d, event.time_text
            )
    if event.time_text or event.explicit_date:
        return ClassifiedStatus("延後更新", d, event.time_text)
    return ClassifiedStatus("無法判斷", None, None)


_HHMM_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})$")


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    match = _HHMM_PATTERN.match(text.strip())
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


# ===========================================================================
# 6. GossipWatcher 背景執行緒（第 4 階段）
# ===========================================================================

_DEFAULT_CHECK_INTERVAL_MINUTES = 1  # 使用者 2026-09-05：比舊版慢，改成每分鐘一次（原本 2 分）
_DEFAULT_RETENTION_DAYS = 30
# `poke()` 觸發的即時檢查最短間隔——使用者 2026-09-04 要「只要有到動畫瘋的程序都順便查
# 公告」，但一連串下載若每次都打首頁很容易被 429（見規格「429 風險」），節流到 2 分鐘。
_DEFAULT_MIN_POKE_INTERVAL_SECONDS = 120.0
_EPISODE_SET_CACHE_TTL_SECONDS = 3600  # 集數集合快取 1 小時，見規格「集數快取有效期」

_NOTIFY_CATEGORY = {
    "pause": "gossip_pause",
    "takedown": "gossip_takedown",
    "delay": "gossip_delay",
    "extra_episode": "gossip_extra_episode",
}

# 「本週*」建立臨時排程覆蓋時，`skip_check` 對應的兩個處置
_SKIP_DISPOSITIONS = ("本週暫停更新", "本週下架")
_WINDOW_RETRY_INTERVAL_MINUTES = 1  # 範圍型覆蓋：範圍內每分鐘複查一次


class EpisodeCatalog(Protocol):
    def get_video(self, video_sn: int): ...

    def latest_main_episode_number(self, video_sn: int) -> int | None: ...


class GossipWatcher:
    """固定週期抓公告 → 解析 → 比對追蹤中的 sn → 分類 →（視 `gossip_dynamic_schedule`
    設定）自動套用建議處置建立臨時排程覆蓋 → 送 `notify/` 通知。同一個迴圈裡日期跨過
    00:00 時額外送一次每日新番彙整。

    跟 `main_loop.py` 的固定週期輪詢是**兩個獨立的背景執行緒**——公告偵測不需要等一輪
    全域下載檢查跑完（舊專案是掛在 `main_loop` 檢查週期尾巴，BahaAD 拆開）。

    `search`／`resolve`／集數查詢用注入的方式（第 3 階段的純函式簽章），這裡把
    `browse.search_anime`／`browse.resolve_ref_sn`／帶 1 小時快取的 `catalog.get_video`
    接上去。
    """

    def __init__(
        self,
        settings: SettingsStore,
        gossip_store: GossipStore,
        schedule_store: ScheduleListStore,
        http: HttpGetter,
        catalog: EpisodeCatalog,
        notify_store: NotifyStore | None = None,
        now_fn: Callable[[], datetime] = datetime.now,
        anime_cache=None,
        min_poke_interval_seconds: float = _DEFAULT_MIN_POKE_INTERVAL_SECONDS,
        on_gossip_scanned: "Callable[[GossipSnapshot], None] | None" = None,
        activation_lock: "threading.Lock | None" = None,
    ) -> None:
        self._settings = settings
        self._gossip_store = gossip_store
        self._schedule_store = schedule_store
        self._http = http
        self._catalog = catalog
        # 下載交握用的同一把鎖（app_shell 傳同一個 threading.Lock 給 playlist / guest_access
        # / 這裡 / completion_watch / cookie_warmup）：抓首頁公告會讓伺服器輪換 BAHARUNE，
        # 下載交握進行中就讓路、這輪不掃（避免 code 1007，見 gamer_client/activation.py）。
        self._activation_lock = activation_lock
        self._notify_store = notify_store
        self._now_fn = now_fn
        # 每次抓完首頁公告（不管 gossip_monitor 開關）就把 snapshot 交給這個回呼——
        # 新番快訊偵測（NewAnimeWatcher）掛在這裡（規格 new_anime_bulletin.md §2）
        self._on_gossip_scanned = on_gossip_scanned
        # .0 改進.txt 第 7 項：先比對「週期表上正在播的番」，比對到再看有沒有訂閱。
        # anime_cache 提供週期表（get_home）與 episode_group（哪些集數屬於同一部番）。
        # None（輕量測試）時退化成只比對訂閱清單。
        self._anime_cache = anime_cache
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # poke()：外部「剛跟動畫瘋互動過」的訊號，叫醒背景執行緒提早查一次公告（節流見
        # `_min_poke_interval`）。用 Event 傳訊，真正的抓取一律在背景執行緒上、不並發。
        self._poke_event = threading.Event()
        self._min_poke_interval = float(min_poke_interval_seconds)
        self._last_check_at = 0.0  # monotonic()；0＝還沒查過，第一個 poke 立刻生效
        # 序列化「清舊覆蓋 + 寫新覆蓋」整段（`apply_disposition()` 是多次各自上鎖的 store
        # 呼叫，沒有這把鎖的話，兩個並發 apply（背景自動套用 vs 使用者在網頁快速連點確認）
        # 可能都先跑完 remove、才輪到 add，疊出兩筆互相矛盾的臨時排程）
        self._apply_lock = threading.Lock()
        # {episode_sn: (frozenset, monotonic_ts)}
        self._episode_cache: dict[int, tuple[frozenset[int], float]] = {}
        # 每日彙整：用「日期」而非時間戳判斷今天有沒有送過，避免輪詢間隔造成誤判送兩次；
        # 已送出那一版的內容簽章（`_digest_content` 回的第三個值——只含**穩定**資料：
        # 哪些番排今天、幾點、有哪些機動調整；**不含**要打網路查的番劇名／預測集數），
        # 當天稍後排程或機動調整有變 → 重發更新版（使用者 2026-09-03；signature 只放穩定
        # 資料是 2026-09-06 修的——原本把 `_resolve_anime_name`／`_predict_next_episode`
        # 的結果也算進去，那兩個值會隨網路成敗浮動，害彙整每小時／每分鐘誤判「變了」狂發）。
        #
        # 使用者 2026-09-05 回報：整天一則彙整都沒收到——這兩個狀態原本只存在記憶體裡，
        # 且 `_last_digest_date` 建構當下就錨定成「今天」（避免程式一天內重啟多次每次
        # 都補送初始版），但 `_last_digest_signature` 這時還是 `None`，而下面「內容有變
        # 就重發」那支判斷特意要求 `_last_digest_signature is not None`（避免拿一個沒有
        # 意義的空基準線誤判成「變了」）——兩者疊在一起的結果是：只要程式在當天重啟過，
        # 不管之後訂閱清單或機動調整異動再怎麼變，**當天都不會再送出任何一則彙整**，
        # 只能等到日期真的跨過 00:00 才會恢復正常。改成存進 `SettingsStore`、跨重啟
        # 讀回來，這樣重啟後比對的還是「上一個行程真正送出去的那一版」，不會被重置。
        self._last_digest_date = self._load_last_digest_date()
        self._last_digest_signature = self._load_last_digest_signature()

    # ------------------------------------------------------------------
    # 背景執行緒生命週期
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._poke_event.set()  # 叫醒 _wait_next
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def poke(self) -> None:
        """外部訊號：程式剛跟動畫瘋（ani.gamer.com.tw）互動過——排程下載檢查、手動下載、
        cookie 保活、週期表刷新等。順便查一次公告有沒有新增／變更，不用乾等下一次
        `gossip_check_interval_minutes` 週期輪詢（使用者 2026-09-04：只靠週期檢查不夠即時）。

        非阻塞：只設一個 Event 叫醒背景執行緒。真正的抓取一律在那條執行緒上、受
        `_min_poke_interval` 節流（避免一連串下載把動畫瘋首頁打到 429）。"""
        self._poke_event.set()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                # 「重新檢查排程更新」執行期間暫停這一輪（見 web_redesign_round2.md 階段 4）
                if not recheck_pause.is_active(self._settings):
                    self.check_once()
                    self._last_check_at = monotonic()
            except Exception:
                logger.exception("gossip_watch 這一輪檢查發生未預期的例外")
            interval_minutes = self._settings.get(
                "gossip_check_interval_minutes", _DEFAULT_CHECK_INTERVAL_MINUTES
            )
            self._wait_next(self._next_wait_seconds(interval_minutes * 60))

    def _wait_next(self, base_wait: float) -> None:
        """輪詢之間的等待：最多睡 `base_wait` 秒；被 `poke()` 叫醒、且距上次檢查已過
        `_min_poke_interval` → 提早回去跑下一輪。還在節流窗內的 poke 就睡到窗結束再查。"""
        self._poke_event.clear()
        deadline = monotonic() + base_wait
        while not self._stop_event.is_set():
            remaining = deadline - monotonic()
            if remaining <= 0:
                return
            if not self._poke_event.wait(remaining):
                return  # 到 deadline 都沒被 poke
            if self._stop_event.is_set():
                return
            self._poke_event.clear()
            idle = monotonic() - self._last_check_at
            if idle >= self._min_poke_interval:
                return  # 節流窗已過 → 立刻查
            # 還在節流窗內：睡到窗結束（其間再被 poke 也只是重設 Event，醒來就查）
            self._stop_event.wait(min(remaining, self._min_poke_interval - idle))
            return

    def _next_wait_seconds(self, interval_seconds: float) -> float:
        """一般照 gossip_check_interval_minutes 等。但每日新番彙整要在 00:00 一過就送
        （比照 aniGamerPlus 每 30 秒輪詢的迴圈），所以跨過午夜前把這一次的等待縮短到
        「距離下一個 00:00 ＋ 幾秒緩衝」，不要拖到下一次 gossip 輪詢（使用者 2026-09-02
        回饋：BahaAD 的彙整比 aniGamerPlus 晚了半小時）。"""
        now = self._now_fn()
        next_midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        seconds_to_midnight = (next_midnight - now).total_seconds() + 5
        return max(1.0, min(interval_seconds, seconds_to_midnight))

    # ------------------------------------------------------------------
    # check_once：一輪完整流程
    # ------------------------------------------------------------------

    def force_recheck(self) -> None:
        """使用者在設定頁按「重新檢查」：立刻重抓公告 + 重跑分類 + 機動調整，無視
        「這則公告先前處理過」的去重。**已手動處置（`disposition_locked=1`）的子事件
        不動、也不重新分類**——不然會把使用者手動調整的排程設定蓋掉（使用者 2026-09-01）。
        `gossip_monitor` 關掉時只跑每日彙整，跟 `check_once` 一致。"""
        self.check_once(force=True)
        self._last_check_at = monotonic()  # 剛查過，抑制緊接著的 poke 重複打首頁

    def check_once(self, *, force: bool = False) -> None:
        now = self._now_fn()
        # 每日彙整跟監視公告共用這個迴圈；就算 gossip_monitor 關掉，彙整還是要送
        # （彙整內容是「今天排定更新哪些番」，不是公告本身）。彙整送 Telegram/Discord，
        # 不打動畫瘋、不輪換 BAHARUNE，鎖外先做。
        self._maybe_send_daily_digest(now)

        # 抓首頁公告 + 後續分類（search_anime / get_video）都會讓伺服器輪換 BAHARUNE →
        # 下載交握進行中就這輪不掃（下一輪再來），避免把下載的 cookie 換掉觸發 code 1007。
        with skip_if_busy(self._activation_lock) as free:
            if not free:
                logger.debug("gossip check_once：下載交握進行中，這輪跳過公告掃描")
                return
            self._scan_gossip(now, force=force)

        # 這一輪掃到的機動調整異動（延後／暫停…）如果動到今天的排程 → 立刻重算彙整、
        # 有變就重發更新版，不用等下一輪（比照 aniGamerPlus：公告通知後緊接著補一份
        # 當日新番通知，使用者 2026-09-06 回報）。`_maybe_send_daily_digest` 靠 signature
        # 比對，沒變就不會重複送。
        self._maybe_send_daily_digest(now)

    def _scan_gossip(self, now: datetime, *, force: bool) -> None:
        scan_ts = now.strftime("%Y-%m-%d %H:%M:%S")
        text = fetch_gossip_text(self._http)

        # 新番快訊偵測不受 gossip_monitor 影響（自己有「新番偵測」開關）——先交給回呼。
        # 公告裡若把 GNN 連結寫成 `<a>` 而 anchor 文字不是網址，這裡的 regex 會漏——
        # 真的碰到再改成 `fetch_gossip()` 抓 href（目前公告都是裸網址）。
        if text is not None and self._on_gossip_scanned is not None:
            try:
                self._on_gossip_scanned(
                    GossipSnapshot(text=text, links=tuple(_GOSSIP_URL_RE.findall(text)))
                )
            except Exception:  # noqa: BLE001
                logger.exception("on_gossip_scanned 回呼發生例外")

        if not self._settings.get("gossip_monitor", True):
            return

        if text is None:
            return  # 這次抓不到（非 200／逾時／例外）——不知道現在有沒有公告，什麼都不動
        # 到這裡：`""` ＝頁面正常、公告欄是空的（站方撤下全部）；非空 ＝公告原文。
        # 空的時候還是要往下跑 _expire_vanished_events，把操作處置頁的舊項目清掉（第 25 項）。
        current_clauses = {c for c in split_clauses(text) if c}
        dynamic_on = self._settings.get("gossip_dynamic_schedule", True)
        tracked = self._schedule_store.get_entries()

        if text:  # 有公告文字才解析
            source_hash = self._gossip_store.record_announcement(text)
            skip_clauses: set[str] = set()
            if force and source_hash is None:
                # 「重新檢查」：這則公告先前處理過，但使用者要求重跑。已手動處置的子事件
                # 留著（clause 加進 skip_clauses、不重新分類），其餘自動分類的清掉重跑。
                source_hash = compute_source_hash(text)
                skip_clauses = self._gossip_store.locked_clauses(source_hash)
                self._gossip_store.delete_unlocked_events(source_hash)
            if source_hash is not None:  # 第一次出現、或 force
                # 整份文字先看是不是「每週更新時間永久變更」公告
                notice = detect_reschedule(text)
                skip_title = None
                if notice is not None and not any(
                    notice.title in c for c in skip_clauses
                ):
                    try:
                        self._process_reschedule(notice, source_hash, now, dynamic_on, tracked)
                        skip_title = notice.title
                    except Exception:
                        logger.exception("處理更新時間變更公告時發生未預期的例外：%s", notice.title)
                for sub in parse_gossip_text(text):
                    if sub.raw_clause in skip_clauses:
                        continue  # 使用者手動處置過的片段，重新檢查時不動
                    if skip_title is not None and (
                        sub.title == skip_title
                        # 更新時間變更公告常見「標題在一行、細節（無《》）在另一行」——
                        # 那句沒有作品名、又帶「固定／每週」，是同一件事的一部分，別再重記
                        or (sub.title is None and ("固定" in sub.raw_clause or "每週" in sub.raw_clause))
                    ):
                        continue
                    try:
                        self._process_sub_event(sub, source_hash, now, dynamic_on, tracked)
                    except Exception:
                        logger.exception("處理公告子事件時發生未預期的例外：%s", sub.raw_clause)

        # 每輪都做（含公告內容沒變、或整個空了）：
        #  - 第 7 項：週期表比對到但當時沒訂閱的事件，現在被訂閱了就補做處置
        #  - 第 25 項：這輪已看不到、使用者沒手動確認過的舊事件 → 清操作處置＋機動調整
        self._recheck_no_subscription_events(current_clauses, now, dynamic_on, tracked)
        self._recheck_unmatched_reschedule_events(now, dynamic_on, tracked)
        self._expire_vanished_events(current_clauses, now)
        self._gossip_store.prune(
            self._settings.get("gossip_retention_days", _DEFAULT_RETENTION_DAYS)
        )

        # 刷新「還掛在站上」的記號＋這一輪掃描時間，歷史公告頁據此把仍生效的公告用金框
        # 框住（見 web_redesign_round4.md）。公告欄空了時 mark 空集合＝所有金框自然消失。
        self._gossip_store.mark_clauses_seen(current_clauses, scan_ts)
        self._settings.update({"_gossip_last_scan_at": scan_ts})

    # ------------------------------------------------------------------
    # .0 改進.txt 第 7 項：先比對週期表、再看訂閱
    # ------------------------------------------------------------------

    def _weekly_schedule_match(self, title: str) -> int | None:
        """公告標題比對「週期表上正在播的番」——**只比標題、不打網路**（比照鈴鐺的
        `card_bell_sns` 用字面比對的理由）。這只是用來判斷「這部在播、但你沒訂閱」，
        配錯的成本只是多一則你不在意的通知，不會動排程。anime_cache 沒給就回 None。

        （不把整份週期表塞進 `match_title_to_sn` 的候選池——那會對 100+ sn 逐一打
        `catalog.get_video` 冷快取，實測會 429，見 code review。）"""
        if self._anime_cache is None:
            return None
        try:
            home = self._anime_cache.get_home()
        except Exception:  # noqa: BLE001
            return None
        if not home:
            return None
        _cards, schedule = home
        base, season = build_search_title(title)
        want = base.strip().casefold()
        for day in schedule:
            for e in day.entries:
                if not e.video_sn:
                    continue
                e_base, e_season = build_search_title(e.title)
                if e_base.strip().casefold() == want and season_compatible(season, e_season):
                    return e.video_sn
        return None

    def _subscribed_sn_for_anime(self, anime_sn: int, tracked) -> int | None:
        """`anime_sn`（週期表比對到的某一集）所屬的番劇，有沒有被訂閱？有就回傳訂閱清單裡
        對應的那個 sn（同番劇不同集 sn 靠 episode_group 展開，比照 Cluster A 的鈴鐺邏輯）。"""
        if anime_sn in tracked:
            return anime_sn
        if self._anime_cache is None:
            return None
        try:
            gk = self._anime_cache.group_key_for(anime_sn)
            members = self._anime_cache.group_members(gk) if gk is not None else set()
        except Exception:  # noqa: BLE001
            members = set()
        for sn in tracked:
            if sn in members:
                return sn
        return None

    def _process_sub_event(self, sub, source_hash, now, dynamic_on, tracked) -> None:
        if sub.title is None:
            self._gossip_store.add_event(
                source_hash,
                raw_clause=sub.raw_clause,
                action="log_only",
                match_status="no_target",
                status_label="無法判斷",
            )
            return

        # 先比對訂閱清單（精確：集數 sn 交集）——候選池就是訂閱項目，量小
        match = match_title_to_sn(
            sub.title,
            list(tracked.keys()),
            search=lambda kw: search_anime(self._http, kw),
            resolve_ref_sn=lambda ref: resolve_ref_sn(self._http, ref),
            episode_set_for=self._episode_set_for,
        )
        subscribed_sn = match.sn
        entry = tracked[subscribed_sn] if subscribed_sn is not None else None
        baseline = (
            baseline_from_schedule(entry.schedule_weekday, entry.schedule_hour, entry.schedule_minute, now)
            if entry is not None
            else None
        )
        status = classify_status(sub, now=now, baseline_time=baseline)
        window_start, window_end = status.target_window or (None, None)
        target_date = status.target_date.isoformat() if status.target_date else None

        if subscribed_sn is None:
            # 沒比對到訂閱項目——這部是不是「正在播、但你沒訂閱」？（純標題比對、不打網路）
            airing_sn = self._weekly_schedule_match(sub.title)
            if airing_sn is None:
                # 連週期表都比不到 → 無法判斷（使用者可在操作處置頁手動指定）
                self._gossip_store.add_event(
                    source_hash, raw_clause=sub.raw_clause, action=sub.action,
                    match_status="unmatched", status_label="無法判斷", title_in_gossip=sub.title,
                )
                return
            # 第 7 項：週期表有、沒訂閱 → 記錄＋通知＋自動忽略。公告還掛在站上期間，
            # 之後每輪 _recheck_no_subscription_events 會看它有沒有被訂閱。
            self._gossip_store.add_event(
                source_hash, raw_clause=sub.raw_clause, action=sub.action,
                match_status="no_subscription", status_label="未訂閱", title_in_gossip=sub.title,
                sn=airing_sn, target_date=target_date, target_time=status.target_time,
                target_time_start=window_start, target_time_end=window_end,
                expect_episode_count=sub.expect_episode_count,
            )
            self._notify_gossip(sub, status, None, now)
            return

        event_id = self._gossip_store.add_event(
            source_hash,
            raw_clause=sub.raw_clause,
            action=sub.action,
            match_status="matched",
            status_label=status.status_label,
            title_in_gossip=sub.title,
            sn=subscribed_sn,
            target_date=target_date,
            target_time=status.target_time,
            target_time_start=window_start,
            target_time_end=window_end,
            expect_episode_count=sub.expect_episode_count,
        )

        self._notify_gossip(sub, status, entry, now)

        # 「機動調整時間」開啟時自動套用建議處置（disposition_locked=0）；不論這個設定
        # 有沒有開，上面的公告通知都照樣送（使用者要知道的是公告內容本身）
        if dynamic_on and status.status_label != "無法判斷" and sub.confidence != "low":
            suggestion = STATUS_TO_SUGGESTION[status.status_label]
            try:
                self.apply_disposition(event_id, suggestion, locked=False, now=now)
            except ValueError as exc:
                logger.warning("sn=%s 自動套用機動調整失敗：%s", subscribed_sn, exc)

    def _process_reschedule(self, notice: RescheduleNotice, source_hash, now, dynamic_on, tracked) -> None:
        """每週更新時間異動公告。記一筆 `action='reschedule'` 的事件、送通知；比對到訂閱
        且機動調整開著 → 自動套用建議處置「調整更新時段」（`apply_disposition` 會永久改
        `schedule_entries` 的檢查時段）。事件一樣列進操作處置頁，使用者可改成「維持原本
        時段」或「自訂更新時段」。

        **只處理一次**：同一部番、同一個新時段，只要 `gossip_events` 裡已經有一筆
        `reschedule` 就跳過——公告會掛在站上一整週，跑馬燈其他內容變動會讓這裡一直被
        重呼叫，不能每次都覆蓋掉使用者手動改回的排程、也不能每天重發通知。

        `notice.hour is None`（公告寫「同時段」＝只換星期）→ 時刻沿用該番目前排程的自訂
        時段（在 `apply_disposition` 裡處理）。"""
        zh_weekday = "一二三四五六日"[notice.weekday - 1]
        if notice.same_slot:
            target_time = f"{notice.weekday}/同時段"
            clause = f"《{notice.title}》更新時間變更為每週{zh_weekday}（同時段）"
        else:
            target_time = f"{notice.weekday}/{notice.hour:02d}:{notice.minute:02d}"
            clause = (
                f"《{notice.title}》更新時間變更為每週{zh_weekday} "
                f"{notice.hour:02d}:{notice.minute:02d}"
            )
        if self._gossip_store.has_reschedule_event(notice.title, target_time):
            return

        fake_sub = _RescheduleNotifySub(notice.title, clause)

        match = match_title_to_sn(
            notice.title,
            list(tracked.keys()),
            search=lambda kw: search_anime(self._http, kw),
            resolve_ref_sn=lambda ref: resolve_ref_sn(self._http, ref),
            episode_set_for=self._episode_set_for,
        )
        subscribed_sn = match.sn
        airing_sn = subscribed_sn or self._weekly_schedule_match(notice.title)

        if airing_sn is None:
            self._gossip_store.add_event(
                source_hash, raw_clause=clause, action="reschedule",
                match_status="unmatched", status_label="無法判斷", title_in_gossip=notice.title,
                target_time=target_time,
            )
            return

        if subscribed_sn is None:
            self._gossip_store.add_event(
                source_hash, raw_clause=clause, action="reschedule",
                match_status="no_subscription", status_label="未訂閱",
                title_in_gossip=notice.title, sn=airing_sn,
                target_date=notice.effective_date, target_time=target_time,
            )
            self._notify_gossip(fake_sub, None, None, now)
            return

        event_id = self._gossip_store.add_event(
            source_hash, raw_clause=clause, action="reschedule",
            match_status="matched", status_label="更新時間變更",
            title_in_gossip=notice.title, sn=subscribed_sn,
            target_date=notice.effective_date, target_time=target_time,
        )
        self._notify_gossip(fake_sub, None, tracked[subscribed_sn], now)

        # 機動調整開著 → 自動套用建議處置「調整更新時段」（永久改 schedule_entries 的檢查
        # 時段）。跟其他公告類型一致：一樣列進操作處置頁，使用者可改成「維持原本時段」
        # 或「自訂更新時段」（使用者 2026-09-01）。
        if dynamic_on:
            try:
                self.apply_disposition(event_id, "調整更新時段", locked=False, now=now)
            except ValueError as exc:
                logger.warning("sn=%s 自動套用更新時段調整失敗：%s", subscribed_sn, exc)

    def _recheck_no_subscription_events(self, current_clauses, now, dynamic_on, tracked) -> None:
        """第 7 項：`no_subscription` 事件的番劇之後被訂閱了 → 補做分類＋處置。公告已從
        站上撤下（`raw_clause` 不在這輪 `current_clauses`）的就跳過。

        `action='reschedule'` 的不在這裡補——訂閱鈴鐺本來就會從**當下的週期表**（那時
        站方已經改成新時間）寫入自訂時段，等於自動套用了。"""
        for event in self._gossip_store.events_by_match_status("no_subscription"):
            if (
                event["action"] == "reschedule"
                or event["sn"] is None
                or event["raw_clause"] not in current_clauses
            ):
                continue
            subscribed_sn = self._subscribed_sn_for_anime(event["sn"], tracked)
            if subscribed_sn is None:
                continue

            subs = parse_gossip_text(event["raw_clause"])
            # 一句提到多部作品 → 多筆 event 共用 raw_clause，用標題挑回這筆對應的 sub
            sub = next(
                (s for s in subs if s.title and s.title == event["title_in_gossip"]), None
            )
            if sub is None:
                continue
            entry = tracked[subscribed_sn]
            baseline = baseline_from_schedule(
                entry.schedule_weekday, entry.schedule_hour, entry.schedule_minute, now
            )
            status = classify_status(sub, now=now, baseline_time=baseline)
            window_start, window_end = status.target_window or (None, None)
            self._gossip_store.promote_no_subscription(
                event["id"],
                subscribed_sn,
                status_label=status.status_label,
                target_date=status.target_date.isoformat() if status.target_date else None,
                target_time=status.target_time,
                target_time_start=window_start,
                target_time_end=window_end,
                expect_episode_count=sub.expect_episode_count,
            )
            self._notify_gossip(sub, status, entry, now)
            if dynamic_on and status.status_label != "無法判斷" and sub.confidence != "low":
                suggestion = STATUS_TO_SUGGESTION[status.status_label]
                try:
                    self.apply_disposition(event["id"], suggestion, locked=False, now=now)
                except ValueError as exc:
                    logger.warning("sn=%s 補做機動調整失敗：%s", subscribed_sn, exc)

    def _recheck_unmatched_reschedule_events(self, now, dynamic_on, tracked) -> None:
        """更新時間異動公告當初比對不到作品（`match_status='unmatched'`——多半是全新啟動、
        週期表快取還空的當下就掃到公告）→ 之後每輪重新比對，比對到就補做分類／處置
        （原本 `_process_reschedule` 只在公告第一次出現時跑一次，`has_reschedule_event`
        又會擋住之後的重跑，所以 unmatched 會永遠卡住，使用者 2026-09-05 回報）。

        不像 `_recheck_no_subscription_events` 那樣要求公告還掛在站上——reschedule 事件
        本來就「不因公告撤下而清」（永久變更、隨時可改回），而且它的 `raw_clause` 是
        合成句、不會出現在原始跑馬燈文字裡。一直試到比對成功、或使用者手動處置為止。"""
        for event in self._gossip_store.events_by_match_status("unmatched"):
            if (
                event["action"] != "reschedule"
                or event["disposition_locked"]
                or event["acknowledged_at"]
                or not event["title_in_gossip"]
            ):
                continue
            # 從已存的欄位重建，不重新解析 raw_clause（那是合成句、格式跟原公告不同）
            weekday, _hour, _minute = _parse_reschedule_target(event["target_time"])
            if weekday is None:
                continue
            title = event["title_in_gossip"]
            match = match_title_to_sn(
                title,
                list(tracked.keys()),
                search=lambda kw: search_anime(self._http, kw),
                resolve_ref_sn=lambda ref: resolve_ref_sn(self._http, ref),
                episode_set_for=self._episode_set_for,
            )
            subscribed_sn = match.sn
            airing_sn = subscribed_sn or self._weekly_schedule_match(title)
            if airing_sn is None:
                continue  # 還是比對不到，下輪再試

            fake_sub = _RescheduleNotifySub(title, event["raw_clause"])
            if subscribed_sn is None:
                self._gossip_store.promote_unmatched_reschedule(
                    event["id"], airing_sn, match_status="no_subscription",
                    status_label="未訂閱", target_date=event["target_date"],
                )
                self._notify_gossip(fake_sub, None, None, now)
                continue

            self._gossip_store.promote_unmatched_reschedule(
                event["id"], subscribed_sn, match_status="matched",
                status_label="更新時間變更", target_date=event["target_date"],
            )
            self._notify_gossip(fake_sub, None, tracked[subscribed_sn], now)
            if dynamic_on:
                try:
                    self.apply_disposition(event["id"], "調整更新時段", locked=False, now=now)
                except ValueError as exc:
                    logger.warning("sn=%s 補做更新時段調整失敗：%s", subscribed_sn, exc)

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------

    def _notify_gossip(self, sub, status: ClassifiedStatus, entry, now: datetime) -> None:
        if self._notify_store is None:
            return
        category = _NOTIFY_CATEGORY.get(sub.action, "gossip_other")
        stamp = now.strftime("%Y-%m-%d %H:%M:%S")
        # entry 為 None＝第 7 項的「未訂閱」通知（週期表有異動、但使用者沒訂閱這部番）
        animation_name = (entry.rename if entry is not None else None) or sub.title
        if entry is None:
            animation_name = f"{animation_name}（未訂閱）"
        context = dict(
            animation_name=animation_name,
            announcement_text=sub.raw_clause,
            announcement_time=stamp,
            finish_time=stamp,
        )
        if sub.action == "delay":
            postpone = _format_postpone(status)
            if postpone:
                context["postpone_time"] = postpone
        send_notification(self._notify_store, self._settings, self._http, category, **context)

    # ------------------------------------------------------------------
    # 套用處置（建立 gossip_pending 臨時排程覆蓋）
    # ------------------------------------------------------------------

    def apply_disposition(
        self,
        event_id: int,
        disposition: str,
        *,
        locked: bool,
        now: datetime | None = None,
        custom_check_date: str | None = None,
        custom_check_time: str | None = None,
        custom_check_time_start: str | None = None,
        custom_check_time_end: str | None = None,
        custom_episode_count: int | None = None,
        custom_weekday: int | None = None,
    ) -> None:
        """把一個處置套到子事件上：先清掉這個子事件先前建立的舊覆蓋（不論系統自動或
        使用者手動），再依處置種類建立新的 `gossip_pending`，最後更新 `gossip_events`
        的 `disposition`／`disposition_locked`。`locked=False` 是系統自動套用建議，
        `locked=True` 是使用者在操作處置頁按過「確認」。

        操作處置頁的「確認」按鈕（第 6 階段）跟這裡的系統自動套用走同一個函式。"""
        now = now or self._now_fn()
        if disposition not in DISPOSITION_OPTIONS:
            raise ValueError(f"未知的處置方式：{disposition}")

        with self._apply_lock:
            event = self._gossip_store.get_event(event_id)
            if event is None:
                raise ValueError("找不到指定的公告子事件")

            self._gossip_store.remove_pending_for_event(event_id)

            if disposition in ("等待處置", "忽略此公告", "維持原本時段"):
                self._gossip_store.set_disposition(event_id, disposition, locked=locked)
                return

            sn = event["sn"]
            if sn is None:
                raise ValueError(
                    "這則子事件尚未比對到追蹤中的作品，無法套用排程處置"
                    "（可選擇「忽略此公告」關閉待處理狀態）"
                )

            if disposition in ("調整更新時段", "自訂更新時段"):
                self._apply_reschedule_disposition(
                    event_id, event, sn, disposition, custom_weekday, custom_check_time, locked=locked
                )
                return

            target_date = _date_or(event["target_date"], now)
            expect = max(1, int(event["expect_episode_count"]))

            if disposition in _SKIP_DISPOSITIONS:
                self._gossip_store.add_pending(
                    event_id=event_id, sn=sn, type="skip_check", check_date=target_date.isoformat()
                )
            elif disposition in ("本週提前更新", "本週延後更新"):
                self._add_time_or_window_pending(
                    event_id, sn, target_date, event["target_time"],
                    event["target_time_start"], event["target_time_end"], expect=1,
                )
            elif disposition == "本週同時更新":
                self._add_time_or_window_pending(
                    event_id, sn, target_date, event["target_time"],
                    event["target_time_start"], event["target_time_end"], expect=max(2, expect),
                )
            elif disposition == "自訂更新時間":
                if not (custom_check_date and custom_check_time):
                    raise ValueError("自訂更新時間需要填寫日期與時間")
                self._add_override_time_pending(event_id, sn, custom_check_date, custom_check_time, expect=1)
            elif disposition == "自訂連續更新時間":
                if not (custom_check_date and custom_check_time and custom_episode_count):
                    raise ValueError("自訂連續更新時間需要填寫日期、時間與集數")
                self._add_override_time_pending(
                    event_id, sn, custom_check_date, custom_check_time, expect=int(custom_episode_count)
                )
            elif disposition == "自訂時間範圍":
                if not (custom_check_date and custom_check_time_start and custom_check_time_end):
                    raise ValueError("自訂時間範圍需要填寫日期、開始時間與結束時間")
                if custom_check_time_start == custom_check_time_end:
                    raise ValueError("自訂時間範圍的開始時間不能跟結束時間相同")
                self._gossip_store.add_pending(
                    event_id=event_id, sn=sn, type="override_check_window", check_date=custom_check_date,
                    check_time_start=custom_check_time_start, check_time_end=custom_check_time_end,
                    expect_episode_count=int(custom_episode_count or 1),
                    retry_interval_minutes=_WINDOW_RETRY_INTERVAL_MINUTES,
                )

            # 「本週*」用事件本來解析出的 target_*，不動；「自訂*」用使用者填的值覆蓋，
            # 且要把不屬於這個處置的欄位清成空字串（否則畫面顯示會被舊值污染——例如
            # 選「自訂時間範圍」但事件還留著上次解析的單一 target_time，範本會優先顯示
            # 那個而不是新的時間範圍）
            if disposition == "自訂更新時間" or disposition == "自訂連續更新時間":
                self._gossip_store.set_disposition(
                    event_id, disposition, locked=locked,
                    target_date=custom_check_date, target_time=custom_check_time,
                    target_time_start="", target_time_end="",
                    expect_episode_count=int(custom_episode_count) if custom_episode_count else None,
                )
            elif disposition == "自訂時間範圍":
                self._gossip_store.set_disposition(
                    event_id, disposition, locked=locked,
                    target_date=custom_check_date, target_time="",
                    target_time_start=custom_check_time_start, target_time_end=custom_check_time_end,
                    expect_episode_count=int(custom_episode_count) if custom_episode_count else None,
                )
            else:
                self._gossip_store.set_disposition(event_id, disposition, locked=locked)

    def _apply_reschedule_disposition(
        self, event_id, event, sn: int, disposition: str,
        custom_weekday, custom_check_time, *, locked: bool,
    ) -> None:
        """每週更新時間異動的兩個「會動排程」處置：永久改 `schedule_entries` 的檢查時段
        （`subscribe_sn`，不是本週臨時覆蓋）。"""
        if disposition == "自訂更新時段":
            if not custom_weekday or not custom_check_time:
                raise ValueError("自訂更新時段需要選擇星期與時間")
            weekday = int(custom_weekday)
            parsed = _parse_hhmm(custom_check_time)
            if parsed is None or not 1 <= weekday <= 7:
                raise ValueError("自訂更新時段的星期或時間格式不對")
            hour, minute = parsed
        else:  # 調整更新時段：用公告解析出來的目標（"W/HH:MM" 或 "W/同時段"）
            weekday, hour, minute = _parse_reschedule_target(event.get("target_time"))
            if weekday is None:
                raise ValueError("這則公告沒有解析出新的更新星期，請改用「自訂更新時段」")
            if hour is None:  # 「同時段」→ 沿用這部番目前排程的自訂時刻
                entry = self._schedule_store.get_entries().get(sn)
                if entry is None or entry.schedule_hour is None:
                    raise ValueError(
                        "公告只說改到每週"
                        f"{'一二三四五六日'[weekday - 1]}、沒給時間，這部目前也沒有自訂時段可沿用"
                        "——請改用「自訂更新時段」填時間"
                    )
                hour, minute = entry.schedule_hour, entry.schedule_minute

        subscribe_sn(self._schedule_store, sn, weekday, hour, minute)
        logger.info(
            "%s 依「%s」永久調整檢查時段為 每週%s %02d:%02d",
            self._resolve_anime_name(sn) or f"sn={sn}",
            disposition,
            "一二三四五六日"[weekday - 1],
            hour,
            minute,
        )
        self._gossip_store.set_disposition(
            event_id, disposition, locked=locked, target_time=f"{weekday}/{hour:02d}:{minute:02d}"
        )

    def _add_time_or_window_pending(
        self, event_id, sn, target_date: date, target_time, window_start, window_end, *, expect: int
    ) -> None:
        check_date = target_date.isoformat()
        if target_time:
            self._add_override_time_pending(event_id, sn, check_date, target_time, expect=expect)
        elif window_start and window_end:
            self._gossip_store.add_pending(
                event_id=event_id, sn=sn, type="override_check_window", check_date=check_date,
                check_time_start=window_start, check_time_end=window_end,
                expect_episode_count=expect, retry_interval_minutes=_WINDOW_RETRY_INTERVAL_MINUTES,
            )
        else:
            raise ValueError("公告內容沒有解析出明確時間，請改用「自訂更新時間」或「自訂時間範圍」")

    def _add_override_time_pending(
        self, event_id, sn, check_date: str, check_time: str, *, expect: int
    ) -> None:
        self._gossip_store.add_pending(
            event_id=event_id, sn=sn, type="override_check_time", check_date=check_date,
            check_time=check_time, expect_episode_count=expect,
            **_retry_fields(check_date, check_time),
        )

    # ------------------------------------------------------------------
    # 公告從頁面撤下時清掉「還沒手動確認過」的舊子事件
    # ------------------------------------------------------------------

    def _expire_vanished_events(self, current_clauses: set[str], now: datetime) -> None:
        """這輪已經看不到（`raw_clause` 不在 `current_clauses`，含公告欄整個空掉的情形）、
        且使用者還沒手動確認過（`disposition_locked=0`）的舊子事件——套「忽略此公告」
        locked=True，讓它從操作處置頁消失、機動調整覆蓋一起清掉（.0 改進.txt 第 25 項；
        歷史公告不清，`list_events()` 照舊全列）。已手動確認過的不動（站方撤下公告不代表
        使用者當初的決定失效）。**例外**：`action='reschedule'`（每週更新時間永久變更）
        不清——那是連帶調過週期表時間的持久變更，站方把公告撤下不代表要還原。"""
        for event in self._gossip_store.get_actionable(now.date().isoformat()):
            if event["disposition_locked"] or event["raw_clause"] in current_clauses:
                continue
            if event["action"] == "reschedule":
                continue
            try:
                self.apply_disposition(event["id"], "忽略此公告", locked=True, now=now)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # 每日新番彙整
    # ------------------------------------------------------------------

    def _maybe_send_daily_digest(self, now: datetime) -> None:
        if self._notify_store is None:
            return
        # 使用者 2026-09-05 回報：整天一則每日彙整都沒收到（連重置/重新啟動後的初始那則
        # 都沒有），發送歷史裡也完全找不到 system_daily_digest 記錄——`_digest_content()`
        # 這裡面沒有像 `_resolve_anime_name` 那樣逐一包 try/except，任一筆訂閱項目在算
        # 名稱/預測集數時炸掉就會讓整支 `_maybe_send_daily_digest` 中途失敗，且原本這裡
        # 沒有自己的例外處理，例外會一路衝出去讓 `check_once()` 那一輪連 `_scan_gossip`
        # 都跳過（`_run_loop` 的 try/except 是包住整個 `check_once()`，不是只包這裡）。
        # 先補上例外處理，讓這裡的失敗不再牽連公告掃描，也把原因記進日誌，下次重現時
        # 才查得到真正炸在哪一行。
        try:
            today = now.date()
            schedule_lines, adjustment_lines, signature = self._digest_content(today)

            if today != self._last_digest_date:
                # 使用者 2026-09-05：重置剛重開時通知憑證可能還沒設定好，那次
                # `send_notification()` 會直接跳過（不算失敗，不寫歷史）。這裡只有
                # 真的送出去（或至少過了類別/憑證那兩關）才記「今天已經送過」，不然
                # 之後補上憑證，當天都不會再送出這則初始彙整。
                sent = self._send_digest(today, schedule_lines, adjustment_lines, is_update=False, now=now)
                if sent:
                    self._last_digest_date = today
                    self._last_digest_signature = signature
                    self._persist_digest_state()
            elif self._last_digest_signature is not None and signature != self._last_digest_signature:
                # 當天稍後彙整內容有變（機動調整異動，或公告把已在彙整內的番改了時間）→ 重發更新版
                sent = self._send_digest(today, schedule_lines, adjustment_lines, is_update=True, now=now)
                if sent:
                    self._last_digest_signature = signature
                    self._persist_digest_state()
        except Exception:  # noqa: BLE001
            logger.exception("每日新番彙整計算或發送失敗（這一輪跳過，公告掃描不受影響）")

    _SETTINGS_KEY_LAST_DIGEST_DATE = "_gossip_last_digest_date"
    _SETTINGS_KEY_LAST_DIGEST_SIGNATURE = "_gossip_last_digest_signature"

    def _load_last_digest_date(self) -> date | None:
        raw = self._settings.get(self._SETTINGS_KEY_LAST_DIGEST_DATE)
        if not raw:
            return None
        try:
            return date.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    def _load_last_digest_signature(self) -> tuple[str, ...] | None:
        raw = self._settings.get(self._SETTINGS_KEY_LAST_DIGEST_SIGNATURE)
        if raw is None:
            return None
        try:
            return tuple(str(x) for x in raw)
        except TypeError:
            return None

    def _persist_digest_state(self) -> None:
        """跨重啟記住「上一版彙整真正送出去的內容」（使用者 2026-09-05：只存記憶體的話，
        程式當天重啟一次，`_last_digest_signature` 就被重設回 `None`，之後不管排程或
        機動調整異動再怎麼變都偵測不到「變了」，當天再也不會補發彙整）。"""
        self._settings.update({
            self._SETTINGS_KEY_LAST_DIGEST_DATE: self._last_digest_date.isoformat(),
            self._SETTINGS_KEY_LAST_DIGEST_SIGNATURE: list(self._last_digest_signature or ()),
        })

    def _digest_content(
        self, today: date
    ) -> tuple[list[str], list[str], tuple[str, ...]]:
        """回傳 `(排程清單文字, 機動調整清單文字, 變更偵測 signature)`。

        「本日排程更新」＝動畫瘋「本日更新」那套「播出日」界定，不是日曆日：
        **今天 13:00 ~ 隔天 12:59** 算同一個播出日（深夜番 23:00~01:00 更新、日曆已
        跨天，仍算「今天」的更新）。所以清單 = 今天 13:00 後排的 ∪ 明天 12:59 前排的。
        只用 `schedule_weekday == 今天` 會（1）漏掉明天凌晨的深夜番，（2）誤收今天
        白天那些其實屬於昨天播出日的項目。比照 aniGamerPlus `_in_broadcast_day`。"""
        today_iso = today.isoformat()
        tomorrow_iso = (today + timedelta(days=1)).isoformat()
        today_wd = today.isoweekday()
        tomorrow_wd = (today + timedelta(days=1)).isoweekday()

        def _in_broadcast_day(weekday: int | None, hour: int | None) -> bool:
            if weekday is None or hour is None:
                return False
            if weekday == today_wd and hour >= 13:
                return True
            if weekday == tomorrow_wd and hour < 13:
                return True
            return False

        def _override_in_broadcast_day(check_date: str, hour: int | None) -> bool:
            # 機動調整覆蓋：用它「實際生效」的 check_date 判斷（不是排程原本的星期），
            # 避免延到明天的覆蓋還印在今天的清單裡。
            if hour is None:
                return check_date == today_iso
            if check_date == today_iso and hour >= 13:
                return True
            if check_date == tomorrow_iso and hour < 13:
                return True
            return False

        # 機動調整先算好：本日排程要據此覆蓋時間、排除本週暫停的項目
        overrides: dict[int, tuple[int, int]] = {}
        skipped: set[int] = set()
        adjustment_lines = []
        adjustment_sig: list[str] = []
        for pending in self._gossip_store.list_pending("pending"):
            hour = self._pending_hour(pending)
            if not _override_in_broadcast_day(pending["check_date"], hour):
                continue
            adjustment_lines.append(
                f"- {self._pending_name(pending)} {self._pending_label(pending)}"
                f"{self._pending_episode_suffix(pending)}"
            )
            raw_time = pending.get("check_time") or pending.get("check_time_start") or ""
            adjustment_sig.append(
                f"{pending['sn']}/{pending['type']}/{pending['check_date']}/{raw_time}"
            )
            if pending["type"] == "skip_check":
                skipped.add(pending["sn"])
            else:
                raw = pending.get("check_time") or pending.get("check_time_start")
                if raw and ":" in str(raw):
                    hh, mm = (int(x) for x in str(raw).split(":")[:2])
                    overrides[pending["sn"]] = (hh, mm)

        pad = self._settings.get("filename_pad_width", _DEFAULT_PAD_WIDTH)
        rows: list[tuple[int, int, int, Any]] = []
        for sn, entry in self._schedule_store.get_entries().items():
            if sn in skipped:
                continue  # 本週暫停，「機動調整異動」區塊已列，這裡不重複成照常排程
            if sn in overrides:
                hh, mm = overrides[sn]  # _override_in_broadcast_day 已篩過落在本日播出日
            elif _in_broadcast_day(entry.schedule_weekday, entry.schedule_hour):
                hh, mm = entry.schedule_hour, entry.schedule_minute
            else:
                continue
            rows.append((hh, mm, sn, entry))
        # 00:00~12:59 視為接在 23:59 之後（比照 aniGamerPlus，呈現順序跟實際播出先後一致）
        rows.sort(key=lambda r: ((r[0] - 13) % 24, r[1]))

        schedule_lines = []
        for hh, mm, sn, entry in rows:
            name = entry.rename or self._resolve_anime_name(sn) or f"sn={sn}"
            ep = self._predict_next_episode(sn)
            ep_part = f"{episode_zh(ep, pad)} " if ep is not None else ""
            schedule_lines.append(f"- {name} {ep_part}{hh:02d}:{mm:02d}")

        # 「內容有沒有變、要不要重發更新版」的判斷 signature——只放**穩定**資料：哪些
        # 番排在今天、幾點（含機動調整覆蓋過的時間）、有哪些機動調整。**不放**
        # `_resolve_anime_name` 跟 `_predict_next_episode` 的結果——那兩個都要打網路查
        # catalog，值會隨網路成敗／新集數上架浮動，放進 signature 會讓彙整每小時（甚至
        # 每分鐘，gossip 現在 1 分一輪）誤判「內容變了」不停重發（使用者 2026-09-06 回報）。
        signature = tuple(
            [f"{sn}@{hh:02d}:{mm:02d}:{entry.rename or ''}" for hh, mm, sn, entry in rows]
            + ["--"]
            + sorted(adjustment_sig)
        )
        return schedule_lines, adjustment_lines, signature

    def _predict_next_episode(self, sn: int) -> int | None:
        """「本篇」目前最新集數 + 1 ＝ 這次排程檢查預期會下載到的集數（比照 aniGamerPlus
        每日彙整的即時查詢）。查不到／電影特別篇作品回 None。"""
        fn = getattr(self._catalog, "latest_main_episode_number", None)
        if fn is None:
            return None
        try:
            current = fn(sn)
        except Exception:  # noqa: BLE001 - 查不到就不附集數
            logger.debug("每日彙整預測集數失敗（sn=%s）", sn, exc_info=True)
            return None
        return current + 1 if current is not None else None

    def _pending_hour(self, pending: dict) -> int | None:
        """機動調整那筆屬於哪個「播出日」——有明確時間（`check_time` / `check_time_start`）
        就取小時；`skip_check` 沒帶時間 → 退回去用該 sn 原本排程的小時判斷。比照
        aniGamerPlus `_row_hour`。"""
        raw = pending.get("check_time") or pending.get("check_time_start")
        if raw and ":" in str(raw):
            try:
                return int(str(raw).split(":")[0])
            except ValueError:
                return None
        entry = self._schedule_store.get_entries().get(pending.get("sn"))
        return entry.schedule_hour if entry is not None else None

    def _pending_episode_suffix(self, pending: dict) -> str:
        """機動調整異動那行的集數預測（比照 aniGamerPlus `_format_expected_episodes`）：
        「本週暫停」不附；改時間／加更依 `expect_episode_count` 附「，預計播出第 N 集」
        或「，預計連續播出 M 集（…）」。"""
        if pending.get("type") == "skip_check":
            return ""
        first = self._predict_next_episode(pending["sn"])
        if first is None:
            return ""
        count = int(pending.get("expect_episode_count") or 1)
        pad = self._settings.get("filename_pad_width", _DEFAULT_PAD_WIDTH)
        if count <= 1:
            return f"，預計播出{episode_zh(first, pad)}"
        labels = [episode_zh(first + i, pad) for i in range(count)]
        joined = "、".join(labels[:-1]) + " 與 " + labels[-1]
        return f"，預計連續播出{count}集（{joined}）"

    def _resolve_anime_name(self, sn: int) -> str | None:
        """訂閱項目沒設「更名」時，每日彙整用的番劇名。使用者 2026-09-02 回饋：以前
        沒更名的都只顯示 `sn=NNNNN`。快取優先（週期表／詳細頁），最後才退回打一次
        `video.php`（每日彙整只列今天要更新的幾部，量小，比照 aniGamerPlus 每日彙整
        本來就會即時查）。全部拿不到就回 None，呼叫端退回 `sn=`。"""
        cache = self._anime_cache
        if cache is not None:
            try:
                home = cache.get_home()
            except Exception:  # noqa: BLE001
                home = None
            if home is not None:
                for weekly in home[1]:
                    for e in getattr(weekly, "entries", []):
                        if getattr(e, "video_sn", None) == sn and getattr(e, "title", ""):
                            return e.title.strip()
            for getter in ("get_detail", "anime_title_for"):
                fn = getattr(cache, getter, None)
                if fn is None:
                    continue
                try:
                    got = fn(sn)
                except Exception:  # noqa: BLE001
                    continue
                if getter == "get_detail" and got is not None and got[0].title:
                    return got[0].title.strip()
                if getter == "anime_title_for" and got:
                    return str(got).strip()
        try:
            info = self._catalog.get_video(sn)
        except Exception:  # noqa: BLE001
            logger.debug("每日彙整查番劇名失敗（sn=%s）", sn, exc_info=True)
            return None
        title = (getattr(info, "anime_title", "") or "").strip()
        return title or None

    def _pending_name(self, pending: dict) -> str:
        event = self._gossip_store.get_event(pending["event_id"]) if pending["event_id"] else None
        if event and event["title_in_gossip"]:
            return event["title_in_gossip"]
        return self._resolve_anime_name(pending["sn"]) or f"sn={pending['sn']}"

    def _pending_label(self, pending: dict) -> str:
        event = self._gossip_store.get_event(pending["event_id"]) if pending["event_id"] else None
        if event:
            return event["disposition"]
        return {"skip_check": "本週暫停更新"}.get(pending["type"], "本週時間調整")

    def _send_digest(
        self, today: date, schedule_lines: list[str], adjustment_lines: list[str], *,
        is_update: bool, now: datetime,
    ) -> bool:
        return send_notification(
            self._notify_store, self._settings, self._http, "system_daily_digest",
            digest_date=today.isoformat(),
            today_schedule_count=len(schedule_lines),
            today_schedule_list="\n".join(schedule_lines) or "今日無排定更新",
            dynamic_adjustment_count=len(adjustment_lines),
            dynamic_adjustment_list="\n".join(adjustment_lines) or "今日無機動調整異動",
            digest_update_note="（機動調整異動更新，重新發送）" if is_update else "",
            finish_time=now.strftime("%Y-%m-%d %H:%M:%S"),
        )

    # ------------------------------------------------------------------
    # 集數集合查詢（1 小時快取）
    # ------------------------------------------------------------------

    def _episode_set_for(self, any_episode_sn: int) -> frozenset[int]:
        """給定該季任一集的 video_sn，回傳整季所有集數的 video_sn 集合（含傳入的那個）。
        快取 1 小時——追蹤中的作品出新一集後，舊快取比對不到新集數的 sn 會誤判成
        「無法判斷」，TTL 讓它定期重查（公告比對本來就是排程性質，慢一小時內發現可接受）。

        傳進來的 sn 若剛好是權限鎖住的集數，`get_video()` 會拋例外 → 退回只含這個 sn
        的集合（v1 接受的限制：追蹤中的 sn 通常看得到，候選解析出來的多半是第一集/最新集
        訪客也看得到）。"""
        now = monotonic()
        cached = self._episode_cache.get(any_episode_sn)
        if cached is not None and now - cached[1] < _EPISODE_SET_CACHE_TTL_SECONDS:
            return cached[0]
        try:
            info = self._catalog.get_video(any_episode_sn)
            sn_set = frozenset({ep.video_sn for ep in info.episodes} | {any_episode_sn})
        except Exception as exc:  # noqa: BLE001
            # exc（WatchingPermissionDenied 等）已帶「《番劇名》第N集」，不再另外印 sn
            logger.warning("公告比對時查詢集數列表失敗：%s", exc)
            sn_set = frozenset({any_episode_sn})
        for sn in sn_set:
            self._episode_cache[sn] = (sn_set, now)
        return sn_set


def _retry_fields(check_date: str, check_time: str) -> dict:
    """`override_check_time` 一律帶「目標時間之後」2 小時的重試視窗（每 30 分鐘複查）——
    公告寫的時間未必精準，目標時間到了但影片還沒上架是常態，只檢查一次很容易誤判過期
    （v25.2.0 上線第一批實測踩到）。期限從「目標時間」而非「現在」起算：中午看到公告說
    「今晚 19:30 更新」，從現在起算 2 小時的話目標時間都還沒到覆蓋就先過期了（v25.2.3
    修正的真實案例）。"""
    try:
        check_dt = datetime.strptime(f"{check_date} {check_time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return {}
    return {
        "retry_deadline": (check_dt + timedelta(hours=2)).isoformat(),
        "retry_interval_minutes": 30,
    }


def _format_postpone(status: ClassifiedStatus) -> str:
    """`@postpone_time@` token：延後後的新排程時間，沒有目標日期時回空字串。"""
    if status.target_date is None:
        return ""
    stamp = status.target_date.isoformat()
    if status.target_time:
        return f"{stamp} {status.target_time}"
    if status.target_window:
        return f"{stamp} {status.target_window[0]}~{status.target_window[1]}"
    return stamp


def _date_or(iso: str | None, now: datetime) -> date:
    if iso:
        try:
            return date.fromisoformat(iso)
        except ValueError:
            pass
    return now.date()


# 匯出；ACTIONS/STATUS_LABELS 從 store.gossip re-export 方便 import 一處
__all__ = [
    "ACTIONS",
    "STATUS_LABELS",
    "HttpGetter",
    "GossipSubEvent",
    "ClassifiedStatus",
    "MatchResult",
    "GossipWatcher",
    "EpisodeCatalog",
    "fetch_gossip_text",
    "fetch_gossip",
    "GossipSnapshot",
    "season_number",
    "season_compatible",
    "build_search_title",
    "match_title_to_sn",
    "split_clauses",
    "parse_gossip_text",
    "baseline_from_schedule",
    "resolve_date",
    "resolve_fuzzy_date",
    "classify_status",
]
