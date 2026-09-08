"""全域週期背景迴圈。規格見 docs/requirements/scheduler_main_loop.md。

以固定的全域週期（`check_interval_minutes`）檢查排程清單裡沒有自訂時段的項目，發現新
集數就依序跑 catalog → playlist → segment → ffmpeg → danmu，組出最終輸出檔。
`check_and_download()` 是跟 `scheduler/custom_schedule.py` 共用的核心邏輯（見該模組），
兩者只差在「什麼時候被叫醒」。

**已知未解決的風險**（實作時發現，沿用 Phase 1 `gamer_client/playlist.py` 就已經標記過的
懸而未決事項）：`checklock()`／`unlock()`（同帳號多裝置鎖定機制）目前完全沒有呼叫——
`playlist.py` 的需求規格說這兩支端點實際觸發時機是「片段已經抓了一部分之後」，跟直覺的
「取網址前鎖、下載完解鎖」對不上，且 `checklock.php` 的 error 欄位列舉值還沒摸清楚，貿然
猜測呼叫方式反而可能誤觸發鎖定或提早釋放，得不償失。這裡選擇維持不呼叫，等使用者用真實
帳號實測、真正搞懂這兩支端點的行為之後再回頭補上。

**最大並發下載數**（見 docs/requirements/scheduler_main_loop.md「最大並發下載數」一節）：
`_maybe_download()` 搶到 `registry.py` 的鎖之後，不是自己同步跑完整個下載流程，而是把實際
下載工作丟進呼叫端傳入的共用 `scheduler/download_pool.py`（`custom_schedule.py` 之後會共用
同一個實例），`check_and_download()` 因此變成「提交工作、立刻回傳」，不會被下載本身的時間
卡住整個排程迴圈。

**手動下載入口**（見 docs/requirements/web_manual_download.md）：`_maybe_download(video,
display_name)` 是不依賴 `ScheduleEntry` 的核心方法，`check_and_download()`／
`trigger_manual_download()` 是兩種不同的觸發來源，都只呼叫這個核心方法——「要不要下載＋
怎麼提交」只在一個地方定義。可選依賴 `manual_task_store` 讓手動任務在下載結束（不管成功
或失敗）時自動從 `store/manual_tasks.py` 移除，呼應該模組「提交時寫入、終結時移除」的需求。
"""

from __future__ import annotations

import logging
import re
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from bahaad.diagnostics.codes import ErrorCode, OperationType, classify_connection_error
from bahaad.diagnostics.reporter import DiagnosticsReporter
from bahaad.downloader.danmu import DanmuError, get_danmu, write_ass
from bahaad.downloader.ffmpeg import FfmpegError, FfmpegRunner
from bahaad.downloader.mediainfo import MediaInfo, probe_media
from bahaad.downloader.naming import (
    DEFAULT_PAD_WIDTH,
    DEFAULT_TEMPLATE,
    episode_zh,
    render_basename,
    sanitize_filename_part as _sanitize_filename_part,
)
from bahaad.downloader.segment import DownloadInterrupted, SegmentDownloadError, SegmentDownloader
from bahaad.gamer_client.catalog import (
    GamerLoginStale,
    ParentPasswordRequired,
    VideoInfo,
    WatchingPermissionDenied,
)
from bahaad.gamer_client.device import DeviceIdError
from bahaad.gamer_client.guest_access import GuestAccessInterrupted
from bahaad.gamer_client.playlist import (
    GamerApiError,
    GamerServerError,
    GuestHandshakeError,
    PlaylistError,
)
from bahaad.notify.dispatch import send_notification
from bahaad.registry import DownloadRegistry
from bahaad.scheduler import recheck_pause
from bahaad.scheduler.download_pool import DownloadPool
from bahaad.scheduler.seasons import (
    DEFAULT_AUTO_SEASON_FOLDER,
    DEFAULT_AUTO_SEASON_YEAR,
    season_folder,
)
from bahaad.store.gossip import GossipStore
from bahaad.store.manual_tasks import ManualTaskStore
from bahaad.store.notify import NotifyStore
from bahaad.store.anime_cache import AnimeCacheStore
from bahaad.store.pending_downloads import PendingDownloadStore
from bahaad.store.schedule_list import ScheduleEntry, ScheduleListStore
from bahaad.store.settings import SettingsStore
from bahaad.store.skipped_episodes import SkippedEpisodeStore

# 下載 worker 裡「當成可重試的下載失敗」處理的例外（不走 `except Exception` 未預期路徑，
# 也不送第二則診斷）。`DeviceIdError`＝申請裝置 ID 時網路不穩／回應異常，本質是暫時性
# 網路問題（使用者 2026-09-05 回報：一個網路異常送兩則診斷、還被判成硬失敗）。
_RETRYABLE_DOWNLOAD_ERRORS = (PlaylistError, SegmentDownloadError, FfmpegError, DeviceIdError)

# 整集重試之間的等待秒數：第 N 次重試等 base*(N+1) 秒（第 1 次 5s、第 2 次 10s...）。
# 使用者 2026-09-06 回報 code 1007 仍偶發：比對 aniGamerPlus 原始碼發現它每次都在同一秒
# 內連續打好幾次 video_src.php（中間還作廢、重新申請 device_id）、全部 1007——而
# aniGamerPlus 本身遇到 1007 是直接判這次失敗，等下一輪排程檢查（通常幾十分鐘後）才會
# 再試，完全不會在同一秒內連續狂打。這幾乎是同一種請求模式在短時間內被站方判定異常／
# 限流的訊號。不能照抄「等下一輪排程」（那樣 episode_retry_count 這個「同一次任務內
# 快速重試」的設計就沒意義了），但至少重試之間該留出間隔，別在同一秒內連環打好幾次
# video_src.php。走 SettingsStore（跟 download_cooldown_seconds 同一套慣例）而不是寫死
# 常數——測試才能設成 0（見 tests/test_scheduler_main_loop.py 的 `_make_loop` defaults），
# 不用每個會重試的測試都乾等好幾秒。
SETTINGS_KEY_RETRY_BACKOFF_SECONDS = "episode_retry_backoff_seconds"
_DEFAULT_RETRY_BACKOFF_SECONDS = 5

# 動畫瘋站方暫時性錯誤（`GamerServerError`＝503 維護中／回應不是 JSON）→ 排進延長重試
# 排程（使用者 2026-09-06）：每 3 分鐘試 10 次 → 每 5 分鐘試 10 次 → 每 10 分鐘試 10 次
# → 都失敗才真的放棄、等使用者手動重下。跟 `episode_retry_count` 那種「同一次任務內
# 秒級快速重試」不同層次——這是「站方壞了、過一陣子自己會好」的長時間等待，不能佔著
# download_pool 的 worker slot 乾等，改成登記進 pending_downloads 由 `_run_loop` 到點
# 重新提交。`(重試次數, 間隔秒數)` 一段一段接。
_SITE_ERROR_RETRY_SCHEDULE = ((10, 180), (10, 300), (10, 600))
_SITE_ERROR_RETRY_TOTAL = sum(count for count, _interval in _SITE_ERROR_RETRY_SCHEDULE)


def _site_error_retry_interval(attempt: int) -> int | None:
    """第 `attempt`（0 起算）次重試要等幾秒。超過總次數回 `None`（＝放棄）。"""
    seen = 0
    for count, interval in _SITE_ERROR_RETRY_SCHEDULE:
        if attempt < seen + count:
            return interval
        seen += count
    return None


def _parse_iso_dt(value: "str | None") -> "datetime | None":
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None

_ERROR_CODE_BY_EXCEPTION_TYPE = {
    # 遊客／非 VIP 看廣告交握失敗——年齡限制等未知 error code 靠這個收樣本
    # （docs/requirements/guest_download.md）。放在 PlaylistError 之前，type() 精確比對。
    GuestHandshakeError: ErrorCode.GUEST_HANDSHAKE_FAILED,
    GamerApiError: ErrorCode.PLAYLIST_FETCH_FAILED,
    PlaylistError: ErrorCode.PLAYLIST_FETCH_FAILED,
    DeviceIdError: ErrorCode.PLAYLIST_FETCH_FAILED,
    SegmentDownloadError: ErrorCode.SEGMENT_DOWNLOAD_FAILED,
    FfmpegError: ErrorCode.FFMPEG_MUX_FAILED,
}

# 已知的動畫瘋 API 錯誤代碼 → 建議操作（使用者 2026-09-02）。不在表裡的代碼由
# _download_failure_advice() 依「錯誤自動回報」設定給通用建議。
_GAMER_ERROR_ADVICE = {
    1007: "請重新登入動畫瘋。",
    1015: "請登入動畫瘋，或開啟「允許未登入／非 VIP 下載」。",
}


def _is_transient_fetch_error(exc: BaseException) -> bool:
    """查集數／申請裝置 ID 時的暫時性網路／回應異常（連線斷、逾時、DNS、空回應…）。
    沿 `__cause__`／`__context__` 鏈往回找 curl_cffi 的請求例外或 JSON 解析失敗。"""
    import json as _json

    from curl_cffi.requests import exceptions as _curl_exc

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (_json.JSONDecodeError, _curl_exc.RequestException)):
            return True
        current = current.__cause__ or current.__context__
    return False


