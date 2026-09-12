"""
app/limits.py

Дешёвый заслон от выжигания общей квоты LLM: счётчик обращений по ключу
(обычно — IP клиента) со скользящими окнами + общий суточный предохранитель
на весь сервис.

Почему в памяти процесса, а не в БД/Redis: приложение однопроцессное
(см. Dockerfile — один uvicorn-воркер), лишняя внешняя зависимость ради
счётчика не нужна, а пережить перезапуск контейнера счётчикам не требуется —
после рестарта немного щедрее, но это не проблема при демо-нагрузке.

Время нигде не берётся напрямую через time.time() внутри логики лимитов —
как и в app/auth.py, каждая публичная функция принимает необязательный
параметр now, чтобы тесты подменяли "текущее время" явно, без sleep().

Потокобезопасность: uvicorn обслуживает запросы в нескольких потоках
(даже с одним воркером), поэтому всё состояние защищено одним threading.Lock.
"""

from __future__ import annotations

import math
import threading
import time

# Лимиты подобраны из ТЗ хакатона: команде выделено 8000 токенов/мин и
# 1000 запросов/сутки на всех. 10/мин и 60/час с одного ключа — щедро для
# одного живого пользователя (диалог с паузами на чтение), но не даёт
# одному браузеру/скрипту в одиночку выесть минутную токен-квоту команды.
MAX_PER_MINUTE = 10
MAX_PER_HOUR = 60

# Общий предохранитель на весь сервис: не больше 600 обращений к модели в
# сутки при квоте 1000/сутки — треть оставляем в запасе на ручные проверки,
# ретраи и то, что демо в последний момент прогонят ещё раз.
MAX_PER_DAY_GLOBAL = 600

_MINUTE_SEC = 60.0
_HOUR_SEC = 3600.0
_DAY_SEC = 86400.0

# Как часто чистить словарь _buckets от ключей, чьё окно давно истекло.
# Без этого поток разных IP (или подмена X-Forwarded-For) рано или поздно
# раздул бы словарь в утечку памяти, хотя каждый отдельный ключ безобиден.
_SWEEP_INTERVAL_SEC = 300.0
# Если ключей накопилось подозрительно много ещё до истечения интервала —
# подметаем немедленно, не дожидаясь таймера (защита от всплеска за раз).
_SWEEP_KEY_THRESHOLD = 5000


class RateLimited(Exception):
    """Превышен один из лимитов — минутный, часовой или суточный общий."""

    def __init__(self, retry_after_sec: float, message: str):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


_lock = threading.Lock()
# key -> список меток времени принятых обращений за последний час, по
# возрастанию. Часовое окно — самое широкое из "персональных", поэтому
# минутный счёт получаем срезом того же списка, отдельного хранилища не надо.
_buckets: dict[str, list[float]] = {}
# Обращения, засчитанные для общего суточного предохранителя (по всем ключам).
_global_calls: list[float] = []
_last_sweep_at = 0.0


def _now() -> float:
    return time.time()


def _prune(timestamps: list[float], now: float, window_sec: float) -> None:
    # Список отсортирован по возрастанию, поэтому старые записи всегда
    # в начале — достаточно отрезать префикс, не сканируя всё целиком.
    cutoff = now - window_sec
    i = 0
    n = len(timestamps)
    while i < n and timestamps[i] <= cutoff:
        i += 1
    if i:
        del timestamps[:i]


def _sweep(now: float) -> None:
    """Убирает из _buckets ключи, у которых не осталось обращений в пределах
    часа. Вызывается под _lock."""
    global _last_sweep_at
    stale_keys = []
    for key, timestamps in _buckets.items():
        _prune(timestamps, now, _HOUR_SEC)
        if not timestamps:
            stale_keys.append(key)
    for key in stale_keys:
        del _buckets[key]
    _last_sweep_at = now


def _maybe_sweep(now: float) -> None:
    if now - _last_sweep_at >= _SWEEP_INTERVAL_SEC or len(_buckets) > _SWEEP_KEY_THRESHOLD:
        _sweep(now)


