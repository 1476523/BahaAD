"""開首頁時「直接用快取 vs 重抓一次」的決策。純函式，方便單元測試。

規格見 docs/requirements/anime_cache.md「觸發 3」：

- 觸發點：開啟首頁，以及停在首頁時 `home_auto_refresh.js` 在時段過後自動重載（.0 改進.txt 第 21 項）。
- 平時兩次「因開首頁而重抓」之間最短間隔 30 分鐘。
- 若某個週期表時段在 40 分鐘內就要到 → 這次作廢（等時段過了再說，免得白抓一次舊資料
  又馬上要再抓一次新的）。
- **有時段在「上次抓取之後、現在之前」經過**（＝那個時段的新集數還沒抓到）→ 該時段
  過後 3 分鐘就允許重抓，**不受 30 分鐘最短間隔限制**（.0 改進.txt 第 20 項：使用者
  在時段剛過想看新集數時進首頁，不該再拿 40 分鐘的舊快取）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

MIN_INTERVAL = timedelta(minutes=30)
PRE_SLOT_BLACKOUT = timedelta(minutes=40)
# 時段過後多久，站方資料通常已更新、可以重抓（前端 home_auto_refresh.js 用同一個值）
POST_SLOT_DELAY = timedelta(minutes=3)


def _slot_datetimes(now: datetime, slots: list[tuple[int, str]]) -> list[datetime]:
    """把 (day_order 1=週一…7=週日, "HH:MM") 展開成 now 前後一週的具體 datetime。"""
    out: list[datetime] = []
    for day_order, hhmm in slots:
        try:
            hh_s, mm_s = str(hhmm).split(":")
            hh, mm = int(hh_s), int(mm_s)
        except (ValueError, AttributeError):
            continue
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            continue
        anchor = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        for week_offset in (-1, 0, 1):
            days_delta = (day_order - now.isoweekday()) + 7 * week_offset
            out.append(anchor + timedelta(days=days_delta))
    return sorted(out)


def home_refresh_decision(
    now: datetime,
    last_fetched_at: datetime | None,
    slots: list[tuple[int, str]],
) -> bool:
    """True＝這次開首頁要重抓 get_newanime()+get_weekly_schedule()；False＝直接渲染快取。"""
    if last_fetched_at is None:
        return True

    slot_dts = _slot_datetimes(now, slots)

    # 有時段在「上次抓取之後、現在之前」經過 → 那個時段的新集數還沒抓到。時段過後
    # 3 分鐘就重抓，優先於下面的「時段前作廢」與 30 分鐘最短間隔（.0 改進.txt 第 20 項）
    passed = [s for s in slot_dts if last_fetched_at < s <= now]
    if passed and now >= max(passed) + POST_SLOT_DELAY:
        return True

    next_slot = next((s for s in slot_dts if s > now), None)
    if next_slot is not None and next_slot - now <= PRE_SLOT_BLACKOUT:
        return False  # 週期表即將更新、資料還沒好，這次作廢

    return now >= last_fetched_at + MIN_INTERVAL
