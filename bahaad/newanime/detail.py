"""`/newanime/<virtual_sn>` 詳細頁的 view-model。規格 §6c。

比照現有番劇詳細頁，但欄位精簡：只有 GNN + seasonal.php + youranimes 補來的那幾項，
沒有代理廠商／集數／相關動畫。seasonal.php / youranimes 還沒補到的欄位一律顯示
**「待確認」**（`_UNCONFIRMED`），下一輪 `NewAnimeWatcher.enrich_once()` 會補上。
"""

from __future__ import annotations

from typing import Mapping

from bahaad.newanime.listing import update_label

_UNCONFIRMED = "待確認"


def _slash_date(iso: str | None) -> str:
    """`2026-10-05` → `2026/10/05`（跟原詳細頁一樣只到「年/月/日」）。"""
    if not iso:
        return "－"
    parts = iso.split("-")
    return "/".join(parts) if len(parts) == 3 else iso


_POST_AIR_NOTE = {
    "converted": "這部已經上架，已自動為你轉成訂閱、開始下載——可到「訂閱列表」查看。",
    "catching_up": "這部已經上架、已轉成訂閱，正在補齊首播的多集內容。",
    "aired": "這部已經上架。當初沒有追蹤，可以到「搜尋番劇」找到它、點鈴鐺訂閱。",
}


def build_detail(item: Mapping, *, tracked: bool) -> dict:
    tags = [t.strip() for t in (item.get("tags") or "").split("\n") if t.strip()]
    stage = item.get("stage")
    return {
        "virtual_sn": item["virtual_sn"],
        "source_name": item["source_name"],
        "display_name": item.get("display_name") or item["source_name"],
        "is_renamed": bool(item.get("display_name")),
        "air_date": _slash_date(item.get("first_air_date")),
        "update_label": update_label(item),
        "director": item.get("director") or _UNCONFIRMED,
        "studio": item.get("studio") or _UNCONFIRMED,
        "tags": tags,
        "tags_unconfirmed": not tags,
        "subscribe_count": item.get("subscribe_count") or _UNCONFIRMED,
        "seasonal_range": item.get("seasonal_range") or "",
        "cover_url": item.get("cover_url") or "",
        "is_vip": bool(item.get("is_vip")),
        "region_locked": bool(item.get("region_locked")),
        "tracked": tracked,
        "stage": stage,
        "post_air_note": _POST_AIR_NOTE.get(stage),
        "is_post_air": stage in _POST_AIR_NOTE,
    }
