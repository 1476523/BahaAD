"""`ImageCacheFetcher`：把番劇封面／集數縮圖的 bytes 抓到本地檔。

規格見 docs/requirements/anime_cache.md。設計比照 `diagnostics/reporter.py`：佇列＋
單一背景執行緒，`enqueue()` 呼叫端完全不會被網路拖慢；抓失敗完全吞掉（只記 debug）。
低速（每張之間 sleep 一點），避免第一次開首頁瞬間對圖片 CDN 打 60 個請求。

bytes 存 `<images_dir>/<url_hash>`（沒有副檔名——`/cache/img/<hash>` 回應時用
`image_cache.content_type` 帶 Content-Type）。帳（大小、時間）存
`AnimeCacheStore.image_cache` 表，只有「已成功抓到」的列。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from pathlib import Path

from bahaad.store.anime_cache import AnimeCacheStore, url_hash

logger = logging.getLogger(__name__)

# 常見圖檔的 magic bytes：JPEG / PNG / GIF / WebP(RIFF....WEBP) / BMP
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"BM")


def _looks_like_image(data: bytes, content_type: str | None) -> bool:
    if content_type and content_type.split(";", 1)[0].strip().lower().startswith("image/"):
        return True
    head = data[:16]
    if head.startswith(_IMAGE_MAGIC):
        return True
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return True
    return False

_MAX_QUEUE = 4000
_DEFAULT_DELAY_SECONDS = 1.5
# 佇列積壓超過這個量（＝剛開一頁、幾十張封面待補）就切到 `_BURST_DELAY_SECONDS`
# 快速清空——302 回退期越短，瀏覽器對圖片 CDN 的密集直連就越少。
_BURST_THRESHOLD = 8
_BURST_DELAY_SECONDS = 0.25
_FETCH_TIMEOUT_SECONDS = 20.0
# `/cache/img` 未命中時的「當場抓」：比背景抓短的逾時（不能讓一次頁面 render 卡住），
# 同時最多幾個 worker 在做（其餘退回背景佇列 + 302），避免 40 張封面的頁面把 waitress
# 的 8 條 worker 全佔滿、其他請求（換頁 / API）跟著卡（使用者 2026-09-08 破圖問題）。
_SYNC_FETCH_TIMEOUT_SECONDS = 8.0
_SYNC_FETCH_CONCURRENCY = 3
_MAX_BYTES = 8 * 1024 * 1024  # 單張封面不可能到 8MB，超過視為異常、不存
_PRUNE_INTERVAL_SECONDS = 6 * 3600  # 這條背景執行緒順便每 6 小時清一次過期／孤兒快取
_DEFAULT_TTL_DAYS = 90


class ImageCacheFetcher:
    def __init__(
        self,
        cache_store: AnimeCacheStore,
        images_dir: Path,
        http,
        *,
        settings=None,
        delay_seconds: float = _DEFAULT_DELAY_SECONDS,
        burst_delay_seconds: float = _BURST_DELAY_SECONDS,
    ) -> None:
        self._cache = cache_store
        self._images_dir = Path(images_dir)
        self._images_dir.mkdir(parents=True, exist_ok=True)
        self._http = http
        self._settings = settings
        self._delay = delay_seconds
        self._burst_delay = burst_delay_seconds
        # -inf（不是 0）→ 開機後第一次 _maybe_prune() 一定會跑。`time.monotonic()` 在
        # Windows 是「開機到現在的秒數」，剛開機時比 _PRUNE_INTERVAL_SECONDS 小，用 0.0
        # 當基準會讓「啟動時清一次」被推遲到開機滿 6 小時才發生。
        self._last_prune = float("-inf")
        self._queue: "queue.Queue[str]" = queue.Queue(maxsize=_MAX_QUEUE)
        self._queued: set[str] = set()  # 記憶體去重，避免同一輪把同一張塞很多次
        self._queued_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        # `fetch_now()` 的 single-flight（同一張圖同時只有一條 worker 真的去抓，其餘
        # 等它）＋ 併發上限（別把 web worker 全佔滿）。
        self._inflight: dict[str, threading.Event] = {}
        self._inflight_lock = threading.Lock()
        self._sync_slots = threading.Semaphore(_SYNC_FETCH_CONCURRENCY)

    # ------------------------------------------------------------------

    def path_for(self, url: str) -> Path:
        return self._images_dir / url_hash(url)

    def path_for_hash(self, url_hash_value: str) -> Path:
        """磁碟檔名就是 `url_hash`——`/cache/img/<hash>` 在 DB 查不到列（被清過 / race）
        但檔案還在時，直接用這個路徑送圖，不要白白 404（使用者 2026-09-08）。"""
        return self._images_dir / url_hash_value

    def has_local(self, url: str) -> bool:
        return bool(url) and self.path_for(url).exists()

    def fetch_now(self, url: str, timeout: float = _SYNC_FETCH_TIMEOUT_SECONDS) -> Path | None:
        """當場把這張圖抓到本地檔並回傳路徑；抓不到（逾時 / 併發滿 / 網路失敗）回 None。
        `/cache/img/<hash>` 未命中時呼叫——比「302 導去圖片 CDN 讓瀏覽器自己直連」可靠
        （一頁 3、40 張同時直連 CDN 常有幾張失敗＝破圖）。

        single-flight：同一張圖同時只有第一條 worker 真的去抓，其餘等它抓完共用結果。
        併發上限 `_SYNC_FETCH_CONCURRENCY`：搶不到名額就直接回 None（呼叫端會改排背景
        佇列 + 回 302），不讓一次頁面 render 佔滿所有 web worker。"""
        if not url or not str(url).startswith(("http://", "https://")):
            return None
        if self.has_local(url):
            return self.path_for(url)

        with self._inflight_lock:
            waiter = self._inflight.get(url)
            leader = waiter is None
            if leader:
                waiter = threading.Event()
                self._inflight[url] = waiter

        if not leader:
            waiter.wait(timeout)
            p = self.path_for(url)
            return p if p.exists() else None

        try:
            if not self._sync_slots.acquire(blocking=False):
                return None  # 併發滿——交給背景佇列
            try:
                self._fetch_one(url, timeout=timeout)
            finally:
                self._sync_slots.release()
        finally:
            with self._inflight_lock:
                self._inflight.pop(url, None)
            waiter.set()

        p = self.path_for(url)
        return p if p.exists() else None

    def enqueue(self, url: str) -> None:
        """把圖片排進待抓佇列（純記憶體，不碰 DB）。呼叫端不會被拖慢。已在本地就跳過。"""
        if not url or not str(url).startswith(("http://", "https://")):
            return
        if self.has_local(url):
            return
        with self._queued_lock:
            if url in self._queued:
                return
            self._queued.add(url)
        try:
            self._queue.put_nowait(url)
        except queue.Full:
            logger.debug("圖片佇列已滿，捨棄：%s", url)
            with self._queued_lock:
                self._queued.discard(url)

    def enqueue_many(self, urls) -> None:
        for u in urls:
            self.enqueue(u)

    # ------------------------------------------------------------------
    # 背景執行緒
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        # 重啟後：登記過但 bytes 還沒抓的補排進來
        try:
            self.enqueue_many(self._cache.pending_image_urls())
        except Exception:
            logger.debug("補排待抓圖片失敗", exc_info=True)
        self._thread = threading.Thread(target=self._run, name="image-cache", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._maybe_prune()
            try:
                url = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            with self._queued_lock:
                self._queued.discard(url)
            try:
                self._fetch_one(url)
            except Exception:
                logger.debug("抓圖片時發生未預期例外：%s", url, exc_info=True)
            # 佇列還積著一堆（＝剛開一頁、幾十張封面還沒補）→ 用短間隔快速清空，
            # 讓 `/cache/img` 的 302 回退期越短越好（使用者 2026-09-08「番劇圖有時候
            # 會缺圖」：302 導去圖片 CDN、瀏覽器短時間打幾十個常有幾張失敗）。積壓
            # 清完、只是零星補圖時才回到正常的低速間隔，不長時間對 CDN 密集打。
            delay = self._burst_delay if self._queue.qsize() > _BURST_THRESHOLD else self._delay
            self._stop_event.wait(delay)

    def _maybe_prune(self) -> None:
        now = time.monotonic()
        if now - self._last_prune < _PRUNE_INTERVAL_SECONDS:
            return
        self._last_prune = now
        try:
            ttl = _DEFAULT_TTL_DAYS
            if self._settings is not None:
                ttl = int(self._settings.get("anime_cache_ttl_days", _DEFAULT_TTL_DAYS))
            result = self._cache.prune(ttl)
            n = self.remove_hashes(result["removed_image_hashes"])
            if result["stale_detail"] or n:
                logger.info(
                    "番劇快取清理：作廢 %d 部快取、刪 %d 個圖片檔",
                    result["stale_detail"], n,
                )
        except Exception:
            logger.debug("番劇快取定期清理失敗", exc_info=True)

    def _fetch_one(self, url: str, timeout: float = _FETCH_TIMEOUT_SECONDS) -> None:
        if self.has_local(url):
            return
        try:
            resp = self._http.get(url, timeout=timeout)
        except Exception:
            logger.debug("圖片下載失敗（網路）：%s", url, exc_info=True)
            return
        if getattr(resp, "status_code", 0) != 200:
            logger.debug("圖片下載失敗（HTTP %s）：%s", getattr(resp, "status_code", "?"), url)
            return
        data = resp.content or b""
        if not data or len(data) > _MAX_BYTES:
            return
        content_type = None
        try:
            content_type = resp.headers.get("content-type")
        except Exception:
            pass
        # CDN 被打太兇時可能回 200 + 一頁 HTML/JSON 錯誤訊息。寫進快取就是永久破圖
        # （之後都走磁碟）。content-type 不是 image/*、magic bytes 也不像圖 → 丟掉、
        # 不記帳，留給下次重試（使用者 2026-09-08 破圖排查的防呆）。
        if not _looks_like_image(data, content_type):
            logger.debug("圖片回應不像圖片（content-type=%r，前 8 bytes=%r）：%s",
                         content_type, data[:8], url)
            return
        try:
            self.path_for(url).write_bytes(data)
        except OSError:
            logger.debug("圖片寫檔失敗：%s", url, exc_info=True)
            return
        try:
            self._cache.record_image(url, content_type, len(data))
        except Exception:
            logger.debug("圖片記帳失敗：%s", url, exc_info=True)

    # ------------------------------------------------------------------

    def remove_hashes(self, hashes) -> int:
        """刪掉指定 hash 的磁碟圖片檔（`AnimeCacheStore.prune()` 已經刪了帳）。"""
        removed = 0
        for h in hashes:
            path = self._images_dir / h
            try:
                if path.is_file():
                    path.unlink()
                    removed += 1
            except OSError:
                pass
        return removed

    def clear_disk(self) -> int:
        removed = 0
        try:
            for path in self._images_dir.iterdir():
                if path.is_file():
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        pass
        except OSError:
            pass
        return removed

    def disk_usage_bytes(self) -> int:
        """快取圖片目錄目前佔用的位元組數（設定頁「番劇資料快取」大小顯示用）。"""
        total = 0
        try:
            for path in self._images_dir.iterdir():
                if path.is_file():
                    try:
                        total += path.stat().st_size
                    except OSError:
                        pass
        except OSError:
            pass
        return total
