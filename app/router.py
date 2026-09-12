"""Выбор действия: ответить / уточнить / завести заявку / отказать.

Ключевой тезис защиты (docs/SOLUTION.md): LLM не принимает решений, она только
раскладывает текст по полям. Всё, что ниже, — детерминированный код без сети
и без вызова модели. `decide` не импортирует app.analyze (чтобы не зависеть от
параллельно разрабатываемого модуля) — вместо этого она описывает протокол,
которому должен соответствовать переданный снаружи объект `analysis`.

Протокол объекта `analysis` (то, что кладёт туда LLM-разбор из app/analyze.py):
    off_topic: bool            — вопрос не про университет
    category: str              — одно из REQUIRED_FIELDS / API.md enum
    audience: str              — applicant|student|teacher|both
    tone: str                  — тон обращения, например "neutral"|"irritated"
    keywords: list[str]        — ключевые слова/сущности, извлечённые моделью
    known_fields: dict         — то, что модель уже нашла в тексте (ФИО и т.п.)
    confidence: float          — уверенность модели в разборе (0..1)
    query: str                 — нормализованный текст вопроса для поиска по FAQ
Атрибуты читаются через getattr, поэтому подойдёт и dict, и dataclass, и
SimpleNamespace — лишь бы нужные поля были.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app import faq, rules


@dataclass
class Decision:
    action: str  # "answer" | "clarify" | "ticket" | "reject"
    articles: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    priority: str | None = None
    reason: str = ""
    trace: dict = field(default_factory=dict)


def _get(analysis: Any, name: str, default: Any) -> Any:
    """Читает поле из analysis независимо от того, dict это или объект."""
    if isinstance(analysis, dict):
        return analysis.get(name, default)
    return getattr(analysis, name, default)


def decide(analysis: Any, role: str) -> Decision:
    """Жёсткий порядок проверок — см. докстринг модуля и team/.../04-core-agent.md.

    1. off_topic -> reject.
    2. Поиск по базе знаний; если нашлось и хватает -> answer.
    3. Не хватает обязательных полей -> clarify.
    4. Иначе -> ticket с приоритетом.
    """
    off_topic = bool(_get(analysis, "off_topic", False))
    category = _get(analysis, "category", "other")
    tone = _get(analysis, "tone", "neutral")
    keywords = _get(analysis, "keywords", []) or []
    known_fields = _get(analysis, "known_fields", {}) or {}
    query = _get(analysis, "query", "") or " ".join(keywords)

    # То, что решила модель — идёт в trace как есть, для прозрачности на защите.
    llm_trace = {
        "category": category,
        "audience": _get(analysis, "audience", role),
        "tone": tone,
        "confidence": _get(analysis, "confidence", None),
    }

    if off_topic:
        reason = rules.route_reason("off_topic->reject")
        return Decision(
            action="reject",
            reason=reason,
            trace={"llm": llm_trace, "code": {"rule_fired": "off_topic", "articles": []}},
        )

    found = faq.search(query, role) if query else []

    if rules.is_sufficient(analysis, found):
        reason = rules.route_reason("faq_match->answer", top_score=found[0]["score"])
        return Decision(
            action="answer",
            articles=found,
            reason=reason,
            trace={
                "llm": llm_trace,
                "code": {
                    "rule_fired": "faq_sufficient",
                    "articles": [{"id": a["id"], "score": a["score"]} for a in found],
                },
            },
        )

    missing = rules.missing_fields(category, known_fields)
    if missing:
        reason = rules.route_reason("missing_fields->clarify", fields=",".join(missing))
        return Decision(
            action="clarify",
            missing=missing,
            articles=found,
            reason=reason,
            trace={
                "llm": llm_trace,
                "code": {
                    "rule_fired": "missing_required_fields",
                    "missing": missing,
                    "articles": [{"id": a["id"], "score": a["score"]} for a in found],
                },
            },
        )

    prio = rules.priority(category, tone, keywords)
    reason = rules.route_reason("no_answer_enough_data->ticket", priority=prio)
    return Decision(
        action="ticket",
        articles=found,
        priority=prio,
        reason=reason,
        trace={
            "llm": llm_trace,
            "code": {
                "rule_fired": "fallback_to_ticket",
                "priority": prio,
                "articles": [{"id": a["id"], "score": a["score"]} for a in found],
            },
        },
    )
