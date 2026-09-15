"""用 Nuitka 把 BahaAD 打包成 standalone 執行檔。

跑法： .venv\\Scripts\\python.exe scripts/build.py

產出： build_nuitka/main.dist/BahaAD.exe（資料夾形式，見 docs/decisions/0002-nuitka-packaging.md）

選項清單與實測遇到的相容性處理見 `docs/decisions/0002`「正式打包實測結果」。
版本號從 bahaad.__version__ 讀，不在這裡另外寫死。
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from bahaad import __version__  # noqa: E402

# Nuitka 的 --file-version / --product-version 要求純數字 X.Y.Z.W
_FILE_VERSION = (__version__.lstrip("vV").split("-")[0] + ".0")


def build_command() -> list[str]:
    return [
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        # 整個 bahaad 套件全包——app_shell.py 等處有「函式內延遲 import」，Nuitka 的
        # 靜態追蹤不一定每個都跟得到，漏掉就會在執行期靜默走 except 分支（v0.0.1 首個
        # 打包版就這樣把日誌鏡射整包漏掉）。自家程式碼很小，全包成本可忽略。
        "--include-package=bahaad",
        # 授權後端 `bahaad/access_gate_server/`（獨立部署、不隨用戶端散布）用戶端從不
        # import——不要編進 BahaAD.exe：省體積，也讓散布的二進位對應原始碼就是公開
        # repo 那份（見 docs/publishing.md）。
        "--nofollow-import-to=bahaad.access_gate_server",
        "--windows-console-mode=disable",
        # 主程式用不到 tkinter（系統匣是 pystray + PIL；tk-inter 只有 access_gate_server
        # 的設定視窗要）。Nuitka anti-bloat 沒擋乾淨，手動排除省掉 _tkinter.pyd +
        # tcl86t.dll + tk86t.dll（~7 MB、3 個檔）。
        "--nofollow-import-to=tkinter",
        "--noinclude-dlls=tcl86t.dll",
        "--noinclude-dlls=tk86t.dll",
        f"--windows-icon-from-ico={ROOT / 'bahaad' / 'assets' / 'app.ico'}",
        "--company-name=TOC (toc.icu)",
        "--product-name=BahaAD",
        f"--file-version={_FILE_VERSION}",
        f"--product-version={_FILE_VERSION}",
        "--file-description=Bahamut Animation Downloader",
        "--output-filename=BahaAD.exe",
        "--output-dir=build_nuitka",
        # web/ 的樣板與靜態檔 Flask 執行期直接讀磁碟，不編譯（見 0002 範圍段）
        "--include-data-dir=bahaad/web/templates=bahaad/web/templates",
        "--include-data-dir=bahaad/web/static=bahaad/web/static",
        # 自我更新小幫手指令碼，維持原樣打包（見 docs/requirements/updater.md）
        "--include-data-file=bahaad/updater/apply_update.ps1=bahaad/updater/apply_update.ps1",
        # 「查看隱私權說明」按鈕即時讀這份 md（web/settings.privacy_policy），要打進 dist
        "--include-data-file=docs/PRIVACY_POLICY.md=docs/PRIVACY_POLICY.md",
        # 品牌圖示（系統匣圖示 load_tray_icon_image() 讀這個）
        "--include-package-data=bahaad.assets",
        # curl_cffi 帶原生 libcurl / cacert，Nuitka 不會自動抓齊
        "--include-package=curl_cffi",
        "--include-package-data=curl_cffi",
        # waitress（WSGI 伺服器）內部有動態 import，整包帶進去
        "--include-package=waitress",
        "main.py",
    ]


def _write_build_info() -> None:
    """把 git commit 短雜湊寫進 `bahaad/_build_info.py`（gitignored），打包後
    側欄版本號會顯示成 `0.0.1 (504e31c)`，方便使用者回報問題時對得上是哪一版
    build（使用者 2026-09-09）。開發模式從原始碼跑時沒有這個檔 → 只顯示版本號。"""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True
        ).strip()
    except Exception:  # noqa: BLE001 - 不是 git 環境就跳過
        return
    dirty = subprocess.call(["git", "diff", "--quiet"], cwd=ROOT) != 0
    tag = f"{commit}+dirty" if dirty else commit
    (ROOT / "bahaad" / "_build_info.py").write_text(
        f'# 由 scripts/build.py 打包時產生；gitignored。\nBUILD_COMMIT = "{tag}"\n',
        encoding="utf-8",
    )
    print(f"build info：commit {tag}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Nuitka 打包 BahaAD")
    parser.add_argument(
        "--zip", action="store_true",
        help="編譯後把 main.dist/ 壓成 build_nuitka/BahaAD-<版本>-win64.zip（掛 Release 用）",
    )
    args = parser.parse_args()

    _write_build_info()

    cmd = build_command()
    print("BahaAD", __version__, "→ Nuitka standalone")
    print(" ".join(cmd), "\n")
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    dist = ROOT / "build_nuitka" / "main.dist"
    print(f"\n完成：{dist / 'BahaAD.exe'}")
    print("驗證清單見 docs/decisions/0002「正式打包實測結果」。")

    if args.zip:
        # zip 裡的檔案包在 BahaAD/ 資料夾底下，使用者隨便解壓到哪都不會散一地
        staging = ROOT / "build_nuitka" / "_zip_staging" / "BahaAD"
        if staging.parent.exists():
            shutil.rmtree(staging.parent)
        shutil.copytree(dist, staging)
        stem = ROOT / "build_nuitka" / f"BahaAD-{__version__}-win64"
        archive = shutil.make_archive(str(stem), "zip", root_dir=staging.parent)
        shutil.rmtree(staging.parent)
        print(f"壓縮包：{archive}")


if __name__ == "__main__":
    main()
