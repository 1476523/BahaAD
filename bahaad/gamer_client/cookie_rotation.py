"""登入態偵測與內建登入流程。規格見 docs/requirements/gamer_client_cookie_rotation.md。

## 設計參考（使用者已同意這支模組可以參考舊專案，不是照抄程式碼）

舊專案 `aniGamerPlus` 的 `Dashboard/quick_fetch.py` 用 Chrome DevTools Protocol
（CDP）操作一個獨立、全新暫存設定檔的瀏覽器視窗完成同樣的事，這裡借用「怎麼做」的
思路重新實作：

1. **自動代填不等於自動登入**：只代填帳號／密碼／TOTP 欄位，**CAPTCHA 與最終送出
   登入全程由使用者自己在彈出的瀏覽器視窗裡完成**，程式不嘗試繞過或自動解決 CAPTCHA。
   這是刻意的設計界線
2. **獨立瀏覽器視窗，全新暫存設定檔**（`--incognito` + 全新 `--user-data-dir`），
   不重用使用者平常的瀏覽紀錄
3. **只偵測「導向到哪裡」，不涉入驗證過程本身**：輪詢 `location.href` 判斷使用者
   是否已經自己完成驗證並登入成功，成功後才透過 CDP 讀取 cookie

## 已知風險（需求規格文件裡就寫明的，不是實作時才發現）

`_ACCOUNT_FIELD_SELECTOR`／`_PASSWORD_FIELD_SELECTOR`／`_TWO_STEP_INPUT_SELECTOR` 這幾個
選擇器沒有真實帳號沒辦法事先驗證登入頁面目前的真實 DOM 結構是否吻合——**第一次真的用
使用者帳號跑 `refresh_login()` 時要親自確認，選擇器隨時可能要調整**。自動代填全程包在
`try/except` 裡，代填失敗不會擋住使用者自己手動完成整個登入。
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Protocol

import websocket

from bahaad.gamer_client.device import DeviceIdManager
from bahaad.gamer_client.session import GamerSession

logger = logging.getLogger(__name__)
from bahaad.vault import NotConfiguredError, Vault, generate_totp

_LOGIN_URL = "https://user.gamer.com.tw/login.php"
_COOKIE_TARGET_URL = "https://ani.gamer.com.tw/"
_COOKIE_TARGET_HOST_MARKER = "ani.gamer.com.tw"
_LOGIN_SUCCESS_URL_PREFIX = "https://www.gamer.com.tw/"
_ANI_HOME_URL = "https://ani.gamer.com.tw/"

# 保溫請求：閒置太久時登入 session（BAHARUNE）會被動畫瘋判定過期，定期發請求維持存活。
#
# **只打 `ani.gamer.com.tw`**（2026-09-08，使用者回報 code 1007 又復發）：原本照
# aniGamerPlus 也先打 `www.gamer.com.tw` 入口站再進動畫瘋，模擬真人瀏覽動線。但
# aniGamerPlus 用的是**丟棄式 session**、只在 `should_refresh_cookie()` 說要時才寫回；
# BahaAD 是共用的 `GamerSession` + 每次 cookie 變動立刻持久化。`www` 和 `ani` 各自
# 發回**不同的 BAHARUNE**，共用 jar 就在兩個值之間來回，`auth_cookie_digest`（登入態
# 雜湊）跟著 A⇄B 震盪 → `device.py` 每 30 分鐘判定「登入態變了」重新申請 device_id
# → VIP 下載沒跑啟用交握 → code 1007。BahaAD 本來就一直在打 `ani`（下載、
# `is_login_valid`、gossip poke），`ani` 的 session 不會閒到過期，不需要 `www` 那一站。
_WARMUP_URLS = ("https://ani.gamer.com.tw/",)

_ACCOUNT_FIELD_SELECTOR = 'input[name="userid"]'
_PASSWORD_FIELD_SELECTOR = 'input[name="password"]'
_TWO_STEP_INPUT_SELECTOR = "#input-2sa"

_PAGE_LOAD_TIMEOUT_SECONDS = 15
_TWO_STEP_WAIT_TIMEOUT_SECONDS = 15
_NAVIGATE_WAIT_TIMEOUT_SECONDS = 10
_POLL_INTERVAL_SECONDS = 0.5
_DEFAULT_LOGIN_TIMEOUT_SECONDS = 180
_DEVTOOLS_READY_TIMEOUT_SECONDS = 15


class CookieRotationError(Exception):
    pass


class BrowserNotFoundError(CookieRotationError):
    pass


def find_browser() -> str:
    """依序找 Chrome／Edge 實際安裝路徑（Windows 登錄檔 App Paths，不依賴固定安裝
    路徑），兩者都是 Chromium 核心、CDP 操作方式完全相同，Chrome 優先。"""
    import winreg

    for app_name in ("chrome.exe", "msedge.exe"):
        key_path = rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{app_name}"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    value, _ = winreg.QueryValueEx(key, None)
                    if value and Path(value).exists():
                        return value
            except OSError:
                continue
    raise BrowserNotFoundError("找不到 Chrome 或 Edge，請確認已安裝")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def bring_pid_to_front(pid: int, *, attempts: int = 12, interval: float = 0.4) -> None:
    """把某個行程的頂層視窗拉到所有視窗最前面。純 Windows API——CDP 的
    `Page.bringToFront` 只把分頁 activate 在 Chrome 內部、不保證整個瀏覽器視窗浮到其他
    程式（例如使用者的主瀏覽器）上面（使用者 2026-09-01：登入視窗還是開在後面）。

    視窗要一小段時間才建立，所以輪詢重試。`SetForegroundWindow` 有前景鎖，先把視窗
    設成 TOPMOST 再取消，是繞過它最省事、不用 `AttachThreadInput` 的老招。全程 best
    effort——失敗（非 Windows、找不到視窗、API 被擋）就算了，不影響使用者手動把視窗點
    到前面。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
    except (ImportError, AttributeError, OSError):
        return

    GW_OWNER = 4
    SW_RESTORE = 9
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    HWND_TOPMOST = wintypes.HWND(-1)
    HWND_NOTOPMOST = wintypes.HWND(-2)

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _find_hwnd() -> int | None:
        found: list[int] = []

        @enum_proc
        def _cb(hwnd: int, _lparam: int) -> bool:
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if (
                wpid.value == pid
                and user32.IsWindowVisible(hwnd)
                and user32.GetWindow(hwnd, GW_OWNER) == 0  # 頂層、不是對話框
                and user32.GetWindowTextLengthW(hwnd) > 0
            ):
                found.append(hwnd)
                return False
            return True

        try:
            user32.EnumWindows(_cb, 0)
        except Exception:  # noqa: BLE001
            return None
        return found[0] if found else None

    for _ in range(max(1, attempts)):
        hwnd = _find_hwnd()
        if hwnd:
            try:
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)
                user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE)
                user32.SetForegroundWindow(hwnd)
            except Exception:  # noqa: BLE001
                pass
            return
        time.sleep(interval)