def _download_failure_advice(code, *, diagnostics_on: bool) -> str:
    known = _GAMER_ERROR_ADVICE.get(code)
    if known:
        return known
    if diagnostics_on:
        return "該項目尚未紀錄，已透過錯誤自動回報回傳該項錯誤。"
    return "該項目尚未紀錄，請開啟自動回報來回傳錯誤項目。"

logger = logging.getLogger(__name__)

_DEFAULT_CHECK_INTERVAL_MINUTES = 30
_DEFAULT_DOWNLOAD_DIR = str(Path.home() / "Downloads" / "BahaAD")
# 排程檢查時，查詢每個追蹤項目的番劇資訊（catalog API）之間的等待秒數——放慢對站方
# API 的請求節奏。使用者 2026-08-27 要求預設／下限 2 秒（設定頁 frontend_min 擋在 2）。
_DEFAULT_SN_QUERY_COOLDOWN_SECONDS = 2.0
# round 7 第 10 項：「下載完一集之後」的等待秒數（放慢對站方的單一集請求頻率——風控看
# 的是這個，不是片段頻率）。使用者定案預設／下限 10 秒。冷卻期間 download_pool 的 worker
# slot 仍被佔著，所以也順帶達成「一批並發下載完後才放下一批」的效果。
_DEFAULT_DOWNLOAD_COOLDOWN_SECONDS = 10

# 判斷「這一集是否已下載過」用的標記：檔名裡含 [{video_sn}]，不是整個檔名一字不差比對，
# 使用者事後手動調整檔名其他部份不會讓判斷失效（見需求規格文件「已下載判斷」一節）
_SN_TAG_RE = re.compile(r"\[(\d+)\]")

class DownloadDirMissing(Exception):
    """整集已在暫存區下載＋合併完成，要搬進下載目錄時發現下載目錄不存在（網路磁碟／
    外接裝置沒接）。round 7 第 11 項定案：不刪暫存、不判失敗——改記成 `pending_downloads`
    的 `awaiting_move` 狀態，等使用者改下載目錄或把裝置接回來後按「再試一次」。"""

    def __init__(self, download_dir: str) -> None:
        super().__init__(f"下載目錄不存在：{download_dir}")
        self.download_dir = download_dir


class DownloadTrigger(Enum):
    """`_maybe_download()` 的結果，給 `web/manual_download.py` 分辨「提交了」／
    「已下載過」／「已經在下載中」三種情況用——單純的 bool 表達不出這三種差異，
    使用者手動補抓時看到的訊息應該要能區分「略過的原因」，不是只有「有/沒有動作」。

    「使用者標記為已下載」（`skipped_episodes`，階段 4）刻意**不**在這個列舉裡——那個
    檢查放在 `check_and_download()` 的迴圈裡、不在 `_maybe_download()`，所以只擋自動
    排程、手動補抓（`trigger_manual_download()`）照樣能下載（使用者手動要的就給）。"""

    SUBMITTED = "submitted"
    ALREADY_DOWNLOADED = "already_downloaded"
    ALREADY_ACTIVE = "already_active"
    BLOCKED_BY_ACCESS_GATE = "blocked_by_access_gate"


class CatalogClientProtocol(Protocol):
    def get_latest_episode(self, video_sn: int) -> VideoInfo: ...
    def get_all_episodes(self, video_sn: int) -> list[VideoInfo]: ...
    def list_episodes(self, video_sn: int) -> tuple[str, list]: ...


@dataclass(frozen=True)
class NewEpisode:
    """`MainLoop.list_new_episodes()` 的一筆——「重新檢查排程更新」找到的、還沒下載的
    集數。刻意不是完整的 `VideoInfo`：那個要逐集 get_video()（慢），這裡只需要
    video_sn（下載用）＋集數標籤＋番劇標題（顯示用）。"""

    video_sn: int
    episode_label: str
    anime_title: str


class PlaylistClientProtocol(Protocol):
    def get_playlist(
        self, video_sn: int, *, stop_check=None, on_phase=None, refresh_device_id=True
    ): ...


def _already_downloaded(output_dir: Path, video_sn: int) -> bool:
    if not output_dir.exists():
        return False
    for path in output_dir.iterdir():
        if not path.is_file():
            continue
        match = _SN_TAG_RE.search(path.name)
        if match and int(match.group(1)) == video_sn:
            return True
    return False


def _downloaded_video_sns(directory: Path) -> set[int]:
    """掃一次目錄，回傳所有檔名帶 `[video_sn]` 標記的 video_sn。給要「一次判斷一整批
    集數」的呼叫端用——`_already_downloaded()` 適合單集查詢（可提早 return），這支避免
    每一集各自 `iterdir()` 一次同一個目錄。"""
    if not directory.exists():
        return set()
    return {
        int(match.group(1))
        for path in directory.iterdir()
        if path.is_file() and (match := _SN_TAG_RE.search(path.name))
    }


def _parent_folder_path(folder: str) -> Path:
    """季別／分類父資料夾字串 → 可以 `download_dir / _parent_folder_path(...) / display_name`
    的 `Path`。先用 ASCII `/` 切段、每段各自全形化（`sanitize_filename_part` 會把 `/`
    翻成全形 `／`，所以一定要先切）。擋掉 `.`／`..` 的路徑穿越。空字串 → `Path()`（no-op）。"""
    path = Path()
    for segment in str(folder or "").split("/"):
        clean = _sanitize_filename_part(segment).strip()
        if clean and clean not in (".", ".."):
            path = path / clean
    return path


