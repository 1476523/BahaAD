"""登入態保活 ＋ 主動排程檢查。規格見 docs/requirements/scheduler_cookie_warmup.md。

兩段式（.0 改進.txt 第 19 項，參考舊專案 aniGamerPlus）：

1. **保活瀏覽**（`warm_up()`，預設每 30 分鐘）：用現有 cookie 打一次 `ani.gamer.com.tw`
   首頁，讓閒置太久的登入 session 不被動畫瘋判定過期。不打 `token.php`、不開任何視窗，
   純保溫。有下載正在跑就跳過（那份下載自己的請求就夠保溫了，別插隊搶 cookie 輪換）。
   （2026-09-08 拿掉原本先打 `www.gamer.com.tw` 那步——`www` 發回不同的 BAHARUNE，
   讓登入態雜湊在兩個值之間震盪、觸發 code 1007，見 `cookie_rotation._WARMUP_URLS`。）
2. **完整檢查**（`check_once()`，預設每 `cookie_warmup_interval_hours` 小時）：先保活一次，
   再用 `token.php` 查登入態。

**背景檢查一律不開登入視窗**（2026-09-01，使用者回報打包版每次啟動都跳動畫瘋登入視窗、
「這功能不是可以在背景運作？」）：`check_once()` / `login_stale()` 查完只透過
`on_login_checked(valid)` 回報結果，`app_shell` 據此設／清「動畫瘋登入態失效」的網頁
橫幅旗標。重新登入改由使用者看到橫幅後自己按「重新登入」（`GamerLoginCoordinator`），
或下載真的遇到 `login:false` 時處理。開機第一輪也只做靜默 `login_stale()`。

被動觸發（`playlist.py` 遇到 `login:false`）只會發生在「正要下載某一集」的當下。主動
保活能大幅降低「真的要下載時 session 已失效」的機率。
"""

from __future__ import annotations

import logging
import threading
import time

from bahaad.gamer_client.activation import skip_if_busy
from bahaad.gamer_client.cookie_rotation import CookieRotation, CookieRotationError
from bahaad.store.schedule_list import ScheduleListStore
from bahaad.store.settings import SettingsStore
from bahaad.vault import Vault

logger = logging.getLogger(__name__)

_DEFAULT_INTERVAL_HOURS = 6
_WARMUP_INTERVAL_SECONDS = 30 * 60  # 保活瀏覽的週期，比完整檢查頻繁得多


