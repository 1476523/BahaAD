"""自訂輸出檔名的 token 模板。規格見 docs/requirements/round6.md 第 14 項。

沿用 `notify/render.py` 的 `@token@` 取代模式：模板是一串含 `@name@` 佔位符的字串，
用實際值替換。未知的 `@token@` 原樣保留（讓使用者一眼看出打錯字）。

預設模板 `@anime_title@ [@episode_num@]` → `吉伊卡哇 [1]`（加上 `.mp4` / `.ass` 副檔名
由呼叫端補）。「補齊長度」(`filename_pad_width`) 對集數數字補零：設 3 → `001` / `第001集`。
"""

from __future__ import annotations

import re
from datetime import datetime

from bahaad.downloader.mediainfo import MediaInfo

# 番劇內的特別篇：`特別篇 1` / `SP1` → 檔名用 `SP` 編號（使用者定案，補齊長度照樣套
# 在數字部分：`特別篇 1` + 補齊 2 → `SP01`）。沒帶數字的 `特別篇` → `SP`。
# 站方 API 的集數本身只是普通數字（實測「徹夜之歌 S2」特別篇 episode=1），要靠 video.title
# 的 `[特別篇]` 標記判斷——`scheduler/main_loop._episode_basename` 偵測到就把 `episode_number`
# 轉成 `"特別篇 N"` 再丟進來，所以這個 regex 是它的搭配。
_SPECIAL_RE = re.compile(r"^\s*(?:特別篇|SP)\s*(\d*)\s*$", re.IGNORECASE)

# Windows 檔名不允許的字元 → 換成對應的全形字（round 7 第 15 項：使用者希望「能換
# 全形的就換全形」，`_` 只用在沒有全形對應的情況——這裡九個都有全形）。
_FULLWIDTH_MAP = str.maketrans({
    "<": "＜", ">": "＞", ":": "：", '"': "＂",
    "/": "／", "\\": "＼", "|": "｜", "?": "？", "*": "＊",
})

DEFAULT_TEMPLATE = "@anime_title@ [@episode_num@]"
DEFAULT_PAD_WIDTH = 1

# (token, 說明) — 給設定頁的 token 說明表用
PLACEHOLDER_REFERENCE: tuple[tuple[str, str], ...] = (
    ("@anime_title@", "番劇名稱（已去掉站方標題尾端的集數標記）"),
    ("@sn@", "這一集的 video sn 碼"),
    ("@episode_num@", "集數（數字）：1、2、3…（依「補齊長度」補零）"),
    ("@episode_zh@", "集數（中文）：第1集、第2集…"),
    ("@episode_ep_zh@", "話數（中文）：第1話、第2話…"),
    ("@resolution@", "影片解析度（小寫）：720p、1080p…"),
    ("@resolution_upper@", "影片解析度（大寫）：720P、1080P…"),
    ("@fps@", "影格率：24、23.98…"),
    ("@vcodec@", "影像編碼器：H.264、H.265…"),
    ("@acodec@", "音訊編碼器：AAC、AC-3…"),
    ("@container@", "檔案格式（小寫）：mp4"),
    ("@container_upper@", "檔案格式（大寫）：MP4"),
    ("@date@", "壓縮完畢的日期：2026-08-29"),
    ("@time@", "壓縮完畢的時間：203005（時分秒，檔名不能有冒號）"),
)


def sanitize_filename_part(text: str) -> str:
    """把不能當 Windows 檔名的字元換成全形，回傳可以直接當資料夾名／檔名的字串。
    `main_loop` 的資料夾名與 `render_basename` 的檔名都用這一支。"""
    return (text or "").translate(_FULLWIDTH_MAP).strip()


# 舊名，內部還有呼叫
_sanitize = sanitize_filename_part


def episode_zh(episode_number, pad_width: int = DEFAULT_PAD_WIDTH) -> str:
    """人看的集數標籤——「第 008 集」（數字部分依「補齊長度」補零）、「特別篇 1」、
    非數字（電影／OVA…）原樣。通知、下載列表、每日彙整、日誌都用這一支，統一格式。"""
    raw = str(episode_number).strip()
    width = max(1, int(pad_width or 1))
    special = _SPECIAL_RE.match(raw)
    if special:
        digits = special.group(1)
        return f"特別篇 {digits.zfill(width)}".rstrip() if digits else "特別篇"
    padded = raw.zfill(width) if raw.isdigit() else raw
    return f"第{padded}集"


def sample_context() -> dict[str, object]:
    """設定頁即時預覽用的範例值（跟 settings.html 內嵌的 JS 預覽一致）。"""
    return {
        "anime_title": "葬送的芙莉蓮",
        "sn": 12345,
        "episode_number": 3,
        "media": MediaInfo(
            resolution="1080p", fps="24", video_codec="H.264", audio_codec="AAC", container="mp4"
        ),
        "finished_at": datetime(2026, 8, 29, 20, 30, 5),
    }


def _media_values(media: MediaInfo | None, finished_at: datetime | None) -> dict[str, str]:
    media = media or MediaInfo()
    when = finished_at or datetime.now()
    return {
        # 大寫版放前面：token 取代是逐一 str.replace，`@resolution@` 是 `@resolution_upper@`
        # 的前綴，先換長的才不會把 `@resolution_upper@` 裡的 `@resolution@` 先吃掉。
        "@resolution_upper@": (media.resolution or "").upper(),
        "@container_upper@": (media.container or "").upper(),
        "@resolution@": media.resolution,
        "@fps@": media.fps,
        "@vcodec@": media.video_codec,
        "@acodec@": media.audio_codec,
        "@container@": media.container,
        "@date@": when.strftime("%Y-%m-%d"),
        "@time@": when.strftime("%H%M%S"),
    }


def render_basename(
    template: str,
    pad_width: int,
    *,
    anime_title: str,
    sn: int,
    episode_number: int,
    media: MediaInfo | None = None,
    finished_at: datetime | None = None,
) -> str:
    """回傳不含副檔名的基本檔名。`episode_number` 是人看的集數（round 7 第 9 項：不再用
    `episode_index + 1`——季不從第 1 集開始時那個會錯）。可能是 `"38"`；也可能是番劇內的
    特別篇 `"特別篇 1"` → `@episode_num@` 變 `SP1`（補齊套在數字部分）；再其他非數字
    （`電影`/`OVA`…）就不補零、原樣用。"""
    raw = str(episode_number)
    width = max(1, int(pad_width or 1))
    special = _SPECIAL_RE.match(raw)
    if special:
        digits = special.group(1)
        padded_digits = digits.zfill(width) if digits else ""
        ep_num = f"SP{padded_digits}"
        ep_zh = f"特別篇 {padded_digits}".rstrip()
        values = {
            "@anime_title@": anime_title,
            "@sn@": str(sn),
            "@episode_num@": ep_num,
            "@episode_zh@": ep_zh,
            "@episode_ep_zh@": ep_zh,
        }
        fallback_ep = ep_num
    else:
        padded = raw.zfill(width) if raw.isdigit() else raw
        values = {
            "@anime_title@": anime_title,
            "@sn@": str(sn),
            "@episode_num@": padded,
            "@episode_zh@": f"第{padded}集",
            "@episode_ep_zh@": f"第{padded}話",
        }
        fallback_ep = padded
    values.update(_media_values(media, finished_at))
    out = template or DEFAULT_TEMPLATE
    for token, value in values.items():
        out = out.replace(token, str(value))
    out = sanitize_filename_part(out)
    return out or sanitize_filename_part(f"{anime_title} [{fallback_ep}]")
