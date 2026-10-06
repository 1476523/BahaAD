"""用 ffprobe 讀成品 mp4 的媒體資訊（使用者 2026-08-29 第 14 項）。

給檔名模板／通知範本的 token 用：解析度、FPS、影像／音訊編碼器、容器格式。
`bahaad.downloader.ffmpeg` 已經要求 ffmpeg 在 PATH 裡，ffprobe 一般跟 ffmpeg 同包同目錄。
ffprobe 找不到、或執行失敗、或輸出解析不出來——一律回全空的 `MediaInfo`，絕不拋例外
（拿不到中繼資料只是 token 顯示空字串，不能讓下載流程掛掉）。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_TIMEOUT_SECONDS = 30

# ffprobe 的 codec_name → 給人看的名字。認不得就原樣大寫。
_VIDEO_CODEC_NAMES = {
    "h264": "H.264",
    "hevc": "H.265",
    "av1": "AV1",
    "vp9": "VP9",
    "mpeg4": "MPEG-4",
}
_AUDIO_CODEC_NAMES = {
    "aac": "AAC",
    "mp3": "MP3",
    "ac3": "AC-3",
    "eac3": "E-AC-3",
    "opus": "Opus",
    "flac": "FLAC",
    "vorbis": "Vorbis",
}
_STANDARD_HEIGHTS = {360, 480, 540, 576, 720, 1080, 1440, 2160, 4320}


@dataclass(frozen=True)
class MediaInfo:
    resolution: str = ""     # "1080p"（非標準高度時 "1920x1080"）
    fps: str = ""            # "24" / "23.98"
    video_codec: str = ""    # "H.264"
    audio_codec: str = ""    # "AAC"
    container: str = ""      # "mp4"


@lru_cache(maxsize=1)
def find_ffprobe() -> str | None:
    return shutil.which("ffprobe")


def _fmt_fps(raw: str) -> str:
    """ffprobe 的 `r_frame_rate` 是 "24000/1001" 這種分數字串。"""
    try:
        num, _, den = raw.partition("/")
        value = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return ""
    if value <= 0:
        return ""
    rounded = round(value)
    if abs(value - rounded) < 0.02:
        return str(rounded)
    return f"{value:.2f}"


def _fmt_resolution(width: object, height: object) -> str:
    try:
        w, h = int(width), int(height)
    except (TypeError, ValueError):
        return ""
    if h <= 0:
        return ""
    return f"{h}p" if h in _STANDARD_HEIGHTS else f"{w}x{h}"


def probe_media(path: Path, ffprobe_path: str | None = None) -> MediaInfo:
    exe = ffprobe_path or find_ffprobe()
    if not exe:
        return MediaInfo(container=Path(path).suffix.lstrip(".").lower())
    try:
        result = subprocess.run(
            [exe, "-v", "quiet", "-print_format", "json", "-show_streams", str(path)],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            creationflags=_NO_WINDOW,
        )
        streams = json.loads(result.stdout or "{}").get("streams", [])
    except (subprocess.SubprocessError, OSError, ValueError):
        return MediaInfo(container=Path(path).suffix.lstrip(".").lower())

    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})
    vcodec = str(video.get("codec_name", "")).lower()
    acodec = str(audio.get("codec_name", "")).lower()
    return MediaInfo(
        resolution=_fmt_resolution(video.get("width"), video.get("height")),
        fps=_fmt_fps(str(video.get("r_frame_rate", ""))),
        video_codec=_VIDEO_CODEC_NAMES.get(vcodec, vcodec.upper()),
        audio_codec=_AUDIO_CODEC_NAMES.get(acodec, acodec.upper()),
        container=Path(path).suffix.lstrip(".").lower(),
    )
