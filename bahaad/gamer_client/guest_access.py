"""遊客／非 VIP 下載：跑一次「看廣告回報」交握，讓 video_src.php 交出跟 VIP 一樣格式的
節目 playlist。規格見 docs/requirements/guest_download.md。

對照 aniGamerPlus `Anime.py.__get_m3u8_dict()` 的去廣告交握重寫（`docs/custom-features.md`
已授權「自訂功能可參考舊專案設計決策」）。伺服器用 `device_id` 在後端記「這個裝置的廣告
看完沒有」，`token.php` 的 `time` 從 0 變 1 就是通過訊號——沒有不透明憑證。

`PlaylistClient.get_playlist()` 在 `video_src.php` 回 `code 1015` 時呼叫本模組的
`clear_for_download()`，完成後重打一次 `video_src.php`。
"""

from __future__ import annotations

import logging
import random
import string
import threading
import time
from datetime import datetime
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://ani.gamer.com.tw/ajax/token.php"
_UNLOCK_URL = "https://ani.gamer.com.tw/ajax/unlock.php"
_CHECKLOCK_URL = "https://ani.gamer.com.tw/ajax/checklock.php"
_CASTCISHU_URL = "https://ani.gamer.com.tw/ajax/videoCastcishu.php"
_VIDEO_START_URL = "https://ani.gamer.com.tw/ajax/videoStart.php"

# aniGamerPlus 寫死的「看廣告回報」session id。站方對免費番劇的看廣告回報路徑不嚴格
# 驗證這個值（歷史上換過幾次，更早是 195081）。哪天站方開始驗證這裡就會壞——見
# guest_download.md「風險」，長期解法是真的去解析 IMA SDK 拿 adSid，v1 不做。
_AD_SESSION_ID = "194699"

# 設定 key／預設值——web/settings.py 也 import 這些，不在兩邊各寫一份。
GUEST_DOWNLOAD_ENABLED_KEY = "guest_download_enabled"
DEFAULT_GUEST_DOWNLOAD_ENABLED = True
ADS_TIME_SETTING_KEY = "ads_time"
DEFAULT_ADS_TIME_SECONDS = 30
# 站方最低廣告時間歷史上 8 → 20 → 25 → 30 一路漲，低於這個值一定不夠。
MIN_ADS_TIME_SECONDS = 20
_ADS_TIME_CALIBRATION_BUFFER_SECONDS = 2

_CONFIRM_MAX_ATTEMPTS = 10
_CONFIRM_RETRY_WAIT_SECONDS = 2


class GuestAccessError(Exception):
    """遊客／非 VIP 交握失敗，訊息已本地化（比照 playlist.py Stage G 的分類）。"""


class GuestGeoBlockedError(GuestAccessError):
    """token.php 回應缺 `time` 欄位——IP 不被動畫瘋認可（地區限制）。"""


class GuestAdClearanceError(GuestAccessError):
    """廣告流程跑完、重試多次後 token.php 的 `time` 還是不變 1。"""


class GuestAccessInterrupted(Exception):
    """交握途中收到中止訊號（使用者中止下載／程式關閉）——不是失敗，呼叫端據此收尾。"""


class _SessionProtocol(Protocol):
    def get(self, url: str, **kwargs: Any) -> Any: ...


class _DeviceManagerProtocol(Protocol):
    def get_or_create(self) -> str: ...

    def invalidate(self) -> None: ...


