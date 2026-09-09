"""番劇標題清理。規格見 docs/requirements/round7.md 第 8、15 項。

站方的番劇標題常帶結尾標記——集數 `[12]`、`[特別篇]`、`[電影]`、`[OVA]` 等。BahaAD 用
標題當資料夾名 / 檔名 / 通知的 `@anime_title@`，這些標記留著會很醜（甚至跟檔名模板的
`[@episode_num@]` 疊成 `[12] [12]`）。一律去掉**所有結尾的 `[...]`**（不管裡面是不是數字）。

只處理站方回來的標題字串——**不要**拿去套使用者的 `filename_template`（那裡的
`[@episode_num@]` 要保留）。
"""

from __future__ import annotations

import re

_TRAILING_TAG_RE = re.compile(r"\s*\[[^\]]*\]\s*$")
# 站方對「番劇內的特別篇」單集，`video.title` 會帶結尾 `[特別篇]`（實測 sn 45523 =
# 「徹夜之歌 Season 2 [1] [特別篇]」）。集數本身（`episodes[季][i].episode`）只是普通
# 數字 `1`，所以要靠標題這個標記才知道它是特別篇 → 檔名用 SP 編號。
_SPECIAL_TITLE_RE = re.compile(r"\[\s*特別篇\s*\]\s*$")


def clean_anime_title(title: str) -> str:
    out = (title or "").strip()
    prev = None
    while out != prev:
        prev = out
        out = _TRAILING_TAG_RE.sub("", out).strip()
    return out or (title or "").strip()


def is_special_episode_title(video_title: str) -> bool:
    """`video.title` 結尾帶 `[特別篇]` → 這一集是番劇內的特別篇。"""
    return bool(_SPECIAL_TITLE_RE.search(video_title or ""))
