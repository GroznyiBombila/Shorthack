"""Parses MISIS-style .xls schedule files into a plain dict structure.

One .xls file = one institute (filename: ``{institute_code}-{ddmmyy}.xls``).
Each sheet in the workbook is one course level. The layout is fixed:

* Row 0: ``Дата | Номер | Время | <group name>...`` — one group occupies
  4 columns, named in a cell merged across rows 0-1.
* Row 1: subgroup numbers (1 / 2) sit at offsets +0 and +2 within a group.
* Rows 2..99: the body, 7 days x 14 rows (7 pairs x 2 rows/pair). The first
  row of a pair is the "upper" (numerator) week, the second is "lower"
  (denominator). Day name / pair number / time are only stamped on the
  first row of their block and must be carried forward.

This module never touches the network and never calls a language model —
the schedule is a table, so a deterministic parser is what belongs here.
It also never raises on unexpected sheet content: on a parse failure for
one sheet, it logs and keeps whatever was already parsed from the others.
"""

from __future__ import annotations

import datetime
import os
import re
from typing import Optional

import xlrd

DAYS = [
    "Понедельник",
    "Вторник",
    "Среда",
    "Четверг",
    "Пятница",
    "Суббота",
    "Воскресенье",
]

KIND_MAP = {
    "Лекционные": "lecture",
    "Практические": "practice",
    "Лабораторные": "lab",
}

_HEADER_ROWS = 2
_ROWS_PER_PAIR = 2
_PAIRS_PER_DAY = 7
_GROUP_START_COL = 3
_COLS_PER_GROUP = 4

# Latin look-alikes that show up in room numbers (e.g. "K-527" with a
# Latin K) get folded onto their Cyrillic counterparts so the same room
# doesn't end up looking like two different rooms.
_LATIN_TO_CYRILLIC_ROOM = str.maketrans(
    {
        "A": "А", "E": "Е", "K": "К", "M": "М", "H": "Н",
        "O": "О", "P": "Р", "C": "С", "T": "Т", "X": "Х",
    }
)

_SUBGROUP_PREFIX_RE = re.compile(r"^\s*\d\s*,\s*\d\s*п\.?\s*г\.?\s+", re.IGNORECASE)
_SUBJECT_KIND_RE = re.compile(r"^(.*?)\s*\(([^()]+)\)\s*$", re.DOTALL)
_FILENAME_RE = re.compile(r"^([A-Za-z]+)-(\d{6})\.xls$", re.IGNORECASE)


def institute_code_from_filename(filename: str) -> Optional[str]:
    """'itkn-110926.xls' -> 'itkn'. Returns None if the name doesn't match."""
    match = _FILENAME_RE.match(os.path.basename(filename))
    return match.group(1) if match else None


def normalize_room(room) -> Optional[str]:
    """Normalizes a room string: folds look-alike Latin letters
    (A E K M H O P C T X) onto Cyrillic, collapses whitespace."""
    if room is None:
        return None
    room = str(room).strip()
    if not room:
        return None
    room = room.translate(_LATIN_TO_CYRILLIC_ROOM)
    return re.sub(r"\s+", " ", room)


def _course_and_level(sheet_name: str) -> tuple[Optional[int], str]:
    course_match = re.search(r"\d+", sheet_name)
    course = int(course_match.group()) if course_match else None
    level = "master" if "магистратура" in sheet_name.lower() else "bachelor"
    return course, level


def parse_lesson_cell(subject_cell, room_cell) -> Optional[dict]:
    """Parses one lesson cell pair.

    ``subject_cell`` looks like ``'Математика (Практические)\\nКим-Тян Л. Р.'``
    — the teacher line is optional (e.g. gym classes have none). A leading
    ``'1, 2 п.г. '`` prefix means the lesson is shared by both subgroups.
    Returns ``None`` if the cell is empty (a window in the schedule).
    """
    if subject_cell is None:
        return None
    text = str(subject_cell).strip()
    if not text:
        return None

    subgroup_override = False
    if _SUBGROUP_PREFIX_RE.match(text):
        text = _SUBGROUP_PREFIX_RE.sub("", text, count=1)
        subgroup_override = True

    lines = text.split("\n")
    first_line = lines[0].strip()
    teacher = lines[1].strip() if len(lines) > 1 and lines[1].strip() else None

    match = _SUBJECT_KIND_RE.match(first_line)
    if match:
        subject = match.group(1).strip()
        kind = KIND_MAP.get(match.group(2).strip(), "other")
    else:
        subject = first_line
        kind = "other"

    return {
        "subject": subject,
        "kind": kind,
        "teacher": teacher,
        "room": normalize_room(room_cell),
        "subgroup_override": subgroup_override,
    }


