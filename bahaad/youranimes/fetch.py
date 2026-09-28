"""抓 youranimes.tw 季度頁／個別番劇頁的 HTTP client——**專用、無 cookie、無登入**。

這是 `docs/requirements/gamer_client_session.md`「只有 `GamerSession` 建立／持有
curl_cffi session」規則的**唯一刻意例外**：

- `GamerSession.get()` 會帶動畫瘋的 `Referer: https://ani.gamer.com.tw/`、`Origin`，
  以及使用者的登入 cookie（`BAHAID` 等）。把這些送給第三方站 youranimes.tw 是隱私外洩。
- 所以這裡自己開一個乾淨的 curl_cffi session，不碰 `IdentityStore`、不帶任何 cookie、
  不做 cookie 持久化。只送出 youranimes 需要的最小標頭。

youranimes 在 Next.js / edge 後面，stdlib `urllib` 的 TLS 指紋常被擋，所以還是用
curl_cffi 的 `impersonate`（Nuitka 已打包）。見 docs/requirements/youranimes.md。
"""

from __future__ import annotations

import time

from curl_cffi import requests as curl_requests

from bahaad.net.proxy import AllProxiesUnavailable, send_with_failover

_BASE_URL = "https://youranimes.tw"
_TIMEOUT_SECONDS = 30
_IMPERSONATE = "chrome"
# 偶爾會撞到暫時性的連線／TLS 錯誤（使用者 2026-09-09：youranimes.tw 有時回
# WRONG_VERSION_NUMBER，重試就好，非固定故障）——連線類例外重試一次再放棄。
_RETRY_ON_ERROR = 1
_RETRY_DELAY_SECONDS = 2.0


class YourAnimesError(Exception):
    """抓取 youranimes 失敗（連線錯誤 / 非 200）。"""


class YourAnimesFetcher:
    def __init__(self, session=None, *, proxy_selector=None) -> None:
        # 乾淨 session：不帶 cookie jar、不帶動畫瘋標頭。測試會注入 fake。
        self._session = session or curl_requests.Session(impersonate=_IMPERSONATE)
        self._proxy_selector = proxy_selector

    def _get(self, url: str):
        last_exc: Exception | None = None
        for attempt in range(_RETRY_ON_ERROR + 1):
            try:
                return send_with_failover(
                    self._session.get, self._proxy_selector, url, timeout=_TIMEOUT_SECONDS
                )
            except AllProxiesUnavailable:
                raise  # 代理全掛了重試也沒用
            except Exception as exc:  # noqa: BLE001 - 連線／TLS 類暫時性錯誤重試一次
                last_exc = exc
                if attempt < _RETRY_ON_ERROR:
                    time.sleep(_RETRY_DELAY_SECONDS)
        raise last_exc  # type: ignore[misc]

    def fetch_season(self, slug: str) -> str:
        """GET `/bangumi/<slug>`，回整頁 HTML 文字。非 200 或連線失敗 raise
        `YourAnimesError`。"""
        url = f"{_BASE_URL}/bangumi/{slug}"
        try:
            response = self._get(url)
        except Exception as exc:  # noqa: BLE001 - curl_cffi 的網路例外種類多，一律包起來
            raise YourAnimesError(f"連線失敗（{slug}）：{exc}") from exc
        status = getattr(response, "status_code", 0)
        if status != 200:
            raise YourAnimesError(f"來源網站回應 HTTP {status}（{slug}）")
        return response.text

    def fetch_anime(self, anime_id: int) -> str:
        """GET `/animes/<anime_id>`，回整頁 HTML 文字。季度頁 `<article>` 抓不到某部
        番劇（18 禁／跨季延續播出）時的補洞 fallback 才會呼叫這個，見
        `scheduler/youranimes_sync.py`。非 200 或連線失敗 raise `YourAnimesError`。"""
        url = f"{_BASE_URL}/animes/{anime_id}"
        try:
            response = self._get(url)
        except Exception as exc:  # noqa: BLE001 - curl_cffi 的網路例外種類多，一律包起來
            raise YourAnimesError(f"連線失敗（animes/{anime_id}）：{exc}") from exc
        status = getattr(response, "status_code", 0)
        if status != 200:
            raise YourAnimesError(f"來源網站回應 HTTP {status}（animes/{anime_id}）")
        return response.text
