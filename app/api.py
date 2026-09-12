"""HTTP-слой по контрактам из docs/API.md.

Здесь нет ни одной строчки, решающей что-то содержательно: разбор делает analyze,
решение принимает router, тексты собирает compose. api.py только принимает запрос,
зовёт их по очереди и складывает результат в формат контракта.

Модули nyonless (profile, schedule) могут быть ещё не написаны — приложение обязано
подниматься и без них, просто соответствующие эндпоинты честно отвечают 503.
"""
from __future__ import annotations

import glob
import hmac
import logging
import re
from email import policy
from email.parser import BytesParser
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import analyze as analyze_mod
from app import auth, compose, config, faq, limits, mailer, rules, router, routing, storage

logging.basicConfig(level=config.LOG_LEVEL)
log = logging.getLogger(__name__)

# Необязательные модули: их пишет другой человек, до готовности живём без них.
try:
    from app import profile as profile_mod
except ImportError:  # pragma: no cover
    profile_mod = None
try:
    from app import schedule as schedule_mod
except ImportError:  # pragma: no cover
    schedule_mod = None

app = FastAPI(title="Помощник поддержки МИСИС", docs_url="/docs")

# Схему создаём при импорте, а не только в startup: так модуль одинаково пригоден
# и под uvicorn, и в тестах через TestClient без контекстного менеджера.
config.ensure_dirs()
storage.init()

# Фронт собирают отдельно и открывают локально (file:// или свой порт), а API
# живёт на сервере — без этого браузер не даст сделать ни одного запроса.
# Пускаем всех: за этим API нет ни личных данных без токена, ни денег, а на
# хакатоне возня с белым списком источников стоит дороже, чем даёт.
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

ROLES = {"applicant", "student", "teacher"}
CATEGORY_RU = {
    "admission": "Поступление", "study": "Учёба", "campus_life": "Студенческая жизнь",
    "account": "Доступы", "documents": "Документы", "other": "Прочее",
}
ROLE_RU = {"applicant": "поступающий", "student": "студент", "teacher": "преподаватель"}


# --- Общее -------------------------------------------------------------------

def fail(status: int, code: str, message: str, **details) -> HTTPException:
    return HTTPException(status_code=status, detail={
        "code": code, "message": message, "details": details or {}})


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {
        "code": "error", "message": str(exc.detail), "details": {}}
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


@app.on_event("startup")
def startup() -> None:
    config.ensure_dirs()
    storage.init()
    _seed_volume()
    if schedule_mod is not None and hasattr(schedule_mod, "load_all"):
        try:
            schedule_mod.load_all(str(config.SCHEDULE_DIR))
        except Exception as exc:  # расписание не должно ронять весь сервис
            log.warning("расписание не загрузилось: %s", exc)


def _seed_volume() -> None:
    """Пустой том наполняем тем, что лежит в репозитории.

    Нужно ровно один раз, на чистом сервере: иначе контейнер поднимется без базы
    знаний и без расписаний. Существующие файлы не перезаписываем — на сервере
    данные могут быть свежее, чем в образе.
    """
    for name in ("faq", "schedule"):
        src, dst = config.REPO_DATA_DIR / name, config.DATA_DIR / name
        if not src.is_dir():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            if item.is_file() and not (dst / item.name).exists():
                (dst / item.name).write_bytes(item.read_bytes())


def _token_email(authorization: str | None) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise fail(401, "invalid_token", "Нужна подтверждённая почта")
    data = auth.validate_token(authorization.split(" ", 1)[1].strip())
    if not data:
        raise fail(401, "invalid_token", "Ссылка устарела, подтвердите почту заново")
    return data


# Поиск отдаёт сырой вес совпадений (десятки), а контракт обещает 0..1. Сжимаем
# монотонно и без насыщения в единицу: 100%% уверенности у keyword-поиска не бывает.
_CONFIDENCE_K = 10.0
# Во втором и третьем найденном разделе часто лежит случайное совпадение по одному
# слову. В ответ и в промпт пускаем только то, что сопоставимо с лучшим попаданием.
_RELEVANCE_RATIO = 0.5
_RELEVANCE_FLOOR = rules.MIN_FAQ_SCORE


def _confidence(score: float) -> float:
    return round(score / (score + _CONFIDENCE_K), 2) if score > 0 else 0.0


