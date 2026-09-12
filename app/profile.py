"""Профиль пользователя: институт/группа/подгруппа (студент) или кафедра/ФИО
(преподаватель) — единственное, чего не хватает /api/schedule, чтобы понять,
чьё расписание показывать (см. docs/API.md, раздел 5a). Абитуриенту профиль
не нужен вовсе — своего расписания у него нет.

Простое SQLite-хранилище: одна строка на email, значения одним полем JSON.
Своя таблица в общей базе (config.DB_PATH), как и договорено в app/storage.py —
общий там только файл, не код (тот же приём, что и в app/auth.py). LLM и сеть
здесь не участвуют вообще: назначение модуля исчерпывается тем, что человек
сам вписал в форму, плюс списком групп/институтов из уже загруженного
расписания — чтобы выпадашка предлагала настоящие, а не придуманные значения.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from app import config

try:
    from app import schedule as schedule_mod
except ImportError:  # pragma: no cover - расписание может быть ещё не подключено
    schedule_mod = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    email       TEXT PRIMARY KEY,
    role        TEXT NOT NULL,
    values_json TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

# Поля, которые реально сохраняются для роли — всё остальное из тела запроса
# отбрасывается, чтобы случайный лишний ключ от фронта не осел в базе мусором.
_ROLE_FIELDS: dict[str, tuple[str, ...]] = {
    "student": ("institute", "group", "subgroup"),
    "teacher": ("department", "name"),
    "applicant": (),
}

_FALLBACK_INSTITUTES = ["ИТКН"]
_FALLBACK_GROUPS = ["ББИ-26-6-1"]


def _connect() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _schedule_list(name: str, fallback: list[str]) -> list[str]:
    """Список для выпадашки не должен ронять форму, если расписание не готово."""
    if schedule_mod is None:
        return fallback
    try:
        values = getattr(schedule_mod, name)()
    except Exception:  # noqa: BLE001
        return fallback
    return list(values) if values else fallback


def required_fields(role: str) -> list[dict]:
    """Состав полей профиля — интерфейс его не зашивает (docs/API.md, 5a)."""
    if role == "teacher":
        return [
            {"name": "department", "label": "Кафедра", "type": "text",
             "required": True, "options": []},
            {"name": "name", "label": "Фамилия и инициалы", "type": "text",
             "required": True, "options": []},
        ]
    if role == "student":
        return [
            {"name": "institute", "label": "Институт", "type": "select",
             "required": True, "options": _schedule_list("list_institutes", _FALLBACK_INSTITUTES)},
            {"name": "group", "label": "Учебная группа", "type": "select",
             "required": True, "options": _schedule_list("list_groups", _FALLBACK_GROUPS)},
            {"name": "subgroup", "label": "Подгруппа", "type": "select",
             "required": False, "options": ["1", "2"]},
        ]
    return []  # abiturient: профиль не нужен, форму не показываем


def get_profile(email: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT values_json FROM profiles WHERE email = ?", (email,)
        ).fetchone()
    if row is None:
        return None
    try:
        return json.loads(row["values_json"])
    except json.JSONDecodeError:
        return None


def save_profile(email: str, role: str, values: dict[str, str]) -> dict:
    """Сохраняет только поля, относящиеся к роли; остальное из values отбрасывается.

    Валидацию значений (существует ли такая группа) намеренно не делаем строгой:
    /api/profile не оборачивает исключения этого модуля в 400, а падать 500-й
    на опечатке в названии группы хуже, чем сохранить её как есть.
    """
    allowed = _ROLE_FIELDS.get(role, ())
    cleaned = {
        key: str(value).strip()
        for key, value in values.items()
        if key in allowed and str(value).strip()
    }
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO profiles (email, role, values_json, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(email) DO UPDATE SET"
            " role=excluded.role, values_json=excluded.values_json, updated_at=excluded.updated_at",
            (email, role, json.dumps(cleaned, ensure_ascii=False),
             datetime.now(timezone.utc).isoformat(timespec="seconds")),
        )
        conn.commit()
    return cleaned
