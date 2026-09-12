"""Тесты app/profile.py — без сети и без модели, своя изолированная база."""
from __future__ import annotations

import pytest

from app import config, profile


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "app.db")
    yield


def test_required_fields_student_has_group_and_subgroup():
    names = {f["name"] for f in profile.required_fields("student")}
    assert names == {"institute", "group", "subgroup"}


def test_required_fields_teacher_has_department_and_name():
    names = {f["name"] for f in profile.required_fields("teacher")}
    assert names == {"department", "name"}


def test_required_fields_applicant_is_empty():
    assert profile.required_fields("applicant") == []


def test_save_and_get_roundtrip():
    saved = profile.save_profile("student@misis.ru", "student",
                                  {"institute": "ИТКН", "group": "ББИ-26-6-1"})
    assert saved == {"institute": "ИТКН", "group": "ББИ-26-6-1"}
    assert profile.get_profile("student@misis.ru") == saved


def test_get_unknown_email_returns_none():
    assert profile.get_profile("nobody@misis.ru") is None


def test_save_drops_fields_not_allowed_for_role():
    # "department" не относится к студенту — не должно осесть в базе.
    saved = profile.save_profile("s2@misis.ru", "student",
                                  {"group": "ББИ-26-6-1", "department": "ИТКН"})
    assert saved == {"group": "ББИ-26-6-1"}


def test_save_overwrites_previous_value():
    profile.save_profile("s3@misis.ru", "student", {"group": "А"})
    saved = profile.save_profile("s3@misis.ru", "student", {"group": "Б"})
    assert saved == {"group": "Б"}
    assert profile.get_profile("s3@misis.ru") == {"group": "Б"}