def _cell_str(sheet, row: int, col: int) -> Optional[str]:
    if row >= sheet.nrows or col >= sheet.ncols:
        return None
    value = sheet.cell_value(row, col)
    if value is None or value == "":
        return None
    return str(value)


def _time_str(cell_value) -> Optional[tuple[str, str]]:
    """``'09:00:00 - 10:35:00'`` -> ``('09:00', '10:35')``."""
    if not cell_value:
        return None
    parts = [p.strip() for p in str(cell_value).split("-")]
    if len(parts) != 2:
        return None

    def _hm(value: str) -> str:
        pieces = value.split(":")
        return f"{pieces[0]}:{pieces[1]}" if len(pieces) >= 2 else value

    return _hm(parts[0]), _hm(parts[1])


def _parse_group_column(sheet, group_col: int, lessons: list) -> None:
    nrows = sheet.nrows
    row = _HEADER_ROWS

    for day_index in range(len(DAYS)):
        for pair_index in range(_PAIRS_PER_DAY):
            top_row = row
            bottom_row = row + 1
            row += _ROWS_PER_PAIR
            if top_row >= nrows:
                continue

            pair_cell = _cell_str(sheet, top_row, 1)
            pair_number = pair_index + 1
            if pair_cell:
                try:
                    pair_number = int(float(pair_cell))
                except ValueError:
                    pass

            times = _time_str(_cell_str(sheet, top_row, 2))

            for sub_row, week in ((top_row, "upper"), (bottom_row, "lower")):
                if sub_row >= nrows:
                    continue
                override_added = False
                for subgroup_num, offset in ((1, 0), (2, 2)):
                    if override_added:
                        break
                    subject_col = group_col + offset
                    room_col = group_col + offset + 1
                    lesson = parse_lesson_cell(
                        _cell_str(sheet, sub_row, subject_col),
                        _cell_str(sheet, sub_row, room_col),
                    )
                    if lesson is None:
                        continue
                    is_override = lesson.pop("subgroup_override")
                    lessons.append(
                        {
                            "day_index": day_index,
                            "day": DAYS[day_index],
                            "pair": pair_number,
                            "time_start": times[0] if times else None,
                            "time_end": times[1] if times else None,
                            "week": week,
                            "subgroup": None if is_override else subgroup_num,
                            **lesson,
                        }
                    )
                    if is_override:
                        override_added = True


def _parse_sheet(sheet, course: Optional[int], level: str, groups: dict) -> None:
    group_col = _GROUP_START_COL
    while group_col < sheet.ncols:
        name = _cell_str(sheet, 0, group_col)
        if not name:
            group_col += _COLS_PER_GROUP
            continue
        group_name = name.strip()
        group = groups.setdefault(
            group_name, {"course": course, "level": level, "lessons": []}
        )
        _parse_group_column(sheet, group_col, group["lessons"])
        group_col += _COLS_PER_GROUP


def parse_schedule_file(path: str, institute: str) -> dict:
    """Parses one .xls schedule file.

    Returns ``{'institute', 'source_file', 'parsed_at', 'groups': {...}}``.
    A malformed sheet is logged and skipped rather than aborting the whole
    file — a partial result beats a crashed process.
    """
    book = xlrd.open_workbook(path)
    groups: dict[str, dict] = {}

    for sheet_name in book.sheet_names():
        try:
            sheet = book.sheet_by_name(sheet_name)
            course, level = _course_and_level(sheet_name)
            _parse_sheet(sheet, course, level, groups)
        except Exception as exc:  # noqa: BLE001 - keep parsing other sheets
            print(f"[schedule_parser] failed to parse sheet {sheet_name!r} in {path}: {exc}")

    return {
        "institute": institute,
        "source_file": os.path.basename(path),
        "parsed_at": datetime.datetime.utcnow().isoformat() + "Z",
        "groups": groups,
    }
