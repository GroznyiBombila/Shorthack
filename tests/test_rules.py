"""Тесты app/rules.py — чистые функции, без сети и без модели."""
from __future__ import annotations

from app import rules


def test_missing_fields_returns_all_when_nothing_known():
    missing = rules.missing_fields("documents", {})
    assert set(missing) == {"ФИО", "вид справки", "куда предоставляется"}


def test_missing_fields_returns_empty_when_all_known():
    known = {"ФИО": "Иванов И.И.", "логин": "ivanov", "описание ошибки": "не входит"}
    assert rules.missing_fields("account", known) == []


def test_missing_fields_unknown_category_falls_back_to_other():
    assert rules.missing_fields("unknown_category", {}) == rules.REQUIRED_FIELDS["other"]


def test_priority_irritated_tone_raises_by_one_step_only():
    calm = rules.priority("other", "neutral", [])
    irritated = rules.priority("other", "irritated", [])
    order = ["P4", "P3", "P2", "P1"]
    assert order.index(irritated) == order.index(calm) + 1


def test_priority_irritated_never_turns_p4_into_p1():
    # "other" без маркеров блокировки/срочности -> P4 в спокойном тоне.
    assert rules.priority("other", "neutral", []) == "P4"
    assert rules.priority("other", "irritated", []) == "P3"


def test_priority_blocking_plus_deadline_is_p1():
    assert rules.priority("account", "neutral", ["не могу войти", "сессия", "сегодня"]) == "P1"


def test_priority_blocking_only_is_p2():
    assert rules.priority("account", "neutral", ["не работает личный кабинет"]) == "P2"


def test_priority_regular_question_is_p3():
    assert rules.priority("study", "neutral", ["расписание"]) == "P3"


def test_is_sufficient_false_when_nothing_found():
    assert rules.is_sufficient({"confidence": 0.9}, []) is False


def test_is_sufficient_false_on_low_confidence():
    found = [{"id": "x", "score": 5.0}]
    assert rules.is_sufficient({"confidence": 0.1}, found) is False


def test_is_sufficient_true_on_good_match():
    found = [{"id": "x", "score": 5.0}]
    assert rules.is_sufficient({"confidence": 0.8}, found) is True


def test_route_reason_is_short_readable_string():
    reason = rules.route_reason("off_topic->reject")
    assert isinstance(reason, str) and reason
