"""主要／備用伺服器自動切換（階段 1，規格 docs/requirements/primary_standby_server.md 2.2）。

BahaAD 程式所有「打自家伺服器」的請求（GitHub 登入中繼、統計、診斷回報、訂閱者通知中繼、推廣內容…）都走
`FailoverHttp`：呼叫端照舊用 `f"{base_url}/路徑"` 組網址，`FailoverHttp` 看到網址是以伺服器清單中任一個網址開頭，
就依 `ServerPool` 的順序（主要優先）逐台嘗試，並在下列情況改打下一台：

- 連線錯誤、逾時（丟例外）
- HTTP 502／503／504（閘道錯誤、維護中）——但 `POST /subscriber/send` 這類**不可重送**的請求只在「連線根本沒送出」
  （例外）時才改打下一台，收到 5xx 不重送，避免重複發訊息
- **該伺服器沒有這支 API**（回應標頭 `X-BahaAD-Route: missing`）——搬遷期間新伺服器還沒做完所有 API，沒做的先由
  舊的那台處理；不算故障，不進入冷卻
- 帶 `Authorization: Bearer …`（或訂閱者中繼的 `X-BahaAD-Installation-Id`）的請求收到 401 且還有下一台——
  工作階段／安裝實例可能是另一台核發的（搬遷中、或備用接手期間核發的），先問下一台，兩台都 401 才算真的無效

打不通的伺服器進入 60 秒冷卻，期間排在最後；冷卻結束後下一個請求會再優先試它（不需要另外的探測執行緒）。
其他網址（動畫瘋、GitHub、Telegram…）原封不動直接放行。

日誌刻意不寫網域名稱（使用者 2026-10-16：日誌不顯示主機域名），只寫「主要／備用」。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Sequence

logger = logging.getLogger(__name__)

ROUTE_MISSING_HEADER = "X-BahaAD-Route"
ROUTE_MISSING_VALUE = "missing"
COOLDOWN_SECONDS = 60.0
# 某台伺服器回「沒有這支 API」後，這段時間內同一路徑直接跳過它（不用每次都白問一趟）
ROUTE_MISSING_TTL_SECONDS = 600.0
FAILOVER_STATUSES = (502, 503, 504)
# 收到 5xx 也不重送的請求路徑（重送可能造成重複效果）
NO_STATUS_RETRY_PATHS = ("/subscriber/send",)


class ServerPool:
    """伺服器清單（優先序由前到後）與各台的故障冷卻狀態。thread-safe。"""

    def __init__(
        self,
        bases: Sequence[str],
        *,
        labels: Sequence[str] | None = None,
        cooldown: float = COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        cleaned = [b.rstrip("/") for b in bases if b]
        if not cleaned:
            raise ValueError("伺服器清單不能是空的")
        self._bases = tuple(dict.fromkeys(cleaned))
        names = list(labels) if labels else ["主要"] + ["備用"] * (len(self._bases) - 1)
        self._labels = dict(zip(self._bases, names))
        self._cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._failed_until: dict[str, float] = {}
        self._last_active: str | None = self._bases[0]  # 一開始預設用主要，第一次切走才會記日誌
        self._last_error_at: dict[str, float] = {}
        self._last_ok_at: dict[str, float] = {}
        self._missing_until: dict[tuple[str, str], float] = {}

    @property
    def bases(self) -> tuple[str, ...]:
        return self._bases

    @property
    def primary(self) -> str:
        return self._bases[0]

    def label(self, base: str) -> str:
        return self._labels.get(base, "備用")

    def match(self, url: str) -> str | None:
        """網址以清單中哪台的網址開頭（要剛好接在網域後面，不能是 `…example.org.evil.com` 這種）。"""
        for base in self._bases:
            if url == base or url.startswith(base + "/") or url.startswith(base + "?"):
                return base
        return None

    def order(self) -> list[str]:
        """這一次請求要依序嘗試的伺服器：沒在冷卻的（維持優先序）在前，冷卻中的排最後（仍保留當最後手段）。"""
        now = self._clock()
        with self._lock:
            healthy = [b for b in self._bases if self._failed_until.get(b, 0.0) <= now]
            cooling = [b for b in self._bases if b not in healthy]
        return healthy + cooling

    def browser_base(self) -> str:
        """瀏覽器要導向的網址（GitHub 登入）：目前優先的那台。"""
        return self.order()[0]

    def mark_route_missing(self, base: str, path: str) -> None:
        with self._lock:
            self._missing_until[(base, path)] = self._clock() + ROUTE_MISSING_TTL_SECONDS

    def route_known_missing(self, base: str, path: str) -> bool:
        with self._lock:
            return self._missing_until.get((base, path), 0.0) > self._clock()

    def report_failure(self, base: str) -> None:
        now = self._clock()
        with self._lock:
            self._failed_until[base] = now + self._cooldown
            self._last_error_at[base] = now
        self._note_active()

    def report_success(self, base: str) -> None:
        with self._lock:
            self._failed_until.pop(base, None)
            self._last_ok_at[base] = self._clock()
        self._note_active()

    def _note_active(self) -> None:
        active = self.order()[0]
        with self._lock:
            changed = active != self._last_active
            previous = self._last_active
            self._last_active = active
        if changed and previous is not None:
            if active == self.primary:
                logger.info("伺服器連線：已恢復使用主要伺服器")
            else:
                logger.warning("伺服器連線：主要伺服器暫時連不上，改用%s伺服器", self.label(active))

    def status(self) -> dict[str, Any]:
        """給設定頁顯示用：目前使用哪台、各台狀態（不含網域名稱以外的資訊；網址只在本機畫面顯示）。"""
        now = self._clock()
        with self._lock:
            servers = [
                {
                    "label": self._labels[b],
                    "base": b,
                    "cooling_down": self._failed_until.get(b, 0.0) > now,
                    "seconds_since_ok": None if b not in self._last_ok_at else round(now - self._last_ok_at[b], 1),
                    "seconds_since_error": None if b not in self._last_error_at else round(now - self._last_error_at[b], 1),
                }
                for b in self._bases
            ]
        active = self.order()[0]
        return {"active": self.label(active), "active_base": active, "servers": servers}


def _has_bearer(kwargs: dict) -> bool:
    headers = kwargs.get("headers") or {}
    for key, value in headers.items():
        if str(key).lower() == "authorization" and str(value).lower().startswith("bearer "):
            return True
        # 訂閱者中繼的安裝實例簽章請求：實例可能是另一台伺服器核發的，401 同樣先問下一台
        if str(key).lower() == "x-bahaad-installation-id" and value:
            return True
    return False


def _route_missing(response: Any) -> bool:
    try:
        return str(response.headers.get(ROUTE_MISSING_HEADER, "")).lower() == ROUTE_MISSING_VALUE
    except Exception:  # noqa: BLE001 - 假物件／沒有 headers 一律當作沒有
        return False


class FailoverHttp:
    """包住一個 http client（GamerSession／_UtilityHttpClient），對「打自家伺服器」的網址做主要／備用容錯。"""

    def __init__(self, inner: Any, pool: ServerPool) -> None:
        self._inner = inner
        self._pool = pool

    @property
    def pool(self) -> ServerPool:
        return self._pool

    def __getattr__(self, name: str) -> Any:  # 其餘屬性（current_cookies 等）原樣轉給內層
        return getattr(self._inner, name)

    def get(self, url: str, **kwargs: Any) -> Any:
        return self._call("get", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self._call("post", url, kwargs)

    def patch(self, url: str, **kwargs: Any) -> Any:
        return self._call("patch", url, kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        return self._call("put", url, kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self._call("delete", url, kwargs)

    # ------------------------------------------------------------------

    def _call(self, method: str, url: str, kwargs: dict) -> Any:
        fn = getattr(self._inner, method)
        base = self._pool.match(url)
        if base is None:
            return fn(url, **kwargs)  # 不是打自家伺服器的網址：原封不動

        suffix = url[len(base):]
        path = suffix.split("?", 1)[0]
        status_retry = not (method == "post" and path in NO_STATUS_RETRY_PATHS)
        bearer = _has_bearer(kwargs)
        order = self._pool.order()
        # 已知「某台沒有這支 API」的就直接跳過（至少留一台）
        known = [b for b in order if not self._pool.route_known_missing(b, path)]
        order = known or order

        first_unauthorized: Any = None
        last_response: Any = None
        last_exc: BaseException | None = None
        for index, target in enumerate(order):
            is_last = index == len(order) - 1
            try:
                response = fn(target + suffix, **kwargs)
            except Exception as exc:  # noqa: BLE001 - 連線／逾時／TLS 等，一律當這台暫時不可用
                self._pool.report_failure(target)
                last_exc = exc
                continue

            status = getattr(response, "status_code", 200)
            if status in FAILOVER_STATUSES and status_retry:
                self._pool.report_failure(target)
                last_response = response
                continue
            if _route_missing(response) and not is_last:
                self._pool.report_success(target)  # 伺服器是活的，只是還沒有這支 API
                self._pool.mark_route_missing(target, path)
                last_response = response
                continue
            if status == 401 and bearer and not is_last:
                self._pool.report_success(target)
                if first_unauthorized is None:
                    first_unauthorized = response
                continue

            self._pool.report_success(target)
            if status == 401 and first_unauthorized is not None:
                return first_unauthorized  # 兩台都說 401：token 真的無效，回第一台（主要）的回應
            return response

        if first_unauthorized is not None:
            return first_unauthorized
        if last_response is not None:
            return last_response
        assert last_exc is not None
        raise last_exc
