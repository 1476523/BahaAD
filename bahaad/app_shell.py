"""把 Phase 0～3 組裝成完整啟動流程。規格見 docs/requirements/app_shell.md。

`main.py` 的唯一責任就是呼叫這裡的 `run()`。這個模組分成幾個可以獨立測試的部分——
`build_services()` 只負責「組出所有服務物件」，不啟動任何背景執行緒或網頁伺服器；
`run()` 才是真正會阻塞（`icon.run()`）、啟動所有東西的進入點，這支函式本身不方便寫
自動化測試（會真的開系統匣圖示、真的監聽 port），所以刻意拆得很細，讓其他部分都能
在不碰真正的系統匣/網路的情況下驗證組裝是否正確。
"""

from __future__ import annotations

import ctypes
import importlib.resources
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

import pystray
from PIL import Image, ImageDraw
from waitress.server import create_server

from bahaad import __version__, version_label
from bahaad.access_gate.heartbeat import AccessGateHeartbeat
from bahaad.diagnostics.codes import AuthState
from bahaad.diagnostics.log_handler import DiagnosticsLogHandler
from bahaad.diagnostics.reminder import check_update_reminder
from bahaad.diagnostics.reporter import DiagnosticsReporter
from bahaad.downloader.ffmpeg import FfmpegRunner, find_ffmpeg
from bahaad.verify_code import VerifyCodeGate, VerifyCodeLockout
from bahaad.web.hls import HlsCache
from bahaad.downloader.segment import SegmentDownloader
from bahaad.gamer_client.catalog import CatalogClient
from bahaad.gamer_client.cookie_rotation import CookieRotation
from bahaad.gamer_client.device import DeviceIdManager, GuestDeviceIdManager
from bahaad.gamer_client.fingerprint_fetch import FingerprintFetcher
from bahaad.gamer_client.gamer_login_coordinator import GamerLoginCoordinator
from bahaad.gamer_client.guest_access import GuestAccessClient
from bahaad.gamer_client.playlist import GuestPlaylistIdentity, PlaylistClient
from bahaad.gamer_client.session import GamerSession, GuestSession
from bahaad.net.proxy import build_proxy_selector
from bahaad.registry import DownloadRegistry
from bahaad.runtime_paths import is_compiled, own_exe_path
from bahaad.scheduler.completion_watch import CompletionWatcher
from bahaad.scheduler.cookie_warmup import CookieWarmup
from bahaad.scheduler.custom_schedule import CustomScheduleRunner
from bahaad.cache import CACHE_IMAGES_DIRNAME
from bahaad.cache.images import ImageCacheFetcher
from bahaad.scheduler.recheck import RecheckCoordinator
from bahaad.scheduler.download_pool import DownloadPool
from bahaad.gamer_client.browse import get_weekly_schedule
from bahaad.newanime.convert import NewAnimeConverter
from bahaad.newanime.fetch import NewAnimeFetcher
from bahaad.newanime.notify import NewAnimeNotifier
from bahaad.newanime.watch import NewAnimeWatcher
from bahaad.scheduler.gossip_watch import GossipWatcher
from bahaad.scheduler.main_loop import MainLoop
from bahaad.scheduler.version_check import VersionChecker
from bahaad.scheduler.youranimes_sync import YourAnimesSync
from bahaad.stats.activity import StatsActivity
from bahaad.stats.collector import StatsCollector
from bahaad.stats.heartbeat import StatsHeartbeat
from bahaad.stats.query import StatsQuery
from bahaad.stats.reporter import StatsReporter
from bahaad.store.access_gate import AccessGateStore
from bahaad.store.activation import ActivationStore
from bahaad.store.stats_pending import StatsPendingStore
from bahaad.store.anime_cache import AnimeCacheStore
from bahaad.store.youranimes_cache import YourAnimesCacheStore
from bahaad.youranimes.fetch import YourAnimesFetcher
from bahaad.logging_db import SqliteLogHandler
from bahaad.store.completion_watch import CompletionWatchStore
from bahaad.store.database import Database
from bahaad.store.gossip import GossipStore
from bahaad.store.newanime_cache import NewAnimeCacheStore
from bahaad.store.identity import IdentityStore
from bahaad.store.manual_tasks import ManualTaskStore
from bahaad.store.notifications import NotificationStore
from bahaad.store.logs import LogStore
from bahaad.store.notify import NotifyStore
from bahaad.store.pending_downloads import PendingDownloadStore
from bahaad.store.schedule_list import ScheduleListStore
from bahaad.store.settings import SettingsStore
from bahaad.store.downloaded_episodes import DownloadedEpisodeStore
from bahaad.store.skipped_episodes import SkippedEpisodeStore
from bahaad.store.web_auth import WebAuthStore
from bahaad.updater import UPDATE_STAGING_DIRNAME
from bahaad.updater.applier import install_root as updater_install_root
from bahaad.updater.applier import launch_apply_and_restart
from bahaad.updater.policy import UpdateCoordinator, revalidate_update_flags
from bahaad.vault import Vault
from bahaad.web import WebDeps, create_app
from bahaad.web.settings import _DEFAULT_WEB_PORT

logger = logging.getLogger(__name__)

# 帶專案識別的具名 mutex，避免跟其他程式的 mutex 名稱衝突
_MUTEX_NAME = "BahaAD-e1476523-SingleInstance"
_ERROR_ALREADY_EXISTS = 183
_MB_ICONINFORMATION = 0x40
_MB_YESNO = 0x04
_MB_ICONQUESTION = 0x20
_IDYES = 6

# access_gate_server 的部署網址（見 docs/requirements/access_gate_server.md「平台與
# 部署」一節）：這是建置期常數，不是使用者可調整的設定——一般使用者不會自己架一份
# access_gate_server，比照 updater/manifest.py 的 MANIFEST_URL 定案方式寫死。
# 網域已定案（GitHub OAuth App 的 Redirect URI 也是設這個網址 + /oauth/callback）；
# **`access_gate_server/` 服務本身尚未實際部署到這台機器上**，這個常數先寫好，等
# 服務真的跑起來、DNS/反向代理設定完成後這個值就會是對的，不用再改（見 PROGRESS.md
# Phase 6 章節「下一步」）
_ACCESS_GATE_SERVER_BASE_URL = "https://bahaad.toccf.eu.org"

# 「動畫瘋登入態失效」網頁橫幅旗標的 settings key（前綴底線＝執行期狀態）
_GAMER_LOGIN_STALE_KEY = "_gamer_login_stale"


def data_dir() -> Path:
    """Windows 應用程式資料的標準位置，不用管理者權限就能寫入，跟 download_dir
    （使用者的 ~/Downloads/BahaAD）是兩個不同概念，不要混在一起。"""
    return Path(os.environ["LOCALAPPDATA"]) / "BahaAD"


