"""youranimes.tw 季度頁解析結果的本地快取。規格見 docs/requirements/youranimes.md。

跟 `AnimeCacheStore` 分開（不同來源、不同刷新節奏＝按季度頁不是按 video_sn、不同
prune 政策）。慣例完全比照 `store/anime_cache.py`：模組級 `_TABLES_SQL`、`threading.Lock`、
`self._database.transaction()`、`_ensure_added_columns` 冪等 ALTER、單一交易內
delete-then-insert。

存法（正規化、一格一值、無 JSON blob）：
- `youranimes_anime`：一部番劇一列，PK = youranimes `/animes/<id>` 的 id。
- `youranimes_staff` / `youranimes_cast` / `youranimes_music`：子表，`position` 保留順序。
- `youranimes_season_meta`：一個季度 slug 一列，記上次抓取時間 + 內容雜湊
  （youranimes 不支援 conditional GET，用雜湊判斷「內容有沒有變」以省掉 DB 寫入）。
"""

from __future__ import annotations

import threading
from datetime import datetime

from bahaad.scheduler.gossip_watch import season_number
from bahaad.store.database import Database
from bahaad.youranimes.match import title_base_key
from bahaad.youranimes.models import (
    YourAnimesCastMember,
    YourAnimesMusic,
    YourAnimesRecord,
    YourAnimesStaff,
)

__all__ = ["YourAnimesCacheStore", "title_base_key"]

