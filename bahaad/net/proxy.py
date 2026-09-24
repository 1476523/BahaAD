"""進階存取：多組代理 + 連不上自動切換下一組。規格見 docs/requirements/advanced_access.md。

使用者可以設定多個「代理設定組」，分三種角色：
- **主要（primary）**：平常用的代理。
- **備用（backup）**：主要連不上時自動切到這組。
- **其他（other）**：第三順位以後，可自訂任意多組。

嘗試順序＝主要 → 備用 → 其他（清單順序）。某一組連不上 → 切下一組（不會靜默改走
直連）；**全部都連不上才拋 `AllProxiesUnavailable`**（呼叫端據此回明確錯誤，而不是
被誤判成「站方維護」）。切走一段時間後會自動切回主要組重試。

只吃 libcurl 原生支援的 scheme（http/https/socks4/socks4a/socks5/socks5h）。使用者填
的是 Clash / v2ray / sing-box 暴露出來的本機混合埠，不解析 vmess/vless、不吃設定檔。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

SETTINGS_ENABLED_KEY = "advanced_access_enabled"
SETTINGS_PROXIES_KEY = "advanced_access_proxies"

VALID_SCHEMES = ("http", "https", "socks4", "socks4a", "socks5", "socks5h")
ROLES = ("primary", "backup", "other")
_ROLE_RANK = {"primary": 0, "backup": 1, "other": 2}

# 切走之後過這麼久沒再失敗，就把「目前生效」重置回主要組、下次請求重試主要
_RESET_TO_PRIMARY_AFTER_SECONDS = 300.0

_SCHEME_LABELS = {
    "http": "HTTP",
    "https": "HTTPS",
    "socks4": "SOCKS4",
    "socks4a": "SOCKS4a",
    "socks5": "SOCKS5",
    "socks5h": "SOCKS5（遠端 DNS）",
}
_ROLE_LABELS = {"primary": "主要設定組", "backup": "備用設定組", "other": "其他設定組"}


class AllProxiesUnavailable(Exception):
    """設定了代理、但每一組都連不上。呼叫端應回明確錯誤，不要靜默改走直連。"""


@dataclass(frozen=True)
class ProxyConfig:
    role: str  # primary | backup | other
    label: str
    scheme: str
    host: str
    port: int

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @property
    def role_label(self) -> str:
        return _ROLE_LABELS.get(self.role, self.role)

    @property
    def scheme_label(self) -> str:
        return _SCHEME_LABELS.get(self.scheme, self.scheme.upper())

    def as_dict(self) -> dict:
        return {
            "role": self.role,
            "label": self.label,
            "scheme": self.scheme,
            "host": self.host,
            "port": self.port,
        }


def validate_proxy_dict(raw: dict) -> tuple[ProxyConfig | None, str | None]:
    """一筆設定組的驗證。回 `(config, None)` 或 `(None, 錯誤訊息)`。"""
    if not isinstance(raw, dict):
        return None, "設定格式不正確"
    role = str(raw.get("role", "other")).strip().lower()
    if role not in ROLES:
        role = "other"
    scheme = str(raw.get("scheme", "")).strip().lower()
    if scheme not in VALID_SCHEMES:
        return None, f"不支援的代理協定：{scheme or '（空白）'}"
    host = str(raw.get("host", "")).strip()
    if not host:
        return None, "代理位址不能空白"
    if "://" in host or "/" in host:
        return None, "代理位址只填主機名稱或 IP，不要帶協定或路徑"
    try:
        port = int(raw.get("port"))
    except (TypeError, ValueError):
        return None, "代理連接埠必須是數字"
    if not (1 <= port <= 65535):
        return None, "代理連接埠必須介於 1–65535"
    label = str(raw.get("label", "")).strip() or _ROLE_LABELS.get(role, "代理")
    return ProxyConfig(role=role, label=label, scheme=scheme, host=host, port=port), None


def parse_proxies(raw_list) -> list[ProxyConfig]:
    """settings 的 `list[dict]` → 依角色排序（primary → backup → other，同角色保留原順序）
    的 `list[ProxyConfig]`。壞掉的一筆略過、只記 debug。"""
    if not isinstance(raw_list, list):
        return []
    out: list[tuple[int, int, ProxyConfig]] = []
    for i, raw in enumerate(raw_list):
        cfg, err = validate_proxy_dict(raw)
        if cfg is None:
            logger.debug("略過一筆無效的代理設定：%s（%s）", raw, err)
            continue
        out.append((_ROLE_RANK.get(cfg.role, 2), i, cfg))
    out.sort(key=lambda t: (t[0], t[1]))
    return [cfg for _, _, cfg in out]


def _proxies_from_settings(settings) -> list[ProxyConfig]:
    if not settings.get(SETTINGS_ENABLED_KEY, False):
        return []
    return parse_proxies(settings.get(SETTINGS_PROXIES_KEY, []))


def build_proxy_selector(settings) -> "ProxySelector":
    """從 `SettingsStore` 建 `ProxySelector`。**一律回一個物件**（總開關關 / 沒設定組時
    是空的——`bool(selector)` 為 False、`send_with_failover` 就直連）。設定頁存檔後
    呼叫 `selector.reload_from_settings(settings)` 就地更新，不用重啟。"""
    return ProxySelector(_proxies_from_settings(settings))


class ProxySelector:
    """執行緒安全的「目前生效代理」選擇器。多個 session 共用同一個實例。"""

    def __init__(self, proxies: list[ProxyConfig], *, clock=time.monotonic) -> None:
        self._proxies = list(proxies)
        self._idx = 0
        self._last_switch = 0.0
        self._clock = clock
        self._lock = threading.Lock()

    def __bool__(self) -> bool:
        return bool(self._proxies)

    @property
    def chain_length(self) -> int:
        return len(self._proxies)

    def reload(self, proxies: list[ProxyConfig]) -> None:
        """設定頁存檔後呼叫——換掉整份清單、回到主要組。"""
        with self._lock:
            self._proxies = list(proxies)
            self._idx = 0
            self._last_switch = 0.0

    def reload_from_settings(self, settings) -> None:
        self.reload(_proxies_from_settings(settings))

    def current_url(self) -> str | None:
        with self._lock:
            if not self._proxies:
                return None
            if self._idx != 0 and (
                self._clock() - self._last_switch >= _RESET_TO_PRIMARY_AFTER_SECONDS
            ):
                logger.info("代理冷卻期滿，切回主要設定組重試")
                self._idx = 0
            return self._proxies[self._idx].url

    def report_failure(self, failed_url: str | None) -> str | None:
        """`failed_url` 這組連不上——切下一組並回新的 url。全部都試完回 `None`。"""
        with self._lock:
            if not self._proxies:
                return None
            cur = self._proxies[self._idx]
            if failed_url is not None and cur.url != failed_url:
                # 別條執行緒已經切過了，直接用現在生效的這組
                return cur.url
            if self._idx + 1 < len(self._proxies):
                self._idx += 1
                self._last_switch = self._clock()
                nxt = self._proxies[self._idx]
                logger.warning(
                    "代理「%s」(%s) 連不上，切換到「%s」(%s)",
                    cur.label, cur.url, nxt.label, nxt.url,
                )
                return nxt.url
            logger.error("所有代理設定組都連不上（共 %d 組）", len(self._proxies))
            self._idx = 0
            self._last_switch = self._clock()
            return None

    def status(self) -> dict:
        with self._lock:
            if not self._proxies:
                return {"active": None, "count": 0}
            cur = self._proxies[self._idx]
            return {
                "active": cur.as_dict(),
                "active_index": self._idx,
                "count": len(self._proxies),
            }


# 型別名 fallback（curl_cffi import 不到時用；正常情況走下面的 isinstance）
_PROXY_TRANSPORT_ERROR_NAMES = frozenset(
    {
        "ProxyError", "InvalidProxyURL", "InvalidSchema", "MissingSchema",
        "ConnectionError", "ConnectTimeout", "DNSError",
        "SSLError", "CertificateVerifyError",
    }
)


def _curl_transport_error_types():
    """curl_cffi 裡「連到代理這一段失敗」的例外類別。`ConnectionError` 這一支涵蓋
    連線被拒、`ConnectTimeout`、`DNSError`、以及 `SSLError`／`CertificateVerifyError`
    （代理 scheme 填錯——例如把純 HTTP 代理填成 `https://`——會是 curl(35)
    `WRONG_VERSION_NUMBER`，屬 SSLError；這確實是「這組代理用不了」，要切下一組）。
    `ReadTimeout`（連上之後才逾時＝目標站慢）**不**算，不然目標站一慢就把整條代理鏈
    燒完。"""
    from curl_cffi.requests import exceptions as ce

    return (ce.ProxyError, ce.ConnectionError, ce.InvalidProxyURL, ce.InvalidSchema, ce.MissingSchema)


def is_proxy_transport_error(exc: BaseException) -> bool:
    """沿著例外鏈找「連到代理本身失敗」的型別。"""
    try:
        types = _curl_transport_error_types()
    except Exception:  # noqa: BLE001 - curl_cffi 匯入不到就退回型別名比對
        types = None
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if types is not None and isinstance(current, types):
            return True
        if type(current).__name__ in _PROXY_TRANSPORT_ERROR_NAMES:
            return True
        current = current.__cause__ or current.__context__
    return False


def send_with_failover(fn, selector: "ProxySelector | None", *args, **kwargs):
    """`fn` 是 `session.get` / `session.post` 之類。`selector` 為 None＝不用代理、直接呼叫。

    連到代理失敗 → 換下一組重試；全部組都失敗 → 拋 `AllProxiesUnavailable`。其他例外
    （逾時、HTTP 錯誤、SSL…）原樣往外拋，不當成代理問題。"""
    if not selector:
        return fn(*args, **kwargs)

    proxy = selector.current_url()
    last_exc: BaseException | None = None
    for _ in range(selector.chain_length + 1):
        try:
            return fn(*args, proxy=proxy, **kwargs)
        except AllProxiesUnavailable:
            raise
        except BaseException as exc:  # noqa: BLE001 - 要看型別決定切不切
            if not is_proxy_transport_error(exc):
                raise
            last_exc = exc
            proxy = selector.report_failure(proxy)
            if proxy is None:
                raise AllProxiesUnavailable(_all_down_message(last_exc)) from exc
    raise AllProxiesUnavailable(_all_down_message(last_exc)) from last_exc


def _all_down_message(last_exc: BaseException | None) -> str:
    base = "所有代理設定組都連不上"
    if last_exc is None:
        return base
    detail = str(last_exc).splitlines()[0][:120]
    return f"{base}（最後錯誤：{detail}）"
