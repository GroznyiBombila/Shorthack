"""
app/mailer.py

Отправка писем в двух режимах:
  - mock (по умолчанию): письмо не уходит в сеть, а сохраняется файлом
    в var/outbox/{timestamp}_{to}.eml — удобно для демо и офлайн-тестов.
  - smtp: реальная отправка через smtplib.SMTP_SSL, параметры берутся
    из окружения. Любая ошибка перехватывается и возвращается в ответе,
    наружу исключения не летят.

Модуль не тянет ничего, кроме стандартной библиотеки, и не читает
app/auth.py — используется им, а не наоборот.
"""

from __future__ import annotations

import os
import smtplib
import ssl
import time
import uuid
from email import policy
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

_OUTBOX_DIR = "var/outbox"


def _build_message(to: str, subject: str, body_text: str, body_html: str | None) -> EmailMessage:
    # policy.default умеет само кодировать не-ASCII заголовки (RFC 2047),
    # поэтому кириллица в теме приходит читаемой без ручной возни с Header().
    msg = EmailMessage(policy=policy.default)
    msg["From"] = os.environ.get("MAIL_FROM", "no-reply@example.com")
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    return msg


def _write_to_outbox(msg: EmailMessage, to: str) -> None:
    os.makedirs(_OUTBOX_DIR, exist_ok=True)
    # time.time() с дробной частью + короткий случайный суффикс — чтобы
    # два письма подряд в тестах не перетёрли друг друга одним и тем же именем.
    ts = time.strftime("%Y%m%dT%H%M%S") + f".{int(time.time() * 1000) % 1000:03d}"
    safe_to = "".join(c if c.isalnum() or c in "@._-+" else "_" for c in to)
    suffix = uuid.uuid4().hex[:6]
    filename = os.path.join(_OUTBOX_DIR, f"{ts}_{safe_to}_{suffix}.eml")
    with open(filename, "wb") as f:
        f.write(msg.as_bytes())


def send(to: str, subject: str, body_text: str, body_html: str | None = None) -> dict:
    """Отправляет письмо.

    Возвращает {'status', 'message_id', 'error'}, status одно из
    'sent' | 'mocked' | 'failed'. Никогда не бросает исключение —
    любая ошибка отправки превращается в {'status': 'failed', ...}.
    """
    mode = os.environ.get("MAIL_MODE", "mock")

    try:
        msg = _build_message(to, subject, body_text, body_html)
        message_id = msg["Message-ID"]

        if mode == "smtp":
            host = os.environ["SMTP_HOST"]
            port = int(os.environ.get("SMTP_PORT", "465"))
            user = os.environ["SMTP_USER"]
            password = os.environ["SMTP_PASSWORD"]

            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, timeout=10, context=context) as server:
                server.login(user, password)
                server.send_message(msg)

            return {"status": "sent", "message_id": message_id, "error": None}

        # Режим mock — штатный демо-режим, не заглушка на случай ошибки.
        _write_to_outbox(msg, to)
        return {"status": "mocked", "message_id": message_id, "error": None}

    except Exception as exc:  # noqa: BLE001 - осознанно широкий перехват
        return {"status": "failed", "message_id": None, "error": str(exc)}
