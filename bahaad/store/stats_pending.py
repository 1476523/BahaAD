"""即時匿名使用統計——「還沒送出去」的緩衝。規格見
docs/requirements/realtime_stats.md「離線緩衝與重試」。

`access_gate_server` 可能因更新停機，回報不能丟——送不出去就累加在這裡，`StatsReporter`
按重試節奏（3分x10 → 1時x10 → 3時無限）之後補送，任何一次成功就 `clear()`。

**兩種資料**：

- **計數（次數）**：`downloads`／`views`／`completions`／`diagnostics_reports`——累加增量，
  暫存期間下載 5 次就 `+5` 一次補回。
- **狀態（人數）**：每部番劇對「這個客戶端」的 `favorite`／`subscription` 是不是 on
  ——存**目標狀態**（0/1/NULL＝沒變），不是增量。伺服器據 activation_id 去重，
  `favorite` 計數＝有多少 activation_id 目前是 on。重送同一個狀態是冪等的。
  收藏轉訂閱時 `favorite` 留 1、`subscription` 設 1（人數不歸零，見規格）。

心跳式資料（在線／正在收看）不進這裡——過期就沒意義，`StatsHeartbeat` 直接送當下狀態。

存進 `bahaad.db`（`/reset-everything` 清掉整個 bahaad.db＝連同還沒送的緩衝一起沒了，
可接受；activation.db 才是要活得比 DB 久的）。
"""

from __future__ import annotations

import threading

from bahaad.store.database import Database

# 重試節奏：phase → (間隔秒數, 最多幾次；None＝不限)
RETRY_PHASES = (
    (3 * 60, 10),
    (60 * 60, 10),
    (3 * 60 * 60, None),
)

_COUNTERS_SQL = """
CREATE TABLE IF NOT EXISTS stats_pending_counters (
    key TEXT PRIMARY KEY,
    delta INTEGER NOT NULL DEFAULT 0
)
"""
_ANIME_SQL = """
CREATE TABLE IF NOT EXISTS stats_pending_anime (
    anime_sn INTEGER PRIMARY KEY,
    title TEXT,
    favorite INTEGER,
    subscription INTEGER,
    views_delta INTEGER NOT NULL DEFAULT 0,
    completions_delta INTEGER NOT NULL DEFAULT 0
)
"""
_META_SQL = """
CREATE TABLE IF NOT EXISTS stats_pending_meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    phase INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0
)
"""


class StatsPendingStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_COUNTERS_SQL)
            conn.execute(_ANIME_SQL)
            conn.execute(_META_SQL)
            conn.execute(
                "INSERT INTO stats_pending_meta (id) VALUES (1) ON CONFLICT(id) DO NOTHING"
            )

    # ---- 累加（各整合點呼叫，經 StatsCollector）-----------------------

    def add_counter(self, key: str, delta: int = 1) -> None:
        if delta == 0:
            return
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO stats_pending_counters (key, delta) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET delta = delta + excluded.delta",
                    (key, delta),
                )

    def set_anime_state(
        self,
        anime_sn: int,
        *,
        title: str | None = None,
        favorite: int | None = None,
        subscription: int | None = None,
    ) -> None:
        """設定這部番劇對本客戶端的 favorite／subscription 目標狀態（0/1）。
        `None` 的欄位維持原值。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO stats_pending_anime (anime_sn, title, favorite, subscription) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(anime_sn) DO UPDATE SET "
                    "title = COALESCE(excluded.title, stats_pending_anime.title), "
                    "favorite = COALESCE(excluded.favorite, stats_pending_anime.favorite), "
                    "subscription = COALESCE(excluded.subscription, stats_pending_anime.subscription)",
                    (anime_sn, title, favorite, subscription),
                )

    def add_anime_count(
        self,
        anime_sn: int,
        *,
        title: str | None = None,
        views: int = 0,
        completions: int = 0,
    ) -> None:
        if not views and not completions and title is None:
            return
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "INSERT INTO stats_pending_anime "
                    "(anime_sn, title, views_delta, completions_delta) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(anime_sn) DO UPDATE SET "
                    "title = COALESCE(excluded.title, stats_pending_anime.title), "
                    "views_delta = views_delta + excluded.views_delta, "
                    "completions_delta = completions_delta + excluded.completions_delta",
                    (anime_sn, title, views, completions),
                )

    # ---- 讀出（StatsReporter 送出前）--------------------------------

    def snapshot(self) -> dict:
        """`{counters: {key: delta}, anime: [{sn, title, favorite?, subscription?, views, completions}]}`。
        沒有任何變化的番劇列不出現（favorite/subscription 都 NULL 且 views/completions 都 0
        且沒 title）。"""
        with self._database.transaction() as conn:
            counters = {
                row["key"]: row["delta"]
                for row in conn.execute("SELECT key, delta FROM stats_pending_counters")
                if row["delta"] != 0
            }
            anime = []
            for row in conn.execute(
                "SELECT anime_sn, title, favorite, subscription, views_delta, completions_delta "
                "FROM stats_pending_anime"
            ):
                has_state = row["favorite"] is not None or row["subscription"] is not None
                has_count = row["views_delta"] or row["completions_delta"]
                if not (has_state or has_count or row["title"]):
                    continue
                entry = {
                    "sn": row["anime_sn"],
                    "title": row["title"],
                    "views": row["views_delta"],
                    "completions": row["completions_delta"],
                }
                if row["favorite"] is not None:
                    entry["favorite"] = row["favorite"]
                if row["subscription"] is not None:
                    entry["subscription"] = row["subscription"]
                anime.append(entry)
        return {"counters": counters, "anime": anime}

    def is_empty(self) -> bool:
        snap = self.snapshot()
        return not snap["counters"] and not snap["anime"]

    def commit_sent(self, sent: dict) -> None:
        """送出成功後呼叫——**只扣掉這次真的送出去的量**，暫存期間（送出的網路往返裡）
        並發新加的事件保留下來、下輪再送。重試狀態歸零。

        - 計數（downloads/views/completions）：`delta -= sent_value`（並發 +1 會被保留）。
        - 狀態（favorite/subscription）：這次送的值還沒被改動才清成 NULL；期間被改過就
          留著、下輪送新值（伺服器 upsert 冪等）。
        """
        counters = sent.get("counters") or {}
        anime = sent.get("anime") or []
        with self._lock:
            with self._database.transaction() as conn:
                for key, value in counters.items():
                    try:
                        v = int(value)
                    except (TypeError, ValueError):
                        continue
                    conn.execute(
                        "UPDATE stats_pending_counters SET delta = delta - ? WHERE key = ?",
                        (v, str(key)),
                    )
                conn.execute("DELETE FROM stats_pending_counters WHERE delta = 0")
                for entry in anime:
                    sn = entry.get("sn")
                    if sn is None:
                        continue
                    conn.execute(
                        "UPDATE stats_pending_anime SET "
                        "views_delta = views_delta - ?, completions_delta = completions_delta - ? "
                        "WHERE anime_sn = ?",
                        (int(entry.get("views") or 0), int(entry.get("completions") or 0), sn),
                    )
                    if "favorite" in entry:
                        conn.execute(
                            "UPDATE stats_pending_anime SET favorite = NULL "
                            "WHERE anime_sn = ? AND favorite = ?",
                            (sn, entry["favorite"]),
                        )
                    if "subscription" in entry:
                        conn.execute(
                            "UPDATE stats_pending_anime SET subscription = NULL "
                            "WHERE anime_sn = ? AND subscription = ?",
                            (sn, entry["subscription"]),
                        )
                conn.execute(
                    "DELETE FROM stats_pending_anime WHERE favorite IS NULL AND subscription IS NULL "
                    "AND views_delta = 0 AND completions_delta = 0"
                )
                conn.execute(
                    "UPDATE stats_pending_meta SET attempt_count = 0, phase = 0, next_attempt_at = 0 "
                    "WHERE id = 1"
                )

    def clear(self) -> None:
        """整個清空——測試／重置用。正常送出成功走 `commit_sent()`。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute("DELETE FROM stats_pending_counters")
                conn.execute("DELETE FROM stats_pending_anime")
                conn.execute(
                    "UPDATE stats_pending_meta SET attempt_count = 0, phase = 0, next_attempt_at = 0 "
                    "WHERE id = 1"
                )

    # ---- 重試狀態 ---------------------------------------------------

    def get_retry_state(self) -> tuple[int, int, float]:
        """回 (attempt_count, phase, next_attempt_at)。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT attempt_count, phase, next_attempt_at FROM stats_pending_meta WHERE id = 1"
            ).fetchone()
        if row is None:
            return 0, 0, 0.0
        return row["attempt_count"], row["phase"], row["next_attempt_at"]

    def note_failure(self, now: float) -> None:
        """送出失敗——照重試節奏推進 phase / attempt_count，算好下次可以再試的時間。"""
        attempt, phase, _ = self.get_retry_state()
        attempt += 1
        _, max_attempts = RETRY_PHASES[phase]
        if max_attempts is not None and attempt >= max_attempts and phase < len(RETRY_PHASES) - 1:
            phase += 1
            attempt = 0
        interval = RETRY_PHASES[phase][0]
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE stats_pending_meta SET attempt_count = ?, phase = ?, next_attempt_at = ? "
                    "WHERE id = 1",
                    (attempt, phase, now + interval),
                )
