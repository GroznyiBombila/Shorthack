"""In-memory schedule store.

Parses every .xls in a directory once at startup and answers lookups by
group or teacher from memory. No network calls, no language model —
the schedule is a table, and a query against a table is just a filter.
"""

from __future__ import annotations

import datetime
import glob
import os
import re

from app.schedule_parser import institute_code_from_filename, parse_schedule_file

_SCHEDULES: dict[str, dict] = {}  # institute code -> parsed file dict


def _semester_start() -> datetime.date:
    raw = os.environ.get("SEMESTER_START")
    if not raw:
        raise RuntimeError(
            "SEMESTER_START is not set (expected an ISO date, e.g. '2026-09-01')"
        )
    return datetime.date.fromisoformat(raw)


def week_parity(date: datetime.date) -> str:
    """'upper' | 'lower', relative to SEMESTER_START."""
    weeks = (date - _semester_start()).days // 7
    return "upper" if weeks % 2 == 0 else "lower"


def load_all(directory: str = "data/schedule") -> None:
    """Parses all .xls in `directory` and holds the result in memory."""
    _SCHEDULES.clear()
    for path in sorted(glob.glob(os.path.join(directory, "*.xls"))):
        code = institute_code_from_filename(path) or os.path.splitext(os.path.basename(path))[0]
        try:
            _SCHEDULES[code] = parse_schedule_file(path, code)
        except Exception as exc:  # noqa: BLE001 - one bad file shouldn't kill startup
            print(f"[schedule] failed to parse {path}: {exc}")


def list_institutes() -> list[str]:
    return sorted(_SCHEDULES.keys())


def list_groups(institute: str | None = None) -> list[str]:
    names: set[str] = set()
    for code, data in _SCHEDULES.items():
        if institute is not None and code != institute:
            continue
        names.update(data.get("groups", {}).keys())
    return sorted(names)


def for_group(group: str, date: datetime.date, subgroup: int | None = None) -> list[dict]:
    """Lessons for `group` on `date`, sorted by pair number.

    Accounts for week parity and subgroup. An empty list means a day off
    or a window — not an error.
    """
    day_index = date.weekday()  # Monday=0 .. Sunday=6, same as DAYS
    week = week_parity(date)
    result = []
    for data in _SCHEDULES.values():
        group_data = data.get("groups", {}).get(group)
        if not group_data:
            continue
        for lesson in group_data.get("lessons", []):
            if lesson["day_index"] != day_index or lesson["week"] != week:
                continue
            if subgroup is not None and lesson["subgroup"] is not None and lesson["subgroup"] != subgroup:
                continue
            result.append(lesson)
    result.sort(key=lambda lesson: lesson["pair"])
    return result


def _normalize_teacher(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def for_teacher(teacher: str, date: datetime.date) -> list[dict]:
    """Lessons for `teacher` on `date`, across all institutes and groups.

    Matching on surname is loose: case and extra whitespace are ignored.
    """
    day_index = date.weekday()
    week = week_parity(date)
    target = _normalize_teacher(teacher)
    result = []
    for data in _SCHEDULES.values():
        for group_name, group_data in data.get("groups", {}).items():
            for lesson in group_data.get("lessons", []):
                if lesson["day_index"] != day_index or lesson["week"] != week:
                    continue
                lesson_teacher = lesson.get("teacher")
                if lesson_teacher and _normalize_teacher(lesson_teacher) == target:
                    entry = dict(lesson)
                    entry["group"] = group_name
                    result.append(entry)
    result.sort(key=lambda lesson: lesson["pair"])
    return result
