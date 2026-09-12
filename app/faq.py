"""Двухступенчатый поиск по базе знаний.

Смысл двух ступеней: в промпт модели (см. app/analyze.py, app/compose.py — не наша
зона) попадают ТОЛЬКО заголовки и ключевые слова разделов — это дёшево по токенам.
Полное содержание (`full_text`) подтягивается отдельным вызовом `get_article` уже
после того, как модель (или код) выбрала конкретный раздел по id.

Ранжирование в `search` работает без эмбеддингов — у Groq их нет (см. решение #35
в docs/SOLUTION.md) — поэтому это чистое текстовое сопоставление по ключевым словам
с примитивной русской морфологией.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache

from app import config

# Роли, для которых существует своя база знаний.
_ROLES = ("applicant", "student", "teacher")

# Типовые русские окончания для примитивного стеммера: отрезаем только у слов
# длиннее 5 букв и не больше 3 символов хвоста, чтобы не съесть короткие слова
# и корень (полноценная морфология вроде pymorphy — избыточна для хакатона).
_ENDINGS = sorted(
    ["ами", "ями", "ах", "ях", "ая", "ое", "ие", "ый", "ий", "ов", "ев", "ам", "ям", "ы", "и", "а", "о", "у", "е"],
    key=len,
    reverse=True,
)

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Нижний регистр, ё->е, без пунктуации, схлопнутые пробелы."""
    text = text.lower().replace("ё", "е")
    text = _PUNCT_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def _stem(word: str) -> str:
    """Отрезает типовое окончание у длинных слов. "стипендии" -> "стипенди"."""
    if len(word) <= 5:
        return word
    for ending in _ENDINGS:
        if len(ending) < len(word) - 2 and word.endswith(ending):
            return word[: -len(ending)]
    return word


def _stem_all(text: str) -> list[str]:
    return [_stem(w) for w in _normalize(text).split() if w]


@lru_cache(maxsize=None)
def _load_cached(role: str) -> tuple[dict, ...]:
    """Кэш в памяти процесса — файл читается максимум один раз на роль."""
    if role not in _ROLES:
        return ()
    # DATA_DIR — рабочий том (var/ или /data в контейнере), наполняется из
    # репозитория при первом запуске (см. app/config.py). До этого наполнения
    # (например, в тестах или сразу после git clone) читаем данные напрямую
    # из репозитория, чтобы поиск работал без отдельного шага деплоя.
    path = config.FAQ_DIR / f"faq_{role}.json"
    if not path.exists():
        path = config.REPO_DATA_DIR / "faq" / f"faq_{role}.json"
    if not path.exists():
        return ()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # Битая или отсутствующая база — не повод падать: просто нет ответов.
        return ()
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, dict) and "id" in item)


def load(role: str) -> list[dict]:
    """Полные записи базы знаний для роли (applicant|student|teacher)."""
    return list(_load_cached(role))


def index(role: str) -> list[dict]:
    """Лёгкий индекс для промпта: без summary и full_text — экономим токены."""
    return [
        {
            "id": item["id"],
            "title": item.get("title", ""),
            "keywords": item.get("keywords", []),
            "category": item.get("category", ""),
        }
        for item in load(role)
    ]


def get_article(article_id: str, role: str) -> dict | None:
    """Вторая ступень: полная запись (включая full_text) по id из нужной роли."""
    for item in load(role):
        if item.get("id") == article_id:
            return dict(item)
    return None


def _field_score(query_stems: list[str], query_norm: str, field_text: str, weight: float) -> float:
    """Score одного поля: пересечение стеммированных токенов + бонус за точную фразу."""
    if not field_text:
        return 0.0
    field_norm = _normalize(field_text)
    field_stems = set(_stem(w) for w in field_norm.split() if w)
    if not field_stems:
        return 0.0
    overlap = sum(1 for s in query_stems if s in field_stems)
    if overlap == 0:
        return 0.0
    score = overlap * weight
    # Бонус за точное вхождение всей фразы запроса целиком.
    if query_norm and query_norm in field_norm:
        score += weight
    return score


def search(query: str, role: str, limit: int = 5) -> list[dict]:
    """Первая ступень: ранжированные заголовки/summary БЕЗ full_text, с полем score."""
    query_norm = _normalize(query)
    query_stems = _stem_all(query)
    if not query_stems:
        return []

    scored: list[tuple[float, dict]] = []
    for item in load(role):
        # keywords весят больше title, title больше summary — так задумано ТЗ:
        # ключевые слова — это разговорные формулировки, самое надёжное совпадение.
        kw_text = " ".join(item.get("keywords", []))
        score = (
            _field_score(query_stems, query_norm, kw_text, weight=3.0)
            + _field_score(query_stems, query_norm, item.get("title", ""), weight=2.0)
            + _field_score(query_stems, query_norm, item.get("summary", ""), weight=1.0)
        )
        if score > 0:
            entry = {
                "id": item["id"],
                "category": item.get("category", ""),
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "keywords": item.get("keywords", []),
                "summary": item.get("summary", ""),
                "score": score,
            }
            scored.append((score, entry))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [entry for _, entry in scored[:limit]]