def _relevant(found: list[dict]) -> list[dict]:
    if not found:
        return []
    cutoff = max(found[0].get("score", 0.0) * _RELEVANCE_RATIO, _RELEVANCE_FLOOR)
    return [f for f in found if f.get("score", 0.0) >= cutoff][:2]


def _guard(request: Request) -> None:
    """Заслон на эндпоинты, которые тратят квоту модели.

    Сервис выложен наружу, а лимит Groq общий на всю команду: без ограничителя
    любой, кто узнает адрес, выжигает суточную квоту и защита проекта встаёт.
    """
    key = limits.client_key(request.headers.get("x-forwarded-for"),
                            request.client.host if request.client else None)
    try:
        limits.check(key)
    except limits.RateLimited as exc:
        raise fail(429, "rate_limited", str(exc), retry_after_sec=exc.retry_after_sec)


# --- 1. Разбор обращения -----------------------------------------------------

class AskIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    role: str
    chat_id: str | None = None


@app.post("/api/ask")
def ask(body: AskIn, request: Request) -> dict:
    _guard(request)
    if body.role not in ROLES:
        raise fail(400, "invalid_role", "Неизвестная роль", role=body.role)

    analysis = analyze_mod.analyze(body.text, body.role)      # [LLM] текст -> структура
    decision = router.decide(analysis, body.role)             # [код] структура -> действие

    questions: list[dict] = []
    to_compose: list[dict] = []
    for i, question in enumerate(analysis.questions or [body.text], start=1):
        qid = f"q{i}"
        found = _relevant(faq.search(question, body.role, limit=3)) if decision.action != "reject" else []
        articles = [a for a in (faq.get_article(f["id"], body.role) for f in found) if a]
        has_answer = bool(articles) and decision.action == "answer"
        questions.append({
            "id": qid,
            "question": question,
            "category": analysis.category,
            "audience": analysis.audience,
            "answer": None,
            "confidence": _confidence(found[0].get("score", 0.0)) if found else 0.0,
            "suggest_ticket": not has_answer and decision.action != "reject",
            "_sources": [{"title": a.get("title", ""), "url": a.get("url", "")} for a in articles],
        })
        if has_answer:
            to_compose.append({"qid": qid, "question": question, "articles": articles})

    summaries = compose.answers(to_compose)                   # [LLM] структура -> текст
    for q in questions:
        summary = summaries.get(q["id"])
        if summary:
            q["answer"] = {"summary": summary, "sources": q["_sources"]}
        q.pop("_sources")

    trace = dict(decision.trace)
    trace.update({
        "llm_calls": 1 + int(bool(to_compose)),
        "faq_hits": sum(1 for q in questions if q["answer"]),
        "degraded": analysis.degraded,
    })
    request_id = storage.save_request(
        role=body.role, text=body.text, chat_id=body.chat_id,
        analysis=analysis, decision=decision, trace=trace)

    if decision.action == "reject":
        overall = "rejected"
    elif any(q["suggest_ticket"] for q in questions):
        overall = "suggest_ticket"
    else:
        overall = "answered"
    return {"request_id": request_id, "questions": questions,
            "overall_action": overall, "trace": trace}


@app.get("/api/faq/article/{article_id}")
def faq_article(article_id: str, role: str = "student") -> dict:
    article = faq.get_article(article_id, role)
    if not article:
        raise fail(404, "not_found", "Раздел не найден")
    return article


# --- 2. Черновик обращения ---------------------------------------------------

class DraftIn(BaseModel):
    request_id: str
    question_ids: list[str] = []
    answers: dict[str, str] = {}
    draft_id: str | None = None


@app.post("/api/ticket/draft")
def ticket_draft(body: DraftIn, request: Request) -> dict:
    _guard(request)
    req = storage.get_request(body.request_id)
    if not req:
        raise fail(404, "request_not_found", "Обращение не найдено")

    analysis = req.get("analysis") or {}
    category = analysis.get("category", "other")
    known = dict(body.answers)
    missing = rules.missing_fields(category, known)
    priority = rules.priority(category, analysis.get("tone", "neutral"),
                              analysis.get("keywords", []))
    questions = analysis.get("questions") or [req["text"]]

    drafted = compose.ticket(role=req["role"], category=category, priority=priority,
                             questions=questions, known=known, original=req["text"])
    # Институт — как и приоритет/адресат, решение кода: если студент уже назвал
    # институт (ключ "институт" из REQUIRED_FIELDS в app/rules.py), маршрутизируем
    # письмо не в общий учебный отдел, а в конкретный деканат.
    institute = known.get("институт")
    subject = _subject(category, priority, drafted["subject_hint"], institute=institute)
    ready = not missing

    if body.draft_id and storage.get_draft(body.draft_id):
        storage.update_draft(body.draft_id, subject=subject, body=drafted["body"],
                             missing=missing, answers=known, ready=ready)
        draft_id = body.draft_id
    else:
        draft_id = storage.save_draft(
            request_id=body.request_id, subject=subject, body=drafted["body"],
            category=category, priority=priority, missing=missing,
            answers=known, ready=ready)

    return {"draft_id": draft_id, "subject": subject, "body": drafted["body"],
            "missing_fields": [{"field": f, "question": _ask_for(f)} for f in missing],
            "ready": ready}


