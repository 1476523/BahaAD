"""監視公告（gossip）的持久化。規格見 docs/requirements/scheduler_gossip_watch.md
「資料模型（`store/gossip.py`）」一節。

三張表，語意直接對應舊專案 `GossipDB.py` 已經驗證過（v25.2.0/v25.2.3/v25.2.6 修過
好幾輪 bug）的三表設計，欄位命名／型別依 BahaAD 慣例調整、欄位數量比舊專案精簡
（舊 `gossip_events` 27 欄 → 這裡 17 欄，砍掉的是 v1 用不到的細節欄位）：

- `gossip_log`：每則「不重複」公告原文的永久紀錄。`source_hash UNIQUE` 天然達成
  「同一則公告只處理一次」的去重——`record_announcement()` 遇到 `IntegrityError`
  就是「已經處理過」的訊號，不用另外查一次。
- `gossip_events`：每則公告拆解出的子事件（一則公告可能同時提到好幾部作品），
  「歷史公告／操作處置」畫面每一列對應一筆。使用者在操作處置頁確認的處置結果也
  存在這張表的 `disposition`／`disposition_locked`。
- `gossip_pending`：目前生效中的臨時排程覆蓋，`main_loop.py`／`custom_schedule.py`
  檢查一個 sn 該不該跳過／該在什麼時間檢查時會多問這張表。`event_id` 回指建立它
  的那筆 `gossip_events`。

不含舊專案的「舊版三份 JSON 檔案匯入」——BahaAD 是全新專案，沒有遺留檔案要遷移。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any

from bahaad.store.database import Database

# --- 代碼表（欄位的合法值，`scheduler/gossip_watch.py` 之後從這裡 import，避免兩邊各寫一份） ---

# reschedule（.0 改進.txt 第 25 項例外）：每週更新時間永久變更公告（「自 X 起固定於
# 每週 N HH:MM 上架」）——這種不是本週一次性異動，會連帶調整週期表時間，站方之後把
# 公告撤下也不該還原，所以 _expire_vanished_events 對它特別放行。
ACTIONS = ("pause", "delay", "extra_episode", "takedown", "manual_review", "log_only", "reschedule")
# no_subscription（.0 改進.txt 第 7 項）：公告作品在週期表上比對得到，但使用者沒有訂閱
# ——記錄＋通知＋自動忽略，公告還掛在站上期間持續看它有沒有被訂閱，一被訂閱就補做處置。
MATCH_STATUSES = ("no_target", "unmatched", "no_subscription", "matched")
STATUS_LABELS = (
    "暫停更新", "提前更新", "延後更新", "同時更新", "暫時下架", "無法判斷", "未訂閱", "更新時間變更",
)
PENDING_TYPES = ("skip_check", "override_check_time", "override_check_window")
PENDING_STATUSES = ("pending", "done", "expired")

# 操作處置頁下拉選單的選項（沿用舊專案定案 + 2026-09-01 三個「每週更新時間異動」專用）
DISPOSITION_OPTIONS = (
    "等待處置",
    "本週暫停更新",
    "本週提前更新",
    "本週延後更新",
    "本週同時更新",
    "本週下架",
    "自訂更新時間",
    "自訂連續更新時間",
    "自訂時間範圍",
    "忽略此公告",
    # 每週更新時間異動（action='reschedule'）專用——永久改 schedule_entries 的檢查時段，
    # 不是「本週」臨時覆蓋
    "調整更新時段",
    "維持原本時段",
    "自訂更新時段",
)
# 只出現在 action='reschedule' 事件的處置下拉；其餘事件的下拉排除這三個
RESCHEDULE_DISPOSITIONS = ("調整更新時段", "維持原本時段", "自訂更新時段")

# 狀態判斷結果 → 系統預設建議處置（操作處置頁下拉選單的預設值，使用者可蓋過）
STATUS_TO_SUGGESTION = {
    "暫停更新": "本週暫停更新",
    "提前更新": "本週提前更新",
    "延後更新": "本週延後更新",
    "同時更新": "本週同時更新",
    "暫時下架": "本週下架",
    "無法判斷": "等待處置",
    # no_subscription（.0 改進.txt 第 7 項）：週期表有、但沒訂閱 → 預設就是「忽略」，
    # 不進操作處置頁；之後被訂閱時 promote_no_subscription() 會換成真正的狀態＋建議
    "未訂閱": "忽略此公告",
    # reschedule（每週更新時間異動）：預設建議「依公告調整更新時段」。機動調整開著時
    # 自動套用（永久改 schedule_entries 的檢查時段），一樣列進操作處置頁讓使用者改回
    # 「維持原本時段」或「自訂更新時段」（2026-09-01 使用者要求）
    "更新時間變更": "調整更新時段",
}

_IGNORE_DISPOSITION = "忽略此公告"

_TIMESTAMP_FMT = "%Y-%m-%d %H:%M:%S"

# update_pending() 允許被更新的欄位白名單——不接受任意欄位名，避免呼叫端傳進來的
# key 直接被拼進 SQL
_PENDING_UPDATABLE = frozenset(
    {
        "status",
        "found_episode_count",
        "seen_episodes",
        "next_retry_at",
        "retry_deadline",
        "retry_interval_minutes",
        "check_time",
        "check_time_start",
        "check_time_end",
        "expect_episode_count",
    }
)

_TABLE_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS gossip_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        captured_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        raw_text TEXT NOT NULL,
        source_hash TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS gossip_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_hash TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        raw_clause TEXT NOT NULL,
        action TEXT NOT NULL,
        match_status TEXT NOT NULL,
        title_in_gossip TEXT,
        sn INTEGER,
        status_label TEXT NOT NULL,
        target_date TEXT,
        target_time TEXT,
        target_time_start TEXT,
        target_time_end TEXT,
        expect_episode_count INTEGER NOT NULL DEFAULT 1,
        system_suggestion TEXT NOT NULL,
        disposition TEXT NOT NULL,
        disposition_locked INTEGER NOT NULL DEFAULT 0,
        disposition_updated_at TEXT,
        last_seen_at TEXT,
        acknowledged_at TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gossip_events_source_hash ON gossip_events (source_hash)",
    """
    CREATE TABLE IF NOT EXISTS gossip_pending (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER,
        sn INTEGER NOT NULL,
        type TEXT NOT NULL,
        check_date TEXT NOT NULL,
        check_time TEXT,
        check_time_start TEXT,
        check_time_end TEXT,
        expect_episode_count INTEGER NOT NULL DEFAULT 1,
        found_episode_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'pending',
        retry_deadline TEXT,
        retry_interval_minutes INTEGER,
        next_retry_at TEXT,
        seen_episodes TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_gossip_pending_lookup ON gossip_pending (sn, check_date, status)",
]

