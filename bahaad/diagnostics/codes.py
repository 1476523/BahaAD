"""固定的錯誤代碼表／操作類型／連線狀態分類。規格見 docs/requirements/diagnostics.md
「錯誤代碼表」「連線狀態分類怎麼判斷」兩節。

刻意用固定 `Enum`，不是開放字串——避免各個呼叫端各自發明代碼，值越長越亂，也呼應
`docs/PRIVACY_POLICY.md`「這份清單本身就是完整範圍」的承諾。
"""

from __future__ import annotations

from enum import Enum


class ErrorCode(Enum):
    # 下載影片
    PLAYLIST_FETCH_FAILED = "playlist_fetch_failed"
    # 遊客／非 VIP 看廣告交握失敗——年齡限制等未知 error code 靠這個收樣本
    # （docs/requirements/guest_download.md）
    GUEST_HANDSHAKE_FAILED = "guest_handshake_failed"
    SEGMENT_DOWNLOAD_FAILED = "segment_download_failed"
    FFMPEG_MUX_FAILED = "ffmpeg_mux_failed"
    DOWNLOAD_UNEXPECTED_ERROR = "download_unexpected_error"
    # 解析番劇頁面——目前代碼表裡先留著，還沒有真正的呼叫點在用，
    # 見 diagnostics.md「誰呼叫 report()」刻意縮小的整合範圍
    BROWSE_PARSE_FAILED = "browse_parse_failed"
    CATALOG_QUERY_FAILED = "catalog_query_failed"
    # 套用更新
    UPDATE_MANIFEST_FETCH_FAILED = "update_manifest_fetch_failed"
    UPDATE_MANIFEST_SIGNATURE_INVALID = "update_manifest_signature_invalid"
    UPDATE_FILE_FETCH_FAILED = "update_file_fetch_failed"
    UPDATE_APPLY_FAILED = "update_apply_failed"
    # 未分類：任何 ERROR/CRITICAL 且帶例外的 log record，由 DiagnosticsLogHandler 自動送
    # （2026-09-01 使用者要求「錯誤都上傳」——見 diagnostics.md「自動回報」一節）。
    # 只帶 exception_type（例外類型名）＋ error_source（logger 名／模組），不帶訊息/堆疊。
    UNHANDLED_EXCEPTION = "unhandled_exception"


class OperationType(Enum):
    """值直接用中文，跟 PRIVACY_POLICY.md 給的範例逐字一致。"""

    DOWNLOAD_VIDEO = "下載影片"
    PARSE_ANIME_PAGE = "解析番劇頁面"
    APPLY_UPDATE = "套用更新"
    UNKNOWN = "未分類"  # DiagnosticsLogHandler 自動回報用


class ConnectionStatus(Enum):
    TIMEOUT = "timeout"
    DISCONNECTED = "disconnected"
    DNS_FAILED = "dns_failed"
    OTHER = "other"


class FileIntegrityResult(Enum):
    MATCH = "match"
    MISMATCH = "mismatch"


class AuthState(Enum):
    """回報發生的當下，是用動畫瘋會員身分還是遊客身分——沒有這個沒辦法判斷「付費番劇
    下載失敗」到底是登入態掉了還是本來就沒登入（.0 改進.txt 第 14 項）。"""

    MEMBER = "member"
    GUEST = "guest"
    UNKNOWN = "unknown"


def classify_connection_error(exc: BaseException) -> ConnectionStatus:
    """沿著 `__cause__`／`__context__` 鏈往回找 curl_cffi 的具體例外型別，找不到就是
    `OTHER`——寧可分類成 other，不猜測（見 diagnostics.md 同名章節）。延後 import
    curl_cffi.requests.exceptions：`codes.py` 不應該因為 curl_cffi 沒裝就整個壞掉
    （目前專案一定會裝，但這個模組本身的職責只是分類邏輯，延後 import 讓依賴關係
    更明確）。"""
    from curl_cffi.requests import exceptions as curl_exceptions

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, curl_exceptions.DNSError):
            return ConnectionStatus.DNS_FAILED
        if isinstance(current, (curl_exceptions.ConnectTimeout, curl_exceptions.ReadTimeout, curl_exceptions.Timeout)):
            return ConnectionStatus.TIMEOUT
        if isinstance(current, curl_exceptions.ConnectionError):
            return ConnectionStatus.DISCONNECTED
        current = current.__cause__ or current.__context__
    return ConnectionStatus.OTHER
