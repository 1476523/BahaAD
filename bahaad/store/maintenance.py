"""版本更新後的資料庫檢查機制（使用者 2026-10-06）。

每次程式版本跟上次跑過的不同（更新後第一次啟動，或第一次升級到有這個機制的版本），
啟動時自動跑一輪「資料庫整理」，分三類，每一步都是**可重複執行（idempotent）**的：

- 自動加入（add）：補齊後來才新增的欄位等，舊資料庫升級後也有。
- 自動刪除（delete）：清掉已經廢棄的設定鍵、指向不存在項目的孤兒紀錄。
- 自動更改（change）：把既有資料改成新規則（例如日誌裡舊的站名字樣）。

跑完把版本記在設定 `_db_maintenance_version`；只要有任何一步失敗就不記，下次啟動重跑。
要加新的整理動作：在 `STEPS` 加一個 `Step`（寫成可重複執行的）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable

from bahaad.logging_db import (
    display_log_source, sanitize_log_message, sanitize_log_source, translate_log_message,
)
from bahaad.store.database import Database
from bahaad.store.settings import SettingsStore

logger = logging.getLogger(__name__)

VERSION_KEY = "_db_maintenance_version"

KIND_LABELS = {"add": "加入", "delete": "刪除", "change": "更改"}

# 後來才加的欄位：（表, 欄位, 宣告）。各 store 自己的建構子也會補，這裡是升級後的保險。
EXPECTED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("newanime_item", "converted_at", "TEXT"),
    ("newanime_item", "catch_up_target", "INTEGER"),
    ("newanime_item", "catch_up_deadline", "TEXT"),
    ("newanime_item", "is_continuing", "INTEGER NOT NULL DEFAULT 0"),
    ("newanime_item", "air_override_date", "TEXT"),
    ("newanime_item", "air_override_time", "TEXT"),
)

# 已經廢棄、不會再被讀取的設定鍵
OBSOLETE_SETTING_KEYS: tuple[str, ...] = ()
# `notify_categories` 這個設定裡已移除的通知類別
OBSOLETE_NOTIFY_CATEGORIES: tuple[str, ...] = ("system_new_version",)


@dataclass(frozen=True)
class Step:
    id: str
    kind: str  # add | delete | change
    description: str
    run: Callable[[Database, SettingsStore], int]


@dataclass
class MaintenanceReport:
    version: str
    counts: dict[str, int] = field(default_factory=lambda: {"add": 0, "delete": 0, "change": 0})
    failed: list[str] = field(default_factory=list)

    def summary(self) -> str:
        parts = [f"{KIND_LABELS[k]} {n} 項" for k, n in self.counts.items()]
        text = f"資料庫版本檢查（{self.version}）：" + "、".join(parts)
        if self.failed:
            text += f"；失敗的步驟：{', '.join(self.failed)}"
        return text


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _add_missing_columns(database: Database, _settings: SettingsStore) -> int:
    added = 0
    with database.transaction() as conn:
        for table, column, decl in EXPECTED_COLUMNS:
            if not _table_exists(conn, table):
                continue
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
                added += 1
    return added


def _delete_obsolete_settings(database: Database, settings: SettingsStore) -> int:
    removed = 0
    with database.transaction() as conn:
        for key in OBSOLETE_SETTING_KEYS:
            removed += conn.execute("DELETE FROM settings WHERE key = ?", (key,)).rowcount
    categories = settings.get("notify_categories", None)
    if isinstance(categories, dict):
        stale = [c for c in OBSOLETE_NOTIFY_CATEGORIES if c in categories]
        if stale:
            settings.update({"notify_categories": {k: v for k, v in categories.items() if k not in stale}})
            removed += len(stale)
    return removed


def _delete_orphan_tracked_newanime(database: Database, _settings: SettingsStore) -> int:
    with database.transaction() as conn:
        if not (_table_exists(conn, "newanime_tracked") and _table_exists(conn, "newanime_item")):
            return 0
        return conn.execute(
            "DELETE FROM newanime_tracked WHERE virtual_sn NOT IN (SELECT virtual_sn FROM newanime_item)"
        ).rowcount


def _change_log_site_names(database: Database, _settings: SettingsStore) -> int:
    """舊日誌裡的第三方資料站名稱 → 「新番資訊抓取」（新寫入的紀錄在寫入時就處理了）。"""
    changed = 0
    with database.transaction() as conn:
        if not _table_exists(conn, "logs"):
            return 0
        rows = conn.execute(
            "SELECT id, logger, message FROM logs "
            "WHERE logger LIKE '%youranimes%' OR message LIKE '%youranimes%' "
            "OR logger LIKE '%your animes%' OR message LIKE '%your animes%' "
            "OR logger = '新番資訊抓取'"
        ).fetchall()
        for row in rows:
            new_logger = sanitize_log_source(row["logger"])
            new_message = sanitize_log_message(row["message"])
            if new_logger != row["logger"] or new_message != row["message"]:
                conn.execute(
                    "UPDATE logs SET logger = ?, message = ? WHERE id = ?",
                    (new_logger, new_message, row["id"]),
                )
                changed += 1
    return changed


def _change_unsubscribed_entries_inactive(database: Database, _settings: SettingsStore) -> int:
    """舊版退訂（保留改名／分類）後的項目沒有排程時段，被當成「全域輪詢項目」繼續自動下載
    （使用者 2026-10-08）。訂閱一定有時段，所以沒時段的項目就是退訂殘留——標成 inactive。"""
    with database.transaction() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(schedule_entries)")}
        if "inactive" not in columns:
            return 0
        return conn.execute(
            "UPDATE schedule_entries SET inactive = 1 "
            "WHERE line_type = 'entry' AND schedule_weekday IS NULL AND inactive = 0"
        ).rowcount


def _change_log_translate(database: Database, _settings: SettingsStore) -> int:
    """既有日誌全部轉成繁體中文：來源欄（logger 名稱）換成中文名稱、訊息裡的連線錯誤／例外
    堆疊／內部名稱英文翻掉（使用者 2026-10-09「下次更新時自動將日誌進行轉換」）。可重複執行。"""
    changed = 0
    with database.transaction() as conn:
        if not _table_exists(conn, "logs"):
            return 0
        rows = conn.execute("SELECT id, logger, message FROM logs").fetchall()
        for row in rows:
            new_logger = display_log_source(row["logger"])
            new_message = translate_log_message(sanitize_log_message(row["message"]))
            if new_logger != row["logger"] or new_message != row["message"]:
                conn.execute(
                    "UPDATE logs SET logger = ?, message = ? WHERE id = ?",
                    (new_logger, new_message, row["id"]),
                )
                changed += 1
    return changed


STEPS: tuple[Step, ...] = (
    Step("add_missing_columns", "add", "補齊後來新增的欄位", _add_missing_columns),
    Step("delete_obsolete_settings", "delete", "清掉已廢棄的設定", _delete_obsolete_settings),
    Step(
        "delete_orphan_tracked_newanime", "delete", "清掉指向不存在新番的追蹤紀錄",
        _delete_orphan_tracked_newanime,
    ),
    Step(
        "change_unsubscribed_entries_inactive", "change",
        "退訂後殘留的項目標為不再自動下載", _change_unsubscribed_entries_inactive,
    ),
    Step("change_log_site_names", "change", "日誌裡的舊站名字樣改為「新番資訊抓取」", _change_log_site_names),
    Step("change_log_translate", "change", "日誌翻成繁體中文（來源名稱、連線錯誤、例外堆疊）", _change_log_translate),
)


def run_maintenance(
    database: Database, settings: SettingsStore, version: str, extra_steps: tuple[Step, ...] = ()
) -> MaintenanceReport:
    """無條件跑全部步驟（測試／手動用）；`run_if_version_changed` 才判斷版本。"""
    report = MaintenanceReport(version=version)
    for step in (*STEPS, *extra_steps):
        try:
            report.counts[step.kind] += int(step.run(database, settings) or 0)
        except Exception:  # noqa: BLE001 - 單一步驟失敗不能擋住啟動，也不能擋住其他步驟
            logger.exception("資料庫版本檢查的步驟失敗：%s", step.id)
            report.failed.append(step.id)
    return report


def run_if_version_changed(
    database: Database, settings: SettingsStore, version: str, extra_steps: tuple[Step, ...] = ()
) -> MaintenanceReport | None:
    """版本跟上次跑過的不同才整理；沒有失敗就記下這個版本。回傳 None＝這個版本已跑過。"""
    if settings.get(VERSION_KEY) == version:
        return None
    report = run_maintenance(database, settings, version, extra_steps)
    logger.info("%s", report.summary())
    if not report.failed:
        settings.update({VERSION_KEY: version})
    return report
