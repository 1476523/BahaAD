"""線上差異更新機制。規格見 docs/requirements/updater.md。

跟 `scheduler/version_check.py`（只查 GitHub Release 提醒使用者有新版本可看）是完全獨立
的兩條路——這裡才是真的會抓 manifest、驗簽章、下載檔案、改動安裝目錄內容的機制。

`manifest.py` 抓 manifest.json＋驗證 ed25519 簽章；`policy.py` 判斷這次更新是「沒有更新／
一般更新／重點更新」，並提供背景執行緒 `UpdateCoordinator` 定期檢查；`fetcher.py` 逐檔
下載＋雜湊比對；`applier.py` 啟動獨立的 PowerShell 小幫手行程 `apply_update.ps1`，等主
程式結束後整批覆蓋安裝目錄、重啟（2026-08-28 起所有差異更新都走這條、一律要重啟）。
"""

from __future__ import annotations

# 暫存目錄名稱：`{data_dir}\update_staging\`，不是安裝目錄底下——安裝目錄在某些部署情境
# 可能是唯讀的，資料目錄本來就保證可寫。app_shell.py（組裝 UpdateCoordinator）跟
# web/dashboard.py（套用更新按鈕）都要算出同一個路徑，共用這個常數避免兩邊各自硬編碼。
UPDATE_STAGING_DIRNAME = "update_staging"
