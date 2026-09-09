"""動畫瘋登入／環境偽裝的背景任務協調器。規格見 docs/requirements/gamer_login_setup.md。

`cookie_rotation.refresh_login()` 會阻塞到使用者完成 CAPTCHA 或逾時（最長約 180 秒），
`fingerprint_fetch.fetch()` 也要等瀏覽器起來、頁面量測完——這兩件事都不能卡在 Flask
請求處理裡。這裡把它們包成「同時只能有一個」的背景 job，web 層開一個 job 之後導到
進度頁，用 status() 輪詢結果。

比照舊專案 aniGamerPlus `Config.run_quick_fetch()` / `quick_fetch_state` 的做法（使用者
已同意「aniGamerPlus 登入設定」「環境偽裝設定」兩項可以參考舊專案，見
docs/custom-features.md 第 3、9 項）。
"""

from __future__ import annotations

import logging
import threading

from bahaad.gamer_client.cookie_rotation import CookieRotation, CookieRotationError
from bahaad.gamer_client.fingerprint_fetch import FingerprintFetcher, FingerprintFetchError
from bahaad.gamer_client.session import GamerSession
from bahaad.store.identity import IdentityStore
from bahaad.vault import Vault

logger = logging.getLogger(__name__)

_STATE_IDLE = "idle"
_STATE_RUNNING = "running"
_STATE_DONE = "done"
_STATE_ERROR = "error"

_KIND_FINGERPRINT = "fingerprint"
_KIND_LOGIN = "login"


class GamerLoginCoordinatorError(Exception):
    pass


class GamerLoginCoordinator:
    def __init__(
        self,
        vault: Vault,
        session: GamerSession,
        identity_store: IdentityStore,
        fingerprint_fetcher: FingerprintFetcher,
        cookie_rotation: CookieRotation,
        on_login_success=None,
    ) -> None:
        self._vault = vault
        self._session = session
        self._identity_store = identity_store
        self._fingerprint_fetcher = fingerprint_fetcher
        self._cookie_rotation = cookie_rotation
        # 登入成功時回報一次——app_shell 據此清掉「動畫瘋登入態失效」的網頁橫幅旗標
        self._on_login_success = on_login_success

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._state = _STATE_IDLE
        self._kind: str | None = None
        self._message = ""

    # ---- 對外 ---------------------------------------------------------

    def status(self) -> dict[str, str | None]:
        with self._lock:
            return {"state": self._state, "kind": self._kind, "message": self._message}

    def start_fingerprint_fetch(self) -> bool:
        """開始在背景採集 UA/JA3/Akamai 指紋。已經有 job 在跑就回 False、不重複起。"""
        return self._start(_KIND_FINGERPRINT, self._run_fingerprint)

    def start_login(self) -> bool:
        """開始動畫瘋登入流程（開瀏覽器、代填、輪詢）。vault 沒設定帳密會丟
        GamerLoginCoordinatorError。已經有 job 在跑就回 False。"""
        if not self._vault.get_status()["configured"]:
            raise GamerLoginCoordinatorError("尚未設定動畫瘋帳號密碼")
        return self._start(_KIND_LOGIN, self._run_login)

    def clear_fingerprint(self) -> None:
        """清掉環境偽裝指紋、退回 curl_cffi 內建罐頭指紋，並即時重建 session。"""
        self._identity_store.clear_fingerprint()
        self._identity_store.invalidate_device_id()
        self._session.reload_fingerprint()

    def logout(self) -> None:
        """刪除動畫瘋帳密與 Cookie（登入資訊全清），並即時清掉 session 的 cookie。"""
        self._vault.delete_credentials()
        self._session.clear_cookies()
        self._identity_store.invalidate_device_id()

    # ---- 內部 ---------------------------------------------------------

    def _start(self, kind: str, target) -> bool:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._state = _STATE_RUNNING
            self._kind = kind
            self._message = ""
            thread = threading.Thread(target=self._guarded(target), name=f"gamer-{kind}", daemon=True)
            self._thread = thread
        thread.start()
        return True

    def _guarded(self, target):
        def _wrapper() -> None:
            try:
                target()
            except Exception:  # noqa: BLE001 - 背景執行緒不能讓例外逃逸
                logger.exception("gamer login coordinator job（%s）發生未預期例外", self._kind)
                self._finish(_STATE_ERROR, "發生未預期的錯誤，請稍後再試")

        return _wrapper

    def _finish(self, state: str, message: str) -> None:
        with self._lock:
            self._state = state
            self._message = message

    def _run_fingerprint(self) -> None:
        try:
            fingerprint = self._fingerprint_fetcher.fetch()
        except FingerprintFetchError as exc:
            self._finish(_STATE_ERROR, f"取得環境偽裝指紋失敗：{exc}")
            return
        except Exception as exc:  # noqa: BLE001 - 例如找不到瀏覽器執行檔
            self._finish(_STATE_ERROR, f"無法啟動瀏覽器採集指紋：{exc}")
            return

        self._identity_store.set_fingerprint(
            fingerprint["ua"], fingerprint["ja3"], fingerprint["akamai"]
        )
        # 指紋換過，舊 device_id 在另一組指紋下核發、沿用會對不上，清掉讓下次重新申請
        self._identity_store.invalidate_device_id()
        self._session.reload_fingerprint()
        self._finish(_STATE_DONE, "已更新環境偽裝指紋")

    def _run_login(self) -> None:
        try:
            ok = self._cookie_rotation.refresh_login()
        except CookieRotationError as exc:
            self._finish(_STATE_ERROR, f"登入流程失敗：{exc}")
            return

        if not ok:
            self._finish(_STATE_ERROR, "登入未完成（可能是取消或逾時），請再試一次")
            return

        # 登入成功、cookie_rotation 已把新 cookie 灌進 session，舊 device_id 一併作廢
        self._identity_store.invalidate_device_id()
        if self._on_login_success is not None:
            try:
                self._on_login_success()
            except Exception:  # noqa: BLE001
                logger.debug("on_login_success 回呼發生例外", exc_info=True)
        self._finish(_STATE_DONE, "已完成動畫瘋登入")
