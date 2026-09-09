"""首頁／番劇詳細頁的「快取優先」資料取得。規格見 docs/requirements/anime_cache.md。

每個會 render 番劇封面的 route 都走這裡的 helper，好處：
  1. 能用快取就不打 `ani.gamer.com.tw`（降低風控機率）。
  2. render 前一次把所有 `cover_url` 批次登記進 `image_cache`＋排進背景抓取佇列，
     `templates/*.html` 裡的 `cached_img` filter 才有東西可以指。

`deps.anime_cache` / `deps.image_fetcher` 是可選依賴——`None` 時全部退化成直接爬取
（既有測試不用改）。
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Callable

from bahaad.cache.home_policy import home_refresh_decision
from bahaad.gamer_client.browse import (
    AnimeCard,
    AnimeDetail,
    BrowseError,
    SearchResult,
    WeeklySchedule,
    get_anime_detail,
    get_newanime,
    get_weekly_schedule,
    resolve_ref_sn,
)

logger = logging.getLogger(__name__)

# 首頁「新上架」區塊（代理商上架日排列）變動很慢，給一個固定的短 TTL 就夠（首頁那套
# home_refresh_decision 是為了「等的那集上架了沒」設計的，這個區塊用不到那種即時性）。
_HOMEPAGE_SECTION_CACHE_TTL_SECONDS = 3 * 3600

# 一次頁面 render 最多對 animeRef.php 解析幾個 ref_sn——冷快取時整批連續請求會被站方
# 風控（實測 429），超出的卡片退化成連 /anime/ref/<ref_sn>（點下去才解析）。
_MAX_REF_NETWORK_RESOLVES_PER_BATCH = 12


# 首頁重抓（get_newanime + get_weekly_schedule）是同步打 ani.gamer.com.tw／GNN 的爬取，
# 動輒好幾秒；放在 `/` 請求執行緒裡跑會讓首頁「一直載入中」（使用者 2026-09-08 回報
# 「一旦到首頁就會卡住」，尤其站方風控／cookie 震盪時）。改成：有可用快取就先回快取、
# 重抓丟到背景執行緒；只有「完全沒有快取」（史上第一次開）才擋著等。這個鎖確保同時
# 湧入的多個 `/` 請求只會觸發一次背景重抓。
_home_refresh_lock = threading.Lock()
_home_refresh_running = False


def _spawn_home_refresh(deps) -> None:
    global _home_refresh_running
    with _home_refresh_lock:
        if _home_refresh_running:
            return
        _home_refresh_running = True

    def _worker() -> None:
        global _home_refresh_running
        try:
            _refetch_and_save_home(deps)
        except Exception:  # noqa: BLE001 - 背景重抓失敗就下次再說，快取還頂著
            logger.debug("背景重抓首頁快取失敗", exc_info=True)
        finally:
            with _home_refresh_lock:
                _home_refresh_running = False

    threading.Thread(target=_worker, daemon=True, name="home-cache-refresh").start()


def _refetch_and_save_home(deps):
    """實際打網路重抓本季新番＋週期表並存進快取。回傳 (cards, schedule) 或 None。"""
    http = getattr(deps, "browse_http", None)
    if http is None:
        return None
    cards = get_newanime(http)
    schedule = get_weekly_schedule(http)
    cache = getattr(deps, "anime_cache", None)
    if cache is not None:
        try:
            cache.save_home(cards, schedule)
        except Exception:
            logger.debug("save_home 失敗", exc_info=True)
    _register_images(deps, [c.cover_url for c in cards])
    return cards, schedule


def _register_images(deps, urls) -> None:
    urls = [u for u in urls if u]
    if not urls:
        return
    if getattr(deps, "anime_cache", None) is not None:
        try:
            deps.anime_cache.register_images(urls)
        except Exception:
            logger.debug("register_images 失敗", exc_info=True)
    if getattr(deps, "image_fetcher", None) is not None:
        deps.image_fetcher.enqueue_many(urls)


def home_data(deps) -> tuple[list[AnimeCard], list[WeeklySchedule], str | None]:
    """回傳 (本季新番, 週期表, browse_error)。快取優先——見「觸發 3」的決策。

    **有可用快取時絕不在這裡等網路**：該重抓就丟背景執行緒，這次先回快取，首頁不會
    卡在「載入中」（使用者 2026-09-08「一旦到首頁就會卡住」——舊版是在 `/` 請求
    執行緒裡同步爬 ani.gamer.com.tw／GNN，站方一慢整個首頁就吊死，配上單執行緒
    伺服器連帶整個 UI 卡住）。只有完全沒有快取（史上第一次開、啟動預熱還沒跑完）
    才同步抓一次。
    """
    cache = getattr(deps, "anime_cache", None)
    http = getattr(deps, "browse_http", None)

    cached = cache.get_home() if cache is not None else None

    if cached is not None:
        cards, schedule = cached
        _register_images(deps, [c.cover_url for c in cards])
        if cache is not None and home_refresh_decision(
            datetime.now(), cache.home_fetched_at(), cache.schedule_slots()
        ):
            _spawn_home_refresh(deps)  # 該更新了——背景抓，這次先頂著舊快取
        return cards, schedule, None

    # 完全沒有快取。正常情況下啟動預熱（app_shell._warm_home_cache）已經填好；
    # 真的沒有就同步抓一次（一次性，之後都走上面的快取分支）。
    if http is None:
        return [], [], None

    try:
        cards = get_newanime(http)
        schedule = get_weekly_schedule(http)
    except BrowseError as exc:
        logger.warning("首頁爬取失敗：%s", exc)
        return [], [], str(exc)

    if cache is not None:
        try:
            cache.save_home(cards, schedule)
        except Exception:
            logger.debug("save_home 失敗", exc_info=True)
    _register_images(deps, [c.cover_url for c in cards])
    return cards, schedule, None


def anime_detail_data(
    deps, video_sn: int, *, ttl_days: int, cache_only: bool = False
) -> tuple[AnimeDetail | None, str | None]:
    """回傳 (AnimeDetail, error)。快取沒過期就用快取（不打網路）；過期或沒有就重抓。
    `cache_only=True`（公開模式）：只吃快取，沒有就回 (None, "no_cache")，絕不打動畫瘋。"""
    cache = getattr(deps, "anime_cache", None)
    http = getattr(deps, "browse_http", None)

    if cache is not None:
        hit = cache.get_detail(video_sn)
        if hit is None and cache_only:
            # 這個 sn 本身從沒被個別抓過快取——常見情境：訂閱記的是某一集當下的 sn
            # （例如追番當下卡片顯示的就是最新一集），但那一集自己的詳細頁從沒被瀏覽過
            # 觸發快取；同一部番劇「另一集」的詳細頁快取（episode_group 記過的）內容其實
            # 完全一樣（動畫瘋回的是整季集數清單，抓哪一集的頁面都一樣），借用它一樣正確
            # （使用者 2026-09-06：公開模式新集數的 sn 沒人進過頁面就整部顯示無快取）。
            hit = _sibling_group_detail(cache, video_sn)
        if hit is not None:
            detail, fetched_at = hit
            fresh = (datetime.now() - fetched_at).total_seconds() < ttl_days * 86400
            if fresh or http is None or cache_only:
                _register_images(deps, _detail_image_urls(detail))
                try:
                    _record_episode_group(cache, video_sn, detail)
                except Exception:  # noqa: BLE001
                    logger.debug("record_episode_group 失敗", exc_info=True)
                return detail, None

    if cache_only:
        return None, "no_cache"
    if http is None:
        return None, "browse_http_unavailable"

    try:
        detail = get_anime_detail(http, video_sn)
    except BrowseError as exc:
        logger.warning("番劇詳細頁爬取失敗（sn=%s）：%s", video_sn, exc)
        if cache is not None:
            hit = cache.get_detail(video_sn)
            if hit is not None:  # 爬失敗，退回舊快取
                _register_images(deps, _detail_image_urls(hit[0]))
                return hit[0], None
        return None, str(exc)

    if cache is not None:
        try:
            cache.save_detail(detail)
            _record_episode_group(cache, video_sn, detail)
        except Exception:
            logger.debug("save_detail 失敗", exc_info=True)
    _register_images(deps, _detail_image_urls(detail))
    return detail, None


def _record_episode_group(cache, video_sn: int, detail) -> None:
    """把這部番劇的所有集數 video_sn 記進 episode_group——之後訂閱／改名／標題顯示
    就能「知道任一集屬於哪一部番劇」（使用者 2026-08-31）。"""
    ep_sns = [video_sn]
    for cat in detail.episode_categories:
        for ep in cat.episodes:
            if ep.video_sn:
                ep_sns.append(ep.video_sn)
    cache.record_episode_group(ep_sns, detail.title)


def _sibling_group_detail(cache, video_sn: int) -> tuple[AnimeDetail, datetime] | None:
    """公開模式專用 fallback：`video_sn` 本身沒有個別快取過，但 `episode_group` 記得
    它跟哪些其他 sn 是同一部番劇（某一集的詳細頁快取存檔時，把當時集數清單裡看到的
    全部 video_sn 都記了進去）——借用任一個真的有快取的兄弟集數，內容完全一樣（動畫瘋
    回的是整季集數清單，不是單集內容）。找不到群組或群組內沒有任何一筆有快取就回
    `None`（呼叫端照原本流程回「尚無快取」）。"""
    group_key = cache.group_key_for(video_sn)
    if group_key is None:
        return None
    # 由小到大找——群組通常最早被瀏覽、快取過的就是最早那幾集（sn 通常也比較小），
    # 排序讓結果穩定、好測試，不是必要條件（隨便哪個有快取的兄弟集數內容都一樣）。
    for member_sn in sorted(cache.group_members(group_key)):
        if member_sn == video_sn:
            continue
        hit = cache.get_detail(member_sn)
        if hit is not None:
            return hit
    return None


def homepage_section_data(deps, *, section: str, scrape) -> tuple[list[SearchResult], str | None]:
    """首頁「新上架」區塊（`#blockAnimeNewArrive`）頁的快取優先資料取得——比照
    `anime_detail_data()`：快取沒過期（`_HOMEPAGE_SECTION_CACHE_TTL_SECONDS`）就用快取、
    不打網路；爬失敗退回舊快取。回傳 (卡片清單, browse_error)。

    `section` 是 `store.anime_cache.HOMEPAGE_SECTION_NEW_ARRIVAL`，`scrape` 是
    `gamer_client.browse.get_new_arrivals`（做成參數是為了好測、以後有別的區塊也能重用）。"""
    cache = getattr(deps, "anime_cache", None)
    http = getattr(deps, "browse_http", None)

    if cache is not None:
        hit = cache.get_section(section)
        if hit is not None:
            cards, fetched_at, _extra = hit
            fresh = (datetime.now() - fetched_at).total_seconds() < _HOMEPAGE_SECTION_CACHE_TTL_SECONDS
            if fresh or http is None:
                _register_images(deps, [c.cover_url for c in cards])
                return cards, None

    if http is None:
        return [], None

    try:
        cards = scrape(http)
    except BrowseError as exc:
        logger.warning("首頁區塊爬取失敗（%s）：%s", section, exc)
        if cache is not None:
            hit = cache.get_section(section)
            if hit is not None:  # 爬失敗，退回舊快取
                _register_images(deps, [c.cover_url for c in hit[0]])
                return hit[0], None
        return [], str(exc)

    if cache is not None:
        try:
            cache.save_section(section, cards)
        except Exception:
            logger.debug("save_section 失敗（%s）", section, exc_info=True)
    _register_images(deps, [c.cover_url for c in cards])
    return cards, None


def cached_card_list(
    deps,
    *,
    cache_key: str,
    scrape: Callable[[object], tuple[list[SearchResult], dict]],
    ttl_seconds: int = _HOMEPAGE_SECTION_CACHE_TTL_SECONDS,
) -> tuple[list[SearchResult], dict, str | None]:
    """「近期熱播」「搜尋番劇（依屬性組合）」的快取優先卡片清單（使用者 2026-09-06：
    同一組屬性按過就記起來、下次秒開）。比照 `homepage_section_data()`：快取沒過期就不
    打網路、爬失敗退回舊快取。

    `cache_key` 帶前綴避免撞到別的快取（`list:trending:1`／`search:kw:…`／
    `search:filter:…`）。`scrape(http)` 回 `(卡片, extra)`，`extra` 是要一起快取的小
    dict（分頁旗標等）。回傳 `(卡片, extra, browse_error)`。"""
    cache = getattr(deps, "anime_cache", None)
    http = getattr(deps, "browse_http", None)

    if cache is not None:
        hit = cache.get_section(cache_key)
        if hit is not None:
            cards, fetched_at, extra = hit
            fresh = (datetime.now() - fetched_at).total_seconds() < ttl_seconds
            if fresh or http is None:
                _register_images(deps, [c.cover_url for c in cards])
                return cards, extra, None

    if http is None:
        return [], {}, None

    try:
        cards, extra = scrape(http)
    except BrowseError as exc:
        logger.warning("卡片清單爬取失敗（%s）：%s", cache_key, exc)
        if cache is not None:
            hit = cache.get_section(cache_key)
            if hit is not None:  # 爬失敗，退回舊快取
                _register_images(deps, [c.cover_url for c in hit[0]])
                return hit[0], hit[2], None
        return [], {}, str(exc)

    if cache is not None:
        try:
            cache.save_section(cache_key, cards, extra=extra)
        except Exception:  # noqa: BLE001
            logger.debug("save_section 失敗（%s）", cache_key, exc_info=True)
    _register_images(deps, [c.cover_url for c in cards])
    return cards, extra, None


def resolve_ref_cached(deps, ref_sn: int, *, ttl_days: int) -> int | None:
    """`animeRef.php?sn={ref_sn}`（番劇層級代碼）→ 真正的 `video_sn`，快取優先。

    近期熱播／最新上架／搜尋結果的卡片站方都只給 `ref_sn`，要連到自己的
    `/anime/<video_sn>` 詳細頁、或判斷訂閱狀態，都得先解析。解析要打一次網路（302
    重導），所以快取——但 ref 解析出的是「該番劇最新一集」的 sn，會隨新集數上架而變，
    快取帶 TTL（沿用 `anime_cache_ttl_days`）。解析失敗回快取值或 None。"""
    cache = getattr(deps, "anime_cache", None)
    http = getattr(deps, "browse_http", None)

    if cache is not None:
        hit = cache.get_ref(ref_sn)
        if hit is not None:
            video_sn, resolved_at = hit
            fresh = (datetime.now() - resolved_at).total_seconds() < ttl_days * 86400
            if fresh or http is None:
                return video_sn

    if http is None:
        return None

    try:
        video_sn = resolve_ref_sn(http, ref_sn)
    except Exception:
        # resolve_ref_sn 走 http.get()，可能丟 curl_cffi 的網路例外（DNS／逾時／連線），
        # 那些不是 BrowseError——退回舊快取值（有的話），不讓整頁掛掉
        logger.warning("animeRef 解析失敗（ref_sn=%s）", ref_sn, exc_info=True)
        hit = cache.get_ref(ref_sn) if cache is not None else None
        return hit[0] if hit is not None else None

    if video_sn is not None and cache is not None:
        try:
            cache.save_ref(ref_sn, video_sn)
        except Exception:
            logger.debug("save_ref 失敗", exc_info=True)
    return video_sn


def resolve_refs_cached(
    deps, ref_sns, *, ttl_days: int, max_network: int = _MAX_REF_NETWORK_RESOLVES_PER_BATCH
) -> dict[int, int | None]:
    """批次解析 `ref_sn → video_sn`，快取優先。回傳 `{ref_sn: video_sn|None}`。

    **一次呼叫最多打 `max_network` 次網路**——冷快取時近期熱播（14 張）／最新上架（每頁
    28 張）會對 `animeRef.php` 連續爆量請求，實測（2026-08-27）會被站方回 429。超出上限
    又沒有快取的 `ref_sn` 回 `None`，卡片退化成連 `/anime/ref/<ref_sn>`（點下去才單筆
    解析），重複造訪時 `ref_resolution` 快取會慢慢補齊。"""
    cache = getattr(deps, "anime_cache", None)
    http = getattr(deps, "browse_http", None)
    out: dict[int, int | None] = {}
    budget = max_network
    for ref_sn in ref_sns:
        if ref_sn in out:
            continue
        cached = cache.get_ref(ref_sn) if cache is not None else None
        if cached is not None:
            video_sn, resolved_at = cached
            fresh = (datetime.now() - resolved_at).total_seconds() < ttl_days * 86400
            if fresh or http is None or budget <= 0:
                out[ref_sn] = video_sn  # 新鮮，或沒預算／沒網路重解析：直接用快取值
                continue
        if http is None or budget <= 0:
            out[ref_sn] = cached[0] if cached is not None else None
            continue
        budget -= 1
        out[ref_sn] = resolve_ref_cached(deps, ref_sn, ttl_days=ttl_days)
    return out


def home_bell_sns(deps, newanime, weekly_schedule) -> dict[int, int]:
    """首頁（本季新番卡片＋週期表側板）每個 video_sn → 訂閱鈴鐺該掛的 video_sn。

    1. 這部番劇**已訂閱**（用標題比對到某筆帶 `schedule_weekday` 的排程項目）→ 掛那筆
       訂閱的 sn。番劇每週更新後週期表項目會指向新一集的 sn（跟訂閱項目 sn 不同），
       不重掛的話鈴鐺顯示成「未訂閱」、按下去還會建出第二筆訂閱（使用者 2026-09-05 回報）。
    2. 沒訂閱、但卡片標題在週期表找得到同名項目 → 掛週期表項目的 sn（只有它查得到時段、
       訂閱才寫得進去，見下方 `card_bell_sns` 原註）。

    純標題比對、零額外請求（首頁一次 render 內資料同源）。訂閱項目的標題從番劇快取
    （`anime_title_for`，訂閱時 `_mark_backlog_downloaded` 會把整季集數＋標題寫進快取）
    或使用者更名拿。"""
    entries = deps.schedule_store.get_entries() if getattr(deps, "schedule_store", None) else {}
    subscribed = {sn: e for sn, e in entries.items() if e.schedule_weekday is not None}
    cache = getattr(deps, "anime_cache", None)

    sub_by_title: dict[str, int] = {}
    for sn, entry in subscribed.items():
        keys: list[str] = []
        if cache is not None:
            try:
                title = cache.anime_title_for(sn)
            except Exception:  # noqa: BLE001
                title = None
            if title:
                keys.append(title.strip())
        if entry.rename:
            keys.append(entry.rename.strip())
        for key in keys:
            sub_by_title.setdefault(key, sn)

    sched_by_title: dict[str, int] = {}
    for weekly in weekly_schedule:
        for entry in weekly.entries:
            title = (entry.title or "").strip()
            if title:
                sched_by_title.setdefault(title, entry.video_sn)

    out: dict[int, int] = {}
    for weekly in weekly_schedule:
        for entry in weekly.entries:
            target = sub_by_title.get((entry.title or "").strip())
            if target is not None and target != entry.video_sn:
                out[entry.video_sn] = target
    for card in newanime:
        title = (card.title or "").strip()
        target = sub_by_title.get(title) or sched_by_title.get(title)
        if target is not None and target != card.video_sn:
            out[card.video_sn] = target
    return out


def card_bell_sns(newanime, weekly_schedule) -> dict[int, int]:
    """首頁本季新番卡片的 video_sn → 訂閱鈴鐺該掛的 video_sn（見 web_redesign_round3.md
    階段 4）。

    本季新番卡片（首頁時間軸）跟週期表項目可能是同一部番劇的**不同 video_sn**。訂閱
    寫入走週期表時段查詢（`web/browse.py subscribe()` → `_weekly_schedule_time()`），
    只有出現在週期表裡的 sn 查得到星期＋時間。所以卡片標題若在週期表找得到同名項目，
    鈴鐺就改掛那個項目的 sn——這樣：(1) 卡片鈴鐺也訂閱得成功；(2) 卡片鈴鐺跟週期表
    側板那顆共用同一個 sn，`subscribe.js` 會把頁面上所有 `data-video-sn` 相同的鈴鐺
    一起切換金色。配不到（或本來就同 sn）就維持卡片自己的 sn。

    用標題字面配對：首頁一次 render 內資料同源、字串一致機率高，而且零額外請求
    （resolve ref_sn 要打網路）。"""
    schedule_sn_by_title: dict[str, int] = {}
    for weekly in weekly_schedule:
        for entry in weekly.entries:
            title = (entry.title or "").strip()
            if title:
                schedule_sn_by_title.setdefault(title, entry.video_sn)
    out: dict[int, int] = {}
    for card in newanime:
        rep = schedule_sn_by_title.get((card.title or "").strip())
        if rep is not None and rep != card.video_sn:
            out[card.video_sn] = rep
    return out


# 相關動畫的 `ref_sn → video_sn` 解析改由 web/browse.py 的 `_section_items()` 處理
# （跟近期熱播／最新上架同一套「快取優先＋每次 render 有網路預算上限」），而且只解析
# 「點擊載入更多」目前這一批，不再一次解析全部——見 web_redesign_round3.md 階段 3-1。


def _detail_image_urls(detail: AnimeDetail) -> list[str]:
    return [detail.cover_url, *(r.cover_url for r in detail.related_anime)]


def group_newanime_by_date(cards) -> list[dict]:
    """.0 改進.txt 第 23 項：首頁「本季新番」依 `AnimeCard.air_date` 分組，仿官方時間軸
    在每組上方標日期（"09/01 (二)"）。站方已經照「最近更新日」由新到舊排好，這裡只把
    連續同一天的併成一組、維持原順序。回傳 `[{label, cards}, ...]`。"""
    groups: list[dict] = []
    for card in cards:
        label = getattr(card, "air_date", "") or "其他"
        if groups and groups[-1]["label"] == label:
            groups[-1]["cards"].append(card)
        else:
            groups.append({"label": label, "cards": [card]})
    return groups
    return out
