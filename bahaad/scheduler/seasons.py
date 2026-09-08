"""依首播日期把番劇分到「一月／四月／七月／十月新番」的季別資料夾。

使用者要求「下載時把每部番劇放進季別資料夾」（例 `下載目錄/2026/七月新番/番劇名/…`）。
分類靠 `video.php` 回傳的 `anime.seasonStart`（`YYYY/MM/DD`，`gamer_client/catalog.py`
會塞進 `VideoInfo.season_start`）或詳細頁的「首播日期」（`AnimeDetail.air_date`，排程
清單的提示用）。

寬限窗口（使用者定案 14 天）：不少番劇會在季別交界前一兩週就更新首集（例如「7 月番」
其實 6 月下旬就開播）。做法是把首播日期先往後加 14 天、再取日曆季——Dec→Jan 的跨年
也自然成立。

**沒有任何 `bahaad.*` import**，避免跟 `main_loop` / `web.settings` / `web.schedule`
產生循環。
"""

from __future__ import annotations

import re
from datetime import date, timedelta

# 設定預設值（`web/settings.py` 與 `scheduler/main_loop.py` 都從這裡讀）
DEFAULT_AUTO_SEASON_FOLDER = True
DEFAULT_AUTO_SEASON_YEAR = True

_GRACE_DAYS = 14
_SEASON_LABELS = {1: "一月新番", 4: "四月新番", 7: "七月新番", 10: "十月新番"}
_DATE_SPLIT = re.compile(r"[/\-.]")


def _parse_date(raw: str) -> date | None:
    """接受 `YYYY/MM/DD`、`YYYY-MM-DD`、`YYYY.MM.DD`，或只有 `YYYY/MM`（日補 1）。
    解析不出來（空字串、亂碼）回 `None`。"""
    parts = _DATE_SPLIT.split((raw or "").strip())
    try:
        if len(parts) == 2:
            year, month, day = int(parts[0]), int(parts[1]), 1
        elif len(parts) >= 3:
            year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        else:
            return None
        return date(year, month, day)
    except (ValueError, TypeError):
        return None


def season_folder(
    season_start: str,
    *,
    total_episode: int | None = None,
    include_year: bool = True,
) -> str:
    """回傳季別資料夾字串：

    - `"2026/七月新番"`（`include_year=True`，`/` 分隔＝呼叫端會拆成巢狀資料夾）
    - `"七月新番"`（`include_year=False`）
    - `""` —— 算不出首播日期，或 `total_episode <= 1`（獨立特別篇／電影，使用者定案的例外）
    """
    if total_episode is not None and total_episode <= 1:
        return ""
    parsed = _parse_date(season_start)
    if parsed is None:
        return ""
    bumped = parsed + timedelta(days=_GRACE_DAYS)
    season_month = ((bumped.month - 1) // 3) * 3 + 1  # -> 1 / 4 / 7 / 10
    label = _SEASON_LABELS[season_month]
    return f"{bumped.year}/{label}" if include_year else label
