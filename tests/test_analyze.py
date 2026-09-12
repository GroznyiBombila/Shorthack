"""Тесты app/analyze.py. Без сети: complete_json мокается через monkeypatch."""
from __future__ import annotations

import pytest

from app import analyze
from app.llm_client import LLMUnavailable


def test_valid_response_parses_into_analysis(monkeypatch):
    payload = {
        "questions": ["Когда подавать документы на бюджет", "Нужен ли оригинал аттестата"],
        "category": "admission",
        "audience": "applicant",
        "keywords": ["бюджет", "документы", "аттестат"],
        "missing": ["направление подготовки"],
        "tone": "neutral",
        "off_topic": False,
    }
    monkeypatch.setattr(analyze, "complete_json", lambda system, user: payload)

    result = analyze.analyze("Когда подавать документы на бюджет? Нужен ли оригинал аттестата?", "applicant")

    assert result.degraded is False
    assert result.category == "admission"
    assert result.audience == "applicant"
    assert result.questions == payload["questions"]
    assert result.keywords == payload["keywords"]
    assert result.missing == payload["missing"]
    assert result.tone == "neutral"
    assert result.off_topic is False
    assert result.raw == payload


def test_garbage_json_object_degrades_without_crash(monkeypatch):
    # complete_json сам гарантирует валидный JSON-объект (иначе кидает LLMBadOutput),
    # поэтому "мусор" на этом уровне — структурно не тот объект, что мы ожидаем.
    monkeypatch.setattr(analyze, "complete_json", lambda system, user: ["не то, что мы просили"])

    result = analyze.analyze("Помогите, не работает личный кабинет", "student")

    assert result.degraded is True
    assert result.category == "other"
    assert result.questions  # не падаем, вопрос сохранён


def test_unknown_category_falls_back_to_other(monkeypatch):
    payload = {
        "questions": ["Как записаться в бассейн"],
        "category": "sport",  # не из списка
        "audience": "student",
        "keywords": ["бассейн"],
        "missing": [],
        "tone": "neutral",
        "off_topic": False,
    }
    monkeypatch.setattr(analyze, "complete_json", lambda system, user: payload)

    result = analyze.analyze("Как записаться в бассейн?", "student")

    assert result.degraded is False
    assert result.category == "other"


def test_llm_unavailable_degrades_with_nonempty_questions(monkeypatch):
    def _boom(system, user):
        raise LLMUnavailable("Groq недоступен")

    monkeypatch.setattr(analyze, "complete_json", _boom)

    text = "Не приходит код подтверждения на почту, что делать?"
    result = analyze.analyze(text, "student")

    assert result.degraded is True
    assert result.category == "other"
    assert result.questions == [text]
    assert len(result.questions[0]) > 0


def test_long_text_is_truncated_before_reaching_llm(monkeypatch):
    captured: dict = {}

    def _capture(system, user):
        captured["user"] = user
        return {
            "questions": ["вопрос"],
            "category": "other",
            "audience": "unknown",
            "keywords": [],
            "missing": [],
            "tone": "neutral",
            "off_topic": False,
        }

    monkeypatch.setattr(analyze, "complete_json", _capture)

    long_text = "Помогите разобраться с заявлением. " * 500  # существенно больше 6000 символов
    assert len(long_text) > 6000

    analyze.analyze(long_text, "applicant")

    assert "user" in captured
    # в user-сообщении есть служебный префикс про роль — сам текст обращения должен
    # укладываться в лимит truncate, поэтому всё сообщение заметно короче исходника
    assert len(captured["user"]) < len(long_text)
    assert len(captured["user"]) <= 6000 + 200


def test_truncate_cuts_on_sentence_boundary():
    text = "Раз. " * 2000  # 5 символов на предложение, гарантированно больше лимита
    truncated = analyze._truncate(text, limit=100)

    assert len(truncated) <= 100
    assert truncated.endswith(".")
