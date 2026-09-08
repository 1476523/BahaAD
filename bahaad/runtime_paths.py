"""執行期路徑工具：拿「正在執行的這支 exe」的真實路徑、判斷是不是打包產物。

集中在這裡是因為 `sys.executable` 對 **Nuitka standalone 不可靠**——實測會指向打包
那台機器上的 `python.exe`（使用者機器上不存在），`subprocess.Popen` 直接
`FileNotFoundError: [WinError 2]`（系統匣「重啟工具」踩到的）。而且 `sys.frozen`
**不會被 Nuitka 設**（那是 PyInstaller 的慣例）。

`app_shell`（重啟工具）跟 `updater/applier`（安裝目錄、重啟用的 exe 路徑）都用同一份。
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path


def is_compiled() -> bool:
    """目前是不是「打包成單一 exe」的執行環境（Nuitka `--standalone` 或 PyInstaller）。
    Nuitka 會在每個編譯過的模組注入 `__compiled__` 全域名稱——包含這個模組。"""
    return "__compiled__" in globals() or getattr(sys, "frozen", False)


def own_exe_path() -> Path:
    """正在執行的這支 exe（打包後＝BahaAD.exe，開發模式＝python.exe）的完整路徑。

    用 Windows `GetModuleFileNameW(NULL)`——不管怎麼被啟動、不管目前工作目錄都回正確
    路徑。拿不到（非 Windows／API 出錯）才退回 `sys.executable`。
    """
    try:
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetModuleFileNameW(None, buffer, len(buffer)):
            return Path(buffer.value)
    except Exception:  # noqa: BLE001 - 退回 sys.executable，best effort
        pass
    return Path(sys.executable)
