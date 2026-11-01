"""簽到頁面的靜態資源解析：獎勵格子、站方說明文字、變更偵測。

簽到對話框（那個 7 欄格子、紅字「須完成手機認證…」、「補簽申請」連結）是站方前端**預編譯的
nunjucks 範本**（`signin.min.js`），不是 API 回傳的——所以這些文字抓得到，也就能在每次簽到時
比對有沒有改動。範本裡獎勵是寫死的規則：預設每格 `baseBonusAward`（100 巴幣）、第 7／14／21 格
500、第 28 格「勳章」，循環長度 `totalWeeks * 7`（預設 4 週＝28 格）。

今天是哪一格：簽到後 API 回的累計天數 `days`，`days % 循環長度`（餘 0 算最後一格）。

變更偵測：三支檔（`signin.js`、`signin_ad.js`、範本）各存雜湊；說明文字另外抽出來存，
改動時能直接告訴使用者「舊 → 新」。站方改了簽到流程（程式雜湊變了）也會提醒，比等到簽到
壞掉才發現早。抓不到（網路、站方改檔名）一律靜默略過，不影響簽到。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

SIGNIN_JS_URL = "https://i2.bahamut.com.tw/js/signin.js"
SIGNIN_AD_JS_URL = "https://i2.bahamut.com.tw/js/signin_ad.js"
TEMPLATE_URL = "https://i2.bahamut.com.tw/build/template/signin.min.js"

# 抓不到範本時的後備值（來源：2026-10 的 signin.min.js）
_DEFAULT_BASE_AWARD = "100"
_DEFAULT_AWARDS = {7: "500", 14: "500", 21: "500", 28: "勳章"}
_DEFAULT_WEEKS = 4

_UNICODE_ESCAPE = re.compile(r"\\u([0-9A-Fa-f]{4})")
_TAGS = re.compile(r"<[^>]+>")


def _decode(js: str) -> str:
    return _UNICODE_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), js)


def _plain(html: str) -> str:
    text = _TAGS.sub(" ", html).replace("&nbsp", " ").replace(";", "")
    return re.sub(r"\s+", " ", text).strip()


def parse_template(template_js: str) -> dict[str, Any]:
    """從 `signin.min.js` 抽出獎勵規則與說明文字。抽不到的欄位用後備值，不拋例外。"""
    text = _decode(template_js)
    base = re.search(r':"(\d+)",o\.set\("baseBonusAward"', text)
    weeks = re.search(r":(\d+),o\.set\(\"totalWeeks\"", text)
    awards: dict[int, str] = {}
    block = re.search(r"\{([^{}]*)\},o\.set\(\"defaultBonusAward\"", text)
    if block:
        for day, award in re.findall(r'(\d+):"([^"]*)"', block.group(1)):
            awards[int(day)] = award
    red = re.search(r'color:\s*red[^>]*>(.*?)</div>', text, re.S)
    links = re.search(r'popup-dailybox__fulilink">(.*?)</p>', text, re.S)
    subtitle = re.search(r'popup-dailybox__badge">(.*?)<img', text, re.S)
    return {
        "base_award": base.group(1) if base else _DEFAULT_BASE_AWARD,
        "awards": awards or dict(_DEFAULT_AWARDS),
        "weeks": int(weeks.group(1)) if weeks else _DEFAULT_WEEKS,
        "notice": _plain(red.group(1)) if red else "",
        "links": _plain(links.group(1)) if links else "",
        "badge_hint": _plain(subtitle.group(1)) if subtitle else "",
    }


def default_rules() -> dict[str, Any]:
    return {
        "base_award": _DEFAULT_BASE_AWARD, "awards": dict(_DEFAULT_AWARDS),
        "weeks": _DEFAULT_WEEKS, "notice": "", "links": "", "badge_hint": "",
    }


def reward_for(days: Any, weeks: Any, rules: dict[str, Any]) -> tuple[int, str, int] | None:
    """`(今天是第幾格, 獎勵名稱, 預期巴幣)`；累計天數未知回 `None`。獎勵是「勳章」這類非
    數字時預期巴幣為 0。"""
    try:
        total = int(days)
        cycle = int(weeks or rules.get("weeks") or _DEFAULT_WEEKS) * 7
    except (TypeError, ValueError):
        return None
    if total <= 0 or cycle <= 0:
        return None
    cell = total % cycle or cycle
    award = str(rules.get("awards", {}).get(cell, rules.get("base_award", _DEFAULT_BASE_AWARD)))
    if award.isdigit():
        return cell, f"{award} 巴幣", int(award)
    return cell, award, 0


_BR = re.compile(r"<\s*br\s*/?\s*>", re.I)
_MESSAGE_REWARD = re.compile(r"簽到\s*\+\s*(.+)")


def reward_from_message(text: str) -> str | None:
    """簽到成功對話框的說明（例如「連續 28 天簽到 + 500 巴幣 & 勳章<br>（斷簽會回收勳章！）…」）裡
    「簽到 + 」後面那段就是站方當天實際給的獎勵；用它比範本規則可靠（站方改獎勵時會自動跟上）。
    抓不到回 `None`，由呼叫端退回範本規則。"""
    first_line = _BR.split(text or "")[0]
    found = _MESSAGE_REWARD.search(_TAGS.sub(" ", first_line))
    reward = re.sub(r"\s+", " ", found.group(1)).strip() if found else ""
    return reward or None


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


_TEXT_FIELDS = (
    ("notice", "紅字說明"),
    ("links", "底部說明／連結"),
    ("badge_hint", "週年勳章說明"),
)


def diff_state(old: dict[str, Any] | None, new: dict[str, Any]) -> list[str]:
    """比對前後兩次的 `{"hashes": {...}, "texts": {...}}`，回傳人看得懂的變更說明；第一次
    （`old` 為空）或沒變回空清單。"""
    if not old:
        return []
    notes: list[str] = []
    old_texts, new_texts = old.get("texts") or {}, new.get("texts") or {}
    for key, label in _TEXT_FIELDS:
        before, after = old_texts.get(key, ""), new_texts.get(key, "")
        if before != after and (before or after):
            notes.append(f"站方{label}有更新：「{before or '（無）'}」→「{after or '（無）'}」")
    if old_texts.get("awards") != new_texts.get("awards"):
        notes.append(f"站方獎勵規則有更新：{old_texts.get('awards')} → {new_texts.get('awards')}")
    old_hashes, new_hashes = old.get("hashes") or {}, new.get("hashes") or {}
    text_changed = bool(notes)
    for name, digest in new_hashes.items():
        if name in old_hashes and old_hashes[name] != digest:
            if name == "template" and text_changed:
                continue  # 已經用文字差異說明了
            notes.append(f"站方簽到頁面程式（{name}）有更新，流程可能改變，請留意簽到是否仍正常")
    return notes
