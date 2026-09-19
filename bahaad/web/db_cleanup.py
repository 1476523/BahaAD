"""資料庫整頓。規格見 docs/requirements/web_redesign_round2.md 階段 5。

BahaAD 沒有舊專案那種 `anime` 下載歷史表，但這一輪之後會累積四種「指向某個 sn 的
資料庫紀錄」：`gossip_events`／`gossip_pending`（監視公告）、`manual_tasks`（未完成的
手動任務）、`skipped_episodes`（使用者標記為已下載的集數）。取消訂閱一部番劇、或直接
編輯排程清單把它移掉之後，這些紀錄就變成孤兒。

兩個清理入口都用這裡的函式：
  - 退訂鈴鐺（`web/browse.py`）：純鈴鐺項目退訂＝整條移除＋連帶 `delete_records()`
  - 設定頁的整頓畫面（`web/settings.py`）：列出所有「已不在追蹤清單、但 DB 還有紀錄」
    的 sn（`list_orphans()`）讓使用者勾選，批次 `delete_records()`

**只動資料庫紀錄，不碰已下載的檔案。**
"""

from __future__ import annotations


def _manual_task_sns(manual_task_store) -> set[int]:
    return {task["sn"] for task in manual_task_store.list_tasks()}


def _manual_task_rename(manual_task_store, sn: int) -> str | None:
    for task in manual_task_store.list_tasks():
        if task["sn"] == sn:
            return (task.get("params") or {}).get("rename")
    return None


def delete_records(
    sn: int,
    *,
    gossip_store=None,
    manual_task_store=None,
    skipped_episode_store=None,
) -> dict[str, int]:
    """刪掉一個 sn 在四張表的所有列，回傳各表刪了幾筆（0 也會列出，方便呼叫端統計）。"""
    counts: dict[str, int] = {}
    if gossip_store is not None:
        counts["gossip"] = gossip_store.delete_for_sn(sn)
    if manual_task_store is not None:
        counts["manual_tasks"] = manual_task_store.remove_task(sn)
    if skipped_episode_store is not None:
        counts["skipped_episodes"] = skipped_episode_store.delete_for_sn(sn)
    return counts


def list_orphans(
    schedule_store,
    *,
    gossip_store=None,
    manual_task_store=None,
    skipped_episode_store=None,
) -> list[dict]:
    """回傳「已不在追蹤清單、但 DB 還有紀錄」的 sn，每筆帶標題＋各表筆數。依 sn 排序。"""
    tracked = set(schedule_store.get_entries().keys())

    candidate_sns: set[int] = set()
    if gossip_store is not None:
        candidate_sns |= gossip_store.sns_with_records()
    if manual_task_store is not None:
        candidate_sns |= _manual_task_sns(manual_task_store)
    if skipped_episode_store is not None:
        candidate_sns |= skipped_episode_store.sns_with_records()

    orphans = []
    for sn in sorted(candidate_sns - tracked):
        gossip_summary = (
            gossip_store.record_summary_for_sn(sn)
            if gossip_store is not None
            else {"gossip_events": 0, "gossip_pending": 0, "title": None}
        )
        manual_count = 1 if (manual_task_store is not None and sn in _manual_task_sns(manual_task_store)) else 0
        skipped_count = skipped_episode_store.count_for_sn(sn) if skipped_episode_store is not None else 0
        skipped_title = None
        if skipped_episode_store is not None:
            for row in skipped_episode_store.list_all():
                if row["sn"] == sn:
                    skipped_title = row["anime_title"]
                    break

        title = (
            skipped_title
            or gossip_summary["title"]
            or (_manual_task_rename(manual_task_store, sn) if manual_task_store is not None else None)
        )

        counts = {
            "gossip_events": gossip_summary["gossip_events"],
            "gossip_pending": gossip_summary["gossip_pending"],
            "manual_tasks": manual_count,
            "skipped_episodes": skipped_count,
        }
        orphans.append(
            {
                "sn": sn,
                "title": title,
                "counts": counts,
                "total": sum(counts.values()),
            }
        )
    return orphans