class _SettingsProtocol(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def update(self, partial: Any) -> Any: ...


class GuestAccessClient:
    def __init__(
        self,
        session: _SessionProtocol,
        device_manager: _DeviceManagerProtocol,
        settings: _SettingsProtocol,
        *,
        activation_lock: threading.Lock | None = None,
        is_member_session: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session = session
        self._device_manager = device_manager
        self._settings = settings
        # True＝這個 client 綁在「已登入」的 session 上（app_shell 的 member_guest_access）。
        # 用來分辨「已登入但非 VIP，改看廣告」vs「登入其實已失效」——後者要提示使用者。
        self._is_member_session = is_member_session
        # 同一個 device_id 短時間內對兩部影片同時交握會被判裝置異常（code 1007）——
        # 用一把鎖把交握（拿到 video_src 結果之前的部分）序列化，分段下載仍可並行。
        # app_shell 傳同一把進來讓 member/guest 兩條路共用；沒傳就自己建一把。
        self._activation_lock = activation_lock or threading.Lock()
        self._sleep = sleep
        self._monotonic = monotonic
        # 一次 clear_for_download() 期間的日誌用作品名（「《番劇名》第N集」，呼叫端帶進來）。
        # 交握本身已被 _activation_lock 序列化，同一個 instance 不會同時跑兩次，直接放
        # instance 屬性就好，不用逐一往下傳（使用者 2026-09-05：日誌顯示番劇名不顯示 sn）。
        self._label: str | None = None

    def clear_for_download(
        self,
        video_sn: int,
        *,
        label: str | None = None,
        stop_check: Callable[[], bool] | None = None,
        on_phase: Callable[[str], None] | None = None,
    ) -> None:
        """跑交握（token.php → 裝置鎖 → 廣告 → 確認）。成功回 None，呼叫端接著自己重打
        video_src.php。失敗丟 GuestAccessError（訊息已本地化）；中止丟 GuestAccessInterrupted。

        `label`：日誌用作品名（「《番劇名》第N集」），沒帶就用 `video_sn=<n>`。
        `on_phase(text)`：回報目前在做哪一步（「等待廣告播放（約 N 秒）」等），讓下載列表
        卡片不會一直只顯示「前置準備」（使用者 2026-09-01 回饋）。"""
        check = stop_check or (lambda: False)
        note = on_phase or (lambda _text: None)
        self._label = label or f"video_sn={video_sn}"
        self._acquire_activation_lock(check)
        try:
            self._raise_if_stopped(check, video_sn, "交握前")
            note("正在與動畫瘋交握")
            device_id = self._device_manager.get_or_create()
            info = self._gain_access(video_sn, device_id)
            self._unlock_dance(video_sn, device_id)
            self._raise_for_access_error(info, video_sn)
            if info.get("vip") is True:
                logger.info("%s 交握：VIP 身分，跳過廣告", self._label)
                return
            self._log_guest_download_reason(video_sn, info)
            ad_started_at = self._watch_ad(video_sn, check, note)
            self._video_start(video_sn)
            self._confirm_ad_cleared(video_sn, device_id, check, ad_started_at, note)
        finally:
            self._activation_lock.release()
            self._label = None

    def _log_guest_download_reason(self, video_sn: int, info: dict) -> None:
        """把「為什麼這一集改用看廣告下載」寫進日誌——使用者 2026-09-04 回報「用了遊客
        下載卻沒有任何提示，也看不出是不是帳號檢查出問題」。token.php 的 `login` 欄位
        分辨「已登入但非 VIP」（正常）跟「登入其實失效了」（要提示重新登入）。"""
        logged_in = bool(info.get("login"))
        if self._is_member_session and not logged_in:
            logger.warning(
                "%s 動畫瘋登入已失效（token.php 回 login=false），這一集改用看廣告下載"
                "——請到「設定 › 動畫瘋登入」重新登入", self._label,
            )
            self._flag_login_stale()
        elif self._is_member_session:
            logger.info("%s 帳號不是 VIP，這一集改用看廣告下載", self._label)
        else:
            logger.info("%s 未登入動畫瘋，改用訪客身分（看廣告）下載", self._label)

    def _flag_login_stale(self) -> None:
        """設定頁／全站橫幅「登入態失效」旗標（跟 cookie_warmup / code 1007 同一個）。"""
        try:
            if not self._settings.get("_gamer_login_stale"):
                self._settings.update(
                    {"_gamer_login_stale": {"detected_at": datetime.now().isoformat(timespec="seconds")}}
                )
        except Exception:  # noqa: BLE001 - 設定寫失敗不該擋下載
            logger.debug("設定 _gamer_login_stale 失敗", exc_info=True)

    # ---- 交握鎖 --------------------------------------------------------

    def _acquire_activation_lock(self, check: Callable[[], bool]) -> None:
        # 短逾時輪詢：還沒排到交握的集數能在使用者中止的當下立刻放棄，不用乾等到輪到自己。
        while not self._activation_lock.acquire(timeout=1):
            if check():
                raise GuestAccessInterrupted("等待交握鎖時收到中止訊號")

    def _raise_if_stopped(self, check: Callable[[], bool], video_sn: int, when: str) -> None:
        if check():
            raise GuestAccessInterrupted(f"{self._label or f'video_sn={video_sn}'} {when}收到中止訊號")

    # ---- token.php / 裝置鎖 -------------------------------------------

    def _gain_access(self, video_sn: int, device_id: str) -> dict:
        resp = self._session.get(
            _TOKEN_URL,
            params={
                "adID": "0",
                "sn": video_sn,
                "device": device_id,
                "hash": _random_hash(),
            },
        )
        return _as_dict(resp)

    def _raise_for_access_error(self, info: dict, video_sn: int) -> None:
        error = info.get("error")
        if not isinstance(error, dict):
            return
        code = str(error.get("code"))
        message = str(error.get("message") or "")
        tag = self._label or f"video_sn={video_sn}"
        if code == "1007":
            self._device_manager.invalidate()
            logger.warning("%s 裝置驗證異常（code 1007），已清 device_id", tag)
            raise GuestAccessError(
                f"{tag} 動畫瘋裝置驗證異常（code 1007），"
                f"已重設裝置識別碼，稍後會自動重試。"
            )
        # 其他 code（付費會員限定 / 年齡限制 / …）——目前沒有樣本能細分，給通用訊息，
        # 由 main_loop 既有的下載失敗整合點送診斷回報收樣本（guest_download.md）。
        logger.warning(
            "%s 交握收到錯誤 code=%s message=%s", tag, code, message
        )
        raise GuestAccessError(
            f"{tag} 無法以遊客／非 VIP 身分下載這一集"
            f"（可能是付費會員限定或年齡限制番劇）。動畫瘋回應：{message or code}"
        )

    def _unlock_dance(self, video_sn: int, device_id: str) -> None:
        # 照 aniGamerPlus 的順序 unlock → checklock → unlock → unlock。訪客沒有帳號、
        # 裝置鎖意義不大，但這幾支可能是伺服器願意交出 playlist 的狀態機的一部分，
        # 保守照做。回應不解讀（checklock 的 error 列舉值仍未摸清）。
        self._unlock(video_sn)
        self._session.get(_CHECKLOCK_URL, params={"device": device_id, "sn": video_sn})
        self._unlock(video_sn)
        self._unlock(video_sn)

    def _unlock(self, video_sn: int) -> None:
        self._session.get(_UNLOCK_URL, params={"sn": video_sn, "ttl": 0})

    # ---- 廣告流程 -----------------------------------------------------

    def _watch_ad(
        self, video_sn: int, check: Callable[[], bool], note: Callable[[str], None]
    ) -> float:
        ads_time = self._ads_time_setting()
        started_at = self._monotonic()
        logger.info("%s 開始等待廣告約 %d 秒", self._label, ads_time)
        self._start_ad(video_sn)
        self._interruptible_sleep(
            ads_time, check, video_sn,
            on_tick=lambda rem: note(f"等待廣告播放（約 {rem} 秒）"),
        )
        self._skip_ad(video_sn)
        return started_at

    def _confirm_ad_cleared(
        self,
        video_sn: int,
        device_id: str,
        check: Callable[[], bool],
        ad_started_at: float,
        note: Callable[[str], None],
    ) -> None:
        note("確認廣告播放完成")
        for attempt in range(_CONFIRM_MAX_ATTEMPTS):
            self._raise_if_stopped(check, video_sn, "確認廣告時")
            resp = self._session.get(
                _TOKEN_URL,
                params={"sn": video_sn, "device": device_id, "hash": _random_hash()},
            )
            data = _as_dict(resp)
            if "time" not in data:
                raise GuestGeoBlockedError(
                    f"{self._label or f'video_sn={video_sn}'} 遭到動畫瘋地區限制，你的 IP 可能不被動畫瘋認可。"
                )
            if data.get("time") == 1:
                if attempt > 0:
                    # 只在「本來設的秒數不夠、需要追加等待」時才校準，且只往上調
                    # （比照 aniGamerPlus——ads_time 單調逼近真實最低值）。
                    self._calibrate_ads_time(self._monotonic() - ad_started_at, video_sn)
                logger.info(
                    "%s 廣告已通過（第 %d 次確認）", self._label, attempt + 1
                )
                return
            remaining = _CONFIRM_MAX_ATTEMPTS - attempt - 1
            logger.info(
                "%s 廣告似乎還沒去除，追加等待 %ds（剩 %d 次）",
                self._label,
                _CONFIRM_RETRY_WAIT_SECONDS,
                remaining,
            )
            note(f"廣告尚未完成，重試中（{attempt + 1}/{_CONFIRM_MAX_ATTEMPTS}）")
            self._interruptible_sleep(_CONFIRM_RETRY_WAIT_SECONDS, check, video_sn)
            self._skip_ad(video_sn)
            self._video_start(video_sn)
        raise GuestAdClearanceError(
            f"{self._label or f'video_sn={video_sn}'} 廣告去除失敗（重試 {_CONFIRM_MAX_ATTEMPTS} 次後放棄）。"
        )

    def _start_ad(self, video_sn: int) -> None:
        self._session.get(_CASTCISHU_URL, params={"sn": video_sn, "s": _AD_SESSION_ID})

    def _skip_ad(self, video_sn: int) -> None:
        self._session.get(
            _CASTCISHU_URL, params={"sn": video_sn, "s": _AD_SESSION_ID, "ad": "end"}
        )

    def _video_start(self, video_sn: int) -> None:
        self._session.get(_VIDEO_START_URL, params={"sn": video_sn})

    # ---- ads_time 設定 / 自動校準 ------------------------------------

    def _ads_time_setting(self) -> int:
        try:
            value = int(self._settings.get(ADS_TIME_SETTING_KEY, DEFAULT_ADS_TIME_SECONDS))
        except (TypeError, ValueError):
            value = DEFAULT_ADS_TIME_SECONDS
        return max(MIN_ADS_TIME_SECONDS, value)

    def _calibrate_ads_time(self, elapsed_seconds: float, video_sn: int) -> None:
        new_value = max(
            MIN_ADS_TIME_SECONDS,
            int(elapsed_seconds) + _ADS_TIME_CALIBRATION_BUFFER_SECONDS,
        )
        current = self._ads_time_setting()
        if new_value <= current:
            return
        # SettingsStore.update() 本來就是「讀最新→合併→寫回」、只動這一個 key，不會蓋掉
        # 下載期間使用者存的其他設定（見 store/settings.py 的設計理由）。
        self._settings.update({ADS_TIME_SETTING_KEY: new_value})
        logger.info(
            "%s 廣告等待時間校準：%d → %d 秒", self._label or f"video_sn={video_sn}", current, new_value
        )

    # ---- 共用 --------------------------------------------------------

    def _interruptible_sleep(
        self,
        seconds: float,
        check: Callable[[], bool],
        video_sn: int,
        *,
        on_tick: Callable[[int], None] | None = None,
    ) -> None:
        whole = int(seconds)
        for i in range(whole):
            self._raise_if_stopped(check, video_sn, "等待廣告時")
            if on_tick is not None:
                on_tick(whole - i)  # 剩餘秒數
            self._sleep(1)
        frac = seconds - whole
        if frac > 0:
            self._sleep(frac)


def _random_hash(length: int = 12) -> str:
    """前端隨機產生的 hash 參數——不是簽章（見 device-id.md），自己產即可。"""
    alphabet = string.ascii_lowercase + string.digits
    return "".join(random.choices(alphabet, k=length))


def _as_dict(response: Any) -> dict:
    try:
        data = response.json()
    except Exception as exc:  # noqa: BLE001
        raise GuestAccessError(f"動畫瘋交握回應不是 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise GuestAccessError(f"動畫瘋交握回應格式不符預期：{data!r}")
    return data
