"""瀏覽類頁面（近期熱播／最新上架／搜尋番劇／訂閱列表／下載列表）＋ UI 偏好設定。
規格見 docs/requirements/web_redesign.md。

Phase 1（主題／側欄殼層）只把導覽骨架搭出來——這五個頁面目前都是「開發中」佔位內容，
真正的資料要等 `gamer_client/browse.py`（見 docs/requirements/gamer_client_browse.md，
尚未實作）跟後續階段接上去才會有東西可以渲染。先讓側欄六個項目全部可以點、頁面殼層一致，
之後每個階段依序把對應頁面的佔位內容換成真正內容，不用等全部做完才能看到殼層動起來。
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlencode

from flask import (
    Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request,
    send_file, url_for,
)

from bahaad.gamer_client.browse import (
    ANIME_LIST_CATEGORIES,
    ANIME_LIST_MAX_TAGS,
    ANIME_LIST_TAGS,
    ANIME_LIST_TARGETS,
    BrowseError,
    _extract_sn,
    get_new_arrivals,
    get_weekly_schedule,
    list_anime,
    search_anime,
)
from bahaad.store.anime_cache import HOMEPAGE_SECTION_NEW_ARRIVAL
from bahaad.gamer_client.catalog import (
    CatalogError,
    GamerLoginStale,
    ParentPasswordRequired,
    WatchingPermissionDenied,
)
from bahaad.scheduler.main_loop import DownloadTrigger

# rename 時擋掉 Windows 檔名不允許的符號——下載時 naming.sanitize_filename_part 會把
# 這些換成全形，但使用者手打的資料夾名還是提示他們別用比較清楚
_ILLEGAL_RENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*]')
from bahaad.store.schedule_list import (
    _tag_before,
    _upsert_entry,
    parse_text,
    render_text,
    schedule_sort_key,
)
from bahaad.stats.collector import resolve_first_ep_sn
from bahaad.subscription_ops import subscribe_sn, unsubscribe_sn
from bahaad.web.db_cleanup import delete_records as _delete_orphan_records
from bahaad.web.public_mode import (
    hidden_titles as _public_hidden_titles,
    set_title_public as _set_title_public,
    title_allowed as _public_title_allowed,
)

browse_bp = Blueprint("browse", __name__)

_THEMES = {"light", "dark"}


def _is_dub_category(name: str) -> bool:
    """站方集數分類名裡的「中文配音」「中文電影」——中文配音版本（custom-features #13）。"""
    return "配音" in name or name.startswith("中文")


def _dub_included(deps) -> bool:
    """設定 `show_dub_episodes`（全站，預設關）或這次請求帶了 `dub=1`（「僅這次顯示」，
    離開頁面就恢復）——任一成立就把中文配音集數也算進去。"""
    if deps.settings.get("show_dub_episodes", False):
        return True
    return request.values.get("dub") == "1"


def _visible_episode_categories(deps, categories):
    """`_dub_included()` 為 False 時，過濾掉中文配音的集數分類。"""
    if _dub_included(deps):
        return list(categories)
    return [c for c in categories if not _is_dub_category(c.name)]

logger = logging.getLogger(__name__)

# video_sn -> {"anime_title", "episode_number", "cover_url"}，下載佇列卡片用。這份
# 資料在整個下載過程中不會變，查過一次的 video_sn 快取起來，避免每次輪詢（每 1 秒
# 一次，見 downloads_queue.js）都重新打一次 catalog.py 的 JSON API——是行程存活期間
# 的記憶體快取，不需要持久化，程式重啟後重新查一次就好。
_episode_info_cache: dict[int, dict] = {}


def _lookup_episode_info(catalog, video_sn: int) -> dict:
    if video_sn in _episode_info_cache:
        return _episode_info_cache[video_sn]
    _EMPTY = {"anime_title": "", "episode_number": None, "cover_url": ""}
    try:
        video = catalog.get_video(video_sn)
    except (WatchingPermissionDenied, ParentPasswordRequired) as exc:
        # 權限問題不會因為輪詢而改變——**負快取**，別讓下載佇列頁每次輪詢（每 1 秒，
        # 見 downloads_queue.js）都對站方重打一次 video.php、還每次都寫一條 warning
        # （使用者 2026-09-08：一個失敗任務就狂洗日誌）。重新登入後要看到標題／封面
        # 就換頁重進即可（`_episode_info_cache` 是行程存活期記憶體快取）。
        logger.debug("下載佇列查詢集數資訊：sn=%s 無觀看權限，這個 session 不再重查（%s）", video_sn, exc)
        _episode_info_cache[video_sn] = _EMPTY
        return _EMPTY
    except CatalogError as exc:
        # 網路／回應異常——**不**負快取，下次輪詢再試（可能只是暫時的）。
        logger.debug("下載佇列查詢集數資訊失敗（sn=%s）：%s", video_sn, exc)
        return _EMPTY

    cover_url = ""
    for ep in video.episodes:
        if ep.video_sn == video_sn:
            cover_url = ep.cover
            break

    info = {"anime_title": video.anime_title, "episode_number": video.episode_number, "cover_url": cover_url}
    _episode_info_cache[video_sn] = info
    return info


def _dl_display_name(deps, sn: int, fallback_title: str, registry_name: str | None = None) -> str:
    """下載列表卡片的番劇名——優先用「使用者已更改的番劇名」（registry 記的
    `display_name` ＝ `entry.rename` 或乾淨標題；失敗卡片沒有 registry 名時退回排程
    `rename` → catalog 標題 → episode_group 反查 → `sn N`）。使用者 2026-09-03：
    不要顯示原始標題或 sn。"""
    if registry_name:
        return registry_name
    try:
        entry = deps.schedule_store.get_entries().get(sn) if deps.schedule_store else None
    except Exception:  # noqa: BLE001
        entry = None
    if entry is not None and entry.rename:
        return entry.rename
    if fallback_title:
        return fallback_title
    cache = getattr(deps, "anime_cache", None)
    if cache is not None:
        try:
            title = cache.anime_title_for(sn)
        except Exception:  # noqa: BLE001
            title = None
        if title:
            return title
    return f"sn {sn}"


def _dl_episode_label(deps, episode_number) -> str:
    """「第 008 集」——依「補齊長度」設定補零（跟檔名／通知同一支 naming.episode_zh）。"""
    if episode_number is None or episode_number == "":
        return ""
    from bahaad.downloader.naming import DEFAULT_PAD_WIDTH, episode_zh

    return episode_zh(episode_number, deps.settings.get("filename_pad_width", DEFAULT_PAD_WIDTH))


def _weekly_schedule_time(deps, video_sn: int) -> tuple[int, int, int] | None:
    """從首頁週期表找這個 sn 對應的星期＋時間，找不到（多半是已完結的舊番，不在
    本季新番／週期表範圍內）回傳 None，見 web_redesign.md「訂閱功能」定案：訂閱
    時段取自週期表。`weekday` 用 store/schedule_list.py 的編碼（1=一...7=日），
    `get_weekly_schedule()` 本身已經照週一到週日的順序回傳，所以用 enumerate 從
    1 開始數就對得上，不用另外查表轉換。

    快取有週期表就用快取（訂閱＝一次網頁互動，不值得為了它再爬一次首頁）。

    比對策略（round5 項目 8）：週期表項目連的是「當週最新一集」的 video_sn，但近期熱播／
    最新上架／搜尋的卡片 video_sn 是 `animeRef.php` 解析結果（可能是該番劇另一集的 sn，
    且快取 TTL 很長）——兩者常對不上。先做精確比對，不中時退化成「這個 sn 屬於的番劇
    有沒有哪一集出現在週期表」（比照 gossip 的集數集合交集，`anime_detail_data` 快取優先）。"""
    schedule = None
    if getattr(deps, "anime_cache", None) is not None:
        cached = deps.anime_cache.get_home()
        if cached is not None:
            schedule = cached[1]
    if schedule is None:
        if deps.browse_http is None:
            raise BrowseError("browse_http 未設定")
        schedule = get_weekly_schedule(deps.browse_http)

    def _time_of(entry) -> tuple[int, int, int] | None:
        try:
            hour_str, minute_str = entry.time_text.split(":")
            return weekday, int(hour_str), int(minute_str)
        except ValueError:
            return None

    for weekday, weekly in enumerate(schedule, start=1):
        for entry in weekly.entries:
            if entry.video_sn == video_sn:
                return _time_of(entry)

    # 精確比對不中：查這個 video_sn 屬於的番劇的所有集數，任一集在週期表上就算「正在更新」
    episode_sns = _anime_episode_sns(deps, video_sn)
    if episode_sns:
        for weekday, weekly in enumerate(schedule, start=1):
            for entry in weekly.entries:
                if entry.video_sn in episode_sns:
                    return _time_of(entry)
    return None


def _anime_episode_sns(deps, video_sn: int) -> set[int]:
    """這個 video_sn 屬於的番劇的全部集數 video_sn 集合（快取優先，抓不到／出錯回空集合
    ——這是訂閱時的盡力補救，不該讓整個訂閱請求炸掉）。"""
    from bahaad.web.anime_data import anime_detail_data
    from bahaad.web.settings import _DEFAULT_ANIME_CACHE_TTL_DAYS

    try:
        ttl_days = int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS))
        detail, _err = anime_detail_data(deps, video_sn, ttl_days=ttl_days)
    except Exception:  # noqa: BLE001
        logger.debug("訂閱時查集數集合失敗（sn=%s）", video_sn, exc_info=True)
        return set()
    if detail is None:
        return set()
    return {
        ep.video_sn
        for category in detail.episode_categories
        for ep in category.episodes
    }


