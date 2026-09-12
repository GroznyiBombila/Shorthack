"""
app/mailer.py

Отправка писем в двух режимах:
  - mock (по умолчанию): письмо не уходит в сеть, а сохраняется файлом
    в var/outbox/{timestamp}_{to}.eml — удобно для демо и офлайн-тестов.
  - smtp: реальная отправка, параметры берутся из окружения. Порт 465 —
    SMTP_SSL (соединение сразу зашифровано), порт 587 — обычный SMTP с
    STARTTLS (сначала открытое соединение, потом апгрейд до TLS). Поддержаны
    оба, потому что у корпоративных ящиков они оба в ходу, а раньше в коде
    был жёстко зашит только SMTP_SSL, при этом .env.example предлагал 587 —
    на этом порту SMTP_SSL просто не соединится. Любая ошибка перехватывается
    и возвращается в ответе, наружу исключения не летят.

Схема отправки — "ящик пишет сам себе": SMTP_USER, MAIL_FROM и SUPPORT_EMAIL
это один и тот же корпоративный адрес (см. .env.example и app/config.py).
Реального адреса деканата/приёмной комиссии у нас нет, поэтому получатель
по умолчанию — тот же ящик, что и отправитель, а кому письмо адресовано по
сути — написано в теме (см. app/routing.py) и в первых строках тела письма.
Кому реально отвечать — Reply-To на подтверждённую почту заявителя, если
она есть.

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

_DEFAULT_OUTBOX_DIR = "var/outbox"


def _outbox_dir() -> str:
    # Путь читается на каждой отправке, а не на импорте: в контейнере том лежит
    # не в var/ (см. DATA_DIR в app/config.py), а тесты подменяют его на время теста.
    override = os.environ.get("OUTBOX_DIR")
    if override:
        return override
    try:
        from app import config
        return str(config.OUTBOX_DIR)
    except Exception:
        return _DEFAULT_OUTBOX_DIR


def _mail_from(mode: str) -> str:
    """Адрес отправителя — единственный корпоративный ящик из окружения.

    В smtp-режиме тихий фallback на чужой домен (как было раньше, на
    no-reply@example.com) означал бы письмо, которое реальный SMTP-сервер
    просто не примет от чужого имени, — лучше явная ошибка сразу. В mock-режиме
    почта никуда не уходит, а демо не должно требовать настроенного SMTP,
    поэтому подставляем локальную заглушку.
    """
    value = os.environ.get("MAIL_FROM", "").strip()
    if value:
        return value
    if mode == "smtp":
        raise RuntimeError(
            "MAIL_FROM не задан, а MAIL_MODE=smtp — почте некого указать отправителем"
        )
    return "mock-sender@localhost"


def _build_message(to: str, subject: str, body_text: str, body_html: str | None,
                   mail_from: str, reply_to: str | None) -> EmailMessage:
    # policy.default умеет само кодировать не-ASCII заголовки (RFC 2047),
    # поэтому кириллица в теме приходит читаемой без ручной возни с Header().
    msg = EmailMessage(policy=policy.default)
    msg["From"] = mail_from
    msg["To"] = to
    if reply_to:
        # Ставим Reply-To, только когда почта заявителя подтверждена кодом:
        # для анонимного обращения отвечать некуда, и подсовывать сотруднику
        # нерабочую кнопку "Ответить" хуже, чем её отсутствие.
        msg["Reply-To"] = reply_to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()

    msg.set_content(body_text)
    if body_html:
        msg.add_alternative(body_html, subtype="html")

    return msg


def _write_to_outbox(msg: EmailMessage, to: str) -> None:
    outbox = _outbox_dir()
    os.makedirs(outbox, exist_ok=True)
    # time.time() с дробной частью + короткий случайный суффикс — чтобы
    # два письма подряд в тестах не перетёрли друг друга одним и тем же именем.
    ts = time.strftime("%Y%m%dT%H%M%S") + f".{int(time.time() * 1000) % 1000:03d}"
    safe_to = "".join(c if c.isalnum() or c in "@._-+" else "_" for c in to)
    suffix = uuid.uuid4().hex[:6]
    filename = os.path.join(outbox, f"{ts}_{safe_to}_{suffix}.eml")
    with open(filename, "wb") as f:
        f.write(msg.as_bytes())


def send(to: str | None = None, subject: str = "", body_text: str = "",
        body_html: str | None = None, reply_to: str | None = None) -> dict:
    """Отправляет письмо.

    `to` необязателен: если не передан, письмо уходит на тот же ящик, что и
    отправитель (MAIL_FROM) — это и есть схема "ящик пишет сам себе" из
    app/routing.py, применяемая при отправке обращений. `reply_to` — почта
    заявителя, если она подтверждена; для анонимного обращения не передаётся.

    Возвращает {'status', 'message_id', 'error'}, status одно из
    'sent' | 'mocked' | 'failed'. Никогда не бросает исключение —
    любая ошибка отправки превращается в {'status': 'failed', ...}.
    """
    mode = os.environ.get("MAIL_MODE", "mock")

    try:
        mail_from = _mail_from(mode)
        recipient = to or mail_from
        msg = _build_message(recipient, subject, body_text, body_html, mail_from, reply_to)
        message_id = msg["Message-ID"]

        if mode == "smtp":
            host = os.environ["SMTP_HOST"]
            port = int(os.environ.get("SMTP_PORT", "465"))
            user = os.environ["SMTP_USER"]
            password = os.environ["SMTP_PASSWORD"]

            context = ssl.create_default_context()
            if port == 465:
                with smtplib.SMTP_SSL(host, port, timeout=10, context=context) as server:
                    server.login(user, password)
                    server.send_message(msg)
            else:
                # 587 и прочие — соединение открытое, шифруем STARTTLS перед login.
                with smtplib.SMTP(host, port, timeout=10) as server:
                    server.starttls(context=context)
                    server.login(user, password)
                    server.send_message(msg)

            return {"status": "sent", "message_id": message_id, "error": None}

        # Режим mock — штатный демо-режим, не заглушка на случай ошибки.
        _write_to_outbox(msg, recipient)
        return {"status": "mocked", "message_id": message_id, "error": None}

    except Exception as exc:  # noqa: BLE001 - осознанно широкий перехват
        return {"status": "failed", "message_id": None, "error": str(exc)}
