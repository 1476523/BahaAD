"""已下載集數的 HLS 串流——在外面透過 Cloudflare DDNS、或網路不穩時，單一大 mp4
容易一斷就整個卡住；切成小片段後斷線只要重抓那一小段。

**只重封裝、不轉檔、不動原始 `.mp4`**（使用者 2026-09-04）：`ffmpeg -c copy` 把 mp4
切成 fMP4 片段 + VOD `m3u8`，放進獨立快取目錄（`%LOCALAPPDATA%\\BahaAD\\hls_cache\\<sn>\\`）。
第一次播放時產生（`-c copy` 幾秒就好），之後直接吃快取；超過保留數的舊快取自動刪掉。
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# `/hls/<name>` 只允許取這兩種檔名（擋路徑穿越）
_SEGMENT_NAME_RE = re.compile(r"^(init\.mp4|seg\d{5}\.m4s)$")
# `-c copy` 重封裝：三小時的電影也 < 60 秒，600 秒綽綽有餘（也擋住萬一卡住的 ffmpeg）
_GENERATE_TIMEOUT_SECONDS = 600


class HlsCache:
    def __init__(
        self,
        cache_dir: str | Path,
        ffmpeg_path: str,
        *,
        keep: int = 8,
        segment_seconds: int = 6,
    ) -> None:
        self._dir = Path(cache_dir)
        self._ffmpeg = ffmpeg_path
        self._keep = max(1, keep)
        self._segment_seconds = segment_seconds
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # 安全檢視 2026-09-04：公開模式下未登入者也能觸發 HLS 產生。全域只允許一個
        # ffmpeg 同時跑——攻擊者狂切不同集數頂多讓「產生」排隊、不會把 CPU／磁碟打爆
        # （`-c copy` 本來就很輕）。搶不到就當「這集暫時沒有 HLS」，播放器自己退回 mp4。
        self._generate_slot = threading.Semaphore(1)

    def _sn_dir(self, video_sn: int) -> Path:
        return self._dir / str(video_sn)

    def _lock_for(self, video_sn: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(video_sn, threading.Lock())

    @staticmethod
    def _stamp(src: Path) -> str:
        st = src.stat()
        return f"{st.st_size}:{int(st.st_mtime)}"

    def playlist_path(self, video_sn: int, source_mp4: str | Path) -> Path | None:
        """回傳這一集的 `index.m3u8`（沒有就先產生）；來源 mp4 不在回 None。"""
        src = Path(source_mp4)
        if not src.exists():
            return None
        d = self._sn_dir(video_sn)
        m3u8 = d / "index.m3u8"
        marker = d / ".src"
        stamp = self._stamp(src)

        if m3u8.exists() and marker.exists() and _read(marker) == stamp:
            _touch(d)
            return m3u8

        with self._lock_for(video_sn):
            if m3u8.exists() and marker.exists() and _read(marker) == stamp:
                return m3u8
            # 全域一次一個 ffmpeg；搶不到（別人正在產生）就先回 None，呼叫端 404、
            # 播放器退回 mp4，不排長隊也不硬等
            if not self._generate_slot.acquire(timeout=30):
                return None
            try:
                self._generate(d, src, stamp)
            finally:
                self._generate_slot.release()

        if m3u8.exists():
            _touch(d)  # 這一集是最新用的，prune 時保住它
        self._prune(keep_dir=d)
        return m3u8 if m3u8.exists() else None

    def segment_path(self, video_sn: int, name: str) -> Path | None:
        if not _SEGMENT_NAME_RE.match(name or ""):
            return None
        p = self._sn_dir(video_sn) / name
        return p if p.exists() else None

    def discard(self, video_sn: int) -> None:
        """刪掉這一集的 HLS 快取——原始 mp4 被使用者刪掉、偵測到「已移除」時一併清掉
        （使用者 2026-09-06：HLS 片段是 mp4 的複本，原檔沒了就不該留著佔空間）。
        沒有快取就無事發生。"""
        d = self._sn_dir(video_sn)
        if not d.exists():
            return
        with self._lock_for(video_sn):
            shutil.rmtree(d, ignore_errors=True)
        logger.info("原始檔案已移除，一併刪掉 HLS 快取（sn=%s）", video_sn)

    def _generate(self, d: Path, src: Path, stamp: str) -> None:
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True, exist_ok=True)
        command = [
            self._ffmpeg,
            "-y",
            "-loglevel", "warning",
            "-i", str(src),
            "-c", "copy",
            "-f", "hls",
            "-hls_playlist_type", "vod",
            "-hls_time", str(self._segment_seconds),
            "-hls_flags", "independent_segments",
            "-hls_segment_type", "fmp4",
            # 這個檔名是相對「執行目錄」寫出的（不是相對播放清單）——一定要配 cwd=d，
            # 不然 fMP4 的 init 片段會落在別處、播放清單的 #EXT-X-MAP 指向 404。
            "-hls_fmp4_init_filename", "init.mp4",
            "-hls_segment_filename", str(d / "seg%05d.m4s"),
            str(d / "index.m3u8"),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=_GENERATE_TIMEOUT_SECONDS,
                creationflags=_NO_WINDOW,
                cwd=str(d),
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("HLS 產生失敗（sn=%s）：%s", d.name, exc)
            shutil.rmtree(d, ignore_errors=True)
            return
        if result.returncode != 0:
            logger.warning("HLS 產生失敗（sn=%s）：%s", d.name, (result.stderr or "")[-800:])
            shutil.rmtree(d, ignore_errors=True)
            return
        (d / ".src").write_text(stamp, encoding="utf-8")

    def _prune(self, keep_dir: Path | None = None) -> None:
        if not self._dir.exists():
            return
        try:
            dirs = [p for p in self._dir.iterdir() if p.is_dir()]
        except OSError:
            return
        # 最近用過的在前；mtime 撞在一起時（同一秒內連續產生）用「數字大的比較新」收尾
        def _num(p: Path) -> int:
            try:
                return int(p.name)
            except ValueError:
                return -1

        dirs.sort(key=lambda p: (_mtime(p), _num(p)), reverse=True)
        for old in dirs[self._keep:]:
            if keep_dir is not None and old == keep_dir:
                continue
            shutil.rmtree(old, ignore_errors=True)


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return ""


def _touch(p: Path) -> None:
    try:
        p.touch()
    except OSError:
        pass


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0
