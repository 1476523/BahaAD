"""番劇資料本地快取的持久化。規格見 docs/requirements/anime_cache.md。

目的：`web/` UI 每開一次首頁／點一部番劇就爬一次 `ani.gamer.com.tw`，頻繁造訪容易
觸發站方風控。把「不常變的東西」（本季新番清單、週期表、番劇標題／簡介／集數清單、
封面圖）快取在本地，能用快取就不打網路。

存法（符合儲存原則，見 docs/decisions／記憶：正規化、每欄一個明確值、不用 JSON blob）：

- `home_card`：本季新番卡片，一列一張，`position` 保留原順序。
- `home_schedule_entry`：週期表項目，一列一項，`day_order`（1=週一…7=週日）＋`position`。
- `home_cache_meta`：單列（`id=1`），只存 `fetched_at`（上次真的爬首頁的時間）。
- `anime_detail_cache`：番劇詳細頁的典型欄位，`video_sn` PK。
- `anime_genre_cache`：番劇分類（重複群組），`(video_sn, position)` PK。
- `anime_episode_cache`：詳細頁的集數清單，一列一集。
- `anime_related_cache`：詳細頁的相關動畫，一列一部。
- `image_cache`：每個圖片 URL 一列，`url_hash` PK；bytes 存磁碟（不進 DB）。
- `homepage_section_card`／`homepage_section_meta`：首頁「新上架」（`#blockAnimeNewArrive`）
  區塊的卡片快取，`section` 欄位預留（目前只有 `new_arrival`）
  （2026-08-27 追加，固定短 TTL，見 web/anime_data.py `_HOMEPAGE_SECTION_CACHE_TTL_SECONDS`）。
- `ref_resolution`：`animeRef.php?sn={ref_sn}` → 真正 `video_sn` 的 302 解析結果
  （2026-08-27 追加，帶 `resolved_at`，TTL 沿用 `anime_cache_ttl_days`）。

TTL 由呼叫端（`prune(ttl_days)`）帶進來，預設值定義在 web/settings.py。
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime
from typing import Iterable

from bahaad.gamer_client.browse import (
    AnimeCard,
    AnimeDetail,
    Episode,
    EpisodeCategory,
    RelatedAnime,
    ScheduleEntry,
    SearchResult,
    WeeklySchedule,
)
from bahaad.store.database import Database

_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS home_card (
    video_sn INTEGER PRIMARY KEY,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    cover_url TEXT NOT NULL,
    time_text TEXT NOT NULL,
    episode_text TEXT NOT NULL,
    watch_count TEXT NOT NULL,
    air_date TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS home_schedule_entry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day_order INTEGER NOT NULL,
    day TEXT NOT NULL,
    position INTEGER NOT NULL,
    video_sn INTEGER NOT NULL,
    title TEXT NOT NULL,
    time_text TEXT NOT NULL,
    episode_text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS home_cache_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anime_detail_cache (
    video_sn INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    cover_url TEXT NOT NULL,
    air_date TEXT NOT NULL,
    director TEXT NOT NULL,
    distributor TEXT NOT NULL,
    producer TEXT NOT NULL,
    description TEXT NOT NULL,
    rating_score TEXT NOT NULL,
    rating_count TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS anime_genre_cache (
    video_sn INTEGER NOT NULL,
    position INTEGER NOT NULL,
    genre TEXT NOT NULL,
    PRIMARY KEY (video_sn, position)
);
CREATE TABLE IF NOT EXISTS anime_episode_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anime_video_sn INTEGER NOT NULL,
    category_order INTEGER NOT NULL,
    category TEXT NOT NULL,
    position INTEGER NOT NULL,
    episode_number TEXT NOT NULL,
    episode_video_sn INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS anime_related_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    anime_video_sn INTEGER NOT NULL,
    position INTEGER NOT NULL,
    ref_sn INTEGER NOT NULL,
    title TEXT NOT NULL,
    cover_url TEXT NOT NULL,
    year_text TEXT NOT NULL,
    episode_count_text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS image_cache (
    url_hash TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    content_type TEXT,
    byte_size INTEGER,
    fetched_at TEXT,          -- NULL = 登記過但 bytes 還沒抓
    first_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ref_resolution (
    ref_sn INTEGER PRIMARY KEY,
    video_sn INTEGER NOT NULL,
    resolved_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS homepage_section_card (
    section TEXT NOT NULL,
    position INTEGER NOT NULL,
    ref_sn INTEGER NOT NULL,
    title TEXT NOT NULL,
    cover_url TEXT NOT NULL,
    year_text TEXT NOT NULL,
    episode_count_text TEXT NOT NULL,
    PRIMARY KEY (section, ref_sn)
);
CREATE TABLE IF NOT EXISTS homepage_section_meta (
    section TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    extra TEXT NOT NULL DEFAULT '{}'
);
-- 「這一集屬於哪一部番劇」對應（使用者 2026-08-31：訂閱／改名要按番劇、不是按單一集數）。
-- group_key ＝該番劇所有已知集數 video_sn 的最小值，同一部番劇不管從哪一集的頁面進來
-- 都算出同一個 key。番劇頁快取存檔時一併寫入。
CREATE TABLE IF NOT EXISTS episode_group (
    episode_video_sn INTEGER PRIMARY KEY,
    group_key INTEGER NOT NULL,
    anime_title TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episode_group_key ON episode_group(group_key);
"""