_EVENT_COLUMNS = (
    "id",
    "source_hash",
    "created_at",
    "raw_clause",
    "action",
    "match_status",
    "title_in_gossip",
    "sn",
    "status_label",
    "target_date",
    "target_time",
    "target_time_start",
    "target_time_end",
    "expect_episode_count",
    "system_suggestion",
    "disposition",
    "disposition_locked",
    "disposition_updated_at",
    "last_seen_at",
    "acknowledged_at",
)

# 「表已存在、但欄位是舊版」的資料庫補欄位（store/ 沒有正式 migration 框架，比照
# store/identity.py 的做法：PRAGMA table_info + ALTER TABLE ADD COLUMN，idempotent）
_EVENT_ADDED_COLUMNS = {
    "last_seen_at": "TEXT",
    "acknowledged_at": "TEXT",
}

_PENDING_COLUMNS = (
    "id",
    "event_id",
    "sn",
    "type",
    "check_date",
    "check_time",
    "check_time_start",
    "check_time_end",
    "expect_episode_count",
    "found_episode_count",
    "status",
    "retry_deadline",
    "retry_interval_minutes",
    "next_retry_at",
    "seen_episodes",
    "created_at",
    "updated_at",
)


def compute_source_hash(text: str) -> str:
    """公告原文的去重鍵。只做前後去空白就丟 sha256——舊專案沒有做任何正規化
    （站方對同一則公告改個空白就當新公告），這裡跟進，多做「同一則公告輕微改字要
    視為同一則」的模糊比對反而容易誤判把不同公告當成同一則。"""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


class GossipStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            for statement in _TABLE_STATEMENTS:
                conn.execute(statement)
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(gossip_events)")}
            for name, decl in _EVENT_ADDED_COLUMNS.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE gossip_events ADD COLUMN {name} {decl}")

    # ------------------------------------------------------------------
    # gossip_log
    # ------------------------------------------------------------------

    def record_announcement(self, raw_text: str) -> str | None:
        """回傳 `source_hash` 代表這是「這則公告文字第一次出現」，呼叫端才需要往下
        解析／建立子事件；回傳 `None` 代表同一則公告先前已經處理過（內容完全相同），
        這次偵測直接略過。靠 `source_hash UNIQUE` 約束＋捕捉 `IntegrityError` 判斷，
        不先查一次再寫（避免 TOCTOU）。"""
        source_hash = compute_source_hash(raw_text)
        with self._lock:
            with self._database.transaction() as conn:
                try:
                    conn.execute(
                        "INSERT INTO gossip_log (raw_text, source_hash) VALUES (?, ?)",
                        (raw_text, source_hash),
                    )
                except sqlite3.IntegrityError:
                    return None
        return source_hash

    def has_seen(self, source_hash: str) -> bool:
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM gossip_log WHERE source_hash = ? LIMIT 1", (source_hash,)
            ).fetchone()
        return row is not None

    def recent_log(self, limit: int = 50) -> list[dict]:
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT id, captured_at, raw_text, source_hash FROM gossip_log "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # gossip_events
    # ------------------------------------------------------------------

    def add_event(
        self,
        source_hash: str,
        *,
        raw_clause: str,
        action: str,
        match_status: str,
        status_label: str,
        title_in_gossip: str | None = None,
        sn: int | None = None,
        target_date: str | None = None,
        target_time: str | None = None,
        target_time_start: str | None = None,
        target_time_end: str | None = None,
        expect_episode_count: int = 1,
    ) -> int:
        """寫入一筆子事件，回傳新的 event id。`system_suggestion`／`disposition` 都
        由 `status_label` 推出來（初值相同），`disposition_locked` 為 0——之後系統
        自動套用建議時維持 0，使用者在操作處置頁按過「確認」才會被 `set_disposition()`
        設成 1。"""
        _require(action in ACTIONS, f"未知的 action：{action}")
        _require(match_status in MATCH_STATUSES, f"未知的 match_status：{match_status}")
        _require(status_label in STATUS_LABELS, f"未知的 status_label：{status_label}")
        suggestion = STATUS_TO_SUGGESTION[status_label]
        with self._lock:
            with self._database.transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO gossip_events ("
                    "source_hash, raw_clause, action, match_status, title_in_gossip, sn, status_label, "
                    "target_date, target_time, target_time_start, target_time_end, expect_episode_count, "
                    "system_suggestion, disposition, disposition_locked"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
                    (
                        source_hash,
                        raw_clause,
                        action,
                        match_status,
                        title_in_gossip,
                        sn,
                        status_label,
                        target_date,
                        target_time,
                        target_time_start,
                        target_time_end,
                        int(expect_episode_count),
                        suggestion,
                        suggestion,
                    ),
                )
                return int(cursor.lastrowid)

    def get_event(self, event_id: int) -> dict | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_EVENT_COLUMNS)} FROM gossip_events WHERE id = ?",
                (event_id,),
            ).fetchone()
        return _event_row_to_dict(row) if row is not None else None

    def list_events(self, *, limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        """歷史公告：全部子事件，新到舊。回傳 `(這一頁的列, 總筆數)`，總筆數給分頁器算頁數。"""
        with self._database.transaction() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_EVENT_COLUMNS)} FROM gossip_events "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = conn.execute("SELECT COUNT(*) FROM gossip_events").fetchone()[0]
        return [_event_row_to_dict(row) for row in rows], int(total)

    def locked_clauses(self, source_hash: str) -> set[str]:
        """這則公告底下已被使用者手動確認過（`disposition_locked=1`）的子事件原文。
        「重新檢查」據此跳過那些片段、不重新分類（不覆蓋使用者手動調整，使用者 2026-09-01）。"""
        with self._database.transaction() as conn:
            return {
                r["raw_clause"]
                for r in conn.execute(
                    "SELECT raw_clause FROM gossip_events "
                    "WHERE source_hash = ? AND disposition_locked = 1",
                    (source_hash,),
                )
            }

    def delete_unlocked_events(self, source_hash: str) -> int:
        """刪掉這則公告底下所有『使用者沒手動確認過』的子事件（含對應 pending）。
        「重新檢查」用——把舊的（可能誤判的）自動分類清掉重跑。已 locked 的留著。
        回傳刪了幾筆。"""
        with self._lock:
            with self._database.transaction() as conn:
                ids = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM gossip_events "
                        "WHERE source_hash = ? AND disposition_locked = 0",
                        (source_hash,),
                    )
                ]
                for eid in ids:
                    conn.execute("DELETE FROM gossip_pending WHERE event_id = ?", (eid,))
                    conn.execute("DELETE FROM gossip_events WHERE id = ?", (eid,))
        return len(ids)

    def events_by_match_status(self, match_status: str) -> list[dict]:
        """指定 `match_status` 的全部子事件，新到舊。`gossip_watch` 每輪重查
        `no_subscription` 的事件（.0 改進.txt 第 7 項）用。"""
        with self._database.transaction() as conn:
            rows = conn.execute(
                f"SELECT {', '.join(_EVENT_COLUMNS)} FROM gossip_events "
                "WHERE match_status = ? ORDER BY id DESC",
                (match_status,),
            ).fetchall()
        return [_event_row_to_dict(row) for row in rows]

    def get_actionable(self, today: str) -> list[dict]:
        """操作處置：目標日期還沒過去（或根本算不出目標日期）、還沒被使用者確認忽略、
        且對應臨時排程還沒完成的子事件。`today` 傳 `YYYY-MM-DD`（字串比較，ISO 日期
        字典序等同時間序）。`no_subscription`（未訂閱、自動忽略）不列進來。

        `action='reschedule'`（每週更新時間異動，2026-09-01 使用者要求列進來）：不受
        「目標日期過了就消失」限制（那是永久變更、隨時可改回），只有選了「維持原本時段」
        或「忽略此公告」並鎖定後才從列表消失。"""
        with self._database.transaction() as conn:
            rows = conn.execute(
                f"SELECT {', '.join('e.' + c for c in _EVENT_COLUMNS)} FROM gossip_events e "
                "WHERE (e.action = 'reschedule' OR e.target_date IS NULL OR e.target_date >= ?) "
                "AND e.match_status != 'no_subscription' "
                "AND NOT (e.disposition IN (?, ?) AND e.disposition_locked = 1) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM gossip_pending p "
                "  WHERE p.event_id = e.id "
                "  AND p.id = (SELECT MAX(p2.id) FROM gossip_pending p2 WHERE p2.event_id = e.id) "
                "  AND p.status = 'done'"
                ") "
                "ORDER BY e.id DESC",
                (today, _IGNORE_DISPOSITION, "維持原本時段"),
            ).fetchall()
        return [_event_row_to_dict(row) for row in rows]

    def set_manual_sn(self, event_id: int, sn: int) -> None:
        """操作處置頁「指定排程」：公告內容自動比對不到追蹤作品（`match_status='unmatched'`）
        時，讓使用者手動指定對應的 sn。只改 sn 與 `match_status`，不動處置。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE gossip_events SET sn = ?, match_status = 'matched' WHERE id = ?",
                    (sn, event_id),
                )

    def promote_no_subscription(
        self,
        event_id: int,
        sn: int,
        *,
        status_label: str,
        target_date: str | None = None,
        target_time: str | None = None,
        target_time_start: str | None = None,
        target_time_end: str | None = None,
        expect_episode_count: int | None = None,
    ) -> None:
        """`no_subscription` 事件的作品之後被訂閱了（.0 改進.txt 第 7 項）——一次把
        `sn`／`match_status='matched'`／分類結果全部補上，接著呼叫端會 `apply_disposition()`。"""
        _require(status_label in STATUS_LABELS, f"未知的 status_label：{status_label}")
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE gossip_events SET sn = ?, match_status = 'matched', status_label = ?, "
                    "target_date = ?, target_time = ?, target_time_start = ?, target_time_end = ?, "
                    "expect_episode_count = ? WHERE id = ?",
                    (
                        sn, status_label, target_date, target_time, target_time_start,
                        target_time_end, expect_episode_count, event_id,
                    ),
                )

    def promote_unmatched_reschedule(
        self,
        event_id: int,
        sn: int,
        *,
        match_status: str,
        status_label: str,
        target_date: str | None = None,
    ) -> None:
        """更新時間異動公告當初比對不到作品（`match_status='unmatched'`，多半是全新啟動、
        週期表快取還空的當下就掃到）——之後重新比對到了，把 `sn` / `match_status`
        （`matched` 或 `no_subscription`）/ `status_label` 補上。使用者 2026-09-05。"""
        _require(match_status in ("matched", "no_subscription"), f"未知的 match_status：{match_status}")
        _require(status_label in STATUS_LABELS, f"未知的 status_label：{status_label}")
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE gossip_events SET sn = ?, match_status = ?, status_label = ?, "
                    "target_date = ? WHERE id = ? AND match_status = 'unmatched'",
                    (sn, match_status, status_label, target_date, event_id),
                )

    def set_disposition(
        self,
        event_id: int,
        disposition: str,
        *,
        locked: bool,
        target_date: str | None = None,
        target_time: str | None = None,
        target_time_start: str | None = None,
        target_time_end: str | None = None,
        expect_episode_count: int | None = None,
    ) -> None:
        """更新一筆子事件目前生效的處置。`locked=True` 代表使用者在操作處置頁手動按過
        「確認」——之後系統自動判斷不會再改動這筆；系統自己套用 `system_suggestion`
        時要傳 `locked=False`，否則畫面會顯示成「已手動確認」，使用者無法分辨。

        使用者選「自訂更新時間／時間範圍」這類處置時會一併帶新的 `target_*`／集數，
        直接覆蓋原本解析出來的值（自訂值就是新的目標，不另存一組欄位）。臨時排程覆蓋
        的建立／清除是呼叫端（`gossip_watch.apply_disposition()`）的職責，不在這裡。"""
        _require(disposition in DISPOSITION_OPTIONS, f"未知的處置：{disposition}")
        # 處置一有變動（不論系統自動或使用者手動）就清掉「已讀」記號——這樣新的調整
        # 結果會重新冒到頂列鈴鐺，使用者才知道機動調整換了做法。
        sets = [
            "disposition = ?",
            "disposition_locked = ?",
            "disposition_updated_at = datetime('now', 'localtime')",
            "acknowledged_at = NULL",
        ]
        params: list[Any] = [disposition, 1 if locked else 0]
        for column, value in (
            ("target_date", target_date),
            ("target_time", target_time),
            ("target_time_start", target_time_start),
            ("target_time_end", target_time_end),
        ):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        if expect_episode_count is not None:
            sets.append("expect_episode_count = ?")
            params.append(int(expect_episode_count))
        params.append(event_id)
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(f"UPDATE gossip_events SET {', '.join(sets)} WHERE id = ?", params)

    def mark_clauses_seen(self, clauses: set[str], seen_at: str) -> None:
        """把這批 `raw_clause` 目前仍掛在動畫瘋首頁公告欄的子事件標成「這一輪掃描時看到」
        （`GossipWatcher.check_once()` 每輪呼叫，公告沒變也刷新）。歷史公告頁據此判斷哪些
        公告還在生效、要用金框框住。`seen_at` 傳這一輪掃描的時間戳（同一輪所有子事件用
        同一個值），畫面比對「`last_seen_at` 是不是等於最後一輪掃描時間」。"""
        clause_list = [c for c in clauses if c]
        if not clause_list:
            return
        placeholders = ", ".join("?" for _ in clause_list)
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    f"UPDATE gossip_events SET last_seen_at = ? WHERE raw_clause IN ({placeholders})",
                    [seen_at, *clause_list],
                )

    def acknowledge_event(self, event_id: int, at: str) -> None:
        """頂列鈴鐺「了解」按鈕：把這筆自動調整標成已讀，之後不再出現在鈴鐺面板
        （機動調整覆蓋本身保留）。處置一有變動 `set_disposition()` 會把它清回 NULL。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "UPDATE gossip_events SET acknowledged_at = ? WHERE id = ?", (at, event_id)
                )

    # ------------------------------------------------------------------
    # gossip_pending
    # ------------------------------------------------------------------

    def add_pending(
        self,
        *,
        sn: int,
        type: str,
        check_date: str,
        event_id: int | None = None,
        check_time: str | None = None,
        check_time_start: str | None = None,
        check_time_end: str | None = None,
        expect_episode_count: int = 1,
        retry_deadline: str | None = None,
        retry_interval_minutes: int | None = None,
        next_retry_at: str | None = None,
    ) -> int:
        """建立一筆生效中的臨時排程覆蓋，回傳新 id。三種 `type`：
        - `skip_check`：本週這個 sn 直接跳過檢查（暫停更新／暫時下架）
        - `override_check_time`：改成 `check_time` 這個時刻檢查，帶 `retry_deadline`／
          `retry_interval_minutes` 的複查視窗（目標時間到了但影片還沒上架時反覆複查）
        - `override_check_window`：在 `check_time_start`~`check_time_end` 範圍內反覆檢查
          （公告只給模糊時段、沒有明確 HH:MM 時）
        """
        _require(type in PENDING_TYPES, f"未知的 pending type：{type}")
        if type == "override_check_time":
            _require(bool(check_time), "override_check_time 需要 check_time")
        if type == "override_check_window":
            _require(
                bool(check_time_start) and bool(check_time_end),
                "override_check_window 需要 check_time_start 與 check_time_end",
            )
        with self._lock:
            with self._database.transaction() as conn:
                cursor = conn.execute(
                    "INSERT INTO gossip_pending ("
                    "event_id, sn, type, check_date, check_time, check_time_start, check_time_end, "
                    "expect_episode_count, retry_deadline, retry_interval_minutes, next_retry_at, seen_episodes"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '[]')",
                    (
                        event_id,
                        sn,
                        type,
                        check_date,
                        check_time,
                        check_time_start,
                        check_time_end,
                        int(expect_episode_count),
                        retry_deadline,
                        retry_interval_minutes,
                        next_retry_at,
                    ),
                )
                return int(cursor.lastrowid)

    def get_pending(self, pending_id: int) -> dict | None:
        with self._database.transaction() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_PENDING_COLUMNS)} FROM gossip_pending WHERE id = ?",
                (pending_id,),
            ).fetchone()
        return _pending_row_to_dict(row) if row is not None else None

    def list_pending(self, status: str | None = "pending") -> list[dict]:
        query = f"SELECT {', '.join(_PENDING_COLUMNS)} FROM gossip_pending"
        params: tuple = ()
        if status is not None:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY id"
        with self._database.transaction() as conn:
            rows = conn.execute(query, params).fetchall()
        return [_pending_row_to_dict(row) for row in rows]

    def update_pending(self, pending_id: int, **fields: Any) -> None:
        """狀態轉移（`pending` → `done`／`expired`）、已抓到集數計數、下次複查時間等
        欄位的更新。只接受 `_PENDING_UPDATABLE` 白名單裡的欄位；`updated_at` 自動更新。"""
        if not fields:
            return
        unknown = set(fields) - _PENDING_UPDATABLE
        _require(not unknown, f"update_pending 不接受這些欄位：{sorted(unknown)}")
        if "status" in fields:
            _require(fields["status"] in PENDING_STATUSES, f"未知的 status：{fields['status']}")
        if "seen_episodes" in fields and not isinstance(fields["seen_episodes"], str):
            fields["seen_episodes"] = json.dumps(list(fields["seen_episodes"]))
        set_clause = ", ".join(f"{key} = ?" for key in fields)
        params = list(fields.values())
        params.append(pending_id)
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    f"UPDATE gossip_pending SET {set_clause}, updated_at = datetime('now', 'localtime') "
                    "WHERE id = ?",
                    params,
                )

    def remove_pending_for_event(self, event_id: int) -> None:
        """使用者在操作處置頁重新選了一個不同的處置方式時，先清掉這個子事件先前建立的
        還在生效中（`status='pending'`）的舊覆蓋，避免同一則公告疊加出兩筆互相矛盾的
        臨時排程。已經 `done`／`expired` 的保留當歷史。"""
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "DELETE FROM gossip_pending WHERE event_id = ? AND status = 'pending'",
                    (event_id,),
                )

    def is_skipped(self, sn: int, check_date: str) -> bool:
        """`main_loop.py`／`custom_schedule.py` 在檢查某個 sn 前多問一次：這個 sn 在
        `check_date`（`YYYY-MM-DD`）當天有沒有生效中的「本週暫停／下架」覆蓋。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM gossip_pending "
                "WHERE sn = ? AND check_date = ? AND type = 'skip_check' AND status = 'pending' LIMIT 1",
                (sn, check_date),
            ).fetchone()
        return row is not None

    def has_takedown_since(self, sn: int, since_iso: str) -> bool:
        """番劇完結偵測（`scheduler/completion_watch.py`）用：這個 sn 在 `since_iso`
        之後有沒有一則「下架」公告事件。有的話代表這部從週期表消失是「暫時下架」、
        不是完結。`status_label='暫時下架'`（gossip 系統對「下架」的正規化標籤）或
        原文含「下架」都算。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM gossip_events "
                "WHERE sn = ? AND created_at >= ? "
                "AND (status_label = '暫時下架' OR raw_clause LIKE '%下架%') LIMIT 1",
                (sn, since_iso),
            ).fetchone()
        return row is not None

    def active_override(self, sn: int, check_date: str) -> dict | None:
        """`main_loop.py`／`custom_schedule.py` 查：這個 sn 在 `check_date` 當天有沒有
        生效中的「改時間／改時段」覆蓋。有的話回傳那筆 `gossip_pending`（呼叫端據此
        決定該在什麼時間點檢查）；同一天同一 sn 若有多筆取最新一筆。"""
        with self._database.transaction() as conn:
            row = conn.execute(
                f"SELECT {', '.join(_PENDING_COLUMNS)} FROM gossip_pending "
                "WHERE sn = ? AND check_date = ? AND status = 'pending' "
                "AND type IN ('override_check_time', 'override_check_window') "
                "ORDER BY id DESC LIMIT 1",
                (sn, check_date),
            ).fetchone()
        return _pending_row_to_dict(row) if row is not None else None

    def has_reschedule_event(self, title_in_gossip: str, target_time: str) -> bool:
        """已經處理過「這部番、這個新時段」的每週更新時間變更公告了嗎？
        （`gossip_watch._process_reschedule` 用來去重——公告會掛在站上一整週，
        期間跑馬燈其他內容變動不能讓它每次都重套一次排程覆蓋。）"""
        with self._database.transaction() as conn:
            row = conn.execute(
                "SELECT 1 FROM gossip_events WHERE action = 'reschedule' "
                "AND title_in_gossip = ? AND target_time = ? LIMIT 1",
                (title_in_gossip, target_time),
            ).fetchone()
        return row is not None

    def prune(self, retention_days: int) -> int:
        """清掉已經結束（`done`／`expired`）且超過保留天數的覆蓋，以及超過保留天數、
        又沒有生效中覆蓋的舊子事件（`no_subscription` 之類會一直累積、每輪被重掃）。
        生效中（`pending`）的覆蓋、以及它對應的事件一律保留。回傳刪除的覆蓋筆數。"""
        cutoff = (datetime.now() - timedelta(days=retention_days)).strftime(_TIMESTAMP_FMT)
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute(
                    "DELETE FROM gossip_events WHERE created_at < ? AND id NOT IN ("
                    "  SELECT event_id FROM gossip_pending "
                    "  WHERE event_id IS NOT NULL AND status = 'pending')",
                    (cutoff,),
                )
                cursor = conn.execute(
                    "DELETE FROM gossip_pending WHERE status != 'pending' AND updated_at < ?",
                    (cutoff,),
                )
                return cursor.rowcount

    # ------------------------------------------------------------------
    # 資料庫整頓（階段 5，見 docs/requirements/web_redesign_round2.md）
    # ------------------------------------------------------------------

    def sns_with_records(self) -> set[int]:
        """`gossip_events`（有比對到 sn 的）＋ `gossip_pending` 涵蓋到的所有 sn。"""
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT sn FROM gossip_events WHERE sn IS NOT NULL "
                "UNION SELECT sn FROM gossip_pending"
            ).fetchall()
        return {row["sn"] for row in rows}

    def record_summary_for_sn(self, sn: int) -> dict:
        """給整頓畫面：這個 sn 在兩張表各幾筆＋一個可顯示的標題（`title_in_gossip`）。"""
        with self._database.transaction() as conn:
            events = conn.execute(
                "SELECT COUNT(*) AS n FROM gossip_events WHERE sn = ?", (sn,)
            ).fetchone()["n"]
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM gossip_pending WHERE sn = ?", (sn,)
            ).fetchone()["n"]
            title_row = conn.execute(
                "SELECT title_in_gossip FROM gossip_events "
                "WHERE sn = ? AND title_in_gossip IS NOT NULL AND title_in_gossip != '' LIMIT 1",
                (sn,),
            ).fetchone()
        return {
            "gossip_events": events,
            "gossip_pending": pending,
            "title": title_row["title_in_gossip"] if title_row else None,
        }

    def delete_for_sn(self, sn: int) -> int:
        """清掉一部番劇在 `gossip_events`＋`gossip_pending` 的所有列。回傳刪掉幾列。"""
        with self._lock:
            with self._database.transaction() as conn:
                n_events = conn.execute("DELETE FROM gossip_events WHERE sn = ?", (sn,)).rowcount
                n_pending = conn.execute("DELETE FROM gossip_pending WHERE sn = ?", (sn,)).rowcount
        return n_events + n_pending


def _event_row_to_dict(row) -> dict:
    data = dict(row)
    data["disposition_locked"] = bool(data["disposition_locked"])
    return data


def _pending_row_to_dict(row) -> dict:
    data = dict(row)
    data["seen_episodes"] = json.loads(data["seen_episodes"]) if data.get("seen_episodes") else []
    return data


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)