class CdpLike(Protocol):
    def evaluate(self, expression: str, timeout: float = 10) -> Any: ...
    def get_cookies(self) -> dict[str, str]: ...
    def bring_to_front(self) -> None: ...
    def close(self) -> None: ...


class ProcessLike(Protocol):
    def poll(self) -> int | None: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


class _CdpConnection:
    """輕量的 CDP JSON-RPC 客戶端，只做這個模組需要的事：送指令、等對應 id 的回應。"""

    def __init__(self, ws_url: str, connect_timeout: float = 10) -> None:
        self._ws = websocket.create_connection(ws_url, timeout=connect_timeout)
        self._next_id = 0

    def _call(self, method: str, params: dict | None = None, timeout: float = 10) -> dict | None:
        self._next_id += 1
        msg_id = self._next_id
        self._ws.send(json.dumps({"id": msg_id, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._ws.settimeout(max(0.1, deadline - time.time()))
            try:
                raw = self._ws.recv()
            except Exception:
                break
            data = json.loads(raw)
            if data.get("id") == msg_id:
                return data
        return None

    def evaluate(self, expression: str, timeout: float = 10) -> Any:
        resp = self._call("Runtime.evaluate", {"expression": expression, "returnByValue": True}, timeout=timeout)
        return (resp or {}).get("result", {}).get("result", {}).get("value")

    def get_cookies(self) -> dict[str, str]:
        # `Network.getAllCookies` 回傳整個瀏覽器的所有 cookie、不限當前分頁網址——比
        # `Network.getCookies({})`（部分 Chrome 版本會只回當前頁面 scope）明確。實測
        # （2026-09-01 使用者真帳號）動畫瘋登入 cookie（`BAHARUNE`／`BAHAENUR`／`BAHAID`
        # …，`BAHARUNE` 是 httpOnly，JS 讀不到）全都在 `.gamer.com.tw`，`refresh_login()`
        # 導回 ani.gamer.com.tw 後三種方法抓到的是同一組，但用最不受頁面 scope 影響的。
        # `getAllCookies` 不支援就退 `Storage.getCookies` 再退 `Network.getCookies`。
        for method in ("Network.getAllCookies", "Storage.getCookies", "Network.getCookies"):
            try:
                resp = self._call(method, {})
            except Exception:  # noqa: BLE001 - 這個 CDP 方法不支援就換下一個
                continue
            raw_cookies = (resp or {}).get("result", {}).get("cookies")
            if raw_cookies:
                return {c["name"]: c["value"] for c in raw_cookies}
        return {}

    def bring_to_front(self) -> None:
        """把登入視窗拉到最前面——使用者要在這個視窗裡解 CAPTCHA、按登入，
        開在 BahaAD 分頁後面會找不到（使用者 2026-08-31 回饋）。失敗就算了。"""
        try:
            self._call("Page.bringToFront", timeout=3)
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:  # noqa: BLE001 - 收尾動作，不能讓關閉本身的例外蓋過真正的錯誤
            pass


def _wait_for_devtools(port: int, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                json.load(r)
                return True
        except Exception:  # noqa: BLE001 - 輪詢中的暫時性失敗，繼續重試
            time.sleep(0.3)
    return False


def _get_page_target(port: int) -> dict | None:
    # 不接受還停在 about:blank 的分頁——防禦性寫法，不是實測到的必要修正（實測發現
    # evaluate() 太早呼叫時 document.title／cookie 會是空的，原因單純是頁面 JS
    # 還沒執行完，不是接到錯誤分頁；用 refresh_login() 本來就有的輪詢等待正常會自己
    # 等到，這裡多加一層過濾只是避免真的撞上瀏覽器剛啟動、還沒開始導航那極短暫的空檔）
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as r:
        targets = json.load(r)
    for target in targets:
        if target.get("type") == "page" and target.get("url") not in ("", "about:blank"):
            return target
    return None


def _default_connect_cdp(port: int, timeout: float) -> _CdpConnection:
    if not _wait_for_devtools(port, timeout):
        raise CookieRotationError("瀏覽器沒有在時間內啟動 DevTools")
    target = None
    deadline = time.time() + timeout
    while time.time() < deadline and target is None:
        target = _get_page_target(port)
        if not target:
            time.sleep(0.3)
    if not target:
        raise CookieRotationError("找不到瀏覽器分頁")
    return _CdpConnection(target["webSocketDebuggerUrl"])


def _terminate(process: ProcessLike) -> None:
    try:
        process.terminate()
        process.wait(timeout=10)
    except Exception:  # noqa: BLE001 - 收尾動作，terminate 失敗就直接 kill
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass


def _fill_field_js(selector: str, value: str) -> str:
    return (
        "(function(sel, val){"
        "var el = document.querySelector(sel);"
        "if (!el) return false;"
        "el.focus();"
        "var proto = Object.getPrototypeOf(el);"
        "var setter = Object.getOwnPropertyDescriptor(proto, 'value').set;"
        "setter.call(el, val);"
        "el.dispatchEvent(new Event('input', {bubbles: true}));"
        "el.dispatchEvent(new Event('change', {bubbles: true}));"
        "el.blur();"
        "return true;"
        f"}})({json.dumps(selector)}, {json.dumps(value)});"
    )


def _click_submit_js(account_selector: str) -> str:
    return (
        "(function(sel){"
        "var field = document.querySelector(sel);"
        "if (!field) return false;"
        "var form = field.closest('form');"
        "if (!form) return false;"
        "var btn = form.querySelector('button[type=\"submit\"], input[type=\"submit\"]');"
        "if (!btn) return false;"
        "btn.click();"
        "return true;"
        f"}})({json.dumps(account_selector)});"
    )


def _exists_js(selector: str) -> str:
    return f"!!document.querySelector({json.dumps(selector)})"


def _visible_js(selector: str) -> str:
    return (
        "(function(sel){var el=document.querySelector(sel);"
        "return !!(el && el.offsetParent !== null);})"
        f"({json.dumps(selector)})"
    )


class CookieRotation:
    def __init__(
        self,
        session: GamerSession,
        vault: Vault,
        device_manager: DeviceIdManager,
        browser_path: str | None = None,
        login_timeout: float = _DEFAULT_LOGIN_TIMEOUT_SECONDS,
        launch_browser: Callable[[list[str]], ProcessLike] = subprocess.Popen,
        connect_cdp: Callable[[int, float], CdpLike] = _default_connect_cdp,
    ) -> None:
        self._session = session
        self._vault = vault
        self._device_manager = device_manager
        self._browser_path = browser_path
        self._login_timeout = login_timeout
        self._launch_browser = launch_browser
        self._connect_cdp = connect_cdp

    def warm_up_session(self) -> bool:
        """發一次 `ani.gamer.com.tw` 首頁請求，讓閒置太久的登入 session 保持存活。動畫瘋
        長時間收不到任何請求時會把 BAHARUNE 判定成過期（不是被別的裝置搶走，單純閒置
        太久），之後只能重新登入——定期發一次，BAHARUNE 會隨這個請求輪換，`GamerSession`
        會自動吸收＋持久化。

        `browse_get()`（**不帶** `Origin`、帶 `Accept-Language`）——看起來像一次一般瀏覽。
        **只打 `ani.gamer.com.tw`、不再先打 `www.gamer.com.tw`**（見 `_WARMUP_URLS` 上方
        說明：`www` 那站會發回不同的 BAHARUNE，讓登入態雜湊在兩個值之間震盪、觸發
        code 1007）。

        只有已登入（cookie 有 `BAHAID`）才有意義——遊客沒有 BAHARUNE 可以保溫。回傳有沒有
        真的跑完保溫請求；任何網路例外都吞掉（這只是保溫、不是關鍵路徑，不該讓背景執行緒
        掛掉）。"""
        if "BAHAID" not in self._session.current_cookies():
            return False
        try:
            prev: str | None = None
            for url in _WARMUP_URLS:
                self._session.browse_get(url, referer=prev, timeout=15)
                prev = url
        except Exception:  # noqa: BLE001 - 保溫失敗不影響任何主流程
            return False
        return True

    def is_login_valid(self, video_sn: int) -> bool:
        """目前 cookie 是否還代表已登入。

        **不再用 `token.php` 的 `login` 欄位**（2026-09-01，使用者拿真帳號實測抓出來）：
        `token.php` 需要一個「已啟用」的 device id，`GamerLoginCoordinator` 登入成功後
        會 `invalidate_device_id()`，新申請的 device id 還沒經過啟用交握時 `token.php`
        直接回 `{"error":{"code":1007,"message":"裝置驗證異常！"}}` → `login` 讀不到 →
        誤判成登入失效（就是「登入沒多久又要登入」的成因）。實測：帶剛登入抓到的
        cookie 打動畫瘋首頁，明明是已登入狀態。

        改成抓一次動畫瘋首頁看有沒有「登出」連結（`user.gamer.com.tw/logout.php`）
        ——不需要 device id、就是一般的頁面請求。`video_sn` 參數保留（呼叫慣例），沒用到。
        判斷不出來（頁面改版／被 CDN 擋／空回應）一律**保守當有效**，寧可漏報也不亂掛
        「登入失效」橫幅。"""
        try:
            html = self._session.get(_ANI_HOME_URL, timeout=15).text
        except Exception as exc:  # noqa: BLE001 - 連線問題不代表登入失效
            raise CookieRotationError(f"查登入態時連線失敗: {exc}") from exc
        if "user.gamer.com.tw/logout.php" in html:
            return True
        # 沒有登出連結、但有明確的登入按鈕 → 遊客頁 ＝ 真的沒登入
        if "user.gamer.com.tw/login.php" in html:
            return False
        return True

    def refresh_login(self) -> bool:
        """觸發瀏覽器代填登入流程，阻塞直到使用者完成、逾時、或取消。成功的話
        把新 cookie 灌進 session 並存回 IdentityStore，回傳是否成功。"""
        try:
            credentials = self._vault.unlock_credentials()
        except NotConfiguredError as exc:
            raise CookieRotationError("vault 裡沒有存動畫瘋帳密，無法自動登入") from exc

        # 這是唯一會開瀏覽器登入視窗的地方。只該從 GamerLoginCoordinator（web「重新登入」
        # / 「登入動畫瘋」按鈕）呼叫——**沒有任何背景排程會呼叫它**（cookie_warmup 只查、
        # 不開視窗，2026-09-01）。記一筆 INFO，日後若視窗意外自己跳出來可據此追來源。
        logger.info("使用者執行「動畫瘋登入」程序並開啟登入視窗")
        browser_path = self._browser_path or find_browser()
        port = _find_free_port()
        user_data_dir = tempfile.mkdtemp(prefix="bahaad_cookie_rotation_")

        process = self._launch_browser(
            [
                browser_path,
                f"--remote-debugging-port={port}",
                # 新版 Chrome/Edge 預設拒絕來自任意 origin 的 CDP WebSocket 連線
                # （實測遇到的錯誤：HTTP 403 "Rejected an incoming WebSocket connection"），
                # 一定要明確允許本地這個埠才能連線，不是理論上的安全建議，是真的會擋
                f"--remote-allow-origins=http://127.0.0.1:{port}",
                f"--user-data-dir={user_data_dir}",
                "--no-first-run",
                "--new-window",
                "--incognito",
                _LOGIN_URL,
            ]
        )

        cdp: CdpLike | None = None
        try:
            cdp = self._connect_cdp(port, _DEVTOOLS_READY_TIMEOUT_SECONDS)
            if hasattr(cdp, "bring_to_front"):
                cdp.bring_to_front()
            # CDP 的 Page.bringToFront 只 activate 分頁，不保證整個瀏覽器視窗浮到使用者
            # 的主瀏覽器上面——再補一個 Windows 原生的前景拉抬（使用者 2026-09-01）
            pid = getattr(process, "pid", None)
            if isinstance(pid, int):
                bring_pid_to_front(pid, attempts=5, interval=0.3)
            self._autofill(cdp, credentials, process)

            if not self._wait_for_login_success(cdp, process):
                return False

            cdp.evaluate(f"location.href = {json.dumps(_COOKIE_TARGET_URL)}")
            self._wait_for(
                cdp,
                process,
                f'location.href.indexOf({json.dumps(_COOKIE_TARGET_HOST_MARKER)}) !== -1 '
                '&& document.readyState === "complete"',
                _NAVIGATE_WAIT_TIMEOUT_SECONDS,
            )

            new_cookies = cdp.get_cookies()
            if not new_cookies:
                raise CookieRotationError("沒有取得任何 cookie，登入流程可能沒有真的成功")

            self._session.load_cookies(new_cookies)
            return True
        finally:
            if cdp is not None:
                cdp.close()
            _terminate(process)
            shutil.rmtree(user_data_dir, ignore_errors=True)

    def _autofill(self, cdp: CdpLike, credentials: dict[str, str | None], process: ProcessLike) -> None:
        # 自動代填只是輔助：整段包在 try/except 裡，失敗不能擋住使用者自己手動完成
        # 整個登入（見模組頂端「已知風險」）
        try:
            if not self._wait_for(cdp, process, _exists_js(_ACCOUNT_FIELD_SELECTOR), _PAGE_LOAD_TIMEOUT_SECONDS):
                return

            cdp.evaluate(_fill_field_js(_ACCOUNT_FIELD_SELECTOR, credentials["account"]))
            cdp.evaluate(_fill_field_js(_PASSWORD_FIELD_SELECTOR, credentials["password"]))
            cdp.evaluate(_click_submit_js(_ACCOUNT_FIELD_SELECTOR))

            totp_secret = credentials.get("totp_secret")
            if not totp_secret:
                return
            if self._wait_for(
                cdp, process, _visible_js(_TWO_STEP_INPUT_SELECTOR), _TWO_STEP_WAIT_TIMEOUT_SECONDS
            ):
                code = generate_totp(totp_secret)
                cdp.evaluate(_fill_field_js(_TWO_STEP_INPUT_SELECTOR, code))
        except Exception:  # noqa: BLE001 - 代填失敗不影響使用者手動登入這條路
            pass

    def _wait_for_login_success(self, cdp: CdpLike, process: ProcessLike) -> bool:
        deadline = time.time() + self._login_timeout
        while time.time() < deadline:
            if process.poll() is not None:
                return False  # 使用者關掉瀏覽器視窗，視同取消
            href = cdp.evaluate("location.href", timeout=3)
            if isinstance(href, str) and href.startswith(_LOGIN_SUCCESS_URL_PREFIX):
                return True
            time.sleep(_POLL_INTERVAL_SECONDS)
        return False

    def _wait_for(self, cdp: CdpLike, process: ProcessLike, js_expr: str, timeout: float) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if process.poll() is not None:
                return False
            if cdp.evaluate(js_expr, timeout=3):
                return True
            time.sleep(_POLL_INTERVAL_SECONDS)
        return False