_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS youranimes_anime (
    anime_id INTEGER PRIMARY KEY,
    season_slug TEXT NOT NULL,
    zh_title TEXT NOT NULL,
    jp_title TEXT NOT NULL DEFAULT '',
    zh_title_base TEXT NOT NULL,
    season_number INTEGER,
    synopsis TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_youranimes_base ON youranimes_anime(zh_title_base);
CREATE INDEX IF NOT EXISTS idx_youranimes_slug ON youranimes_anime(season_slug);
CREATE TABLE IF NOT EXISTS youranimes_staff (
    anime_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    name_position INTEGER NOT NULL,
    job TEXT NOT NULL,
    name TEXT NOT NULL,
    PRIMARY KEY (anime_id, position, name_position)
);
CREATE TABLE IF NOT EXISTS youranimes_cast (
    anime_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    character TEXT NOT NULL,
    actor TEXT NOT NULL,
    PRIMARY KEY (anime_id, position)
);
CREATE TABLE IF NOT EXISTS youranimes_music (
    anime_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    slot TEXT NOT NULL,
    song TEXT NOT NULL,
    artist TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (anime_id, position)
);
CREATE TABLE IF NOT EXISTS youranimes_season_meta (
    season_slug TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT ''
);
"""

# 後加欄位（對舊資料庫冪等 ALTER，比照 anime_cache._ADDED_COLUMNS）
_ADDED_COLUMNS: dict[str, dict[str, str]] = {}


# `normalize_title_key` / `title_base_key` 搬去 `youranimes/match.py`（比對邏輯本來就該
# 跟那支模組放一起，這裡改成從那邊 import／re-export——避免 store 反過來被 match.py
# 依賴造成循環 import）。

class YourAnimesCacheStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.executescript(_TABLES_SQL)
            self._ensure_added_columns(conn)

    @staticmethod
    def _ensure_added_columns(conn) -> None:
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ------------------------------------------------------------------
    # 季度 meta
    # ------------------------------------------------------------------

    def season_meta(self, slug: str) -> dict | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT fetched_at, content_hash FROM youranimes_season_meta WHERE season_slug = ?",
                (slug,),
            ).fetchone()
        if row is None:
            return None
        fetched_at = None
        try:
            fetched_at = datetime.fromisoformat(row["fetched_at"])
        except (TypeError, ValueError):
            pass
        return {"fetched_at": fetched_at, "content_hash": row["content_hash"] or ""}

    def touch_season(self, slug: str, content_hash: str, now: datetime | None = None) -> None:
        """內容雜湊沒變時只更新 `fetched_at`（省掉整批重寫子表）。"""
        now = now or datetime.now()
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO youranimes_season_meta (season_slug, fetched_at, content_hash) "
                    "VALUES (?, ?, ?) ON CONFLICT(season_slug) DO UPDATE SET "
                    "fetched_at = excluded.fetched_at, content_hash = excluded.content_hash",
                    (slug, now.isoformat(timespec="seconds"), content_hash),
                )

    # ------------------------------------------------------------------
    # 整季替換
    # ------------------------------------------------------------------

    def replace_season(
        self,
        slug: str,
        records: list[YourAnimesRecord],
        content_hash: str,
        now: datetime | None = None,
    ) -> None:
        """整季換掉——同一交易刪掉這個 slug 舊資料（含子表）再全插入。番劇離開該季頁時
        自然被清掉。同一 `anime_id` 出現在別的 slug 頁時以最新寫入的為準。"""
        now = now or datetime.now()
        ts = now.isoformat(timespec="seconds")
        with self._lock:
            with self._database.transaction() as conn:
                old_ids = [
                    r["anime_id"]
                    for r in conn.execute(
                        "SELECT anime_id FROM youranimes_anime WHERE season_slug = ?", (slug,)
                    )
                ]
                new_ids = {r.anime_id for r in records}
                for aid in old_ids:
                    if aid not in new_ids:
                        self._delete_anime(conn, aid)
                for rec in records:
                    self._delete_anime(conn, rec.anime_id)
                    conn.execute(
                        "INSERT INTO youranimes_anime "
                        "(anime_id, season_slug, zh_title, jp_title, zh_title_base, "
                        " season_number, synopsis, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            rec.anime_id, slug, rec.zh_title, rec.jp_title,
                            title_base_key(rec.zh_title), season_number(rec.zh_title),
                            rec.synopsis, ts,
                        ),
                    )
                    conn.executemany(
                        "INSERT INTO youranimes_staff "
                        "(anime_id, position, name_position, job, name) VALUES (?, ?, ?, ?, ?)",
                        [
                            (rec.anime_id, i, j, s.job, name)
                            for i, s in enumerate(rec.staff)
                            for j, name in enumerate(s.names)
                        ],
                    )
                    conn.executemany(
                        "INSERT INTO youranimes_cast "
                        "(anime_id, position, character, actor) VALUES (?, ?, ?, ?)",
                        [(rec.anime_id, i, c.character, c.actor) for i, c in enumerate(rec.cast)],
                    )
                    conn.executemany(
                        "INSERT INTO youranimes_music "
                        "(anime_id, position, slot, song, artist) VALUES (?, ?, ?, ?, ?)",
                        [
                            (rec.anime_id, i, m.slot, m.song, m.artist)
                            for i, m in enumerate(rec.music)
                        ],
                    )
                conn.execute(
                    "INSERT INTO youranimes_season_meta (season_slug, fetched_at, content_hash) "
                    "VALUES (?, ?, ?) ON CONFLICT(season_slug) DO UPDATE SET "
                    "fetched_at = excluded.fetched_at, content_hash = excluded.content_hash",
                    (slug, ts, content_hash),
                )

    def upsert_anime(self, record: YourAnimesRecord, now: datetime | None = None) -> None:
        """插入或整筆覆蓋單一番劇（含子表），**不動同一季度 slug 底下其他番劇**——跟
        `replace_season` 不一樣，這支不會因為「這個 slug 只有這幾筆」就把其他既有的
        刪掉。給季度頁內容沒變（`replace_season` 被 content_hash 節流跳過）但個別頁
        補洞仍要新增/更新一筆時用（見 `scheduler/youranimes_sync.py`）。"""
        now = now or datetime.now()
        ts = now.isoformat(timespec="seconds")
        with self._lock:
            with self._database.transaction() as conn:
                self._delete_anime(conn, record.anime_id)
                conn.execute(
                    "INSERT INTO youranimes_anime "
                    "(anime_id, season_slug, zh_title, jp_title, zh_title_base, "
                    " season_number, synopsis, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.anime_id, record.season_slug, record.zh_title, record.jp_title,
                        title_base_key(record.zh_title), season_number(record.zh_title),
                        record.synopsis, ts,
                    ),
                )
                conn.executemany(
                    "INSERT INTO youranimes_staff "
                    "(anime_id, position, name_position, job, name) VALUES (?, ?, ?, ?, ?)",
                    [
                        (record.anime_id, i, j, s.job, name)
                        for i, s in enumerate(record.staff)
                        for j, name in enumerate(s.names)
                    ],
                )
                conn.executemany(
                    "INSERT INTO youranimes_cast "
                    "(anime_id, position, character, actor) VALUES (?, ?, ?, ?)",
                    [(record.anime_id, i, c.character, c.actor) for i, c in enumerate(record.cast)],
                )
                conn.executemany(
                    "INSERT INTO youranimes_music "
                    "(anime_id, position, slot, song, artist) VALUES (?, ?, ?, ?, ?)",
                    [
                        (record.anime_id, i, m.slot, m.song, m.artist)
                        for i, m in enumerate(record.music)
                    ],
                )

    @staticmethod
    def _delete_anime(conn, anime_id: int) -> None:
        conn.execute("DELETE FROM youranimes_anime WHERE anime_id = ?", (anime_id,))
        conn.execute("DELETE FROM youranimes_staff WHERE anime_id = ?", (anime_id,))
        conn.execute("DELETE FROM youranimes_cast WHERE anime_id = ?", (anime_id,))
        conn.execute("DELETE FROM youranimes_music WHERE anime_id = ?", (anime_id,))

    # ------------------------------------------------------------------
    # 查詢
    # ------------------------------------------------------------------

    def find_by_base(self, base_casefolded: str) -> list[YourAnimesRecord]:
        if not base_casefolded:
            return []
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM youranimes_anime WHERE zh_title_base = ? ORDER BY season_slug DESC",
                (base_casefolded,),
            ).fetchall()
            return [self._hydrate(conn, r) for r in rows]

    def find_by_base_prefix(self, base_casefolded: str) -> list[YourAnimesRecord]:
        """`zh_title_base` 以這個字串開頭的候選（前綴比對）。給「其中一邊標題被截短／多了
        副標題」的比對容錯用（見 youranimes/match.py，使用者 2026-09-05：例如動畫瘋
        「我是不才惡女」vs youranimes「我是不才惡女～雛宮蝶鼠互換傳～」）。至少 2 個字才
        查——太短的前綴容易配到不相干的番劇。"""
        if not base_casefolded or len(base_casefolded) < 2:
            return []
        escaped = (
            base_casefolded.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM youranimes_anime WHERE zh_title_base LIKE ? ESCAPE '\\' "
                "ORDER BY season_slug DESC",
                (escaped + "%",),
            ).fetchall()
            return [self._hydrate(conn, r) for r in rows]

    def get(self, anime_id: int) -> YourAnimesRecord | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM youranimes_anime WHERE anime_id = ?", (anime_id,)
            ).fetchone()
            return self._hydrate(conn, row) if row is not None else None

    @staticmethod
    def _hydrate(conn, row) -> YourAnimesRecord:
        aid = row["anime_id"]
        staff_rows = conn.execute(
            "SELECT position, name_position, job, name FROM youranimes_staff "
            "WHERE anime_id = ? ORDER BY position, name_position",
            (aid,),
        ).fetchall()
        by_pos: dict[int, dict] = {}
        for r in staff_rows:
            entry = by_pos.setdefault(r["position"], {"job": r["job"], "names": []})
            entry["names"].append(r["name"])
        staff = tuple(
            YourAnimesStaff(job=by_pos[p]["job"], names=tuple(by_pos[p]["names"]))
            for p in sorted(by_pos)
        )
        cast = tuple(
            YourAnimesCastMember(character=r["character"], actor=r["actor"])
            for r in conn.execute(
                "SELECT character, actor FROM youranimes_cast WHERE anime_id = ? ORDER BY position",
                (aid,),
            )
        )
        music = tuple(
            YourAnimesMusic(slot=r["slot"], song=r["song"], artist=r["artist"])
            for r in conn.execute(
                "SELECT slot, song, artist FROM youranimes_music WHERE anime_id = ? ORDER BY position",
                (aid,),
            )
        )
        return YourAnimesRecord(
            anime_id=aid,
            zh_title=row["zh_title"],
            jp_title=row["jp_title"],
            season_number=row["season_number"],
            synopsis=row["synopsis"],
            staff=staff,
            cast=cast,
            music=music,
            season_slug=row["season_slug"],
        )

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def prune(self, retention_days: int, now: datetime | None = None) -> int:
        """刪掉 `fetched_at` 超過保留天數的番劇（含子表），以及同樣過期的季度 meta。
        回傳刪掉幾部番劇。

        注意不能用「沒有番劇 row 指到的 meta 就刪」——橫跨兩季的連續放送番劇同一
        `anime_id` 會在多個季度頁出現，但 `anime_id` 是 PK、只留一列（最後寫入的
        slug 勝），其他 slug 就會「沒有 row 指到」但其實還是有效的季度頁。
        """
        now = now or datetime.now()
        cutoff = now.timestamp() - retention_days * 86400
        removed = 0
        with self._lock:
            with self._database.transaction() as conn:
                rows = conn.execute("SELECT anime_id, fetched_at FROM youranimes_anime").fetchall()
                for r in rows:
                    if _iso_ts(r["fetched_at"]) < cutoff:
                        self._delete_anime(conn, r["anime_id"])
                        removed += 1
                for r in conn.execute(
                    "SELECT season_slug, fetched_at FROM youranimes_season_meta"
                ).fetchall():
                    if _iso_ts(r["fetched_at"]) < cutoff:
                        conn.execute(
                            "DELETE FROM youranimes_season_meta WHERE season_slug = ?",
                            (r["season_slug"],),
                        )
        return removed

    def clear(self) -> None:
        with self._lock:
            with self._database.transaction() as conn:
                for table in (
                    "youranimes_anime", "youranimes_staff", "youranimes_cast",
                    "youranimes_music", "youranimes_season_meta",
                ):
                    conn.execute(f"DELETE FROM {table}")


def _iso_ts(value) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0
