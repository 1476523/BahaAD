"""套用差異更新。規格見 docs/requirements/updater.md「重要架構修正」一節。

2026-08-28：更新策略收斂成兩級，所有差異更新一律走「獨立小幫手行程」自我更新模式
（原本 `core=false` 檔案的「不重啟熱套用」已移除——正式版 Flask 會快取編譯後的樣板，
熱套用其實不生效）。`launch_apply_and_restart()` 啟動一支不受主程式生命週期綁定的
PowerShell 小幫手（`apply_update.ps1`），主程式接著自己優雅關閉退出，小幫手等主程式
行程真的結束（Windows 不允許覆蓋還在執行中的 .exe）才整批覆蓋安裝目錄、重新啟動主程式。

2026-09-01：`install_root()`／重啟用的 exe 路徑改走 `bahaad.runtime_paths`——Nuitka
standalone 的 `sys.executable` 會指向打包機的 `python.exe`（系統匣「重啟工具」踩到
`FileNotFoundError`），改用 `GetModuleFileNameW(NULL)` 拿正在執行的 exe 真實路徑。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from bahaad.runtime_paths import is_compiled, own_exe_path

_APPLY_SCRIPT_NAME = "apply_update.ps1"


def install_root() -> Path:
    """打包後（Nuitka `--standalone` / PyInstaller）回傳 exe 所在資料夾（＝整個安裝
    目錄，`apply_update.ps1` 就是整批覆蓋這裡）；開發模式（`python main.py`）回傳專案
    根目錄。"""
    if is_compiled():
        return own_exe_path().parent
    return Path(__file__).resolve().parent.parent.parent


def launch_apply_and_restart(install_root: Path, staging_dir: Path, exe_path: Path) -> None:
    """啟動獨立的 PowerShell 小幫手行程（不是子行程——主程式結束後它要能繼續活著），
    不等待、立刻回傳。呼叫端（`web/dashboard.py`／`app_shell.py`）接著自己跑優雅關閉
    流程。"""
    script_path = Path(__file__).resolve().parent / _APPLY_SCRIPT_NAME
    # CREATE_NO_WINDOW (not DETACHED_PROCESS): a no-console main process (Nuitka
    # --windows-console-mode=disable) launching `powershell.exe -File` with
    # DETACHED_PROCESS makes PowerShell exit immediately without running the script
    # (it needs a console). CREATE_NO_WINDOW gives it a hidden console so the script
    # actually runs; the child is still independent and outlives the main process.
    subprocess.Popen(
        [
            "powershell.exe",
            "-ExecutionPolicy",
            "Bypass",
            "-NonInteractive",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script_path),
            "-MainPid",
            str(os.getpid()),
            "-StagingDir",
            str(staging_dir),
            "-InstallDir",
            str(install_root),
            "-ExePath",
            str(exe_path),
        ],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
    )
