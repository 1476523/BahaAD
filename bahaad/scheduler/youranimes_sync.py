"""每小時抓 youranimes.tw 季度頁、解析、寫進本地快取的背景 job。

規格見 docs/requirements/youranimes.md。結構比照 `scheduler/version_check.py`：
`threading.Event` + daemon thread + `start()` 防重入 + `stop()` join + `_run_loop`。

- 抓「當季 + 前 2 季」（涵蓋橫跨兩季的連續放送番劇）。當季每輪都抓；前面的季度頁
  改動慢，每 24h 才重抓一次。
- youranimes 不支援 conditional GET（無 ETag / Last-Modified）→ 抓下來算 SHA-256，
  跟上次一樣就只 bump `fetched_at`、跳過整批子表重寫。
- 一個季度 slug 抓失敗只記 warning、不影響其他 slug。
- 空 parse（改版 / 站方出錯）不覆蓋既有好資料。

這個補充功能一律開啟、沒有開關——它是單純的公開季度頁 GET（無 cookie、無識別參數），
沒有需要讓使用者關掉的理由（隱私說明見 docs/PRIVACY_POLICY.md）。
"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import date, datetime
from typing import Protocol

from bahaad.store.settings import SettingsStore
from bahaad.store.youranimes_cache import YourAnimesCacheStore
from bahaad.youranimes.fetch import YourAnimesError
from bahaad.youranimes.parse import (
    parse_anime_page,
    parse_season_page,
    season_page_all_anime_ids,
)
from bahaad.youranimes.season import youranimes_season_slugs

logger = logging.getLogger(__name__)

# 檢查週期固定 1 小時，不開放自訂（比照 version_check）
_INTERVAL_HOURS = 1
# 抓當季 + 前 2 季
_SEASON_COUNT = 3
# 非當季的季度頁改動很慢，隔這麼久才重抓一次
_STALE_SEASON_REFRESH_HOURS = 24
# 超過這麼多天沒再出現在任何季度頁的番劇資料就清掉
SETTINGS_KEY_RETENTION_DAYS = "youranimes_retention_days"
DEFAULT_RETENTION_DAYS = 400
# 季度卡片抓不到（18 禁／跨季延續播出）才逐一補抓個別番劇頁，且只在季度頁內容本身有變
# （content_hash 不同）時才做一次——這是一次性的補洞，不是每小時都重打（使用者
# 2026-09-01 的「訪問量過大」顧慮，靠這個節流，而不是限制數量）。每輪還是設個上限防
# 失控；實測 2026-07 號季度頁「跨季動畫」約 26 部，抓不到卡片格的都在這個量級。
_MAX_FALLBACK_FETCHES_PER_SEASON = 30

_SEASON_MONTH_NAMES = {"01": "一月", "04": "四月", "07": "七月", "10": "十月"}


def _season_label(slug: str) -> str:
    """`"202607"` → `"2026 年七月號"`（日誌訊息用，比原始 slug 好讀）。"""
    if len(slug) == 6 and slug.isdigit():
        return f"{slug[:4]} 年{_SEASON_MONTH_NAMES.get(slug[4:], slug[4:] + ' 月')}號"
    return slug


class _SeasonFetcher(Protocol):
    def fetch_season(self, slug: str) -> str: ...
    def fetch_anime(self, anime_id: int) -> str: ...


class YourAnimesSync:
    def __init__(
        self,
        settings: SettingsStore,
        store: YourAnimesCacheStore,
        fetcher: _SeasonFetcher,
        *,
        clock=date.today,
    ) -> None:
        self._settings = settings
        self._store = store
        self._fetcher = fetcher
        self._clock = clock
        self._stop_event = threading.Event()
        # 被 `wake()` set 一下就讓 `_run_loop` 的 wait() 提早結束、立刻再跑一輪
        # check_once()——清除快取後不要乾等一小時（使用者 2026-09-08）。
        self._wake_event = threading.Event()
        self._thread: threading.Thread | None = None

    def check_once(self) -> None:
        slugs = youranimes_season_slugs(self._clock(), _SEASON_COUNT)
        for i, slug in enumerate(slugs):
            if not self._due(slug, is_current=(i == 0)):
                continue
            try:
                html = self._fetcher.fetch_season(slug)
            except YourAnimesError as exc:
                logger.warning("季度頁 %s 抓取失敗：%s", _season_label(slug), exc)
                continue

            content_hash = hashlib.sha256(html.encode("utf-8", "replace")).hexdigest()
            meta = self._store.season_meta(slug)
            if meta is not None and meta["content_hash"] == content_hash:
                self._store.touch_season(slug, content_hash)
                self._backfill_missing(html, slug)
                continue

            records = parse_season_page(html, season_slug=slug)
            if not records:
                logger.warning(
                    "季度頁 %s 回應正常但沒解析出任何番劇（來源網站可能改版）",
                    _season_label(slug),
                )
                continue
            # 季度頁 content_hash 幾乎每小時都會變（當季頁面本來就常動）→ 每輪都會走到
            # 這裡重新 `replace_season`。`replace_season` 會把「這個 slug 有、但這次
            # `<article>` 沒解析到」的 anime_id 刪掉——上一輪透過個別頁補抓的 18 禁／
            # 跨季番劇就是這種，會被連帶洗掉、下一輪又整批重抓（使用者 2026-09-08 回報
            # 日誌每小時都「補齊了 12 部」＝訪問量過大）。先把上一輪補過、這次 article
            # 沒有的那幾筆從快取讀回來一起塞進 `records`，`replace_season` 就會保留它們，
            # `_backfill_missing` 之後只會抓真的還沒有的。
            self._carry_over_backfilled(html, records)
            self._store.replace_season(slug, records, content_hash)
            logger.info(
                "季度頁資訊於 %s 已更新：%d 部番劇", _season_label(slug), len(records)
            )
            self._backfill_missing(html, slug)

    def _carry_over_backfilled(self, season_html: str, records: list) -> None:
        """把「季度頁結構化資料裡有、但這次 `<article>` 沒解析到、而且快取裡已經有」的
        番劇（＝上一輪個別頁補抓來的）加進 `records`，避免被 `replace_season` 洗掉。"""
        all_ids = season_page_all_anime_ids(season_html)
        if not all_ids:
            return
        have = {r.anime_id for r in records}
        for aid in all_ids:
            if aid in have:
                continue
            existing = self._store.get(aid)
            if existing is not None:
                records.append(existing)

        retention = int(self._settings.get(SETTINGS_KEY_RETENTION_DAYS, DEFAULT_RETENTION_DAYS))
        try:
            self._store.prune(retention)
        except Exception:  # noqa: BLE001
            logger.debug("季度頁資訊 prune 失敗", exc_info=True)

    def _backfill_missing(self, season_html: str, slug: str) -> None:
        """季度頁的 `<article>` 卡片格只涵蓋一般分級、當季／前幾季的番劇；18 禁內容跟
        跨季延續播出的番劇有時只出現在頁面的 `ItemList` 結構化資料裡、沒有卡片格
        （見 docs/requirements/youranimes.md）。這裡逐一補抓那些漏掉的個別番劇頁
        （`/animes/<id>`），一樣完整（簡介／製作／配音／音樂），用 `upsert_anime`
        單筆寫入（不像 `replace_season` 整季重寫，才不會因為這幾筆的 slug 一樣就把
        同季其他番劇洗掉）。

        **靠查快取本身判斷缺不缺**（`store.get(id) is None`），不是靠這一輪有沒有
        重新解析 `<article>`——季度頁內容沒變時 `check_once` 會跳過整批重寫（省 DB
        churn），但漏掉的番劇不會因為「頁面沒變」就自己出現，每輪都要補查一次；已經
        補過的（`store.get` 找得到）就不會重複打 `/animes/<id>`。單一 id 抓失敗只記
        debug、不影響其他 id 或整批季度頁更新。"""
        all_ids = season_page_all_anime_ids(season_html)
        if not all_ids:
            return
        missing = [aid for aid in all_ids if self._store.get(aid) is None]
        if not missing:
            return
        if len(missing) > _MAX_FALLBACK_FETCHES_PER_SEASON:
            logger.info(
                "季度頁 %s 有 %d 部番劇卡片格抓不到，這輪只補前 %d 部",
                _season_label(slug), len(missing), _MAX_FALLBACK_FETCHES_PER_SEASON,
            )
            missing = missing[:_MAX_FALLBACK_FETCHES_PER_SEASON]

        added = 0
        for anime_id in missing:
            try:
                page_html = self._fetcher.fetch_anime(anime_id)
            except YourAnimesError as exc:
                logger.debug("個別番劇頁 %s 抓取失敗：%s", anime_id, exc)
                continue
            record = parse_anime_page(page_html, anime_id, season_slug=slug)
            if record is None:
                continue
            self._store.upsert_anime(record)
            added += 1
        if added:
            logger.info(
                "季度頁 %s 透過個別番劇頁補齊了 %d 部（18 禁／跨季）番劇",
                _season_label(slug), added,
            )

    def _due(self, slug: str, *, is_current: bool) -> bool:
        meta = self._store.season_meta(slug)
        if meta is None or meta["fetched_at"] is None:
            return True
        if is_current:
            return True
        age_hours = (datetime.now() - meta["fetched_at"]).total_seconds() / 3600
        return age_hours >= _STALE_SEASON_REFRESH_HOURS

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()  # 也叫醒 wait()，讓執行緒馬上看到 stop
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def wake(self) -> None:
        """叫醒背景執行緒立刻再跑一輪 `check_once()`（不等每小時那一輪）。清除快取後
        由 `web/settings.py` 呼叫——清完快取當下就重抓，而不是被動等到下一個整點。
        `check_once()` 自己有 `_due()` 節流，重複叫醒不會多打站方。"""
        self._wake_event.set()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.check_once()
            except Exception:
                logger.exception("季度頁資訊同步這一輪發生未預期的例外")
            # 等到「滿一小時」或「被 wake()／stop() 叫醒」任一先發生。stop() 會同時
            # set 這個 event，所以下一圈的 while 條件就會看到 stop 而收工。
            self._wake_event.wait(_INTERVAL_HOURS * 3600)
            self._wake_event.clear()
