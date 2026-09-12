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
    sent_to     TEXT,            -- почта отключена, поле держим для истории записей
    mail_status TEXT,            -- то же самое: не используется, но не мигрируем старые строки
    status      TEXT NOT NULL DEFAULT 'new',  -- new | in_progress | answered | closed
    created_at  TEXT NOT NULL
);

-- Переписка по обращению: почты нет, ответ сотрудника и уточнение студента
-- живут здесь, а не улетают письмом (см. Decision log в README).
CREATE TABLE IF NOT EXISTS ticket_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL,
    author      TEXT NOT NULL,   -- 'operator' | 'user'
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_drafts_request ON drafts(request_id);
CREATE INDEX IF NOT EXISTS idx_ticket_messages_ticket ON ticket_messages(ticket_id, created_at);
"""

# Допустимые статусы обращения — используется и для валидации входа в API,
# и как единый источник правды вместо разбросанных строковых литералов.
TICKET_STATUSES = ("new", "in_progress", "answered", "closed")


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
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Точечные ALTER TABLE для баз, созданных до статусов и переписки.

    На сервере база уже с данными: CREATE TABLE IF NOT EXISTS не добавляет
    столбец в существующую таблицу, поэтому докатываем миграцию отдельно и
    идемпотентно — проверяем PRAGMA table_info, а не ловим ошибку "duplicate
    column", иначе на чистой базе (без tickets вовсе) можно словить другую
    ошибку раньше, чем схема вообще создалась.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
    if "status" not in columns:
        conn.execute("ALTER TABLE tickets ADD COLUMN status TEXT NOT NULL DEFAULT 'new'")


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
    subject: str, body: str, sent_to: str | None = None, mail_status: str | None = None,
) -> None:
    """Кладёт обращение во внутреннюю очередь со статусом 'new'.

    sent_to/mail_status — наследие почтовой схемы, почта из /api/ticket/submit
    убрана (см. Decision log), но столбцы оставлены нетронутыми ради старых
    строк на сервере; новые обращения просто не заполняют mail_status.
    """
    with _conn() as conn:
        conn.execute(
            "INSERT INTO tickets (ticket_id, draft_id, email, anonymous, subject, body,"
            " sent_to, mail_status, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ticket_id, draft_id, email, int(anonymous), subject, body,
             sent_to, mail_status, "new", _now()),
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


# --- Статусы и переписка (замена почте, см. Decision log в README) ----------

_TICKET_JOIN_DRAFT = (
    "SELECT t.*, d.category AS category, d.priority AS priority, d.answers AS answers"
    " FROM tickets t LEFT JOIN drafts d ON d.draft_id = t.draft_id"
)


def _attach_messages(conn: sqlite3.Connection, data: dict) -> dict:
    """Достаёт переписку и производные поля (messages_count, last_message_at).

    Общий код для карточки одного обращения и для списков — иначе счётчик
    сообщений и дата последнего сообщения считались бы по-разному в разных
    ручках API.
    """
    rows = conn.execute(
        "SELECT author, text, created_at FROM ticket_messages"
        " WHERE ticket_id = ? ORDER BY created_at ASC",
        (data["ticket_id"],),
    ).fetchall()
    data["messages"] = [dict(r) for r in rows]
    data["messages_count"] = len(data["messages"])
    data["last_message_at"] = data["messages"][-1]["created_at"] if data["messages"] else data["created_at"]
    return data


def set_ticket_status(ticket_id: str, status: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE tickets SET status = ? WHERE ticket_id = ?", (status, ticket_id))


def add_message(ticket_id: str, author: str, text: str) -> dict:
    """author: 'operator' | 'user'. Возвращает добавленное сообщение."""
    created_at = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO ticket_messages (ticket_id, author, text, created_at) VALUES (?,?,?,?)",
            (ticket_id, author, text, created_at),
        )
    return {"author": author, "text": text, "created_at": created_at}


def get_ticket_card(ticket_id: str) -> dict | None:
    """Полная карточка обращения: поля тикета + категория/приоритет черновика
    (для маршрутизации адресата) + переписка."""
    with _conn() as conn:
        row = conn.execute(_TICKET_JOIN_DRAFT + " WHERE t.ticket_id = ?", (ticket_id,)).fetchone()
        if row is None:
            return None
        data = _row(row, json_fields=("answers",))
        return _attach_messages(conn, data)


def list_tickets(status: str | None = None, limit: int = 50) -> list[dict]:
    """Обращения для консоли сотрудника, свежие сверху.

    status=None — без фильтра (соответствует 'all' в контракте API).
    """
    query = _TICKET_JOIN_DRAFT
    params: list = []
    if status:
        query += " WHERE t.status = ?"
        params.append(status)
    query += " ORDER BY t.created_at DESC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(query, params).fetchall()
        return [_attach_messages(conn, _row(r, json_fields=("answers",))) for r in rows]


def tickets_by_email(email: str) -> list[dict]:
    """Все обращения студента/абитуриента по подтверждённой почте, свежие сверху."""
    query = _TICKET_JOIN_DRAFT + " WHERE t.email = ? ORDER BY t.created_at DESC"
    with _conn() as conn:
        rows = conn.execute(query, (email,)).fetchall()
        return [_attach_messages(conn, _row(r, json_fields=("answers",))) for r in rows]


def ticket_counts() -> dict:
    """Сколько обращений в каждом статусе — для /api/health."""
    with _conn() as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM tickets GROUP BY status").fetchall()
    return {row["status"]: row["n"] for row in rows}


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
