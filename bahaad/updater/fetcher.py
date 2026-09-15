"""差異檔案下載。規格見 docs/requirements/updater.md。

跟 `downloader/segment.py` 的既有慣例一致：逐檔下載、下載完重新算 SHA-256 跟 manifest
登記的值比對，對不上就丟棄重試——這層防的是傳輸中斷/檔案不完整，manifest 本身的完整性
由 `manifest.py` 的簽章驗證負責，兩層各司其職。
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from bahaad.updater.manifest import ManifestFileEntry

_DEFAULT_MAX_RETRIES = 3


class FetchError(Exception):
    pass


class HashMismatchError(FetchError):
    """`FetchError` 的子類別，只在 SHA-256 比對不符那個分支拋出，其他失敗（連線問題等）
    仍然是普通 `FetchError`——讓呼叫端（`updater/policy.py`）能區分「真的是完整性比對
    失敗」跟「其他原因下載不成功」，用來正確填診斷回報的 `file_integrity_result` 欄位，
    見 docs/requirements/diagnostics.md「updater/fetcher.py 的小改動」一節。因為是子
    類別，所有既有 `except FetchError` 的地方行為不變。"""


class HttpGetter(Protocol):
    def get(self, url: str) -> "_ResponseLike": ...


class _ResponseLike(Protocol):
    content: bytes


def fetch_files(
    http: HttpGetter,
    entries: list[ManifestFileEntry],
    staging_dir: Path,
    max_retries: int = _DEFAULT_MAX_RETRIES,
) -> None:
    """把 entries 下載到 staging_dir，保留各自的相對路徑結構（applier.py 才知道要
    往哪裡覆蓋）。任何一個檔案重試用盡仍失敗就直接拋出，不吞掉——manifest 已經驗過
    簽章，值得信任內容應該要能正確下載，靜默跳過某個檔案反而會讓套用結果不完整。"""
    staging_dir = Path(staging_dir)
    for entry in entries:
        dest_path = staging_dir / entry.path
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        _download_one(http, entry, dest_path, max_retries)


def _download_one(http: HttpGetter, entry: ManifestFileEntry, dest_path: Path, max_retries: int) -> None:
    last_error: Exception | None = None
    for _ in range(max_retries):
        try:
            response = http.get(entry.url)
            content = response.content
            if hashlib.sha256(content).hexdigest() != entry.sha256:
                raise HashMismatchError(f"{entry.path} 下載後雜湊跟 manifest 登記的值不符")
            dest_path.write_bytes(content)
            return
        except Exception as exc:  # noqa: BLE001 - 重試迴圈刻意攔截所有例外
            last_error = exc
    raise FetchError(f"{entry.path} 下載失敗（重試 {max_retries} 次）") from last_error
