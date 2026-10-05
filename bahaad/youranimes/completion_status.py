"""跟 youranimes.tw 的搜尋 API 確認一部番劇是不是真的完結了——完結偵測的最後一道
確認關卡，見 docs/requirements/completion_detection.md「youranimes 完結確認關卡」。

使用者 2026-09-29 提議：週期表消失式的判斷本質上是推測，youranimes 的番劇資料本身有
明確的 `status`（`播映中`／`播映完畢`）欄位，用它在真的要判定完結之前再確認一次，可以
擋掉「週期表本身的比對出問題（例如同一天稍早修好的更名比對 bug）」這類情況——即使
以後又冒出其他類似的比對問題，這道關卡也能獨立擋下來，不是取代原本的偵測機制。

**只在完結偵測已經判定「連續缺席夠多輪」之後才呼叫**，不是每輪都查（youranimes 是
第三方站，沒必要對每個訂閱番劇每 10 分鐘都打一次）。查詢失敗、配不到、配到不只一筆
都當「不確定」，回 `None`——呼叫端維持原本「連續缺席就完結」的判斷，不會因為這一關
查不到反而卡住不完結（youranimes 掛掉／改版／没收錄都不該讓完結偵測整個失效）。

**比對重用 `youranimes/match.py` 的 `match_record()`**，不是自己重寫一套簡單字串比對：
實測 `tk=小書痴的下剋上` 這個真實案例發現，youranimes 搜尋 API 的 `name` 欄位（例如
「小書痴的下剋上 領主的養女」）可能整段省略動畫瘋原始標題裡的副標（「為了成為圖書
管理員不擇手段！」），只靠正規化後的字串完全相等根本配不到——這正是 `match_record()`
第 4 層「前綴＋後段字詞篩選」設計來解決的情況，不應該另外重造一套比較弱的比對邏輯。
"""

from __future__ import annotations

import logging
import re

from bahaad.scheduler.gossip_watch import build_search_title
from bahaad.youranimes.fetch import YourAnimesError
from bahaad.youranimes.match import match_record, normalize_title_key, title_base_key
from bahaad.youranimes.models import YourAnimesRecord

logger = logging.getLogger(__name__)

_STILL_AIRING_STATUS = "播映中"
_FINISHED_STATUS = "播映完畢"
_FIRST_SEGMENT_RE = re.compile(r"[\s　]+")


def _to_records(results: list[dict]) -> list[YourAnimesRecord]:
    records = []
    for r in results:
        name = (r.get("name") or "").strip()
        if not name:
            continue
        _base, season = build_search_title(name)
        try:
            anime_id = int(r.get("_id") or 0)
        except (TypeError, ValueError):
            anime_id = 0
        records.append(
            YourAnimesRecord(
                anime_id=anime_id, zh_title=name, season_number=season,
                season_slug=str(r.get("_id") or ""),
            )
        )
    return records


def is_still_airing(fetcher, title: str) -> bool | None:
    """回 `True`＝youranimes 說還在播映中（完結偵測這輪該暫緩，等下輪再查）；
    `False`＝youranimes 也同意已經播映完畢；`None`＝配不到／查詢失敗／不確定，
    呼叫端維持原本「連續缺席就完結」的既有判斷。"""
    title = (title or "").strip()
    if not title:
        return None
    try:
        results = fetcher.search_animes(title)
    except YourAnimesError as exc:
        logger.debug("youranimes 完結確認查詢失敗：%s", exc)
        return None
    except Exception:  # noqa: BLE001 - 這道關卡本身出狀況不該讓完結偵測整個當掉
        logger.debug("youranimes 完結確認查詢發生未預期例外", exc_info=True)
        return None

    records = _to_records(results)
    if not records:
        return None

    by_base: dict[str, list[YourAnimesRecord]] = {}
    by_prefix: dict[str, list[YourAnimesRecord]] = {}
    for rec in records:
        by_base.setdefault(title_base_key(rec.zh_title), []).append(rec)
        front = _FIRST_SEGMENT_RE.split(rec.zh_title.strip(), maxsplit=1)[0]
        by_prefix.setdefault(normalize_title_key(front), []).append(rec)

    matched = match_record(
        [title], lookup=lambda k: by_base.get(k, []), prefix_lookup=lambda k: by_prefix.get(k, [])
    )
    if matched is None:
        return None

    status = next(
        (r.get("status") for r in results if (r.get("name") or "").strip() == matched.zh_title), None
    )
    if status == _STILL_AIRING_STATUS:
        return True
    if status == _FINISHED_STATUS:
        return False
    return None
