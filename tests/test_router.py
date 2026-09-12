"""Тесты app/router.py — без сети, без модели. analysis передаётся как dict,
что и проверяет заявленный в докстрине decide протокол (dict или объект).

Роутер проверяем на подменённом поиске (fake_faq), а не на боевой базе знаний:
здесь важна развилка «нашлось / не нашлось», а не содержимое data/faq. Пока
тесты ссылались на id заглушек, первое же пополнение базы Стасей их уронило,
хотя роутер вёл себя правильно.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import faq, rules
from app.router import decide

# Две записи ровно для развилки роутера: по слову «стипендия» находится сильно,
# по всему остальному — не находится вовсе.
_HIT = {"id": "fx-001", "title": "Стипендии и выплаты",
        "category": "campus_life", "keywords": ["стипендия", "стипуха"]}


@pytest.fixture(autouse=True)
def fake_faq(monkeypatch):
    """Поиск отвечает по одному слову-триггеру, остальное считает ненайденным.

    Вес совпадения берём заведомо выше порога rules.MIN_FAQ_SCORE — иначе тест
    проверял бы не роутер, а настройку порога.
    """
    def search(query: str, role: str) -> list[dict]:
        text = (query or "").lower()
        if "стипенди" in text or "стипух" in text:
            return [dict(_HIT, score=rules.MIN_FAQ_SCORE + 5.0)]
        return []

    monkeypatch.setattr(faq, "search", search)


def _analysis(**overrides) -> dict:
    base = {
        "off_topic": False,
        "category": "campus_life",
        "audience": "student",
        "tone": "neutral",
        "keywords": ["стипендия"],
        "known_fields": {},
        "confidence": 0.8,
        "query": "когда придет стипуха",
    }
    base.update(overrides)
    return base


def test_off_topic_leads_to_reject():
    d = decide(_analysis(off_topic=True), role="student")
    assert d.action == "reject"
    assert d.trace


def test_found_and_sufficient_leads_to_answer():
    d = decide(_analysis(), role="student")
    assert d.action == "answer"
    assert d.articles
    assert d.articles[0]["id"] == "fx-001"


def test_missing_required_fields_leads_to_clarify():
    analysis = _analysis(
        category="documents",
        query="нужна справка для военкомата",
        keywords=["справка"],
        known_fields={},
    )
    d = decide(analysis, role="student")
    assert d.action == "clarify"
    assert set(d.missing) == {"ФИО", "вид справки", "куда предоставляется"}


def test_nothing_found_and_fields_complete_leads_to_ticket():
    analysis = _analysis(
        category="documents",
        query="космический вопрос без ответа в базе",
        keywords=["никогда такого не будет в faq"],
        known_fields={"ФИО": "Иванов", "вид справки": "об обучении", "куда предоставляется": "в банк"},
    )
    d = decide(analysis, role="student")
    assert d.action == "ticket"
    assert d.priority in {"P1", "P2", "P3", "P4"}


def test_trace_present_for_all_four_outcomes():
    outcomes = {}
    outcomes["reject"] = decide(_analysis(off_topic=True), role="student")
    outcomes["answer"] = decide(_analysis(), role="student")
    outcomes["clarify"] = decide(
        _analysis(category="documents", query="справка", keywords=["справка"], known_fields={}),
        role="student",
    )
    outcomes["ticket"] = decide(
        _analysis(
            category="documents",
            query="ничего похожего в базе нет",
            keywords=["ничего похожего в базе нет"],
            known_fields={"ФИО": "Иванов", "вид справки": "x", "куда предоставляется": "y"},
        ),
        role="student",
    )
    for action, decision in outcomes.items():
        assert decision.action == action
        assert decision.trace, f"trace пуст для {action}"
        assert "llm" in decision.trace and "code" in decision.trace


def test_decide_accepts_object_with_attributes_not_only_dict():
    analysis = SimpleNamespace(
        off_topic=False,
        category="campus_life",
        audience="student",
        tone="neutral",
        keywords=["стипендия"],
        known_fields={},
        confidence=0.9,
        query="когда придет стипуха",
    )
    d = decide(analysis, role="student")
    assert d.action == "answer"


def test_reject_trace_contains_llm_decision_fields():
    d = decide(_analysis(off_topic=True, category="other", tone="neutral"), role="student")
    assert d.trace["llm"]["category"] == "other"
    assert d.trace["code"]["rule_fired"] == "off_topic"
