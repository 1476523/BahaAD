"""隨套件打包的二進位素材（品牌圖示）。

內容由 `scripts/generate_icons.py` 從 `圖示/BahaAD.png` 產生：

- `BahaAD.png`  — 512×512，系統匣圖示（`app_shell.load_tray_icon_image()`）
- `app.ico`     — 多尺寸，Nuitka `--windows-icon-from-ico`（exe 圖示）

執行期用 `importlib.resources.files("bahaad.assets")` 取用，不要用相對路徑。
"""
