"""唯一允許執行 ffmpeg subprocess 的地方。規格見 docs/requirements/downloader_ffmpeg.md。

拿 `downloader/segment.py` 產出的本地播放清單（片段跟金鑰都已經下載到本機）合併／
解密成單一輸出檔。**這裡完全不碰網路**——所有網路下載都是 `segment.py` 的責任，
`ffmpeg.py` 只對本地檔案動作。

實測驗證過（見 docs/api-observations/playback-and-ads.md「ffmpeg 直接消化 HLS」）：
1. ffmpeg 可以直接吃 AES-128 加密的 HLS 播放清單並自動解密（不用自己寫解密邏輯）
2. 讀本地播放清單時，如果金鑰檔案的副檔名不是常見多媒體格式（`segment.py` 存的是
   `key.bin`），ffmpeg 自己的安全機制會擋下讀取，錯誤訊息是「blocked for security
   reasons」，**一定要加 `-allowed_extensions ALL` 才能正常讀取**，這不是理論推測，
   是實際跑過才發現的坑
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# 無主控台的 GUI exe（--windows-console-mode=disable）spawn 一個 console 程式（ffmpeg.exe）
# 時，Windows 會配一個新的 console 視窗，每次合併都會閃一下黑框（round 6 第 10 項）。
# 非 Windows 平台沒有這個旗標，取 0（等於不帶）。
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_DEFAULT_TIMEOUT_SECONDS = 3600  # 一集份量的合併作業，給足夠寬裕的上限避免卡住時無限等待
_STDERR_TAIL_CHARS = 4000  # 例外訊息只保留 stderr 尾段，ffmpeg 的完整輸出可能很長


class FfmpegError(Exception):
    pass


class FfmpegNotFoundError(FfmpegError):
    pass


def find_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if not path:
        raise FfmpegNotFoundError("找不到 ffmpeg 執行檔，請確認已安裝且在 PATH 裡")
    return path


class FfmpegRunner:
    def __init__(self, ffmpeg_path: str | None = None, timeout: float = _DEFAULT_TIMEOUT_SECONDS) -> None:
        self._ffmpeg_path = ffmpeg_path or find_ffmpeg()
        self._timeout = timeout

    def mux(self, local_playlist: Path, output_path: Path, overwrite: bool = False) -> Path:
        """把 segment.py 產出的本地播放清單合併（並視需要解密）成單一輸出檔，
        回傳輸出檔路徑。"""
        local_playlist = Path(local_playlist)
        output_path = Path(output_path)
        if output_path.exists() and not overwrite:
            raise FfmpegError(f"輸出檔已存在，且沒有指定覆蓋: {output_path}")
        output_path.parent.mkdir(parents=True, exist_ok=True)

        command = [
            self._ffmpeg_path,
            "-y",
            "-loglevel", "warning",
            "-allowed_extensions", "ALL",
            "-i", str(local_playlist),
            "-c", "copy",
            # moov atom 移到檔案最前面——遠端／區網用 <video> 直接串這個 mp4 時，
            # 播放器不必先抓檔尾就能開始播、拖曳也更快（使用者 2026-09-04：在外面透過
            # Cloudflare DDNS 看自己下載的影片）。只影響 mp4 容器，多一次寫入、可忽略。
            "-movflags", "+faststart",
            str(output_path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired as exc:
            raise FfmpegError(f"ffmpeg 逾時（{self._timeout} 秒）: {local_playlist}") from exc

        if result.returncode != 0:
            raise FfmpegError(
                f"ffmpeg 執行失敗（returncode={result.returncode}）: "
                f"{result.stderr[-_STDERR_TAIL_CHARS:]}"
            )
        return output_path
