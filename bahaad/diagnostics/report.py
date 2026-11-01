"""回報內容的資料型別與去識別化。規格見 docs/requirements/diagnostics.md「欄位清單」
「`diagnostics/` 模組結構」兩節。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from bahaad.diagnostics.codes import (
    AuthState,
    ConnectionStatus,
    ErrorCode,
    FileIntegrityResult,
    OperationType,
)


@dataclass(frozen=True)
class PathInfo:
    relative_path: str
    drive: str
    free_space_bytes: int | None


def describe_path(path: Path, known_roots: dict[str, Path]) -> PathInfo | None:
    """`known_roots` 是呼叫端傳進來的「已知安全根目錄」（例如安裝目錄／下載目錄）。
    `path` 不在任何一個根目錄底下就回 `None`——**整組都不回報**，不去猜測怎麼去識別化，
    這是比「先移除路徑中的使用者名稱」更保守的做法，從根本上避免任何洩漏使用者名稱
    的風險，見 diagnostics.md 欄位清單「路徑資訊」一列。"""
    path = Path(path).resolve()
    for root in known_roots.values():
        root = Path(root).resolve()
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        free_space_bytes: int | None
        try:
            free_space_bytes = shutil.disk_usage(path.anchor).free
        except OSError:
            free_space_bytes = None
        return PathInfo(relative_path=str(relative), drive=path.drive, free_space_bytes=free_space_bytes)
    return None


@dataclass(frozen=True)
class DiagnosticsReport:
    error_code: ErrorCode
    operation_type: OperationType
    connection_status: ConnectionStatus | None = None
    network_latency_ms: float | None = None
    network_download_rate_kbps: float | None = None
    file_integrity_result: FileIntegrityResult | None = None
    path_info: PathInfo | None = field(default=None)
    auth_state: AuthState | None = None
    # 自動回報（DiagnosticsLogHandler）用——都是程式碼層級的識別字，不含任何使用者資料：
    # exception_type = 例外類別名（"FileNotFoundError"）；error_source = logger 名／模組
    # （"bahaad.app_shell"）。**不含**例外訊息、堆疊、參數、路徑。
    exception_type: str | None = None
    error_source: str | None = None


def to_payload(report: DiagnosticsReport) -> dict:
    """轉成 JSON-safe dict，`None` 欄位整個省略不送（不是送 `null`）——降低 payload
    大小，也避免伺服器端要處理一堆 null 判斷。"""
    payload: dict = {
        "error_code": report.error_code.value,
        "operation_type": report.operation_type.value,
    }
    if report.connection_status is not None:
        payload["connection_status"] = report.connection_status.value
    if report.network_latency_ms is not None:
        payload["network_latency_ms"] = report.network_latency_ms
    if report.network_download_rate_kbps is not None:
        payload["network_download_rate_kbps"] = report.network_download_rate_kbps
    if report.file_integrity_result is not None:
        payload["file_integrity_result"] = report.file_integrity_result.value
    if report.auth_state is not None:
        payload["auth_state"] = report.auth_state.value
    if report.exception_type is not None:
        payload["exception_type"] = report.exception_type
    if report.error_source is not None:
        payload["error_source"] = report.error_source
    if report.path_info is not None:
        payload["path_relative"] = report.path_info.relative_path
        payload["path_drive"] = report.path_info.drive
        if report.path_info.free_space_bytes is not None:
            payload["path_free_space_bytes"] = report.path_info.free_space_bytes
    return payload
