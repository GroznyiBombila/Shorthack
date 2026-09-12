"""
app/auth.py

Подтверждение почты кодом + выдача подписанного токена сессии.
Чистая серверная логика, LLM тут не участвует.

Хранилище — SQLite (по умолчанию var/app.db, путь переопределяется
переменной окружения APP_DB_PATH — это же используется в тестах для
изоляции каждого теста в своей базе).

Время нигде не берётся напрямую через time.time() внутри бизнес-логики:
есть обёртка _now(), и каждая публичная функция принимает необязательный
параметр now, которым тесты подменяют "текущее время" явно.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
import time
from contextlib import closing

from app import mailer

CORPORATE_DOMAINS = {"misis.ru", "edu.misis.ru"}

CODE_TTL_SEC = 600  # время жизни кода
MAX_ATTEMPTS = 5  # максимум попыток ввода кода
RESEND_COOLDOWN_SEC = 60  # не чаще раза в минуту
MAX_CODES_PER_EMAIL_WINDOW_SEC = 600  # окно для лимита кодов на адрес
MAX_CODES_PER_EMAIL = 3  # не больше 3 кодов за 10 минут
MAX_REQUESTS_PER_IP_WINDOW_SEC = 3600  # окно для лимита по IP
MAX_REQUESTS_PER_IP = 20  # не больше 20 запросов с одного IP за час
TOKEN_TTL_SEC = 30 * 60  # токен сессии живёт 30 минут

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class InvalidEmail(Exception):
    """Адрес пустой, без @, без домена верхнего уровня и т.п."""


class RateLimited(Exception):
    """Слишком частые запросы кода (cooldown / лимит на адрес / лимит по IP)."""

    def __init__(self, retry_after_sec: float, message: str = "rate limited"):
        super().__init__(message)
        self.retry_after_sec = retry_after_sec


class InvalidCode(Exception):
    """Код неверный, но попытки ещё остались."""

    def __init__(self, attempts_left: int, message: str = "invalid code"):
        super().__init__(message)
        self.attempts_left = attempts_left


class CodeExpired(Exception):
    """Коду больше 600 секунд, либо для адреса вовсе нет активного кода."""


class TooManyAttempts(Exception):
    """Код сгорел из-за 5 неверных попыток ввода."""


def _now() -> float:
    return time.time()


def _db_path() -> str:
    return os.environ.get("APP_DB_PATH", "var/app.db")


def _connect() -> sqlite3.Connection:
    path = _db_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS codes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            user_type TEXT NOT NULL,
            salt TEXT NOT NULL,
            code_hash TEXT NOT NULL,
            created_at REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active'
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_codes_email ON codes(email, created_at)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ip_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ip TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ip_log_ip ON ip_log(ip, created_at)")
    conn.commit()


def _normalize_email(email: str) -> str:
    if not isinstance(email, str):
        raise InvalidEmail("email must be a string")
    normalized = email.strip().lower()
    if not normalized or not _EMAIL_RE.match(normalized):
        raise InvalidEmail(f"invalid email format: {email!r}")
    local, _, domain = normalized.partition("@")
    if not local or not domain or domain.startswith(".") or domain.endswith("."):
        raise InvalidEmail(f"invalid email format: {email!r}")
    return normalized


def classify_email(email: str) -> str:
    """'student' для корпоративных доменов, 'applicant' для любых других.

    Нормализует адрес (обрезка пробелов, нижний регистр).
    Бросает InvalidEmail на мусоре.
    """
    normalized = _normalize_email(email)
    domain = normalized.rsplit("@", 1)[1]
    return "student" if domain in CORPORATE_DOMAINS else "applicant"


def _generate_code() -> str:
    # secrets.randbelow — криптостойкий источник случайности, не random.
    return f"{secrets.randbelow(1_000_000):06d}"


def _hash_code(code: str, salt: str) -> str:
    return hashlib.sha256((salt + code).encode("utf-8")).hexdigest()


def request_code(email: str, ip: str, now: float | None = None) -> dict:
    """Генерирует код, сохраняет его ХЭШ, отправляет письмо через mailer.send().

    Возвращает {'user_type', 'ttl_sec', 'resend_after_sec'}.
    Письмо уходит вне зависимости от того, существует ли пользователь —
    эту развилку (есть юзер в реальной БД проекта или нет) решает
    вызывающий код в app/api.py; здесь она уже не участвует, поэтому
    сам факт вызова request_code всегда приводит к отправке письма.
    """
    now = _now() if now is None else now
    email_norm = _normalize_email(email)
    user_type = classify_email(email_norm)

    with closing(_connect()) as conn:
        last_row = conn.execute(
            "SELECT created_at FROM codes WHERE email = ? ORDER BY created_at DESC LIMIT 1",
            (email_norm,),
        ).fetchone()
        if last_row is not None:
            elapsed = now - last_row["created_at"]
            if elapsed < RESEND_COOLDOWN_SEC:
                raise RateLimited(retry_after_sec=math.ceil(RESEND_COOLDOWN_SEC - elapsed))

        window_start = now - MAX_CODES_PER_EMAIL_WINDOW_SEC
        recent_rows = conn.execute(
            "SELECT created_at FROM codes WHERE email = ? AND created_at > ? ORDER BY created_at ASC",
            (email_norm, window_start),
        ).fetchall()
        if len(recent_rows) >= MAX_CODES_PER_EMAIL:
            oldest = recent_rows[0]["created_at"]
            retry_after = (oldest + MAX_CODES_PER_EMAIL_WINDOW_SEC) - now
            raise RateLimited(retry_after_sec=math.ceil(max(retry_after, 0)))

        ip_window_start = now - MAX_REQUESTS_PER_IP_WINDOW_SEC
        ip_rows = conn.execute(
            "SELECT created_at FROM ip_log WHERE ip = ? AND created_at > ? ORDER BY created_at ASC",
            (ip, ip_window_start),
        ).fetchall()
        if len(ip_rows) >= MAX_REQUESTS_PER_IP:
            oldest = ip_rows[0]["created_at"]
            retry_after = (oldest + MAX_REQUESTS_PER_IP_WINDOW_SEC) - now
            raise RateLimited(retry_after_sec=math.ceil(max(retry_after, 0)))

        code = _generate_code()
        salt = secrets.token_hex(16)
        code_hash = _hash_code(code, salt)

        conn.execute(
            """
            INSERT INTO codes (email, user_type, salt, code_hash, created_at, attempts, status)
            VALUES (?, ?, ?, ?, ?, 0, 'active')
            """,
            (email_norm, user_type, salt, code_hash, now),
        )
        conn.execute("INSERT INTO ip_log (ip, created_at) VALUES (?, ?)", (ip, now))
        conn.commit()

    # Код не должен попадать в логи ни при каких условиях — сюда его тоже
    # не пишем, только в тело письма.
    mailer.send(
        to=email_norm,
        subject="Код подтверждения — поддержка МИСИС",
        body_text=(
            f"Ваш код подтверждения: {code}\n"
            f"Код действителен {CODE_TTL_SEC // 60} минут.\n"
            "Если вы не запрашивали код, просто проигнорируйте это письмо."
        ),
        body_html=(
            f"<p>Ваш код подтверждения: <b>{code}</b></p>"
            f"<p>Код действителен {CODE_TTL_SEC // 60} минут.</p>"
            "<p>Если вы не запрашивали код, просто проигнорируйте это письмо.</p>"
        ),
    )

    return {
        "user_type": user_type,
        "ttl_sec": CODE_TTL_SEC,
        "resend_after_sec": RESEND_COOLDOWN_SEC,
    }


def verify_code(email: str, code: str, now: float | None = None) -> dict:
    """Проверяет код.

    Успех → {'token', 'user_type', 'expires_at'}.
    Иначе поднимает InvalidCode / CodeExpired / TooManyAttempts.
    """
    now = _now() if now is None else now
    email_norm = _normalize_email(email)

    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT * FROM codes WHERE email = ? ORDER BY created_at DESC LIMIT 1",
            (email_norm,),
        ).fetchone()

        if row is None:
            raise CodeExpired("no code was requested for this email")

        if row["status"] == "burned":
            raise TooManyAttempts()

        if row["status"] == "used":
            # Повторное использование уже погашенного кода — не проходит.
            raise InvalidCode(attempts_left=0)

        if now - row["created_at"] > CODE_TTL_SEC:
            raise CodeExpired()

        if row["attempts"] >= MAX_ATTEMPTS:
            conn.execute("UPDATE codes SET status = 'burned' WHERE id = ?", (row["id"],))
            conn.commit()
            raise TooManyAttempts()

        expected_hash = _hash_code(code, row["salt"])
        if hmac.compare_digest(expected_hash, row["code_hash"]):
            conn.execute("UPDATE codes SET status = 'used' WHERE id = ?", (row["id"],))
            conn.commit()
            expires_at = now + TOKEN_TTL_SEC
            token = _make_token(email_norm, row["user_type"], expires_at)
            return {"token": token, "user_type": row["user_type"], "expires_at": expires_at}

        new_attempts = row["attempts"] + 1
        new_status = "burned" if new_attempts >= MAX_ATTEMPTS else "active"
        conn.execute(
            "UPDATE codes SET attempts = ?, status = ? WHERE id = ?",
            (new_attempts, new_status, row["id"]),
        )
        conn.commit()
        raise InvalidCode(attempts_left=max(MAX_ATTEMPTS - new_attempts, 0))


def _secret_key() -> bytes:
    key = os.environ.get("SECRET_KEY")
    if not key:
        raise RuntimeError("SECRET_KEY is not set in environment")
    return key.encode("utf-8")


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64decode(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _make_token(email: str, user_type: str, expires_at: float) -> str:
    payload = {"email": email, "user_type": user_type, "expires_at": expires_at}
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_b64 = _b64encode(payload_bytes)
    signature = hmac.new(_secret_key(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    signature_b64 = _b64encode(signature)
    return f"{payload_b64}.{signature_b64}"


def validate_token(token: str, now: float | None = None) -> dict | None:
    """Возвращает {'email', 'user_type', 'expires_at'} или None, если токен
    недействителен, испорчен или просрочен.
    """
    now = _now() if now is None else now

    try:
        payload_b64, signature_b64 = token.split(".")
        expected_signature = hmac.new(
            _secret_key(), payload_b64.encode("ascii"), hashlib.sha256
        ).digest()
        actual_signature = _b64decode(signature_b64)
        if not hmac.compare_digest(expected_signature, actual_signature):
            return None

        payload = json.loads(_b64decode(payload_b64).decode("utf-8"))
        email = payload["email"]
        user_type = payload["user_type"]
        expires_at = float(payload["expires_at"])

        if now > expires_at:
            return None

        return {"email": email, "user_type": user_type, "expires_at": expires_at}
    except Exception:
        # Любая проблема формата/подписи/данных — токен считается недействительным,
        # а не поводом упасть с исключением наружу.
        return None
