"""排程清單（追蹤項目）的持久化與文字格式匯入/匯出。

規格見 docs/requirements/schedule_list.md——文字語法是獨立設計的（key=value 屬性），
不是沿用舊專案的 <rename>/*weekday*/$time$ 符號語法。資料庫是權威來源，文字格式只是
使用者編輯用的匯入/匯出介面。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Optional

from bahaad.store.database import Database

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schedule_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sort_order INTEGER NOT NULL,
    line_type TEXT NOT NULL,     -- 'tag' / 'entry' / 'comment' / 'blank' / 'raw'
    sn INTEGER,
    tag TEXT,
    mode TEXT,
    rename TEXT,
    schedule_weekday INTEGER,
    schedule_hour INTEGER,
    schedule_minute INTEGER,
    text TEXT                    -- comment/raw 行的原始文字
)
"""

_WEEKDAY_CODES = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 7}
_WEEKDAY_NAMES = {v: k for k, v in _WEEKDAY_CODES.items()}

_ATTR_PATTERN = re.compile(r'(\w+)=("[^"]*"|\S+)')
_SN_PATTERN = re.compile(r"^(\d+)\s*(.*)$")
_SCHEDULE_PATTERN = re.compile(r"^(mon|tue|wed|thu|fri|sat|sun):(\d{1,2}):(\d{2})$")


@dataclass
class ScheduleEntry:
    sn: int
    tag: Optional[str] = None
    mode: Optional[str] = None
    rename: Optional[str] = None
    schedule_weekday: Optional[int] = None
    schedule_hour: Optional[int] = None
    schedule_minute: Optional[int] = None


def schedule_sort_key(entry: ScheduleEntry) -> tuple[int, int, int, int]:
    """排程清單／訂閱列表照「播出時段」排：星期（1=一…7=日）→ 時 → 分 → sn。
    沒有自訂時段的項目（退訂後保留設定那種）排最後（使用者 2026-09-09）。"""
    if entry.schedule_weekday is None:
        return (99, 0, 0, entry.sn)
    return (entry.schedule_weekday, entry.schedule_hour or 0, entry.schedule_minute or 0, entry.sn)


@dataclass
class _Line:
    line_type: str
    sn: Optional[int] = None
    tag: Optional[str] = None
    mode: Optional[str] = None
    rename: Optional[str] = None
    schedule_weekday: Optional[int] = None
    schedule_hour: Optional[int] = None
    schedule_minute: Optional[int] = None
    text: Optional[str] = None


def parse_text(content: str) -> list[_Line]:
    """把文字格式解析成結構化的行清單。無法辨識的行保留成 raw 類型，不遺失使用者內容。"""
    lines: list[_Line] = []
    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            lines.append(_Line(line_type="blank"))
            continue
        if stripped.startswith("#"):
            lines.append(_Line(line_type="comment", text=stripped[1:].strip()))
            continue
        if stripped.startswith("@"):
            lines.append(_Line(line_type="tag", tag=stripped[1:].strip()))
            continue

        sn_match = _SN_PATTERN.match(stripped)
        if not sn_match:
            lines.append(_Line(line_type="raw", text=raw_line))
            continue

        sn = int(sn_match.group(1))
        attrs_text = sn_match.group(2)
        attrs = {
            key: value[1:-1] if value.startswith('"') and value.endswith('"') else value
            for key, value in _ATTR_PATTERN.findall(attrs_text)
        }

        schedule_weekday = schedule_hour = schedule_minute = None
        schedule_raw = attrs.get("schedule")
        if schedule_raw:
            schedule_match = _SCHEDULE_PATTERN.match(schedule_raw)
            if schedule_match:
                schedule_weekday = _WEEKDAY_CODES[schedule_match.group(1)]
                schedule_hour = int(schedule_match.group(2))
                schedule_minute = int(schedule_match.group(3))

        lines.append(
            _Line(
                line_type="entry",
                sn=sn,
                mode=attrs.get("mode"),
                rename=attrs.get("rename"),
                schedule_weekday=schedule_weekday,
                schedule_hour=schedule_hour,
                schedule_minute=schedule_minute,
            )
        )
    return lines


def render_text(lines: list[_Line]) -> str:
    """把結構化的行清單組回文字格式，是 parse_text() 的反向操作。"""
    out: list[str] = []
    for line in lines:
        if line.line_type == "blank":
            out.append("")
        elif line.line_type == "comment":
            out.append("# " + (line.text or ""))
        elif line.line_type == "tag":
            out.append("@" + (line.tag or ""))
        elif line.line_type == "raw":
            out.append(line.text or "")
        elif line.line_type == "entry":
            parts = [str(line.sn)]
            if line.mode:
                parts.append(f"mode={line.mode}")
            if line.rename:
                parts.append(f'rename="{line.rename}"')
            if line.schedule_weekday and line.schedule_hour is not None:
                weekday_code = _WEEKDAY_NAMES[line.schedule_weekday]
                parts.append(f"schedule={weekday_code}:{line.schedule_hour:02d}:{line.schedule_minute:02d}")
            out.append(" ".join(parts))
    return "\n".join(out)