class MainLoop:
    def __init__(
        self,
        schedule_store: ScheduleListStore,
        settings: SettingsStore,
        registry: DownloadRegistry,
        catalog: CatalogClientProtocol,
        playlist: PlaylistClientProtocol,
        segment_downloader: SegmentDownloader,
        ffmpeg_runner: FfmpegRunner,
        danmu_session,
        download_pool: DownloadPool,
        manual_task_store: ManualTaskStore | None = None,
        diagnostics: DiagnosticsReporter | None = None,
        notify_store: NotifyStore | None = None,
        gossip_store: GossipStore | None = None,
        skipped_episode_store: SkippedEpisodeStore | None = None,
        anime_cache: AnimeCacheStore | None = None,
        staging_root: Path | None = None,
        downloaded_store: "DownloadedEpisodeStore | None" = None,
        pending_store: PendingDownloadStore | None = None,
        on_gamer_activity: "Callable[[], None] | None" = None,
        stats_collector=None,
    ) -> None:
        self._schedule_store = schedule_store
        self._settings = settings
        self._registry = registry
        self._catalog = catalog
        self._playlist = playlist
        self._segment_downloader = segment_downloader
        self._ffmpeg_runner = ffmpeg_runner
        self._danmu_session = danmu_session
        self._download_pool = download_pool
        self._manual_task_store = manual_task_store
        self._diagnostics = diagnostics
        self._notify_store = notify_store
        self._gossip_store = gossip_store
        self._skipped_episode_store = skipped_episode_store
        self._anime_cache = anime_cache
        self._staging_root = Path(staging_root) if staging_root is not None else None
        self._downloaded_store = downloaded_store
        self._pending_store = pending_store
        # 即時匿名使用統計（可選依賴，使用者 2026-09-08）——下載成功時 +1 總下載次數
        self._stats_collector = stats_collector
        # 每次跟動畫瘋互動（排程檢查／手動下載／重新檢查）順便叫 gossip_watcher 查一次公告
        # ——使用者 2026-09-04：只靠 gossip 自己的週期輪詢不夠即時。gossip 端有節流。
        self._on_gamer_activity = on_gamer_activity or (lambda: None)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _poke_gamer_activity(self) -> None:
        try:
            self._on_gamer_activity()
        except Exception:  # noqa: BLE001 - 這只是「順便」，絕不能影響下載
            logger.debug("on_gamer_activity 回呼發生例外", exc_info=True)

    def check_and_download(self, entry: ScheduleEntry, *, force_all_episodes: bool = False) -> int:
        """檢查這個追蹤項目、把該下載的集數提交進 download_pool，回傳「這次新提交下載」
        的集數（不含本來就下載過的，也不含已經在下載中的）——`scheduler/custom_schedule.py`
        處理監視公告的覆蓋排程重試視窗時，用這個數字判斷「等的那一集上架了沒」。

        `force_all_episodes`：不管追蹤項目自己設定的 `mode`，一律抓整季所有集數——監視
        公告「同時更新」（一次要等 ≥2 集）的覆蓋排程需要，`mode=latest` 一次只看得到
        最新一集會漏掉。

        監視公告的「本週暫停／下架」覆蓋（`gossip_store.is_skipped()`）在這裡擋下——
        `check_and_download()` 是 `main_loop.run_once()` 跟 `custom_schedule.py` 共用的
        自動下載入口，擋在這裡兩邊都涵蓋；手動補抓（`trigger_manual_download()`）走的是
        另一條路，不受這個覆蓋影響（使用者手動要的就給）。"""
        self._poke_gamer_activity()  # 跟動畫瘋互動了 → 順便查一次公告

        if self._gossip_store is not None and self._gossip_store.is_skipped(
            entry.sn, datetime.now().date().isoformat()
        ):
            logger.info("sn=%s 本週有監視公告的暫停/下架覆蓋，這次跳過檢查", entry.sn)
            return 0

        try:
            videos = self._fetch_videos(entry, force_all_episodes=force_all_episodes)
        except (WatchingPermissionDenied, ParentPasswordRequired) as exc:
            logger.warning("%s 這次檢查跳過：%s", self._entry_label(entry), exc)
            return 0
        except Exception as exc:  # noqa: BLE001
            # 網路不穩／回應異常（空 body、逾時、斷線）→ 這次當「還沒有新集數」處理：
            # 回 0 讓呼叫端開複查時窗、之後自動重試，不當硬失敗、不送未預期例外診斷
            # （使用者 2026-09-05）。真的是程式 bug 的例外照樣往外丟。
            if _is_transient_fetch_error(exc):
                logger.warning(
                    "%s 查詢新集數時網路／回應異常，這次跳過（會自動複查）：%s",
                    self._entry_label(entry), exc,
                )
                return 0
            raise

        cooldown = self._settings.get("sn_query_cooldown_seconds", _DEFAULT_SN_QUERY_COOLDOWN_SECONDS)
        if cooldown > 0:
            time.sleep(cooldown)

        if not force_all_episodes:
            # force_all_episodes＝公告「同時更新」補抓 / 新番快訊集數捕齊——那正是要
            # 回頭抓好幾集，不能在這裡把它們標成「已下載」
            self._backfill_backlog_if_untouched(entry, videos)

        submitted = 0
        for video in videos:
            # 使用者在「重新檢查排程更新」把這一集標記為已下載（階段 4）——自動排程不
            # 再排入。擋在這裡（不在 _maybe_download），所以 trigger_manual_download()
            # 的手動補抓不受影響。
            if (
                self._skipped_episode_store is not None
                and self._skipped_episode_store.is_skipped(video.video_sn)
            ):
                continue
            display_name = _sanitize_filename_part(entry.rename or video.anime_title)
            parent_folder = self._season_folder_for(video, entry)
            if self._maybe_download(video, display_name, parent_folder) is DownloadTrigger.SUBMITTED:
                submitted += 1

        # 觸發 2（見 docs/requirements/anime_cache.md）：排程時段偵測到新集數 → 這部的
        # detail 快取作廢（下次造訪重爬）＋就地把首頁那張卡片的集數文字更新。背景不做
        # HTML 爬取，只動快取。
        if submitted and self._anime_cache is not None and videos:
            try:
                latest = max(videos, key=lambda v: v.episode_index).episode_number
                self._anime_cache.invalidate_detail(entry.sn)
                self._anime_cache.patch_home_card(entry.sn, episode_text=f"第{latest}集")
            except Exception:
                logger.debug("%s 更新番劇快取失敗（不影響下載）", self._entry_label(entry), exc_info=True)

        # 新集數上架 → 更新 episode_group（訂閱鈴鐺靠它把「週期表最新一集的 sn」對回
        # 訂閱項目的 sn；detail 已作廢但 group 不會自動跟上，這裡順手補，使用者 2026-09-05
        # 回報週期表把更新中的收藏番劇顯示成未訂閱）。
        if self._anime_cache is not None:
            for video in videos:
                members = {ep.video_sn for ep in getattr(video, "episodes", ()) or ()} | {video.video_sn}
                if members and video.anime_title:
                    try:
                        self._anime_cache.record_episode_group(members, video.anime_title)
                    except Exception:  # noqa: BLE001
                        logger.debug("更新 episode_group 失敗（不影響下載）", exc_info=True)

        return submitted

    def list_new_episodes(self, entry: ScheduleEntry) -> list[NewEpisode]:
        """「只檢查、不下載」——給「重新檢查排程更新」用（見 web_redesign_round2.md
        階段 4）。回傳這個追蹤項目目前有哪幾集是「還沒下載完成、沒在下載中、也沒被
        使用者標記為已下載」的。

        完全不碰 `download_pool`／`registry`，純查詢；而且用 `catalog.list_episodes()`
        （只打一次 API）粗篩，不逐集 get_video()——「重新檢查」大部分時候要掃十幾二十
        部收藏、每部十幾集，逐集查會慢到不能用。不管追蹤項目自己的 `mode`，一律看整季
        所有集數（「重新檢查」＝有沒有漏掉的，只看最新一集會漏）。"""
        self._poke_gamer_activity()  # 跟動畫瘋互動了 → 順便查一次公告
        try:
            anime_title, summaries = self._catalog.list_episodes(entry.sn)
        except (WatchingPermissionDenied, ParentPasswordRequired) as exc:
            logger.warning("sn=%s 重新檢查跳過（無觀看權限）：%s", entry.sn, exc)
            return []

        display_name = _sanitize_filename_part(entry.rename or anime_title)
        # 這裡只有 EpisodeSummary（沒有 season_start），拿不到自動季別——只吃手動「分類」
        # 覆寫。反正這個 output_dir 只餵無 DB 的 `_is_downloaded` fallback（正式有 DB 時
        # 是用 video_sn 查、跟路徑無關）。
        output_dir = self._download_dir() / _parent_folder_path(entry.tag or "") / display_name
        result: list[NewEpisode] = []
        for ep in summaries:
            if self._is_downloaded(ep.video_sn, output_dir):
                continue
            if self._registry.is_active(ep.video_sn):
                continue
            if (
                self._skipped_episode_store is not None
                and self._skipped_episode_store.is_skipped(ep.video_sn)
            ):
                continue
            result.append(
                NewEpisode(video_sn=ep.video_sn, episode_label=str(ep.episode), anime_title=anime_title)
            )
        return result

    def trigger_manual_download(self, video_sn: int, rename: str | None = None) -> DownloadTrigger:
        """直接抓指定的 video_sn，不經過 mode="all"/"latest" 的判斷——給
        web/manual_download.py 的手動補抓、以及程式啟動時接續未完成的手動任務用。

        季別資料夾：追蹤中的番劇（有排程項目）→ 依項目的「分類」或自動季別；沒追蹤的
        （手動下載陌生番劇）→ 沒有排程項目、`_season_folder_for` 收 `None` → 純自動，
        算不出就 `""`（維持 `下載目錄/番劇名/`，使用者定案）。"""
        self._poke_gamer_activity()  # 跟動畫瘋互動了 → 順便查一次公告
        video = self._catalog.get_video(video_sn)
        entry = self._entry_for_video(video)
        # 手動下載某一集時，如果這部番劇已經在訂閱清單裡設過「更名」，沿用它——不然
        # 資料夾／檔案會用原始番劇標題（使用者 2026-09-04：手動下載沒查更名）。呼叫端
        # （手動下載表單）自己有填 rename 就以表單為準。
        effective_rename = rename or (entry.rename if entry is not None else None)
        display_name = _sanitize_filename_part(effective_rename or video.anime_title)
        parent_folder = self._season_folder_for(video, entry)
        return self._maybe_download(video, display_name, parent_folder)

    def _entry_for_video(self, video: VideoInfo) -> "ScheduleEntry | None":
        """把一集 `video` 對到它所屬番劇的訂閱項目。排程項目的 `sn` 是「訂閱當下的某一集
        video_sn」，通常不等於現在這一集——先看精確命中，再看這集的整季集數集合裡有沒有
        哪個排程項目的 sn（＝同一部番劇）。`video.episodes` 是 `get_video()` 已經帶回來的
        集數清單，不用再打網路。"""
        entries = self._schedule_store.get_entries()
        hit = entries.get(video.video_sn)
        if hit is not None:
            return hit
        episode_sns = {ep.video_sn for ep in getattr(video, "episodes", ()) or ()}
        for sn, entry in entries.items():
            if sn in episode_sns:
                return entry
        return None

    def _is_downloaded(self, video_sn: int, output_dir: Path) -> bool:
        """有 DownloadedEpisodeStore（round 6 第 11 項起）就查 DB＋確認檔案還在；
        沒有（舊測試路徑）退回掃檔名 `[video_sn]`。"""
        if self._downloaded_store is not None:
            return self._downloaded_store.is_downloaded(video_sn)
        return _already_downloaded(output_dir, video_sn)

    def downloaded_episode_sns(self, anime_title: str) -> set[int]:
        """給 `web/browse.py` 的集數選取器判斷「這一集是不是已經下載過」用。
        有 DB 表就用 `anime_title` 欄位查（跟路徑結構無關——季別資料夾多一層也沒差）；
        沒有退回掃檔名。"""
        if self._downloaded_store is not None:
            return self._downloaded_store.downloaded_video_sns_for_title(anime_title)
        return _downloaded_video_sns(self._download_dir() / _sanitize_filename_part(anime_title))

    def downloaded_episode_path(self, video_sn: int) -> str | None:
        """已下載且檔案還在的話回傳本地 .mp4 路徑（瀏覽器內建播放器用，使用者 2026-09-04）。"""
        if self._downloaded_store is None:
            return None
        return self._downloaded_store.playable_path(video_sn)

    def downloaded_episode_anime_title(self, video_sn: int) -> str | None:
        """這一集屬於的番劇原始標題（公開模式白名單過濾播放請求用）。"""
        if self._downloaded_store is None:
            return None
        return self._downloaded_store.anime_title_for_episode(video_sn)

    def removed_episode_sns(self, anime_title: str) -> set[int]:
        """集數選取器的「已移除」紫框用（round 7 第 7 項）——曾下載過、但檔案已被
        使用者刪掉的集數。只有 DB 表路徑支援（舊掃檔名路徑無法分辨）。"""
        if self._downloaded_store is None:
            return set()
        return self._downloaded_store.removed_video_sns_for_title(anime_title)

    def removed_download_history(self) -> list[dict]:
        """「資料庫整頓」的「刪除歷史」區塊（round 7 第 7 項）。"""
        if self._downloaded_store is None:
            return []
        return self._downloaded_store.list_removed()

    def clear_removed_download_history(self, video_sn: int | None = None) -> int:
        """硬刪「已移除」的已下載紀錄——使用者在「資料庫整頓」手動清。"""
        if self._downloaded_store is None:
            return 0
        return self._downloaded_store.clear_removed(video_sn)

    def downloaded_anime_summaries(self) -> list[dict]:
        """下載列表頁「已下載的番劇」用——有 DB 表就直接回聚合結果（封面用正確的
        anime sn、完成即時可見）；沒有退回掃 `download_dir` 子資料夾＋檔名標記。"""
        if self._downloaded_store is not None:
            return self._downloaded_store.list_by_anime()
        # 無 DB 的退路（只剩舊測試會走到）：假設下載目錄底下第一層就是番劇資料夾——
        # 季別資料夾功能開啟後這個假設不成立（第一層變成季別），正式路徑一律有 store。
        download_dir = self._download_dir()
        if not download_dir.exists():
            return []
        out = []
        for folder in sorted(download_dir.iterdir()):
            if not folder.is_dir():
                continue
            sns = _downloaded_video_sns(folder)
            if sns:
                out.append(
                    {
                        "anime_sn": None,
                        "title": folder.name,
                        "cover_url": "",
                        "sample_video_sn": min(sns),
                        "episode_count": len(sns),
                        "latest_at": "",
                    }
                )
        return out

    def _fetch_videos(self, entry: ScheduleEntry, *, force_all_episodes: bool = False) -> list[VideoInfo]:
        if entry.mode == "all" or force_all_episodes:
            return self._catalog.get_all_episodes(entry.sn)
        return [self._catalog.get_latest_episode(entry.sn)]

    def _season_folder_for(self, video: VideoInfo, entry: "ScheduleEntry | None") -> str:
        """這一集要放進哪個季別／分類父資料夾（相對於下載目錄）。回傳 `""` ＝直接放
        `下載目錄/番劇名/`。

        1. 排程項目的「分類」（`entry.tag`）有設 → 逐字用（可含 `/` 做多層），使用者
           手動覆寫最大，涵蓋跨季／提前更新／任何個人偏好。
        2. 否則「自動歸類至每季」開著 → 依 `video.season_start` 算季別（見
           `scheduler/seasons.py`；`total_episode <= 1` 的獨立特別篇／電影回 `""`）。
        3. 都沒有 → `""`。
        """
        if entry is not None and entry.tag:
            return entry.tag
        if not self._settings.get("auto_season_folder", DEFAULT_AUTO_SEASON_FOLDER):
            return ""
        return season_folder(
            video.season_start,
            total_episode=video.total_episode,
            include_year=bool(
                self._settings.get("auto_season_year_folder", DEFAULT_AUTO_SEASON_YEAR)
            ),
        )

    def _maybe_download(
        self, video: VideoInfo, display_name: str, parent_folder: str = ""
    ) -> DownloadTrigger:
        """不依賴 ScheduleEntry 的核心觸發邏輯：已下載判斷 → registry.try_start() →
        提交進 download_pool。check_and_download()／trigger_manual_download() 都只
        呼叫這個方法，「要不要下載」只在這裡判斷一次。

        `parent_folder`：季別／分類父資料夾字串（見 `_season_folder_for`），空字串＝
        直接放下載目錄底下。

        access_gate/ 的同帳號多 IP 防護觸發時，`_access_gate_blocked` 為真——這裡是
        唯一的下載觸發入口，檢查放在這裡就同時涵蓋 check_and_download()（含
        custom_schedule.py，共用同一個 check_and_download()）／trigger_manual_
        download()／程式啟動時的 resume_manual_tasks() 三種來源，不用三個地方各自
        檢查一次，見 docs/requirements/access_gate.md「封鎖整個程式」定案。"""
        if self._settings.get("_access_gate_blocked"):
            return DownloadTrigger.BLOCKED_BY_ACCESS_GATE

        output_dir = self._download_dir() / _parent_folder_path(parent_folder) / display_name

        if self._is_downloaded(video.video_sn, output_dir):
            return DownloadTrigger.ALREADY_DOWNLOADED

        if not self._registry.try_start(video.video_sn, display_name=display_name):
            return DownloadTrigger.ALREADY_ACTIVE

        # round 7 第 11 項：進行中的下載寫進 pending_downloads，程式異常關閉後
        # app_shell.resume_pending_downloads() 掃這張表重新提交（不做 segment 真續傳）。
        if self._pending_store is not None:
            self._pending_store.mark_downloading(
                video.video_sn,
                anime_sn=getattr(video, "anime_sn", None),
                anime_title=video.anime_title,
                display_name=display_name,
                parent_folder=parent_folder,
            )

        self._download_pool.submit(
            self._download_worker, video, display_name, output_dir, task_key=video.video_sn
        )
        return DownloadTrigger.SUBMITTED

    def _download_worker(self, video: VideoInfo, display_name: str, output_dir: Path) -> None:
        """在 download_pool 的 worker 執行緒裡跑：實際下載＋registry 收尾，
        搭配 max_concurrent_downloads 讓多部作品可以真的同時下載。兩個既有 except
        分支各自補一次診斷回報（Phase 7，`diagnostics` 可選依賴，`None` 時完全不
        影響原本行為），見 docs/requirements/diagnostics.md「誰呼叫 report()」。
        成功/失敗都各補一次 `notify/` 通知（Phase 8，`notify_store` 同樣可選依賴），
        見 docs/requirements/notify.md「通知類別」一節。"""
        started_at = time.monotonic()
        final_path: Path | None = None
        media = MediaInfo()
        finished_at = datetime.now()
        succeeded = False
        # 整集重試：下載失敗（非中斷、非未預期例外）自動重跑幾次才放棄（round 6 第 9 項）
        attempts = 1 + max(0, int(self._settings.get("episode_retry_count", 1)))
        try:
            last_exc: Exception | None = None
            for attempt in range(attempts):
                try:
                    final_path, media, finished_at = self._download_one(video, display_name, output_dir)
                    last_exc = None
                    break
                except _RETRYABLE_DOWNLOAD_ERRORS as exc:
                    last_exc = exc
                    if attempt + 1 < attempts and not self._download_pool.stop_event.is_set():
                        logger.warning(
                            "%s",
                            self._format_download_failure(
                                display_name, video, exc, attempt + 1, attempts
                            ),
                        )
                        self._wait_before_retry(video.video_sn, attempt)
            if last_exc is not None:
                raise last_exc
        except DownloadDirMissing as exc:
            # 整集下載好了、只差搬進下載目錄，但下載目錄不在。不判失敗——_download_one
            # 已把 pending 列改成 awaiting_move、保留暫存。UI 會出現「再試一次／改下載目錄」。
            logger.warning("%s 待搬移：%s", self._video_label(video, display_name), exc)
            self._registry.cancel(video.video_sn)
        except (DownloadInterrupted, GuestAccessInterrupted) as exc:
            label = self._video_label(video, display_name)
            if self._download_pool.stop_event.is_set():
                # 程式關閉中——不是失敗，pending 列留著讓重啟後續傳整集
                logger.info("%s 下載中斷（程式關閉中）：%s", label, exc)
                self._registry.finish(video.video_sn, success=False, error="已取消")
            else:
                # 使用者按了「中止」——移除 pending（不再下載）、不留失敗紀錄。暫存由
                # _download_one 的 finally 清掉。
                logger.info("%s 使用者中止下載", label)
                self._registry.cancel(video.video_sn)
                if self._pending_store is not None:
                    self._pending_store.remove(video.video_sn)
        except _RETRYABLE_DOWNLOAD_ERRORS as exc:
            logger.warning(
                "%s",
                self._format_download_failure(display_name, video, exc, attempts, attempts),
            )
            if isinstance(exc, GamerServerError) and self._schedule_site_error_retry(
                video, display_name
            ):
                # 已排進延長重試排程（動畫瘋 503 等站方暫時性錯誤）——registry 訊息在
                # 裡面設好了，不當「硬失敗」送診斷／通知，也不寫 stage='failed'。
                pass
            else:
                self._registry.finish(video.video_sn, success=False, error=str(exc))
                self._persist_download_failure(video, display_name, str(exc))
                self._note_gamer_login_stale_if_needed(exc)
                self._report_download_failure(exc, started_at)
                self._notify_download_failure(video, display_name, str(exc))
        except Exception as exc:
            logger.exception("%s 下載時發生未預期的例外", self._video_label(video, display_name))
            self._registry.finish(video.video_sn, success=False, error="未預期的例外，詳見日誌")
            self._persist_download_failure(video, display_name, "未預期的例外，詳見日誌")
            self._report_download_failure(exc, started_at, unexpected=True)
            self._notify_download_failure(video, display_name, "未預期的例外，詳見日誌")
        else:
            succeeded = True
            self._registry.finish(video.video_sn, success=True)
            # pending 列已在 _download_one 搬檔成功後移除（「下載完成」＝資料夾進下載目錄）
            if self._downloaded_store is not None and final_path is not None:
                self._downloaded_store.record(
                    video_sn=video.video_sn,
                    anime_sn=getattr(video, "anime_sn", None),
                    anime_title=video.anime_title,
                    file_path=str(final_path),
                    display_name=display_name,
                )
            if final_path is not None:
                self._notify_download_success(
                    video, display_name, final_path, media, finished_at
                )
            if self._stats_collector is not None:
                try:
                    self._stats_collector.record_download()
                except Exception:  # noqa: BLE001 - 統計埋點不影響下載
                    logger.debug("stats: record_download 失敗", exc_info=True)
        finally:
            # 不管這次下載是不是手動觸發的都呼叫——remove_task() 對不存在的 sn
            # 就是刪除 0 筆，本來就是安全的空操作，不需要額外標記下載來源
            if self._manual_task_store is not None:
                self._manual_task_store.remove_task(video.video_sn)
            # 下載冷卻（round 7 第 10 項 + .0 改進.txt 第 12 項，參考 aniGamerPlus）：
            # 只有「這一集真的下載完成」才冷卻——下載失敗／使用者中止／待搬移都不睡
            # （失敗要能盡快重試，不是被冷卻卡著）。worker slot 在睡眠期間仍被佔著，
            # 一批並發各自跑完各自冷卻、滾動放行下一部。registry 記下冷卻到期時間，
            # 下載列表頁才能顯示「冷卻中，約 N 秒後開始下一部」。
            cooldown = self._settings.get("download_cooldown_seconds", _DEFAULT_DOWNLOAD_COOLDOWN_SECONDS)
            if succeeded and cooldown > 0 and not self._download_pool.stop_event.is_set():
                self._registry.mark_cooldown(video.video_sn, cooldown)
                try:
                    self._download_pool.stop_event.wait(cooldown)
                finally:
                    self._registry.clear_cooldown(video.video_sn)

    def _wait_before_retry(self, video_sn: int, attempt: int) -> None:
        """整集重試之間留出間隔（見 `SETTINGS_KEY_RETRY_BACKOFF_SECONDS` 上面的說明）。
        用 `stop_event.wait()` 而不是 `time.sleep()`——程式關閉中不用乾等這幾秒（比照
        `_download_worker` 下載完成後的冷卻等待，同一種寫法）。"""
        base = float(
            self._settings.get(SETTINGS_KEY_RETRY_BACKOFF_SECONDS, _DEFAULT_RETRY_BACKOFF_SECONDS)
        )
        if base <= 0:
            return
        delay = base * (attempt + 1)
        logger.debug("video_sn=%s 重試前等待 %s 秒", video_sn, delay)
        self._download_pool.stop_event.wait(delay)

    def _format_download_failure(
        self, display_name: str, video: VideoInfo, exc: BaseException,
        attempt: int, total: int,
    ) -> str:
        """下載失敗的日誌訊息（使用者 2026-09-02）——不把整包原始錯誤 dict 印出來，
        `GamerApiError` 拆成「錯誤代碼／錯誤訊息／建議操作」三段。"""
        ep_label = self._notify_episode_label(video)
        head = f"{display_name} {ep_label} 下載失敗（第 {attempt}/{total} 次）"
        if isinstance(exc, GamerApiError):
            diag_on = bool(self._settings.get("diagnostics_enabled", True))
            advice = _download_failure_advice(exc.code, diagnostics_on=diag_on)
            return (
                f"{head}，錯誤代碼：{exc.code}；"
                f"錯誤訊息：{exc.gamer_message}；建議操作：{advice}"
            )
        return f"{head}：{exc}"

    def _note_gamer_login_stale_if_needed(self, exc: BaseException) -> None:
        """下載重試（含換一組新 device_id）到底仍然 code 1007 ＝ 不是 device_id 過期，
        是動畫瘋登入態真的失效了——掛「需要重新登入」的網頁橫幅旗標（跟 cookie_warmup
        背景偵測到失效時同一個旗標／同一條橫幅，使用者 2026-09-02 回報 1007 沒提示）。"""
        if not (isinstance(exc, GamerApiError) and exc.code == 1007):
            return
        # 前綴底線＝執行期狀態；web/__init__.py 的 context processor 讀這個 key 掛橫幅，
        # GamerLoginCoordinator 登入成功時 reset 掉。
        if not self._settings.get("_gamer_login_stale"):
            self._settings.update(
                {"_gamer_login_stale": {"detected_at": datetime.now().isoformat(timespec="seconds")}}
            )

    def _schedule_site_error_retry(self, video: VideoInfo, display_name: str) -> bool:
        """動畫瘋站方暫時性錯誤（`GamerServerError`）→ 排進延長重試排程
        （`_SITE_ERROR_RETRY_SCHEDULE`）。還在排程內回 `True`（已登記，呼叫端不當硬
        失敗）；重試次數用完回 `False`（呼叫端走正常的「放棄、等使用者手動」流程）。
        使用者 2026-09-06。"""
        if self._pending_store is None:
            return False
        row = self._pending_store.get(video.video_sn)
        done = int(row["retry_attempt"]) if row else 0  # 已經自動重試過幾次
        interval = _site_error_retry_interval(done)     # 第 done 次（0 起算）等多久
        if interval is None:
            return False  # 30 次都試完了 → 放棄
        next_at = datetime.now() + timedelta(seconds=interval)
        mins = interval // 60
        label = self._video_label(video, display_name)
        msg = (
            f"動畫瘋暫時無法連線（站方維護中）——約 {mins} 分鐘後自動重試"
            f"（第 {done + 1}/{_SITE_ERROR_RETRY_TOTAL} 次）"
        )
        self._registry.finish(video.video_sn, success=False, error=msg)
        self._pending_store.mark_retry_pending(
            video.video_sn,
            error=msg,
            retry_attempt=done + 1,
            next_retry_at=next_at.isoformat(timespec="seconds"),
            anime_sn=getattr(video, "anime_sn", None),
            anime_title=video.anime_title,
            display_name=display_name,
            parent_folder=(row.get("parent_folder", "") if row else ""),
        )
        logger.info("%s %s", label, msg)
        return True

    def _fail_site_retry_permanently(self, row: dict, sn: int, msg: str) -> None:
        """把一筆延長重試列標成硬失敗、退出重試排程、發下載失敗通知。用在「再重試也
        沒用」的情況（無觀看權限、或重試次數／未預期錯誤用完）。"""
        name = row.get("display_name") or row.get("anime_title") or f"sn {sn}"
        self._pending_store.mark_failed(
            sn, error=msg,
            failure_count=self._registry.failure_count(sn) + 1,
            anime_sn=row.get("anime_sn"),
            anime_title=row.get("anime_title", ""),
            display_name=row.get("display_name", ""),
        )
        self._registry.finish(sn, success=False, error=msg)
        if self._notify_store is not None:
            send_notification(
                self._notify_store, self._settings, self._danmu_session,
                "download_failed", animation_name=name, episode="",
                fail_reason=msg,
                download_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )

    def _advance_site_retry(self, row: dict, sn: int, now: datetime, give_up_msg: str) -> None:
        """站方暫時性錯誤 or 未預期錯誤 → 把延長重試排程往後推一格；次數用完就照
        `give_up_msg` 放棄。**一定要更新 `next_retry_at`**——不然這一列會 past-due 卡在
        `list_retry_pending()` 裡，`_run_loop` 每輪都重打（緊迴圈狂打站方＋狂噴 log，
        使用者 2026-09-08 回報）。"""
        done = int(row.get("retry_attempt", 0))
        interval = _site_error_retry_interval(done)
        if interval is None:
            self._fail_site_retry_permanently(row, sn, give_up_msg)
            return
        mins = interval // 60
        msg = (
            f"動畫瘋暫時無法連線——約 {mins} 分鐘後自動重試"
            f"（第 {done + 1}/{_SITE_ERROR_RETRY_TOTAL} 次）"
        )
        self._pending_store.mark_retry_pending(
            sn, error=msg, retry_attempt=done + 1,
            next_retry_at=(now + timedelta(seconds=interval)).isoformat(timespec="seconds"),
            anime_sn=row.get("anime_sn"), anime_title=row.get("anime_title", ""),
            display_name=row.get("display_name", ""),
            parent_folder=row.get("parent_folder", ""),
        )
        self._registry.finish(sn, success=False, error=msg)

    def _process_site_error_retries(self) -> None:
        """`_run_loop` 每輪呼叫：把到期的 `retry_pending` 列重新提交下載。站方還是壞的話
        `trigger_manual_download` 會再拋 `GamerServerError` → `_download_worker` 或這裡
        接住、把 `retry_attempt` 往下推一格；推到底就標成 `failed`（放棄）。無觀看權限
        （VIP 限定／地區限制／家長密碼）則立刻終止重試——再打站方也不會變。"""
        if self._pending_store is None:
            return
        now = datetime.now()
        for row in self._pending_store.list_retry_pending():
            due = _parse_iso_dt(row.get("next_retry_at"))
            if due is not None and due > now:
                continue
            sn = row["video_sn"]
            try:
                trigger = self.trigger_manual_download(sn, row.get("display_name") or None)
                if trigger == DownloadTrigger.ALREADY_DOWNLOADED:
                    # 這一集其實已經下載好了（可能使用者手動補過）→ 收掉這個排程
                    self._pending_store.remove(sn)
                    self._registry.clear_failure(sn)
            except GamerLoginStale as exc:
                # 帶登入 cookie 還是拿不到權限、且 session 裡有登入身分 → 登入態失效。
                # 掛「動畫瘋登入態失效」橫幅、終止重試，訊息叫使用者重新登入（使用者
                # 2026-09-08：VIP 集數因 cookie 失效被當站方維護每 5 秒重試 30 次、
                # 狂噴例外 log；重新登入後就正常下載了）。
                name = row.get("display_name") or row.get("anime_title") or f"sn {sn}"
                if not self._settings.get("_gamer_login_stale"):
                    self._settings.update(
                        {"_gamer_login_stale": {"detected_at": datetime.now().isoformat(timespec="seconds")}}
                    )
                logger.info("《%s》延長重試終止（動畫瘋登入態失效）：%s", name, exc)
                self._fail_site_retry_permanently(
                    row, sn,
                    "動畫瘋登入態已失效——請到「設定 › 動畫瘋登入」重新登入後再手動重新下載。已停止自動重試。",
                )
            except (WatchingPermissionDenied, ParentPasswordRequired) as exc:
                # 站方沒壞、登入態也沒失效——這一集本來就沒有觀看權限。繼續重試沒有意義。
                msg = (
                    "沒有觀看權限——這一集可能是動畫瘋付費會員（VIP）限定、地區限制、"
                    "或需要家長密碼。已停止自動重試；若你的帳號有 VIP，請到「設定 › 動畫瘋登入」"
                    "重新登入後再手動重下。"
                )
                name = row.get("display_name") or row.get("anime_title") or f"sn {sn}"
                logger.info("《%s》延長重試終止（無觀看權限）：%s", name, exc)
                self._fail_site_retry_permanently(row, sn, msg)
            except _RETRYABLE_DOWNLOAD_ERRORS as exc:
                # 連 get_video 都失敗（站方還在維護）——把排程往下推一格。
                name = row.get("display_name") or row.get("anime_title") or f"sn {sn}"
                logger.warning("《%s》延長重試這一輪仍失敗：%s", name, exc)
                self._advance_site_retry(
                    row, sn, now,
                    "動畫瘋持續無法連線，已重試多次仍失敗——請稍後手動重新下載",
                )
            except Exception:  # noqa: BLE001
                # 未預期例外——不是站方 503、也不是權限問題。**不能**留著 past-due 的列
                # 讓 `_run_loop` 每輪重打（同上）。照重試排程往後推、次數用完就放棄。
                logger.exception("video_sn=%s 站方錯誤延長重試時發生未預期的例外", sn)
                self._advance_site_retry(
                    row, sn, now,
                    "重試多次仍失敗（未預期的錯誤）——請稍後手動重新下載，或到「設定 › 日誌」查看詳情",
                )

    def _next_site_error_retry_wait(self) -> float | None:
        """最近一筆 `retry_pending` 還要多久到期（給 `_run_loop` 調整睡眠時間用）。
        沒有回 `None`。"""
        if self._pending_store is None:
            return None
        now = datetime.now()
        dues = [
            _parse_iso_dt(row.get("next_retry_at"))
            for row in self._pending_store.list_retry_pending()
        ]
        future = [(d - now).total_seconds() for d in dues if d is not None and d > now]
        return min(future) if future else (0.0 if dues else None)

    def _persist_download_failure(self, video: VideoInfo, display_name: str, error: str) -> None:
        """整集重試都失敗 → `pending_downloads` 那一列從 `downloading` 改成 `failed`（不是
        刪掉），程式重啟／更新後 `resume_pending_downloads()` 會把「最近失敗」卡片還原
        （使用者 2026-09-03：「下載失敗的項目在更新或重啟後會遺失」）。"""
        if self._pending_store is None:
            return
        self._pending_store.mark_failed(
            video.video_sn,
            error=error,
            failure_count=self._registry.failure_count(video.video_sn),
            anime_sn=getattr(video, "anime_sn", None),
            anime_title=video.anime_title,
            display_name=display_name,
        )

    def _report_download_failure(self, exc: BaseException, started_at: float, unexpected: bool = False) -> None:
        if self._diagnostics is None:
            return
        error_code = ErrorCode.DOWNLOAD_UNEXPECTED_ERROR if unexpected else _ERROR_CODE_BY_EXCEPTION_TYPE.get(
            type(exc), ErrorCode.DOWNLOAD_UNEXPECTED_ERROR
        )
        # 例外類型名（不含訊息／堆疊，是非個資，見 PRIVACY_POLICY.md）。GamerApiError 一併帶
        # 站方數字回應碼；包裝過的例外（DeviceIdError←JSONDecodeError 之類）帶上根因類別名，
        # 這樣單看一則回報也知道「是 getdeviceid 拿到空回應」還是「連線斷了」（使用者
        # 2026-09-05 回報 #7 太簡潔、看不出線索）。
        if isinstance(exc, GamerApiError):
            exception_type = f"GamerApiError(code={exc.code})"
        else:
            names = [type(exc).__name__]
            cause = getattr(exc, "__cause__", None)
            if cause is not None and type(cause).__name__ not in names:
                names.append(type(cause).__name__)
            exception_type = "←".join(names)
        self._diagnostics.report(
            error_code,
            OperationType.DOWNLOAD_VIDEO,
            connection_status=classify_connection_error(exc),
            network_latency_ms=(time.monotonic() - started_at) * 1000,
            exception_type=exception_type,
        )

    def _backfill_backlog_if_untouched(self, entry: ScheduleEntry, videos: list[VideoInfo]) -> None:
        """使用者 2026-09-05：訂閱＝只自動追**之後**的新集數。web 端 `subscribe` 現在會在
        訂閱當下把既有集數標記為已下載，但那之前建立的舊訂閱沒有——第一次被排程檢查、
        且完全沒有「已標記／已下載」紀錄時補做一次：把當下已上架的所有集數標記為已下載
        （`skipped_episodes`），排程之後只抓新的。修「1007 時第一個抓的是舊集數（例：該抓
        47 卻抓 46）」——那一集其實是動畫瘋 API 還沒列出新集數時的『當下最新』，使用者
        根本沒打算讓排程回頭抓它。"""
        store = self._skipped_episode_store
        if store is None or entry.mode == "all" or not videos:
            return
        if store.count_for_sn(entry.sn) > 0:
            return  # 已經處理過（新訂閱走 web 端、或這裡上一輪補過）
        all_eps = [ep for v in videos for ep in (getattr(v, "episodes", ()) or ())]
        episode_sns = {ep.video_sn for ep in all_eps}
        if not episode_sns:
            return
        # 這個訂閱已經有下載紀錄 → 使用者確實在用排程抓這部，別動它的既有行為
        if self._downloaded_store is not None:
            try:
                if episode_sns & self._downloaded_store.sns_with_records():
                    return
            except Exception:  # noqa: BLE001
                pass
        # 不標記「目前的最新一集」——舊訂閱遷移時那一集可能正是使用者在等的新集數
        # （API 若還沒列出更新的一集，這裡放行的就是它）
        numbered = [
            (int(str(ep.episode).strip()), ep.video_sn)
            for ep in all_eps
            if str(getattr(ep, "episode", "")).strip().isdigit()
        ]
        if numbered:
            episode_sns.discard(max(numbered)[1])
        if not episode_sns:
            return
        title = _sanitize_filename_part(entry.rename or videos[0].anime_title)
        for sn in episode_sns:
            store.mark(entry.sn, sn, title)
        logger.info(
            "%s 首次排程檢查：把當下已上架的 %d 集標記為已下載（訂閱只自動追之後的新集數）",
            self._entry_label(entry), len(episode_sns),
        )

    def _entry_label(self, entry: ScheduleEntry) -> str:
        """排程檢查相關日誌的作品名（使用者 2026-09-05：日誌顯示番劇名不顯示 sn）——
        使用者更名 → 番劇快取標題 → 真的查不到才退回 `sn N`。"""
        if entry.rename:
            return f"《{entry.rename}》"
        cache = self._anime_cache
        if cache is not None:
            try:
                title = cache.anime_title_for(entry.sn)
            except Exception:  # noqa: BLE001
                title = None
            if title:
                return f"《{title}》"
        return f"sn {entry.sn}"

    def _video_label(self, video: VideoInfo, display_name: str) -> str:
        """下載相關日誌的「作品名 第N集」（使用者 2026-09-05）。`display_name` 已是
        使用者更名或乾淨標題。"""
        return f"《{display_name}》{self._notify_episode_label(video)}"

    def _notify_episode_label(self, video: VideoInfo) -> str:
        """通知裡的集數標籤——「第 008 集」（補齊長度照套），番劇內特別篇「特別篇 N」。
        使用者 2026-09-03：改用「第 N 集」（不再是 `[N]`）、依補齊長度補零。"""
        ep = video.episode_number
        pad = self._settings.get("filename_pad_width", DEFAULT_PAD_WIDTH)
        if getattr(video, "is_special_episode", False):
            return f"特別篇 {ep}"
        return episode_zh(ep, pad)

    def _notify_download_success(
        self, video: VideoInfo, display_name: str, final_path: Path,
        media: MediaInfo, finished_at: datetime,
    ) -> None:
        if self._notify_store is None:
            return
        try:
            file_size_mb = round(final_path.stat().st_size / (1024 * 1024), 1)
        except OSError:
            file_size_mb = 0
        send_notification(
            self._notify_store, self._settings, self._danmu_session, "download_success",
            # 使用者 2026-09-03：通知要用已更改的番劇名（display_name＝entry.rename 或
            # 乾淨標題），不是原始 anime_title。@episode@ 現在是「第 N 集」不是「[N]」，
            # 不會再疊出「[1] [1]」。
            animation_name=display_name, episode=self._notify_episode_label(video),
            file_size=file_size_mb,
            # 使用者 2026-08-29 第 14 項：媒體資訊 token
            resolution=media.resolution, fps=media.fps,
            vcodec=media.video_codec, acodec=media.audio_codec, container=media.container,
            # @download_time@＝這一集下載＋壓縮完成的時間（完整日期時間）；
            # @mux_date@／@mux_time@＝同一個時間點拆成日期／時分秒兩段。都比
            # @finish_time@（通知送出時間、dispatch 自動補）早幾秒（使用者 2026-09-04）。
            download_time=finished_at.strftime("%Y-%m-%d %H:%M:%S"),
            mux_date=finished_at.strftime("%Y-%m-%d"), mux_time=finished_at.strftime("%H:%M:%S"),
        )

    def _notify_download_failure(self, video: VideoInfo, display_name: str, fail_reason: str) -> None:
        if self._notify_store is None:
            return
        send_notification(
            self._notify_store, self._settings, self._danmu_session, "download_failed",
            animation_name=display_name, episode=self._notify_episode_label(video),
            fail_reason=fail_reason,
            download_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )

    def _episode_basename(
        self,
        display_name: str,
        video: VideoInfo,
        *,
        media: MediaInfo | None = None,
        finished_at: datetime | None = None,
    ) -> str:
        """輸出檔的基本檔名（不含副檔名），依設定頁的「檔案命名」模板算。
        `display_name` 已經是去過標題尾巴／使用者 rename 的資料夾名，直接當 @anime_title@。
        `media`／`finished_at` 只有合併完、ffprobe 過之後才有值（`@fps@` 這類 token）。"""
        episode_number = video.episode_number
        if getattr(video, "is_special_episode", False):
            # 番劇內特別篇：站方 episodes 裡的集數只是普通數字（實測「徹夜之歌 S2」的
            # 特別篇 episode=1），要靠標題的 [特別篇] 標記，這裡轉成 render_basename
            # 認得的 "特別篇 N" → 檔名變 SP N（補齊長度照樣套）。
            episode_number = f"特別篇 {episode_number}"
        return render_basename(
            self._settings.get("filename_template", DEFAULT_TEMPLATE),
            self._settings.get("filename_pad_width", DEFAULT_PAD_WIDTH),
            anime_title=display_name,
            sn=video.video_sn,
            episode_number=episode_number,
            media=media,
            finished_at=finished_at,
        )

    def _staging_dir(self) -> Path:
        """.ts 片段的暫存根目錄——round 6 第 15 項起放在下載目錄之外
        （app_shell 傳 %LOCALAPPDATA%\\BahaAD\\download_staging\\），下載目錄只會有成品。"""
        if self._staging_root is not None:
            return self._staging_root
        return self._download_dir() / ".staging"

    def _download_one(
        self, video: VideoInfo, display_name: str, output_dir: Path
    ) -> tuple[Path, MediaInfo, datetime]:
        """下載這一集、合併、（可選）寫彈幕，全部在 {staging}/{video_sn}/ 底下完成，
        最後把成品移進 output_dir（下載目錄的番劇資料夾）。回傳 (最終 .mp4 路徑, 媒體資訊, 合併完成時間)。
        使用者定案的流程：建 {sn}/ → 內建 {番劇名}/ → .ts 下到 {sn}/ → 在 {sn}/ 合併 →
        成品移進 {sn}/{番劇名}/ → 再移進下載目錄的 {番劇名}/ → 刪 {sn}/。"""
        # 合併前先算一次檔名（媒體資訊 token 這時還是空的）；合併完 ffprobe 拿到資訊後
        # 若檔名有變（模板用了 @fps@ 這類 token）就改名。
        basename = self._episode_basename(display_name, video)
        sn_dir = self._staging_dir() / str(video.video_sn)
        anime_dir = sn_dir / display_name
        park_staging = False
        try:
            anime_dir.mkdir(parents=True, exist_ok=True)
            # 遊客／非 VIP 下載時 get_playlist() 內部會等約 30 秒廣告——使用者按「中止」
            # 時停在等待迴圈（stop_event_for 是 per-task + 全域 stop 的聯集）。
            stop_check = lambda: self._download_pool.stop_event_for(video.video_sn).is_set()
            # 「前置準備」階段的細部狀態——遊客看廣告下載時卡片才不會一直只顯示「前置準備」
            on_phase = lambda text: self._registry.set_phase(video.video_sn, text)
            on_phase("取得節目資訊")
            video_label = self._video_label(video, display_name)
            playlist_info = self._playlist.get_playlist(
                video.video_sn, stop_check=stop_check, on_phase=on_phase, label=video_label
            )
            self._registry.set_phase(video.video_sn, None)  # 之後交給下載進度百分比
            quality = self._segment_downloader.select_quality(playlist_info, label=video_label)
            local_playlist = self._segment_downloader.download(
                playlist_info,
                quality,
                sn_dir,
                refresh=lambda: self._playlist.get_playlist(
                    video.video_sn, stop_check=stop_check, on_phase=on_phase,
                    refresh_device_id=False,  # 只是簽章網址過期重簽，沿用同一組 device_id
                    label=video_label,
                ),
                progress_callback=lambda p: self._registry.update_progress(video.video_sn, p),
                stop_event=self._download_pool.stop_event_for(video.video_sn),
            )
            muxed = anime_dir / f"{basename}.mp4"
            self._ffmpeg_runner.mux(local_playlist, muxed)

            media = probe_media(muxed)
            finished_at = datetime.now()
            final_basename = self._episode_basename(
                display_name, video, media=media, finished_at=finished_at
            )
            if final_basename != basename:
                muxed.rename(anime_dir / f"{final_basename}.mp4")
                basename = final_basename
            self._write_danmu(video, anime_dir, basename)

            # round 7 第 11 項：整集已下載＋合併完成。要搬進下載目錄時「下載目錄的上層」
            # 不在（網路磁碟／外接裝置沒接）→ 保留暫存、記成 awaiting_move，不判失敗。
            # 下載目錄本身、或季別／分類子資料夾不在（上層還在）＝正常首次下載，直接
            # mkdir。錨定 self._download_dir()、不從 output_dir.parent 往上推——季別資料夾
            # 讓 output_dir 多一層，用 .parent 推會誤判。
            download_dir = self._download_dir()
            if not download_dir.parent.exists():
                if self._pending_store is not None:
                    self._pending_store.mark_awaiting_move(video.video_sn, str(anime_dir))
                park_staging = True
                raise DownloadDirMissing(str(download_dir))

            output_dir.mkdir(parents=True, exist_ok=True)
            for item in anime_dir.iterdir():
                dest = output_dir / item.name
                if dest.exists():
                    dest.unlink()  # 重下同一集就覆蓋
                shutil.move(str(item), str(dest))
            # 「下載任務完成」＝番劇資料夾成功搬進下載目錄那一刻（使用者定案）
            if self._pending_store is not None:
                self._pending_store.remove(video.video_sn)
            return output_dir / f"{basename}.mp4", media, finished_at
        finally:
            # 一般情況（成功、失敗、中止）暫存 {sn}/ 整個清掉（修掉舊版「segment 失敗時
            # local_playlist 還是 None 就不清」的 bug）。只有 awaiting_move 要保留暫存。
            if not park_staging:
                shutil.rmtree(sn_dir, ignore_errors=True)

    def _write_danmu(self, video: VideoInfo, output_dir: Path, basename: str) -> None:
        if not self._settings.get("download_danmu", True):
            return
        try:
            entries = get_danmu(self._danmu_session, video.video_sn)
            write_ass(entries, output_dir / f"{basename}.ass")
        except DanmuError as exc:
            # 彈幕失敗不影響這一集判定為下載成功，只是沒有字幕檔
            logger.warning("video_sn=%s 彈幕抓取/轉檔失敗（不影響本集下載結果）：%s", video.video_sn, exc)

    def _download_dir(self) -> Path:
        return Path(self._settings.get("download_dir", _DEFAULT_DOWNLOAD_DIR))

    def wait_for_downloads(self, timeout: float | None = None) -> None:
        """等待目前已提交給 download_pool 的下載全部完成。測試用來讓非同步下載變成
        可斷言的同步結果；正式執行時不是常態呼叫（下載本來就該在背景默默進行）。"""
        self._download_pool.wait_idle(timeout=timeout)

    # ---- round 7 第 18 項：使用者從下載列表逐項控制 ----

    def abort_download(self, video_sn: int) -> bool:
        """「中止」——取消一個進行中／排隊中的下載。回傳是否真的有這個任務可取消。
        實際的收尾（registry、pending、清暫存）由 `_download_worker` 攔到
        `DownloadInterrupted` 時處理；排隊中還沒跑的任務，pending 列在這裡先清掉。"""
        cancelled = self._download_pool.cancel(video_sn)
        if not cancelled:
            # 沒登記在 pool（可能已跑完，或此功能之前就提交的）——盡力收尾
            if self._registry.is_active(video_sn):
                self._registry.cancel(video_sn)
            else:
                return False
        if self._pending_store is not None:
            self._pending_store.remove(video_sn)
        return True

    def discard_failed(self, video_sn: int) -> bool:
        """「丟棄」——不再下載一個失敗的集數。清 registry 失敗紀錄（卡片消失）＋標記
        skipped（自動排程不再排入）＋清 pending 列。"""
        self._registry.clear_failure(video_sn)
        if self._pending_store is not None:
            self._pending_store.remove(video_sn)
        if self._skipped_episode_store is not None:
            try:
                video = self._catalog.get_video(video_sn)
                self._skipped_episode_store.mark(
                    getattr(video, "anime_sn", 0) or 0, video_sn, video.anime_title
                )
            except Exception:  # noqa: BLE001
                logger.warning("丟棄 video_sn=%s：查番劇資訊失敗，僅清失敗紀錄", video_sn, exc_info=True)
        return True

    def awaiting_move_list(self) -> list[dict]:
        """下載完成、但還沒搬進下載目錄（下載目錄當時不在）的集數——下載列表頁顯示
        「再試一次／改下載目錄」提示用。"""
        if self._pending_store is None:
            return []
        out = []
        for row in self._pending_store.list_awaiting_move():
            out.append(
                {
                    "video_sn": row["video_sn"],
                    "anime_title": row["anime_title"],
                    "display_name": row["display_name"],
                }
            )
        return out

    def failed_download_names(self) -> dict[int, str]:
        """`{video_sn: 顯示名稱}`——持久化的失敗列存了下載當下的番劇名／使用者更名，
        下載列表頁的「最近失敗」卡片拿它當名稱來源（catalog 查不到時的後備），重啟後
        也有名字可顯示，不會變成 sn（使用者 2026-09-03）。"""
        if self._pending_store is None:
            return {}
        return {
            row["video_sn"]: (row["display_name"] or row["anime_title"] or "")
            for row in self._pending_store.list_failed()
        }

    def retry_pending_move(self, video_sn: int) -> tuple[bool, str]:
        """把一個 awaiting_move 的集數從暫存搬進下載目錄。下載目錄還是不在就回
        `(False, 原因)`。"""
        if self._pending_store is None:
            return False, "沒有待搬移的任務"
        row = self._pending_store.get(video_sn)
        if row is None or row["stage"] != "awaiting_move":
            return False, "沒有這個待搬移的任務"
        staged = Path(row["staged_dir"])
        download_root = self._download_dir()
        if not download_root.parent.exists():
            return False, "下載目錄還是找不到，請先確認路徑或把裝置接回來"
        if not staged.exists():
            self._pending_store.remove(video_sn)
            return False, "暫存檔已不存在，請重新下載這一集"
        dest_dir = download_root / _parent_folder_path(row.get("parent_folder", "")) / row["display_name"]
        dest_dir.mkdir(parents=True, exist_ok=True)
        mp4_name = next((p.name for p in staged.iterdir() if p.suffix.lower() == ".mp4"), None)
        for item in staged.iterdir():
            dest = dest_dir / item.name
            if dest.exists():
                dest.unlink()
            shutil.move(str(item), str(dest))
        if self._downloaded_store is not None and mp4_name is not None:
            self._downloaded_store.record(
                video_sn=video_sn,
                anime_sn=row["anime_sn"],
                anime_title=row["anime_title"],
                file_path=str(dest_dir / mp4_name),
                display_name=row["display_name"] or row["anime_title"],
            )
        self._pending_store.remove(video_sn)
        shutil.rmtree(staged.parent, ignore_errors=True)  # {sn}/
        return True, "已搬進下載目錄"

    def retry_all_pending_moves(self) -> int:
        """下載目錄變更／重新可用時，把所有 awaiting_move 的集數搬進去。回傳成功幾筆。"""
        if self._pending_store is None:
            return 0
        moved = 0
        for row in self._pending_store.list_awaiting_move():
            ok, _msg = self.retry_pending_move(row["video_sn"])
            if ok:
                moved += 1
        return moved

    def resume_pending_downloads(self) -> None:
        """程式啟動時：`pending_downloads` 裡還留著 `downloading` 的列＝上次異常關閉時
        正在下載到一半的集數，重新提交整集下載（不做 segment 真續傳）。`awaiting_move`
        的列不動——那要等使用者處理下載目錄。`failed` 的列灌回 `registry` 的失敗紀錄
        ——下載列表頁的「最近失敗」卡片重啟／更新後照樣在（使用者 2026-09-03）。
        `retry_pending`（站方 503 延長重試中）：灌回失敗訊息＋留著列，`_run_loop` 下一輪
        會依 `next_retry_at` 到點重新提交（使用者 2026-09-06）。"""
        if self._pending_store is None:
            return
        for row in self._pending_store.list_downloading():
            try:
                self.trigger_manual_download(row["video_sn"], row["display_name"] or None)
            except Exception:  # noqa: BLE001
                logger.exception("video_sn=%s 續傳未完成的下載失敗", row["video_sn"])
        for row in self._pending_store.list_failed() + self._pending_store.list_retry_pending():
            self._registry.restore_failure(
                row["video_sn"], row.get("failure_count") or 1, row.get("last_error") or None
            )

    def run_once(self) -> None:
        for entry in self._schedule_store.get_entries().values():
            if entry.schedule_weekday is not None:
                continue
            self.check_and_download(entry)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run_loop(self) -> None:
        last_full_check = 0.0  # monotonic 時間；0＝還沒跑過 → 開機立刻跑一次
        while not self._stop_event.is_set():
            interval_minutes = self._settings.get(
                "check_interval_minutes", _DEFAULT_CHECK_INTERVAL_MINUTES
            )
            interval_seconds = interval_minutes * 60
            try:
                if not recheck_pause.is_active(self._settings):
                    # 「重新檢查排程更新」執行期間暫停自動觸發（不中斷進行中的下載），
                    # 見 docs/requirements/web_redesign_round2.md 階段 4。
                    #
                    # **排程檢查（run_once）只在 `check_interval_minutes` 真的到期時才跑**
                    # ——延長重試（3/5/10 分一格）會把整個迴圈叫醒得比較頻繁，但那是為了
                    # 那一集，不該連帶讓「掃全部 55 部訂閱、每部打一次 video.php」也跟著
                    # 每幾分鐘重跑一次（使用者 2026-09-08：某集因 cookie 失效卡在延長重試
                    # → 迴圈被拉到每 5 秒一輪 → run_once 每 5 秒對站方狂打 55 個請求 →
                    # 被站方限流 → 「番劇更新完全沒動靜」、封面圖也載不出來）。
                    now_mono = time.monotonic()
                    if last_full_check == 0.0 or now_mono - last_full_check >= interval_seconds:
                        self.run_once()
                        last_full_check = time.monotonic()
                    # 延長重試每輪都查（它自己看 next_retry_at 決定到期沒），成本低。
                    self._process_site_error_retries()
            except Exception:
                logger.exception("main_loop 這一輪檢查發生未預期的例外")

            # 下次醒來：預設等到下一次排程檢查到期；有延長重試在排隊就提早醒（但不低於
            # 60 秒——避免 past-due 的列把迴圈打成緊迴圈）。
            due_in = interval_seconds
            if last_full_check:
                due_in = max(5.0, interval_seconds - (time.monotonic() - last_full_check))
            next_retry = self._next_site_error_retry_wait()
            if next_retry is not None:
                due_in = min(due_in, max(60.0, next_retry))
            self._stop_event.wait(due_in)
