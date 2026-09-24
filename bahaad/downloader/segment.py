"""HLS 片段下載。規格見 docs/requirements/downloader_segment.md。

拿 `gamer_client/playlist.py` 給的 `PlaylistInfo` 與挑好的 `QualityVariant`，解析
`chunklist_b{bitrate}.m3u8`（標準 HLS 媒體播放清單，RFC 8216：`#EXTINF` 標記每段
長度，下一行是片段網址），把 `.ts` 片段下載到暫存目錄，並在同一個目錄裡重寫出一份
「本地版」播放清單，讓 `downloader/ffmpeg.py` 可以直接吃、不必再碰網路。

AES-128 金鑰與 downloader/ 的分工（實測驗證過，不是設計時的猜測）：觀察到的 chunklist
都是 `#EXT-X-KEY:METHOD=AES-128,URI="key_b{bitrate}.m3u8key"`（見
docs/api-observations/playback-and-ads.md）。實測 ffmpeg 可以直接吃遠端簽章網址自動
下金鑰解密，但 segment.py 本來就要自己控制下載（重試/進度/過期換網址），所以金鑰檔案
也一併下載到本機，並且把 chunklist 重寫成本地路徑版本——這樣 ffmpeg.py 完全不用碰
網路，只需要對本地檔案動作。實測發現一個重要細節：ffmpeg 對本地金鑰檔案的副檔名有
安全限制（非常見多媒體副檔名會被擋，錯誤訊息是「blocked for security reasons」），
`ffmpeg.py` 呼叫時要加 `-allowed_extensions ALL` 才能讀到金鑰檔案，不是 segment.py
這邊的責任，但這裡的檔名/寫法要跟 ffmpeg.py 那邊的呼叫方式對得上。

簽章網址過期處理：`QualityVariant.expires_at` 是這個畫質專屬的到期時間（見
`gamer_client/playlist.py`——`hdntl` 跟主清單的 `hdnts` 不是同一個值）。下載途中
偵測到快過期時，透過呼叫端傳入的 `refresh` 回呼重新拿一組 `PlaylistInfo`，不是自己
持有 `PlaylistClient`——`downloader/` 不直接依賴 `gamer_client/` 的 HTTP 細節，只依賴
`playlist.py` 對外的資料型別，維持職責分離。

片段併發下載（見 docs/requirements/downloader_segment.md「最大並發分段數」一節）：
片段依 `max_concurrent_segments` 併發下載——這是「同一部影片內」的併發，跟
`scheduler/download_pool.py` 的「同時下載幾部影片」是不同層級的控制，兩者疊加才是完整
的並發行為。**整段下載共用同一個 `ThreadPoolExecutor`**（2026-09-09 改，之前是每批
new/join 一個）：worker 執行緒連同 curl_cffi 的 thread-local curl handle 與它保持的
keep-alive 連線在整段期間都活著，片段之間重用連線，不用每 N 段就重跑一次 TLS + 瀏覽器
指紋交握（使用者回報下載比舊專案慢快一分鐘的主因）。用滾動視窗：手上永遠保持最多
`max_concurrent_segments` 個請求在飛，完成一個補一個，慢的片段不會卡住整批。進度回報
是「完成幾段／共幾段」的累計數，跟完成順序無關。片段之間不睡眠（round 7 第 10 項：
風控看的是「單一集的請求頻率」不是片段，「下載完一集後的冷卻」搬到 `main_loop`）。
`max_concurrent_segments` 上限固定為 5，設定值超過會被夾住，同一部影片開太多條連線
對站方 CDN 也是負擔，沒有理由無限制往上調（見 PROGRESS.md）。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor
from concurrent.futures import wait as _wait_futures
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import urljoin

from bahaad.gamer_client.playlist import PlaylistInfo, QualityVariant
from bahaad.store.settings import SettingsStore

_EXTINF_RE = re.compile(r"#EXTINF:(?P<duration>[\d.]+)[^\n]*\n(?P<uri>\S+)")
_KEY_RE = re.compile(
    r'#EXT-X-KEY:METHOD=(?P<method>[\w-]+)(?:,URI="(?P<uri>[^"]+)")?(?:,IV=(?P<iv>0[xX][0-9A-Fa-f]+))?'
)
_MEDIA_SEQUENCE_RE = re.compile(r"#EXT-X-MEDIA-SEQUENCE:(\d+)")

_KEY_FILENAME = "key.bin"
_LOCAL_PLAYLIST_FILENAME = "local_playlist.m3u8"

_DEFAULT_MAX_RETRIES = 3
_DEFAULT_MAX_CONCURRENT_SEGMENTS = 4
_MAX_ALLOWED_CONCURRENT_SEGMENTS = 5
# round 7 第 10 項：片段之間不再有冷卻（風控看的是「單一集的請求頻率」不是片段頻率）。
# 「下載完一集後的冷卻」搬到 scheduler/main_loop.py 的 `download_cooldown_seconds`。
# 快過期的安全邊界：距離 expires_at 少於這個秒數，下載下一個片段前就先觸發 refresh，
# 不要等到簽章網址真的過期、片段開始下載失敗才發現
_EXPIRY_SAFETY_MARGIN_SECONDS = 30

# 偏好畫質預設（使用者 2026-09-05）：沒設定 / 還原成預設時都當 720P，不是「自動最高」。
# 站方主清單非 VIP 本來就只給到 720p，所以這個預設對訪客／非 VIP 沒有副作用。
DEFAULT_PREFERRED_QUALITY = "720p"

logger = logging.getLogger(__name__)


class SegmentDownloadError(Exception):
    pass


class DownloadInterrupted(Exception):
    """關閉程式時，下載流程收到 stop_event、主動中斷。呼叫端（main_loop）看到這個
    就直接收尾、不重試、不通知——不是「失敗」，是「使用者要關程式了」。"""


class HttpGetter(Protocol):
    def get(self, url: str) -> "_ResponseLike": ...


class _ResponseLike(Protocol):
    text: str
    content: bytes


@dataclass(frozen=True)
class DownloadProgress:
    """不可變快照——每次進度更新都建一個新物件，呼叫端把收到的實例存起來（例如累積
    歷史紀錄）不會被後續更新悄悄改掉內容，是刻意的設計，不是疏漏。"""

    total: int
    completed: int = 0

    @property
    def fraction(self) -> float:
        return self.completed / self.total if self.total else 0.0


@dataclass(frozen=True)
class _ChunklistInfo:
    media_sequence: int
    key_method: str | None
    key_uri: str | None
    key_iv: str | None
    segments: list[tuple[str, str]]  # (duration_str, absolute_url)


class SegmentDownloader:
    def __init__(
        self,
        http: HttpGetter,
        settings: SettingsStore,
        max_retries: int | None = None,
        max_concurrent_segments: int | None = None,
        download_cooldown_seconds: float | None = None,  # round 7 第 10 項起無作用（片段之間不再冷卻），保留參數避免動到既有呼叫端
    ) -> None:
        self._http = http
        self._settings = settings
        # 建構子明確傳入的值優先（測試要固定值）；沒傳就每次用到時重新從 SettingsStore 讀，
        # 讓使用者在設定頁改「最大下載線程數」不用重開程式（round 6 第 15 項）。
        self._max_retries_override = max_retries
        self._max_concurrent_segments_override = max_concurrent_segments

    @property
    def _max_retries(self) -> int:
        if self._max_retries_override is not None:
            return self._max_retries_override
        return self._settings.get("max_retries", _DEFAULT_MAX_RETRIES)

    def _current_max_concurrent_segments(self) -> int:
        raw = (
            self._max_concurrent_segments_override
            if self._max_concurrent_segments_override is not None
            else self._settings.get("max_concurrent_segments", _DEFAULT_MAX_CONCURRENT_SEGMENTS)
        )
        return max(1, min(raw, _MAX_ALLOWED_CONCURRENT_SEGMENTS))

    def select_quality(
        self, playlist_info: PlaylistInfo, *, label: str | None = None
    ) -> QualityVariant:
        """依 store/settings.py 的畫質偏好挑一個；沒設定或拿不到對應畫質時（例如偏好
        1080P 但帳號不是動畫瘋 VIP、站方這一集的主清單只提供到 720P），退避到位元率
        最高的那個，並記在日誌（使用者 2026-09-05）。"""
        best = max(playlist_info.qualities, key=lambda q: q.bitrate)
        preferred_label = self._settings.get("preferred_quality") or DEFAULT_PREFERRED_QUALITY
        for quality in playlist_info.qualities:
            if quality.label == preferred_label:
                return quality
        who = label or f"video_sn={playlist_info.video_sn}"
        logger.info(
            "%s 偏好畫質 %s 這一集拿不到（帳號可能不是動畫瘋 VIP，或站方只提供到 %s），改用 %s",
            who, preferred_label, best.label, best.label,
        )
        return best

    def download(
        self,
        playlist_info: PlaylistInfo,
        quality: QualityVariant,
        dest_dir: Path,
        refresh: Callable[[], PlaylistInfo] | None = None,
        progress_callback: Callable[[DownloadProgress], None] | None = None,
        stop_event: "threading.Event | None" = None,
    ) -> Path:
        """下載這個畫質的所有片段（跟金鑰，如果有加密），回傳本地播放清單的路徑，
        交給 downloader/ffmpeg.py 直接使用。`stop_event` set 時每批之間會中斷、拋
        `DownloadInterrupted`（關閉程式用，round 6 第 16 項）。"""
        def _check_stop() -> None:
            if stop_event is not None and stop_event.is_set():
                raise DownloadInterrupted(f"video_sn={playlist_info.video_sn} 下載中斷（程式關閉中）")
        # dest_dir 由呼叫端負責是「暫存區、下載完會整個清掉」的資料夾——round 6 第 15 項
        # 起 main_loop 傳的是 %LOCALAPPDATA%\BahaAD\download_staging\{video_sn}\，片段、
        # 金鑰、本地播放清單直接放這裡，不再自建 .{sn}-downloading 隱藏子夾（Windows 下
        # 那個點開頭不是真的隱藏，使用者會在下載目錄看到一堆 .ts）。
        incomplete_dir = Path(dest_dir)
        incomplete_dir.mkdir(parents=True, exist_ok=True)

        chunklist = self._fetch_chunklist(quality.chunklist_url)
        total = len(chunklist.segments)

        if chunklist.key_uri:
            self._download_one(chunklist.key_uri, incomplete_dir / _KEY_FILENAME)

        completed_count = 0
        count_lock = threading.Lock()

        def _report_one_done() -> None:
            nonlocal completed_count
            with count_lock:
                completed_count += 1
                snapshot = DownloadProgress(total=total, completed=completed_count)
            if progress_callback:
                progress_callback(snapshot)

        # 整段下載共用同一個 executor（不是每批一個）——worker 執行緒連同 curl_cffi 的
        # thread-local curl handle 與它保持的 keep-alive 連線，在整段下載期間都活著，
        # 片段之間直接重用連線，不用每 N 段就重跑一次 TLS + 瀏覽器指紋交握（使用者
        # 2026-09-09「比舊專案慢快一分鐘」的主因：舊寫法每批 new/join 一個 ThreadPoolExecutor
        # → 每批的執行緒都是新的 → thread-local curl handle 重建 → 連線重連）。
        # 滾動視窗：手上永遠保持最多 max_workers 個請求在飛，完成一個補一個，慢的片段
        # 不會卡住整批。進度回報是「完成幾段／共幾段」的累計數，跟完成順序無關。
        max_workers = self._current_max_concurrent_segments()
        next_index = 0
        done = 0
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            in_flight: dict = {}  # Future -> segment index
            while done < len(chunklist.segments):
                _check_stop()

                if quality.expires_at - time.time() < _EXPIRY_SAFETY_MARGIN_SECONDS:
                    if refresh is None:
                        raise SegmentDownloadError(
                            f"video_sn={playlist_info.video_sn} 簽章網址快過期，"
                            "但沒有提供 refresh 回呼"
                        )
                    # 先把在飛的片段收完（它們用的舊網址 ~30 秒內仍有效），再換新網址
                    # 繼續——不讓新舊世代的網址混在同一輪視窗裡數錯進度
                    for fut in list(in_flight):
                        idx = in_flight.pop(fut)
                        fut.result()
                        next_index = max(next_index, idx + 1)
                        done += 1
                        _report_one_done()
                    playlist_info = refresh()
                    quality = _match_quality(playlist_info, quality.label)
                    chunklist = self._fetch_chunklist(quality.chunklist_url)
                    total = len(chunklist.segments)  # 同一集不該變，防禦性重讀
                    if chunklist.key_uri:
                        self._download_one(chunklist.key_uri, incomplete_dir / _KEY_FILENAME)
                    continue

                while len(in_flight) < max_workers and next_index < len(chunklist.segments):
                    _, segment_url = chunklist.segments[next_index]
                    fut = executor.submit(
                        self._download_one,
                        segment_url,
                        incomplete_dir / f"segment_{next_index:05d}.ts",
                    )
                    in_flight[fut] = next_index
                    next_index += 1

                if not in_flight:
                    break
                finished, _ = _wait_futures(in_flight, return_when=FIRST_COMPLETED)
                for fut in finished:
                    in_flight.pop(fut)
                    fut.result()  # 片段重試用盡 → SegmentDownloadError 往外拋
                    done += 1
                    _report_one_done()

        return _write_local_playlist(incomplete_dir, chunklist)

    def _fetch_chunklist(self, chunklist_url: str) -> _ChunklistInfo:
        response = self._http.get(chunklist_url)
        text = response.text

        media_sequence_match = _MEDIA_SEQUENCE_RE.search(text)
        media_sequence = int(media_sequence_match.group(1)) if media_sequence_match else 0

        key_method = key_uri = key_iv = None
        key_match = _KEY_RE.search(text)
        if key_match:
            key_method = key_match.group("method")
            raw_key_uri = key_match.group("uri")
            key_uri = _resolve(chunklist_url, raw_key_uri) if raw_key_uri else None
            key_iv = key_match.group("iv")

        segments = [
            (duration, _resolve(chunklist_url, uri))
            for duration, uri in _EXTINF_RE.findall(text)
        ]
        if not segments:
            raise SegmentDownloadError(f"chunklist 沒有解析出任何片段: {chunklist_url}")

        return _ChunklistInfo(
            media_sequence=media_sequence,
            key_method=key_method,
            key_uri=key_uri,
            key_iv=key_iv,
            segments=segments,
        )

    def _download_one(self, url: str, dest_path: Path) -> None:
        last_error: Exception | None = None
        for _ in range(self._max_retries):
            try:
                response = self._http.get(url)
                dest_path.write_bytes(response.content)
                return
            except Exception as exc:  # noqa: BLE001 - 重試迴圈刻意攔截所有例外
                last_error = exc
        raise SegmentDownloadError(
            f"下載失敗（重試 {self._max_retries} 次）: {url}"
        ) from last_error


def _resolve(base_url: str, uri: str) -> str:
    return uri if uri.startswith("http") else urljoin(base_url, uri)


def _write_local_playlist(incomplete_dir: Path, chunklist: _ChunklistInfo) -> Path:
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-MEDIA-SEQUENCE:{chunklist.media_sequence}"]
    if chunklist.key_uri:
        key_line = f'#EXT-X-KEY:METHOD={chunklist.key_method},URI="{_KEY_FILENAME}"'
        if chunklist.key_iv:
            key_line += f",IV={chunklist.key_iv}"
        lines.append(key_line)
    for index, (duration, _) in enumerate(chunklist.segments):
        lines.append(f"#EXTINF:{duration},")
        lines.append(f"segment_{index:05d}.ts")
    lines.append("#EXT-X-ENDLIST")

    playlist_path = incomplete_dir / _LOCAL_PLAYLIST_FILENAME
    playlist_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return playlist_path


def _match_quality(playlist_info: PlaylistInfo, label: str) -> QualityVariant:
    for quality in playlist_info.qualities:
        if quality.label == label:
            return quality
    raise SegmentDownloadError(f"重新整理後找不到畫質 {label}")
