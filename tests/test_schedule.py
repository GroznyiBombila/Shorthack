import datetime
import os

import pytest

from app import schedule
from app.schedule_parser import normalize_room, parse_lesson_cell, parse_schedule_file

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "schedule", "itkn-110926.xls")


@pytest.fixture(scope="module")
def parsed():
    return parse_schedule_file(FIXTURE_PATH, "itkn")


def test_all_sheets_parsed_with_many_groups(parsed):
    # 6 sheets ('1 курс' .. '2 курс магистратура|СВО'), each contributing groups.
    assert len(parsed["groups"]) > 10


def test_group_monday_second_pair_upper_week(parsed):
    group = parsed["groups"]["ББИ-26-6-1"]
    lesson = next(
        l for l in group["lessons"]
        if l["day"] == "Понедельник" and l["pair"] == 2 and l["week"] == "upper"
    )
    assert lesson["subject"] == "Введение в специальность"
    assert lesson["kind"] == "lab"
    assert lesson["room"] == "Л-812-УВЦ"


def test_group_monday_second_pair_lower_week(parsed):
    group = parsed["groups"]["ББИ-26-6-1"]
    lesson = next(
        l for l in group["lessons"]
        if l["day"] == "Понедельник" and l["pair"] == 2 and l["week"] == "lower"
    )
    assert lesson["subject"] == "Математика"
    assert lesson["kind"] == "practice"


def test_parse_lesson_cell_strips_subgroup_prefix():
    lesson = parse_lesson_cell(
        "1, 2 п.г. Введение в специальность (Лабораторные)\nНеворошкин В. А.",
        "Л-812-УВЦ",
    )
    assert lesson["subject"] == "Введение в специальность"
    assert lesson["subgroup_override"] is True


def test_lesson_without_teacher_is_null(parsed):
    group = parsed["groups"]["ББИ-26-6-1"]
    gym_lessons = [l for l in group["lessons"] if l["subject"] == "Физическая культура и спорт"]
    assert gym_lessons, "expected at least one gym lesson in the fixture"
    assert gym_lessons[0]["teacher"] is None


def test_normalize_room_folds_latin_k_to_cyrillic():
    assert normalize_room("K-527") == normalize_room("К-527") == "К-527"


def test_week_parity_alternates(monkeypatch):
    monkeypatch.setenv("SEMESTER_START", "2026-09-07")  # a Monday
    week1 = schedule.week_parity(datetime.date(2026, 9, 8))
    week2 = schedule.week_parity(datetime.date(2026, 9, 15))
    assert week1 != week2


def test_for_group_sunday_is_empty_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv("SEMESTER_START", "2026-09-07")
    _load_fixture_into(schedule)
    sunday = datetime.date(2026, 9, 13)  # a Sunday within the semester window
    assert schedule.for_group("ББИ-26-6-1", sunday) == []


def test_for_teacher_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("SEMESTER_START", "2026-08-31")  # a Monday, so this Monday is 'upper'
    _load_fixture_into(schedule)
    monday = datetime.date(2026, 9, 7)  # a 'lower' week Monday given SEMESTER_START above
    lessons = schedule.for_teacher("ким-тян л. р.", monday)
    assert lessons
    assert all("математика" in l["subject"].lower() for l in lessons)


def _load_fixture_into(schedule_module) -> None:
    data_dir = os.path.join(os.path.dirname(__file__), "..", "data", "schedule")
    schedule_module.load_all(data_dir)
