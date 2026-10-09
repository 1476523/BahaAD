"""youranimes.tw（你的動畫）番劇資料補充。規格見 docs/requirements/youranimes.md。

動畫瘋的番劇詳細頁簡介常常很短，也完全沒有製作／配音／音樂資訊。這個套件每小時抓
youranimes.tw 的季度頁（當季 + 前 2 季），解析後存本地 DB；web 層在 render 番劇詳細頁
時用週期表的原始中文標題去配對，配到就用 youranimes 的簡介、右側多一欄製作／配音／音樂，
配不到或缺欄位就退回動畫瘋原資料。

**完全不碰 `gamer_client/`**：youranimes 不是動畫瘋資料，抓取用自己的無 cookie session
（見 fetch.py，`gamer_client_session.md`「單一 session」規則的刻意例外）。
"""

from __future__ import annotations

from bahaad.youranimes.fetch import YourAnimesError, YourAnimesFetcher
from bahaad.youranimes.match import match_record
from bahaad.youranimes.models import (
    YourAnimesCastMember,
    YourAnimesMusic,
    YourAnimesRecord,
    YourAnimesStaff,
)
from bahaad.youranimes.parse import parse_season_page
from bahaad.youranimes.season import youranimes_season_slugs

__all__ = [
    "YourAnimesError",
    "YourAnimesFetcher",
    "YourAnimesRecord",
    "YourAnimesStaff",
    "YourAnimesCastMember",
    "YourAnimesMusic",
    "parse_season_page",
    "youranimes_season_slugs",
    "match_record",
]
