"""從 `access_gate_server` 讀彙總後的統計數字，給網頁前端顯示用。規格見
docs/requirements/realtime_stats.md。

`web/stats.py` 的本地端點呼叫這裡；前端 JS 打的是 `web/stats.py` 的本地端點、不是
直接打 access_gate_server（避免每個訪客都打後端、也避免前端知道後端網址）。

**短快取**：每個 key 快取幾十秒——番劇頁一次可能查很多 sn、隱私浮層每分鐘刷新，
不該每次都真的打後端。後端沒上線／連不上就回上一次的值或空 dict（前端顯示「—」）。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Protocol

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 45
_DEFAULT_TIMEOUT = 4.0
# 每個瀏覽過的番劇頁會產生一個 anime:<sns> 快取 key——長時間跑會累積，設個上限
_MAX_CACHE_ENTRIES = 64


class HttpClient(Protocol):
    def get(self, url: str, **kwargs) -> object: ...
    def post(self, url: str, **kwargs) -> object: ...


class StatsQuery:
    def __init__(
        self,
        http: HttpClient,
        base_url: str,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._http = http
        self._base_url = base_url.rstrip("/")
        self._ttl = ttl_seconds
        self._timeout = timeout
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, object]] = {}

    def _cached(self, key: str):
        with self._lock:
            hit = self._cache.get(key)
        if hit and time.time() - hit[0] < self._ttl:
            return hit[1]
        return None

    def _store(self, key: str, value) -> None:
        with self._lock:
            self._cache[key] = (time.time(), value)
            if len(self._cache) > _MAX_CACHE_ENTRIES:
                # 丟掉最舊的那筆（time.time() 存在 tuple[0]）
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                self._cache.pop(oldest, None)

    def _get_json(self, path: str) -> dict | None:
        try:
            resp = self._http.get(f"{self._base_url}{path}", timeout=self._timeout)
            if 200 <= getattr(resp, "status_code", 0) < 300:
                return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug("stats 查詢 %s 失敗：%s", path, exc)
        return None

    def _post_json(self, path: str, payload: dict) -> dict | None:
        try:
            resp = self._http.post(
                f"{self._base_url}{path}", json=payload, timeout=self._timeout
            )
            if 200 <= getattr(resp, "status_code", 0) < 300:
                return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.debug("stats 查詢 %s 失敗：%s", path, exc)
        return None

    # ---- 對外（web/stats.py 呼叫）------------------------------------

    def summary(self) -> dict:
        """總下載／使用人數／在線人數／所有使用者已回報錯誤次數。拿不到回上次的、
        再拿不到回 {}。"""
        cached = self._cached("summary")
        if cached is not None:
            return cached
        fresh = self._get_json("/stats/summary")
        if fresh is not None:
            self._store("summary", fresh)
            return fresh
        stale = self._cache.get("summary")
        return stale[1] if stale else {}

    def anime(self, sns: list[int]) -> dict:
        """一批番劇首集 sn → 每個的 favorite/subscription/views/completions/watching。
        回 `{str(sn): {...}}`。"""
        if not sns:
            return {}
        want = sorted({int(s) for s in sns if s})
        key = "anime:" + ",".join(str(s) for s in want)
        cached = self._cached(key)
        if cached is not None:
            return cached
        fresh = self._post_json("/stats/anime", {"sns": want})
        result = (fresh or {}).get("anime", {}) if fresh is not None else {}
        if fresh is not None:
            self._store(key, result)
        return result

    def leaderboard(self) -> dict:
        """四個榜（訂閱數／收藏數／總觀看／看完）的前 N 名。"""
        cached = self._cached("leaderboard")
        if cached is not None:
            return cached
        fresh = self._get_json("/stats/leaderboard")
        if fresh is not None:
            self._store("leaderboard", fresh)
            return fresh
        stale = self._cache.get("leaderboard")
        return stale[1] if stale else {}