# 集數選取器的每一格顏色：藍＝已下載／黃＝正在下載／紅＝下載失敗／紫＝曾下載檔案已移除／
# 灰＝未下載。番劇詳細頁初次 render 跟「即時輪詢」端點（.0 改進.txt 第 13 項）共用這套算法。
_EPISODE_CELL_STATES = ("not-downloaded", "downloading", "downloaded", "removed", "failed")


def _episode_states(deps, detail, episode_categories) -> dict[int, str]:
    """`{episode video_sn: cell state}`——只讀 `main_loop`／`registry` 的記憶體狀態，
    不打網路。`main_loop`／`registry` 是可選依賴（`dev_web_server` 之類的輕量場景不給），
    沒給就全部當「未下載」。"""
    downloaded_sns: set[int] = set()
    removed_sns: set[int] = set()
    failed_sns: set[int] = set()
    if deps.main_loop is not None:
        downloaded_sns = deps.main_loop.downloaded_episode_sns(detail.title)
        if hasattr(deps.main_loop, "removed_episode_sns"):
            removed_sns = deps.main_loop.removed_episode_sns(detail.title)
    if deps.registry is not None:
        try:
            failed_sns = set(deps.registry.failing_snapshot().keys())
        except Exception:  # noqa: BLE001
            failed_sns = set()

    states: dict[int, str] = {}
    for category in episode_categories:
        for ep in category.episodes:
            if deps.registry is not None and deps.registry.is_active(ep.video_sn):
                states[ep.video_sn] = "downloading"
            elif ep.video_sn in downloaded_sns:
                states[ep.video_sn] = "downloaded"
            elif ep.video_sn in failed_sns:
                # .0 改進.txt 第 13 項：下載失敗的集數也要標出來（紅框）
                states[ep.video_sn] = "failed"
            elif ep.video_sn in removed_sns:
                # round 7 第 7 項：曾下載過、檔案已被移除 → 紫框
                states[ep.video_sn] = "removed"
            else:
                states[ep.video_sn] = "not-downloaded"
    return states


@browse_bp.route("/anime/<int:video_sn>")
def anime_detail(video_sn: int):
    """番劇詳細頁。Phase C，見 docs/requirements/web_redesign.md「加入動畫／番劇詳細頁」。
    快取優先（點擊番劇＝觸發 1，見 anime_cache.md）——快取沒過期就不打網路。"""
    from bahaad.web.anime_data import anime_detail_data
    from bahaad.web.settings import _DEFAULT_ANIME_CACHE_TTL_DAYS
    from bahaad.web.youranimes_view import youranimes_panel

    deps = current_app.config["DEPS"]
    public = bool(getattr(g, "public_readonly", False))
    ttl_days = int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS))
    detail, error = anime_detail_data(deps, video_sn, ttl_days=ttl_days, cache_only=public)
    if detail is None:
        if error == "no_cache":  # 公開模式、這部番劇還沒建立快取
            return render_template("anime_no_cache.html", message="該番劇尚無快取，請登入後建立快取。"), 200
        abort(503 if error == "browse_http_unavailable" else 502)
    if public and not _public_title_allowed(deps.settings, detail.title):
        return render_template("anime_no_cache.html", message="這部番劇未開放公開觀看。"), 403

    # youranimes.tw 補充：配到就用它的簡介、右側多一欄製作/配音/音樂；配不到或該欄
    # 缺資料就退回動畫瘋原本的（見 docs/requirements/youranimes.md）。純讀本地 DB、
    # 不打網路，所以公開模式也照給（使用者 2026-09-04：公開模式番劇頁要跟登入後一致）。
    youranimes = youranimes_panel(deps, detail)
    synopsis = (youranimes.synopsis if youranimes and youranimes.synopsis else "") or detail.description

    # 相關動畫給的是 animeRef.php 的關聯代碼，不是真正的 video_sn——初次 render 只帶
    # 前 _RELATED_ANIME_PAGE_SIZE 個、只解析這一批的 ref_sn（整批解析＝風控），其餘
    # 靠「點擊載入更多」（/anime/<sn>/related）分批補。查不到 video_sn 的卡片退化成
    # 連 /anime/ref/<ref_sn>（點下去才單筆解析），跟其他 browse 頁一致。
    # 公開模式：不解析 ref_sn（會打網路），相關動畫直接不列。
    related_items = [] if public else _section_items(deps, detail.related_anime[:_RELATED_ANIME_PAGE_SIZE])
    related_has_next = (not public) and len(detail.related_anime) > _RELATED_ANIME_PAGE_SIZE

    # 集數選取器要依下載狀態上色（灰＝未下載／黃＝正在下載／藍＝已下載），見
    # web_redesign.md「集數選取器」——main_loop／registry 兩個依賴都是可選的
    # （dev_web_server.py 這種輕量測試用途不會給），沒給就全部當「未下載」處理
    # 中文配音集數分類：設定沒開、且這次沒帶 dub=1 就不顯示（custom-features #13）
    episode_categories = _visible_episode_categories(deps, detail.episode_categories)
    has_dub = any(_is_dub_category(c.name) for c in detail.episode_categories)
    dub_setting_on = bool(deps.settings.get("show_dub_episodes", False))
    dub_this_time = request.args.get("dub") == "1"

    episode_states = _episode_states(deps, detail, episode_categories)

    # 作品分類標籤：值對得上「搜尋番劇」篩選面板的屬性（`ANIME_LIST_TAGS`）時做成連結，
    # 點了跳到 /browse/search?tags=<標籤>（使用者 2026-09-06）。公開模式沒有搜尋頁 → 純文字。
    genre_search = None if public else set(ANIME_LIST_TAGS)

    # 即時統計 key＝首集 sn（跟訂閱／收藏對齊）。episode_group 有記就用它，否則用
    # 本篇第一集、再不行用這頁自己的 sn。
    stats_first_ep_sn, _t = resolve_first_ep_sn(getattr(deps, "anime_cache", None), video_sn)
    if stats_first_ep_sn == video_sn:
        for _cat in detail.episode_categories or []:
            if _cat.episodes:
                try:
                    stats_first_ep_sn = int(_cat.episodes[0].video_sn)
                except (TypeError, ValueError, AttributeError):
                    pass
                break

    return render_template(
        "anime_detail.html",
        detail=detail,
        stats_first_ep_sn=stats_first_ep_sn,
        synopsis=synopsis,
        genre_search=genre_search,
        youranimes=youranimes,
        episode_categories=episode_categories,
        has_dub=has_dub,
        dub_setting_on=dub_setting_on,
        dub_this_time=dub_this_time,
        related_items=related_items,
        related_has_next=related_has_next,
        related_next_offset=_RELATED_ANIME_PAGE_SIZE,
        episode_states=episode_states,
        # 播放器串流來源：公開模式一律 HLS；登入則自動判斷（從外面連進來＝非 loopback／
        # 私網 → HLS，本機／區網 → mp4）。使用者 2026-09-04。
        remote_playback=(public or _is_remote_request()) and getattr(deps, "hls_cache", None) is not None,
    )


@browse_bp.route("/anime/<int:video_sn>/episode-states")
def anime_episode_states(video_sn: int):
    """集數選取器的即時狀態輪詢（.0 改進.txt 第 13 項）——停在番劇頁時，下載完成／
    失敗／檔案被刪都要即時反映到每一格的顏色。只讀本地快取＋`registry`／`main_loop`
    的記憶體狀態，**不打網路**（快取沒有這部番劇就回空）。回 `{states: {sn: state}}`，
    key 是字串（JSON 物件 key 一律字串）。"""
    deps = current_app.config["DEPS"]
    cache = getattr(deps, "anime_cache", None)
    hit = cache.get_detail(video_sn) if cache is not None else None
    if hit is None:
        return jsonify({"states": {}})
    detail = hit[0]
    if getattr(g, "public_readonly", False) and not _public_title_allowed(deps.settings, detail.title):
        return jsonify({"states": {}})
    states = _episode_states(deps, detail, detail.episode_categories)
    return jsonify({"states": {str(sn): state for sn, state in states.items()}})


def _downloaded_mp4(deps, video_sn: int) -> str | None:
    return deps.main_loop.downloaded_episode_path(video_sn) if deps.main_loop is not None else None


def _public_episode_blocked(deps, video_sn: int) -> bool:
    """公開模式且開了「只公開勾選的番劇」時，這一集屬於的番劇不在白名單 → 擋。"""
    if not getattr(g, "public_readonly", False):
        return False
    title = deps.main_loop.downloaded_episode_anime_title(video_sn) if deps.main_loop is not None else None
    return not _public_title_allowed(deps.settings, title)


