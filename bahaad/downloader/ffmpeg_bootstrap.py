"""Windows 版 ffmpeg.exe 自動下載。

`downloader/ffmpeg.py` 的 `find_ffmpeg()` 刻意保持「完全不碰網路」（見該檔案開頭），
實際發送 HTTP 請求的邏輯獨立成這一個檔案。

使用者沒有自行安裝 ffmpeg、系統 PATH 也找不到時，從自建鏡像抓一份放進
`data_dir()/ffmpeg.exe`，不需要使用者手動安裝（使用者 2026-09-28 回報：完全沒用過
BahaAD 的電腦，開機沒裝過 ffmpeg，`build_services()` 在啟動最早期就直接整個崩潰——
無主控台、無訊息，使用者只看到「完全沒反應」；讀 `bahaad.db` 的 `logs` 表才抓到
`FfmpegNotFoundError`）。

比照 aniGamerPlus 既有的作法（`Anime.py` 的 `_ensure_windows_ffmpeg`）：下載網址不直接
以明文字串存在原始碼裡——執行期本來就一定要能還原出網址才能發送請求，這裡只是簡單的
XOR + base64 混淆，避免有人隨手 grep/搜尋原始碼或看 log 就撈到網址去對外轉貼/盜連
自家鏡像流量，**不是**真正的加密防護（不具備防止真正想反組譯還原的能力）。

**多線程分段下載**（使用者 2026-09-28 要求）：實測過自家鏡像有支援 HTTP Range（回
206 Partial Content），比照 `updater/fetcher.py` 的 `download_zip()` 精神——支援分段
就開多條執行緒平行下載，每段各自獨立重試；探測失敗（純連線問題、鏡像不支援分段等）
退回單線程整檔下載。跟 `download_zip()` 不同的是這裡沒有事先知道正確的 SHA-256（這個
鏡像只放這一份檔案、沒有另外公告雜湊值），完整性只靠「下載到的位元組數等於 Content-
Range 回報的檔案大小」這個基本檢查，不是真正的雜湊驗證。
"""

from __future__ import annotations

import base64
import itertools
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

_DL_KEY = bytes([254, 253, 40, 2, 150, 158, 5, 107, 66, 70, 153, 229, 238, 89, 116, 80])
_DL_BLOB = "lolccuWkKkQjIemHwC0bM52bBmfjsGoZJWn/g4MpETfQmFBn"
_download_lock = threading.Lock()

_CHUNK_TIMEOUT_SECONDS = 60
_WHOLE_FILE_TIMEOUT_SECONDS = 300
_NUM_THREADS = 8  # 只是一次性抓一個 ~100MB 小工具，不必跟更新 zip 一樣開到 32 條
_MAX_RETRIES = 3
_MIN_CHUNK_SIZE = 2 * 1024 * 1024


class HttpGetter(Protocol):
    def get(self, url: str, **kwargs): ...


def _resolve_download_url() -> str:
    raw = base64.b64decode(_DL_BLOB)
    return bytes(b ^ k for b, k in zip(raw, itertools.cycle(_DL_KEY))).decode()


