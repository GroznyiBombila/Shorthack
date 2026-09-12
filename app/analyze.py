"""Разбор обращения: один вызов LLM + строгая валидация + запасной путь без сети.

Решение о действии (ответить/уточнить/завести заявку) здесь не принимается — это
router.py. analyze() только раскладывает сырой текст по полям (docs/SOLUTION.md).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app import prompts
from app.llm_client import LLMBadOutput, LLMUnavailable, complete_json

_CATEGORIES = {"admission", "study", "campus_life", "account", "documents", "other"}
_AUDIENCES = {"applicant", "student", "teacher", "unknown"}
_TONES = {"neutral", "irritated"}

_MAX_CHARS = 6000
_SENTENCE_END = re.compile(r"[.!?…]\s")
_WORD_RE = re.compile(r"[а-яёa-z]{5,}", re.IGNORECASE)


@dataclass
class Analysis:
    questions: list[str]
    category: str
    audience: str
    keywords: list[str]
    missing: list[str]
    tone: str
    off_topic: bool
    raw: dict
    degraded: bool = False


def _truncate(text: str, limit: int = _MAX_CHARS) -> str:
    """Режет по границе предложения, а не посреди слова — иначе модель видит обрубок."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    last_break = None
    for m in _SENTENCE_END.finditer(head):
        last_break = m.end()
    return head[:last_break].rstrip() if last_break else head.rstrip()


def _as_str_list(value: object) -> list[str]:
    """Модель иногда кладёт в списки не-строки или мусор — просто отбрасываем такие элементы."""
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _fallback_keywords(text: str) -> list[str]:
    """Простое извлечение слов длиннее 4 букв — работает и без сети."""
    seen: list[str] = []
    for word in _WORD_RE.findall(text.lower()):
        if word not in seen:
            seen.append(word)
    return seen[:20]


def _degrade(text: str, role: str, raw: dict) -> Analysis:
    audience = role if role in _AUDIENCES else "unknown"
    return Analysis(
        questions=[text],
        category="other",
        audience=audience,
        keywords=_fallback_keywords(text),
        missing=[],
        tone="neutral",
        off_topic=False,
        raw=raw,
        degraded=True,
    )


def analyze(text: str, role: str) -> Analysis:
    """role: "applicant"|"student"|"teacher" — известная роль, подмешивается в промпт."""
    truncated = _truncate(text)
    user = f"Известная роль отправителя: {role}.\n\nТекст обращения:\n{truncated}"

    try:
        data = complete_json(prompts.ANALYZE_SYSTEM, user)
    except (LLMUnavailable, LLMBadOutput) as exc:
        return _degrade(truncated, role, {"error": str(exc)})

    if not isinstance(data, dict):
        # валидный JSON, но не объект (например, модель вернула массив) — тоже деградация
        return _degrade(truncated, role, {"error": "ответ модели не является объектом"})

    category = data.get("category")
    category = category if category in _CATEGORIES else "other"

    audience = data.get("audience")
    audience = audience if audience in _AUDIENCES else "unknown"

    tone = data.get("tone")
    tone = tone if tone in _TONES else "neutral"

    off_topic = data.get("off_topic") if isinstance(data.get("off_topic"), bool) else False

    questions = _as_str_list(data.get("questions")) or [truncated]

    return Analysis(
        questions=questions,
        category=category,
        audience=audience,
        keywords=_as_str_list(data.get("keywords")),
        missing=_as_str_list(data.get("missing")),
        tone=tone,
        off_topic=off_topic,
        raw=data,
        degraded=False,
    )