def _is_remote_request() -> bool:
    """請求是不是「從外面連進來」的——loopback／私網位址＝本機或區網，用 mp4 直接串；
    其他（含 Cloudflare 代理過來、remote_addr 是 CF 的公網 IP）＝遠端，改用 HLS 串流
    （小片段、斷線只重抓一段，網路不穩比一個大 mp4 耐斷，使用者 2026-09-04）。"""
    import ipaddress

    try:
        ip = ipaddress.ip_address((request.remote_addr or "").strip())
    except ValueError:
        return False
    return not (ip.is_loopback or ip.is_private or ip.is_link_local)


@browse_bp.route("/anime/episode/<int:video_sn>/play")
def play_episode(video_sn: int):
    """已下載集數的瀏覽器內建播放（使用者 2026-09-04）：集數選取器點到「已下載」（藍框）
    的那一集會冒出「播放」鈕，開這個端點。只吐 `downloaded_episodes` 表裡登記過、且檔案
    還在磁碟上的路徑（不接受任意路徑），`conditional=True` 讓 `<video>` 能 range 拖曳。"""
    deps = current_app.config["DEPS"]
    if _public_episode_blocked(deps, video_sn):
        abort(404)
    path = _downloaded_mp4(deps, video_sn)
    if not path:
        abort(404)
    return send_file(path, mimetype="video/mp4", conditional=True, max_age=0)


@browse_bp.route("/anime/episode/<int:video_sn>/hls/index.m3u8")
def play_episode_hls(video_sn: int):
    """遠端播放用的 HLS 播放清單——`web/hls.py` 第一次被要時用 `ffmpeg -c copy` 把已下載
    的 mp4 重封裝成 fMP4 片段（不轉檔、不動原始 mp4），之後吃快取。"""
    deps = current_app.config["DEPS"]
    if _public_episode_blocked(deps, video_sn):
        abort(404)
    cache = getattr(deps, "hls_cache", None)
    path = _downloaded_mp4(deps, video_sn)
    if cache is None or not path:
        abort(404)
    m3u8 = cache.playlist_path(video_sn, path)
    if m3u8 is None:
        abort(404)
    return send_file(m3u8, mimetype="application/vnd.apple.mpegurl", conditional=True, max_age=0)


@browse_bp.route("/anime/episode/<int:video_sn>/hls/<seg>")
def play_episode_hls_segment(video_sn: int, seg: str):
    deps = current_app.config["DEPS"]
    if _public_episode_blocked(deps, video_sn):
        abort(404)
    cache = getattr(deps, "hls_cache", None)
    p = cache.segment_path(video_sn, seg) if cache is not None else None
    if p is None:
        abort(404)
    mimetype = "video/mp4" if seg.endswith(".mp4") else "video/iso.segment"
    return send_file(p, mimetype=mimetype, conditional=True, max_age=3600)


@browse_bp.route("/anime/<int:video_sn>/related")
def anime_related(video_sn: int):
    """相關動畫「點擊載入更多」——回下一批卡片 HTML 片段（見 web_redesign_round3.md
    階段 3-1）。相關動畫清單本身從 detail 快取拿（多半不打網路），只有這一批的
    `ref_sn` 要解析、且受 `_section_items` 的每次網路預算上限保護。前端 load_more.js
    用回傳的 `next_offset`／`has_next` 決定要不要再抓。"""
    from bahaad.web.anime_data import anime_detail_data
    from bahaad.web.settings import _DEFAULT_ANIME_CACHE_TTL_DAYS

    deps = current_app.config["DEPS"]
    ttl_days = int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS))
    offset = max(request.args.get("offset", default=0, type=int) or 0, 0)
    detail, _err = anime_detail_data(deps, video_sn, ttl_days=ttl_days)
    related = detail.related_anime if detail is not None else []
    batch = related[offset : offset + _RELATED_ANIME_PAGE_SIZE]
    html = render_template(
        "_card_grid_fragment.html", items=_section_items(deps, batch), show_bell=False
    )
    next_offset = offset + len(batch)
    return jsonify(
        {"html": html, "has_next": len(related) > next_offset, "next_offset": next_offset}
    )


@browse_bp.route("/anime/<int:video_sn>/download", methods=["POST"])
def download_episodes(video_sn: int):
    """集數選取器提交下載（見 web_redesign_round3.md 階段 1-1）：
    - 沒帶 `download_all` → 下載所有勾選的集數（跨分類）。沒勾＝提示。
    - `download_all="__all__"` → 整部番劇全部集數。
    - `download_all="<分類名>"` → 該分類全部集數。
    「全部」的集數清單由後端從 detail 快取自己拿，不信前端。跟 web/manual_download.py
    共用同一套 `trigger_manual_download()` 觸發邏輯。

    **資料夾名稱**（階段 6-5）：這個 `video_sn` 是訂閱項目（`schedule_weekday` 不是 None）
    → 沿用訂閱時設的 `rename`（沒設＝原標題）；不是訂閱項目 → 用前端問到的 `rename`
    表單欄位（`episode_picker.js` 在提交前跳 `showPrompt`）。"""
    from bahaad.web.anime_data import anime_detail_data
    from bahaad.web.settings import _DEFAULT_ANIME_CACHE_TTL_DAYS

    deps = current_app.config["DEPS"]
    if deps.main_loop is None:
        abort(503)

    entry = deps.schedule_store.get_entries().get(video_sn)
    if entry is not None and entry.schedule_weekday is not None:
        rename = entry.rename
    else:
        rename = (request.form.get("rename") or "").strip() or None

    download_all = (request.form.get("download_all") or "").strip()
    if download_all:
        ttl_days = int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS))
        detail, _err = anime_detail_data(deps, video_sn, ttl_days=ttl_days)
        if detail is None or not detail.episode_categories:
            flash("抓不到集數清單，無法下載")
            return redirect(url_for("browse.anime_detail", video_sn=video_sn))
        # 中文配音集數：設定沒開時「整個番劇 全部下載」也不含配音版（跟番劇頁一致）
        selected = [
            ep.video_sn
            for category in _visible_episode_categories(deps, detail.episode_categories)
            if download_all == "__all__" or download_all == category.name
            for ep in category.episodes
        ]
        if not selected:
            flash("找不到對應分類的集數")
            return redirect(url_for("browse.anime_detail", video_sn=video_sn))
    else:
        selected = [int(v) for v in request.form.getlist("video_sn") if v.isdigit()]
        if not selected:
            flash("請先選擇要下載的集數，或按「全部下載」")
            return redirect(url_for("browse.anime_detail", video_sn=video_sn))

    submitted = already = active = errors = 0
    for sn in selected:
        if deps.manual_task_store is not None:
            deps.manual_task_store.save_task(sn, "single", {"rename": rename})
        try:
            result = deps.main_loop.trigger_manual_download(sn, rename=rename)
        except CatalogError as exc:
            errors += 1
            logger.warning("集數選取器下載失敗（sn=%s）：%s", sn, exc)
            continue
        if result == DownloadTrigger.SUBMITTED:
            submitted += 1
        elif result == DownloadTrigger.ALREADY_DOWNLOADED:
            already += 1
        elif result == DownloadTrigger.ALREADY_ACTIVE:
            active += 1

    parts = [f"已送出 {submitted} 集下載"]
    if already:
        parts.append(f"{already} 集已下載過")
    if active:
        parts.append(f"{active} 集正在下載中")
    if errors:
        parts.append(f"{errors} 集失敗")
    flash("，".join(parts))
    return redirect(url_for("browse.anime_detail", video_sn=video_sn))


# ---- 近期熱播／最新上架／搜尋番劇（左側導覽第 2～4 項，見 web_redesign.md）----
# 三頁的卡片站方都是 `a.theme-list-main` + `animeRef.php?sn={ref_sn}`（番劇層級代碼），
# 不是首頁本季新番那種 video_sn——連到自己的 /anime/<video_sn> 詳細頁要先解析。
#
# 資料來源（2026-08-27 使用者定案，見 web_redesign.md）：
#   近期熱播 = `list_anime(sort=2)`＝所有動畫「依月人氣排序」，穩定、有意義，下滑無限捲動
#             （首頁 #blockHotAnime 每次載入隨機洗牌、不適合快取，不用）
#   最新上架 = 首頁 #blockAnimeNewArrive（代理商上架日排列，穩定，只有 ~21 筆、無「看更多」）
#             animeList.php 只有「依年份」「依月人氣」、沒有「依上架日」，重現不了「最新上架」

_SEARCH_RESULT_LIMIT = 60
_TRENDING_SORT_MONTHLY_POPULARITY = 2

# 「點擊載入更多」每批筆數（見 web_redesign_round3.md 階段 3）。相關動畫初次 render
# 只帶前 10 個、只解析這 10 個的 ref_sn（數量多時整批解析＝風控）；搜尋結果站方
# 一次給完，前 24 筆先顯示、其餘藏起來靠按鈕逐批顯示（不再打網路）。
_RELATED_ANIME_PAGE_SIZE = 10
_SEARCH_PAGE_SIZE = 24