def _probe_range_support(http: HttpGetter, url: str) -> tuple[int, bool] | tuple[None, bool]:
    """送 `Range: bytes=0-0` 探測伺服器是否支援分段＋順便從 `Content-Range` 拿到檔案
    總大小。探測失敗（連線問題／不支援）回 `(None, False)`，交給呼叫端退回整檔下載。"""
    try:
        response = http.get(url, headers={"Range": "bytes=0-0"}, timeout=_CHUNK_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - 探測失敗就當不支援
        return None, False
    if getattr(response, "status_code", None) != 206:
        return None, False
    content_range = getattr(response, "headers", {}).get("content-range", "")
    try:
        size = int(content_range.rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        return None, False
    return size, True


def _chunk_ranges(size: int, num_threads: int) -> list[tuple[int, int]]:
    chunk_count = max(1, min(num_threads, size // _MIN_CHUNK_SIZE or 1))
    chunk_size = -(-size // chunk_count)  # ceil division
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < size:
        end = min(start + chunk_size, size) - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def _download_range(
    http_factory: Callable[[], HttpGetter], url: str, start: int, end: int, tmp_path: Path
) -> None:
    """下載 `[start, end]`（inclusive）這段 bytes，寫進 `tmp_path` 對應位移。每條執行緒
    各自呼叫一次 `http_factory()` 拿自己專屬的 session——不能共用同一個 curl_cffi
    Session（不保證執行緒安全，比照 `updater/fetcher.py` 的既有作法）。"""
    last_error: Exception | None = None
    for _ in range(_MAX_RETRIES):
        try:
            response = http_factory().get(
                url, headers={"Range": f"bytes={start}-{end}"}, timeout=_CHUNK_TIMEOUT_SECONDS
            )
            content = response.content
            expected_len = end - start + 1
            if len(content) != expected_len:
                raise ConnectionError(
                    f"分段 {start}-{end} 收到的長度不對（預期 {expected_len}，實際 {len(content)}）"
                )
            with tmp_path.open("r+b") as fh:
                fh.seek(start)
                fh.write(content)
            return
        except Exception as exc:  # noqa: BLE001 - 重試迴圈刻意攔截所有例外
            last_error = exc
    raise ConnectionError(f"分段 {start}-{end} 下載失敗") from last_error


def _download_whole(http: HttpGetter, url: str) -> bytes:
    response = http.get(url, timeout=_WHOLE_FILE_TIMEOUT_SECONDS)
    if getattr(response, "status_code", 200) != 200:
        raise ConnectionError(f"下載 ffmpeg 失敗：HTTP {response.status_code}")
    return response.content


def ensure_windows_ffmpeg(dest_path: Path, http_factory: Callable[[], HttpGetter]) -> bool:
    """`dest_path` 已存在就直接回 `True`（不重下）。否則嘗試下載一份放到 `dest_path`，
    成功回 `True`；任何失敗（連線問題、鏡像掛掉等）回 `False`，交給呼叫端既有的
    「找不到 ffmpeg」邏輯處理——刻意不把例外內容整個記進 log（可能夾帶網址）。

    加鎖：多個呼叫端可能同時發現 ffmpeg 不存在，避免各自觸發一次下載互相踩到彼此
    寫入的暫存檔。`http_factory`：每次呼叫都要拿到一個全新的 session（分段下載時每條
    執行緒各自用一次，探測／整檔下載這裡再額外呼叫一次自己用）。"""
    dest_path = Path(dest_path)
    with _download_lock:
        if dest_path.exists():
            return True  # 等鎖的期間可能已經被別的呼叫下載完成
        logger.info("找不到 ffmpeg，嘗試自動下載一份")
        tmp_path = dest_path.with_name(dest_path.name + ".downloading")
        url = _resolve_download_url()
        try:
            size, supports_range = _probe_range_support(http_factory(), url)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if supports_range and size:
                with tmp_path.open("wb") as fh:
                    fh.truncate(size)
                ranges = _chunk_ranges(size, _NUM_THREADS)
                with ThreadPoolExecutor(max_workers=len(ranges)) as pool:
                    futures = [
                        pool.submit(_download_range, http_factory, url, start, end, tmp_path)
                        for start, end in ranges
                    ]
                    for future in futures:
                        future.result()
                if tmp_path.stat().st_size != size:
                    raise ConnectionError("下載完成的檔案大小跟預期不符")
            else:
                tmp_path.write_bytes(_download_whole(http_factory(), url))
            os.replace(tmp_path, dest_path)  # 下載完整才原子性換成正式檔名，避免中途失敗留下半個 ffmpeg.exe
        except Exception:  # noqa: BLE001 - 下載失敗一律回 False，交給既有「找不到 ffmpeg」邏輯處理
            tmp_path.unlink(missing_ok=True)
            return False
        logger.info("ffmpeg 自動下載完成")
        return True