# Тема — контракт для сотрудника, который разбирает почтовый ящик: адресат
# первым, чтобы фильтром/глазом сразу понять, кому пересылать письмо.
_MAX_SUBJECT_LEN = 200


def _subject(category: str, priority: str, gist: str, ticket_id: str | None = None,
            institute: str | None = None) -> str:
    to = routing.recipient(category, institute)
    head = f"[MISIS-SUPPORT] {to} / {CATEGORY_RU.get(category, 'Прочее')} / {priority}"
    prefix = f"{head} / {ticket_id} — " if ticket_id else f"{head} / "

    # 200 символов режем за счёт сути, а не служебной части: адресат, категория,
    # приоритет и номер обращения нужны сотруднику в любом случае, а суть и так
    # дублируется в теле письма.
    budget = _MAX_SUBJECT_LEN - len(prefix)
    gist = gist.strip()
    if budget <= 0:
        return prefix[:_MAX_SUBJECT_LEN]
    if len(gist) > budget:
        gist = (gist[:budget - 1].rstrip() + "…") if budget > 1 else "…"
    return prefix + gist


# В черновике тема уже собрана по формату, а при отправке её пересобирают с номером
# обращения. Без снятия старого заголовка тема обрастает префиксом дважды.
# Сегментов теперь три (адресат / категория / приоритет) перед сутью — было два.
_SUBJECT_RE = re.compile(
    r"^\[MISIS-SUPPORT\][^/]*/[^/]*/[^/]*/\s*(?:MISIS-\d{4}-\d+\s+—\s*)?(.*)$"
)


def _gist(subject: str) -> str:
    match = _SUBJECT_RE.match(subject)
    return (match.group(1).strip() if match else subject.strip()) or subject.strip()


_FIELD_QUESTIONS = {
    "fio": "Как вас зовут — фамилия, имя, отчество?",
    "group": "В какой вы учебной группе?",
    "institute": "В каком вы институте?",
    "login": "Какой логин вы вводите?",
    "error": "Что именно пишет система при ошибке?",
    "doc_type": "Какая справка нужна?",
    "purpose": "Куда её нужно предоставить?",
    "programme": "На какое направление подаёте документы?",
}


def _ask_for(field: str) -> str:
    return _FIELD_QUESTIONS.get(field, f"Уточните, пожалуйста: {field}")


# --- 3-4. Подтверждение почты ------------------------------------------------

class EmailIn(BaseModel):
    email: str


class VerifyIn(BaseModel):
    email: str
    code: str


