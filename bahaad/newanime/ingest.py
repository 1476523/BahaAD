"""把「抓 GNN 文章 → 解析 → 寫進 `newanime_item` + 配虛構 sn」串起來。

規格 docs/requirements/new_anime_bulletin.md §2、§4、§5。

**這裡不做 diff → 通知**（那是階段 3 的 `NewAnimeWatcher`）。`sync_from_gnn()` 回一份
`GnnSyncResult`（新增了哪些、哪些還在、更新了哪些），階段 3 拿去比對發通知。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bahaad.newanime.parse import ParsedBulletin, ParsedBulletinItem, parse_gnn_article
from bahaad.store.newanime_cache import _GNN_FIELDS, NewAnimeCacheStore

# 虛構 sn 序號的排序鍵：首播日期 → 首播時間 → 文章原始順序（待定的排最後）
_FAR_FUTURE = ("9999-99-99", "99:99")


@dataclass(frozen=True)
class TimeChange:
    virtual_sn: int
    old_weekday: int | None
    old_time: str | None
    new_weekday: int | None
    new_time: str | None


@dataclass
class GnnSyncResult:
    season_key: str
    published_at: str | None
    pending_note_present: bool
    added_virtual_sns: list[int] = field(default_factory=list)
    updated_virtual_sns: list[int] = field(default_factory=list)
    time_changes: list[TimeChange] = field(default_factory=list)
    seen_source_names: set[str] = field(default_factory=set)


def _sort_key(item: ParsedBulletinItem):
    return (
        item.first_air_date or _FAR_FUTURE[0],
        item.first_air_time or _FAR_FUTURE[1],
        item.article_order,
    )


def _gnn_fields(item: ParsedBulletinItem) -> dict:
    return {
        "source_name": item.source_name,
        "first_air_date": item.first_air_date,
        "first_air_time": item.first_air_time,
        "first_air_weekday": item.first_air_weekday,
        "first_ep_count": item.first_ep_count,
        "first_ep_number": item.first_ep_number,
        "backfill_from_episode": item.backfill_from_episode,
        "ongoing_weekday": item.ongoing_weekday,
        "ongoing_time": item.ongoing_time,
        "is_vip": int(item.is_vip),
        "region_locked": int(item.region_locked),
        "is_undetermined": int(item.is_undetermined),
        "article_order": item.article_order,
    }


def sync_from_gnn(
    store: NewAnimeCacheStore,
    parsed: ParsedBulletin,
    *,
    season_key: str,
    now_iso: str,
) -> GnnSyncResult:
    """把一次 GNN 解析結果寫進 store。`newanime_bulletin` 那一季的列**要先存在**
    （呼叫端 `upsert_bulletin`）。"""
    result = GnnSyncResult(
        season_key=season_key,
        published_at=parsed.published_at,
        pending_note_present=parsed.pending_note_present,
        seen_source_names={it.source_name for it in parsed.items},
    )
    if parsed.published_at:
        store.set_published_at(season_key, parsed.published_at)
    if not parsed.pending_note_present:
        store.mark_pending_note_gone(season_key, now_iso)

    existing_by_name = {row["source_name"]: row for row in store.list_items(season_key)}

    new_items: list[ParsedBulletinItem] = []
    for item in parsed.items:
        row = existing_by_name.get(item.source_name)
        if row is None:
            new_items.append(item)
            continue
        # 已存在 → 更新 GNN 欄位（若有變）＋摸一下 last_seen
        fields = _gnn_fields(item)
        if any(row.get(k) != fields.get(k) for k in _GNN_FIELDS):
            wd_or_time_changed = (
                row.get("first_air_weekday") != fields.get("first_air_weekday")
                or row.get("first_air_time") != fields.get("first_air_time")
            )
            store.update_item_gnn_fields(row["virtual_sn"], fields, seen_at=now_iso)
            result.updated_virtual_sns.append(row["virtual_sn"])
            if wd_or_time_changed:
                result.time_changes.append(
                    TimeChange(
                        virtual_sn=row["virtual_sn"],
                        old_weekday=row.get("first_air_weekday"),
                        old_time=row.get("first_air_time"),
                        new_weekday=fields.get("first_air_weekday"),
                        new_time=fields.get("first_air_time"),
                    )
                )
        else:
            store.touch_seen(row["virtual_sn"], now_iso)

    # 新的番依「首播日期 → 首播時間 → 文章順序」排序後，依序配虛構 sn 序號
    # （首次掃描：全部都是新的、從 001 起；之後：接在 next_seq 後面、不重排既有的）
    if new_items:
        sorted_new = sorted(new_items, key=_sort_key)
        start = store.take_virtual_seq(season_key, len(sorted_new))
        for offset, item in enumerate(sorted_new):
            virtual_sn = int(f"{season_key}{start + offset:03d}")
            store.add_item(virtual_sn, season_key, _gnn_fields(item), seen_at=now_iso)
            result.added_virtual_sns.append(virtual_sn)

    return result


def parse_and_sync_gnn(
    store: NewAnimeCacheStore,
    html: str,
    *,
    season_key: str,
    now_iso: str,
) -> GnnSyncResult:
    """`parse_gnn_article` + `sync_from_gnn` 一起。"""
    return sync_from_gnn(
        store, parse_gnn_article(html, season_key=season_key),
        season_key=season_key, now_iso=now_iso,
    )
