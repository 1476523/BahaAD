"""播放清單／真正節目網址。規格見 docs/requirements/gamer_client_playlist.md。

流程依 docs/api-observations/playback-and-ads.md 四輪觀察確認，端點/參數/回應格式都是從
正式程式碼實際送出/收到的內容擷取，不是猜測：

1. 取得 deviceid（device.py）
2. GET video_src.php（`videoSn`／`deviceid`／`deviceTypeUseCases=1` 三個 query 參數）
   拿到 playlist_advance.m3u8 的簽章網址——**GET 不是 POST**（2026-08-27 用真實登入
   帳號實測：POST form body 一律回 400「參數錯誤」，GET query string 才成功，跟舊專案
   aniGamerPlus 一致；`api-observations/playback-and-ads.md` 原本推測的 POST 是錯的）
3. 抓 playlist_advance.m3u8（標準 HLS 主播放清單格式），解析出各畫質的 chunklist 網址。
   akamaized.net 的請求要帶 `Origin` 標頭（GamerSession 已預設帶上），否則 403

`checklock.php`／`unlock.php`（同帳號多裝置鎖定機制）**刻意不包在 get_playlist() 裡**：
回頭比對第二輪觀察的原始請求順序，這兩支端點實際上是在播放已經開始、片段都抓了一部分之後
才觸發的，跟「取網址前先鎖、取到後就解鎖」的直覺順序對不上，且 checklock.php 的 error 欄位
完整列舉值還沒摸清楚——如果照原本設計在 get_playlist() 裡 lock/unlock 包住取網址這一步，
unlock 可能在下載真正開始前就把裝置名額放掉，反而失去鎖定的保護效果。這個機制的正確呼叫
時機留給 downloader/ 實作、更了解真實下載流程長短之後再決定，這裡只提供 checklock()／
unlock() 兩個獨立方法供上層視情況呼叫，不假設任何一種用法。
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import unquote, urljoin, urlparse

from bahaad.gamer_client.device import DeviceIdManager
from bahaad.gamer_client.guest_access import (
    DEFAULT_GUEST_DOWNLOAD_ENABLED,
    GUEST_DOWNLOAD_ENABLED_KEY,
    GuestAccessClient,
    GuestAccessError,
)
from bahaad.gamer_client.session import GamerSession, session_auth_digest

logger = logging.getLogger(__name__)

_CHECKLOCK_URL = "https://ani.gamer.com.tw/ajax/checklock.php"
_UNLOCK_URL = "https://ani.gamer.com.tw/ajax/unlock.php"
_VIDEO_SRC_URL = "https://api.gamer.com.tw/anime/v1/video_src.php"
# token.php 回 {"vip": bool, "login": bool, ...}——未登入打這支還是拿得到 `vip`，
# 用來分辨「這一集是付費會員（VIP）限定」還是「免費但訪客要看廣告」（round 7 第 17 項）。
_TOKEN_URL = "https://ani.gamer.com.tw/ajax/token.php"
_DEVICE_TYPE_USE_CASES = "1"

# 標準 HLS 主播放清單格式（RFC 8216）：#EXT-X-STREAM-INF 的下一行是這個畫質的
# chunklist 網址。只抓 BANDWIDTH 屬性做排序/標籤用，其餘屬性（CODECS 等）沒有
# 觀察過站方是否夾帶，用寬鬆的正則忽略即可。
_STREAM_INF_RE = re.compile(r"#EXT-X-STREAM-INF:(?P<attrs>[^\n]*)\n(?P<uri>\S+)")
_BANDWIDTH_RE = re.compile(r"BANDWIDTH=(\d+)")
_EXP_RE = re.compile(r"exp=(\d+)")


def _display(label: str | None, video_sn: int) -> str:
    """下載相關日誌的顯示名——有 `label`（`main_loop._video_label()` 的
    「《番劇名》第N集」）就用那個，沒有（測試／舊呼叫端沒傳）才退回 `video_sn=%s`
    （使用者 2026-09-06：`取得節目網址` 等 log 一直顯示 sn 不顯示番劇名）。"""
    return label or f"video_sn={video_sn}"


class PlaylistError(Exception):
    pass


class GamerApiError(PlaylistError):
    """`video_src.php`／`token.php` 回了 `{"error": {"code": N, "message": "..."}}`。
    `code`／`message` 分開存，讓 `main_loop` 能把日誌／通知組成「錯誤代碼：N；錯誤
    訊息：…；建議操作：…」而不是把整包原始 dict 印出來（使用者 2026-09-02）。"""

    def __init__(self, code, message: str) -> None:
        self.code = code
        self.gamer_message = message or ""
        super().__init__(f"動畫瘋 API 錯誤 code={code}：{self.gamer_message}")


class GuestHandshakeError(PlaylistError):
    """遊客／非 VIP 看廣告交握失敗。PlaylistError 的子類——`main_loop` 既有的
    `except PlaylistError` 會照常處理（失敗、重試、通知）；診斷回報用
    `ErrorCode.GUEST_HANDSHAKE_FAILED`（見 docs/requirements/guest_download.md）。"""


class GamerServerError(PlaylistError):
    """動畫瘋站方暫時性錯誤——`video_src.php` 回 **5xx**、或回應**根本不是 JSON**
    （維護頁面是 HTML）、或非 200 又解析不出任何內容。這**不是**帳號/裝置/權限問題，
    純粹是站方當下不可用，過一陣子自己會好。`PlaylistError` 的子類 → `main_loop` 既有
    的重試流程會照常接手；另外 `_download_worker` 會據此排進「延長重試排程」（使用者
    2026-09-06：碰到動畫瘋 503 時當下直接停、沒有重試）。

    **2026-09-09 收窄**：舊版一律用 `status != 200` 判定，把 HTTP 299（內容其實是
    `{"error": {"code": ...}}` 的 cookie／裝置問題）也吃進來 → cookie 失效被誤判成
    「站方維護中」狂重試 30 次都不會好，換 cookie 才正常。現在非 200 但內容是 JSON
    error 物件的一律走 `GamerApiError`（能觸發 1007 換 device_id／「請重新登入」橫幅）。"""


# _fetch_video_src() 碰到 code 1015（未登入／未交握）時回傳這個哨兵，讓 get_playlist()
# 決定要跑看廣告交握還是直接回明確錯誤訊息。
_VIDEO_SRC_NEEDS_HANDSHAKE = object()


@dataclass(frozen=True)
class GuestPlaylistIdentity:
    """「BahaAD 沒有設定動畫瘋登入」時整套訪客身分：不帶登入 cookie 的 session、
    只放記憶體的訪客 device id、綁在這個 session 上的交握 client。見
    docs/requirements/guest_download.md「session 選擇」。"""

    session: Any
    device_manager: Any
    guest_access: GuestAccessClient


@dataclass(frozen=True)
class QualityVariant:
    bitrate: int
    chunklist_url: str
    expires_at: float

    @property
    def label(self) -> str:
        # 觀察到的目錄命名（720p/1080p）剛好等於 chunklist_url 路徑裡的一段，
        # 比自己拿 bitrate 換算更可靠，直接從網址取
        for part in urlparse(self.chunklist_url).path.split("/"):
            if part.endswith("p") and part[:-1].isdigit():
                return part
        return f"{self.bitrate // 1000}k"


@dataclass(frozen=True)
class PlaylistInfo:
    video_sn: int
    qualities: list[QualityVariant]


class PlaylistClient:
    def __init__(
        self,
        session: GamerSession,
        device_manager: DeviceIdManager,
        *,
        settings: Any = None,
        guest_access: GuestAccessClient | None = None,
        guest_identity: GuestPlaylistIdentity | None = None,
        is_logged_in: Callable[[], bool] | None = None,
        activation_lock: "threading.Lock | None" = None,
        cdn_session: Any = None,
        on_auth_recover: Callable[[], bool] | None = None,
    ) -> None:
        self._session = session
        self._device_manager = device_manager
        self._settings = settings
        # master playlist 是**已簽章**的 CDN 網址，不需要登入 cookie——比照 aniGamerPlus
        # `parse_playlist()` 的 `no_cookies=True`。用獨立的無 cookie session（跟片段下載
        # 同一個）打，不讓它輪換登入 cookie（使用者 2026-09-08：cookie 曝光量對齊
        # aniGamerPlus）。沒傳就退回原本的 session。
        self._cdn_session = cdn_session or session
        # 已登入但非 VIP：用這個（綁在登入 session 上）跑看廣告交握。
        self._member_guest_access = guest_access
        # 完全未登入：整套訪客身分（不帶登入 cookie 的 session）。
        self._guest_identity = guest_identity
        self._is_logged_in = is_logged_in or (lambda: True)
        # 「換一組新 device_id + 打 video_src.php」這段跟裝置身分綁在一起——並發下載
        # 對同一 device_id 同時做這段會被站方判成裝置異常（code 1007）。用這把鎖序列化
        # （跟 guest_access.py 的看廣告交握共用同一把 `ad_handshake_lock`）。分段下載
        # 不受影響。
        self._activation_lock = activation_lock
        # 下載時 video_src.php 回帳號／cookie 類錯誤（`GamerApiError`，例如 HTTP 299
        # 帶 error 物件）→ 先跑一次這個回呼（登入保活瀏覽 `cookie_rotation.warm_up_session`），
        # 閒置沒真死的 session 這樣就救回來，再整段重試一次；回 True＝有跑保活、值得重試。
        # 還是失敗才往上丟、掛「請重新登入」橫幅（使用者 2026-09-10 選 A）。
        self._on_auth_recover = on_auth_recover

    def get_playlist(
        self,
        video_sn: int,
        *,
        stop_check: Callable[[], bool] | None = None,
        on_phase: Callable[[str], None] | None = None,
        refresh_device_id: bool = True,
        label: str | None = None,
    ) -> PlaylistInfo:
        """`refresh_device_id`（預設 True）：這次是「新的一集下載」，確認拿到的 device_id
        跟目前 cookie 對得上（`DeviceIdManager.ensure_fresh()`）。簽章網址過期時的重新
        取號（`segment.py` 的 refresh 回呼）傳 False，直接沿用既有那組。

        **2026-09-06 改**：`ensure_fresh()` 取代了原本「每次下載都 `invalidate()` 硬換一組
        新的」——真實前端本來就是「cookie 沒換就沿用同一組 device_id」（見
        docs/api-observations/device-id.md），同帳號同時間應該只有一組有效 device_id。
        每次下載都硬換，並發下載／連續重試時彼此的 device_id 互踢，才是使用者 2026-09-06
        回報「日誌顯示每次都是全新 device_id、登入 cookie 全程沒變、仍然 1007」的真正
        原因（先前的「cookie 輪換跟 device_id 核發時機錯開」理論，被這批診斷日誌推翻）。"""
        session, device_manager, guest_access = self._pick_identity()

        guard = (
            self._activation_lock
            if (refresh_device_id and self._activation_lock is not None)
            else contextlib.nullcontext()
        )
        if not refresh_device_id:
            # 簽章網址過期重簽：沿用同一組 device_id、且**沒有**取交握鎖——這段期間若有
            # 背景請求／網頁瀏覽輪換了登入 cookie，這裡打 video_src.php 就會 1007
            # （使用者 2026-09-06：排查用，先把這條路標出來）。
            logger.info(
                "簽章網址重簽（%s，沿用 device_id、未取交握鎖，登入態 %s）",
                _display(label, video_sn), session_auth_digest(session),
            )
        with guard:
            if refresh_device_id:
                logger.info(
                    "取得節目網址（%s，%s，登入態 %s）",
                    _display(label, video_sn),
                    "已序列化" if self._activation_lock is not None else "無交握鎖",
                    session_auth_digest(session),
                )
                device_id = device_manager.ensure_fresh()
            else:
                device_id = device_manager.get_or_create()
            playlist_url = self._fetch_video_src_recovered(
                session, device_manager, video_sn, device_id, label
            )

        if playlist_url is _VIDEO_SRC_NEEDS_HANDSHAKE:
            playlist_url = self._handshake_then_refetch(
                session, device_manager, guest_access, video_sn,
                device_manager.get_or_create(), stop_check, on_phase, label,
            )
        master_playlist_text = self._cdn_session.get(playlist_url).text
        qualities = _parse_master_playlist(master_playlist_text, playlist_url)
        return PlaylistInfo(video_sn=video_sn, qualities=qualities)

    def _fetch_video_src_recovered(
        self, session, device_manager, video_sn, device_id, label=None,
    ):
        """`_fetch_video_src_recover_1007` 外再包一層登入態救援：非 1015 的 `GamerApiError`
        （1007 換 device_id 後仍失敗、或 HTTP 299 帶 error 物件的 cookie 失效…）→ 先跑一次
        `on_auth_recover`（登入保活瀏覽），閒置沒真死的 session 這樣救回來，換一組對得上
        新 cookie 的 device_id 再整段重試一次。還是失敗才往上丟（使用者 2026-09-10 選 A）。"""
        try:
            return self._fetch_video_src_recover_1007(
                session, device_manager, video_sn, device_id, label
            )
        except GamerApiError as exc:
            if self._on_auth_recover is None or exc.code == 1015:
                raise
            logger.warning(
                "video_src.php 回 code=%s（%s）——跑一次登入保活瀏覽再重試一次",
                exc.code, _display(label, video_sn),
            )
            try:
                recovered = bool(self._on_auth_recover())
            except Exception:  # noqa: BLE001 - 保活失敗不該蓋掉原本的錯誤
                logger.debug("on_auth_recover 回呼發生例外", exc_info=True)
                recovered = False
            if not recovered:
                raise
            device_manager.invalidate()  # cookie 剛換過，換一組對得上的 device_id
            return self._fetch_video_src_recover_1007(
                session, device_manager, video_sn, device_manager.get_or_create(), label
            )

    def _fetch_video_src_recover_1007(
        self, session, device_manager, video_sn, device_id, label=None,
    ):
        """打 video_src.php；碰 code 1007（裝置驗證異常）＝這組 device_id 被站方標記了，
        換一組全新的再試一次。還是 1007 就往上丟（`main_loop` 會給「請重新登入動畫瘋」
        的建議）。"""
        try:
            return self._fetch_video_src(session, video_sn, device_id)
        except GamerApiError as exc:
            if exc.code != 1007:
                raise
            logger.warning(
                "code 1007（%s）：用的 device_id …%s，目前登入態 %s——"
                "換一組新 device_id 重試一次",
                _display(label, video_sn), device_id[-6:], session_auth_digest(session),
            )
            device_manager.invalidate()
            new_id = device_manager.get_or_create()
            try:
                return self._fetch_video_src(session, video_sn, new_id)
            except GamerApiError as exc2:
                if exc2.code == 1007:
                    logger.error(
                        "code 1007（%s）換 device_id 後**仍然**失敗：新 device_id "
                        "…%s，目前登入態 %s。下載交握這短短兩步（getdeviceid.php→"
                        "video_src.php）中間有別的請求在輪換登入 cookie，或這台帳號的登入"
                        "態真的失效了。",
                        _display(label, video_sn), new_id[-6:], session_auth_digest(session),
                    )
                raise

    def _pick_identity(self):
        """回傳 (session, device_manager, guest_access)。沒登入且有配置訪客身分 → 用訪客
        整套；否則用登入 session（VIP 直接成功、非 VIP 走 member_guest_access）。"""
        if self._guest_identity is not None and not self._is_logged_in():
            gi = self._guest_identity
            return gi.session, gi.device_manager, gi.guest_access
        return self._session, self._device_manager, self._member_guest_access

    def _handshake_then_refetch(
        self, session, device_manager, guest_access, video_sn, device_id,
        stop_check, on_phase=None, label=None,
    ):
        if guest_access is None or not self._guest_download_enabled():
            # 沒配置交握 client（舊呼叫端／測試），或使用者關掉了遊客下載 → 明確錯誤訊息
            raise PlaylistError(self._guest_download_message(session, video_sn, device_id))
        try:
            guest_access.clear_for_download(
                video_sn, label=label, stop_check=stop_check, on_phase=on_phase
            )
        except GuestAccessError as exc:
            raise GuestHandshakeError(str(exc)) from exc
        # 交握途中碰 code 1007 會 invalidate device id——重新取一次
        device_id = device_manager.get_or_create()
        playlist_url = self._fetch_video_src(
            session, video_sn, device_id, after_handshake=True
        )
        if playlist_url is _VIDEO_SRC_NEEDS_HANDSHAKE:
            raise GuestHandshakeError(
                f"video_sn={video_sn} 已跑完看廣告交握，動畫瘋仍未交出節目網址（code 1015）。"
            )
        return playlist_url

    def _guest_download_enabled(self) -> bool:
        if self._settings is None:
            return True
        return bool(
            self._settings.get(GUEST_DOWNLOAD_ENABLED_KEY, DEFAULT_GUEST_DOWNLOAD_ENABLED)
        )

    def checklock(self, video_sn: int, device_id: str) -> dict:
        """呼叫時機未定案（見模組頂端說明），回傳原始回應供呼叫端自己判斷，這裡不阻擋。"""
        response = self._session.get(_CHECKLOCK_URL, params={"device": device_id, "sn": video_sn})
        return response.json()

    def unlock(self, video_sn: int) -> None:
        """呼叫時機未定案（見模組頂端說明），由呼叫端決定何時釋放裝置佔用。"""
        self._session.get(_UNLOCK_URL, params={"sn": video_sn, "ttl": 0})

    def _fetch_video_src(
        self, session, video_sn: int, device_id: str, *, after_handshake: bool = False
    ):
        # 這支端點是 GET＋query string，不是 POST＋form body（實測：POST 一律回
        # 400 INVALID_ARGUMENT「參數錯誤」，跟舊專案 aniGamerPlus 的做法一致）。
        # 參數名稱是 deviceid（全小寫、無底線），送成 device 會被當缺參數。
        params = {
            "videoSn": video_sn,
            "deviceid": device_id,
            "deviceTypeUseCases": _DEVICE_TYPE_USE_CASES,
        }
        response = session.get(_VIDEO_SRC_URL, params=params)
        status = getattr(response, "status_code", None)
        # 內容優先、狀態碼其次——`api.gamer.com.tw` 會用非標準狀態碼回應（實測見過
        # HTTP 299），內容其實是正常 JSON：可能是成功的 `data`，也可能是
        # `{"error": {"code": ...}}` 的帳號／裝置／cookie 問題（例如 cookie 失效）。
        # 舊版一律 `status != 200` → `GamerServerError`「站方維護中」→ 排進 30 次延長
        # 重試，cookie 失效的情況怎麼等都不會好、換 cookie 才正常（使用者 2026-09-09）。
        # 只有「5xx」或「回應根本不是 JSON」（維護頁是 HTML）才是真的站方暫時性錯誤。
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            body = (getattr(response, "text", "") or "")[:300].replace("\n", " ")
            logger.warning("video_src.php 回應不是 JSON（HTTP %s）：%s", status, body)
            raise GamerServerError(
                "video_src.php 回應不是 JSON（動畫瘋可能正在維護，稍後重試）"
            ) from exc
        if status is not None and status >= 500:
            raise GamerServerError(
                f"video_src.php 回應 HTTP {status}（動畫瘋站方暫時性錯誤，稍後重試）"
            )
        if status is not None and status != 200:
            # 非 200、但內容是 JSON——多半帶 error.code（下面處理）。記一筆診斷，之後才
            # 知道 HTTP 299 這種到底裝了什麼、對應哪個 error code。
            logger.warning(
                "video_src.php 非 200（HTTP %s）但回應是 JSON：%s", status, str(payload)[:300]
            )
        # 未登入／未交握時 video_src.php 回 {"error": {"code": 1015, ...}}。站上訪客能看
        # （含約 30 秒廣告），要先跑過 token.php + 看廣告交握才拿得到 playlist——回哨兵讓
        # get_playlist() 決定要跑交握（guest_access.py）還是直接回明確錯誤（round 6 第 9
        # 項 / guest_download.md）。after_handshake=True 時交握已跑過，仍 1015 → 一樣回
        # 哨兵，由呼叫端丟出「交握後仍失敗」的清楚訊息。
        error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(error, dict) and "code" in error:
            if error.get("code") == 1015:
                return _VIDEO_SRC_NEEDS_HANDSHAKE
            # 其他 error code（1007 裝置驗證異常…）——拆成 code/message，別把整包 dict
            # 原封不動印進日誌（使用者 2026-09-02）
            raise GamerApiError(error.get("code"), error.get("message"))
        try:
            use_cases = payload["data"]["srcUseCases"]
            playlist_url = use_cases[0]["src"]["playlist"]
        except (KeyError, IndexError, TypeError) as exc:
            if status is not None and status != 200:
                # 非 200、內容是 JSON、卻既沒有 error.code 也沒有 data——當可重試的站方
                # 錯誤，不要一路衝成「未預期的例外」被判硬失敗。
                raise GamerServerError(
                    f"video_src.php 回應 HTTP {status}、內容無法解析（稍後重試）"
                ) from exc
            raise PlaylistError(f"video_src.php 回應格式不符預期: {payload}") from exc
        if not playlist_url:
            raise PlaylistError(f"video_sn={video_sn} 沒有拿到節目網址（可能沒有觀看權限）")
        return playlist_url

    def _guest_download_message(self, session, video_sn: int, device_id: str) -> str:
        """code 1015 時、但**沒有跑看廣告交握**（沒配置 guest_access／使用者關掉遊客
        下載）的說明訊息（round 7 第 17 項）。打一次 token.php 看 `vip` 分辨兩種情況。"""
        base = "請到「設定 › 動畫瘋登入」登入，或開啟「允許未登入／非 VIP 下載」。"
        try:
            resp = session.get(
                _TOKEN_URL, params={"adID": "undefined", "sn": video_sn, "device": device_id}
            )
            data = resp.json()
        except Exception:  # noqa: BLE001 - 拿不到就退回通用訊息
            return f"video_sn={video_sn} 未登入動畫瘋，無法下載。{base}"
        if isinstance(data, dict) and data.get("vip") is True:
            return (
                f"video_sn={video_sn} 這一集是動畫瘋付費會員（VIP）限定，"
                f"要登入有 VIP 的動畫瘋帳號才能下載。{base}"
            )
        if isinstance(data, dict) and data.get("vip") is False:
            return (
                f"video_sn={video_sn} 這一集是免費番劇，站上訪客要看完約 30 秒廣告才能看。"
                f"目前未啟用「未登入／非 VIP 下載」。{base}"
            )
        return f"video_sn={video_sn} 未登入動畫瘋，無法下載。{base}"


def _parse_master_playlist(text: str, base_url: str) -> list[QualityVariant]:
    qualities = []
    for match in _STREAM_INF_RE.finditer(text):
        bandwidth_match = _BANDWIDTH_RE.search(match.group("attrs"))
        bitrate = int(bandwidth_match.group(1)) if bandwidth_match else 0
        uri = match.group("uri")
        chunklist_url = uri if uri.startswith("http") else urljoin(base_url, uri)
        qualities.append(
            QualityVariant(
                bitrate=bitrate,
                chunklist_url=chunklist_url,
                expires_at=_extract_expiry(chunklist_url),
            )
        )
    if not qualities:
        raise PlaylistError("playlist_advance.m3u8 沒有解析出任何畫質")
    return qualities


def _extract_expiry(signed_url: str) -> float:
    # exp 在主清單網址（hdnts）裡是 query string、有 URL 編碼（exp%3D...），在各畫質的
    # chunklist/key/ts 網址（hdntl）裡是路徑片段、沒有編碼（exp=...）——兩種格式都先
    # unquote() 一次再找 exp=(數字) 就能統一處理，不用分開寫兩套解析邏輯。
    #
    # 這兩組期限不是同一個值：觀察到 hdntl（畫質層級）的到期時間比 hdnts（主清單）
    # 晚了約 22.5～23 小時，真正限制片段下載期限的是 hdntl，所以 expires_at 掛在
    # QualityVariant 上、從 chunklist_url 本身抓，不是從主清單網址抓。
    exp_match = _EXP_RE.search(unquote(signed_url))
    if not exp_match:
        raise PlaylistError(f"網址裡沒有 exp 欄位，無法判斷有效期限: {signed_url}")
    return float(exp_match.group(1))
