"""BahaAD 進入點。

真正的啟動流程在 bahaad/app_shell.py：組裝 Phase 0～3 所有服務物件、啟動背景排程
執行緒、起網頁伺服器、顯示系統匣圖示。這裡只是入口，不放任何組裝邏輯。
"""

from __future__ import annotations

from bahaad.app_shell import run


def main() -> None:
    run()


if __name__ == "__main__":
    main()
