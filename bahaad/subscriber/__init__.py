"""訂閱者 Discord/Telegram 通知（Phase 1：中繼綁定基礎建設）。規格見
docs/requirements/subscriber_notify.md。

跟 `bahaad/access_gate_server/` 是兩個獨立方向的通訊端點：那邊是中繼伺服器本身
（維護者集中部署，不隨 `main.exe` 散布），這裡是**安裝實例**（`main.exe`）呼叫中繼
的客戶端程式碼，比照 `bahaad/access_gate/`（GitHub 帳號連結客戶端）同樣的角色劃分。
"""
