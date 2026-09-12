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
  "subject": "[MISIS-SUPPORT] Учебный отдел / Документы / P3 / Нужен ли оригинал аттестата",
  "body": "текст карточки заявки",
  "missing_fields": [ { "field": "programme", "question": "На какое направление подаёте документы?" } ],
  "ready": false
}
```

`ready: true` — недостающих полей нет, можно отправлять. Повторный вызов с заполненным
`answers` возвращает тот же `draft_id` и `ready: true`.

Тема уже собрана по формату `[MISIS-SUPPORT] {адресат} / {Категория} / {приоритет} / {суть}`.
`{адресат}` — человекочитаемое имя того, кому по сути адресовано обращение («Деканат ИТКН»,
«Приёмная комиссия», «Техподдержка», «Учебный отдел», «Студгородок», запасной вариант —
«Поддержка МИСИС»). Это решает код (`app/routing.py`) по категории и, если он уже известен из
`answers`, институту — а не модель. Реального ящика деканата нет, письмо всё равно физически
уходит на единый корпоративный адрес (см. раздел 5); адресат в теме — подсказка сотруднику,
кому переслать или на что ответить.

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

**Демо-режим `AUTH_SHOW_CODE`.** Корпоративная почта не может отправлять письма (см. раздел 5а
и Decision log в README), поэтому код подтверждения студенту в реальности не доедет. Если в
окружении включена переменная `AUTH_SHOW_CODE` (`1`/`true`/`yes`/`on`), ответ дополнительно
содержит `"demo_code": "418203"` — сам код, только что записанный в `var/outbox/`. **Это
исключительно режим показа/демонстрации для защиты — в боевом окружении `AUTH_SHOW_CODE`
должен быть выключен** (переменная не задана), иначе любой человек с доступом к API получает
чужой код подтверждения в открытом виде.

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
  "delivery": "inbox",
  "anonymous": false
}
```

При отправке в тему добавляется номер обращения: `[MISIS-SUPPORT] {адресат} / {Категория} /
{приоритет} / {ticket_id} — {суть}`. Тема не превышает 200 символов: при необходимости режется
только суть, служебная часть (адресат, категория, приоритет, номер) не трогается.

**Почта отключена.** Администратор домена `edu.misis.ru` (Google Workspace) запретил пароли
приложений, поэтому отправить письмо от корпоративного ящика мы не можем в принципе — `POST
/api/ticket/submit` больше не вызывает `mailer.send` и никуда не «уходит». Обращение сохраняется
во внутренней очереди со статусом `new` (см. раздел 7), `delivery: "inbox"` — единственное
подтверждение того, что оно принято. Адресат по сути (Деканат/Приёмная комиссия/Учебный отдел/
Студгородок/Техподдержка) по-прежнему вычисляется тем же кодом (`app/routing.py`) и виден
сотруднику в карточке обращения (поле `recipient`, раздел 7) — просто он больше не пишется в тему
письма ради письма, а показывается в консоли. Модуль `app/mailer.py` никуда не делся: он всё ещё
шлёт код подтверждения (раздел 3).

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

`GET /api/health` →

```json
{
  "status": "ok", "mail_mode": "mock", "llm": "ready", "faq_items": 42,
  "modules": { "profile": true, "schedule": true },
  "tickets": { "new": 3, "in_progress": 1, "answered": 5 },
  "limits": { "day_used": 12, "day_limit": 600, "active_keys": 4 }
}
```

`tickets` — сколько обращений сейчас в каждом из незакрытых статусов (см. раздел 7); `closed`
сюда не входит. `limits` — снимок `limits.snapshot()`, тот же, что использует ограничитель
запросов к модели.

## 7. Консоль сотрудника поддержки

Почта отключена (см. раздел 5), поэтому сотрудник отвечает не письмом, а здесь. Каждый запрос
несёт заголовок `X-Operator-Token`, сверяемый с переменной окружения `OPERATOR_TOKEN` через
`hmac.compare_digest` (защита от тайминг-атак). Если `OPERATOR_TOKEN` не задан в окружении,
все ручки отвечают `503 not_configured` — это осознанное решение: без настроенного значения
сравнивать токен не с чем, и превращать это в «пускать всех» нельзя. Ручки консоли квоту
модели не тратят.

`GET /api/operator/tickets?status=new|in_progress|answered|closed|all&limit=50`

```json
{
  "tickets": [
    {
      "ticket_id": "MISIS-2026-0007", "subject": "[MISIS-SUPPORT] ...",
      "status": "new", "priority": "P3", "category": "documents",
      "recipient": "Учебный отдел", "anonymous": false, "email": "ivanov@edu.misis.ru",
      "created_at": "2026-09-12T10:00:00+00:00", "last_message_at": "2026-09-12T10:00:00+00:00",
      "messages_count": 0
    }
  ]
}
```

Свежие обращения — сверху (`created_at DESC`). `status=all` (по умолчанию) — без фильтра.
Ошибки: `503 not_configured`, `401 invalid_token`, `400 invalid_status` (неизвестное значение
`status` в query).

`GET /api/operator/tickets/{ticket_id}` → те же поля, что в списке, плюс `body` (текст обращения)
и `messages: [{ "author": "operator"|"user", "text": "...", "created_at": "..." }]` по
возрастанию времени. Ошибки: те же плюс `404 ticket_not_found`.

`POST /api/operator/tickets/{ticket_id}/reply` `{ "text": "Готово, справка у вас в личном кабинете" }`
→ добавляет сообщение автора `operator`, переводит статус в `answered`, возвращает обновлённую
карточку (как в `GET .../{ticket_id}`). Ошибки: те же плюс `404 ticket_not_found`.

`POST /api/operator/tickets/{ticket_id}/status` `{ "status": "in_progress" }` → меняет статус,
возвращает обновлённую карточку. `400 invalid_status` на неизвестное значение, `404
ticket_not_found`.

## 8. Сторона студента: свои обращения

`GET /api/my/tickets` с заголовком `Authorization: Bearer <token>` (см. раздел 4) → обращения
этой подтверждённой почты, каждое — полная карточка (`status`, `priority`, `category`,
`recipient`, `body`, `messages`), свежие сверху. Ручка квоту модели не тратит.

`GET /api/ticket/{ticket_id}` → карточка одного обращения. Доступ: анонимное обращение открыто
всем, у кого есть сам `ticket_id` (он и есть секрет, см. раздел 5); именное — только по
Bearer-токену той же почты, иначе `403 forbidden`. Ошибки: `404 ticket_not_found`, `403
forbidden`.

`POST /api/ticket/{ticket_id}/message` `{ "text": "Уточняю: группа ИТКН-24-1" }` — студент
дописывает уточнение в своё обращение (правила доступа те же, что у `GET`), статус
обращения возвращается в `in_progress`. Языковая модель не вызывается. Ошибки: те же плюс `404
ticket_not_found`.

## Формат карточки обращения (поле `body`)

Почта отключена (раздел 5), поэтому этот текст никуда не отправляется — это `body` в
`GET /api/operator/tickets/{ticket_id}` и `GET /api/ticket/{ticket_id}`, тот же формат, что
раньше уходил письмом:

Первые строки: кому по сути адресовано (`recipient`), номер обращения, приоритет, роль и
подтверждённая почта заявителя (или пометка «анонимное — ответить невозможно»). Дальше —
категория, суть, извлечённые данные и исходный текст обращения.
