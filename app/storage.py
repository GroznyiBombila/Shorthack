"""SQLite: обращения, черновики, trace.

Одна база на всё приложение, файл лежит в томе (config.DB_PATH). Схема создаётся при
старте и никогда не мигрируется — за шесть часов проще пересоздать базу, чем писать
миграции.

Модули авторизации (auth, profile) держат свои таблицы в этой же базе, но своими
средствами: общий здесь только файл, не код.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from app import config

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id  TEXT PRIMARY KEY,
    chat_id     TEXT,
    role        TEXT NOT NULL,
    text        TEXT NOT NULL,
    analysis    TEXT,            -- JSON разбора модели
    decision    TEXT,            -- JSON решения роутера
    trace       TEXT,            -- JSON: что решила модель, что решил код
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    draft_id    TEXT PRIMARY KEY,
    request_id  TEXT NOT NULL,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    category    TEXT,
    priority    TEXT,
    missing     TEXT,            -- JSON списка недостающих полей
    answers     TEXT,            -- JSON уже полученных уточнений
    ready       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    ticket_id   TEXT PRIMARY KEY,
    draft_id    TEXT NOT NULL,
    email       TEXT,            -- NULL у анонимного обращения
    anonymous   INTEGER NOT NULL DEFAULT 0,
    subject     TEXT NOT NULL,
    body        TEXT NOT NULL,
    sent_to     TEXT,
    mail_status TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drafts_request ON drafts(request_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(3)}"


@contextmanager
def _conn() -> Iterator[sqlite3.Connection]:
    # Одно соединение на операцию: uvicorn ходит из нескольких потоков, а SQLite
    # по умолчанию к этому не готов. Блокировка снимает гонку на запись.
    with _lock:
        conn = sqlite3.connect(config.DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init() -> None:
    config.ensure_dirs()
    with _conn() as conn:
        conn.executescript(SCHEMA)


# --- Обращения ---------------------------------------------------------------

def save_request(
    *, role: str, text: str, chat_id: str | None,
    analysis: Any = None, decision: Any = None, trace: dict | None = None,
) -> str:
    request_id = _new_id("r")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO requests (request_id, chat_id, role, text, analysis, decision,"
            " trace, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (request_id, chat_id, role, text,
             _dump(analysis), _dump(decision), _dump(trace), _now()),
        )
    return request_id


def get_request(request_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
    return _row(row, json_fields=("analysis", "decision", "trace"))


# --- Черновики ---------------------------------------------------------------

def save_draft(
    *, request_id: str, subject: str, body: str, category: str | None,
    priority: str | None, missing: list | None, answers: dict | None, ready: bool,
) -> str:
    draft_id = _new_id("d")
    with _conn() as conn:
        conn.execute(
            "INSERT INTO drafts (draft_id, request_id, subject, body, category, priority,"
            " missing, answers, ready, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (draft_id, request_id, subject, body, category, priority,
             _dump(missing or []), _dump(answers or {}), int(ready), _now()),
        )
    return draft_id


def update_draft(draft_id: str, *, subject: str, body: str,
                 missing: list, answers: dict, ready: bool) -> None:
    """Повторное уточнение не плодит черновики: id остаётся прежним (контракт API)."""
    with _conn() as conn:
        conn.execute(
            "UPDATE drafts SET subject=?, body=?, missing=?, answers=?, ready=?"
            " WHERE draft_id=?",
            (subject, body, _dump(missing), _dump(answers), int(ready), draft_id),
        )


def get_draft(draft_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
    return _row(row, json_fields=("missing", "answers"))


# --- Отправленные обращения --------------------------------------------------

def next_ticket_id() -> str:
    """MISIS-2026-0007: сквозная нумерация по году, видна человеку в письме."""
    year = datetime.now(timezone.utc).year
    with _conn() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM tickets WHERE ticket_id LIKE ?", (f"MISIS-{year}-%",)
        ).fetchone()["n"]
    return f"MISIS-{year}-{count + 1:04d}"


def save_ticket(
    *, ticket_id: str, draft_id: str, email: str | None, anonymous: bool,
    subject: str, body: str, sent_to: str | None, mail_status: str,
) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO tickets (ticket_id, draft_id, email, anonymous, subject, body,"
            " sent_to, mail_status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (ticket_id, draft_id, email, int(anonymous), subject, body,
             sent_to, mail_status, _now()),
        )


def get_ticket(ticket_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
        ).fetchone()
    return _row(row)


def ticket_by_draft(draft_id: str) -> dict | None:
    """Уже отправленное обращение по черновику: защита от повторной отправки."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE draft_id = ? ORDER BY created_at LIMIT 1",
            (draft_id,),
        ).fetchone()
    return _row(row)


def recent_tickets(limit: int = 20) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tickets ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row(r) for r in rows]  # type: ignore[misc]


# --- Мелочи ------------------------------------------------------------------

def _dump(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "__dict__") and not isinstance(value, dict):
        value = vars(value)
    return json.dumps(value, ensure_ascii=False, default=str)


def _row(row: sqlite3.Row | None, json_fields: tuple[str, ...] = ()) -> dict | None:
    if row is None:
        return None
    data = dict(row)
    for field in json_fields:
        if data.get(field):
            try:
                data[field] = json.loads(data[field])
            except json.JSONDecodeError:
                pass  # сохраняем как есть: битый JSON в базе не повод ронять ответ
    return data
