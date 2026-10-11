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

# 結尾標記若剛好是純數字集數（例："...第三季 [25]"），代表這一頁本身就是某一集——
# 跟上面 _TRAILING_TAG_RE 一起用：clean_anime_title 拿掉整個標記當番劇名，這裡單獨
# 抓出數字本身給「這集沒有 section.season 集數清單時」的集數顯示用（round 8：
# 剛首播、站方還沒建好整季清單的集數，[N] 標記是唯一還查得到集數的地方）。
_TRAILING_EPISODE_NUMBER_RE = re.compile(r"\[\s*(\d+(?:\.\d+)?)\s*\]\s*$")


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


def extract_trailing_episode_number(raw_title: str) -> str | None:
    """從**未清理**的標題原文抓結尾 `[N]`（純數字，可帶小數）集數標記，抓不到回 None。"""
    match = _TRAILING_EPISODE_NUMBER_RE.search(raw_title or "")
    return match.group(1) if match else None
