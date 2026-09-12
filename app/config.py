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