class CookieWarmup:
    def __init__(
        self,
        vault: Vault,
        schedule_store: ScheduleListStore,
        cookie_rotation: CookieRotation,
        settings: SettingsStore,
        registry=None,
        on_login_checked=None,
        on_gamer_activity=None,
        activation_lock=None,
    ) -> None:
        self._vault = vault
        self._schedule_store = schedule_store
        self._cookie_rotation = cookie_rotation
        self._settings = settings
        self._registry = registry
        # 保活／登入態檢查會刻意讓伺服器輪換 BAHARUNE——下載交握進行中就跳過這輪
        # （跟 playlist / guest_access / gossip_watch / completion_watch 共用同一把鎖，
        # 見 gamer_client/activation.py）。`_download_active()` 是更快的前置檢查，兩者並用。
        self._activation_lock = activation_lock
        # 保活瀏覽／登入態檢查都會打 ani.gamer.com.tw 首頁——順便叫 gossip_watcher 查一次
        # 公告（使用者 2026-09-04：只靠 gossip 週期輪詢不夠即時）。gossip 端有節流。
        self._on_gamer_activity = on_gamer_activity
        # 每次查完登入態就回報結果（`True`=有效／`False`=失效）給呼叫端。app_shell 據此
        # 設／清「動畫瘋登入態失效」的網頁橫幅旗標。**背景檢查不再自己開登入視窗**
        # ——那太打擾（使用者 2026-09-01「這功能不是可以在背景運作？」）；重新登入改由
        # 使用者看到橫幅後自己按、或下載真的遇到 login:false 時才觸發。
        self._on_login_checked = on_login_checked
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _download_active(self) -> bool:
        if self._registry is None:
            return False
        try:
            return bool(self._registry.snapshot())
        except Exception:  # noqa: BLE001 - 查不到就當作沒有，保活本來就是盡力而為
            return False

    def warm_up(self) -> bool | None:
        """跑一次「保活瀏覽」。`None` 表示這次跳過（沒帳密／遊客／有下載在跑）；
        `True`／`False` 對應「有沒有真的跑完保溫請求」。"""
        if not self._vault.get_status().get("configured"):
            return None
        if self._download_active():
            return None
        with skip_if_busy(self._activation_lock) as free:
            if not free:
                logger.debug("cookie_warmup：下載交握進行中，這輪跳過（不輪換登入 cookie）")
                return None
            try:
                return self._cookie_rotation.warm_up_session()
            except Exception:  # noqa: BLE001 - 保活失敗不該讓背景執行緒掛掉
                logger.debug("cookie_warmup 保活瀏覽發生例外（不影響主流程）", exc_info=True)
                return False

    def _report_login(self, valid: bool) -> None:
        if self._on_login_checked is not None:
            try:
                self._on_login_checked(valid)
            except Exception:  # noqa: BLE001 - 回報失敗不該讓背景執行緒掛掉
                logger.debug("cookie_warmup on_login_checked 回呼發生例外", exc_info=True)

    def login_stale(self) -> bool:
        """查一次登入態，**不開任何視窗**。回 `True` ＝已失效。查得出結果就一併回報給
        `on_login_checked`。開機第一輪用這個，避免每次開 BahaAD 都立刻跳出動畫瘋登入
        視窗（使用者 2026-09-01 回報）。"""
        if not self._vault.get_status().get("configured"):
            return False
        entries = self._schedule_store.get_entries()
        if not entries:
            return False
        sn = next(iter(entries.values())).sn
        with skip_if_busy(self._activation_lock) as free:
            if not free:
                logger.debug("cookie_warmup 開機檢查：下載交握進行中，這輪不查登入態")
                return False  # 下載交握進行中，這輪不查（開機檢查不急）
            try:
                valid = self._cookie_rotation.is_login_valid(sn)
            except CookieRotationError as exc:
                logger.warning("cookie_warmup 靜默檢查登入態失敗（sn=%s）：%s", sn, exc)
                return False
        self._report_login(valid)
        return not valid

    def check_once(self) -> bool | None:
        """跑一次完整檢查。`None` 表示這次跳過（沒帳密／沒追蹤項目）；`True`／`False`
        對應「登入態有效」／「偵測到失效（或查詢失敗）」。**不開登入視窗**——失效時
        透過 `on_login_checked(False)` 回報，讓網頁介面掛橫幅提醒，重新登入由使用者
        自己按（使用者 2026-09-01）。"""
        if not self._vault.get_status().get("configured"):
            return None

        # 先保活一次——閒置太久的 session 常常還沒真的失效，走一趟瀏覽動線就救回來，
        # 接著的 token.php 就會回 login:true（.0 改進.txt 第 19 項）。就算還沒有任何訂閱
        # 也要保活（保持登入態）。`warm_up()` 內部自己有 `_download_active` 前置檢查 + 搶鎖。
        self.warm_up()

        entries = self._schedule_store.get_entries()
        if not entries:
            return None
        sn = next(iter(entries.values())).sn

        # 查登入態會輪換 BAHARUNE——下載交握進行中就這輪不查（下輪再來）
        with skip_if_busy(self._activation_lock) as free:
            if not free:
                logger.debug("cookie_warmup：下載交握進行中，這輪跳過（不輪換登入 cookie）")
                return None
            try:
                valid = self._cookie_rotation.is_login_valid(sn)
                if not valid:
                    # 別因為一次抽風就掛「登入失效」橫幅（使用者回報「登入沒多久又要登入」）
                    # ——再保活一次、再查一次，兩次都失效才當真（已拿著鎖，直接呼叫）。
                    self._cookie_rotation.warm_up_session()
                    valid = self._cookie_rotation.is_login_valid(sn)
            except CookieRotationError as exc:
                logger.warning("cookie_warmup 查詢登入態失敗（sn=%s）：%s", sn, exc)
                return False

        self._report_login(valid)
        if not valid:
            logger.info("偵測到動畫瘋登入態失效，等使用者重新登入")
        return valid

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
        # `check_once()` 現在完全靜默（不開視窗），開機第一輪直接跑也不會打擾使用者。
        last_full_check = 0.0
        while not self._stop_event.is_set():
            try:
                now = time.monotonic()
                full_interval = (
                    self._settings.get("cookie_warmup_interval_hours", _DEFAULT_INTERVAL_HOURS) * 3600
                )
                if now - last_full_check >= full_interval:
                    last_full_check = now
                    self.check_once()  # check_once() 自己會先 warm_up() 一次
                else:
                    self.warm_up()
                if self._on_gamer_activity is not None:
                    self._on_gamer_activity()  # 剛打過動畫瘋首頁 → 順便查公告
            except Exception:
                logger.exception("cookie_warmup 這一輪發生未預期的例外")
            self._stop_event.wait(_WARMUP_INTERVAL_SECONDS)
