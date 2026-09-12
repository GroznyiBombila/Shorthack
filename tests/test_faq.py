"""Тесты app/faq.py — без сети, без модели, на реальной заглушечной базе student."""
from __future__ import annotations

from app import faq


def test_search_finds_scholarship_by_colloquial_query():
    results = faq.search("когда придет стипуха", "student")
    assert any(r["id"] == "std-003" for r in results)


def test_search_handles_morphology_stipendii():
    # "стипендии" должно находить запись про стипендию через отсечение окончания.
    results = faq.search("расскажи про стипендии", "student")
    assert any(r["id"] == "std-003" for r in results)


def test_search_empty_database_returns_empty_list(monkeypatch):
    monkeypatch.setattr(faq, "load", lambda role: [])
    assert faq.search("стипендия", "student") == []


def test_search_missing_role_file_is_not_an_error():
    assert faq.search("что угодно", "teacher") == []


def test_index_has_no_full_text():
    idx = faq.index("student")
    assert idx, "индекс не должен быть пустым на заглушечных данных"
    for item in idx:
        assert "full_text" not in item
        assert "summary" not in item
        assert set(item.keys()) == {"id", "title", "keywords", "category"}


def test_search_results_have_no_full_text():
    results = faq.search("расписание занятий", "student")
    assert results
    for r in results:
        assert "full_text" not in r
        assert "score" in r


def test_get_article_returns_full_text():
    article = faq.get_article("std-003", "student")
    assert article is not None
    assert "full_text" in article
    assert article["full_text"]


def test_get_article_unknown_id_returns_none():
    assert faq.get_article("does-not-exist", "student") is None


def test_search_ranks_keyword_match_above_summary_only():
    # "личный кабинет" — точная фраза в keywords записи std-005.
    results = faq.search("личный кабинет", "student")
    assert results
    assert results[0]["id"] == "std-005"