def setup_logging(base_dir: Path) -> None:
    """掛在 "bahaad" 這個 logger 上（不是 root logger）——bahaad/ 底下所有模組的
    logger 名稱都是 __name__，天生是 "bahaad" 的子 logger，自動繼承這個 handler。
    無主控台視窗的程式沒有 stderr 可看，不設定這個的話，散落在 scheduler/ 各處的
    logger.warning()/logger.exception() 呼叫全部會悄悄消失。

    2026-08-29 定案（使用者）：日誌只寫進 bahaad.db（`logs` 表），不再另外寫
    `logs/bahaad.log` 檔案——理由見 `bahaad/logging_db.py` 開頭。SqliteLogHandler
    在模組頂端 import（不要改成函式內延遲 import）：Nuitka standalone 對函式內
    import 的靜態追蹤不保證跟得到，漏包了就會每次啟動靜默失敗（v0.0.1 首個打包版
    中過這個；現在還多了 `--include-package=bahaad` 兜底）。"""
    bahaad_logger = logging.getLogger("bahaad")
    bahaad_logger.setLevel(logging.INFO)
    try:
        bahaad_logger.addHandler(SqliteLogHandler(LogStore(Database(base_dir / "bahaad.db"))))
    except Exception:  # noqa: BLE001 - DB 建不起來時沒有檔案 log 可退，至少不要炸在啟動
        bahaad_logger.addHandler(logging.lastResort or logging.NullHandler())
        bahaad_logger.warning("SQLite 日誌初始化失敗，這次執行的日誌不會被記錄", exc_info=True)
    _install_crash_hooks(bahaad_logger)