def _last_demo_code(email: str) -> str | None:
    """AUTH_SHOW_CODE: достаём код тем же способом, что и тесты app/auth.py
    (см. tests/test_auth.py::_last_code_from_outbox) — из последнего письма в
    outbox, не трогая логику auth.py. Почта администратором домена запрещена,
    поэтому это единственный способ узнать код в демо-режиме.
    """
    safe = "".join(c if c.isalnum() or c in "@._-+" else "_" for c in email.strip().lower())
    files = sorted(glob.glob(str(Path(mailer._outbox_dir()) / f"*_{safe}_*.eml")))
    if not files:
        return None
    with open(files[-1], "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)
    part = msg.get_body(preferencelist=("plain",))
    if part is None:
        return None
    match = re.search(r"код подтверждения:\s*(\d{6})", part.get_content(), re.IGNORECASE)
    return match.group(1) if match else None


@app.post("/api/auth/request-code")
def request_code(body: EmailIn, request: Request) -> dict:
    ip = request.client.host if request.client else "0.0.0.0"
    try:
        result = auth.request_code(body.email, ip)
    except auth.InvalidEmail:
        raise fail(400, "invalid_email", "Проверьте адрес почты")
    except auth.RateLimited as exc:
        raise fail(429, "rate_limited", "Слишком часто, подождите немного",
                   retry_after_sec=getattr(exc, "retry_after_sec", 60))
    response = {"ok": True, **result}
    if config.AUTH_SHOW_CODE:
        demo_code = _last_demo_code(body.email)
        if demo_code:
            response["demo_code"] = demo_code
    return response


@app.post("/api/auth/verify")
def verify(body: VerifyIn) -> dict:
    try:
        return auth.verify_code(body.email, body.code)
    except auth.InvalidCode as exc:
        raise fail(400, "invalid_code", "Неверный код",
                   attempts_left=getattr(exc, "attempts_left", 0))
    except auth.CodeExpired:
        raise fail(410, "code_expired", "Код истёк, запросите новый")
    except auth.TooManyAttempts:
        raise fail(429, "too_many_attempts", "Слишком много попыток, запросите новый код")


# --- 5. Отправка обращения ---------------------------------------------------

class SubmitIn(BaseModel):
    draft_id: str
    token: str | None = None
    anonymous: bool = False


@app.post("/api/ticket/submit")
def submit(body: SubmitIn) -> dict:
    draft = storage.get_draft(body.draft_id)
    if not draft:
        raise fail(404, "draft_not_found", "Черновик не найден")
    if not draft["ready"]:
        raise fail(409, "draft_not_ready", "Не хватает данных для обращения")

    # Повторная отправка того же черновика возвращает уже созданное обращение.
    # Иначе двойной клик на кнопке — это две записи в очереди с разными
    # номерами на одно и то же обращение, а заодно дешёвый способ засорить
    # очередь чужим draft_id.
    existing = storage.ticket_by_draft(body.draft_id)
    if existing:
        return {"ticket_id": existing["ticket_id"], "subject": existing["subject"],
                "delivery": "inbox", "anonymous": bool(existing["anonymous"]),
                "duplicate": True}

    email = None
    if not body.anonymous:
        email = _token_email(f"Bearer {body.token or ''}")["email"]

    # Институт нужен и для темы, и для тела — берём из уже собранных ответов
    # черновика (тот же ключ "институт", что и при сборке черновика).
    institute = (draft.get("answers") or {}).get("институт")
    req = storage.get_request(draft["request_id"])
    role = (req or {}).get("role")

    ticket_id = storage.next_ticket_id()
    to_whom = routing.recipient(draft["category"], institute)
    subject = _subject(draft["category"], draft["priority"],
                       _gist(draft["subject"]), ticket_id, institute=institute)

    # Блок для сотрудника, который разбирает очередь: кому по сути адресовано,
    # номер, кто спрашивает и приоритет — те же первые строки, что раньше были
    # в письме, только оно теперь никуда не уходит (см. Decision log в README:
    # администратор домена запретил пароли приложений, письмом послать нечем).
    who = (f"{ROLE_RU.get(role, role or 'роль не определена')}, {email}" if email
           else "анонимное — ответить невозможно")
    header_lines = [
        f"Кому: {to_whom}",
        f"Номер обращения: {ticket_id}",
        f"Приоритет: {draft['priority']}",
        f"Заявитель: {who}",
    ]
    text = "\n".join(header_lines) + "\n\n" + draft["body"]

    # mailer.send здесь больше не вызывается: обращение остаётся во внутренней
    # очереди со статусом 'new', сотрудник отвечает в консоли (см. ниже), а
    # студент видит ответ через /api/my/tickets и /api/ticket/{id}.
    storage.save_ticket(ticket_id=ticket_id, draft_id=body.draft_id, email=email,
                        anonymous=body.anonymous, subject=subject, body=text,
                        sent_to=to_whom)
    return {"ticket_id": ticket_id, "subject": subject, "delivery": "inbox",
            "anonymous": body.anonymous}


# --- Общие помощники карточки обращения (консоль сотрудника + сторона студента)

def _recipient_of(ticket: dict) -> str:
    """Адресат по сути — та же таблица routing.py, что и при отправке; институт
    достаём из answers черновика (join сделан в storage), а не храним второй раз."""
    category = ticket.get("category") or "other"
    institute = (ticket.get("answers") or {}).get("институт")
    return routing.recipient(category, institute)


def _ticket_summary(ticket: dict) -> dict:
    return {
        "ticket_id": ticket["ticket_id"],
        "subject": ticket["subject"],
        "status": ticket["status"],
        "priority": ticket.get("priority"),
        "category": ticket.get("category"),
        "recipient": _recipient_of(ticket),
        "anonymous": bool(ticket["anonymous"]),
        "email": ticket.get("email"),
        "created_at": ticket["created_at"],
        "last_message_at": ticket.get("last_message_at") or ticket["created_at"],
        "messages_count": ticket.get("messages_count", len(ticket.get("messages") or [])),
    }


def _ticket_card(ticket: dict) -> dict:
    card = _ticket_summary(ticket)
    card["body"] = ticket["body"]
    card["messages"] = ticket.get("messages") or []
    return card


def _check_ticket_owner(ticket: dict, authorization: str | None) -> None:
    """Именное обращение доступно только своей подтверждённой почте; анонимное —
    всем, у кого есть сам ticket_id (он и есть секрет, см. контракт п.4)."""
    if ticket.get("anonymous"):
        return
    session = None
    if authorization and authorization.lower().startswith("bearer "):
        session = auth.validate_token(authorization.split(" ", 1)[1].strip())
    if not session or session.get("email") != ticket.get("email"):
        raise fail(403, "forbidden", "Обращение недоступно")


# --- Консоль сотрудника поддержки --------------------------------------------
# Почта отключена (см. Decision log): вместо ответа письмом сотрудник отвечает
# здесь, студент видит ответ в своём интерфейсе. Квоту модели эти ручки не
# тратят — под _guard не ставим.

def _require_operator(x_operator_token: str | None) -> None:
    if not config.OPERATOR_TOKEN:
        # Не 401: без настроенного токена нельзя даже сравнить его с чем-то,
        # а пускать всех в этом случае — незащищённая консоль по недосмотру.
        raise fail(503, "not_configured", "Консоль сотрудника не настроена")
    if not x_operator_token or not hmac.compare_digest(x_operator_token, config.OPERATOR_TOKEN):
        raise fail(401, "invalid_token", "Неверный токен сотрудника")


@app.get("/api/operator/tickets")
def operator_tickets(status: str = "all", limit: int = 50,
                     x_operator_token: str | None = Header(default=None)) -> dict:
    _require_operator(x_operator_token)
    if status != "all" and status not in storage.TICKET_STATUSES:
        raise fail(400, "invalid_status", "Неизвестный статус", requested=status)
    tickets = storage.list_tickets(None if status == "all" else status, limit=limit)
    return {"tickets": [_ticket_summary(t) for t in tickets]}


@app.get("/api/operator/tickets/{ticket_id}")
def operator_ticket(ticket_id: str, x_operator_token: str | None = Header(default=None)) -> dict:
    _require_operator(x_operator_token)
    ticket = storage.get_ticket_card(ticket_id)
    if not ticket:
        raise fail(404, "ticket_not_found", "Обращение не найдено")
    return _ticket_card(ticket)


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


@app.post("/api/operator/tickets/{ticket_id}/reply")
def operator_reply(ticket_id: str, body: ReplyIn,
                   x_operator_token: str | None = Header(default=None)) -> dict:
    _require_operator(x_operator_token)
    if not storage.get_ticket(ticket_id):
        raise fail(404, "ticket_not_found", "Обращение не найдено")
    storage.add_message(ticket_id, "operator", body.text)
    storage.set_ticket_status(ticket_id, "answered")
    return _ticket_card(storage.get_ticket_card(ticket_id))


class TicketStatusIn(BaseModel):
    status: str


@app.post("/api/operator/tickets/{ticket_id}/status")
def operator_status(ticket_id: str, body: TicketStatusIn,
                    x_operator_token: str | None = Header(default=None)) -> dict:
    _require_operator(x_operator_token)
    if body.status not in storage.TICKET_STATUSES:
        raise fail(400, "invalid_status", "Неизвестный статус", requested=body.status)
    if not storage.get_ticket(ticket_id):
        raise fail(404, "ticket_not_found", "Обращение не найдено")
    storage.set_ticket_status(ticket_id, body.status)
    return _ticket_card(storage.get_ticket_card(ticket_id))


# --- Сторона студента: свои обращения и переписка ----------------------------
# Чтение своих обращений и дозапись уточнения модель не зовут — под _guard
# не ставим (см. п.6 постановки: квоту тратят только /api/ask и черновик).

@app.get("/api/my/tickets")
def my_tickets(authorization: str | None = Header(default=None)) -> dict:
    session = _token_email(authorization)
    tickets = storage.tickets_by_email(session["email"])
    return {"tickets": [_ticket_card(t) for t in tickets]}


@app.get("/api/ticket/{ticket_id}")
def ticket_get(ticket_id: str, authorization: str | None = Header(default=None)) -> dict:
    ticket = storage.get_ticket_card(ticket_id)
    if not ticket:
        raise fail(404, "ticket_not_found", "Обращение не найдено")
    _check_ticket_owner(ticket, authorization)
    return _ticket_card(ticket)


class TicketMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@app.post("/api/ticket/{ticket_id}/message")
def ticket_message(ticket_id: str, body: TicketMessageIn,
                   authorization: str | None = Header(default=None)) -> dict:
    ticket = storage.get_ticket_card(ticket_id)
    if not ticket:
        raise fail(404, "ticket_not_found", "Обращение не найдено")
    _check_ticket_owner(ticket, authorization)
    storage.add_message(ticket_id, "user", body.text)
    storage.set_ticket_status(ticket_id, "in_progress")
    return _ticket_card(storage.get_ticket_card(ticket_id))


# --- 5a-5b. Профиль и расписание (модули nyonless) ---------------------------

@app.get("/api/profile/fields")
def profile_fields(role: str = "student") -> dict:
    if profile_mod is None:
        raise fail(503, "not_ready", "Профили ещё не подключены")
    return {"fields": profile_mod.required_fields(role)}


@app.get("/api/profile")
def profile_get(authorization: str | None = Header(default=None)) -> dict:
    if profile_mod is None:
        raise fail(503, "not_ready", "Профили ещё не подключены")
    session = _token_email(authorization)
    values = profile_mod.get_profile(session["email"]) or {}
    return {"role": session.get("user_type", "student"),
            "filled": bool(values), "values": values}


class ProfileIn(BaseModel):
    values: dict[str, str]


@app.post("/api/profile")
def profile_set(body: ProfileIn, authorization: str | None = Header(default=None)) -> dict:
    if profile_mod is None:
        raise fail(503, "not_ready", "Профили ещё не подключены")
    session = _token_email(authorization)
    saved = profile_mod.save_profile(
        session["email"], session.get("user_type", "student"), body.values)
    return {"ok": True, "values": saved or body.values}


@app.get("/api/schedule")
def schedule(date: str, authorization: str | None = Header(default=None)) -> dict:
    if schedule_mod is None:
        raise fail(503, "not_ready", "Расписание ещё не подключено")
    session = _token_email(authorization)
    values = (profile_mod.get_profile(session["email"]) if profile_mod else None) or {}
    from datetime import date as date_cls
    try:
        day = date_cls.fromisoformat(date)
    except ValueError:
        raise fail(400, "invalid_date", "Дата в формате ГГГГ-ММ-ДД")

    if session.get("user_type") == "teacher":
        lessons = schedule_mod.for_teacher(values.get("name", ""), day)
        holder = {"teacher": values.get("name", "")}
    else:
        group = values.get("group")
        if not group:
            raise fail(409, "profile_incomplete", "Нужна ваша учебная группа")
        subgroup = int(values["subgroup"]) if values.get("subgroup", "").isdigit() else None
        lessons = schedule_mod.for_group(group, day, subgroup)
        holder = {"group": group}

    return {"date": date, "day": _WEEKDAYS[day.weekday()],
            "week": schedule_mod.week_parity(day), **holder, "lessons": lessons}


_WEEKDAYS = ["Понедельник", "Вторник", "Среда", "Четверг",
             "Пятница", "Суббота", "Воскресенье"]


# --- 6. Служебное ------------------------------------------------------------

@app.get("/api/health")
def health() -> dict:
    counts = storage.ticket_counts()
    return {
        "status": "ok",
        "mail_mode": config.MAIL_MODE,
        "llm": "ready" if config.GROQ_API_KEY else "no_key",
        "faq_items": sum(len(faq.load(r)) for r in ROLES),
        "modules": {"profile": profile_mod is not None,
                    "schedule": schedule_mod is not None},
        "tickets": {"new": counts.get("new", 0),
                   "in_progress": counts.get("in_progress", 0),
                   "answered": counts.get("answered", 0)},
        "limits": limits.snapshot(),
    }


_web = Path(__file__).resolve().parent.parent / "web"
if (_web / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(_web), html=True), name="web")