# `homepage_section_card.section` 允許的值（目前只有「新上架」——「近期熱播」的
# `#blockHotAnime` 每次載入隨機洗牌、不適合快取，那頁改用 `list_anime(sort=2)`）
HOMEPAGE_SECTION_NEW_ARRIVAL = "new_arrival"

_DAY_ORDER = {"週一": 1, "週二": 2, "週三": 3, "週四": 4, "週五": 5, "週六": 6, "週日": 7}
_WEEKDAY_NAMES = ("週一", "週二", "週三", "週四", "週五", "週六", "週日")

# 後加欄位（對舊資料庫冪等 ALTER）。home_card.air_date：.0 改進.txt 第 23 項；
# homepage_section_meta.extra：近期熱播／搜尋番劇快取的分頁旗標 JSON（使用者 2026-09-06）
_ADDED_COLUMNS = {
    "home_card": {"air_date": "TEXT NOT NULL DEFAULT ''"},
    "homepage_section_meta": {"extra": "TEXT NOT NULL DEFAULT '{}'"},
    # image_cache.last_seen_at：任何頁面 render 時 register_images() 就更新——prune 的
    # 「孤兒清除」改看這個 + 寬限期，不會一次 6 小時的 prune 就把剛在下載列表/剛換過
    # URL 的封面砍掉導致一直破圖（使用者 2026-09-08「先前的快取圖都會失效」）。
    "image_cache": {"last_seen_at": "TEXT NOT NULL DEFAULT ''"},
}

# prune 的孤兒清除寬限期：距上次在任何頁面被引用超過這麼多天、且目前不在任何內容表
# 引用清單裡，才刪。TTL(anime_cache_ttl_days，預設 90) 到期則一律刪。
_IMAGE_ORPHAN_GRACE_DAYS = 21

# `homepage_section_card`/`_meta` 現在也拿來快取「近期熱播」「搜尋番劇（依屬性組合）」的
# 結果（section key 帶前綴，見 web/anime_data.cached_card_list）。`new_arrival` 以外的
# key 最多留這麼多組，`save_section` 每次寫入順手修剪最舊的（避免使用者亂點屬性組合把
# 表撐大）。
_DYNAMIC_SECTION_KEEP = 60


def url_hash(url: str) -> str:
    """圖片 URL → 檔名安全的短雜湊（sha256 前 16 bytes hex）。`/cache/img/<hash>` 用。"""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


class AnimeCacheStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.executescript(_TABLES_SQL)
            self._ensure_added_columns(conn)
            self._backfill_episode_group(conn)

    @staticmethod
    def _ensure_added_columns(conn) -> None:
        """後加的欄位對舊資料庫冪等補上（比照 store/downloaded_episodes.py）。"""
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    if (table, name) == ("home_card", "air_date"):
                        # 舊 home_card 每列 air_date 都會是 DEFAULT ''（→「其他」一組）。
                        # 清掉 meta 讓下次開首頁強制重抓一次、把日期標籤帶進來。
                        conn.execute("DELETE FROM home_cache_meta")

    @staticmethod
    def _backfill_episode_group(conn) -> None:
        """episode_group 是 2026-08-31 新增的表。舊資料庫已經有 anime_episode_cache
        （番劇頁快取的集數清單）＋ anime_detail_cache（標題）——一次性從那裡回填，
        使用者不用把每部番劇都重新點過一遍。"""
        if conn.execute("SELECT 1 FROM episode_group LIMIT 1").fetchone():
            return
        rows = conn.execute(
            "SELECT e.anime_video_sn, e.episode_video_sn, d.title "
            "FROM anime_episode_cache e LEFT JOIN anime_detail_cache d "
            "  ON d.video_sn = e.anime_video_sn"
        ).fetchall()
        by_anime: dict[int, dict] = {}
        for r in rows:
            g = by_anime.setdefault(r["anime_video_sn"], {"sns": set(), "title": ""})
            g["sns"].add(r["anime_video_sn"])
            g["sns"].add(r["episode_video_sn"])
            if r["title"]:
                g["title"] = r["title"]
        ts = datetime.now().isoformat(timespec="seconds")
        for g in by_anime.values():
            if not g["sns"]:
                continue
            key = min(g["sns"])
            conn.executemany(
                "INSERT OR IGNORE INTO episode_group "
                "(episode_video_sn, group_key, anime_title, updated_at) VALUES (?, ?, ?, ?)",
                [(sn, key, g["title"], ts) for sn in g["sns"]],
            )

    # ------------------------------------------------------------------
    # 首頁（本季新番 + 週期表）
    # ------------------------------------------------------------------

    def home_fetched_at(self) -> datetime | None:
        with self._database.transaction() as conn:
            row = conn.execute("SELECT fetched_at FROM home_cache_meta WHERE id = 1").fetchone()
        if row is None:
            return None
        try:
            return datetime.fromisoformat(row["fetched_at"])
        except (TypeError, ValueError):
            return None

    def get_home(self) -> tuple[list[AnimeCard], list[WeeklySchedule]] | None:
        """回傳 (本季新番卡片, 週期表)；快取空的時候回 None。"""
        with self._database.transaction() as conn:
            if conn.execute("SELECT 1 FROM home_cache_meta WHERE id = 1").fetchone() is None:
                return None
            card_rows = conn.execute(
                "SELECT * FROM home_card ORDER BY position"
            ).fetchall()
            sched_rows = conn.execute(
                "SELECT * FROM home_schedule_entry ORDER BY day_order, position"
            ).fetchall()

        cards = [
            AnimeCard(
                video_sn=r["video_sn"],
                title=r["title"],
                cover_url=r["cover_url"],
                time_text=r["time_text"],
                episode_text=r["episode_text"],
                watch_count=r["watch_count"],
                air_date=r["air_date"] if "air_date" in r.keys() else "",
            )
            for r in card_rows
        ]
        # 一律回 7 天（週一～週日），沒有番劇的那天也要在——不然 home.html 的
        # `loop.index` 會算出偏移的 data-weekday，週期表金框／首頁自動重載會挑錯時間
        # （code review）。`get_weekly_schedule()` 本來就固定回 7 天，這裡對齊。
        by_day: dict[str, list[ScheduleEntry]] = {}
        for r in sched_rows:
            by_day.setdefault(r["day"], []).append(
                ScheduleEntry(
                    video_sn=r["video_sn"],
                    title=r["title"],
                    time_text=r["time_text"],
                    episode_text=r["episode_text"],
                )
            )
        schedule = [WeeklySchedule(day=day, entries=by_day.get(day, [])) for day in _WEEKDAY_NAMES]
        return cards, schedule

    def save_home(
        self,
        cards: list[AnimeCard],
        schedule: list[WeeklySchedule],
        now: datetime | None = None,
    ) -> None:
        """整批換掉——同一交易刪光再插入，中途壞掉會 rollback、不留半套資料。"""
        now = now or datetime.now()
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute("DELETE FROM home_card")
                conn.execute("DELETE FROM home_schedule_entry")
                conn.executemany(
                    "INSERT INTO home_card "
                    "(video_sn, position, title, cover_url, time_text, episode_text, watch_count, air_date) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            c.video_sn, i, c.title, c.cover_url, c.time_text, c.episode_text,
                            c.watch_count, getattr(c, "air_date", ""),
                        )
                        for i, c in enumerate(cards)
                    ],
                )
                sched_rows = []
                for day in schedule:
                    day_order = _DAY_ORDER.get(day.day, 99)
                    for pos, e in enumerate(day.entries):
                        sched_rows.append(
                            (day_order, day.day, pos, e.video_sn, e.title, e.time_text, e.episode_text)
                        )
                conn.executemany(
                    "INSERT INTO home_schedule_entry "
                    "(day_order, day, position, video_sn, title, time_text, episode_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    sched_rows,
                )
                conn.execute(
                    "INSERT INTO home_cache_meta (id, fetched_at) VALUES (1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET fetched_at = excluded.fetched_at",
                    (now.isoformat(timespec="seconds"),),
                )

    def invalidate_detail(self, video_sn: int) -> bool:
        """把一部番劇的 detail 快取標記過期——下次「真的要重抓」的造訪（非公開模式）就會
        爬到最新的（集數清單、評分…）。**不整組刪掉**：只是把 `fetched_at` 打回很久以前
        （讓 `anime_detail_data()` 的新鮮度判斷認定它過期、觸發重抓），內容原封不動留著。

        公開模式（`cache_only=True`）永遠不會自己重抓，只要有快取（不管新不新鮮）就直接
        顯示——原本整組刪掉的話，觸發 2（排程時段偵測到新集數，不論下載最後成功或失敗）
        一發生，公開頁面就會馬上變成「尚無快取」、標題也退化成 `sn=`，1007 讓下載卡在
        重試迴圈時這個空窗期特別長更明顯（使用者 2026-09-06 回報）。回傳原本有沒有快取。"""
        with self._lock:
            with self._database.transaction() as conn:
                cur = conn.execute(
                    "UPDATE anime_detail_cache SET fetched_at = ? WHERE video_sn = ?",
                    (datetime.min.isoformat(timespec="seconds"), video_sn),
                )
                return cur.rowcount > 0

    def patch_home_card(
        self,
        video_sn: int,
        *,
        episode_text: str | None = None,
        time_text: str | None = None,
        cover_url: str | None = None,
    ) -> bool:
        """觸發 2（排程時段刷新）用：就地更新首頁那張卡片，不整份重抓。回傳有沒有那張卡片。"""
        fields = {"episode_text": episode_text, "time_text": time_text, "cover_url": cover_url}
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return False
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            with self._database.transaction() as conn:
                cur = conn.execute(
                    f"UPDATE home_card SET {assignments} WHERE video_sn = ?",
                    (*fields.values(), video_sn),
                )
                return cur.rowcount > 0

    def schedule_slots(self) -> list[tuple[int, str]]:
        """(day_order 1-7, "HH:MM") 清單——首頁重抓決策 `home_refresh_decision()` 用。"""
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT day_order, time_text FROM home_schedule_entry"
            ).fetchall()
        return [(r["day_order"], r["time_text"]) for r in rows]

    # ------------------------------------------------------------------
    # 首頁區塊快取（新上架）＋ animeRef.php 解析結果快取
    # ------------------------------------------------------------------

    def get_section(
        self, section: str
    ) -> tuple[list[SearchResult], datetime, dict] | None:
        """回傳 (該區塊卡片, fetched_at, extra)；快取空的時候回 None。`extra` 是分頁旗標
        之類的小 JSON dict（`{}` if 無）。`section` ＝ `HOMEPAGE_SECTION_NEW_ARRIVAL`
        或帶前綴的動態 key（近期熱播／搜尋番劇，見 web/anime_data.cached_card_list）。"""
        with self._database.transaction() as conn:
            meta = conn.execute(
                "SELECT fetched_at, extra FROM homepage_section_meta WHERE section = ?",
                (section,),
            ).fetchone()
            if meta is None:
                return None
            rows = conn.execute(
                "SELECT * FROM homepage_section_card WHERE section = ? ORDER BY position", (section,)
            ).fetchall()
        cards = [
            SearchResult(
                ref_sn=r["ref_sn"],
                title=r["title"],
                cover_url=r["cover_url"],
                year_text=r["year_text"],
                episode_count_text=r["episode_count_text"],
            )
            for r in rows
        ]
        try:
            fetched_at = datetime.fromisoformat(meta["fetched_at"])
        except (TypeError, ValueError):
            fetched_at = datetime.min
        try:
            extra = json.loads(meta["extra"]) if meta["extra"] else {}
        except (TypeError, ValueError):
            extra = {}
        return cards, fetched_at, extra

    def save_section(
        self,
        section: str,
        cards: list[SearchResult],
        now: datetime | None = None,
        *,
        extra: dict | None = None,
    ) -> None:
        """整批換掉該區塊——同一交易刪光再插入，比照 `save_home()`。`new_arrival` 以外的
        動態 key 順手修剪最舊的（`_DYNAMIC_SECTION_KEEP`）。"""
        now = now or datetime.now()
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute("DELETE FROM homepage_section_card WHERE section = ?", (section,))
                conn.executemany(
                    "INSERT INTO homepage_section_card "
                    "(section, position, ref_sn, title, cover_url, year_text, episode_count_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (section, i, c.ref_sn, c.title, c.cover_url, c.year_text, c.episode_count_text)
                        for i, c in enumerate(cards)
                    ],
                )
                conn.execute(
                    "INSERT INTO homepage_section_meta (section, fetched_at, extra) VALUES (?, ?, ?) "
                    "ON CONFLICT(section) DO UPDATE SET fetched_at = excluded.fetched_at, "
                    "extra = excluded.extra",
                    (
                        section,
                        now.isoformat(timespec="seconds"),
                        json.dumps(extra or {}, ensure_ascii=False),
                    ),
                )
                if section != HOMEPAGE_SECTION_NEW_ARRIVAL:
                    self._prune_dynamic_sections(conn)

    @staticmethod
    def _prune_dynamic_sections(conn) -> None:
        stale = [
            row["section"]
            for row in conn.execute(
                "SELECT section FROM homepage_section_meta "
                "WHERE section != ? ORDER BY fetched_at DESC LIMIT -1 OFFSET ?",
                (HOMEPAGE_SECTION_NEW_ARRIVAL, _DYNAMIC_SECTION_KEEP),
            )
        ]
        for key in stale:
            conn.execute("DELETE FROM homepage_section_card WHERE section = ?", (key,))
            conn.execute("DELETE FROM homepage_section_meta WHERE section = ?", (key,))

    def get_ref(self, ref_sn: int) -> tuple[int, datetime] | None:
        """`animeRef.php?sn={ref_sn}` → 真正 video_sn 的快取。回傳 (video_sn, resolved_at)
        或 None。**帶時間讓呼叫端自己判斷過不過期**——ref 解析出的是「該番劇最新一集」的
        sn，會隨新集數上架而變，不能永久快取。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT video_sn, resolved_at FROM ref_resolution WHERE ref_sn = ?", (ref_sn,)
            ).fetchone()
        if row is None:
            return None
        try:
            resolved_at = datetime.fromisoformat(row["resolved_at"])
        except (TypeError, ValueError):
            resolved_at = datetime.min
        return row["video_sn"], resolved_at

    def save_ref(self, ref_sn: int, video_sn: int, now: datetime | None = None) -> None:
        now = now or datetime.now()
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO ref_resolution (ref_sn, video_sn, resolved_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(ref_sn) DO UPDATE SET "
                    " video_sn = excluded.video_sn, resolved_at = excluded.resolved_at",
                    (ref_sn, video_sn, now.isoformat(timespec="seconds")),
                )

    # ------------------------------------------------------------------
    # 番劇詳細頁
    # ------------------------------------------------------------------

    def get_detail(self, video_sn: int) -> tuple[AnimeDetail, datetime] | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM anime_detail_cache WHERE video_sn = ?", (video_sn,)
            ).fetchone()
            if row is None:
                return None
            genre_rows = conn.execute(
                "SELECT genre FROM anime_genre_cache WHERE video_sn = ? ORDER BY position",
                (video_sn,),
            ).fetchall()
            ep_rows = conn.execute(
                "SELECT * FROM anime_episode_cache WHERE anime_video_sn = ? "
                "ORDER BY category_order, position",
                (video_sn,),
            ).fetchall()
            rel_rows = conn.execute(
                "SELECT * FROM anime_related_cache WHERE anime_video_sn = ? ORDER BY position",
                (video_sn,),
            ).fetchall()

        categories: list[EpisodeCategory] = []
        for r in ep_rows:
            if not categories or categories[-1].name != r["category"]:
                categories.append(EpisodeCategory(name=r["category"], episodes=[]))
            categories[-1].episodes.append(
                Episode(number=r["episode_number"], video_sn=r["episode_video_sn"])
            )

        detail = AnimeDetail(
            video_sn=row["video_sn"],
            title=row["title"],
            cover_url=row["cover_url"],
            air_date=row["air_date"],
            director=row["director"],
            distributor=row["distributor"],
            producer=row["producer"],
            genres=[r["genre"] for r in genre_rows],
            description=row["description"],
            rating_score=row["rating_score"],
            rating_count=row["rating_count"],
            episode_categories=categories,
            related_anime=[
                RelatedAnime(
                    ref_sn=r["ref_sn"],
                    title=r["title"],
                    cover_url=r["cover_url"],
                    year_text=r["year_text"],
                    episode_count_text=r["episode_count_text"],
                )
                for r in rel_rows
            ],
        )
        try:
            fetched_at = datetime.fromisoformat(row["fetched_at"])
        except (TypeError, ValueError):
            fetched_at = datetime.min
        return detail, fetched_at

    def save_detail(self, detail: AnimeDetail, now: datetime | None = None) -> None:
        now = now or datetime.now()
        sn = detail.video_sn
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute("DELETE FROM anime_genre_cache WHERE video_sn = ?", (sn,))
                conn.execute("DELETE FROM anime_episode_cache WHERE anime_video_sn = ?", (sn,))
                conn.execute("DELETE FROM anime_related_cache WHERE anime_video_sn = ?", (sn,))
                conn.execute(
                    "INSERT INTO anime_detail_cache "
                    "(video_sn, title, cover_url, air_date, director, distributor, producer, "
                    " description, rating_score, rating_count, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(video_sn) DO UPDATE SET "
                    " title=excluded.title, cover_url=excluded.cover_url, air_date=excluded.air_date, "
                    " director=excluded.director, distributor=excluded.distributor, "
                    " producer=excluded.producer, description=excluded.description, "
                    " rating_score=excluded.rating_score, rating_count=excluded.rating_count, "
                    " fetched_at=excluded.fetched_at",
                    (
                        sn, detail.title, detail.cover_url, detail.air_date, detail.director,
                        detail.distributor, detail.producer, detail.description,
                        detail.rating_score, detail.rating_count, now.isoformat(timespec="seconds"),
                    ),
                )
                conn.executemany(
                    "INSERT INTO anime_genre_cache (video_sn, position, genre) VALUES (?, ?, ?)",
                    [(sn, i, g) for i, g in enumerate(detail.genres)],
                )
                ep_rows = []
                for cat_order, cat in enumerate(detail.episode_categories):
                    for pos, ep in enumerate(cat.episodes):
                        ep_rows.append((sn, cat_order, cat.name, pos, ep.number, ep.video_sn))
                conn.executemany(
                    "INSERT INTO anime_episode_cache "
                    "(anime_video_sn, category_order, category, position, episode_number, episode_video_sn) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ep_rows,
                )
                conn.executemany(
                    "INSERT INTO anime_related_cache "
                    "(anime_video_sn, position, ref_sn, title, cover_url, year_text, episode_count_text) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (sn, i, r.ref_sn, r.title, r.cover_url, r.year_text, r.episode_count_text)
                        for i, r in enumerate(detail.related_anime)
                    ],
                )

    # ------------------------------------------------------------------
    # 集數 → 番劇 對應（使用者 2026-08-31：訂閱／改名按番劇不按單一集數）
    # ------------------------------------------------------------------

    def record_episode_group(
        self, video_sns, anime_title: str, now: datetime | None = None
    ) -> None:
        """`video_sns` 是同一部番劇已知的所有集數 video_sn（含進來的那一集）。全部指向
        同一個 group_key（＝集合最小值）。番劇頁快取存檔時呼叫。"""
        sns = sorted({int(s) for s in video_sns if s})
        if not sns:
            return
        group_key = sns[0]
        ts = (now or datetime.now()).isoformat(timespec="seconds")
        with self._lock:
            with self._database.transaction() as conn:
                conn.executemany(
                    "INSERT INTO episode_group (episode_video_sn, group_key, anime_title, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(episode_video_sn) DO UPDATE SET "
                    " group_key=excluded.group_key, anime_title=excluded.anime_title, "
                    " updated_at=excluded.updated_at",
                    [(sn, group_key, anime_title, ts) for sn in sns],
                )

    def group_key_for(self, video_sn: int) -> int | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT group_key FROM episode_group WHERE episode_video_sn = ?", (video_sn,)
            ).fetchone()
        return int(row["group_key"]) if row else None

    def group_members(self, group_key: int) -> set[int]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT episode_video_sn FROM episode_group WHERE group_key = ?", (group_key,)
            ).fetchall()
        return {int(r["episode_video_sn"]) for r in rows}

    def anime_title_for(self, video_sn: int) -> str | None:
        """這個 video_sn 屬於的番劇標題（快取得到就有，用來取代畫面上顯示 sn 號碼）。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT anime_title FROM episode_group WHERE episode_video_sn = ?", (video_sn,)
            ).fetchone()
        return row["anime_title"] if row and row["anime_title"] else None

    def resolve_subscription_expansion(self, subscribed_sns) -> tuple[set[int], dict[int, int]]:
        """把「使用者訂閱的那幾個 video_sn」展開成「這些番劇的全部集數 video_sn」，
        並回傳 {展開後的 sn: 原本訂閱的 sn}（給 rename 對應用）。找不到對應的 sn 原樣保留。"""
        expanded: set[int] = set(subscribed_sns)
        origin: dict[int, int] = {sn: sn for sn in subscribed_sns}
        for sn in subscribed_sns:
            gk = self.group_key_for(sn)
            if gk is None:
                continue
            for member in self.group_members(gk):
                expanded.add(member)
                origin.setdefault(member, sn)
        return expanded, origin

    # ------------------------------------------------------------------
    # 圖片（bytes 存磁碟，這張表只記帳）
    # ------------------------------------------------------------------

    def register_images(self, urls, now: datetime | None = None) -> None:
        """把一批圖片 URL 登記進帳（`fetched_at` 還是 NULL＝待抓）。頁面 render 完呼叫
        一次（一個交易），不要每張圖各開一個交易。已登記過的不動。"""
        now = now or datetime.now()
        ts = now.isoformat(timespec="seconds")
        rows = [
            (url_hash(u), u, ts, ts)
            for u in urls
            if u and str(u).startswith(("http://", "https://"))
        ]
        if not rows:
            return
        with self._lock:
            with self._database.transaction() as conn:
                # 新的 → 插入；已存在的 → 只把 last_seen_at 往前推（表示「這張圖現在還
                # 有頁面在用」），其他欄位不動。
                conn.executemany(
                    "INSERT INTO image_cache (url_hash, url, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(url_hash) DO UPDATE SET last_seen_at=excluded.last_seen_at",
                    rows,
                )

    def record_image(
        self, url: str, content_type: str | None, byte_size: int, now: datetime | None = None
    ) -> None:
        """圖片 bytes 已寫進磁碟後補上 `fetched_at`／大小／型別。"""
        now = now or datetime.now()
        with self._lock:
            with self._database.transaction() as conn:
                iso = now.isoformat(timespec="seconds")
                conn.execute(
                    "INSERT INTO image_cache "
                    "(url_hash, url, content_type, byte_size, fetched_at, first_seen_at, last_seen_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(url_hash) DO UPDATE SET content_type=excluded.content_type, "
                    " byte_size=excluded.byte_size, fetched_at=excluded.fetched_at, "
                    " last_seen_at=excluded.last_seen_at",
                    (url_hash(url), url, content_type, byte_size, iso, iso, iso),
                )

    def image_record(self, url_hash_value: str) -> dict | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM image_cache WHERE url_hash = ?", (url_hash_value,)
            ).fetchone()
        return dict(row) if row is not None else None

    def has_image_bytes(self, url: str) -> bool:
        rec = self.image_record(url_hash(url))
        return rec is not None and rec["fetched_at"] is not None

    def pending_image_urls(self) -> list[str]:
        """登記過、bytes 還沒抓的圖片 URL——`ImageCacheFetcher` 續抓用。"""
        with self._database.transaction() as conn:
            rows = conn.execute("SELECT url FROM image_cache WHERE fetched_at IS NULL").fetchall()
        return [r["url"] for r in rows]

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def prune(self, ttl_days: int, now: datetime | None = None) -> dict:
        """清掉超過 TTL 的 detail 快取與 `animeRef.php` 解析快取，以及「超過 TTL」或
        「沒有內容表引用（孤兒）」的圖片帳。回傳
        `{"stale_detail": n, "stale_ref": n, "removed_image_hashes": [...]}`——磁碟圖片檔
        的實際刪除交給呼叫端（`ImageCacheFetcher.remove_hashes()`）。
        （首頁／首頁區塊快取不看 TTL——它們靠 `home_refresh_decision()`／自己的短 TTL
        自然汰換。）"""
        now = now or datetime.now()
        cutoff = now.timestamp() - ttl_days * 86400
        with self._lock:
            with self._database.transaction() as conn:
                stale = [
                    row["video_sn"]
                    for row in conn.execute(
                        "SELECT video_sn, fetched_at FROM anime_detail_cache"
                    ).fetchall()
                    if _iso_ts(row["fetched_at"]) < cutoff
                ]
                for sn in stale:
                    conn.execute("DELETE FROM anime_detail_cache WHERE video_sn = ?", (sn,))
                    conn.execute("DELETE FROM anime_genre_cache WHERE video_sn = ?", (sn,))
                    conn.execute("DELETE FROM anime_episode_cache WHERE anime_video_sn = ?", (sn,))
                    conn.execute("DELETE FROM anime_related_cache WHERE anime_video_sn = ?", (sn,))

                stale_ref = [
                    row["ref_sn"]
                    for row in conn.execute(
                        "SELECT ref_sn, resolved_at FROM ref_resolution"
                    ).fetchall()
                    if _iso_ts(row["resolved_at"]) < cutoff
                ]
                for ref_sn in stale_ref:
                    conn.execute("DELETE FROM ref_resolution WHERE ref_sn = ?", (ref_sn,))

                referenced = self._referenced_image_urls(conn)
                orphan_cutoff = now.timestamp() - _IMAGE_ORPHAN_GRACE_DAYS * 86400
                removed_hashes = []
                for row in conn.execute(
                    "SELECT url_hash, url, fetched_at, first_seen_at, last_seen_at FROM image_cache"
                ).fetchall():
                    last_touch = _iso_ts(
                        row["last_seen_at"] or row["fetched_at"] or row["first_seen_at"]
                    )
                    if last_touch < cutoff:
                        removed_hashes.append(row["url_hash"])  # 過了 TTL（預設 90 天）
                    elif row["url"] not in referenced and last_touch < orphan_cutoff:
                        # 沒有內容表引用、而且已經超過寬限期沒被任何頁面用到 → 才刪。
                        # 剛在下載列表看到、或封面 URL 剛換過的新鮮圖片不會被 6 小時一輪的
                        # prune 誤砍（使用者 2026-09-08）。
                        removed_hashes.append(row["url_hash"])
                for h in removed_hashes:
                    conn.execute("DELETE FROM image_cache WHERE url_hash = ?", (h,))
        return {
            "stale_detail": len(stale),
            "stale_ref": len(stale_ref),
            "removed_image_hashes": removed_hashes,
        }

    @staticmethod
    def _referenced_image_urls(conn) -> set[str]:
        urls: set[str] = set()
        for sql in (
            "SELECT cover_url AS u FROM home_card",
            "SELECT cover_url AS u FROM homepage_section_card",
            "SELECT cover_url AS u FROM anime_detail_cache",
            "SELECT cover_url AS u FROM anime_related_cache",
            # 下載列表的封面（`downloaded_episodes.cover_url`）——同一個 bahaad.db，
            # 直接查。少了這個 → 下載列表封面每 6 小時被 prune 當孤兒砍掉（使用者 2026-09-08）。
            "SELECT cover_url AS u FROM downloaded_episodes",
        ):
            try:
                urls.update(row["u"] for row in conn.execute(sql).fetchall() if row["u"])
            except Exception:  # noqa: BLE001 - 表不存在（極舊 DB／測試）就跳過這張表
                pass
        return urls

    def clear(self) -> None:
        """設定頁「清空快取」用——清光所有表（磁碟圖片檔由呼叫端另外刪）。"""
        with self._lock:
            with self._database.transaction() as conn:
                for table in (
                    "home_card", "home_schedule_entry", "home_cache_meta",
                    "homepage_section_card", "homepage_section_meta", "ref_resolution",
                    "anime_detail_cache", "anime_genre_cache", "anime_episode_cache",
                    "anime_related_cache", "image_cache",
                ):
                    conn.execute(f"DELETE FROM {table}")

    def all_image_hashes(self) -> Iterable[str]:
        with self._database.transaction() as conn:
            return [row["url_hash"] for row in conn.execute("SELECT url_hash FROM image_cache").fetchall()]


def _iso_ts(value) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0