def check(key: str, *, now: float | None = None) -> None:
    """Засчитывает обращение для ключа `key` (обычно IP) либо бросает
    RateLimited, если превышен минутный, часовой или общий суточный лимит.

    Порядок проверки: минутный -> часовой -> суточный общий. При простом
    флуде с одного ключа сработает минутный лимит раньше остальных, что и
    даёт самый быстрый и понятный ответ клиенту.
    """
    now = _now() if now is None else now

    with _lock:
        _maybe_sweep(now)

        timestamps = _buckets.setdefault(key, [])
        _prune(timestamps, now, _HOUR_SEC)

        # Минутное окно — это "хвост" часового списка: сколько из хранимых
        # обращений моложе 60 секунд.
        minute_cutoff = now - _MINUTE_SEC
        minute_start_idx = len(timestamps)
        for i, t in enumerate(timestamps):
            if t > minute_cutoff:
                minute_start_idx = i
                break
        minute_count = len(timestamps) - minute_start_idx

        if minute_count >= MAX_PER_MINUTE:
            oldest_in_minute = timestamps[minute_start_idx]
            retry_after = math.ceil(max(_MINUTE_SEC - (now - oldest_in_minute), 0))
            raise RateLimited(
                retry_after_sec=retry_after,
                message=(
                    f"Слишком много запросов: лимит {MAX_PER_MINUTE} в минуту. "
                    f"Повторите через {retry_after} с."
                ),
            )

        if len(timestamps) >= MAX_PER_HOUR:
            oldest_in_hour = timestamps[0]
            retry_after = math.ceil(max(_HOUR_SEC - (now - oldest_in_hour), 0))
            raise RateLimited(
                retry_after_sec=retry_after,
                message=(
                    f"Слишком много запросов: лимит {MAX_PER_HOUR} в час. "
                    f"Повторите через {retry_after} с."
                ),
            )

        _prune(_global_calls, now, _DAY_SEC)
        if len(_global_calls) >= MAX_PER_DAY_GLOBAL:
            oldest_global = _global_calls[0]
            retry_after = math.ceil(max(_DAY_SEC - (now - oldest_global), 0))
            raise RateLimited(
                retry_after_sec=retry_after,
                message=(
                    "Сервис исчерпал суточный лимит обращений к модели "
                    f"({MAX_PER_DAY_GLOBAL} в сутки). Попробуйте позже."
                ),
            )

        timestamps.append(now)
        _global_calls.append(now)


def snapshot(now: float | None = None) -> dict:
    """Текущее состояние для эндпоинта здоровья.

    now можно передать явно (тесты); без аргумента берётся реальное время —
    так /health показывает актуальную картину, даже если давно не было check().
    """
    now = _now() if now is None else now

    with _lock:
        _prune(_global_calls, now, _DAY_SEC)
        _sweep(now)
        return {
            "day_used": len(_global_calls),
            "day_limit": MAX_PER_DAY_GLOBAL,
            "active_keys": len(_buckets),
        }


def reset() -> None:
    """Полный сброс состояния — используется только в тестах."""
    global _last_sweep_at
    with _lock:
        _buckets.clear()
        _global_calls.clear()
        _last_sweep_at = 0.0


def client_key(x_forwarded_for: str | None, remote_addr: str | None) -> str:
    """Достаёт ключ клиента из простых значений (не из объекта запроса
    FastAPI), чтобы модуль не зависел от веб-фреймворка и легко тестировался.

    X-Forwarded-For может содержать цепочку адресов через запятую
    ("клиент, прокси1, прокси2") — нам нужен первый, он ближе всего к
    реальному клиенту. Значение может быть мусорным (пустые звенья, лишние
    пробелы) — такие пропускаем и берём первое непустое.
    """
    if x_forwarded_for:
        for part in x_forwarded_for.split(","):
            candidate = part.strip()
            if candidate:
                return candidate

    if remote_addr and remote_addr.strip():
        return remote_addr.strip()

    return "unknown"