# 分類（@tag 行）不是 entry 自己的欄位，是「這一行前面最近一個 @tag 行是誰」這種位置
# 關係決定的。所以「改一個項目的分類」＝把這一行搬到對應 @tag 群組底下。這三個 helper
# 原本在 web/schedule.py，2026-08-27 搬來這裡——排程文字操作、不是 web 專屬，scheduler
# 的 subscription_ops.py 也要用。
def _tag_before(lines: list[_Line], index: int) -> str | None:
    for i in range(index - 1, -1, -1):
        if lines[i].line_type == "tag":
            return lines[i].tag
    return None


def _move_entry_into_tag_group(lines: list[_Line], new_line: _Line, tag: str | None) -> list[_Line]:
    """把 new_line 這個 entry 行插入到指定分類的群組裡（緊接在該 @tag 群組最後一項
    之後）。沒有指定分類的話插到最前面，確保解析時 current_tag 還是 None。分類不存在
    的話新增一個 @tag 行到最後，再接上這個 entry。"""
    if not tag:
        return [new_line] + list(lines)

    lines = list(lines)
    tag_index = next(
        (i for i, line in enumerate(lines) if line.line_type == "tag" and line.tag == tag), None
    )
    if tag_index is None:
        lines.append(_Line(line_type="tag", tag=tag))
        lines.append(new_line)
        return lines

    insert_at = len(lines)
    for i in range(tag_index + 1, len(lines)):
        if lines[i].line_type == "tag":
            insert_at = i
            break
    lines.insert(insert_at, new_line)
    return lines


def _upsert_entry(
    lines: list[_Line],
    sn: int,
    tag: str | None,
    mode: str | None,
    rename: str | None,
    schedule_weekday: int | None,
    schedule_hour: int | None,
    schedule_minute: int | None,
) -> list[_Line]:
    new_line = _Line(
        line_type="entry",
        sn=sn,
        mode=mode,
        rename=rename,
        schedule_weekday=schedule_weekday,
        schedule_hour=schedule_hour,
        schedule_minute=schedule_minute,
    )

    existing_index = next(
        (i for i, line in enumerate(lines) if line.line_type == "entry" and line.sn == sn), None
    )
    if existing_index is None:
        return _move_entry_into_tag_group(lines, new_line, tag)

    if _tag_before(lines, existing_index) == tag:
        # 分類沒變，原地更新內容，不搬動這一行在清單裡的位置
        lines = list(lines)
        lines[existing_index] = new_line
        return lines

    # 分類變了，先把舊的那一行拿掉，才能依新分類重新定位
    remaining = [line for i, line in enumerate(lines) if i != existing_index]
    return _move_entry_into_tag_group(remaining, new_line, tag)


class ScheduleListStore:
    def __init__(self, database: Database) -> None:
        self._database = database
        self._lock = threading.Lock()
        with self._database.transaction() as conn:
            conn.execute(_TABLE_SQL)

    def get_entries(self) -> dict[int, ScheduleEntry]:
        """回傳 {sn: ScheduleEntry}，只包含 entry 類型的行(略過 tag/comment/blank/raw)。"""
        result: dict[int, ScheduleEntry] = {}
        current_tag: Optional[str] = None
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM schedule_entries ORDER BY sort_order"
            ).fetchall()
        for row in rows:
            if row["line_type"] == "tag":
                current_tag = row["tag"]
                continue
            if row["line_type"] != "entry":
                continue
            result[row["sn"]] = ScheduleEntry(
                sn=row["sn"],
                tag=current_tag,
                mode=row["mode"],
                rename=row["rename"],
                schedule_weekday=row["schedule_weekday"],
                schedule_hour=row["schedule_hour"],
                schedule_minute=row["schedule_minute"],
            )
        return result

    def get_raw_text(self) -> str:
        with self._database.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM schedule_entries ORDER BY sort_order"
            ).fetchall()
        lines = [
            _Line(
                line_type=row["line_type"],
                sn=row["sn"],
                tag=row["tag"],
                mode=row["mode"],
                rename=row["rename"],
                schedule_weekday=row["schedule_weekday"],
                schedule_hour=row["schedule_hour"],
                schedule_minute=row["schedule_minute"],
                text=row["text"],
            )
            for row in rows
        ]
        return render_text(lines)

    def replace_from_text(self, content: str) -> None:
        """用一份新的文字內容整批取代目前的排程清單(使用者在編輯器裡按下儲存時呼叫)。"""
        lines = parse_text(content)
        with self._lock:
            with self._database.transaction() as conn:
                conn.execute("DELETE FROM schedule_entries")
                for index, line in enumerate(lines):
                    conn.execute(
                        "INSERT INTO schedule_entries "
                        "(sort_order, line_type, sn, tag, mode, rename, "
                        " schedule_weekday, schedule_hour, schedule_minute, text) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            index,
                            line.line_type,
                            line.sn,
                            line.tag,
                            line.mode,
                            line.rename,
                            line.schedule_weekday,
                            line.schedule_hour,
                            line.schedule_minute,
                            line.text,
                        ),
                    )
