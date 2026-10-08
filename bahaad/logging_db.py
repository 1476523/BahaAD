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


def display_log_source(name: str) -> str:
    """日誌頁來源欄顯示用：已遮蔽站名後，再把已知的 logger 名稱換成中文。"""
    name = sanitize_log_source(name)
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
                logger=sanitize_log_source(record.name),
                message=sanitize_log_message(self.format(record)),
                ts=datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
            )
        except Exception:  # noqa: BLE001 - handler 不能讓程式崩潰
            self.handleError(record)
