"""彈幕抓取與字幕檔產出。規格見 docs/requirements/downloader_danmu.md。

端點格式依 docs/api-observations/danmu.md 實測確認：訪客身分即可，不需要登入、不需要
CSRF，比 gamer_client/playlist.py 的 video_src.php 簡單很多。

`DanmuEntry` 刻意不收 API 回應裡的 `userid`（發文者的動畫瘋帳號代稱）——那是其他使用者
的個人資料，彈幕疊加層本來就不需要標示是誰發的。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

_DANMU_URL = "https://api.gamer.com.tw/anime/v1/danmu.php"
_DEFAULT_GEO = "TW,HK"

# 跑馬燈從畫面右緣飄到左緣的固定時長；官方實際排版怎麼決定時長沒有觀察到，這是
# 常見彈幕系統的簡化慣例（固定時長，不隨文字長度調整）
_SCROLL_DURATION_SECONDS = 8.0
_FIXED_DURATION_SECONDS = 4.0
_SCROLL_LANE_COUNT = 12
_FIXED_LANE_COUNT = 4

_POSITION_SCROLL = 0
_POSITION_TOP = 1
_POSITION_BOTTOM = 2

# 用字數粗略估算文字寬度（像素），不是量測實際字型渲染寬度——只用來讓跑馬燈的移動
# 距離看起來合理，不要求精確
_PIXELS_PER_CHAR_ESTIMATE = 24


class DanmuError(Exception):
    pass


class HttpGetter(Protocol):
    def get(self, url: str, params: dict | None = None): ...


@dataclass(frozen=True)
class DanmuEntry:
    text: str
    time_offset_seconds: float
    color: str  # "#RRGGBB"
    size: int  # 1 或 2，觀察到的值域
    position: int  # 0=跑馬燈／1=頂部固定／2=底部固定，見需求規格文件


def get_danmu(session: HttpGetter, video_sn: int, geo: str = _DEFAULT_GEO) -> list[DanmuEntry]:
    """抓這一集全部彈幕（不帶 limit，拿完整份）。沒有彈幕回傳空列表，不是錯誤。"""
    response = session.get(_DANMU_URL, params={"videoSn": video_sn, "geo": geo})
    try:
        raw_entries = response.json()["data"]["danmu"]
    except (KeyError, TypeError, ValueError) as exc:
        raise DanmuError(f"danmu.php 回應格式不符預期: {exc}") from exc

    entries = []
    for raw in raw_entries:
        try:
            entries.append(
                DanmuEntry(
                    text=raw["text"],
                    time_offset_seconds=float(raw["time"]),
                    color=raw["color"],
                    size=int(raw["size"]),
                    position=int(raw["position"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise DanmuError(f"danmu 項目格式不符預期: {raw!r} ({exc})") from exc
    return entries


def write_ass(
    entries: list[DanmuEntry],
    output_path: Path,
    video_width: int = 1920,
    video_height: int = 1080,
) -> Path:
    """把彈幕轉成 .ass 字幕檔，跑馬燈飄過 + 頂/底部固定，用車道機制避免同時間彈幕
    互相重疊（簡化排版，不是精確還原官方播放器）。"""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    lines = [_ass_header(video_width, video_height)]
    scroll_lanes = _LaneTracker(_SCROLL_LANE_COUNT)
    top_lanes = _LaneTracker(_FIXED_LANE_COUNT)
    bottom_lanes = _LaneTracker(_FIXED_LANE_COUNT)

    for entry in sorted(entries, key=lambda e: e.time_offset_seconds):
        if entry.position == _POSITION_TOP:
            lines.append(_fixed_dialogue(entry, top_lanes, video_width, video_height, from_top=True))
        elif entry.position == _POSITION_BOTTOM:
            lines.append(_fixed_dialogue(entry, bottom_lanes, video_width, video_height, from_top=False))
        else:
            lines.append(_scroll_dialogue(entry, scroll_lanes, video_width, video_height))

    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return output_path


class _LaneTracker:
    """N 條車道，每條記錄「什麼時候會空出來」；指派時優先選已經空出來的車道，
    全部都還沒空出來時退而求其次選最快會空的那條（接受輕微重疊，不卡住）。"""

    def __init__(self, lane_count: int) -> None:
        self._free_at = [0.0] * lane_count

    def assign(self, start: float, duration: float) -> int:
        for lane, free_at in enumerate(self._free_at):
            if free_at <= start:
                self._free_at[lane] = start + duration
                return lane
        lane = min(range(len(self._free_at)), key=lambda i: self._free_at[i])
        self._free_at[lane] = start + duration
        return lane


def _ass_header(video_width: int, video_height: int) -> str:
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {video_width}\n"
        f"PlayResY: {video_height}\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Danmu,Microsoft JhengHei,36,&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,"
        "0,0,0,0,100,100,0,0,1,1.5,0,7,20,20,20,1\n"
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )


def _ass_time(seconds: float) -> str:
    seconds = max(0.0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, remainder = divmod(remainder, 60)
    centiseconds = round((remainder - int(remainder)) * 100)
    return f"{int(hours)}:{int(minutes):02d}:{int(remainder):02d}.{centiseconds:02d}"


def _ass_color(hex_color: str) -> str:
    """ASS 顏色是 &HBBGGRR&（BGR 順序），跟一般網頁的 #RRGGBB 順序相反。"""
    hex_color = hex_color.lstrip("#")
    if len(hex_color) != 6:
        hex_color = "FFFFFF"
    r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
    return f"&H{b}{g}{r}&"


