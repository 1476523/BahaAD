"""非同步、盡力而為的回報送出。規格見 docs/requirements/diagnostics.md「`diagnostics/`
模組結構」一節。

`report()` 是唯一對外入口，設計成呼叫端完全不用擔心會被拖慢或被例外波及——呼叫端全部
是深藏在其他背景執行緒 `except` 區塊裡的呼叫（見 scheduler/main_loop.py／updater/
policy.py 的整合點），這裡出錯不能連帶讓外層那個例外處理流程也跟著炸掉。
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path
from typing import Callable, Protocol

from bahaad.diagnostics.codes import (
    AuthState,
    ConnectionStatus,
    ErrorCode,
    FileIntegrityResult,
    OperationType,
)
from bahaad.diagnostics.report import DiagnosticsReport, describe_path, to_payload
from bahaad.store.settings import SettingsStore

logger = logging.getLogger(__name__)

_DEFAULT_QUEUE_MAXSIZE = 50
_DEFAULT_REQUEST_TIMEOUT_SECONDS = 5.0
_SETTINGS_KEY = "diagnostics_enabled"
_REPORTED_COUNT_KEY = "diagnostics_reported_count"


class HttpPoster(Protocol):
    def post(self, url: str, **kwargs) -> object: ...


class DiagnosticsReporter:
    def __init__(
        self,
        settings: SettingsStore,
        http: HttpPoster,
        base_url: str,
        queue_maxsize: int = _DEFAULT_QUEUE_MAXSIZE,
        request_timeout: float = _DEFAULT_REQUEST_TIMEOUT_SECONDS,
        auth_state_fn: Callable[[], AuthState] | None = None,
    ) -> None:
        self._settings = settings
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._request_timeout = request_timeout
        # .0 改進.txt 第 14 項：回報當下是會員身分還是遊客身分（`app_shell` 注入一個查
        # `GamerSession` cookie 有沒有 BAHAID 的函式）。None／查詢出錯 → 不帶這個欄位。
        self._auth_state_fn = auth_state_fn
        self._queue: queue.Queue = queue.Queue(maxsize=queue_maxsize)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def report(
        self,
        error_code: ErrorCode,
        operation_type: OperationType,
        *,
        connection_status: ConnectionStatus | None = None,
        network_latency_ms: float | None = None,
        network_download_rate_kbps: float | None = None,
        file_integrity_result: FileIntegrityResult | None = None,
        path: Path | None = None,
        known_roots: dict[str, Path] | None = None,
        exception_type: str | None = None,
        error_source: str | None = None,
    ) -> None:
        """關閉時整個方法直接 no-op，連 payload 都不組——每次呼叫都即時檢查設定，
        不是啟動時讀一次快取起來，見 diagnostics.md「使用者關閉診斷回報後又重新
        打開」邊界案例。"""
        if not self._settings.get(_SETTINGS_KEY, True):
            return

        path_info = describe_path(path, known_roots) if path is not None and known_roots else None
        report = DiagnosticsReport(
            error_code=error_code,
            operation_type=operation_type,
            connection_status=connection_status,
            network_latency_ms=network_latency_ms,
            network_download_rate_kbps=network_download_rate_kbps,
            file_integrity_result=file_integrity_result,
            path_info=path_info,
            auth_state=self._current_auth_state(),
            exception_type=exception_type,
            error_source=error_source,
        )
        try:
            self._queue.put_nowait(to_payload(report))
        except queue.Full:
            logger.debug("診斷回報佇列已滿，捨棄這筆回報（error_code=%s）", error_code.value)

    def _current_auth_state(self) -> AuthState | None:
        if self._auth_state_fn is None:
            return None
        try:
            return self._auth_state_fn()
        except Exception:  # noqa: BLE001 - 查登入態失敗不能拖垮回報
            return AuthState.UNKNOWN

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                payload = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            self._send(payload)

    def _send(self, payload: dict) -> None:
        try:
            self._http.post(
                f"{self._base_url}/diagnostics/report", json=payload, timeout=self._request_timeout
            )
        except Exception as exc:
            # 送出失敗完全吞掉，不能讓「回報錯誤」這件事本身又拋出例外——見模組
            # docstring；只記 debug（不是 warning），連不上 access_gate_server
            # 不是使用者需要在意的問題
            logger.debug("診斷回報送出失敗（不影響任何功能）：%s", exc)
            return
        # 送出成功 → 「已回報錯誤的次數」+1（使用者 2026-09-08，設定頁顯示）
        try:
            self._settings.increment(_REPORTED_COUNT_KEY)
        except Exception:  # noqa: BLE001 - 計數失敗不影響回報本身
            logger.debug("診斷回報計數器 +1 失敗（不影響回報）", exc_info=True)
