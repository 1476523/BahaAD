"""把 `bahaad` logger 的紀錄直接寫進 `store/logs.py` 的 SQLite 表。

2026-08-29 定案（使用者）：**資料庫就是主要、唯一的日誌儲存**，不再另外寫
`logs/bahaad.log` 檔案。理由：真的會讓 DB 鎖死／損毀／磁碟滿的狀況，同時也會讓
網頁介面連登入都無法運作，這時「有一份純文字檔可看」也救不了什麼，不值得為那個
極端情況維護兩套寫入路徑。

寫 DB 失敗時絕不能讓應用程式崩潰、也不能遞迴（寫失敗又去 log），所以吞掉所有例外、
交給 `logging.Handler.handleError()`。多行訊息（`exc_info=True` 的例外堆疊）由
`logging.Formatter.format()` 自己接在訊息後面，整段存進同一列。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from bahaad.store.logs import LogStore

# 日誌（網頁日誌頁、診斷回報）不要出現第三方資料站的名稱，一律顯示成「新番資訊抓取」
# （使用者 2026-10-06）。模組／logger 名稱本身（`bahaad.youranimes.*`）不改，只改顯示。
_THIRD_PARTY_RE = re.compile(r"your\s?animes(?:\.tw)?(?:_sync|_cache|_view)?", re.IGNORECASE)
_THIRD_PARTY_LABEL = "新番資訊抓取"
# 來源欄不能跟訊息用同一個詞（使用者 2026-10-06），改顯示這個名稱
_SOURCE_LABEL = "番劇資料同步"


def sanitize_log_message(text: str) -> str:
    return _THIRD_PARTY_RE.sub(_THIRD_PARTY_LABEL, text) if text else text


# 日誌頁「來源」欄的中文名稱（只改顯示，資料庫存的還是 logger 原名）。比對最長前綴，
# 沒列到的維持原名。
_SOURCE_NAMES: tuple[tuple[str, str], ...] = (
    ("bahaad.access_gate.heartbeat", "帳號連結心跳"),
    ("bahaad.access_gate", "帳號連結"),
    ("bahaad.ad_promo", "擴充元件"),
    ("bahaad.app_shell", "程式主體"),
    ("bahaad.cache", "圖片快取"),
    ("bahaad.diagnostics", "錯誤回報"),
    ("bahaad.downloader.ffmpeg_bootstrap", "ffmpeg 準備"),
    ("bahaad.downloader", "下載器"),
    ("bahaad.firewall", "防火牆"),
    ("bahaad.gamer_client.cookie_rotation", "動畫瘋登入保活"),
    ("bahaad.gamer_client.gamer_login_coordinator", "動畫瘋登入"),
    ("bahaad.gamer_client", "動畫瘋連線"),
    ("bahaad.net", "代理連線"),
    ("bahaad.newanime.convert", "新番轉換"),
    ("bahaad.newanime.notify", "新番通知"),
    ("bahaad.newanime", "新番快訊"),
    ("bahaad.notify", "通知發送"),
    ("bahaad.scheduler.completion_watch", "番劇完結偵測"),
    ("bahaad.scheduler.cookie_warmup", "登入態檢查"),
    ("bahaad.scheduler.custom_schedule", "自訂排程"),
    ("bahaad.scheduler.download_dir_migration", "下載目錄遷移"),
    ("bahaad.scheduler.gossip_watch", "官方公告監視"),
    ("bahaad.scheduler.main_loop", "排程下載"),
    ("bahaad.scheduler.recheck", "重新檢查"),
    ("bahaad.scheduler.season_repair", "季別資料夾整理"),
    ("bahaad.scheduler.version_check", "版本檢查"),
    ("bahaad.scheduler.youranimes_sync", "番劇資料同步"),
    ("bahaad.scheduler", "排程"),
    ("bahaad.stats", "使用統計"),
    ("bahaad.store.maintenance", "資料庫版本檢查"),
    ("bahaad.store", "資料庫"),
    ("bahaad.subscriber", "會員通知"),
    ("bahaad.updater", "自我更新"),
    ("bahaad.web.settings", "設定頁"),
    ("bahaad.web", "網頁介面"),
    ("bahaad.youranimes", "番劇資料同步"),
)


_FETCH_FAIL_DETAIL_RE = re.compile(r"連線失敗（[^）]*）：")
_CURL_HELP_RE = re.compile(r"\s*See https://curl\.se/libcurl/c/libcurl-errors\.html first for more details\.?")
_CURL_PERFORM_RE = re.compile(r"Failed to perform, curl: \((\d+)\)\s*")
_CURL_TIMEOUT_RE = re.compile(
    r"Operation timed out after (\d+) milliseconds with (\d+)(?: out of (-?\d+))? bytes received"
)

# 執行緒名稱（輪換日誌「執行緒：xxx」）
_THREAD_NAMES = {
    "gamer-login": "登入程序",
    "home-cache-refresh": "首頁快取更新",
    "detail-cache-prefetch": "番劇頁快取補齊",
}


def _thread_name(m) -> str:
    name = m.group(2)
    if name in _THREAD_NAMES:
        return m.group(1) + _THREAD_NAMES[name]
    if re.fullmatch(r"Thread-\d+ \(\w+\)", name):
        return m.group(1) + "背景執行緒"
    return m.group(0)


_TRACEBACK_RULES: tuple[tuple["re.Pattern[str]", object], ...] = (
    (re.compile(r"Traceback \(most recent call last\):"), "錯誤追蹤（最近一次呼叫在最後）："),
    (
        re.compile(r'^(\s*)File "(.*)", line (\d+), in (.+)$', re.MULTILINE),
        lambda m: f'{m.group(1)}檔案 "{m.group(2)}"，第 {m.group(3)} 行，於 {m.group(4)}',
    ),
    (
        re.compile(r'^(\s*)File "(.*)", line (\d+)$', re.MULTILINE),
        lambda m: f'{m.group(1)}檔案 "{m.group(2)}"，第 {m.group(3)} 行',
    ),
    (re.compile(r"During handling of the above exception, another exception occurred:"),
     "處理上述例外時，又發生另一個例外："),
    (re.compile(r"The above exception was the direct cause of the following exception:"),
     "上述例外直接導致了下面這個例外："),
    # 連線錯誤（libcurl）：拿掉說明連結、常見英文全翻成繁體中文（使用者 2026-10-09）
    (_CURL_HELP_RE, ""),
    # 「連線失敗（網址／代號）：」後面的括號資訊不需要顯示 → 「連線失敗：」
    (_FETCH_FAIL_DETAIL_RE, "連線失敗："),
    (_CURL_PERFORM_RE, lambda m: f"curl 錯誤 {m.group(1)}："),
    (re.compile(r"Could not resolve host:"), "無法解析主機："),
    (re.compile(r"Could not resolve proxy:"), "無法解析代理主機："),
    (re.compile(r"Failed to connect to (\S+) port (\d+)(?: after \d+ ms)?: (?:Connection refused|Couldn't connect to server)"),
     lambda m: f"無法連線到 {m.group(1)}（連接埠 {m.group(2)}）：對方拒絕連線"),
    (re.compile(r"TLS connect error: error:\w+:SSL routines:OPENSSL_internal:WRONG_VERSION_NUMBER\.?"),
     "TLS 連線錯誤（對方回應的不是 TLS 通訊協定）"),
    (re.compile(r"TLS connect error: error:\w+:SSL routines:OPENSSL_internal:(\w+)\.?"),
     lambda m: f"TLS 連線錯誤（{m.group(1)}）"),
    (_CURL_TIMEOUT_RE, lambda m: (
        f"連線逾時（{int(m.group(1)) / 1000:g} 秒，已收到 {m.group(2)}"
        + (f"／{m.group(3)}" if m.group(3) and m.group(3) != "-1" else "") + " 位元組）"
    )),
    (re.compile(r"Connection timed out after (\d+) milliseconds"),
     lambda m: f"連線逾時（{int(m.group(1)) / 1000:g} 秒）"),
    (re.compile(r"Recv failure: Connection was reset"), "接收失敗：連線被重置"),
    (re.compile(r"Send failure: Connection was reset"), "送出失敗：連線被重置"),
    (re.compile(r"Empty reply from server"), "伺服器沒有回應內容"),
    # 內部名稱：設定旗標、排程名稱、執行緒、網址
    (re.compile(r"\b_pending_update\b"), "待套用更新"),
    (re.compile(r"\b_update_available\b"), "有新版可更新"),
    (re.compile(r"\bcookie_warmup(?=：)"), "登入態檢查"),
    (re.compile(r"(執行緒：)([^）;；]+)"), _thread_name),
    (re.compile(r"(觸發：)https://ani\.gamer\.com\.tw/animeVideo\.php"), r"\g<1>動畫瘋番劇頁"),
    (re.compile(r"(觸發：)https://ani\.gamer\.com\.tw/(?=[；;）]|\s|$)"), r"\g<1>動畫瘋首頁"),
    (re.compile(r"https://ani\.gamer\.com\.tw/seasonal\.php"), "新番授權情報頁"),
    (re.compile(r"\bseasonal\.php\b"), "新番授權情報頁"),
    # 其餘夾在中文訊息裡的英文名詞
    (re.compile(r"處置=unsubscribe"), "處置=自動退訂"),
    (re.compile(r"處置=notify"), "處置=只通知"),
    (re.compile(r"處置=mark"), "處置=標記完結"),
    (re.compile(r"\bdevice_id\b"), "裝置識別碼"),
    (re.compile(r"\baccess_gate\b"), "帳號連結"),
    (re.compile(r"\bcookie\b", re.IGNORECASE), "憑證"),
)

_TRANSLATE_HINTS = (
    "Traceback", 'File "', "exception", "curl", "連線失敗（", "_pending_update", "_update_available",
    "cookie_warmup", "執行緒：", "觸發：", "seasonal.php", "處置=", "device_id", "access_gate", "cookie",
    "Cookie",
)


def translate_log_message(text: str) -> str:
    """日誌訊息顯示／寫入用：把例外堆疊與常見 libcurl 連線錯誤的固定英文字樣、內部名稱翻成
    繁體中文（例外型別名稱、檔案路徑、網域、系統給的中文訊息維持原樣），並拿掉「See
    https://curl.se/… for more details」說明連結與「連線失敗（網址）」的括號資訊
    （使用者 2026-10-09）。可重複套用（翻過的不會再變）。"""
    if not text or not any(hint in text for hint in _TRANSLATE_HINTS):
        return text
    for pattern, repl in _TRACEBACK_RULES:
        text = pattern.sub(repl, text)
    return text


def display_log_source(name: str) -> str:
    """日誌頁來源欄顯示用：已遮蔽站名後，再把已知的 logger 名稱換成中文。"""
    name = sanitize_log_source(name)
    if name == "bahaad":  # 根 logger（app_shell 的伺服器監看等）只在完全相等時才翻譯
        return "程式主體"
    best = max(
        (item for item in _SOURCE_NAMES if name == item[0] or name.startswith(item[0] + ".")),
        key=lambda item: len(item[0]), default=None,
    )
    return best[1] if best else name


def sanitize_log_source(name: str) -> str:
    """來源欄：logger 名稱含第三方站名（`bahaad.scheduler.youranimes_sync`）整個換成來源標籤；
    之前已經被換成訊息用標籤的舊紀錄也一併改成來源標籤。"""
    if name == _THIRD_PARTY_LABEL or (name and _THIRD_PARTY_RE.search(name)):
        return _SOURCE_LABEL
    return name


class SqliteLogHandler(logging.Handler):
    def __init__(self, log_store: LogStore, level: int = logging.INFO) -> None:
        super().__init__(level)
        self._log_store = log_store
        # %(message)s：format() 會自己把 exc_info／stack_info 接在後面
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._log_store.add(
                level=record.levelname,
                logger=display_log_source(record.name),
                message=translate_log_message(sanitize_log_message(self.format(record))),
                ts=datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
            )
        except Exception:  # noqa: BLE001 - handler 不能讓程式崩潰
            self.handleError(record)
