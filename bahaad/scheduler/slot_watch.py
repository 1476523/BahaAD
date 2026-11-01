"""訂閱時間監視：隨時核對「訂閱的更新時間」跟「網站實際上架時間」，不一致就校正。

起因（使用者 2026-10-18）：《小狸貓和小狐狸》網站週表寫週日 06:30，第 2、3 集也都是 06:30 上架，但
訂閱時間是週日 08:30——因為第 1 集（首播）上架時間是 08:30，首播時間常常跟之後每週固定的更新時間不同。
程式只在訂閱時間才去檢查，所以每週都晚兩小時才下載，使用者以為「到更新時間卻沒有下載」。

做法：每次排程檢查（`MainLoop.check_and_download`）看到的**最新一集**，把它的實際上架時段（星期＋時間）記下來
（每個訂閱留最近 3 筆，存在設定裡、重啟不會掉），然後依下面兩條規則決定要不要校正：

1. **連續兩集**（不含第 1 集，因為首播時間不具代表性）的實際上架時段相同、而且跟訂閱時間不一樣；
2. **最新一集的實際上架時段跟網站週表的時段相同**、但跟訂閱時間不一樣（兩個獨立來源互相印證，
   一集就夠，所以第 2 集上架那週就會校正，不用等到第 3 集）。

單獨一集的上架時間不會觸發校正——公告「本週延後更新」這類一次性的調整不能被當成新的固定時間。
校正只改「星期＋時間」，分類／模式／更名全部沿用；日誌會寫清楚由什麼改成什麼、依據是什麼。
使用者想固定自己選的時間（刻意晚一點下載）可以把設定 `auto_correct_schedule_time` 關掉。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

logger = logging.getLogger(__name__)

SETTINGS_KEY_ENABLED = "auto_correct_schedule_time"
DEFAULT_ENABLED = True
_OBS_KEY_PREFIX = "_slot_obs:"  # 前綴底線＝執行期狀態（跟 `_gamer_login_stale` 同慣例）
_KEEP_OBSERVATIONS = 3

_WEEKDAY_NAMES = {1: "週一", 2: "週二", 3: "週三", 4: "週四", 5: "週五", 6: "週六", 7: "週日"}
_UP_TIME_RE = re.compile(r"^\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})\s+(\d{1,2}):(\d{2})")

Slot = tuple[int, int, int]  # (星期 1=週一…7=週日, 時, 分)


def parse_up_time(raw: str) -> Slot | None:
    """`"2026/10/18 06:30"` → `(7, 6, 30)`；格式不對（站上沒給時間）回 None。"""
    from datetime import date

    match = _UP_TIME_RE.match(raw or "")
    if not match:
        return None
    try:
        day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        hour, minute = int(match.group(4)), int(match.group(5))
    except ValueError:
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return day.isoweekday(), hour, minute


def format_slot(slot: Slot) -> str:
    return f"{_WEEKDAY_NAMES.get(slot[0], '?')} {slot[1]:02d}:{slot[2]:02d}"


def decide_correction(entry_slot: Slot, observed: list[Slot], weekly_slot: Slot | None) -> tuple[Slot, str] | None:
    """純邏輯：回 `(新時段, 依據說明)`，不需要校正回 None。

    `observed`：最近幾集（舊→新，不含第 1 集）的實際上架時段；`weekly_slot`：網站週表目前這部番劇的時段
    （查不到就 None）。規則見模組說明。"""
    if not observed:
        return None
    latest = observed[-1]
    if weekly_slot is not None and latest == weekly_slot and latest != entry_slot:
        return latest, "最新一集的實際上架時間與網站週表一致"
    if len(observed) >= 2 and observed[-1] == observed[-2] and latest != entry_slot:
        return latest, "最近兩集的實際上架時間相同"
    return None


class SlotWatcher:
    """把觀察紀錄存在設定裡、套用 `decide_correction`、校正時改排程清單。"""

    def __init__(
        self,
        settings,
        schedule_store,
        *,
        weekly_slot_for: Callable[[set[int]], Slot | None] | None = None,
        on_corrected: Callable[[Any, Slot, Slot, str], None] | None = None,
    ) -> None:
        self._settings = settings
        self._schedule_store = schedule_store
        self._weekly_slot_for = weekly_slot_for
        self._on_corrected = on_corrected

    def _load(self, entry_sn: int) -> list[list[int]]:
        raw = self._settings.get(_OBS_KEY_PREFIX + str(entry_sn), [])
        return [list(x) for x in raw if isinstance(x, (list, tuple)) and len(x) == 4] if isinstance(raw, list) else []

    def observe(self, entry, videos, label: str = "") -> Slot | None:
        """看這輪檢查拿到的集數，記下最新一集的實際上架時段並評估校正。回傳校正後的新時段（沒校正回 None）。
        任何例外都吞掉——監視是附加功能，絕不能影響下載。"""
        try:
            return self._observe(entry, videos, label)
        except Exception:  # noqa: BLE001
            logger.debug("訂閱時間監視失敗（不影響下載）", exc_info=True)
            return None

    def _observe(self, entry, videos, label: str) -> Slot | None:
        if entry.schedule_weekday is None or entry.schedule_hour is None or entry.schedule_minute is None:
            return None
        if not self._settings.get(SETTINGS_KEY_ENABLED, DEFAULT_ENABLED):
            return None
        videos = [v for v in videos or [] if getattr(v, "up_time", "")]
        if not videos:
            return None
        latest = max(videos, key=lambda v: getattr(v, "episode_index", 0))
        # 第 1 集（首播）的上架時間不具代表性；沒有集數順序資訊的（電影等）也略過
        if getattr(latest, "episode_index", 0) < 1:
            return None
        slot = parse_up_time(latest.up_time)
        if slot is None:
            return None

        key = _OBS_KEY_PREFIX + str(entry.sn)
        obs = [o for o in self._load(entry.sn) if o[0] != latest.video_sn]
        obs.append([latest.video_sn, slot[0], slot[1], slot[2]])
        obs.sort(key=lambda o: o[0])
        obs = obs[-_KEEP_OBSERVATIONS:]
        self._settings.update({key: obs})

        weekly = None
        if self._weekly_slot_for is not None:
            members = {ep.video_sn for v in videos for ep in (getattr(v, "episodes", ()) or ())}
            members |= {v.video_sn for v in videos} | {entry.sn}
            weekly = self._weekly_slot_for(members)
        entry_slot: Slot = (int(entry.schedule_weekday), int(entry.schedule_hour), int(entry.schedule_minute))
        decision = decide_correction(entry_slot, [(o[1], o[2], o[3]) for o in obs], weekly)
        if decision is None:
            return None
        new_slot, why = decision

        from bahaad.subscription_ops import subscribe_sn

        subscribe_sn(self._schedule_store, entry.sn, new_slot[0], new_slot[1], new_slot[2])
        self._settings.update({key: []})  # 校正完重新累積，避免同一批觀察重複觸發
        logger.info(
            "%s 訂閱時間自動校正：%s → %s（%s）", label or f"sn {entry.sn}", format_slot(entry_slot), format_slot(new_slot), why
        )
        if self._on_corrected is not None:
            try:
                self._on_corrected(entry, entry_slot, new_slot, why)
            except Exception:  # noqa: BLE001
                logger.debug("訂閱時間校正通知失敗", exc_info=True)
        return new_slot
