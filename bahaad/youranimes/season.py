"""youranimes.tw 季度頁的 slug（`YYYYMM`，MM ∈ {01,04,07,10}）運算。

季別語意跟 `scheduler/seasons.py` 單一來源——一樣的「季月 = ((month-1)//3)*3+1」＋
14 天寬限窗（不少番劇在季別交界前一兩週就開播，youranimes 也照這個把頁面歸季）。
"""

from __future__ import annotations

from datetime import date, timedelta

from bahaad.scheduler.seasons import _GRACE_DAYS


def youranimes_season_slugs(today: date, count: int = 3) -> list[str]:
    """回傳「當季 + 前 count-1 季」的 slug，例 `["202607", "202604", "202601"]`。

    抓多季是為了涵蓋橫跨兩季的連續放送番劇（週期表裡都是正在播的，多半落在當季或
    前一兩季）。
    """
    bumped = today + timedelta(days=_GRACE_DAYS)
    month = ((bumped.month - 1) // 3) * 3 + 1  # -> 1 / 4 / 7 / 10
    year = bumped.year
    slugs: list[str] = []
    for _ in range(max(count, 1)):
        slugs.append(f"{year}{month:02d}")
        month -= 3
        if month < 1:
            month += 12
            year -= 1
    return slugs
