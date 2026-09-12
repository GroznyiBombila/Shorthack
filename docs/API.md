# Контракты API (зафиксированы, менять только с уведомлением команды)

База: `http://localhost:8000`. Все тела — JSON, кодировка UTF-8.
Ошибки: `{"error": {"code": "...", "message": "...", "details": {...}}}` + HTTP-код.

## 1. Разбор обращения

`POST /api/ask`

```json
{
  "text": "Здравствуйте! Когда подавать документы на бюджет и нужен ли оригинал аттестата?",
  "role": "applicant",
  "chat_id": "c_44f1"
}
```

`role` — обязательное: `applicant` | `student` | `teacher`. Выбирается ползунком на главном
экране и определяет, в какой части базы знаний искать. `chat_id` — необязательный, для
продолжения диалога.

```json
{
  "request_id": "r_8f3a1c",
  "questions": [
    {
      "id": "q1",
      "question": "Сроки подачи документов на бюджетные места",
      "category": "admission",
      "audience": "applicant",
      "answer": {
        "summary": "Короткая выжимка на 2-3 предложения по сути вопроса.",
        "sources": [ { "title": "Приёмная кампания 2026", "url": "https://misis.ru/..." } ]
      },
      "confidence": 0.82,
      "suggest_ticket": false
    },
    {
      "id": "q2",
      "question": "Нужен ли оригинал аттестата при подаче",
      "category": "documents",
      "audience": "applicant",
      "answer": null,
      "confidence": 0.21,
      "suggest_ticket": true
    }
  ],
  "overall_action": "suggest_ticket",
  "trace": { "llm_calls": 2, "faq_hits": 3, "rules_fired": ["low_confidence->ticket"] }
}
```

- `category`: `admission` | `study` | `campus_life` | `account` | `documents` | `other`
- `audience`: `applicant` | `student` | `teacher` | `both`
- `overall_action`: `answered` — на все вопросы есть ответ; `suggest_ticket` — хотя бы по одному нужна заявка.
- `trace.tools_used` перечисляет вызванные инструменты: `search_faq`, `get_article`, `fetch_page`.

## 1a. Содержание раздела базы знаний

`GET /api/faq/article/{id}` → `{ "id": "adm-001", "title": "...", "url": "...", "full_text": "..." }`

Внутренний инструмент агента: в промпт попадают только ключевые слова и заголовки, а текст
раздела подгружается этим вызовом уже после того, как раздел выбран.

## 2. Черновик заявки

`POST /api/ticket/draft`

```json
{ "request_id": "r_8f3a1c", "question_ids": ["q2"], "answers": { "programme": "Информатика" } }
```

```json
{
  "draft_id": "d_11b2",
  "subject": "[MISIS-SUPPORT] Документы / P3 / Нужен ли оригинал аттестата",
  "body": "текст карточки заявки",
  "missing_fields": [ { "field": "programme", "question": "На какое направление подаёте документы?" } ],
  "ready": false
}
```

`ready: true` — недостающих полей нет, можно отправлять. Повторный вызов с заполненным
`answers` возвращает тот же `draft_id` и `ready: true`.

## 3. Запрос кода подтверждения

`POST /api/auth/request-code`

```json
{ "email": "ivanov@edu.misis.ru" }
```

```json
{ "ok": true, "user_type": "student", "ttl_sec": 600, "resend_after_sec": 60 }
```

- `user_type`: `student` — домен `misis.ru` / `edu.misis.ru`; `applicant` — любой другой.
- Ошибки: `400 invalid_email`, `429 rate_limited` (+ `retry_after_sec`).

## 4. Проверка кода

`POST /api/auth/verify`

```json
{ "email": "ivanov@edu.misis.ru", "code": "418203" }
```

```json
{ "token": "eyJ...", "user_type": "student", "expires_at": "2026-09-12T14:05:00Z" }
```

- Ошибки: `400 invalid_code` (+ `attempts_left`), `410 code_expired`, `429 too_many_attempts`.

## 5. Отправка заявки

`POST /api/ticket/submit`

```json
{ "draft_id": "d_11b2", "token": "eyJ...", "anonymous": false }
```

`token` обязателен, если `anonymous: false`. При `anonymous: true` токен не нужен: обращение
уходит в поддержку с пометкой «без обратной связи», ответить на него будет некуда.

```json
{
  "ticket_id": "MISIS-2026-0007",
  "subject": "[MISIS-SUPPORT] Документы / P3 / MISIS-2026-0007 — Нужен ли оригинал аттестата",
  "sent_to": "support@misis.ru",
  "mail_status": "sent",
  "anonymous": false
}
```

- `mail_status`: `sent` — письмо ушло по SMTP; `mocked` — записано в `var/outbox/`.
- Ошибки: `401 invalid_token`, `409 draft_not_ready`, `404 draft_not_found`.

## 5a. Профиль

Состав полей приходит с сервера — интерфейс его не зашивает.

`GET /api/profile/fields?role=student` → 

```json
{ "fields": [
  { "name": "institute", "label": "Институт", "type": "select", "required": true, "options": ["ИТКН", "ИНМиН"] },
  { "name": "group",     "label": "Учебная группа", "type": "select", "required": true, "options": ["ББИ-26-6-1"] },
  { "name": "subgroup",  "label": "Подгруппа", "type": "select", "required": false, "options": ["1", "2"] }
] }
```

Для `teacher` — `department` (Кафедра, text) и `name` (Фамилия и инициалы, text).
Для `applicant` — пустой список: профиль не нужен, форму не показываем.

`GET /api/profile` с заголовком `Authorization: Bearer <token>` →
`{ "role": "student", "filled": true, "values": { "institute": "ИТКН", "group": "ББИ-26-6-1", "subgroup": "1" } }`

`filled: false` — профиль ещё не заполняли, показываем форму.

`POST /api/profile` `{ "values": { ... } }` + токен → `{ "ok": true, "values": { ... } }`
Ошибки: `401 invalid_token`, `400 invalid_field` (+ `details.field`).

## 5b. Расписание

`GET /api/schedule?date=2026-09-14` + токен. Группа и подгруппа берутся из профиля,
преподавателю — по фамилии из профиля. Передавать их в запросе не нужно.

```json
{
  "date": "2026-09-14",
  "day": "Понедельник",
  "week": "upper",
  "group": "ББИ-26-6-1",
  "lessons": [
    {
      "pair": 2, "time_start": "10:50", "time_end": "12:25",
      "subject": "Введение в специальность", "kind": "lab",
      "teacher": "Неворошкин В. А.", "room": "Л-812-УВЦ", "subgroup": null
    }
  ]
}
```

- `kind`: `lecture` | `practice` | `lab` | `other`
- `week`: `upper` — числитель, `lower` — знаменатель.
- Пустой `lessons` — валидный ответ: в этот день занятий нет. Это не ошибка.
- Ошибки: `401 invalid_token`, `409 profile_incomplete` (профиль не заполнен, группы нет),
  `404 schedule_not_found` (нет файла для этого института).

Языковая модель в этом эндпоинте не участвует вообще — только разбор таблицы.

## 6. Служебное

`GET /api/health` → `{"status":"ok","mail_mode":"mock","llm":"ready","faq_items":42}`

## Формат письма

Тема: `[MISIS-SUPPORT] {Категория} / {Приоритет} / {ticket_id} — {краткая суть}`

Тело: роль заявителя, подтверждённая почта (или пометка «анонимное обращение, обратная связь
невозможна»), категория, приоритет, суть, извлечённые данные, исходный текст обращения и то,
что помощник уже успел показать пользователю.
