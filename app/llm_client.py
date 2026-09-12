"""Клиент Groq: OpenAI-совместимый /chat/completions с диск-кэшем и фолбэком.

Бесплатный тариф Groq — 1000 запросов в сутки и 8000 токенов в минуту НА ВСЮ КОМАНДУ
(team/alexa/task/04-core-agent.md), поэтому кэш обязателен: одинаковый запрос не должен
уходить в сеть дважды.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import Path

import httpx

from app import config

logger = logging.getLogger(__name__)
logger.setLevel(getattr(logging, config.LOG_LEVEL.upper(), logging.INFO))

# Без User-Agent Groq отвечает 403 — проверено на живых вызовах.
_USER_AGENT = "shorthack/1.0"

_RETRYABLE_TRANSPORT_ERRORS = (httpx.TimeoutException, httpx.TransportError)


class LLMUnavailable(Exception):
    """Groq недоступен: сеть/таймаут, 5xx после ретрая, или обе модели ответили 429/413."""


class LLMBadOutput(Exception):
    """Модель ответила текстом, который не парсится как JSON. Сырой текст — для диагностики."""

    def __init__(self, raw: str):
        super().__init__(f"LLM вернула не-JSON: {raw[:200]!r}")
        self.raw = raw


def _cache_key(model: str, system: str, user: str) -> str:
    payload = f"{model}\x00{system}\x00{user}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _cache_path(key: str) -> Path:
    return config.CACHE_DIR / f"{key}.json"


def _read_cache(key: str) -> tuple[dict, str] | None:
    """Возвращает (ответ, модель-автор) или None. Автор нужен, чтобы в логе не
    выдавать ответ запасной модели за ответ основной."""
    path = _cache_path(key)
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None  # битый файл кэша — считаем, что его нет, а не падаем
    if isinstance(raw, dict) and isinstance(raw.get("data"), dict) and "model" in raw:
        return raw["data"], str(raw["model"])
    return raw, "неизвестна"  # файл старого формата: только сам ответ


def _write_cache(key: str, data: dict, model: str) -> None:
    config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"model": model, "data": data}
    _cache_path(key).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# Groq отклоняет response_format=json_object, если слово "json" не встречается в сообщениях.
# Страховка на уровне клиента: промпты пишут разные люди, и такой 400 ловится только в рантайме.
_JSON_HINT = "\n\nОтвет верните строго в формате JSON."


def _call_model(model: str, system: str, user: str, max_tokens: int) -> httpx.Response:
    if "json" not in (system + user).lower():
        system = system + _JSON_HINT
    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
        "User-Agent": _USER_AGENT,
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    return httpx.post(config.GROQ_ENDPOINT, headers=headers, json=body, timeout=config.LLM_TIMEOUT)


def _send_with_retry(model: str, system: str, user: str, max_tokens: int) -> httpx.Response:
    """Один ретрай через секунду — только на таймаут/сетевую ошибку/5xx одной и той же модели."""
    try:
        resp = _call_model(model, system, user, max_tokens)
        if resp.status_code < 500:
            return resp
    except _RETRYABLE_TRANSPORT_ERRORS:
        pass

    time.sleep(1)
    try:
        resp = _call_model(model, system, user, max_tokens)
    except _RETRYABLE_TRANSPORT_ERRORS as exc:
        raise LLMUnavailable(f"{model}: сеть недоступна после ретрая") from exc
    if resp.status_code >= 500:
        raise LLMUnavailable(f"{model}: HTTP {resp.status_code} после ретрая")
    return resp


def _extract_json(resp: httpx.Response) -> tuple[dict, dict]:
    """Достаёт content модели и usage. Возвращает (распарсенный JSON модели, usage)."""
    try:
        resp_json = resp.json()
        content = resp_json["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise LLMUnavailable(f"неожиданный формат ответа Groq: {exc}") from exc

    usage = resp_json.get("usage", {})
    try:
        return json.loads(content), usage
    except json.JSONDecodeError as exc:
        raise LLMBadOutput(content) from exc


def complete_json(system: str, user: str, *, max_tokens: int = 1200) -> dict:
    """Разбор промпта моделью с обязательным JSON-ответом.

    Порядок: кэш основной модели -> основная модель -> при 429/413 переключение на
    запасную -> при 429/413 запасной тоже — LLMUnavailable.
    """
    primary_key = _cache_key(config.GROQ_MODEL, system, user)
    cached = _read_cache(primary_key)
    if cached is not None:
        logger.info("llm cache=hit model=%s", cached[1])
        return cached[0]

    resp = _send_with_retry(config.GROQ_MODEL, system, user, max_tokens)
    model_used = config.GROQ_MODEL

    if resp.status_code in (429, 413):
        logger.warning(
            "llm model=%s status=%s -> переключаюсь на %s",
            config.GROQ_MODEL, resp.status_code, config.GROQ_MODEL_FALLBACK,
        )
        resp = _send_with_retry(config.GROQ_MODEL_FALLBACK, system, user, max_tokens)
        model_used = config.GROQ_MODEL_FALLBACK
        if resp.status_code in (429, 413):
            raise LLMUnavailable(f"обе модели вернули {resp.status_code}")

    if resp.status_code >= 400:
        # Тело ответа в тексте ошибки: без него 400 от Groq неотличимы друг от друга.
        raise LLMUnavailable(f"{model_used}: HTTP {resp.status_code} {resp.text[:300]}")

    parsed, usage = _extract_json(resp)
    logger.info(
        "llm cache=miss model=%s tokens=%s",
        model_used, usage.get("total_tokens", "?"),
    )
    # Кладём ответ и под ключ основной модели тоже. Иначе прогрев, попавший на 429 и
    # отработавший запасной моделью, не прогревает ничего: на защите тот же вопрос
    # снова пойдёт в сеть. Автор ответа при этом сохранён внутри файла и виден в логе.
    _write_cache(_cache_key(model_used, system, user), parsed, model_used)
    if model_used != config.GROQ_MODEL:
        _write_cache(primary_key, parsed, model_used)
    return parsed


def warm_cache(pairs: list[tuple[str, str]]) -> int:
    """Прогревает дисковый кэш парами (system, user) перед демо.

    Возвращает число пар, которые реально ушли в сеть (уже закэшированные пропускаются).
    """
    sent = 0
    for system, user in pairs:
        if _read_cache(_cache_key(config.GROQ_MODEL, system, user)) is not None:
            continue
        try:
            complete_json(system, user)
            sent += 1
        except (LLMUnavailable, LLMBadOutput):
            logger.warning("warm_cache: не удалось прогреть одну из пар")
    return sent