def _card_item(result, video_sn: int | None) -> dict:
    """`SearchResult` (+ 已解析的 video_sn，可能 None) → 樣板／JSON 卡片 dict。
    有 video_sn → 卡片直接連詳細頁；沒有 → 連 `/anime/ref/<ref_sn>` 重導向路由
    （點下去才即時解析）。`year_text` 去掉「年份：」前綴（新上架卡片是「上架日：MM/DD」，
    那個標籤有意義、留著）。"""
    from bahaad.web.cache import cached_img_url

    if video_sn is not None:
        detail_url = url_for("browse.anime_detail", video_sn=video_sn)
    else:
        detail_url = url_for("browse.anime_by_ref", ref_sn=result.ref_sn)
    year_text = result.year_text.removeprefix("年份：").strip()
    episode_count_text = result.episode_count_text.strip()
    meta = " ・ ".join(part for part in (year_text, episode_count_text) if part)
    return {
        "ref_sn": result.ref_sn,
        "video_sn": video_sn,
        "title": result.title,
        "cover_url": result.cover_url,
        "cover_src": cached_img_url(result.cover_url),
        "year_text": year_text,
        "episode_count_text": episode_count_text,
        "meta": meta,
        "detail_url": detail_url,
    }


def _section_items(deps, results) -> list[dict]:
    """一批 `SearchResult` → 卡片 dict 清單（登記封面圖＋快取優先解析 ref_sn）。"""
    from bahaad.web.anime_data import _register_images, resolve_refs_cached
    from bahaad.web.settings import _DEFAULT_ANIME_CACHE_TTL_DAYS

    _register_images(deps, [r.cover_url for r in results])
    ttl_days = int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS))
    resolved = resolve_refs_cached(deps, [r.ref_sn for r in results], ttl_days=ttl_days)
    return [_card_item(r, resolved.get(r.ref_sn)) for r in results]


def _trending_page(deps, page: int) -> dict:
    """`animeList.php?sort=2&page=N` 一頁 → `{"html": <卡片片段>, "has_next": bool, "page": N}`。
    每一頁的結果快取（`list:trending:<page>`，3 小時 TTL，使用者 2026-09-06：加快載入）——
    「依月人氣」變動很慢，短 TTL 就夠。卡片片段用 `_card_grid_fragment.html`。"""
    from bahaad.web.anime_data import cached_card_list

    if deps.browse_http is None:
        return {"html": "", "has_next": False, "page": page, "error": "browse_http_unavailable"}

    def _scrape(http):
        p = list_anime(http, sort=_TRENDING_SORT_MONTHLY_POPULARITY, page=page)
        return p.results, {"has_next": p.has_next}

    cards, extra, err = cached_card_list(deps, cache_key=f"list:trending:{page}", scrape=_scrape)
    if err:
        return {"html": "", "has_next": False, "page": page, "error": err}
    # 使用者 2026-09-05：近期熱播列的番劇大多是舊番/完結番，訂閱多半會被週期表拒絕；
    # 拿掉鈴鐺順便避開「剛訂閱、還沒導頁完成前這裡就看到舊狀態」的競態（訂閱只留在
    # 首頁／番劇詳細頁／訂閱列表，那幾個地方本來就沒有這個問題）。
    html = render_template("_card_grid_fragment.html", items=_section_items(deps, cards), show_bell=False)
    return {"html": html, "has_next": bool(extra.get("has_next")), "page": page}


@browse_bp.route("/browse/trending")
def trending():
    """近期熱播——`animeList.php?sort=2`（依月人氣排序）。第一頁由前端 `trending.js`
    自動打 `/browse/trending/api?page=N` 拿，之後靠「點擊載入更多」按鈕手動補下一頁
    （round 7 第 3 項，不再下滑無限捲動）。卡片比照首頁：鈴鐺＋連自己的詳細頁。"""
    return render_template("trending.html")


@browse_bp.route("/browse/trending/api")
def trending_api():
    deps = current_app.config["DEPS"]
    page = request.args.get("page", default=1, type=int) or 1
    return jsonify(_trending_page(deps, max(page, 1)))


@browse_bp.route("/browse/latest")
def latest():
    """最新上架——首頁 `#blockAnimeNewArrive` 區塊（代理商依上架日排列，只有 ~21 筆、
    無「看更多」）。比照近期熱播：畫面先出殼＋置中的載入動畫，內容交給前端 `latest.js`
    打 `/browse/latest/api` 拿（server-render 冷快取時要等上游爬取，單執行緒 web 會整頁
    卡住數秒，使用者以為沒點到就重複點擊）。"""
    return render_template("latest.html")


@browse_bp.route("/browse/latest/api")
def latest_api():
    """最新上架的內容：`homepage_section_data`（快取優先，3 小時 TTL）一次給完 ~21 筆，
    沒有分頁。回 `{"html": <卡片片段>, "error": <browse_error 或 None>}`。"""
    from bahaad.web.anime_data import homepage_section_data

    deps = current_app.config["DEPS"]
    results, browse_error = homepage_section_data(
        deps, section=HOMEPAGE_SECTION_NEW_ARRIVAL, scrape=get_new_arrivals
    )
    # 使用者 2026-09-05：最新上架同理，拿掉鈴鐺（見 _trending_page 的說明）。
    html = render_template(
        "_card_grid_fragment.html", items=_section_items(deps, results), show_bell=False
    )
    return jsonify({"html": html, "error": browse_error})


_ANIME_LIST_ALL = "全部"


def _parse_filter_args():
    """搜尋頁篩選面板的 query args → `(tags, category, target)`，全部驗證過只留站方
    認得的值（`category`／`target` 不合法或空 → `""`＝「全部」＝不篩）。見階段 7-2。
    屬性的「全部」chip（`tags=全部`）不算具體屬性，這裡濾掉——它只影響「要不要觸發
    篩選搜尋」，由呼叫端另外用 `_browse_all_attrs()` 判斷。

    屬性是主篩選：使用者 2026-09-06 回饋——屬性完全沒選（沒按具體屬性、也沒按「全部」）
    時，類型／對象正常來說不該起作用（之前的 bug：沒選屬性也會被當成「隱性選了屬性的
    全部」，單靠類型／對象就送出篩選）。所以屬性沒有任何選擇時，類型／對象一律清空。"""
    tags = [t for t in request.args.getlist("tags") if t in ANIME_LIST_TAGS][:ANIME_LIST_MAX_TAGS]
    if not tags and not _browse_all_attrs():
        return tags, "", ""
    category = request.args.get("category") or ""
    target = request.args.get("target") or ""
    return (
        tags,
        category if category in ANIME_LIST_CATEGORIES else "",
        target if target in ANIME_LIST_TARGETS else "",
    )


def _filter_cache_key(tags, category: str, target: str, page: int) -> str:
    """搜尋番劇「依屬性組合」的快取 key——標籤排序過，`戀愛,校園` 跟 `校園,戀愛` 算同一組
    （使用者 2026-09-06：按下任意屬性組合就記起來、下次秒開）。"""
    return f"search:filter:t={','.join(sorted(tags))};c={category};g={target}:{page}"


def _browse_all_attrs() -> bool:
    """屬性面板選了「全部」chip（比照動畫瘋 animeList 的屬性列，「全部」也是一顆
    可點的 chip＝「不限屬性、瀏覽全部動畫」）——沒帶任何具體屬性也要觸發篩選搜尋。
    頁面初次載入（完全沒有 query args）時 `tags` 是空的，不會誤觸。"""
    return _ANIME_LIST_ALL in request.args.getlist("tags")


def _attr_tag_groups():
    """屬性 chip 依「中文二字→三字→四字→英文」分成四組（`ANIME_LIST_TAGS` 已依此排序）。
    篩選面板每組各自一行起頭：二字組每行 6 個、三字／四字／英文各排一行。見階段 7-2
    使用者回饋。回傳 `[(key, [tag, ...]), ...]` 依顯示順序。"""
    groups: dict[str, list[str]] = {"len2": [], "len3": [], "len4": [], "en": []}
    for tag in ANIME_LIST_TAGS:
        if tag.isascii():
            groups["en"].append(tag)
        elif len(tag) == 3:
            groups["len3"].append(tag)
        elif len(tag) >= 4:
            groups["len4"].append(tag)
        else:
            groups["len2"].append(tag)
    return [(key, groups[key]) for key in ("len2", "len3", "len4", "en")]


