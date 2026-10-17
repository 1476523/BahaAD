"""下載整包更新 zip（GitHub Release 附件）。規格見 docs/requirements/updater.md。

2026-09-25 改版：從「逐檔比對 manifest 下載差異檔案」全面改成「整包下載 GitHub
Release 的 zip」。使用者回報差異更新在他的網路環境下常常卡住／永遠下載不完，追查
＋實測（見 docs/decisions/0001 當日附加章節）發現根本原因是 raw.githubusercontent.com
（原本差異更新檔案來源）完全不支援 HTTP Range——送 `Range: bytes=0-1023` 照樣回整份
200，導致：①沒辦法分段續傳，單一大檔案（`BahaAD.exe`）斷線就整個作廢重來；②沒辦法
多線程分段加速。GitHub Release 附件走的是 Azure Blob 儲存，Range 請求會正確回 206
Partial Content——`download_zip()` 靠這個做多線程分段下載＋每段各自獨立重試，使用者
實測單線程只有幾十 KB/s、多線程能明顯加快（跟 IDM 這類下載管理器同樣的原理：多條
並行連線繞過單一連線的頻寬限制）。伺服器探測不支援分段（極端情況：镜像/代理不支援、
或檔案太小不值得分段）時退回單線程整檔下載，行為與改版前一致。
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Mapping, Protocol

logger = logging.getLogger(__name__)

_DEFAULT_MAX_RETRIES = 3
# 分段下載每段各自的逾時——比改版前單一大檔案的 600 秒短很多是刻意的：分段之後每段
# 本來就小很多（幾 MB），不需要那麼長的容忍時間；真的卡住的分段能更快被判定逾時、
# 重試，不必像以前一樣「賭」一整個大檔案的傳輸過程都不出狀況。
_CHUNK_TIMEOUT_SECONDS = 60
# 整檔（不支援分段時的退路）沿用改版前放寬過的逾時——理由見改版前的既有註解：
# 使用者網路慢，30 秒的預設值會把「真的還在下載中」誤判成逾時。
_WHOLE_FILE_TIMEOUT_SECONDS = 600
_DEFAULT_NUM_THREADS = 32  # 使用者要求「跟 IDM 一樣開到 32 線程」
# 分段下限：檔案（或最後一段）小於這個大小就不值得為了平行下載額外付出連線開銷。
_MIN_CHUNK_SIZE = 2 * 1024 * 1024
# 工作佇列的分片大小：原本是「檔案平均切成 32 大段、每條執行緒各領一段」，只要有一條連線
# 卡住，整包就得等它逾時（120 秒）再重來；改成很多個 1MB 小片、執行緒做完一片再領下一片，
# 卡住的連線只影響一小片，其他連線照樣把剩下的做完（使用者 2026-10-06：背景下載很久）。
_PIECE_SIZE = 512 * 1024
_RETRY_BACKOFF_SECONDS = 0.5


class FetchError(Exception):
    pass


class HashMismatchError(FetchError):
    """`FetchError` 的子類別，只在整包下載完但 SHA-256 跟 manifest 登記的值對不上時
    拋出，其他失敗（連線問題、分段逾時等）仍然是普通 `FetchError`——呼叫端
    （`updater/policy.py`）據此區分「真的是完整性比對失敗」跟「其他原因下載不成功」，
    用來正確填診斷回報的 `file_integrity_result` 欄位。"""


class HttpGetter(Protocol):
    def get(self, url: str, **kwargs) -> "_ResponseLike": ...


class _ResponseLike(Protocol):
    status_code: int
    headers: Mapping[str, str]
    content: bytes


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_matches(dest_path: Path, sha256: str, size: int) -> bool:
    """已經下載好、大小跟雜湊都對得上就不用重下——crash-safe resume：程式意外中斷或
    重新啟動之後，不會白白重下已經下載完成的整包 zip（跟改版前 `fetch_files()` 的
    逐檔跳過邏輯同樣的精神，只是現在只有一個檔案要顧）。"""
    if not dest_path.is_file():
        return False
    try:
        if dest_path.stat().st_size != size:
            return False
        return _sha256_file(dest_path) == sha256
    except OSError:
        return False


def _probe_range_support(http: HttpGetter, url: str) -> bool:
    """送 `Range: bytes=0-0` 探測伺服器是否真的支援分段——回 206 才算，回 200 代表
    伺服器直接忽略 Range header、整份送回來（這正是 raw.githubusercontent.com 的實測
    行為）。探測本身失敗（連線問題）保守當作不支援，退回整檔下載，不讓探測失敗變成
    整個更新失敗。"""
    try:
        response = http.get(url, headers={"Range": "bytes=0-0"}, timeout=_CHUNK_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - 探測失敗就當不支援，交給退路處理
        return False
    return getattr(response, "status_code", None) == 206


def _download_range(
    http: HttpGetter,
    url: str,
    start: int,
    end: int,
    dest_path: Path,
    max_retries: int,
) -> None:
    """下載 `[start, end]`（inclusive）這小片 bytes，寫進 `dest_path` 對應位移。獨立
    重試（失敗之間稍微退避）、不影響其他分片。`http` 由呼叫端（每條工作執行緒各一個）傳入，
    不能讓多條執行緒共用同一個 curl_cffi Session。"""
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            response = http.get(
                url, headers={"Range": f"bytes={start}-{end}"}, timeout=_CHUNK_TIMEOUT_SECONDS
            )
            content = response.content
            expected_len = end - start + 1
            if len(content) != expected_len:
                raise FetchError(
                    f"分段 {start}-{end} 收到的長度不對"
                    f"（預期 {expected_len} bytes，實際 {len(content)} bytes）"
                )
            with dest_path.open("r+b") as fh:
                fh.seek(start)
                fh.write(content)
            return
        except Exception as exc:  # noqa: BLE001 - 重試迴圈刻意攔截所有例外
            last_error = exc
            if attempt + 1 < max_retries:
                time.sleep(_RETRY_BACKOFF_SECONDS * (attempt + 1))
    raise FetchError(f"分段 {start}-{end} 下載失敗（重試 {max_retries} 次）") from last_error


def _download_whole(http: HttpGetter, url: str, dest_path: Path, max_retries: int) -> None:
    """伺服器不支援分段時的退路：單線程整檔下載，行為跟改版前一致。"""
    last_error: Exception | None = None
    for _ in range(max_retries):
        try:
            response = http.get(url, timeout=_WHOLE_FILE_TIMEOUT_SECONDS)
            dest_path.write_bytes(response.content)
            return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise FetchError(f"整檔下載失敗（重試 {max_retries} 次）") from last_error


def _chunk_ranges(size: int, num_threads: int) -> list[tuple[int, int]]:
    """切成每片最多 `_PIECE_SIZE` 的小片（`num_threads` 只決定同時幾條在做，不再決定切幾段）。"""
    ranges: list[tuple[int, int]] = []
    start = 0
    while start < size:
        end = min(start + _PIECE_SIZE, size) - 1
        ranges.append((start, end))
        start = end + 1
    return ranges


def _parts_path(dest_path: Path) -> Path:
    return dest_path.with_name(dest_path.name + ".parts")


def _load_done(parts_file: Path, dest_path: Path, url: str, sha256: str, size: int) -> set[tuple[int, int]]:
    """上一輪沒下載完留下的進度（已完成的分片）——同一個檔案（網址／雜湊／大小都一樣）、
    暫存檔大小也對才採信，不然從頭來。"""
    try:
        data = json.loads(parts_file.read_text(encoding="utf-8"))
        if (
            data.get("url") == url and data.get("sha256") == sha256 and data.get("size") == size
            and dest_path.is_file() and dest_path.stat().st_size == size
        ):
            return {(int(a), int(b)) for a, b in data.get("done", [])}
    except (OSError, ValueError, TypeError):
        pass
    return set()


def download_zip(
    http: HttpGetter,
    http_factory: Callable[[], HttpGetter],
    url: str,
    sha256: str,
    size: int,
    dest_path: Path,
    *,
    num_threads: int = _DEFAULT_NUM_THREADS,
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> None:
    """下載整包更新 zip 到 `dest_path`，完成後驗證整體 SHA-256，不符就刪掉重丟
    `HashMismatchError`（呼叫端據此判斷是完整性問題，不是單純連線問題）。

    `http`：只用來探測伺服器支不支援分段（單次請求，不需要執行緒安全）。
    `http_factory`：每條分段下載執行緒各自呼叫一次拿自己的 `HttpGetter`。"""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    if _file_matches(dest_path, sha256, size):
        return

    supports_range = size >= _MIN_CHUNK_SIZE and _probe_range_support(http, url)
    started = time.monotonic()
    parts_file = _parts_path(dest_path)

    if not supports_range:
        _download_whole(http, url, dest_path, max_retries)
    else:
        ranges = _chunk_ranges(size, num_threads)
        done = _load_done(parts_file, dest_path, url, sha256, size)
        if done:
            logger.info("更新檔續傳：先前已完成 %d/%d 片", len(done & set(ranges)), len(ranges))
        else:
            # 預先把檔案配置到完整大小，讓每條執行緒可以直接 seek 到自己的位移寫入
            with dest_path.open("wb") as fh:
                fh.truncate(size)
        todo = [r for r in ranges if r not in done]
        lock = threading.Lock()
        queue = list(reversed(todo))
        errors: list[Exception] = []

        def _save_progress() -> None:
            parts_file.write_text(
                json.dumps({"url": url, "sha256": sha256, "size": size, "done": sorted(done)}),
                encoding="utf-8",
            )

        def _worker() -> None:
            worker_http = http_factory()
            while True:
                with lock:
                    if not queue or errors:
                        return
                    start, end = queue.pop()
                try:
                    _download_range(worker_http, url, start, end, dest_path, max_retries)
                except Exception as exc:  # noqa: BLE001
                    with lock:
                        errors.append(exc)
                    return
                with lock:
                    done.add((start, end))
                    _save_progress()

        workers = max(1, min(num_threads, len(todo)))
        threads = [threading.Thread(target=_worker, daemon=True) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            # 進度（已完成的分片）留在 .parts，下一輪續傳，不必重頭來
            raise FetchError(f"更新檔分片下載失敗（{len(done)}/{len(ranges)} 片已完成）") from errors[0]

    parts_file.unlink(missing_ok=True)
    actual_sha256 = _sha256_file(dest_path)
    if actual_sha256 != sha256:
        dest_path.unlink(missing_ok=True)
        raise HashMismatchError(f"{dest_path.name} 下載後雜湊跟 manifest 登記的值不符")
    logger.info("更新檔下載完成：%.1f MB，耗時 %.0f 秒", size / 1048576, time.monotonic() - started)


def extract_zip(zip_path: Path, dest_dir: Path) -> None:
    """解壓縮 GitHub Release 的 zip 到 `dest_dir`。`scripts/build.py --zip` 刻意把整個
    安裝目錄包在單一頂層資料夾裡（讓使用者手動解壓到哪都不會散一地）——這裡要把那層
    頂層資料夾剝掉，讓 `dest_dir` 直接對應安裝目錄本身（`dest_dir/BahaAD.exe`，不是
    `dest_dir/BahaAD/BahaAD.exe`），`apply_update.ps1` 的 robocopy 才對得上。

    `dest_dir` 先整個清空再解壓——不是疊加：整包更新後，`dest_dir` 應該精確等於這個
    版本的完整安裝內容，不該摻雜前一次解壓留下的殘留檔案。"""
    dest_dir = Path(dest_dir)
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if not names:
            return
        top_level = names[0].split("/", 1)[0]
        all_under_top = all(name.startswith(top_level + "/") for name in names)
        for name in names:
            member = name[len(top_level) + 1 :] if all_under_top else name
            if not member:  # 頂層資料夾本身這個條目
                continue
            target = dest_dir / member
            if name.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(name) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
