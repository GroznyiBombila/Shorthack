"""Единая точка чтения окружения. Больше никто os.environ не трогает."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _path(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser().resolve()


# --- Данные ---
# Локально ./var, в контейнере /data. Внутри одинаковая раскладка.
DATA_DIR: Path = _path("DATA_DIR", "./var")
FAQ_DIR: Path = DATA_DIR / "faq"
SCHEDULE_DIR: Path = DATA_DIR / "schedule"
CACHE_DIR: Path = DATA_DIR / "cache"
OUTBOX_DIR: Path = DATA_DIR / "outbox"
DB_PATH: Path = DATA_DIR / "app.db"

# Исходные данные в репозитории: из них наполняется том при первом запуске.
REPO_DATA_DIR: Path = Path(__file__).resolve().parent.parent / "data"

# --- Groq ---
GROQ_API_KEY: str = os.getenv("GROQ_API_KEY", "")
GROQ_ENDPOINT: str = os.getenv(
    "GROQ_ENDPOINT", "https://api.groq.com/openai/v1/chat/completions"
)
GROQ_MODEL: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
GROQ_MODEL_FALLBACK: str = os.getenv("GROQ_MODEL_FALLBACK", "qwen/qwen3.8-27b")

# Полная цепочка моделей на случай, если и основная, и запасная за день упрутся
# в свой суточный лимит (обе делят его с квотой других команд хакатона на этом
# же ключе). На бесплатном тарифе Groq нет ни одной chat-модели с лимитом
# 500К токенов/сутки — единственные модели с таким TPD, llama-prompt-guard-2
# (22m/86m), не генерируют текст, это классификаторы промпт-инъекций, для
# составления ответа не годятся. Поэтому вместо одной модели с нужным лимитом
# собираем цепочку из четырёх пригодных (у каждой по 200К/сутки) — суммарный
# запас всей цепочки получается 800К/сутки. Порядок — от лучшей по качеству
# к простой; первые два элемента совпадают с GROQ_MODEL/GROQ_MODEL_FALLBACK
# выше, чтобы старые .env с них не ломались.
GROQ_MODEL_CHAIN: tuple[str, ...] = tuple(
    m.strip() for m in os.getenv(
        "GROQ_MODEL_CHAIN",
        f"{GROQ_MODEL},{GROQ_MODEL_FALLBACK},openai/gpt-oss-20b,qwen/qwen3.6-27b",
    ).split(",")
    if m.strip()
)

LLM_TIMEOUT: float = float(os.getenv("LLM_TIMEOUT", "30"))

# --- Почта ---
# SMTP_USER (логин на сервере), MAIL_FROM (заголовок From) и SUPPORT_EMAIL
# (куда уходят заявки) — это ОДИН И ТОТ ЖЕ корпоративный ящик: согласованного
# адреса деканата у нас нет, поэтому ящик пишет заявки сам себе, а реальный
# адресат по сути — в теме письма (см. app/routing.py и app/mailer.py).
# SMTP_USER/SMTP_PASSWORD читает только app/mailer.py (нужны исключительно
# для логина на SMTP-сервере), здесь читать их незачем.
MAIL_MODE: str = os.getenv("MAIL_MODE", "mock")
MAIL_FROM: str = os.getenv("MAIL_FROM", "")
# Явно не заданный SUPPORT_EMAIL по умолчанию равен MAIL_FROM — тот же ящик,
# другого адресата "по умолчанию" в этой схеме и не может быть.
SUPPORT_EMAIL: str = os.getenv("SUPPORT_EMAIL", "") or MAIL_FROM

# --- Расписание ---
SEMESTER_START: str = os.getenv("SEMESTER_START", "2026-09-01")

# --- Консоль сотрудника поддержки ---
# Не задан -> эндпоинты /api/operator/* отвечают 503, а не пускают всех подряд
# без проверки (см. Decision log: почта отключена администратором домена,
# консоль — единственный канал ответа, значит и защищать её нужно всерьёз).
OPERATOR_TOKEN: str = os.getenv("OPERATOR_TOKEN", "")

# --- Демо-режим кода подтверждения ---
# Почта корпоративного ящика недоступна (запрещены пароли приложений), поэтому
# код подтверждения студенту доехать не может. Включается только для защиты/
# демо и должна быть выключена в бою — см. docs/API.md.
AUTH_SHOW_CODE: bool = os.getenv("AUTH_SHOW_CODE", "").strip().lower() in {"1", "true", "yes", "on"}

# --- Приложение ---
APP_HOST: str = os.getenv("APP_HOST", "0.0.0.0")
APP_PORT: int = int(os.getenv("APP_PORT", "8000"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

# Куда агенту разрешено ходить инструментом fetch_page.
ALLOWED_FETCH_DOMAINS: tuple[str, ...] = ("misis.ru", "www.misis.ru")


def ensure_dirs() -> None:
    """Создаёт раскладку тома. Вызывается на старте приложения."""
    for d in (DATA_DIR, FAQ_DIR, SCHEDULE_DIR, CACHE_DIR, OUTBOX_DIR):
        d.mkdir(parents=True, exist_ok=True)
