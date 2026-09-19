"""公開模式的「可見番劇」設定（使用者 2026-09-04）。

`public_mode` 開著時，**預設所有訂閱／已下載的番劇（含之後新增的）訪客都看得到**；
使用者可以在訂閱列表／下載列表的番劇卡片上取消勾選「公開」，把個別番劇藏起來——被
藏起來的番劇原始站方標題記在 `public_hidden`（denylist）。key 用番劇的**原始站方標題**
（`anime_title`），因為訂閱／已下載／番劇頁三邊都對得上這個值。

勾選框預設是勾的（＝公開）；取消勾選才會把那部加進 `public_hidden`。
"""

from __future__ import annotations

from typing import Any


def hidden_titles(settings: Any) -> set[str]:
    return {t.strip() for t in (settings.get("public_hidden", []) or []) if t and t.strip()}


def title_allowed(settings: Any, title: str | None) -> bool:
    """公開模式關著 → 一律 True（本來就要登入）。開著 → 預設 True，只有被使用者取消
    勾選（列在 `public_hidden`）的番劇才擋。查不到標題（None）保守起見擋掉。"""
    if not settings.get("public_mode", False):
        return True
    if title is None:
        return False
    return title.strip() not in hidden_titles(settings)


def set_title_public(settings: Any, title: str, public: bool) -> list[str]:
    """設定一部番劇（原始標題）在公開模式下要不要給訪客看，回傳更新後的隱藏清單。
    `public=True` → 移出 denylist（＝公開，預設狀態）；`False` → 加進 denylist。"""
    title = (title or "").strip()
    hidden = hidden_titles(settings)
    if not title:
        return sorted(hidden)
    if public:
        hidden.discard(title)
    else:
        hidden.add(title)
    ordered = sorted(hidden)
    settings.update({"public_hidden": ordered})
    return ordered
