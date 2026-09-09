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
from datetime import datetime

from bahaad.store.logs import LogStore


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
                logger=record.name,
                message=self.format(record),
                ts=datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
            )
        except Exception:  # noqa: BLE001 - handler 不能讓程式崩潰
            self.handleError(record)
