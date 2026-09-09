"""Windows Defender 防火牆：讓 BahaAD 的網頁介面在區網放行「一次」，之後就地更新
（換掉同路徑的 BahaAD.exe）不會再跳「是否允許存取」詢問。

背景：Windows 對**沒有數位簽章**的 exe，防火牆放行是綁「這個檔案（路徑＋內容）」的。
自我更新把 BahaAD.exe 換成新版（路徑一樣、內容不同）之後 Windows 認不得，就重新詢問。
解法是我們自己建一條「依映像路徑（`program=`）放行」的規則——這種規則只看路徑、不看
內容，換 exe 也一直成立。

流程（`app_shell._ensure_firewall_rule`）：第一次啟動（監聽 `0.0.0.0`）時、規則不在，
就主動跳一次 UAC 建規則，並**等它做完**再讓網頁伺服器 bind——這樣連第一次的 Windows
防火牆詢問也不會出現。使用者在 UAC 按「否」→ 設旗標讓設定頁留一顆手動按鈕，不再自動
打擾。建規則要系統管理員權限；查詢規則不用。非 Windows／從原始碼跑一律 no-op。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# ASCII——`ShellExecute` 帶過去的參數字串裡有非 ASCII 會有編碼風險，規則名稱維持 7-bit。
RULE_NAME = "BahaAD Web UI"

_CREATE_NO_WINDOW = 0x08000000
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_WAIT_OBJECT_0 = 0x0


def _is_windows() -> bool:
    return sys.platform == "win32"


def rule_matches(exe_path: Path) -> bool:
    """已經有一條 `name=RULE_NAME`、`dir=in`、放行、且指向 `exe_path` 的規則？

    查不到 netsh／查詢失敗一律回 `True`——寧可少提示一次，也不要因為查詢本身有問題
    就一直煩使用者。非 Windows 也回 `True`（沒有這個問題）。"""
    if not _is_windows():
        return True
    try:
        result = subprocess.run(
            ["netsh", "advfirewall", "firewall", "show", "rule", f"name={RULE_NAME}", "dir=in", "verbose"],
            capture_output=True, text=True,
            # netsh advfirewall 在現代 Windows 一律輸出 UTF-8——`text=True` 預設用系統
            # 語系（繁中＝cp950）解碼會 UnicodeDecodeError，`result.stdout` 變 None，
            # 讀取執行緒還會拋未捕捉例外（2026-09-08 firewall UAC e2e 抓到）。強制 UTF-8
            # ＋ errors="replace"：要找的 exe 路徑是純 ASCII，就算輸出不是 UTF-8 也還在。
            encoding="utf-8", errors="replace",
            timeout=15, creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        logger.debug("查詢防火牆規則失敗", exc_info=True)
        return True
    if result.returncode != 0:
        return False  # 「沒有規則符合指定的準則」
    # verbose 輸出的「程式:」欄位標籤會隨系統語系不同，改用「完整路徑字串有沒有出現在
    # 輸出裡」判斷（路徑本身不會被在地化）。規則存在但指向舊路徑（資料夾被搬過）→ 回
    # False，呼叫端會重新建一條正確的。
    needle = str(exe_path).casefold()
    return any(needle in line.casefold() for line in (result.stdout or "").splitlines())


def ensure_rule(exe_path: Path, *, timeout_s: float = 45.0) -> bool:
    """規則已存在就直接回 `True`。否則跳一次 UAC 用 `netsh` 建規則、**等它做完**（最多
    `timeout_s` 秒），回傳「現在規則到底建起來了沒」。

    用 `ShellExecuteExW`（`runas` 動詞）叫 `netsh.exe`——`netsh.exe` 是 Windows 內建、
    微軟簽章的，UAC 對話框顯示的是可信的程式名；不用 `cmd.exe`（顯示成「Windows 命令
    處理器」比較嚇人）。同名舊規則不先刪：`rule_matches()` 會擋著，這條 `add` 一輩子
    最多跑一兩次（首次 + 資料夾被搬過），頂多留一條指向舊路徑的無害規則。"""
    if not _is_windows():
        return True
    if rule_matches(exe_path):
        return True

    import ctypes
    from ctypes import wintypes

    class _SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", ctypes.c_ulong),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HINSTANCE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HKEY),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    params = (
        f'advfirewall firewall add rule name="{RULE_NAME}" dir=in action=allow'
        f' program="{exe_path}" enable=yes profile=any'
    )
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    # 明確宣告型別——handle 是指標寬度，用預設的 c_int 在 64-bit 下有截斷風險。
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_SHELLEXECUTEINFOW)]
    shell32.ShellExecuteExW.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    info = _SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = "netsh.exe"
    info.lpParameters = params
    info.nShow = 0  # SW_HIDE
    try:
        ok = shell32.ShellExecuteExW(ctypes.byref(info))
    except OSError:
        logger.warning("叫起防火牆設定的提權程序失敗", exc_info=True)
        return rule_matches(exe_path)
    if not ok or not info.hProcess:
        # 使用者在 UAC 按「否」→ ShellExecuteExW 失敗（GetLastError=ERROR_CANCELLED 1223）
        return rule_matches(exe_path)
    try:
        kernel32.WaitForSingleObject(info.hProcess, int(max(1.0, timeout_s) * 1000))
    finally:
        kernel32.CloseHandle(info.hProcess)
    return rule_matches(exe_path)