@browse_bp.route("/browse/search")
def search():
    """搜尋番劇。三條路：
    - 貼上動畫瘋網址（含 `sn=`）→ 直接轉到詳細頁（`animeVideo.php`）／ref 解析（`animeRef.php`），見 7-1。
    - 純數字關鍵字＝當成 video_sn 直接前往詳細頁。
    - 有關鍵字 → `search.php?keyword=`（一次回全部，前端逐批顯示，見階段 3）。
    - 只有右側篩選面板的屬性／類型／對象 → `animeList.php`（每頁 28，「點擊載入更多」抓下一頁）。
    卡片比照近期熱播／最新上架：`_section_items` 解析 ref_sn（有解到就直接連詳細頁、
    帶訂閱鈴鐺；沒解到就連 `/anime/ref/<ref_sn>`、當次不顯示鈴鐺）。"""
    deps = current_app.config["DEPS"]
    query = (request.args.get("q") or "").strip()
    tags, category, target = _parse_filter_args()
    browse_all_attrs = _browse_all_attrs()
    # 屬性是主篩選：類型／對象已經在 _parse_filter_args 裡被屬性未選給清空了，這裡只看屬性。
    filters_active = bool(tags or browse_all_attrs)

    # 篩選面板要不要一開始就展開：正在看篩選結果，或帶了 `_panel=1`（篩選變動時
    # search_filter.js 會保留這個旗標，讓面板跨「自動送出」的整頁重載維持開著；
    # 側欄「搜尋番劇」連結與「清除全部篩選」都不帶，所以那兩條路會收起）。見 round5 項目 7。
    panel_open = filters_active or request.args.get("_panel") == "1"

    tmpl = dict(
        query=query, results=None, browse_error=None, truncated=False, page_size=_SEARCH_PAGE_SIZE,
        all_tags=ANIME_LIST_TAGS, attr_tag_groups=_attr_tag_groups(),
        all_categories=ANIME_LIST_CATEGORIES, all_targets=ANIME_LIST_TARGETS,
        max_tags=ANIME_LIST_MAX_TAGS, sel_tags=tags, sel_all_tags=browse_all_attrs,
        sel_category=category, sel_target=target,
        filter_mode=False, filter_has_next=False, filter_query="", panel_open=panel_open,
    )

    # 7-1：貼上動畫瘋網址 → 直接轉址（用既有 _extract_sn 抽 sn）
    if query and "gamer.com.tw" in query:
        sn = _extract_sn(query)
        if sn is not None:
            if "animeRef.php" in query:
                return redirect(url_for("browse.anime_by_ref", ref_sn=sn))
            return redirect(url_for("browse.anime_detail", video_sn=sn))

    if query.isdigit():
        return redirect(url_for("browse.anime_detail", video_sn=int(query)))

    if not query and not filters_active:
        return render_template("search.html", **{**tmpl, "query": ""})

    if deps.browse_http is None:
        return render_template("search.html", **{**tmpl, "results": [], "browse_error": "browse_http_unavailable"})

    from bahaad.web.anime_data import cached_card_list

    if query:
        # 關鍵字搜尋（篩選面板此時忽略——有關鍵字走 keyword）。結果快取
        # （`search:kw:<關鍵字>`，3 小時 TTL）——使用者 2026-09-06：同一組搜尋秒開。
        def _scrape_kw(http):
            hits = search_anime(http, query)
            trunc = len(hits) > _SEARCH_RESULT_LIMIT
            return hits[:_SEARCH_RESULT_LIMIT], {"truncated": trunc}

        found, extra, err = cached_card_list(
            deps, cache_key=f"search:kw:{query}", scrape=_scrape_kw
        )
        if err:
            logger.warning("搜尋失敗（keyword=%s）：%s", query, err)
            return render_template("search.html", **{**tmpl, "results": [], "browse_error": err})
        # 比照近期熱播／最新上架：`_section_items` 解析 ref_sn（一次上限 12，見
        # `resolve_refs_cached`），解到的卡片直接連詳細頁＋帶訂閱鈴鐺，其餘退化成連
        # `/anime/ref/<ref_sn>`。`_section_items` 內部也會 `_register_images`。
        items = _section_items(deps, found)
        # search.php 一次回全部，前 _SEARCH_PAGE_SIZE 筆先顯示、其餘先 render 好但藏起來，
        # 「點擊載入更多」逐批顯示（reveal 模式，不再打網路）——見 round3 階段 3。
        return render_template(
            "search.html", **{**tmpl, "results": items, "truncated": bool(extra.get("truncated"))}
        )

    # 只有篩選：animeList.php（每頁 28，第 1 頁 server-render，之後 /browse/search/filter 抓）。
    # 每組屬性組合的每一頁都快取（`_filter_cache_key`）。
    def _scrape_filter(http):
        p = list_anime(http, page=1, tags=tags, category=category or None, target=target or None)
        return p.results, {"has_next": p.has_next}

    page1_cards, extra, err = cached_card_list(
        deps, cache_key=_filter_cache_key(tags, category, target, 1), scrape=_scrape_filter
    )
    if err:
        logger.warning("篩選搜尋失敗（tags=%s cat=%s tgt=%s）：%s", tags, category, target, err)
        return render_template("search.html", **{**tmpl, "results": [], "browse_error": err})
    filter_query = urlencode(
        [("tags", t) for t in tags] + [("category", category), ("target", target)]
    )
    return render_template("search.html", **{
        **tmpl,
        "results": _section_items(deps, page1_cards),
        "filter_mode": True,
        "filter_has_next": bool(extra.get("has_next")),
        "filter_query": filter_query,
    })


@browse_bp.route("/browse/search/filter")
def search_filter_api():
    """篩選搜尋的「點擊載入更多」——回下一頁 animeList 結果（每頁 28）。load_more.js
    的 fetch 模式：`?<篩選 query>&offset=<頁碼>` → `{html, has_next, next_offset}`。"""
    from bahaad.web.anime_data import cached_card_list

    deps = current_app.config["DEPS"]
    if deps.browse_http is None:
        return jsonify({"html": "", "has_next": False, "next_offset": 1})
    tags, category, target = _parse_filter_args()
    page = max(request.args.get("offset", default=2, type=int) or 2, 2)

    def _scrape(http):
        p = list_anime(http, page=page, tags=tags, category=category or None, target=target or None)
        return p.results, {"has_next": p.has_next}

    cards, extra, err = cached_card_list(
        deps, cache_key=_filter_cache_key(tags, category, target, page), scrape=_scrape
    )
    if err:
        logger.warning("篩選載入更多失敗（page=%s）：%s", page, err)
        return jsonify({"html": "", "has_next": False, "next_offset": page})
    # 使用者 2026-09-05：搜尋番劇（含篩選）同理拿掉鈴鐺（見 _trending_page 的說明）。
    html = render_template(
        "_card_grid_fragment.html", items=_section_items(deps, cards), show_bell=False
    )
    return jsonify({"html": html, "has_next": bool(extra.get("has_next")), "next_offset": page + 1})


@browse_bp.route("/anime/ref/<int:ref_sn>")
def anime_by_ref(ref_sn: int):
    """`animeRef.php` 番劇代碼 → 真正 video_sn，即時解析後導去詳細頁。把解析成本推到
    使用者真的點下去的時候，不是每頁 render 都把整批卡片解析一輪。"""
    from bahaad.web.anime_data import resolve_ref_cached
    from bahaad.web.settings import _DEFAULT_ANIME_CACHE_TTL_DAYS

    deps = current_app.config["DEPS"]
    if deps.browse_http is None and getattr(deps, "anime_cache", None) is None:
        abort(503)
    ttl_days = int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS))
    video_sn = resolve_ref_cached(deps, ref_sn, ttl_days=ttl_days)
    if video_sn is None:
        abort(404)
    return redirect(url_for("browse.anime_detail", video_sn=video_sn))


@browse_bp.route("/browse/subscribe/<int:video_sn>", methods=["POST"])
def subscribe(video_sn: int):
    """訂閱鈴鐺。Phase E，見 docs/requirements/web_redesign.md「訂閱功能」。功能上
    等同 scheduler/custom_schedule.py 的個別項目自訂排程（星期＋時分），只是 UI 呈現
    成鈴鐺切換而不是文字排程編輯——訂閱＝把週期表查到的星期＋時間寫成一筆帶
    `schedule_weekday` 的 `schedule_entries` 項目，`CustomScheduleRunner` 本來就只
    處理這種項目，不需要另外新增追蹤機制。「這個 sn 算不算已訂閱」全站統一用
    `schedule_weekday is not None` 判斷（見 `web/__init__.py` 的 `subscribed_sns`
    context processor），所以這裡**刻意不**在查不到週期表時段時退化成不帶
    `schedule_weekday` 的項目——那樣會建立一筆「後端說訂閱成功、但 subscribed_sns
    判斷卻不算」的不一致項目，重新整理後鈴鐺會自己跳回未訂閱、卻留下一筆使用者
    刪不掉的殭屍排程項目。已完結的舊番（不在本季新番／週期表範圍內）查不到時段時
    直接拒絕，不寫入任何東西，回傳 `reason` 讓前端顯示原因——這是規格文件留給
    實作階段決定的邊界案例，選了「不建立看不見/管不到的追蹤項目」這個更安全的做法。
    跟 `web/schedule.py` 共用同一套 parse_text/`_upsert_entry`/render_text/
    replace_from_text 寫入路徑（含分類 `_tag_before` 定位邏輯），使用者已經在排程
    清單頁手動設定過分類/模式/改名的既有項目不會被鈴鐺點擊清掉。"""
    deps = current_app.config["DEPS"]
    return jsonify(_subscribe_video(deps, video_sn))


