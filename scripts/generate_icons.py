"""從單一來源圖 `圖示/BahaAD.png` 產生所有衍生圖示檔。

跑法： python scripts/generate_icons.py

來源資料夾 `圖示/` 是 gitignored（見 memory「Icon source folder」），所以來源圖本身
不進版控——這支腳本產生「可提交的多份衍生檔」，產出一併 commit。使用者日後換 Logo
只要覆蓋 `圖示/BahaAD.png` 後重跑這支腳本即可（冪等）。

產出：
- bahaad/assets/BahaAD.png        512×512  系統匣圖示
- bahaad/assets/app.ico           多尺寸   Nuitka --windows-icon-from-ico（exe 圖示）
- bahaad/web/static/icons/logo.png 256×256 網頁 side-bar brand-logo
- bahaad/web/static/favicon.ico    16/32/48 網頁瀏覽器分頁圖示

BahaAD.png 是這個工具的唯一 Logo：工具圖示、網頁 Logo、網頁 favicon、公開 repo 都用這張。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "圖示" / "BahaAD.png"

ASSETS_DIR = ROOT / "bahaad" / "assets"
WEB_ICONS_DIR = ROOT / "bahaad" / "web" / "static" / "icons"
WEB_STATIC_DIR = ROOT / "bahaad" / "web" / "static"

ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
FAVICON_SIZES = [(16, 16), (32, 32), (48, 48)]


def _load_source() -> Image.Image:
    if not SOURCE.exists():
        raise SystemExit(f"找不到來源圖：{SOURCE}\n請把 Logo 放到這個路徑後再跑一次。")
    return Image.open(SOURCE).convert("RGBA")


def _square_png(img: Image.Image, size: int, dest: Path) -> None:
    resized = img.resize((size, size), Image.LANCZOS)
    dest.parent.mkdir(parents=True, exist_ok=True)
    resized.save(dest, format="PNG")
    print(f"  {dest.relative_to(ROOT)}  ({size}x{size})")


def _ico(img: Image.Image, sizes: list[tuple[int, int]], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    # 先縮到最大尺寸再交給 Pillow 產生多解析度 .ico，避免它從原尺寸直接降採樣鋸齒
    base = img.resize(sizes[-1], Image.LANCZOS)
    base.save(dest, format="ICO", sizes=sizes)
    print(f"  {dest.relative_to(ROOT)}  ({', '.join(f'{w}' for w, _ in sizes)})")


def main() -> None:
    src = _load_source()
    print(f"來源：{SOURCE.relative_to(ROOT)}  {src.size}  {src.mode}")
    print("產出：")
    _square_png(src, 512, ASSETS_DIR / "BahaAD.png")
    _ico(src, ICO_SIZES, ASSETS_DIR / "app.ico")
    _square_png(src, 256, WEB_ICONS_DIR / "logo.png")
    _ico(src, FAVICON_SIZES, WEB_STATIC_DIR / "favicon.ico")
    print("完成。記得 git add 這些產出檔。")


if __name__ == "__main__":
    main()
