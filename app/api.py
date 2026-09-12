"""HTTP-слой по контрактам из docs/API.md.

Здесь нет ни одной строчки, решающей что-то содержательно: разбор делает analyze,
решение принимает router, тексты собирает compose. api.py только принимает запрос,
зовёт их по очереди и складывает результат в формат контракта.

Модули nyonless (profile, schedule) могут быть ещё не написаны — приложение обязано
подниматься и без них, просто соответствующие эндпоинты честно отвечают 503.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app import analyze as analyze_mod
from app import auth, compose, config, faq, mailer, rules, router, storage

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

ROLES = {"applicant", "student", "teacher"}
CATEGORY_RU = {
    "admission": "Поступление", "study": "Учёба", "campus_life": "Студенческая жизнь",
    "account": "Доступы", "documents": "Документы", "other": "Прочее",
}


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
_RELEVANCE_FLOOR = 3.0


def _confidence(score: float) -> float:
    return round(score / (score + _CONFIDENCE_K), 2) if score > 0 else 0.0


def _relevant(found: list[dict]) -> list[dict]:
    if not found:
        return []
    cutoff = max(found[0].get("score", 0.0) * _RELEVANCE_RATIO, _RELEVANCE_FLOOR)
    return [f for f in found if f.get("score", 0.0) >= cutoff][:2]


# --- 1. Разбор обращения -----------------------------------------------------

class AskIn(BaseModel):
    text: str = Field(min_length=1, max_length=8000)
    role: str
    chat_id: str | None = None


@app.post("/api/ask")
def ask(body: AskIn) -> dict:
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
def ticket_draft(body: DraftIn) -> dict:
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
    subject = _subject(category, priority, drafted["subject_hint"])
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


def _subject(category: str, priority: str, gist: str, ticket_id: str | None = None) -> str:
    head = f"[MISIS-SUPPORT] {CATEGORY_RU.get(category, 'Прочее')} / {priority}"
    return f"{head} / {ticket_id} — {gist}" if ticket_id else f"{head} / {gist}"


# В черновике тема уже собрана по формату, а при отправке её пересобирают с номером
# обращения. Без снятия старого заголовка тема обрастает префиксом дважды.
_SUBJECT_RE = re.compile(r"^\[MISIS-SUPPORT\][^/]*/[^/]*/\s*(?:MISIS-\d{4}-\d+\s+—\s*)?(.*)$")


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
    return {"ok": True, **result}


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

    email = None
    if not body.anonymous:
        email = _token_email(f"Bearer {body.token or ''}")["email"]

    ticket_id = storage.next_ticket_id()
    subject = _subject(draft["category"], draft["priority"],
                       _gist(draft["subject"]), ticket_id)
    header = (f"Заявитель: {email}" if email else
              "Анонимное обращение — обратная связь невозможна")
    text = f"{header}\nНомер обращения: {ticket_id}\n\n{draft['body']}"

    sent_to = config.SUPPORT_EMAIL or "support@misis.ru"
    result = mailer.send(sent_to, subject, text)
    storage.save_ticket(ticket_id=ticket_id, draft_id=body.draft_id, email=email,
                        anonymous=body.anonymous, subject=subject, body=text,
                        sent_to=sent_to, mail_status=result["status"])
    return {"ticket_id": ticket_id, "subject": subject, "sent_to": sent_to,
            "mail_status": result["status"], "anonymous": body.anonymous}


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
    return {
        "status": "ok",
        "mail_mode": config.MAIL_MODE,
        "llm": "ready" if config.GROQ_API_KEY else "no_key",
        "faq_items": sum(len(faq.load(r)) for r in ROLES),
        "modules": {"profile": profile_mod is not None,
                    "schedule": schedule_mod is not None},
    }


_web = Path(__file__).resolve().parent.parent / "web"
if (_web / "index.html").exists():
    app.mount("/", StaticFiles(directory=str(_web), html=True), name="web")
