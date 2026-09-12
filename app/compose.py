"""Генерация текста: выжимки ответов и карточка обращения.

Здесь LLM снова только переводит структуру в текст. Что именно отвечать и на какие
вопросы — уже решено роутером, сюда приходит готовый список статей.

Все вызовы пакетные: один запрос на все отвеченные вопросы, а не по запросу на каждый.
При лимите 8000 токенов в минуту на команду это разница между работающим демо и 429.
"""
from __future__ import annotations

import json
import logging

from app import prompts
from app.llm_client import LLMBadOutput, LLMUnavailable, complete_json

log = logging.getLogger(__name__)

# Сколько символов статьи отдаём модели. Больше — риск упереться в лимит токенов.
_ARTICLE_CHARS = 1200


def answers(items: list[dict]) -> dict[str, str]:
    """items: [{"qid","question","articles":[{title,url,full_text|summary}]}]

    Возвращает {qid: выжимка}. При недоступной модели — запасной путь: отдаём
    summary первой статьи. Ответ без модели хуже, но честный и из базы знаний.
    """
    if not items:
        return {}

    payload = [
        {
            "id": it["qid"],
            "question": it["question"],
            "articles": [
                {
                    "title": a.get("title", ""),
                    "text": (a.get("full_text") or a.get("summary") or "")[:_ARTICLE_CHARS],
                }
                for a in it["articles"]
            ],
        }
        for it in items
    ]
    user = (
        "Составь короткую выжимку по каждому вопросу, опираясь ТОЛЬКО на переданные "
        "статьи. Верни JSON вида {\"answers\": {\"<id>\": \"текст\"}}.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        data = complete_json(prompts.COMPOSE_ANSWER_SYSTEM, user, max_tokens=900)
        out = data.get("answers") or {}
        result = {
            it["qid"]: str(out[it["qid"]]).strip()
            for it in items
            if isinstance(out.get(it["qid"]), str) and out[it["qid"]].strip()
        }
    except (LLMUnavailable, LLMBadOutput) as exc:
        log.warning("compose.answers: модель недоступна (%s), отдаём summary статей", exc)
        result = {}

    for it in items:  # добиваем то, что модель не вернула
        if it["qid"] not in result:
            first = it["articles"][0] if it["articles"] else {}
            result[it["qid"]] = (first.get("summary") or first.get("full_text") or "")[:400]
    return result


def ticket(*, role: str, category: str, priority: str, questions: list[str],
           known: dict, original: str) -> dict:
    """Возвращает {"subject_hint": ..., "body": ...}.

    subject_hint — краткая суть одной строкой, из неё api.py собирает тему письма
    по формату из docs/API.md. Саму тему модель не составляет: формат темы — наш
    контракт, его нельзя отдавать на усмотрение модели.
    """
    user = json.dumps(
        {
            "роль": role,
            "категория": category,
            "приоритет": priority,
            "вопросы": questions,
            "известные_данные": known,
            "исходный_текст": original[:3000],
        },
        ensure_ascii=False,
    )
    try:
        data = complete_json(prompts.COMPOSE_TICKET_SYSTEM, user, max_tokens=700)
        subject_hint = str(data.get("subject_hint") or data.get("subject") or "").strip()
        body = str(data.get("body") or "").strip()
        if subject_hint and body:
            return {"subject_hint": subject_hint[:120], "body": body}
        log.warning("compose.ticket: модель вернула неполный ответ, собираем сами")
    except (LLMUnavailable, LLMBadOutput) as exc:
        log.warning("compose.ticket: модель недоступна (%s), собираем сами", exc)

    return {"subject_hint": _fallback_subject(questions, original), "body":
            _fallback_body(role, category, priority, questions, known, original)}


def _fallback_subject(questions: list[str], original: str) -> str:
    source = questions[0] if questions else original
    return " ".join(source.split())[:100]


def _fallback_body(role: str, category: str, priority: str, questions: list[str],
                   known: dict, original: str) -> str:
    lines = [
        f"Роль заявителя: {role}",
        f"Категория: {category}",
        f"Приоритет: {priority}",
        "",
        "Вопросы:",
        *(f"- {q}" for q in questions),
    ]
    if known:
        lines += ["", "Извлечённые данные:",
                  *(f"- {k}: {v}" for k, v in known.items())]
    lines += ["", "Исходный текст обращения:", original]
    return "\n".join(lines)
