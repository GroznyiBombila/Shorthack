"""Детерминированные правила: обязательные поля, приоритет, достаточность.

Никакой модели и никакой магии — чистые функции над простыми структурами
(dict / list), полностью тестируемые без сети. Это ровно то, что решает код,
а не LLM (см. docs/SOLUTION.md, раздел «Где LLM, а где обычный код»).
"""
from __future__ import annotations

# Категории соответствуют enum из docs/API.md.
REQUIRED_FIELDS: dict[str, list[str]] = {
    "admission": ["ФИО", "направление подготовки"],
    "study": ["ФИО", "институт", "группа"],
    "campus_life": ["ФИО", "группа"],
    "account": ["ФИО", "логин", "описание ошибки"],
    "documents": ["ФИО", "вид справки", "куда предоставляется"],
    "other": ["описание", "контакт для связи"],
}

# Ступени приоритета в порядке возрастания важности — используется, чтобы
# «поднять на одну ступень» было арифметикой по индексу, а не набором if.
_PRIORITY_ORDER = ["P4", "P3", "P2", "P1"]

# Ключевые слова, по которым код (не модель!) распознаёт горящие сроки и
# полную блокировку — список сознательно небольшой и прозрачный для защиты.
_URGENT_DEADLINE_MARKERS = ("сегодня", "сессия", "экзамен", "дедлайн", "горит", "завтра")
_BLOCKING_MARKERS = ("не могу войти", "заблокирован", "не работает", "доступ")
_IRRITATED_TONES = ("irritated", "angry", "раздражен", "раздражён", "злой")


def missing_fields(category: str, known: dict) -> list[str]:
    """Каких обязательных полей категории нет среди уже известных (known)."""
    required = REQUIRED_FIELDS.get(category, REQUIRED_FIELDS["other"])
    # known — это extracted_fields от LLM: ключ известен, если значение непустое.
    return [f for f in required if not known.get(f)]


def _base_priority(category: str, keywords: list[str]) -> str:
    """Приоритет по фактам обращения, без учёта тона."""
    text = " ".join(keywords).lower()
    has_deadline = any(m in text for m in _URGENT_DEADLINE_MARKERS)
    has_blocking = any(m in text for m in _BLOCKING_MARKERS)

    # P1: потеря доступа именно в период сессии/экзамена с горящими сроками.
    if has_blocking and has_deadline:
        return "P1"
    # P2: блокирует учёбу (доступ пропал), но сроки не поджимают.
    if has_blocking:
        return "P2"
    # P3: обычный вопрос по существу (не общая справка).
    if category != "other":
        return "P3"
    # P4: справочный вопрос без конкретики.
    return "P4"


def priority(category: str, tone: str, keywords: list[str]) -> str:
    """P1..P4. Раздражённый тон поднимает приоритет максимум на одну ступень.

    Осознанное решение: тон — это эмоция, а не факт о серьёзности проблемы.
    Он не должен превращать справочный вопрос (P4) в критичный инцидент (P1) —
    иначе достаточно написать грубо, чтобы обойти реальную приоритизацию.
    """
    base = _base_priority(category, keywords)
    if tone.lower() not in _IRRITATED_TONES:
        return base
    idx = _PRIORITY_ORDER.index(base)
    idx = min(idx + 1, len(_PRIORITY_ORDER) - 1)
    return _PRIORITY_ORDER[idx]


def is_sufficient(analysis_like, found: list[dict]) -> bool:
    """Хватает ли найденного в базе знаний, чтобы ответить, а не заводить заявку.

    analysis_like — любой объект с атрибутом/полем confidence (0..1) от LLM,
    протокол описан в router.decide. found — результат faq.search (со score).
    Порог confidence подобран так, чтобы низкая уверенность модели в теме
    вопроса не приводила к выдуманному ответу (см. критерии приёмки в
    team/alexa/task/04-core-agent.md).
    """
    if not found:
        return False
    confidence = getattr(analysis_like, "confidence", None)
    if confidence is None and isinstance(analysis_like, dict):
        confidence = analysis_like.get("confidence")
    if confidence is not None and confidence < 0.4:
        return False
    return True


def route_reason(rule: str, **details: object) -> str:
    """Короткая человекочитаемая причина решения — уходит в trace для защиты."""
    parts = ", ".join(f"{k}={v}" for k, v in details.items())
    return f"{rule}" + (f" ({parts})" if parts else "")