def _install_crash_hooks(log: logging.Logger) -> None:
    """未捕捉的例外（主執行緒 / 背景執行緒）也要進日誌——無主控台的 exe 沒有 stderr，
    不接的話整個行程默默死掉、什麼都查不到。`critical` 等級 → `DiagnosticsLogHandler`
    掛上之後也會自動回報一筆（只帶例外類型名）。"""

    def _main_hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        log.critical("未捕捉的例外（主執行緒）", exc_info=(exc_type, exc_value, exc_tb))

    def _thread_hook(args) -> None:
        if issubclass(args.exc_type, SystemExit):
            return
        log.critical(
            "未捕捉的例外（背景執行緒 %s）",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _main_hook
    threading.excepthook = _thread_hook


# 取得成功的具名 mutex handle 存這裡——正常情況跟著行程生命週期自動釋放，但「重啟
# 工具」時要在開新行程**之前**主動 CloseHandle（見 _relaunch_bahaad）。
_single_instance_handle: int | None = None


def acquire_single_instance_lock(name: str = _MUTEX_NAME) -> bool:
    """回傳 True 代表成功取得鎖（這個行程是唯一實例）；False 代表已經有其他實例在跑。
    正常結束（含異常結束、被工作管理員砍掉）時 Windows 會自動釋放 mutex，不像檔案鎖／
    PID 紀錄檔那樣需要處理殘留；只有「重啟工具」要在開新行程前主動關掉這個 handle。"""
    global _single_instance_handle
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    if not handle:
        # CreateMutexW 失敗（回傳 NULL）視為無法確認，保守起見允許繼續執行，
        # 不要因為單一實例檢查本身出問題就擋下使用者正常啟動程式
        return True
    if ctypes.windll.kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        return False
    _single_instance_handle = handle
    return True


def _relaunch_bahaad() -> None:
    """開一份新的 BahaAD——給系統匣「重啟工具」跟 web「改連接埠後重啟」用。

    **順序很重要**：先 `CloseHandle` 掉本行程持有的具名 mutex，再開新行程。Windows
    具名 mutex 的核心物件只要還有任何 handle 開著就存在，先開新行程的話，新行程自己的
    `CreateMutexW` 會看到 `ERROR_ALREADY_EXISTS`、直接跳「已在執行中」對話框然後退出，
    看起來就是「按了重啟沒反應／變成回報已在執行」——TOCMP／WAD 都踩過的坑。

    打包後：`os.startfile()` 走 ShellExecute（等同使用者自己雙擊），最不會踩到主控台／
    路徑解析的坑。從原始碼跑：`python.exe` + 原本的參數（含 `main.py`）。呼叫端負責在這
    之後盡快讓行程結束，把 port／socket 交還給 OS。"""
    global _single_instance_handle
    if _single_instance_handle is not None:
        ctypes.windll.kernel32.CloseHandle(_single_instance_handle)
        _single_instance_handle = None

    exe = own_exe_path()
    if is_compiled():
        os.startfile(str(exe))  # noqa: S606 - 就是要像雙擊一樣開自己
    else:
        subprocess.Popen(
            [str(exe), *sys.argv],
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
        )


def build_placeholder_icon_image() -> Image.Image:
    """`load_tray_icon_image()` 讀不到品牌圖示時的保險：純色圓形＋「B」字樣。"""
    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((2, 2, size - 2, size - 2), fill=(37, 99, 235, 255))
    draw.text((size / 2 - 6, size / 2 - 10), "B", fill=(255, 255, 255, 255))
    return image


def load_tray_icon_image() -> Image.Image:
    """系統匣圖示：讀 `bahaad/assets/BahaAD.png`（隨套件打包，Nuitka 用
    `--include-package-data=bahaad.assets` 帶進去）。讀不到就退回 placeholder，
    圖示壞掉不該讓整個程式起不來。"""
    # 先試 importlib.resources（標準做法），失敗再試相對 __file__ 的路徑
    # （Nuitka standalone 下有時 importlib.resources 對 package data 行為不一致）
    try:
        with importlib.resources.as_file(
            importlib.resources.files("bahaad.assets").joinpath("BahaAD.png")
        ) as path:
            return Image.open(path).convert("RGBA")
    except Exception:
        pass
    try:
        fallback = Path(__file__).resolve().parent / "assets" / "BahaAD.png"
        return Image.open(fallback).convert("RGBA")
    except Exception:
        logging.getLogger("bahaad").warning("讀不到品牌圖示，系統匣改用佔位圖示", exc_info=True)
        return build_placeholder_icon_image()


@dataclass
class Services:
    database: Database
    settings: SettingsStore
    activation_store: ActivationStore
    schedule_store: ScheduleListStore
    manual_task_store: ManualTaskStore
    skipped_episode_store: SkippedEpisodeStore
    pending_download_store: PendingDownloadStore
    web_auth: WebAuthStore
    vault: Vault
    registry: DownloadRegistry
    catalog: CatalogClient
    main_loop: MainLoop
    custom_schedule_runner: CustomScheduleRunner
    recheck_coordinator: RecheckCoordinator
    gossip_watcher: GossipWatcher
    newanime_cache: NewAnimeCacheStore
    newanime_watcher: NewAnimeWatcher
    cookie_warmup: CookieWarmup
    completion_watcher: CompletionWatcher
    version_checker: VersionChecker
    youranimes_cache: YourAnimesCacheStore
    youranimes_sync: YourAnimesSync
    update_coordinator: UpdateCoordinator
    access_gate_store: AccessGateStore
    access_gate_heartbeat: AccessGateHeartbeat
    diagnostics_reporter: DiagnosticsReporter
    stats_pending_store: StatsPendingStore
    stats_collector: StatsCollector
    stats_reporter: StatsReporter
    stats_heartbeat: StatsHeartbeat
    notify_store: NotifyStore
    gossip_store: GossipStore
    anime_cache: AnimeCacheStore
    image_fetcher: ImageCacheFetcher
    download_pool: DownloadPool
    web_deps: WebDeps


def build_services(base_dir: Path) -> Services:
    """組裝 Phase 0～3 全部服務物件，不啟動任何背景執行緒——啟動是 start_background_
    services() 的責任，這裡只負責「組出來」，方便測試單獨驗證組裝是否正確。"""
    database = Database(base_dir / "bahaad.db")
    settings = SettingsStore(database, defaults={})
    # 進階存取（代理 / proxy，見 docs/requirements/advanced_access.md）：一個共用的
    # ProxySelector 傳給所有對外的 session；總開關關或沒設定＝ None＝直連。設定頁存檔
    # 後會 reload 這個物件（不用重啟）。
    proxy_selector = build_proxy_selector(settings)
    # 匿名啟動識別碼：獨立的 activation.db（不進 bahaad.db）——`/reset-everything`
    # 刪掉整個 bahaad.db 也不會弄丟，只有刪掉整個 %LOCALAPPDATA%\BahaAD 才會重產。
    # 首次啟動立刻產生（見 docs/requirements/realtime_stats.md）。
    activation_store = ActivationStore(Database(base_dir / "activation.db"))
    activation_store.get_or_create()  # 首次啟動立刻產生（不用回傳值）
    # 高風險網頁動作（首次設定／忘記密碼救援）的本機驗證碼關卡（使用者 2026-09-06）。
    # 純記憶體，程式重啟＝重置——不值得為了這個開資料庫表。
    verify_code_gate = VerifyCodeGate()
    verify_code_lockout = VerifyCodeLockout()
    identity_store = IdentityStore(database)
    schedule_store = ScheduleListStore(database)
    manual_task_store = ManualTaskStore(database)
    skipped_episode_store = SkippedEpisodeStore(database)
    # keep=3：HLS 片段是 mp4 的複本（-c copy），保太多會佔一堆磁碟；只留最近看的幾集。
    hls_cache = HlsCache(base_dir / "hls_cache", find_ffmpeg(), keep=3)
    # 偵測到某一集的原始 mp4 被刪掉時，一併清掉那一集的 HLS 快取（使用者 2026-09-06）。
    downloaded_episode_store = DownloadedEpisodeStore(
        database, on_removed=hls_cache.discard
    )
    pending_download_store = PendingDownloadStore(database)
    log_store = LogStore(database)
    web_auth = WebAuthStore(database)
    vault = Vault(database)
    access_gate_store = AccessGateStore(database)

    def _flag_gamer_login_stale() -> None:
        # GamerSession 收到 BAHARUNE=deleted（登入 cookie 被站方消耗掉、救不回）時呼叫，
        # 掛「動畫瘋登入態失效」網頁橫幅（跟 cookie_warmup 背景偵測、code 1007 同一個旗標）。
        if not settings.get("_gamer_login_stale"):
            settings.update(
                {"_gamer_login_stale": {"detected_at": datetime.now().isoformat(timespec="seconds")}}
            )

    session = GamerSession(identity_store, on_login_lost=_flag_gamer_login_stale, proxy_selector=proxy_selector)
    device_manager = DeviceIdManager(identity_store, session)
    # 即時匿名使用統計（使用者 2026-09-08）——跟 diagnostics 完全獨立的一條路徑。
    # 送不出去（access_gate_server 尚未部署／停機）就累加在 stats_pending，之後補送。
    # 沿用同一個 GamerSession 打 access_gate_server（多帶一個無害 Referer，理由同
    # diagnostics_reporter／version_checker）。collector 先於 MainLoop 建好、注入下去。
    stats_pending_store = StatsPendingStore(database)
    stats_collector = StatsCollector(settings, stats_pending_store)
    stats_reporter = StatsReporter(
        settings,
        activation_store,
        stats_pending_store,
        schedule_store,
        http=session,
        base_url=_ACCESS_GATE_SERVER_BASE_URL,
    )
    stats_query = StatsQuery(session, _ACCESS_GATE_SERVER_BASE_URL)
    stats_activity = StatsActivity()
    stats_heartbeat = StatsHeartbeat(
        settings, activation_store, stats_activity,
        http=session, base_url=_ACCESS_GATE_SERVER_BASE_URL,
    )
    # 新集數檢查（`api/anime/v1/video.php`）跟網頁瀏覽（爬 HTML）本來就不需要登入
    # cookie——比照 aniGamerPlus（web 模式的 `animeVideo.php` 是 `no_cookies=True`）。
    # 每次排程檢查、每部追蹤番劇都打一次帶 cookie 的 video.php，會一直輪換 BAHARUNE、
    # 干擾 cookie 保活（使用者 2026-09-08 懷疑「過多的 cookie 導致保活異常」）。改用
    # 各自獨立的無 cookie GuestSession。`catalog` 額外收 `member_session=session`：只有
    # 訪客身分查到某集 `watchingPermission.pass=False`（可能是年齡限制番劇）時，才帶
    # 登入 cookie 再查一次。
    catalog_session = GuestSession(identity_store, proxy_selector=proxy_selector)
    browse_session = GuestSession(identity_store, proxy_selector=proxy_selector)
    catalog = CatalogClient(catalog_session, member_session=session)
    # 片段下載 + master playlist 都打已簽章的 CDN 網址，不需要 cookie（比照 aniGamerPlus
    # `no_cookies=True`）——共用一個無 cookie session。
    cdn_session = GuestSession(identity_store, proxy_selector=proxy_selector)
    # 遊客／非 VIP 看廣告下載（docs/requirements/guest_download.md）：
    # - 已登入非 VIP → 用登入 session 跑交握（member_guest_access）
    # - 完全未登入 → 用不帶登入 cookie 的 GuestSession + 記憶體 device id
    # 同一個 device_id 短時間內對兩部影片同時交握會觸發 code 1007 → member / guest
    # 兩條路共用同一把交握鎖，把交握序列化（分段下載仍並行）。
    # 這把鎖只給「真的會動用登入 cookie／device_id」的流程用（member/guest 下載交握、
    # 換 device_id）。公告掃描／完結偵測本身不需要登入態（首頁公告欄、週期表都是公開
    # 內容，實測訪客身分也看得到——使用者 2026-09-05 指出：原版/舊專案的公告與新集數
    # 檢查本來就不用 cookie），改用下面獨立的 checker_session／checker_catalog，
    # 徹底不跟下載共用 cookie jar，不用再互相搶這把鎖（比照 youranimes 的無 cookie
    # session 做法）。cookie 保活（CookieWarmup）例外——它的工作本來就是維護登入
    # cookie，本質上就是要動到這把鎖／這個 session，維持原樣。
    ad_handshake_lock = threading.Lock()
    # 公告掃描／完結偵測專用：獨立的「訪客」session（跟下面 guest_session 是不同物件、
    # 不同 cookie jar），完全不碰登入 cookie，也不會被下載交握的 skip_if_busy 卡住。
    # （新集數檢查 main_loop／catalog 現在也是無 cookie——見上面 `catalog_session`，
    # 只有查到 `watchingPermission.pass=False` 才 fallback 到帶 cookie 的 `session`。）
    checker_session = GuestSession(identity_store, proxy_selector=proxy_selector)
    checker_catalog = CatalogClient(checker_session)
    guest_session = GuestSession(identity_store, proxy_selector=proxy_selector)
    guest_device_manager = GuestDeviceIdManager(guest_session)
    member_guest_access = GuestAccessClient(
        session, device_manager, settings, activation_lock=ad_handshake_lock,
        is_member_session=True,
    )
    guest_guest_access = GuestAccessClient(
        guest_session, guest_device_manager, settings, activation_lock=ad_handshake_lock
    )
    playlist = PlaylistClient(
        session,
        device_manager,
        settings=settings,
        guest_access=member_guest_access,
        guest_identity=GuestPlaylistIdentity(
            guest_session, guest_device_manager, guest_guest_access
        ),
        is_logged_in=lambda: "BAHAID" in session.current_cookies(),
        # 每次下載換一組對得上目前 cookie 的新 device_id；跟看廣告交握共用同一把鎖
        # 把「換號 + video_src.php」序列化（避免並發下載觸發 code 1007）。
        activation_lock=ad_handshake_lock,
        cdn_session=cdn_session,  # master playlist 用無 cookie session 打
    )
    cookie_rotation = CookieRotation(session, vault, device_manager)
    fingerprint_fetcher = FingerprintFetcher()

    # 「動畫瘋登入態失效」的網頁橫幅旗標（前綴底線＝執行期狀態、不是使用者偏好）。
    # cookie_warmup 背景查完登入態就回報結果來設／清這個旗標——**背景不再自己開登入
    # 視窗**（使用者 2026-09-01「這功能不是可以在背景運作？」）；使用者看到橫幅自己按
    # 「重新登入」，登入成功時 GamerLoginCoordinator 回報清掉。
    def _note_gamer_login_state(valid: bool) -> None:
        if valid:
            settings.reset([_GAMER_LOGIN_STALE_KEY])
        elif not settings.get(_GAMER_LOGIN_STALE_KEY):
            settings.update(
                {_GAMER_LOGIN_STALE_KEY: {"detected_at": datetime.now().isoformat(timespec="seconds")}}
            )

    gamer_login_coordinator = GamerLoginCoordinator(
        vault, session, identity_store, fingerprint_fetcher, cookie_rotation,
        on_login_success=lambda: _note_gamer_login_state(True),
    )

    # 片段下載對 CDN（bahamut.akamaized.net）打的是**已簽章**的網址，不需要登入
    # cookie（比照 aniGamerPlus `parse_playlist()` 的 `no_cookies=True`）。用上面那個
    # 獨立的 `cdn_session`（跟 master playlist 共用）——一次下載會用
    # `max_concurrent_segments` 條執行緒平行抓幾 MB 的片段，全部擠在共用的 `session`
    # 上會（1）跟網頁瀏覽搶同一個 curl_cffi cookie jar、把登入態雜湊攪成 A→B→A
    # （使用者 2026-09-06），（2）讓網頁請求排在一堆片段後面變得很卡／像卡死。
    segment_downloader = SegmentDownloader(http=cdn_session, settings=settings)
    ffmpeg_runner = FfmpegRunner()

    registry = DownloadRegistry()
    download_pool = DownloadPool(settings)

    # 診斷回報「掛在 Phase 6 的後端上」（見 docs/decisions/0000-architecture-overview.md），
    # 送到同一台 access_gate_server，沿用同一個 GamerSession，跟 version_checker/
    # access_gate_heartbeat 同樣的理由：多帶一個無意義但無害的 Referer 標頭，不值得
    # 為了語意純粹另外建一份。先於 main_loop／update_coordinator 建構，兩者都要吃
    # 這個依賴
    diagnostics_reporter = DiagnosticsReporter(
        settings, http=session, base_url=_ACCESS_GATE_SERVER_BASE_URL,
        # .0 改進.txt 第 14 項：回報當下是會員還是遊客身分——看現有 cookie 有沒有 BAHAID
        auth_state_fn=lambda: (
            AuthState.MEMBER if "BAHAID" in session.current_cookies() else AuthState.GUEST
        ),
    )
    # 「錯誤都上傳」（使用者 2026-09-01）：任何 ERROR/CRITICAL 且帶例外的 log record，
    # 自動送一筆診斷回報（只帶例外類型名＋模組，不帶訊息/堆疊）。掛在 `bahaad` logger
    # 上、跟 SqliteLogHandler 並存。開關由 DiagnosticsReporter.report() 內部判斷。
    _bahaad_logger = logging.getLogger("bahaad")
    for _h in list(_bahaad_logger.handlers):
        if isinstance(_h, DiagnosticsLogHandler):
            _bahaad_logger.removeHandler(_h)  # build_services 被叫第二次時換掉舊的（測試會）
    _bahaad_logger.addHandler(DiagnosticsLogHandler(diagnostics_reporter))
    # notify/（Phase 8）：Telegram/Discord 通知，跟診斷回報同樣先於 main_loop／
    # version_checker 建構，兩者都要吃這個依賴；同一個 GamerSession 也拿來打
    # Telegram/Discord API，見 notify.md「實作後補充」
    notify_store = NotifyStore(database)
    gossip_store = GossipStore(database)
    # 番劇完結自動偵測（completion_detection.md）：週期表監視 + 站內「訂閱通知」收件匣
    completion_watch_store = CompletionWatchStore(database)
    notification_store = NotificationStore(database)
    # 番劇資料本地快取（見 docs/requirements/anime_cache.md）：降低對 ani.gamer.com.tw
    # 的請求量。ImageCacheFetcher 打的是圖片 CDN，不需要登入 cookie——跟 segment 一樣
    # 用獨立的 GuestSession，背景一次補幾十張封面時不會拖慢網頁、也不碰登入 cookie jar。
    anime_cache = AnimeCacheStore(database)
    image_session = GuestSession(identity_store, proxy_selector=proxy_selector)
    image_fetcher = ImageCacheFetcher(
        anime_cache, base_dir / CACHE_IMAGES_DIRNAME, image_session, settings=settings
    )

    # 新番快訊（docs/requirements/new_anime_bulletin.md）：抓 GNN 每季「新番節目資訊」
    # 文章。**專用無 cookie curl_cffi**（不帶登入 cookie），掛在 gossip 掃描完之後。
    newanime_cache = NewAnimeCacheStore(database)
    newanime_notifier = NewAnimeNotifier(notify_store, settings, session, newanime_cache)

    def _newanime_weekly_schedule():
        cached = anime_cache.get_home()
        if cached is not None:
            return cached[1]
        return get_weekly_schedule(session)

    # 追蹤 → 訂閱轉換（§7）：番劇上架→週期表比對出真 video_sn→有追蹤的寫進正式排程。
    # `main_loop` 還沒建（下面才建）→ 先不帶，建好後 `set_main_loop()` 補上。
    newanime_converter = NewAnimeConverter(
        settings, newanime_cache, schedule_store, catalog,
        weekly_schedule_fn=_newanime_weekly_schedule,
        stats_collector=stats_collector,
        anime_cache=anime_cache,
    )
    newanime_watcher = NewAnimeWatcher(
        settings, newanime_cache, NewAnimeFetcher(proxy_selector=proxy_selector),
        notifier=newanime_notifier, converter=newanime_converter,
        notification_store=notification_store,
    )

    # 監視公告（Phase 8）：跟 main_loop 是兩個獨立的背景執行緒。使用者 2026-09-05：
    # 公告掃描／集數比對不需要登入態，改用獨立的 checker_session／checker_catalog——
    # 不用再跟下載共用 cookie jar，不傳 activation_lock（不用再因為下載交握進行中
    # 就跳過這一輪，公告掃描徹底獨立）。Telegram/Discord 通知一樣借這個 session 打
    # （外部 API，跟登入 cookie 無關）。`poke()` 掛給下面每一個會跟動畫瘋互動的背景
    # 執行緒（使用者 2026-09-04：只靠 gossip 週期輪詢不夠即時）。
    gossip_watcher = GossipWatcher(
        settings,
        gossip_store,
        schedule_store,
        http=checker_session,
        catalog=checker_catalog,
        notify_store=notify_store,
        anime_cache=anime_cache,
        on_gossip_scanned=newanime_watcher.notify_gossip_scan,
    )

    main_loop = MainLoop(
        schedule_store=schedule_store,
        settings=settings,
        registry=registry,
        catalog=catalog,
        playlist=playlist,
        segment_downloader=segment_downloader,
        ffmpeg_runner=ffmpeg_runner,
        danmu_session=session,
        download_pool=download_pool,
        manual_task_store=manual_task_store,
        diagnostics=diagnostics_reporter,
        notify_store=notify_store,
        gossip_store=gossip_store,
        skipped_episode_store=skipped_episode_store,
        anime_cache=anime_cache,
        staging_root=base_dir / "download_staging",
        downloaded_store=downloaded_episode_store,
        pending_store=pending_download_store,
        on_gamer_activity=gossip_watcher.poke,
        stats_collector=stats_collector,
    )
    newanime_converter.set_main_loop(main_loop)
    custom_schedule_runner = CustomScheduleRunner(
        schedule_store, main_loop, gossip_store=gossip_store, settings=settings,
        completion_watch_store=completion_watch_store,
    )
    recheck_coordinator = RecheckCoordinator(
        schedule_store, main_loop, registry, settings, skipped_episode_store
    )
    cookie_warmup = CookieWarmup(
        vault, schedule_store, cookie_rotation, settings, registry=registry,
        on_login_checked=_note_gamer_login_state,
        on_gamer_activity=gossip_watcher.poke,
        activation_lock=ad_handshake_lock,
    )
    # 番劇完結偵測執行緒：抓週期表（公開內容，不需要登入態）——跟 gossip_watcher 同樣
    # 理由改用獨立的 checker_session，不用再跟下載共用 cookie jar／搶 activation_lock
    # （使用者 2026-09-05）。`gossip_check_interval_minutes` 當週期。自動退訂時連帶清
    # 孤兒紀錄，跟退訂鈴鐺同一個 delete_records
    from bahaad.web.db_cleanup import delete_records as _delete_orphan_records

    completion_watcher = CompletionWatcher(
        schedule_store,
        completion_watch_store,
        notification_store,
        gossip_store,
        notify_store,
        settings,
        http=checker_session,
        anime_cache=anime_cache,
        orphan_cleanup=lambda sn: _delete_orphan_records(
            sn,
            gossip_store=gossip_store,
            manual_task_store=manual_task_store,
            skipped_episode_store=skipped_episode_store,
        ),
        on_gamer_activity=gossip_watcher.poke,
    )
    # 沿用同一個 GamerSession 打 GitHub API：多帶一個對 GitHub 無意義但無害的
    # Referer 標頭，不值得為了語意純粹另外建一個「只給 GitHub 用」的乾淨 session
    # 前置宣告，讓下面的 on_new_version 回呼能引用（update_coordinator 在後面才建）
    _update_coordinator_ref: list = []

    def _kick_diff_update(latest_version: str) -> None:
        # GitHub 上偵測到新版 → 立刻在背景抓差異檔（不等 UpdateCoordinator 自己每小時
        # 那輪）。抓完 _pending_update 就有值，系統匣「套用更新」與網頁橫幅隨即出現
        # （使用者 2026-09-08：「先直接下載好最新版本再顯示提示」）。
        if not _update_coordinator_ref:
            return
        threading.Thread(
            target=_update_coordinator_ref[0].check_once, daemon=True, name="diff-update-on-newver"
        ).start()

    version_checker = VersionChecker(
        settings, http=session, current_version=__version__,
        notify_store=notify_store, on_new_version=_kick_diff_update,
    )
    # youranimes.tw 番劇資料補充（見 docs/requirements/youranimes.md）：每小時抓季度頁補
    # 番劇詳細頁的簡介 + 製作/配音/音樂。**不沿用 GamerSession**——那會把動畫瘋登入
    # cookie 送給第三方站，YourAnimesFetcher 自己開乾淨無 cookie 的 session。
    youranimes_cache = YourAnimesCacheStore(database)
    youranimes_sync = YourAnimesSync(settings, youranimes_cache, YourAnimesFetcher(proxy_selector=proxy_selector))
    update_coordinator = UpdateCoordinator(
        settings,
        http=session,
        current_version=__version__,
        staging_dir=base_dir / UPDATE_STAGING_DIRNAME,
        install_root=updater_install_root(),
        diagnostics=diagnostics_reporter,
    )
    _update_coordinator_ref.append(update_coordinator)  # 給上面的 on_new_version 回呼用
    # 沿用同一個 GamerSession 打 access_gate_server：跟 version_checker/update_
    # coordinator 同樣的理由，多帶一個對 access_gate_server 無意義但無害的 Referer
    # 標頭，不值得為了語意純粹另外建一個乾淨 session
    access_gate_heartbeat = AccessGateHeartbeat(
        settings,
        access_gate_store,
        http=session,
        access_gate_server_base_url=_ACCESS_GATE_SERVER_BASE_URL,
    )
    # 版本更新後如果診斷回報是關閉狀態，設一次性提醒旗標——見
    # docs/requirements/diagnostics.md「使用者可見的部分」第 3 點；這裡跟
    # `resume_manual_tasks()` 一樣是輕量、不碰網路/背景執行緒的 DB 操作，放進
    # build_services() 而不是另外要求 run() 多呼叫一次
    check_update_reminder(settings, __version__)
    # 開機清掉已經不適用的更新旗標（`_pending_update` / `_update_available`）——套用
    # 更新到新版後，新版程式啟動時舊旗標還留著，系統匣「套用更新並重新啟動」與網頁
    # 橫幅會對「你已經在跑的版本」顯示（使用者 2026-09-08）。純本機比對版本號，不碰網路。
    revalidate_update_flags(settings, __version__)

    web_deps = WebDeps(
        database=database,
        schedule_store=schedule_store,
        settings=settings,
        registry=registry,
        catalog=catalog,
        main_loop=main_loop,
        vault=vault,
        web_auth=web_auth,
        manual_task_store=manual_task_store,
        skipped_episode_store=skipped_episode_store,
        access_gate_store=access_gate_store,
        access_gate_http=session,
        access_gate_server_base_url=_ACCESS_GATE_SERVER_BASE_URL,
        # 網頁瀏覽（爬首頁卡片／週期表／番劇詳細頁／搜尋 HTML）不需要登入 cookie——
        # 用無 cookie session，不讓網頁流量一直輪換 BAHARUNE 干擾保活（使用者
        # 2026-09-08）。年齡限制番劇的詳細頁 HTML 會退化成沒有 18 禁專屬資料，但
        # youranimes 補充資料＋episode_group 借快取還是有，可接受。
        browse_http=browse_session,
        notify_store=notify_store,
        notify_http=session,
        gossip_store=gossip_store,
        gossip_watcher=gossip_watcher,
        recheck_coordinator=recheck_coordinator,
        anime_cache=anime_cache,
        image_fetcher=image_fetcher,
        identity_store=identity_store,
        gamer_login_coordinator=gamer_login_coordinator,
        completion_watch_store=completion_watch_store,
        notification_store=notification_store,
        log_store=log_store,
        youranimes_cache=youranimes_cache,
        youranimes_sync=youranimes_sync,
        proxy_selector=proxy_selector,
        newanime_cache=newanime_cache,
        hls_cache=hls_cache,
        verify_code_gate=verify_code_gate,
        verify_code_lockout=verify_code_lockout,
        stats_collector=stats_collector,
        stats_pending_store=stats_pending_store,
        stats_query=stats_query,
        stats_activity=stats_activity,
    )

    return Services(
        database=database,
        settings=settings,
        activation_store=activation_store,
        schedule_store=schedule_store,
        manual_task_store=manual_task_store,
        skipped_episode_store=skipped_episode_store,
        pending_download_store=pending_download_store,
        web_auth=web_auth,
        vault=vault,
        registry=registry,
        catalog=catalog,
        main_loop=main_loop,
        custom_schedule_runner=custom_schedule_runner,
        recheck_coordinator=recheck_coordinator,
        gossip_watcher=gossip_watcher,
        newanime_cache=newanime_cache,
        newanime_watcher=newanime_watcher,
        cookie_warmup=cookie_warmup,
        completion_watcher=completion_watcher,
        version_checker=version_checker,
        youranimes_cache=youranimes_cache,
        youranimes_sync=youranimes_sync,
        update_coordinator=update_coordinator,
        access_gate_store=access_gate_store,
        access_gate_heartbeat=access_gate_heartbeat,
        diagnostics_reporter=diagnostics_reporter,
        stats_pending_store=stats_pending_store,
        stats_collector=stats_collector,
        stats_reporter=stats_reporter,
        stats_heartbeat=stats_heartbeat,
        notify_store=notify_store,
        gossip_store=gossip_store,
        anime_cache=anime_cache,
        image_fetcher=image_fetcher,
        download_pool=download_pool,
        web_deps=web_deps,
    )


def resume_manual_tasks(services: Services) -> None:
    """呼應 web_manual_download.md「重啟後接續」：程式啟動時掃一次還沒完成的手動
    任務，重新觸發——送出手動下載後、還沒下載完程式就關掉的情況，這裡負責接上。

    round 7 第 11 項：`pending_downloads` 表另外記了「所有」正在下載（含排程觸發的）
    的集數，先接續那些整集重下；`manual_tasks` 是它的子集，`_maybe_download` 的
    `registry.try_start()` 會擋掉重複提交。"""
    services.main_loop.resume_pending_downloads()
    for task in services.manual_task_store.list_tasks():
        rename = task["params"].get("rename")
        try:
            services.main_loop.trigger_manual_download(task["sn"], rename)
        except Exception:
            logger.exception("sn=%s 接續手動任務失敗", task["sn"])


def _warm_home_cache(services: Services) -> None:
    """啟動時在背景把首頁快取（本季新番卡片＋週期表）抓一次。

    使用者 2026-09-06：啟動 BahaAD 後如果沒人開內網網頁，透過 DDNS 進來的**公開模式**
    頁面會怪怪的——因為公開模式一律 `cache_only`、不打網路，而首頁快取（週期表也在
    裡面，訂閱／詳細頁都會用到）只有在有人真的用瀏覽器開 `GET /` 時才會被填。這裡讓
    啟動時就先填好，公開模式訪客不用等擁有者先開一次內網頁。`home_data()` 自己有
    `home_refresh_decision` 節流，快取還新鮮就只讀不抓。"""
    try:
        from bahaad.web.anime_data import home_data

        home_data(services.web_deps)
    except Exception:  # noqa: BLE001 - 純預熱，失敗不影響任何東西
        logger.debug("啟動預熱首頁快取失敗", exc_info=True)


def _ensure_firewall_rule(services: Services) -> None:
    """網頁伺服器 bind `0.0.0.0` 之前呼叫：確認防火牆有一條「依映像路徑放行」的規則，
    沒有就**主動跳一次 UAC**建好、等它做完再回來（`bahaad/firewall.py`）。

    使用者 2026-09-08：BahaAD 監聽 `0.0.0.0`（手機／區網裝置才連得到），Windows 對沒有
    數位簽章的 exe 是綁「這個檔案」放行的，自我更新換掉 exe 之後就重新詢問。第一次啟動
    就把 `program=` 路徑規則建好（換 exe 也一直成立）→ 連第一次的 Windows 防火牆詢問都
    不會出現。使用者在 UAC 按「否」→ 設 `_firewall_setup_needed` 讓首頁橫幅留一顆手動
    按鈕，不再自動打擾（`web_lan_access`＝隱藏逃生口、沒有 UI，設 False＝監聽
    `127.0.0.1` 就根本不會走到這裡）。

    `_firewall_rule_ok` 存「已確認過、規則指向的 exe 路徑」——路徑沒變就整個跳過、
    連 `netsh show` 都不打，不拖慢日後每次啟動。"""
    try:
        from bahaad import firewall
        from bahaad.runtime_paths import is_compiled

        if not is_compiled():
            return
        exe = str(own_exe_path())
        if services.settings.get("_firewall_rule_ok") == exe:
            return
        if firewall.rule_matches(own_exe_path()):
            services.settings.update({"_firewall_rule_ok": exe})
            services.settings.reset(["_firewall_setup_needed"])
            return
        if services.settings.get("_firewall_setup_needed"):
            return  # 之前跳過 UAC 被拒 → 靠首頁橫幅的按鈕手動補，不再自動跳
        logger.info("第一次啟動：設定防火牆放行規則（會跳一次系統權限確認）")
        if firewall.ensure_rule(own_exe_path()):
            services.settings.update({"_firewall_rule_ok": exe})
            services.settings.reset(["_firewall_setup_needed"])
        else:
            services.settings.update({"_firewall_setup_needed": True})
    except Exception:  # noqa: BLE001 - 純輔助，失敗不影響啟動
        logger.debug("啟動設定防火牆規則失敗", exc_info=True)


def start_background_services(services: Services) -> None:
    threading.Thread(
        target=_warm_home_cache, args=(services,), daemon=True, name="warm-home-cache",
    ).start()
    services.main_loop.start()
    services.custom_schedule_runner.start()
    services.gossip_watcher.start()
    services.newanime_watcher.start()
    services.cookie_warmup.start()
    services.completion_watcher.start()
    services.version_checker.start()
    services.youranimes_sync.start()
    services.update_coordinator.start()
    services.access_gate_heartbeat.start()
    services.diagnostics_reporter.start()
    services.stats_reporter.start()
    services.stats_heartbeat.start()
    services.image_fetcher.start()


def stop_background_services(services: Services) -> None:
    """停止所有背景執行緒。`download_pool.shutdown()` 現在會 set stop_event 讓進行中的
    下載自己提早中斷（round 6 第 16 項）——不再等它跑完（配上一集可能下載很久，等於
    讓「結束工具」掛很久佔著 port）。診斷回報背景執行緒最後才停——前面幾個服務關閉
    過程中如果觸發了診斷回報，讓它們有機會先送出去，不是關閉順序隨便排的。"""
    services.access_gate_heartbeat.stop()
    services.update_coordinator.stop()
    services.youranimes_sync.stop()
    services.version_checker.stop()
    services.cookie_warmup.stop()
    services.completion_watcher.stop()
    services.newanime_watcher.stop()
    services.gossip_watcher.stop()
    services.custom_schedule_runner.stop()
    # 「重新檢查排程更新」如果正卡在等使用者確認，關掉暫停旗標，避免下次啟動誤以為
    # 還在檢查中而暫停排程（等 30 分鐘 timeout）。已提交的下載走 download_pool 收尾。
    services.recheck_coordinator.cancel()
    services.image_fetcher.stop()
    services.download_pool.shutdown()  # set stop_event，進行中的下載自己中斷
    services.main_loop.stop()
    services.diagnostics_reporter.stop()
    services.stats_reporter.stop()
    services.stats_heartbeat.stop()


def _web_port(settings: SettingsStore) -> int:
    return settings.get("web_port", _DEFAULT_WEB_PORT)


_GITHUB_PROJECT_URL = "https://github.com/1476523/BahaAD"


def _build_tray_icon(
    port: int, version_checker, update_coordinator, settings, base_dir: Path
) -> tuple[pystray.Icon, dict]:
    """回 (icon, control)。`control["restart"]` 被選單設成 True 時，`run()` 在關閉
    流程跑完後會重新啟動 BahaAD。選單回呼裡不做耗時工作、也不做關閉流程本身——
    只呼叫 `icon.stop()` 讓 `icon.run()` 回傳，其餘留給主執行緒。"""
    control = {"restart": False}

    def _pending() -> dict | None:
        p = settings.get("_pending_update")
        return p if isinstance(p, dict) and p.get("version") else None

    def _open_web(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        webbrowser.open(f"http://127.0.0.1:{port}")

    def _start_apply(icon: pystray.Icon) -> None:
        """套用已下載好的更新：起小幫手行程、然後 icon.stop() 讓主執行緒跑優雅關閉
        （小幫手等本行程結束才覆蓋安裝目錄、重啟）。不設 control['restart']——重啟由
        小幫手負責，兩邊都開新行程會打架。"""
        # 套用更新重啟後自動開網頁（跟 web/dashboard.py 的「套用更新」按鈕一致）
        try:
            settings.update({"_reopen_web_on_start": True})
        except Exception:  # noqa: BLE001
            pass
        try:
            launch_apply_and_restart(
                install_root=updater_install_root(),
                staging_dir=base_dir / UPDATE_STAGING_DIRNAME,
                exe_path=own_exe_path(),
            )
        except Exception:  # noqa: BLE001
            logging.getLogger("bahaad").exception("系統匣套用更新：啟動小幫手失敗")
            ctypes.windll.user32.MessageBoxW(
                None, "套用更新失敗，請改從網頁介面的更新橫幅套用。", "BahaAD", _MB_ICONINFORMATION
            )
            return
        icon.stop()

    def _apply_update(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        if _pending() is None:
            return
        _start_apply(icon)

    _update_check_running = threading.Event()

    def _check_update(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        def _work() -> None:
            # 1) 已經下載好了（背景每小時那輪或上次點這個下載完的）→ 直接問要不要套用。
            pending = _pending()
            if pending is not None:
                ver = pending.get("version")
                res = ctypes.windll.user32.MessageBoxW(
                    None,
                    f"更新 v{ver} 已下載完成。現在套用並重新啟動嗎？\n\n"
                    "（也可以稍後從網頁介面的更新橫幅套用。）",
                    "BahaAD 檢查更新",
                    _MB_YESNO | _MB_ICONQUESTION,
                )
                if res == _IDYES:
                    _start_apply(icon)
                return

            # 2) 已經有一輪檢查／下載在跑 → 別重複觸發，請使用者稍候。
            if _update_check_running.is_set():
                ctypes.windll.user32.MessageBoxW(
                    None, "更新檢查正在背景進行中，請稍候……完成後會通知。",
                    "BahaAD 檢查更新", _MB_ICONINFORMATION,
                )
                return

            # 3) 開跑：version_check（快）先問一下 GitHub 有沒有新版，再把 update_coordinator
            #    的「抓 manifest ＋下載差異檔」丟到背景（可能要下載幾十 MB、耗時十幾秒到
            #    一分多鐘——**不能卡在這裡等**，不然使用者按了「檢查更新」像沒反應）。
            _update_check_running.set()
            try:
                latest = None
                try:
                    latest = version_checker.check_once()
                except Exception:  # noqa: BLE001
                    logging.getLogger("bahaad").debug("系統匣檢查更新：version_check 失敗", exc_info=True)

                if latest:
                    ctypes.windll.user32.MessageBoxW(
                        None,
                        f"發現新版本 v{latest}，已開始在背景下載更新檔。\n\n"
                        "下載完成後會跳通知，屆時可從系統匣「套用更新並重新啟動」或網頁橫幅套用。",
                        "BahaAD 檢查更新", _MB_ICONINFORMATION,
                    )
                else:
                    ctypes.windll.user32.MessageBoxW(
                        None,
                        "GitHub 上沒有更新的 Release。仍會在背景比對一次差異更新清單……",
                        "BahaAD 檢查更新", _MB_ICONINFORMATION,
                    )

                try:
                    update_coordinator.check_once()
                except Exception:  # noqa: BLE001
                    logging.getLogger("bahaad").debug("系統匣檢查更新：update_coordinator 失敗", exc_info=True)

                pending = _pending()
                if pending is not None:
                    try:
                        icon.notify(
                            f"更新 v{pending.get('version')} 已下載完成——右鍵系統匣選「套用更新並重新啟動」。",
                            "BahaAD 有更新可套用",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                elif not latest:
                    ctypes.windll.user32.MessageBoxW(
                        None, "目前已是最新版本。", "BahaAD 檢查更新", _MB_ICONINFORMATION,
                    )
            finally:
                _update_check_running.clear()

        threading.Thread(target=_work, daemon=True).start()

    def _about(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        webbrowser.open(_GITHUB_PROJECT_URL)

    def _restart(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        control["restart"] = True
        icon.stop()

    def _quit(icon: pystray.Icon, item: pystray.MenuItem) -> None:
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("開啟網頁", _open_web, default=True),
        pystray.MenuItem("檢查更新", _check_update),
        # 只有 `_pending_update` 有值（差異檔已下載）時才出現
        pystray.MenuItem(
            "套用更新並重新啟動", _apply_update, visible=lambda item: _pending() is not None
        ),
        pystray.MenuItem("關於工具", _about),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("重啟工具", _restart),
        pystray.MenuItem("結束工具", _quit),
    )
    # 系統匣圖示 tooltip 帶版本（打包版含 commit 短雜湊）——滑鼠移上去就看得到是哪一版
    return pystray.Icon("bahaad", load_tray_icon_image(), f"BahaAD v{version_label()}", menu), control


def run() -> None:
    base_dir = data_dir()
    setup_logging(base_dir)

    if not acquire_single_instance_lock():
        ctypes.windll.user32.MessageBoxW(
            None, "BahaAD 已經在執行中，請直接使用系統匣裡的圖示。", "BahaAD", _MB_ICONINFORMATION
        )
        return

    services = build_services(base_dir)
    resume_manual_tasks(services)

    port = _web_port(services.settings)
    icon, tray_control = _build_tray_icon(
        port, services.version_checker, services.update_coordinator, services.settings, base_dir
    )
    # 差異檔下載好時冒一則系統匣氣泡通知（使用者 2026-09-08「好歹也提示說有更新」）。
    # 要在 start_background_services() 之前掛好——UpdateCoordinator 一 start 就會立刻
    # 跑第一輪 check_once()，晚掛就漏掉第一次。
    def _notify_update_ready(version: str, policy_value: str) -> None:
        try:
            icon.notify(
                f"BahaAD v{version} 已下載完成，右鍵系統匣選「套用更新並重新啟動」即可更新。",
                "BahaAD 有更新可套用",
            )
        except Exception:  # noqa: BLE001
            pass

    services.update_coordinator.set_update_ready_callback(_notify_update_ready)

    start_background_services(services)
    # web/dashboard.py 的「套用更新並重新啟動」按鈕透過這個回呼觸發整個程式優雅關閉——
    # 讓 icon.stop() 讓 icon.run() 回傳，之後的關閉流程（下方 icon.run() 回傳後那幾行）
    # 自然接手，不需要另外寫一套關閉邏輯。要在 create_app() 之前設好，不然可能有請求
    # 在這之前就進來讀到 None
    services.web_deps.app_shutdown_hook = icon.stop

    def _request_restart() -> None:
        tray_control["restart"] = True
        icon.stop()

    services.web_deps.app_restart_hook = _request_restart
    # 驗證碼系統匣通知（使用者 2026-09-06）——同樣要在 create_app() 之前設好。
    services.web_deps.tray_notify = icon.notify

    app = create_app(services.web_deps)
    # 監聽範圍：一律 `0.0.0.0`（手機／區網裝置連得到）。`web_lan_access` 是隱藏逃生口
    # ——沒有 UI，只有「只透過 Cloudflare Tunnel／反向代理連、想完全避開防火牆與 UAC」
    # 的極少數人才會自己改 DB 設成 False（監聽 `127.0.0.1`）。監聽 `0.0.0.0` 時，bind
    # 之前先把防火牆規則備好（第一次會跳一次 UAC），之後就地更新換掉 exe 都不會再被
    # 防火牆詢問。
    host = "0.0.0.0" if services.settings.get("web_lan_access", True) else "127.0.0.1"
    if host == "0.0.0.0":
        _ensure_firewall_rule(services)
    # WSGI 伺服器用 **waitress**，不用 werkzeug 內建的 dev server。
    #
    # 為什麼換：werkzeug 的 `WSGIRequestHandler.protocol_version` 預設 HTTP/1.0，**每個
    # 回應都帶 `Connection: close`**——瀏覽器沒辦法重用連線。首頁一次要 ~60 張
    # `/cache/img` ＋ CSS/JS/圖示，全部各自要重新握手一條新 TCP 連線，變成連線風暴，
    # werkzeug 的 accept 迴圈在 Windows 上跟不上就對一部分連線直接送 RST →
    # `ERR_CONNECTION_RESET`、圖示/資料載不出來、首頁一直卡在「載入中」、週期表打不開
    # （使用者 2026-09-08 回報）。`threaded=True` 沒有解決這個——問題是連線數，不是
    # 佇列。waitress 是純 Python、無原生相依、Windows 友善的正式級 WSGI 伺服器，正確
    # 支援 HTTP/1.1 keep-alive，用 asyncore 多工不會一條 idle 連線佔一個執行緒。
    # threads=16：一頁 ~60 張 /cache/img ＋ CSS/JS 同時進來，8 條不太夠（尤其手機
    # 開一頁、電腦又同時開著）。/cache/img 現在不阻塞（見 web/cache.py），純 I/O
    # bound，多開幾條成本很低。
    # connection_limit=1000：waitress 預設 100，超過就**直接關掉新連線**
    # （瀏覽器端 net::ERR_CONNECTION_RESET，使用者 2026-09-09）。一頁幾十張封面 ＋ 手機
    # 電腦同時開，很容易破 100。連線都短命（handler 不阻塞），拉高上限沒什麼成本。
    server = create_server(
        app, host=host, port=port, threads=16, connection_limit=1000, ident="BahaAD"
    )
    server_thread = threading.Thread(target=server.run, daemon=True)
    server_thread.start()

    # 改連接埠後重啟（使用者 2026-08-29 第 12 項）：重啟後自動開新分頁到新的埠
    if services.settings.get("_reopen_web_on_start"):
        services.settings.reset(["_reopen_web_on_start"])

        def _open_after_boot() -> None:
            time.sleep(1.5)  # 等網頁伺服器確定起來
            webbrowser.open(f"http://127.0.0.1:{port}")

        threading.Thread(target=_open_after_boot, daemon=True).start()

    # 首次啟動提示（使用者 2026-08-29 第 7 項）：新使用者不知道 BahaAD 會縮到系統匣，
    # 冒一則氣泡通知帶路。只跳一次，之後靠 `_tray_hint_shown` 旗標略過。
    show_tray_hint = not services.settings.get("_tray_hint_shown", False)

    def _tray_setup(tray_icon: pystray.Icon) -> None:
        tray_icon.visible = True
        if show_tray_hint:
            try:
                tray_icon.notify("點我開啟網頁，或按右鍵查看其他功能。", "BahaAD 已在背景執行")
            except Exception:  # noqa: BLE001 - 通知失敗不影響啟動
                pass
            services.settings.update({"_tray_hint_shown": True})

    icon.run(setup=_tray_setup)  # 阻塞在這裡，直到系統匣選單「結束工具」／「重啟工具」或 web 的更新套用回呼呼叫 icon.stop()

    # 「重啟工具」：**先**開新行程，不等關閉流程——關閉流程可能卡住被下面的看門狗
    # os._exit 砍掉（使用者 2026-09-01 回報：某個背景 HTTP 呼叫沒醒，關閉逾時 10 秒）。
    # 先開新行程 → 重啟瞬間感、關閉流程慢也沒差。mutex 已在 _relaunch_bahaad 裡先關掉，
    # 舊行程還活著也不擋新行程；waitress 建 socket 時 set_reuse_addr()（SO_REUSEADDR）
    # 讓新行程在舊 socket 還沒收乾淨前也能綁上同一個 port。包成「只做一次」，看門狗
    # 逾時路徑當保底。
    _restart_lock = threading.Lock()
    _restart_done = [False]

    def _relaunch_once() -> None:
        if not tray_control["restart"]:
            return
        with _restart_lock:
            if _restart_done[0]:
                return
            _restart_done[0] = True
        try:
            _relaunch_bahaad()
        except Exception:  # noqa: BLE001 - 重啟失敗至少別讓關閉流程也炸掉
            logging.getLogger("bahaad").exception("重啟工具：開新行程失敗")

    _relaunch_once()  # 重啟的話立刻開新行程

    # 看門狗：關閉流程萬一卡住（背景執行緒沒醒、下載沒中斷…），10 秒後強制退出。
    # 使用者按了「結束工具」就是要程式消失，不能因為某個服務停不下來就一直掛著
    # 佔著 port（round 6 第 16 項）。比照 web/auth.py reset 的 os._exit。
    # on_timeout 是保底——正常情況上面那行已經開好新行程了。
    _arm_shutdown_watchdog(10.0, on_timeout=_relaunch_once)

    stop_background_services(services)
    # waitress：close() 關掉監聽 socket、run() 的 asyncore 迴圈隨即收工。萬一沒收乾淨，
    # server_thread 是 daemon、關閉看門狗 10 秒後 os._exit 保底。
    try:
        server.close()
    except Exception:  # noqa: BLE001
        pass
    server_thread.join(timeout=5.0)


def _arm_shutdown_watchdog(seconds: float, on_timeout=None) -> None:
    def _kill() -> None:
        time.sleep(seconds)
        logging.getLogger("bahaad").warning("關閉流程逾時 %.0f 秒，強制退出", seconds)
        if on_timeout is not None:
            try:
                on_timeout()
            except Exception:  # noqa: BLE001
                pass
        os._exit(0)

    threading.Thread(target=_kill, daemon=True).start()
