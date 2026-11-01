"""把動畫瘋的番劇標題配到 youranimes 的一筆記錄。純函式，好測。

比對邏輯重用 `scheduler/gossip_watch` 既有的標題正規化（不另立一套）：
- `build_search_title(title)` → (去季別的基礎名, 明確寫出的季別數字)
- `season_compatible(a, b)` → 沒寫季別一律當第一季；只有雙方都明確寫出且不同才不相容
  （擋掉《相反的你和我》被誤配到「相反的你和我 第二季」這種）

呼叫端給的 `bahamut_titles` 依優先序（週期表原始標題優先、再 `AnimeDetail.title`）。
`lookup(base_casefolded)` = `YourAnimesCacheStore.find_by_base`。

**分層比對策略**（使用者 2026-09-05 回饋——動畫瘋跟 youranimes 的標題常有落差，光靠
`title_base_key` 精確比對常常配不到）：

1. 原始標題（含季別，若有）。
2. 動畫瘋偶爾用純數字季別（不寫「第」「S」「Season」，例如「GRAND BLUE 碧藍之海 3」）
   →轉成中文「第N季」再試一次。
3. 去掉季別（不重組回中文），只留基礎名稱——處理「動畫瘋標了季別、youranimes 沒標」
   這個方向（跟第 1 層對稱）。
4. 前三層都是「整串正規化後完全相等」比對，還是配不到的話，可能是其中一邊的標題被
   截短、多寫了副標題、或反過來「查詢端沒季別但儲存端有」（去掉季別的基礎名稱天生是
   有季別那筆的前綴，例如「超超超超超喜歡你的 100 個女朋友」vs youranimes「...第三季」；
   也涵蓋動畫瘋「我是不才惡女」vs youranimes「我是不才惡女～雛宮蝶鼠互換傳～」）：用
   標題的第一段（第一個空白前）當前綴去找候選（`prefix_lookup`），只有一筆就直接用；
   多筆的話再看候選標題有沒有出現「第一段之後」那段文字的任一詞，篩到剩一筆才用，
   篩不出來就放棄（寧可配不到，不要配錯）。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable, Sequence

from bahaad.scheduler.gossip_watch import (
    build_search_title,
    season_compatible,
    season_number,
    season_number_to_zh,
    strip_season_suffix,
)
from bahaad.youranimes.models import YourAnimesRecord

_Lookup = Callable[[str], list[YourAnimesRecord]]


def normalize_title_key(title: str) -> str:
    """比對鍵共用的正規化：NFKC（統一羅馬數字／全形半形字元差異，例如「Ⅱ」→「II」）
    ＋去除**所有**空白（不只頭尾，動畫瘋跟 youranimes 標題常常只差空格位置——「GRAND
    BLUE」vs「GRANDBLUE」、「世界最強後衛 ～...～」vs「世界最強後衛～...～」）＋
    casefold。"""
    normalized = unicodedata.normalize("NFKC", title or "")
    return "".join(normalized.split()).casefold()


def title_base_key(title: str) -> str:
    """`build_search_title` 的基礎名（去季別、有偵測到季別就重組回中文「第N季」）→
    `normalize_title_key`，當比對的鍵。`store/youranimes_cache.py` 存 `zh_title_base`
    欄位時也是用這支——兩邊用同一份正規化，鍵才會一致。"""
    base, _season = build_search_title((title or "").strip())
    return normalize_title_key(base)

# 純數字季別（動畫瘋偶爾這樣寫，例如「GRAND BLUE 碧藍之海 3」）——只在這裡（youranimes
# 比對）額外辨識，刻意不動 `gossip_watch.season_number()`（公告標題比對也用那支，怕誤傷）。
_BARE_DIGIT_SEASON_RE = re.compile(r"(?<![A-Za-z0-9])([2-9]|1[0-9])\s*$")
# 前綴比對用：標題第一個空白（含全形空白）前的片段
_FIRST_SEGMENT_RE = re.compile(r"[\s　]+")
# 後段驗證用：把「前綴之後」的殘餘文字切成詞，標點/空白都當分隔
_WORD_SPLIT_RE = re.compile(r"[\s　！？!?。，,、～~：:「」『』（）()]+")


def _bare_digit_season(title: str) -> tuple[str, int] | None:
    """「XXX 3」→ (「XXX」, 3)；`season_number()` 已經認得出明確季別標記的話回 None
    （避免跟第 1／2 層重複處理，或誤把「第 3 季」的「3」當第二次辨識）。"""
    if season_number(title) is not None:
        return None
    m = _BARE_DIGIT_SEASON_RE.search(title.strip())
    if not m:
        return None
    return title[: m.start()].rstrip(), int(m.group(1))


def _candidate_keys(name: str) -> list[tuple[str, int | None, bool]]:
    """回傳依序嘗試的 `(比對鍵, 季別, 是否略過季別檢查)`，重複的鍵自動略過。"""
    candidates: list[tuple[str, int | None, bool]] = []
    seen: set[str] = set()

    def _add(key: str, season: int | None, skip_check: bool) -> None:
        if key and key not in seen:
            seen.add(key)
            candidates.append((key, season, skip_check))

    _base, season = build_search_title(name)
    _add(title_base_key(name), season, False)

    bare = _bare_digit_season(name)
    if bare is not None:
        base_no_season, n = bare
        zh = season_number_to_zh(n)
        if zh:
            _add(title_base_key(f"{base_no_season} 第{zh}季"), n, False)

    stripped_key = normalize_title_key(strip_season_suffix(name))
    _add(stripped_key, None, True)

    return candidates


def _first_segment(title: str) -> tuple[str, str]:
    """標題依第一個空白切成 (前段, 後段)；沒有空白就整串當前段、後段是空字串。"""
    parts = _FIRST_SEGMENT_RE.split(title.strip(), maxsplit=1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return title.strip(), ""


def _prefix_match(name: str, prefix_lookup: _Lookup) -> YourAnimesRecord | None:
    """標題被其中一邊截短／多寫副標題時的容錯（見模組說明第 4 層）。前綴命中多筆時先靠
    後段字詞篩，篩完還是不只一筆（例如同一季拆好幾個「篇」的作品，各篇標題都帶「第N季」，
    後段字詞篩不掉彼此）就比照精確比對那幾層的作法：挑季度頁最新的那筆。"""
    front, rest = _first_segment(name)
    if len(front) < 2:  # 太短的前綴容易配到不相干的番劇，不試
        return None
    candidates = prefix_lookup(normalize_title_key(front))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    words = [w for w in _WORD_SPLIT_RE.split(rest) if len(w) >= 2]
    scored = [c for c in candidates if any(w in c.zh_title for w in words)] if words else []
    if len(scored) == 1:
        return scored[0]
    return max(scored or candidates, key=lambda r: r.season_slug)


def match_record(
    bahamut_titles: Sequence[str],
    lookup: _Lookup,
    prefix_lookup: _Lookup | None = None,
) -> YourAnimesRecord | None:
    for name in bahamut_titles:
        name = (name or "").strip()
        if not name:
            continue
        for key, season, skip_check in _candidate_keys(name):
            candidates = lookup(key)
            hits = (
                candidates
                if skip_check
                else [r for r in candidates if season_compatible(season, r.season_number)]
            )
            if len(hits) == 1:
                return hits[0]
            if len(hits) > 1:
                exact = [r for r in hits if name in (r.zh_title.strip(), r.jp_title.strip())]
                if len(exact) == 1:
                    return exact[0]
                # 還是分不出來：挑季度頁最新的那筆（slug 字串比大小即可，YYYYMM 遞增）
                return max(hits, key=lambda r: r.season_slug)
        if prefix_lookup is not None:
            hit = _prefix_match(name, prefix_lookup)
            if hit is not None:
                return hit
    return None