def _fontsize(entry_size: int) -> int:
    return 48 if entry_size >= 2 else 32


def _scroll_dialogue(entry: DanmuEntry, lanes: _LaneTracker, video_width: int, video_height: int) -> str:
    duration = _SCROLL_DURATION_SECONDS
    lane = lanes.assign(entry.time_offset_seconds, duration)
    lane_height = video_height / _SCROLL_LANE_COUNT
    y = int(lane_height * lane + lane_height / 2)

    text_width_estimate = len(entry.text) * _PIXELS_PER_CHAR_ESTIMATE
    x_start = video_width
    x_end = -text_width_estimate

    start = _ass_time(entry.time_offset_seconds)
    end = _ass_time(entry.time_offset_seconds + duration)
    color = _ass_color(entry.color)
    fontsize = _fontsize(entry.size)
    text = _escape_ass_text(entry.text)

    override = f"{{\\move({x_start},{y},{x_end},{y})\\fs{fontsize}\\c{color}\\an5}}"
    return f"Dialogue: 0,{start},{end},Danmu,,0,0,0,,{override}{text}"


def _fixed_dialogue(
    entry: DanmuEntry, lanes: _LaneTracker, video_width: int, video_height: int, *, from_top: bool
) -> str:
    duration = _FIXED_DURATION_SECONDS
    lane = lanes.assign(entry.time_offset_seconds, duration)
    lane_height = video_height / _FIXED_LANE_COUNT / 2  # 只用畫面上/下各一半的高度堆疊
    if from_top:
        y = int(lane_height * lane + lane_height / 2)
        alignment = 8  # 頂部置中錨點
    else:
        y = int(video_height - lane_height * lane - lane_height / 2)
        alignment = 2  # 底部置中錨點
    x = video_width // 2

    start = _ass_time(entry.time_offset_seconds)
    end = _ass_time(entry.time_offset_seconds + duration)
    color = _ass_color(entry.color)
    fontsize = _fontsize(entry.size)
    text = _escape_ass_text(entry.text)

    override = f"{{\\pos({x},{y})\\fs{fontsize}\\c{color}\\an{alignment}}}"
    return f"Dialogue: 0,{start},{end},Danmu,,0,0,0,,{override}{text}"


def _escape_ass_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\N").replace("{", "\\{").replace("}", "\\}")
