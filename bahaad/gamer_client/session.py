"""gamer_client/ 對動畫瘋發 HTTP 請求的唯一入口。規格見 docs/requirements/gamer_client_session.md。

唯一建立、持有 curl_cffi session 的地方——curl_cffi 的瀏覽器特徵模擬（TLS/HTTP 指紋）由
`impersonate` 參數處理，其他模組不直接 import curl_cffi，一律透過這裡拿到的 GamerSession
發請求，避免各模組各自決定要不要模擬瀏覽器特徵。

環境偽裝（見 docs/requirements/gamer_login_setup.md）：使用者用真實瀏覽器採集的
UA/JA3/Akamai 指紋存在 `IdentityStore`（DPAPI 加密），這裡在建構 curl_cffi session 時
帶進去，取代內建的罐頭指紋。三者都沒設定就退回 `impersonate="chrome"` 的預設行為。
`reload_fingerprint()` 讓指紋採集或設定變更後不必重啟程式就套用新指紋。

Cookie 生命週期：啟動時從 IdentityStore 讀回上次存的 cookie 灌進 session；每次請求後把
session 目前的 cookie 狀態寫回 IdentityStore——跨進程重啟後登入態才能延續。

CSRF 雙重提交 cookie：往 api.gamer.com.tw 發 POST（例如 playlist.py 呼叫的
video_src.php）需要帶 cookie ckBahamutCsrfToken 與請求標頭 X-Bahamut-Csrf-Token，
兩者要是同一組自己隨機產生的值，伺服器只驗證兩者相符，不是伺服器發的機密值
（見 docs/api-observations/playback-and-ads.md 第四輪）。post() 自動處理這件事，
呼叫端不用自己管。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
import threading
from contextlib import AbstractContextManager
from typing import Any

from curl_cffi import requests as curl_requests

from bahaad.net.proxy import send_with_failover
from bahaad.store.identity import IdentityStore

logger = logging.getLogger(__name__)

# 登入態核心 cookie：BAHARUNE 是動畫瘋登入 session（httpOnly），會隨保活／請求輪換；
# 其餘幾個是帳號識別。`auth_cookie_digest()` 只取這幾個算短雜湊，用來比對「device_id
# 是在哪一組登入 cookie 底下核發的」——code 1007（裝置驗證異常）排查用，不洩漏 cookie
# 值本身（使用者 2026-09-06：需要更多除錯資訊）。
_AUTH_COOKIE_NAMES = ("BAHARUNE", "BAHAENUR", "BAHAID", "BAHAALLROUND", "BAHARUNE_M")


def auth_cookie_digest(cookies: dict[str, Any]) -> str:
    """登入態核心 cookie 的 8 碼短雜湊（值本身不進日誌）。兩次結果不同 ＝ 登入 cookie
    在這中間輪換過了。"""
    joined = "|".join(f"{name}={cookies.get(name, '')}" for name in _AUTH_COOKIE_NAMES)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:8]


def session_auth_digest(session: Any) -> str:
    """`auth_cookie_digest(session.current_cookies())`，但對「沒有 current_cookies() 的
    測試假物件」寬容——這只是除錯用的雜湊，絕不能讓它自己把主流程搞掛。"""
    getter = getattr(session, "current_cookies", None)
    if getter is None:
        return "?"
    try:
        return auth_cookie_digest(getter())
    except Exception:  # noqa: BLE001 - 除錯輔助，拿不到就回問號
        return "?"


_REFERER = "https://ani.gamer.com.tw/"
# Akamai CDN（bahamut.akamaized.net 的 playlist / chunklist / segment / key）會擋掉
# 沒有 Origin 標頭的請求（實測：少了 Origin 直接回 403 Access Denied）。動畫瘋自己的
# API 端點帶著也無妨，所以跟 Referer 一樣當預設標頭一律帶上。
_ORIGIN = "https://ani.gamer.com.tw"
_DEFAULT_IMPERSONATE = "chrome"
_CSRF_COOKIE_NAME = "ckBahamutCsrfToken"
_CSRF_HEADER_NAME = "X-Bahamut-Csrf-Token"
_CSRF_COOKIE_DOMAIN = ".gamer.com.tw"


def _with_api_headers(kwargs: dict[str, Any]) -> dict[str, Any]:
    """動畫瘋自己的 API／Akamai CDN 通用的預設標頭（Referer + Origin）。GamerSession 與
    GuestSession 共用，兩者對這幾個端點的請求長相要一致。"""
    headers = kwargs.pop("headers", {})
    headers.setdefault("Referer", _REFERER)
    headers.setdefault("Origin", _ORIGIN)
    kwargs["headers"] = headers
    return kwargs


def _build_session(fingerprint: dict[str, str | None]) -> curl_requests.Session:
    ua = (fingerprint.get("ua") or "").strip()
    ja3 = (fingerprint.get("ja3") or "").strip()
    akamai = (fingerprint.get("akamai") or "").strip()

    kwargs: dict[str, Any] = {
        "impersonate": "firefox" if "firefox" in ua.lower() else _DEFAULT_IMPERSONATE
    }
    # ja3 與 akamai 必須同時有值才套用自訂指紋（只有一個沒有意義、也對不起來）
    if ja3 and akamai:
        kwargs["ja3"] = ja3
        kwargs["akamai"] = akamai
    if ua:
        # curl_cffi 的 default_headers 是 bool（要不要帶內建標頭），自訂 UA 要走
        # session.headers——採集到的 cookie 需要 UA 一致，設在 session 層每個請求都帶到
        kwargs["headers"] = {"User-Agent": ua}
    return curl_requests.Session(**kwargs)


class GamerSession:
    def __init__(
        self,
        identity_store: IdentityStore,
        impersonate: str = _DEFAULT_IMPERSONATE,
        *,
        on_login_lost=None,
        proxy_selector=None,
    ) -> None:
        self._identity_store = identity_store
        self._impersonate_override = impersonate
        # 進階存取（見 bahaad/net/proxy.py）：None＝直連。所有請求走
        # send_with_failover，代理連不上會自動切下一組。
        self._proxy_selector = proxy_selector
        # 收到 `BAHARUNE=deleted`（送出去的登入 cookie 已被站方消耗/輪換掉、救不回來）
        # 時呼叫一次——app_shell 掛「動畫瘋登入態失效」橫幅旗標。詳見 `_persist_cookies`。
        self._on_login_lost = on_login_lost
        self._rebuild_lock = threading.Lock()
        # 這一個 GamerSession 被很多條路共用（網頁瀏覽 browse_http、新集數檢查 catalog、
        # cookie 保活、版本檢查、診斷回報…）。curl_cffi 的 curl handle 是 thread-local
        # 沒問題，但 **cookie jar 是共用的、不是 thread-safe**——多執行緒同時發請求時，
        # `_persist_cookies()` 讀 `dict(self._session.cookies)` 會讀到別條執行緒正在改到
        # 一半的狀態，於是把半套 cookie 寫回 IdentityStore，下一條執行緒又寫回它看到的
        # 另一半，登入態雜湊就會 A→B→A 來回跳（使用者 2026-09-06 回報，也懷疑跟 1007
        # 有關）。用一把鎖把「發請求 + 立刻持久化 cookie」整段序列化。片段下載／圖片
        # 抓取那種高併發的量已經改用各自獨立的 GuestSession（見 app_shell），不會來搶
        # 這把鎖。
        self._request_lock = threading.Lock()
        self._session = self._make_session()
        self._load_cookies()
        # 只在 cookie 真的變動時才寫回 IdentityStore（DPAPI 加密 + SQLite 寫入）。
        # 片段下載會平行打上千個 GET，每個都無條件持久化的話會全部卡在同一把鎖 +
        # 每段一次 DPAPI 系統呼叫，把平行下載卡成序列（round 6 第 15 項）。
        self._persisted_cookies = dict(self._session.cookies)
        # 啟動時記一次登入態雜湊——跟上次關閉前的最後一筆「cookie 輪換」比對，就知道
        # 重啟到底有沒有把登入 cookie 弄丟（使用者 2026-09-06）。
        logger.info(
            "啟動載入動畫瘋 cookie（登入態雜湊 %s，共 %d 個 cookie）",
            auth_cookie_digest(self._persisted_cookies), len(self._persisted_cookies),
        )

    def _make_session(self) -> curl_requests.Session:
        fingerprint = self._identity_store.get_fingerprint()
        if not any(fingerprint.values()):
            # 沒有自訂指紋：維持原本「呼叫端指定 impersonate、預設 chrome」的行為
            return curl_requests.Session(impersonate=self._impersonate_override)
        return _build_session(fingerprint)

    def _load_cookies(self) -> None:
        stored = self._identity_store.get_cookies()
        # 舊版可能把壞掉的 `BAHARUNE=deleted` 寫進 IdentityStore（見 `_persist_cookies`
        # 的防呆說明）。載入時就把它剔掉——帶著 `deleted` 去打站方只會讓事情更糟，
        # 直接當「沒有登入 cookie」處理，讓保活／橫幅走「請重新登入」流程。
        if str(stored.get("BAHARUNE", "")).strip() in ("", "deleted"):
            if stored:
                logger.warning("IdentityStore 的 BAHARUNE 是空的或 deleted——當作未登入載入")
            stored = {k: v for k, v in stored.items() if k != "BAHARUNE"}
        for name, value in stored.items():
            self._session.cookies.set(name, value)

    def _persist_cookies(self, source: str = "") -> None:
        current = dict(self._session.cookies)
        if current == self._persisted_cookies:
            return

        # ---- BAHARUNE=deleted / 消失 的防呆（使用者 2026-09-08「一直失效」的元凶）----
        # 動畫瘋的 BAHARUNE 是「單次使用、每次請求輪換」的登入 session cookie。送出一個
        # 已被消耗掉的舊值 → 站方回 `Set-Cookie: BAHARUNE=deleted`（或直接讓它過期消失）。
        # curl_cffi 會把這個壞值/空缺原封不動放進 jar，舊版 `_persist_cookies` 二話不說
        # 寫回 IdentityStore → **每次重啟都重播這個壞值、登入態永久壞掉**，只能手動重登。
        # aniGamerPlus 明確擋這個（`Anime.py` 檢查 set-cookie 有沒有 `deleted`）。
        #
        # 做法：偵測「本來有真的 BAHARUNE、現在變成 deleted/空/不見了」→ 不寫回、把 jar
        # 的壞值換回上次的好值（阻止後續請求繼續送 deleted 讓站方更火），呼叫
        # `on_login_lost` 掛失效橫幅，等使用者重新登入（`load_cookies()` 會乾淨覆蓋）。
        prev_baharune = self._persisted_cookies.get("BAHARUNE")
        had_real_login = bool(prev_baharune) and prev_baharune != "deleted"
        cur_baharune = current.get("BAHARUNE")
        if had_real_login and cur_baharune in (None, "", "deleted"):
            logger.warning(
                "動畫瘋回 BAHARUNE=%r——不寫回 IdentityStore（避免壞值重播），標記登入態失效"
                "（觸發：%s；執行緒：%s）",
                cur_baharune, source or "?", threading.current_thread().name,
            )
            try:
                self._session.cookies.set("BAHARUNE", prev_baharune)
            except Exception:  # noqa: BLE001
                pass
            if self._on_login_lost is not None:
                try:
                    self._on_login_lost()
                except Exception:  # noqa: BLE001
                    logger.debug("on_login_lost 回呼發生例外", exc_info=True)
            return

        before = auth_cookie_digest(self._persisted_cookies)
        after = auth_cookie_digest(current)
        if before != after:
            # 登入態核心 cookie 真的換了——記下是哪個請求、哪條執行緒觸發的。下載交握
            # （getdeviceid.php → video_src.php）進行中被這個輪換插進來 ＝ code 1007
            # （使用者 2026-09-06：需要更多除錯資訊）。
            logger.info(
                "動畫瘋登入 cookie 輪換 %s→%s（觸發：%s；執行緒：%s）",
                before, after, source or "?", threading.current_thread().name,
            )
        self._identity_store.set_cookies(current)
        self._persisted_cookies = current

    def get(self, url: str, **kwargs: Any) -> curl_requests.Response:
        with self._request_lock:
            try:
                response = send_with_failover(
                    self._session.get, self._proxy_selector, url, **_with_api_headers(kwargs)
                )
            finally:
                # `finally`：就算請求拋例外（連線在收到 Set-Cookie 之後才斷、或回應解析
                # 失敗），只要 curl_cffi 的 jar 已經吸收了站方輪換的新 BAHARUNE，就要立刻
                # 寫回 IdentityStore——不然這次是那顆新值的最後一次觸碰、程式接著關掉／
                # 崩潰，IdentityStore 留的是「已被站方消耗掉的舊值」，下次啟動一送出就被
                # HTTP 299 拒絕（使用者 2026-09-10：不乾淨關閉的 BAHARUNE race）。
                # `_persist_cookies` 內部 `current == persisted` 就 no-op，沒變動時零成本。
                self._persist_cookies(url)
        return response

    def browse_get(self, url: str, *, referer: str | None = None, **kwargs: Any) -> curl_requests.Response:
        """模擬「真人用瀏覽器逛頁面」的 GET——**不帶** `Origin`，`Referer` 由呼叫端指定
        （進入口站時是 `None`＝沒有 referer，進動畫瘋時才帶入口站的 URL）。給
        `cookie_rotation.warm_up_session()` 用：保溫請求要看起來像一次真的瀏覽動線，
        硬塞 `Referer/Origin: ani.gamer.com.tw` 到入口站 `www.gamer.com.tw` 反而不像
        （見 aniGamerPlus `warm_up_cookie_session()` 的做法）。"""
        headers = kwargs.pop("headers", {})
        headers.setdefault(
            "Accept-Language", "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.6"
        )
        if referer:
            headers.setdefault("Referer", referer)
        with self._request_lock:
            try:
                response = send_with_failover(
                    self._session.get, self._proxy_selector, url, headers=headers, **kwargs
                )
            finally:
                self._persist_cookies(f"{url}（保活瀏覽）")  # 見 get() 的 finally 說明
        return response

    def post(self, url: str, **kwargs: Any) -> curl_requests.Response:
        kwargs = _with_api_headers(kwargs)
        csrf_token = secrets.token_hex(16)
        with self._request_lock:
            self._session.cookies.set(_CSRF_COOKIE_NAME, csrf_token, domain=_CSRF_COOKIE_DOMAIN)
            kwargs["headers"].setdefault(_CSRF_HEADER_NAME, csrf_token)
            try:
                response = send_with_failover(self._session.post, self._proxy_selector, url, **kwargs)
            finally:
                self._persist_cookies(url)  # 見 get() 的 finally 說明
        return response

    def current_cookies(self) -> dict[str, str]:
        return dict(self._session.cookies)

    def clear_cookies(self) -> None:
        with self._request_lock:
            self._session.cookies.clear()
            self._identity_store.set_cookies({})
            self._persisted_cookies = {}

    def load_cookies(self, cookies: dict[str, str]) -> None:
        """把外部拿到的一組全新 cookie（例如 cookie_rotation.py 從瀏覽器登入流程
        取得的）灌進目前這個 session，取代舊的，並立刻持久化。跟 __init__ 時的
        _load_cookies() 不同：那是唯讀啟動載入，這裡是登入態更新後的主動覆寫。"""
        with self._request_lock:
            self._session.cookies.clear()
            for name, value in cookies.items():
                self._session.cookies.set(name, value)
            self._persist_cookies("重新登入動畫瘋")

    def reload_fingerprint(self) -> None:
        """重新從 IdentityStore 讀環境偽裝指紋、重建底層 curl_cffi session，把目前的
        cookie 一起搬過去。指紋採集或設定變更後呼叫，不必重啟程式。

        重建期間若有其他執行緒正在用舊 session 發請求，該請求會用舊 session 完成
        （屬性指派在 GIL 下是原子的，in-flight 請求握的是舊物件）——可接受，見
        docs/requirements/gamer_login_setup.md「已知風險」。"""
        with self._rebuild_lock, self._request_lock:
            carried_cookies = dict(self._session.cookies)
            new_session = self._make_session()
            for name, value in carried_cookies.items():
                new_session.cookies.set(name, value)
            self._session = new_session


class GuestSession:
    """訪客身分（未登入動畫瘋）對 ani/api.gamer.com.tw 發請求用——遊客／非 VIP 看廣告
    下載流程（`gamer_client/guest_access.py`）在「BahaAD 沒有設定動畫瘋登入」時用這個。

    跟 GamerSession 的差別：**完全不碰 IdentityStore 的 cookie**——啟動不載入登入 cookie、
    每次請求後也不寫回。訪客交握自己會拿到 `nologinuser` / `ckBahamutCsrfToken` 等 cookie，
    留在這個 session 內部跨請求重用，程式重啟後丟掉。這樣訪客身分不會污染「應該只放登入
    cookie」的 IdentityStore，也不會干擾 `cookie_rotation.is_login_valid()` / `cookie_warmup`。

    環境偽裝指紋（ua/ja3/akamai）仍沿用 IdentityStore 採集到的那組——同一台機器不管登入
    與否，瀏覽器特徵本來就是同一套，這裡共用是合理的。

    見 docs/requirements/guest_download.md「session 選擇」、docs/requirements/
    gamer_client_session.md「單一 session」規則的第二個刻意例外（第一個是 youranimes 的
    第三方無 cookie 抓取器，理由不同）。
    """

    def __init__(
        self,
        identity_store: IdentityStore,
        impersonate: str = _DEFAULT_IMPERSONATE,
        *,
        proxy_selector=None,
        serialize_requests: bool = True,
    ) -> None:
        self._identity_store = identity_store
        self._impersonate_override = impersonate
        self._proxy_selector = proxy_selector
        self._rebuild_lock = threading.Lock()
        # 同 GamerSession：curl_cffi 的 cookie jar 不是 thread-safe，一個 GuestSession 被
        # 多條背景執行緒共用（gossip 掃描＋完結偵測共用 checker_session）時要序列化。
        # **例外**：片段下載用的 `cdn_session` 傳 `serialize_requests=False`——它打的是
        # 無 cookie 的已簽章 CDN 網址，沒有 cookie jar 競態問題，而且 `max_concurrent_segments`
        # 條執行緒平行抓片段就是靠這個 session 併發，加鎖會退化成單執行緒（使用者 2026-09-09
        # 「最大下載線程數失效、全部單執行緒下載」）。
        self._request_lock: "AbstractContextManager[Any]" = (
            threading.Lock() if serialize_requests else contextlib.nullcontext()
        )
        self._session = self._make_session()

    def _make_session(self) -> curl_requests.Session:
        fingerprint = self._identity_store.get_fingerprint()
        if not any(fingerprint.values()):
            return curl_requests.Session(impersonate=self._impersonate_override)
        return _build_session(fingerprint)

    def get(self, url: str, **kwargs: Any) -> curl_requests.Response:
        with self._request_lock:
            return send_with_failover(
                self._session.get, self._proxy_selector, url, **_with_api_headers(kwargs)
            )

    def post(self, url: str, **kwargs: Any) -> curl_requests.Response:
        kwargs = _with_api_headers(kwargs)
        csrf_token = secrets.token_hex(16)
        with self._request_lock:
            self._session.cookies.set(_CSRF_COOKIE_NAME, csrf_token, domain=_CSRF_COOKIE_DOMAIN)
            kwargs["headers"].setdefault(_CSRF_HEADER_NAME, csrf_token)
            return send_with_failover(self._session.post, self._proxy_selector, url, **kwargs)

    def current_cookies(self) -> dict[str, str]:
        return dict(self._session.cookies)

    def reload_fingerprint(self) -> None:
        """指紋採集／設定變更後呼叫，重建底層 session、把目前的訪客 cookie 搬過去。
        比照 GamerSession.reload_fingerprint() 的 in-flight 請求風險（可接受）。"""
        with self._rebuild_lock, self._request_lock:
            carried_cookies = dict(self._session.cookies)
            new_session = self._make_session()
            for name, value in carried_cookies.items():
                new_session.cookies.set(name, value)
            self._session = new_session