@browse_bp.route("/browse/subscribe/ref/<int:ref_sn>", methods=["POST"])
def subscribe_by_ref(ref_sn: int):
    """卡片的 `animeRef.php` 代碼還沒被解析成 `video_sn`（一次 render 只解析前
    `_MAX_REF_NETWORK_RESOLVES_PER_BATCH` 個，見 `anime_data.resolve_refs_cached`）時，
    鈴鐺帶的是 `ref_sn`——按下去才即時解析（一次一筆、只有真的要訂閱時才打網路，不會
    整批風控），解到就照常訂閱，回傳解到的 `video_sn` 讓前端把按鈕換成正式的
    `data-video-sn`。冷快取時整頁只有前 12 張卡片有鈴鐺的問題（使用者 2026-09-05）。"""
    from bahaad.web.anime_data import resolve_ref_cached
    from bahaad.web.settings import _DEFAULT_ANIME_CACHE_TTL_DAYS

    deps = current_app.config["DEPS"]
    if deps.browse_http is None and getattr(deps, "anime_cache", None) is None:
        return jsonify({"subscribed": False, "reason": "browse_unavailable"})
    ttl_days = int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS))
    video_sn = resolve_ref_cached(deps, ref_sn, ttl_days=ttl_days)
    if video_sn is None:
        return jsonify({"subscribed": False, "reason": "ref_resolve_failed"})
    result = _subscribe_video(deps, video_sn)
    result["video_sn"] = video_sn  # 前端據此把 data-ref-sn 的鈴鐺換成 data-video-sn
    return jsonify(result)


def _subscribe_video(deps, video_sn: int) -> dict:
    """訂閱一個已知 `video_sn` 的番劇——查週期表時段、寫一筆帶 `schedule_weekday` 的排程
    項目、把當下已上架的集數標記為已下載（只追之後的新集數）。回傳 `subscribe.js` 看的
    dict（`{"subscribed": bool, "reason"?: str}`）。"""
    reason = None
    found = None
    has_schedule_source = deps.browse_http is not None or getattr(deps, "anime_cache", None) is not None
    if not has_schedule_source:
        reason = "browse_unavailable"
    else:
        try:
            found = _weekly_schedule_time(deps, video_sn)
        except BrowseError as exc:
            logger.warning("訂閱查詢週期表失敗（sn=%s）：%s", video_sn, exc)
            reason = "schedule_lookup_failed"
        else:
            if found is None:
                reason = "no_schedule_time"

    # 這部番劇（可能是別集的 sn）已經訂閱了 → 不要再建第二筆殭屍訂閱，直接回報成功
    # （鈴鐺狀態顯示錯了才會走到這，回 True 讓前端自我修正，使用者 2026-09-05）。
    if _anime_already_subscribed(deps, video_sn):
        return {"subscribed": True}

    if found is None:
        return {"subscribed": False, "reason": reason}
    weekday, hour, minute = found

    # 週期表時段用「按訂閱鈕的那一集」的 sn 查（多半是最新一集），但排程項目寫在該番劇
    # 的首集 sn 底下，這樣不管從哪一集訂閱，項目 sn 都一致（使用者 2026-09-09）。
    entry_sn = _canonical_subscription_sn(deps, video_sn)
    subscribe_sn(deps.schedule_store, entry_sn, weekday, hour, minute)
    _mark_backlog_downloaded(deps, video_sn)
    _stats_subscription(deps, video_sn, subscribed=True)
    return {"subscribed": True}


def _stats_subscription(deps, video_sn: int, *, subscribed: bool) -> None:
    """即時匿名使用統計——訂閱／退訂用該番劇首集 sn 當 key（使用者 2026-09-08）。"""
    collector = getattr(deps, "stats_collector", None)
    if collector is None:
        return
    try:
        first_sn, title = resolve_first_ep_sn(getattr(deps, "anime_cache", None), video_sn)
        collector.record_subscription(first_sn, title, subscribed=subscribed)
    except Exception:  # noqa: BLE001 - 埋點不影響訂閱
        logger.debug("stats: 記訂閱事件失敗", exc_info=True)


def _anime_already_subscribed(deps, video_sn: int) -> bool:
    """這個 video_sn 屬於的番劇，是不是已經有一筆帶 schedule_weekday 的排程項目了
    （可能 key 在同番劇的別集 sn 上）。純讀本地快取，抓不到就回 False（讓正常訂閱流程走）。"""
    return _subscribed_sibling_sn(deps, video_sn) is not None


def _subscribed_sibling_sn(deps, video_sn: int) -> int | None:
    """`video_sn` 屬於的番劇目前的訂閱項目**存在哪個 sn 底下**（可能是同一部番劇別集的
    sn——訂閱是用「訂閱當下那一集」的 sn 建的）。沒訂閱回 None。用 episode_group 反查，
    再退回「同標題」比對。rename／unsubscribe 拿這個對到真正該改的排程項目。"""
    entries = deps.schedule_store.get_entries()
    subscribed = {sn for sn, e in entries.items() if e.schedule_weekday is not None}
    if not subscribed:
        return None
    if video_sn in subscribed:
        return video_sn
    cache = getattr(deps, "anime_cache", None)
    if cache is None:
        return None
    try:
        gk = cache.group_key_for(video_sn)
        if gk is not None:
            hit = cache.group_members(gk) & subscribed
            if hit:
                return next(iter(hit))
        title = cache.anime_title_for(video_sn)
        if title:
            title = title.strip()
            for sn in subscribed:
                if cache.anime_title_for(sn) == title:
                    return sn
    except Exception:  # noqa: BLE001
        return None
    return None


def _canonical_subscription_sn(deps, video_sn: int) -> int:
    """新訂閱要寫在哪個 sn 底下——優先用該番劇的首集 sn（`episode_group` 的 group_key），
    這樣不管使用者從哪一集按訂閱，排程項目的 sn 都一致（使用者 2026-09-09）。快取查不到
    就用原本的 `video_sn`。"""
    cache = getattr(deps, "anime_cache", None)
    if cache is None:
        return video_sn
    try:
        gk = cache.group_key_for(video_sn)
    except Exception:  # noqa: BLE001
        return video_sn
    return gk or video_sn


def _mark_backlog_downloaded(deps, video_sn: int) -> int:
    """訂閱＝只自動追**之後**的新集數（使用者 2026-09-05）。把訂閱當下已上架的所有集數
    標記為「已下載」（`skipped_episodes`）——排程檢查看到這些就跳過（`check_and_download`
    的 `is_skipped` 判斷），使用者要舊集數自己去番劇頁下載。抓不到集數清單就算了（盡力
    而為，不讓訂閱請求失敗）。回傳這次新標記的集數。"""
    store = getattr(deps, "skipped_episode_store", None)
    if store is None:
        return 0
    from bahaad.web.anime_data import anime_detail_data
    from bahaad.web.settings import _DEFAULT_ANIME_CACHE_TTL_DAYS

    try:
        ttl_days = int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS))
        detail, _err = anime_detail_data(deps, video_sn, ttl_days=ttl_days)
    except Exception:  # noqa: BLE001
        logger.debug("訂閱時標記既有集數為已下載失敗（sn=%s）", video_sn, exc_info=True)
        return 0
    if detail is None:
        return 0
    marked = 0
    for category in detail.episode_categories:
        for ep in category.episodes:
            if not store.is_skipped(ep.video_sn):
                store.mark(video_sn, ep.video_sn, detail.title)
                marked += 1
    if marked:
        label = f"《{detail.title}》" if detail.title else f"sn={video_sn}"
        logger.info("訂閱 %s：把當下已上架的 %d 集標記為已下載（只追之後的新集數）", label, marked)
    return marked


@browse_bp.route("/browse/unsubscribe/<int:video_sn>", methods=["POST"])
def unsubscribe(video_sn: int):
    """取消訂閱＝移除對應的排程項目（見 web_redesign.md「訂閱功能」）。**但**如果這個
    項目除了訂閱以外，使用者還在排程清單頁另外設定過分類／模式／改名，只清掉排程
    時段、讓它退化成一般全域輪詢項目，不整條刪掉——不然點一下鈴鐺會意外把使用者
    手動設定的其他東西也清空。純粹由鈴鐺建立、沒有其他設定的項目才整條移除。

    整條移除的那種（＝真的不再追蹤這部）連帶清掉它的孤兒資料庫紀錄（監視公告／
    手動任務／已標記集數），比照使用者「退訂＝不再觀看＋移除排程更新及資料庫資訊」
    的心智模型（web_redesign_round2.md 階段 5）。退化成保留設定的那種不清——那部
    番劇還在被全域排程追蹤，不算孤兒。不動已下載的檔案。"""
    deps = current_app.config["DEPS"]

    # 從同一部番劇的其他集數頁面退訂：對到排程項目實際存在的那個 sn（使用者 2026-09-09）。
    entry_sn = _subscribed_sibling_sn(deps, video_sn) or video_sn

    cleaned = None
    if unsubscribe_sn(deps.schedule_store, entry_sn)["removed"]:
        cleaned = _delete_orphan_records(
            entry_sn,
            gossip_store=deps.gossip_store,
            manual_task_store=deps.manual_task_store,
            skipped_episode_store=deps.skipped_episode_store,
        )
    _stats_subscription(deps, entry_sn, subscribed=False)
    return jsonify({"subscribed": False, "cleaned_records": sum(cleaned.values()) if cleaned else 0})


_MAX_RENAME_LEN = 120


