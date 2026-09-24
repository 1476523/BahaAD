"""新番快訊的 HTTP client——**專用、無 cookie、無登入**，比照 `youranimes/fetch.py`。

規格 docs/requirements/new_anime_bulletin.md §1d：使用者定「不開瀏覽器、不去 ja3.zone、
不用登入的 cookie」。就一個乾淨的 `curl_cffi` session（`impersonate="chrome"`，Nuitka 已
打包）抓三個公開頁面：

- `gnn.gamer.com.tw/detail.php?sn=<N>`（新番節目資訊文章）——純抓會 403，要瀏覽器指紋
- `ani.gamer.com.tw/seasonal.php`（新番推廣頁）
- `youranimes.tw/bangumi/<YYYYMM>`（主視覺圖）——`YourAnimesFetcher` 已經在做，這裡不重複

**不碰 `IdentityStore`、不帶動畫瘋登入 cookie／`Referer`／`Origin`。**
"""

from __future__ import annotations

from curl_cffi import requests as curl_requests

from bahaad.net.proxy import send_with_failover

_GNN_DETAIL_URL = "https://gnn.gamer.com.tw/detail.php"
_SEASONAL_URL = "https://ani.gamer.com.tw/seasonal.php"
_YOURANIMES_SEASON_URL = "https://youranimes.tw/bangumi/{slug}"
_TIMEOUT_SECONDS = 30
_IMPERSONATE = "chrome"


class NewAnimeFetchError(Exception):
    """抓取失敗（連線錯誤 / 非 200）。"""


class NewAnimeFetcher:
    def __init__(self, session=None, *, proxy_selector=None) -> None:
        self._session = session or curl_requests.Session(impersonate=_IMPERSONATE)
        self._proxy_selector = proxy_selector

    def _get(self, url: str, params: dict | None = None) -> str:
        try:
            response = send_with_failover(
                self._session.get, self._proxy_selector, url,
                params=params, timeout=_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # noqa: BLE001 - curl_cffi 的網路例外種類多，一律包起來
            raise NewAnimeFetchError(f"連線失敗（{url}）：{exc}") from exc
        status = getattr(response, "status_code", 0)
        if status != 200:
            raise NewAnimeFetchError(f"來源網站回應 HTTP {status}（{url}）")
        return response.text

    def fetch_gnn_article(self, gnn_sn: int) -> str:
        """GET `gnn.gamer.com.tw/detail.php?sn=<gnn_sn>`，回整頁 HTML。"""
        return self._get(_GNN_DETAIL_URL, params={"sn": gnn_sn})

    def fetch_seasonal_page(self) -> str:
        """GET `ani.gamer.com.tw/seasonal.php`（當前推廣季的新番推廣頁）。"""
        return self._get(_SEASONAL_URL)

    def fetch_youranimes_season(self, season_key: str) -> str:
        """GET `youranimes.tw/bangumi/<YYYYMM>`（拿主視覺圖 + 製作陣容）。"""
        return self._get(_YOURANIMES_SEASON_URL.format(slug=season_key))
