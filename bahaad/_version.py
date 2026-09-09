# 單一版本號來源：updater/ 比對 minimum_required_version、UI 顯示版本號都讀這裡，
# 不要在別的地方另外寫死版本字串。
#
# 版本方案（見 docs/decisions/0001「Beta 期間版本方案」）：主版本 0 = Beta（GitHub
# Release 標 prerelease）；第一個正式版是 1.0.0。Beta 期間修 bug 走 patch bump
# （0.0.2…），功能里程碑走 minor bump（0.1.0…），都仍是 Beta。
__version__ = "0.0.2"


def is_beta(version: str = __version__) -> bool:
    """主版本為 0 就是 Beta。1.0.0 起自動回 False，side-bar 的 BETA 徽章隨之消失。"""
    head = version.lstrip("vV").split(".", 1)[0]
    return head == "0"


def build_tag() -> str:
    """打包時嵌入的 git commit 短雜湊（`scripts/build.py` 產生 `bahaad/_build_info.py`）。
    開發模式從原始碼跑時沒有這個檔 → 回空字串。**只用於顯示**，不能拿去跟 manifest
    版本比對（那個一定是純 `__version__`）。"""
    try:
        from bahaad._build_info import BUILD_COMMIT

        return str(BUILD_COMMIT or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def version_label() -> str:
    """UI 顯示用：`0.0.1` 或 `0.0.1 (504e31c)`（打包版帶 commit 短雜湊，方便回報問題
    時對得上是哪一版 build——使用者 2026-09-09）。"""
    tag = build_tag()
    return f"{__version__} ({tag})" if tag else __version__