@browse_bp.route("/browse/rename/<int:video_sn>", methods=["POST"])
def rename_anime(video_sn: int):
    """改一部訂閱中番劇未來下載時的資料夾名稱（`schedule_entries` 的 `rename`）。見
    使用者 2026-08-26 需求。只對訂閱項目（`schedule_weekday` 不是 None）有效。名稱
    這裡再檢查一次不能空白／不能有 Windows 檔名不允許的符號——前端 rename.js 也檢查
    過，但不信任前端，避免存進去之後下載時 `_sanitize_filename_part` 默默改字或炸掉。"""
    deps = current_app.config["DEPS"]
    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip()
    if not name:
        return jsonify({"ok": False, "error": "名稱不能是空白"}), 400
    if _ILLEGAL_RENAME_CHARS_RE.search(name) or any(ord(ch) < 0x20 for ch in name):
        return jsonify({"ok": False, "error": '名稱不能包含 < > : " / \\ | ? * 等系統不允許的符號'}), 400
    if len(name) > _MAX_RENAME_LEN:
        return jsonify({"ok": False, "error": f"名稱太長（最多 {_MAX_RENAME_LEN} 個字）"}), 400

    # 使用者可能在「同一部番劇的其他集數」頁面上按更名——排程項目其實建在別集的 sn
    # 底下，對到那個 sn 才改得到（使用者 2026-09-09）。
    entry_sn = _subscribed_sibling_sn(deps, video_sn) or video_sn
    lines = parse_text(deps.schedule_store.get_raw_text())
    idx = next(
        (i for i, line in enumerate(lines) if line.line_type == "entry" and line.sn == entry_sn), None
    )
    if idx is None or lines[idx].schedule_weekday is None:
        return jsonify({"ok": False, "error": "這部番劇不在訂閱清單裡"}), 404

    line = lines[idx]
    tag = _tag_before(lines, idx)
    lines = _upsert_entry(
        lines, entry_sn, tag, line.mode, name,
        line.schedule_weekday, line.schedule_hour, line.schedule_minute,
    )
    deps.schedule_store.replace_from_text(render_text(lines))
    return jsonify({"ok": True, "name": name})


def _public_card_ctx(deps) -> dict:
    """訂閱／下載列表卡片要不要顯示「公開」勾選框——只有登入者（不是公開唯讀訪客）、
    且公開模式開著時才顯示。勾選框預設是勾的（公開模式下預設全部公開），只有列在
    `public_hidden` 的番劇是沒勾的。回傳 `public_pick`（bool）＋ `public_hidden`（set）。"""
    if getattr(g, "public_readonly", False):
        return {"public_pick": False, "public_hidden": set()}
    on = bool(deps.settings.get("public_mode", False))
    return {
        "public_pick": on,
        "public_hidden": _public_hidden_titles(deps.settings) if on else set(),
    }


@browse_bp.route("/browse/public-anime", methods=["POST"])
def set_public_anime():
    """訂閱／下載列表卡片上的「公開」勾選框——設定一部番劇（原始站方標題）在公開模式下
    給不給訪客看。預設全部公開；取消勾選就把那部加進 `public_hidden`。只有登入者能打
    （這條路由不在 `_PUBLIC_MODE_ENDPOINTS` 裡）。"""
    deps = current_app.config["DEPS"]
    title = (request.form.get("title") or "").strip()
    if not title:
        return jsonify({"ok": False, "error": "缺少番劇名稱"}), 400
    public = request.form.get("public") == "on"
    hidden = _set_title_public(deps.settings, title, public)
    return jsonify({"ok": True, "title": title, "public": public, "hidden_count": len(hidden)})


@browse_bp.route("/browse/subscriptions")
def subscriptions():
    """訂閱列表頁。Phase E，見 web_redesign.md「訂閱功能」。列出排程清單裡
    `schedule_weekday` 不是 None 的項目（訂閱本身就是這種項目，不是另外開一張表存），
    Phase B 卡片版型變體：不顯示下載按鈕／評分，改顯示「共 N 集」。"""
    from bahaad.web.anime_data import anime_detail_data
    from bahaad.web.settings import _DEFAULT_ANIME_CACHE_TTL_DAYS

    deps = current_app.config["DEPS"]
    cache_only = bool(getattr(g, "public_readonly", False))
    ttl_days = int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS))
    subscribed = []
    entries = [entry for entry in deps.schedule_store.get_entries().values() if entry.schedule_weekday is not None]
    for entry in sorted(entries, key=schedule_sort_key):
        detail, _err = anime_detail_data(deps, entry.sn, ttl_days=ttl_days, cache_only=cache_only)
        if detail is None:
            # 公開模式沒快取：還是列出這部（連過去會顯示「請登入後建立快取」）
            if cache_only:
                subscribed.append({
                    "video_sn": entry.sn,
                    "title": entry.rename or f"sn {entry.sn}",
                    "cover_url": "",
                    "episode_count": None,
                    "no_cache": True,
                })
            continue
        if cache_only and not _public_title_allowed(deps.settings, detail.title):
            continue  # 「只公開勾選的番劇」：這部沒勾
        episode_count = sum(len(category.episodes) for category in detail.episode_categories)
        subscribed.append(
            {
                "video_sn": entry.sn,
                "title": entry.rename or detail.title,
                "raw_title": detail.title,
                "cover_url": detail.cover_url,
                "episode_count": episode_count,
            }
        )
    return render_template(
        "subscriptions.html", subscribed=subscribed, **_public_card_ctx(deps)
    )


@browse_bp.route("/browse/downloads")
def downloads():
    """下載列表頁。Phase F，見 docs/requirements/web_redesign.md「下載列表頁」。

    「進行中任務」區塊完全交給前端 JS（`downloads_queue.js`）用
    `/browse/downloads/api/status` 輪詢渲染；「已下載的番劇」server-render，有下載
    完成時 JS 抓 `/browse/downloads/downloaded` 片段換掉（round 6 第 11 項）。"""
    deps = current_app.config["DEPS"]
    return render_template(
        "downloads.html",
        downloaded_anime=_downloaded_anime_list(),
        **_public_card_ctx(deps),
    )


def _downloaded_anime_list() -> list[dict]:
    from bahaad.web.anime_data import anime_detail_data, _register_images
    from bahaad.web.settings import _DEFAULT_ANIME_CACHE_TTL_DAYS

    deps = current_app.config["DEPS"]
    if deps.main_loop is None:
        return []
    ttl_days = int(deps.settings.get("anime_cache_ttl_days", _DEFAULT_ANIME_CACHE_TTL_DAYS))
    public = bool(getattr(g, "public_readonly", False))
    out = []
    for row in deps.main_loop.downloaded_anime_summaries():
        if public and not _public_title_allowed(deps.settings, row.get("anime_title") or row.get("title")):
            continue  # 「只公開勾選的番劇」：這部沒勾
        # round 6 第 12 項：封面用該番劇實際下載過的一個 video_sn 去解（DB 存的是真的
        # 下載過的 sn，不是掃檔名猜的），連詳細頁的集數選取器會依下載狀態上色。
        sample_sn = row.get("sample_video_sn")
        cover_url = row.get("cover_url") or ""
        if not cover_url and sample_sn is not None:
            detail, _err = anime_detail_data(
                deps, sample_sn, ttl_days=ttl_days, cache_only=bool(getattr(g, "public_readonly", False))
            )
            cover_url = detail.cover_url if detail is not None else ""
        out.append(
            {
                "title": row["title"],
                "raw_title": row.get("anime_title") or row["title"],
                "cover_url": cover_url,
                "episode_count": row["episode_count"],
                "video_sn": sample_sn,
            }
        )
    # 封面走 /cache/img/<hash>——沒先登記過會 404（公開模式的訪客尤其明顯，卡片全破圖）
    _register_images(deps, [a["cover_url"] for a in out if a["cover_url"]])
    return out


@browse_bp.route("/browse/downloads/downloaded")
def downloads_downloaded_fragment():
    """「已下載的番劇」區塊的 HTML 片段——`downloads_queue.js` 在有下載完成時抓一次
    換掉，不用整頁重新整理（round 6 第 11 項）。"""
    deps = current_app.config["DEPS"]
    return render_template(
        "_downloaded_anime.html",
        downloaded_anime=_downloaded_anime_list(),
        **_public_card_ctx(deps),
    )


