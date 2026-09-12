"""Маршрутизация адресата — чистый код, без сети и без LLM (см. app/routing.py)."""
from __future__ import annotations

from app import routing


def test_known_categories_map_to_fixed_recipients():
    assert routing.recipient("admission") == "Приёмная комиссия"
    assert routing.recipient("campus_life") == "Студгородок"
    assert routing.recipient("account") == "Техподдержка"
    assert routing.recipient("documents") == "Учебный отдел"


def test_study_without_institute_falls_back_to_common_department():
    assert routing.recipient("study") == "Учебный отдел"
    assert routing.recipient("study", institute=None) == "Учебный отдел"
    assert routing.recipient("study", institute="") == "Учебный отдел"


def test_study_with_known_institute_routes_to_its_dean_office():
    assert routing.recipient("study", institute="ИТКН") == "Деканат ИТКН"


def test_unknown_category_falls_back_to_general_support():
    assert routing.recipient("no_such_category") == "Поддержка МИСИС"
    assert routing.recipient("other") == "Поддержка МИСИС"


def test_institute_slash_is_stripped_to_keep_subject_parsing_safe():
    # "/" — служебный разделитель темы письма (см. _subject в app/api.py);
    # если институт его вдруг содержит, тема не должна ломаться при пересборке.
    assert "/" not in routing.recipient("study", institute="ИТКН/что-то")
