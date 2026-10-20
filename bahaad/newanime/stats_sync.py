"""新番收藏統計的對帳（使用者 2026-10-20）。

排行榜「新番收藏數」用虛構 sn 當 key。使用者在「新番消失提示」按「不再追蹤」只移出本機追蹤清單，
沒通知統計把收藏取消，伺服器上那筆 `favorite=1` 就永遠留著——官方改名／改時間後舊的虛構項目
下架，排行榜卻還掛著它（沒封面、連到已下架的新番頁，例如《轉生哥布林有什麼問題嗎？》）。

- 新做法：「不再追蹤」當下就送 `favorite=0`（`web/notifications.py`）。
- 這裡是一次性補救：本機已下架、已提示過消失、且現在沒在追蹤的項目，補送一次 `favorite=0`。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_DONE_KEY = "_stale_newanime_favorites_cleared"


def clear_stale_favorites(store, collector, settings) -> int:
    """回傳補送幾筆。只跑一次（設定旗標）；任何失敗都不影響啟動。"""
    try:
        if settings.get(_DONE_KEY):
            return 0
        tracked = store.list_tracked()
        sent = 0
        for bulletin in store.list_bulletins():
            for item in store.list_items(bulletin["season_key"]):
                if (
                    item.get("stage") == "removed"
                    and item.get("disappeared_prompt_at")
                    and item["virtual_sn"] not in tracked
                ):
                    name = item.get("display_name") or item["source_name"]
                    collector.record_favorite(item["virtual_sn"], name, favorited=False)
                    sent += 1
        settings.update({_DONE_KEY: True})
        if sent:
            logger.info("新番快訊：已補送 %d 筆「已不再追蹤」的收藏統計更正", sent)
        return sent
    except Exception:  # noqa: BLE001
        logger.debug("新番收藏統計對帳失敗", exc_info=True)
        return 0
