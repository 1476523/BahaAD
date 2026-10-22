"""把「ERROR/CRITICAL 且帶例外」的 log record 自動轉成一筆診斷回報。

2026-09-01：使用者要求「錯誤都上傳」——原本 `diagnostics/` 刻意只在寫死的呼叫點
回報、不掛全域攔截；改成再多這一條「兜底」路徑：任何地方 `logger.exception(...)` /
`logger.error(..., exc_info=True)` / 未捕捉的例外冒到 `logging` 這一層，都送一筆。

**只送兩個新欄位**，兩個都是程式碼層級的識別字、不含任何使用者資料：
  - `exception_type`：例外類別名，例如 `"FileNotFoundError"`
  - `error_source`：logger 名／模組，例如 `"bahaad.app_shell"`
**不送**例外訊息文字、堆疊、`record.args`、任何路徑。訊息與完整堆疊照舊只留在本機
`bahaad.db` 的日誌表。

`DiagnosticsReporter.report()` 內部自己會檢查 `diagnostics_enabled` 設定，關閉時整個
no-op，所以這個 handler 不用另外判斷開關。
"""

from __future__ import annotations

import logging

from bahaad.diagnostics.codes import ErrorCode, OperationType


class DiagnosticsLogHandler(logging.Handler):
    def __init__(self, reporter) -> None:
        super().__init__(level=logging.ERROR)
        self._reporter = reporter
        self._in_emit = False  # 防遞迴：report() 內部若有 log 也不會再繞回來

    def emit(self, record: logging.LogRecord) -> None:
        if self._in_emit:
            return
        exc_type = None
        if record.exc_info and record.exc_info[0] is not None:
            exc_type = record.exc_info[0].__name__
        elif record.exc_text:
            exc_type = "Unknown"
        if exc_type is None:
            return  # 沒有例外資訊的 error log（純狀態訊息）不送

        self._in_emit = True
        try:
            self._reporter.report(
                ErrorCode.UNHANDLED_EXCEPTION,
                OperationType.UNKNOWN,
                exception_type=exc_type,
                error_source=record.name,
            )
        except Exception:  # noqa: BLE001 - log handler 絕對不能自己炸
            pass
        finally:
            self._in_emit = False
