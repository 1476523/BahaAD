"""把「抓 GNN 文章 → 解析 → 寫進 `newanime_item` + 配虛構 sn」串起來。

規格 docs/requirements/new_anime_bulletin.md §2、§4、§5。

**這裡不做 diff → 通知**（那是階段 3 的 `NewAnimeWatcher`）。`sync_from_gnn()` 回一份
`GnnSyncResult`（新增了哪些、哪些還在、更新了哪些），階段 3 拿去比對發通知。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher

from bahaad.newanime.enrich import match_key
from bahaad.scheduler.gossip_watch import season_compatible, season_number
from bahaad.newanime.parse import ParsedBulletin, ParsedBulletinItem, parse_gnn_article
from bahaad.store.newanime_cache import _GNN_FIELDS, NewAnimeCacheStore

def _loose_key(name: str) -> str:
    """`match_key` 再把所有標點符號丟掉，只留文字／數字——只給「名稱備援比對」用。"""
    return "".join(ch for ch in match_key(name) if ch.isalnum())


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
        "is_continuing": int(item.is_continuing),
        "article_order": item.article_order,
    }


_FUZZY_MIN_RATIO = 0.8
_FUZZY_MIN_GAP = 0.05


def _match_existing(
    parsed_items: tuple[ParsedBulletinItem, ...], existing_rows: list[dict]
) -> dict[int, dict]:
    """把公告裡的每一部番對到既有的 `newanime_item`（`{parsed 索引: 既有列}`）。

    seasonal.php 提早偵測建的項目跟公告的番名常常不是完全同字（使用者 2026-10-05：公告
    出現、時間確定後原本追蹤的項目被當成消失→「暫時被下架」、封面圖也不見，因為公告的
    番名被當成另一部新番新增，舊的那筆就變成「公告裡沒出現」）。依序嘗試三層，每層都只
    採信**唯一**的對應，寧可當新增也不要配錯：

    1. 番名完全相等。
    2. 去標點／空白／季別寫法後相等（`_loose_key`），且這個鍵在兩邊各只出現一次
       （「X」跟「X 第二季」可能同鍵，這種就不備援）。
    3. 剩下沒配到的兩邊：相似度（difflib）夠高、明顯贏過第二名、**雙方互為最佳**、而且
       季別寫法相容（擋掉「X 第二季」配到「X 第三季」）——處理「有什麼問題嗎」vs「有問題嗎」
       這種用字不同的情況。
    """
    matched: dict[int, dict] = {}
    claimed: set[int] = set()

    by_name = {row["source_name"]: row for row in existing_rows}
    for index, item in enumerate(parsed_items):
        row = by_name.get(item.source_name)
        if row is not None and row["virtual_sn"] not in claimed:
            matched[index] = row
            claimed.add(row["virtual_sn"])

    left_items = [i for i in range(len(parsed_items)) if i not in matched]
    left_rows = [r for r in existing_rows if r["virtual_sn"] not in claimed]
    if not left_items or not left_rows:
        return matched

    item_counts: dict[str, int] = {}
    for i in left_items:
        key = _loose_key(parsed_items[i].source_name)
        item_counts[key] = item_counts.get(key, 0) + 1
    row_counts: dict[str, int] = {}
    for r in left_rows:
        key = _loose_key(r["source_name"])
        row_counts[key] = row_counts.get(key, 0) + 1
    rows_by_key = {_loose_key(r["source_name"]): r for r in left_rows}
    for i in list(left_items):
        key = _loose_key(parsed_items[i].source_name)
        row = rows_by_key.get(key)
        if (
            row is not None
            and item_counts.get(key) == 1
            and row_counts.get(key) == 1
            and season_compatible(season_number(parsed_items[i].source_name), season_number(row["source_name"]))
        ):
            matched[i] = row
            claimed.add(row["virtual_sn"])
            left_items.remove(i)
    left_rows = [r for r in left_rows if r["virtual_sn"] not in claimed]
    if not left_items or not left_rows:
        return matched

    def _score(item_idx: int, row: dict) -> float:
        a = parsed_items[item_idx].source_name
        b = row["source_name"]
        if not season_compatible(season_number(a), season_number(b)):
            return 0.0
        return SequenceMatcher(None, _loose_key(a), _loose_key(b)).ratio()

    def _best(scores: list[tuple[float, object]]):
        ranked = sorted(scores, key=lambda t: t[0], reverse=True)
        if not ranked or ranked[0][0] < _FUZZY_MIN_RATIO:
            return None
        if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < _FUZZY_MIN_GAP:
            return None
        return ranked[0][1]

    best_row_for_item = {
        i: _best([(_score(i, r), r["virtual_sn"]) for r in left_rows]) for i in left_items
    }
    best_item_for_row = {
        r["virtual_sn"]: _best([(_score(i, r), i) for i in left_items]) for r in left_rows
    }
    rows_by_sn = {r["virtual_sn"]: r for r in left_rows}
    for i in left_items:
        sn = best_row_for_item[i]
        if sn is not None and best_item_for_row.get(sn) == i:
            matched[i] = rows_by_sn[sn]
    return matched


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

    existing_rows = store.list_items(season_key)
    matched = _match_existing(parsed.items, existing_rows)

    new_items: list[ParsedBulletinItem] = []
    for index, item in enumerate(parsed.items):
        row = matched.get(index)
        if row is None:
            new_items.append(item)
            continue
        if row["source_name"] != item.source_name:
            # 既有那筆的 source_name 沒變（不可變），但這次掃描「有看到它」
            result.seen_source_names.add(row["source_name"])
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