@browse_bp.route("/browse/downloads/api/status")
def downloads_api_status():
    """「進行中任務」（`active`）＋「最近失敗」（`failing`）——後者是階段 3 從已退休的
    /dashboard 搬過來的（見 web_redesign_round2.md）。兩份都只是 `DownloadRegistry`
    既有記憶體狀態的唯讀快照，不新增持久化。失敗清單排除掉「已經重試、現在正在下載中」
    的 sn（重試後 `failure_count` 還沒歸零、但它已經回到 active，不該再顯示成失敗）。"""
    deps = current_app.config["DEPS"]
    if deps.registry is None:
        return jsonify({"active": [], "failing": []})

    active = []
    active_sns = set()
    for sn, entry in deps.registry.snapshot().items():
        active_sns.add(sn)
        info = _lookup_episode_info(deps.catalog, sn) if deps.catalog is not None else {}

        if entry.progress is None:
            state, percent = "preparing", None
        elif entry.progress.fraction >= 1.0:
            state, percent = "finalizing", 100
        else:
            state, percent = "downloading", round(entry.progress.fraction * 100)

        active.append(
            {
                "video_sn": sn,
                "state": state,
                "percent": percent,
                # 「前置準備」階段的細部狀態（遊客看廣告等待等）；前端只在 preparing 顯示
                "phase": entry.phase if state == "preparing" else None,
                "anime_title": _dl_display_name(
                    deps, sn, info.get("anime_title", ""), getattr(entry, "display_name", None)
                ),
                "episode_number": info.get("episode_number"),
                "episode_label": _dl_episode_label(deps, info.get("episode_number")),
                "cover_url": info.get("cover_url", ""),
            }
        )

    failing = []
    # 持久化失敗列存的番劇名（重啟後 catalog 可能查不到、schedule 也可能已退訂）
    persisted_failed_names = (
        deps.main_loop.failed_download_names()
        if deps.main_loop is not None and hasattr(deps.main_loop, "failed_download_names")
        else {}
    )
    for sn, fail in deps.registry.failing_snapshot().items():
        if sn in active_sns:
            continue
        info = _lookup_episode_info(deps.catalog, sn) if deps.catalog is not None else {}
        failing.append(
            {
                "video_sn": sn,
                "failure_count": fail.failure_count,
                "last_error": fail.last_error,
                "anime_title": _dl_display_name(
                    deps, sn, info.get("anime_title") or persisted_failed_names.get(sn, "")
                ),
                "episode_number": info.get("episode_number"),
                "episode_label": _dl_episode_label(deps, info.get("episode_number")),
                "cover_url": info.get("cover_url", ""),
            }
        )

    # round 7 第 11 項：整集下載完成、但下載目錄當時不在，等使用者處理
    awaiting_move = []
    if deps.main_loop is not None:
        for row in deps.main_loop.awaiting_move_list():
            sn = row["video_sn"]
            info = _lookup_episode_info(deps.catalog, sn) if deps.catalog is not None else {}
            awaiting_move.append(
                {
                    "video_sn": sn,
                    "anime_title": _dl_display_name(
                        deps, sn, info.get("anime_title") or row["anime_title"]
                    ),
                    "episode_number": info.get("episode_number"),
                    "episode_label": _dl_episode_label(deps, info.get("episode_number")),
                    "cover_url": info.get("cover_url", ""),
                }
            )
    # .0 改進.txt 第 12 項：下載完一集後的冷卻期間，worker slot 還佔著、下一部還沒開始
    # ——回報最久還要等幾秒，前端顯示「冷卻中」橫幅，不會看起來像卡住
    cooldown_seconds = 0
    try:
        cooldown_seconds = round(deps.registry.cooldown_remaining())
    except Exception:  # noqa: BLE001
        cooldown_seconds = 0

    return jsonify({
        "active": active,
        "failing": failing,
        "awaiting_move": awaiting_move,
        "cooldown_seconds": cooldown_seconds,
    })


@browse_bp.route("/browse/downloads/retry/<int:video_sn>", methods=["POST"])
def downloads_retry(video_sn: int):
    """「最近失敗」卡片封面正中央「重試」按鈕。重新提交這一集下載，跟手動補抓／集數
    選取器共用同一套 `trigger_manual_download()`。成功回到 active，下一輪輪詢就會把它
    從失敗清單移到進行中。"""
    deps = current_app.config["DEPS"]
    if deps.main_loop is None:
        abort(503)
    try:
        result = deps.main_loop.trigger_manual_download(video_sn)
    except GamerLoginStale as exc:
        # 登入態失效——掛「動畫瘋登入態失效」橫幅（跟背景偵測同一個旗標／同一條橫幅），
        # 回一個叫使用者去重新登入的訊息。
        logger.info("重試下載：動畫瘋登入態失效（sn=%s）：%s", video_sn, exc)
        try:
            if not deps.settings.get("_gamer_login_stale"):
                from datetime import datetime as _dt
                deps.settings.update(
                    {"_gamer_login_stale": {"detected_at": _dt.now().isoformat(timespec="seconds")}}
                )
        except Exception:  # noqa: BLE001
            pass
        return jsonify({
            "ok": False,
            "error": "動畫瘋登入態已失效，請到「設定 › 動畫瘋登入」重新登入後再重試。",
        }), 502
    except CatalogError as exc:
        logger.warning("重試下載失敗（sn=%s）：%s", video_sn, exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    except Exception:
        # get_video() 也可能丟 curl_cffi 的網路例外（DNS／逾時／連線）——那些不是
        # CatalogError，不攔的話會變成未處理的 500。比照 recheck confirm 的 except Exception。
        logger.exception("重試下載時發生未預期的例外（sn=%s）", video_sn)
        return jsonify({"ok": False, "error": "發生未預期的錯誤，請稍後再試"}), 502
    return jsonify({"ok": True, "result": result.name})


@browse_bp.route("/browse/downloads/abort/<int:video_sn>", methods=["POST"])
def downloads_abort(video_sn: int):
    """「中止」進行中／排隊中的下載（round 7 第 18 項）。送中斷訊號給這一個 video_sn，
    下載流程收尾、清掉它的暫存。"""
    deps = current_app.config["DEPS"]
    if deps.main_loop is None:
        abort(503)
    aborted = deps.main_loop.abort_download(video_sn)
    return jsonify({"ok": True, "aborted": bool(aborted)})


@browse_bp.route("/browse/downloads/discard/<int:video_sn>", methods=["POST"])
def downloads_discard(video_sn: int):
    """「丟棄」一個下載失敗的集數（round 7 第 18 項）——不再重試、自動排程也不再排入。"""
    deps = current_app.config["DEPS"]
    if deps.main_loop is None:
        abort(503)
    deps.main_loop.discard_failed(video_sn)
    return jsonify({"ok": True})


@browse_bp.route("/browse/downloads/retry-move/<int:video_sn>", methods=["POST"])
def downloads_retry_move(video_sn: int):
    """「再試一次」——把 awaiting_move 的集數從暫存搬進下載目錄（round 7 第 11 項）。"""
    deps = current_app.config["DEPS"]
    if deps.main_loop is None:
        abort(503)
    ok, message = deps.main_loop.retry_pending_move(video_sn)
    return jsonify({"ok": ok, "message": message})


# ---- 重新檢查排程更新（階段 4，見 docs/requirements/web_redesign_round2.md）----
# 檢查本身是背景執行緒（`scheduler/recheck.py` 的 RecheckCoordinator）——make_server 是
# 單執行緒，同步跑一整輪會卡住整個 web，所以這裡的端點只是「啟動／查狀態／確認／取消」，
# 前端用 recheck.js 輪詢 status。


@browse_bp.route("/browse/recheck", methods=["POST"])
def recheck_all():
    deps = current_app.config["DEPS"]
    if deps.recheck_coordinator is None:
        abort(503)
    started = deps.recheck_coordinator.start_check(None)
    return jsonify({"started": started})


@browse_bp.route("/browse/recheck/<int:video_sn>", methods=["POST"])
def recheck_one(video_sn: int):
    deps = current_app.config["DEPS"]
    if deps.recheck_coordinator is None:
        abort(503)
    started = deps.recheck_coordinator.start_check(video_sn)
    return jsonify({"started": started})


@browse_bp.route("/browse/recheck/status")
def recheck_status():
    deps = current_app.config["DEPS"]
    if deps.recheck_coordinator is None:
        return jsonify({"state": "idle"})
    return jsonify(deps.recheck_coordinator.status())


@browse_bp.route("/browse/recheck/confirm", methods=["POST"])
def recheck_confirm():
    """body: {"choices": {"<video_sn>": "download" | "skip"}}"""
    deps = current_app.config["DEPS"]
    if deps.recheck_coordinator is None:
        abort(503)
    payload = request.get_json(silent=True) or {}
    raw = payload.get("choices", {})
    if not isinstance(raw, dict):
        raw = {}
    choices = {
        int(video_sn): choice
        for video_sn, choice in raw.items()
        if str(video_sn).isdigit() and choice in ("download", "skip")
    }
    ok = deps.recheck_coordinator.confirm(choices)
    return jsonify({"ok": ok})


@browse_bp.route("/browse/recheck/cancel", methods=["POST"])
def recheck_cancel():
    deps = current_app.config["DEPS"]
    if deps.recheck_coordinator is not None:
        deps.recheck_coordinator.cancel()
    return jsonify({"ok": True})


@browse_bp.route("/ui/theme", methods=["POST"])
def set_theme():
    """淺色／深色主題偏好，存進 SettingsStore（跟其他使用者偏好一樣），重新整理後維持選擇。"""
    value = request.form.get("value")
    if value in _THEMES:
        current_app.config["DEPS"].settings.update({"web_theme": value})
    return ("", 204)


@browse_bp.route("/ui/sidebar", methods=["POST"])
def set_sidebar_collapsed():
    """側欄收合狀態，同樣存進 SettingsStore 讓下次開啟維持上次的選擇。"""
    value = request.form.get("value")
    current_app.config["DEPS"].settings.update({"web_sidebar_collapsed": value == "true"})
    return ("", 204)


@browse_bp.route("/ui/subscribe-intro-seen", methods=["POST"])
def set_subscribe_intro_seen():
    """使用者看過「首次訂閱說明」對話框後 fire-and-forget 存一個旗標，之後不再跳。
    見 docs/requirements/web_redesign_round2.md 階段 2-2——一次性 UI 狀態，用
    SettingsStore 的 `_`-前綴內部 key，不進正規化的 `store/` 表。"""
    current_app.config["DEPS"].settings.update({"_subscribe_intro_seen": True})
    return ("", 204)
