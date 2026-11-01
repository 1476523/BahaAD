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
_DATE_PREFIX = re.compile(r"\s*(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})")
# 這一集上架日距離番劇首播日超過這麼多天，視為「接著舊番集數的新季度」（上一季沒播完、
# 下一季接著播，同一個番劇頁／同一個 seasonStart），改依這一集自己的上架日分季別。一般
# 12～13 集的番劇最後一集大約在首播後 80～100 天內，不會誤判（使用者 2026-10-06）。
_CARRYOVER_DAYS = 120
# `total_episode <= 1` 的「電影／特別篇」例外只對「首播已經有一陣子」的作品成立：剛首播的
# 連續劇第一集也是 totalEpisode=1，沒有訂閱項目（手動下載、新番轉換出問題時）會被誤判成
# 電影而下載到季別資料夾外（使用者 2026-10-06）。首播在這個天數內視為新番。
_NEW_SERIES_DAYS = 45


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


def _parse_air_date(raw: str) -> date | None:
    match = _DATE_PREFIX.match(raw or "")
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def season_folder(
    season_start: str,
    *,
    total_episode: int | None = None,
    include_year: bool = True,
    air_date: str = "",
    today: date | None = None,
) -> str:
    """回傳季別資料夾字串：

    - `"2026/七月新番"`（`include_year=True`，`/` 分隔＝呼叫端會拆成巢狀資料夾）
    - `"七月新番"`（`include_year=False`）
    - `air_date`（這一集的上架日）給了，而且比 `season_start` 晚超過 `_CARRYOVER_DAYS` 天
      ＝接著舊番集數的新季度，改用這一集的上架日分季別（例：四月首播、十月才接著播第 13 話
      的番劇，十月更新的集數進「十月新番」）。
    - `""` —— 算不出首播日期，或 `total_episode <= 1`（獨立特別篇／電影，使用者定案的例外）
    """
    parsed = _parse_date(season_start)
    if total_episode is not None and total_episode <= 1:
        recent = parsed is not None and 0 <= ((today or date.today()) - parsed).days <= _NEW_SERIES_DAYS
        if not recent:
            return ""
    if parsed is None:
        return ""
    aired = _parse_air_date(air_date)
    if aired is not None and (aired - parsed).days >= _CARRYOVER_DAYS:
        parsed = aired
    bumped = parsed + timedelta(days=_GRACE_DAYS)
    season_month = ((bumped.month - 1) // 3) * 3 + 1  # -> 1 / 4 / 7 / 10
    label = _SEASON_LABELS[season_month]
    return f"{bumped.year}/{label}" if include_year else label
