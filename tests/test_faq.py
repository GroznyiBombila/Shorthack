"""Тесты app/faq.py — без сети и без модели.

Поиск проверяем на собственной маленькой базе-фикстуре, а не на боевых данных
из data/faq. Раньше тесты ссылались на id заглушек, и первое же пополнение базы
знаний уронило семь тестов разом, хотя поиск работал правильно. Свойства
алгоритма (морфология, ранжирование, отсутствие full_text в выдаче) от
содержимого базы не зависят — значит, и тесты зависеть не должны.

Отдельно, последним блоком, проверяем боевую базу: не на конкретные id, а на то,
что файлы на месте, читаются и отвечают на вопросы своей роли.
"""
from __future__ import annotations

import json

import pytest

from app import config, faq

# Минимальная база: две записи, различимые и по ключевым словам, и по смыслу.
FIXTURE = [
    {
        "id": "fx-001",
        "category": "campus_life",
        "title": "Стипендии и выплаты",
        "url": "https://example.test/stipendii",
        "keywords": ["стипендия", "стипуха", "выплаты", "когда придут деньги"],
        "summary": "Сроки и виды стипендиальных выплат.",
        "full_text": "Стипендия перечисляется до 25 числа каждого месяца.",
    },
    {
        "id": "fx-002",
        "category": "account",
        "title": "Личный кабинет",
        "url": "https://example.test/lk",
        "keywords": ["личный кабинет", "вход", "пароль"],
        "summary": "Как войти в личный кабинет и что в нём есть.",
        "full_text": "Вход в личный кабинет — по корпоративной почте.",
    },
]


@pytest.fixture(autouse=True)
def fixture_base(tmp_path, monkeypatch):
    """Подменяет базу знаний роли student на FIXTURE.

    Загрузчик кэширует файлы в памяти процесса (lru_cache), поэтому кэш
    сбрасываем и до, и после теста: иначе фикстура протечёт в соседние тесты,
    которые работают с боевой базой.
    """
    faq_dir = tmp_path / "faq"
    faq_dir.mkdir(parents=True)
    (faq_dir / "faq_student.json").write_text(
        json.dumps(FIXTURE, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "FAQ_DIR", faq_dir)
    monkeypatch.setattr(config, "REPO_DATA_DIR", tmp_path)
    faq._load_cached.cache_clear()
    yield
    faq._load_cached.cache_clear()


def test_search_finds_scholarship_by_colloquial_query():
    results = faq.search("когда придет стипуха", "student")
    assert any(r["id"] == "fx-001" for r in results)


def test_search_handles_morphology_stipendii():
    # "стипендии" должно находить запись про стипендию через отсечение окончания.
    results = faq.search("расскажи про стипендии", "student")
    assert any(r["id"] == "fx-001" for r in results)


def test_search_empty_database_returns_empty_list(monkeypatch):
    monkeypatch.setattr(faq, "load", lambda role: [])
    assert faq.search("стипендия", "student") == []


def test_search_missing_role_file_is_not_an_error():
    assert faq.search("что угодно", "teacher") == []


def test_index_has_no_full_text():
    idx = faq.index("student")
    assert idx, "индекс не должен быть пустым на данных фикстуры"
    for item in idx:
        assert "full_text" not in item
        assert "summary" not in item
        assert set(item.keys()) == {"id", "title", "keywords", "category"}


def test_search_results_have_no_full_text():
    results = faq.search("стипендия", "student")
    assert results
    for r in results:
        assert "full_text" not in r
        assert "score" in r


def test_get_article_returns_full_text():
    article = faq.get_article("fx-001", "student")
    assert article is not None
    assert article["full_text"]


def test_get_article_unknown_id_returns_none():
    assert faq.get_article("does-not-exist", "student") is None


def test_search_ranks_keyword_match_above_summary_only():
    # "личный кабинет" — точная фраза в keywords записи fx-002, а в fx-001
    # совпадений нет вовсе: точное попадание обязано быть первым.
    results = faq.search("личный кабинет", "student")
    assert results
    assert results[0]["id"] == "fx-002"


# --- Боевая база знаний -------------------------------------------------------
# Здесь фикстура не нужна: проверяем ровно те файлы, что поедут на защиту.

@pytest.mark.parametrize("role", ["student", "applicant", "teacher"])
def test_real_knowledge_base_loads_and_is_well_formed(role, fixture_base, monkeypatch):
    monkeypatch.undo()  # снимаем подмену каталога, читаем настоящие data/faq
    faq._load_cached.cache_clear()
    items = faq.load(role)
    assert items, f"база знаний роли {role} пуста — на защите эта роль отвечать не будет"
    required = {"id", "category", "title", "url", "keywords", "summary", "full_text"}
    ids = set()
    for item in items:
        assert required <= set(item), f"в записи {item.get('id')} роли {role} не хватает полей"
        assert item["id"] not in ids, f"дубль id {item['id']} в базе роли {role}"
        ids.add(item["id"])


@pytest.mark.parametrize("role,query", [
    ("student", "где посмотреть расписание занятий"),
    ("applicant", "какие документы нужны для поступления"),
    ("teacher", "как подключиться к корпоративному wi-fi"),
])
def test_real_knowledge_base_answers_typical_question(role, query, fixture_base, monkeypatch):
    """Сторожевой тест: типовой вопрос роли обязан находиться уверенно.

    Порог тот же, что у роутера (rules.MIN_FAQ_SCORE): если совпадение слабее,
    ассистент не ответит, а поведёт человека в обращение — и демо развалится
    ровно на самом очевидном вопросе.
    """
    from app import rules

    monkeypatch.undo()
    faq._load_cached.cache_clear()
    found = faq.search(query, role)
    assert found, f"роль {role}: на вопрос «{query}» база не нашла ничего"
    assert found[0]["score"] >= rules.MIN_FAQ_SCORE, (
        f"роль {role}: «{query}» нашлось слабо (score={found[0]['score']}), "
        f"ассистент отправит человека писать обращение")
