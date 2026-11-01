"""Windows DPAPI（`CryptProtectData`／`CryptUnprotectData`）的唯一包裝。

原本這兩支函式各自寫在 `vault.py` 裡，`store/access_gate.py`（Phase 6）需要同一層
加密（見 `docs/requirements/access_gate.md`「只用 DPAPI 這一層，不需要 vault.py 的
可選 PIN 分層」）時，比照 `docs/decisions/0000-architecture-overview.md` 的「一個問題
一個唯一答案」原則抽出來共用，不要讓兩個模組各自維護一份幾乎一樣的 ctypes 呼叫。

綁定目前 Windows 使用者帳戶——只有同一台機器、同一個系統帳號能解密，這是 DPAPI 的
既有行為，不是這裡額外設計的限制。本專案目前只支援 Windows（見 `docs/decisions/
0000-architecture-overview.md`），一律視為可用，不需要額外的平台檢查介面。
"""

from __future__ import annotations

import ctypes

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_char))]


def protect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def unprotect(data: bytes) -> bytes:
    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DataBlob()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None,
        _CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)
