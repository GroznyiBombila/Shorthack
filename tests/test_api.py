"""Регрессионные тесты HTTP-слоя по контрактам из docs/API.md.

Сеть не задействована: complete_json подменяется в обоих модулях, которые её
импортировали по имени (app.analyze и app.compose), а httpx.post в фикстуре
client превращён в ошибку — реальный вызов модели сломает тест, а не пройдёт молча.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

# Разбор расписания: категория study, аудитория student — попадание в std-001.
SCHEDULE_ANALYSIS = {
    "questions": ["Где посмотреть расписание занятий"],
    "category": "study",
    "audience": "student",
    "keywords": ["расписание", "расписание занятий"],
    "missing": [],
    "tone": "neutral",
    "off_topic": False,
}

# Справка: в базе знаний ответа нет, зато у категории documents есть обязательные поля.
CERTIFICATE_ANALYSIS = {
    "questions": ["Нужна справка об обучении для военкомата"],
    "category": "documents",
    "audience": "student",
    "keywords": ["справка об обучении", "военкомат"],
    "missing": ["ФИО"],
    "tone": "neutral",
    "off_topic": False,
}

# Один ответ модели обслуживает и compose.answers, и compose.ticket: у них общий
# complete_json, а лишние ключи каждая из функций просто не читает.
COMPOSE_REPLY = {
    "answers": {f"q{i}": "Расписание смотрите в личном кабинете." for i in range(1, 6)},
    "subject_hint": "Нужна справка об обучении",
    "body": "Карточка обращения: нужна справка об обучении.",
}


def stub_llm(monkeypatch, analysis: dict, compose_reply: dict = COMPOSE_REPLY) -> None:
    monkeypatch.setattr("app.analyze.complete_json", lambda *a, **kw: analysis)
    monkeypatch.setattr("app.compose.complete_json", lambda *a, **kw: compose_reply)


def stub_llm_down(monkeypatch) -> None:
    from app.llm_client import LLMUnavailable

    def unavailable(*args, **kwargs):
        raise LLMUnavailable("groq недоступен")

    monkeypatch.setattr("app.analyze.complete_json", unavailable)
    monkeypatch.setattr("app.compose.complete_json", unavailable)


def ask(client, text: str, role: str = "student") -> dict:
    response = client.post("/api/ask", json={"text": text, "role": role})
    assert response.status_code == 200, response.text
    return response.json()


def ready_draft(client, monkeypatch) -> str:
    """Черновик, доведённый до ready: true — стартовая точка тестов отправки."""
    stub_llm(monkeypatch, CERTIFICATE_ANALYSIS)
    request_id = ask(client, "Нужна справка об обучении для военкомата")["request_id"]

    first = client.post("/api/ticket/draft", json={"request_id": request_id}).json()
    filled = {field["field"]: "значение" for field in first["missing_fields"]}
    second = client.post("/api/ticket/draft", json={
        "request_id": request_id, "draft_id": first["draft_id"], "answers": filled}).json()
    assert second["ready"] is True
    return second["draft_id"]


# --- Служебное ---------------------------------------------------------------

def test_health_reports_llm_and_knowledge_base_state(client):
    body = client.get("/api/health").json()

    assert body["status"] == "ok"
    assert body["llm"] in {"ready", "no_key"}
    assert body["faq_items"] > 0
    assert set(body["modules"]) == {"profile", "schedule"}


# --- Разбор обращения --------------------------------------------------------

def test_ask_answers_from_knowledge_base_with_sources(client, monkeypatch):
    stub_llm(monkeypatch, SCHEDULE_ANALYSIS)

    body = ask(client, "Где посмотреть расписание занятий?")

    assert body["overall_action"] == "answered"
    question = body["questions"][0]
    assert question["answer"]["summary"]
    assert question["answer"]["sources"]
    assert all(source["title"] for source in question["answer"]["sources"])


def test_ask_returns_confidence_as_fraction_not_raw_search_score(client, monkeypatch):
    stub_llm(monkeypatch, SCHEDULE_ANALYSIS)

    body = ask(client, "Где посмотреть расписание занятий?")

    # Регресс: наружу утекал сырой вес поиска (десятки), контракт обещает 0..1.
    for question in body["questions"]:
        assert isinstance(question["confidence"], (int, float))
        assert 0.0 <= question["confidence"] <= 1.0


def test_ask_rejects_unknown_role(client, monkeypatch):
    stub_llm(monkeypatch, SCHEDULE_ANALYSIS)

    response = client.post("/api/ask", json={"text": "Вопрос", "role": "rector"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_role"


def test_ask_degrades_instead_of_failing_when_llm_is_down(client, monkeypatch):
    stub_llm_down(monkeypatch)

    response = client.post("/api/ask", json={
        "text": "Где посмотреть расписание занятий?", "role": "student"})

    # Деградация без 500 — ключевое свойство: помощник обязан отвечать и без модели.
    assert response.status_code == 200, response.text
    assert response.json()["trace"]["degraded"] is True


# --- Черновик обращения ------------------------------------------------------

def test_draft_for_unknown_request_returns_404(client):
    response = client.post("/api/ticket/draft", json={"request_id": "r_нет_такого"})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "request_not_found"


def test_draft_keeps_same_id_when_missing_fields_are_filled_in(client, monkeypatch):
    stub_llm(monkeypatch, CERTIFICATE_ANALYSIS)
    request_id = ask(client, "Нужна справка об обучении для военкомата")["request_id"]

    first = client.post("/api/ticket/draft", json={
        "request_id": request_id, "question_ids": ["q1"]})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["ready"] is False
    assert first_body["missing_fields"]
    assert all(field["question"] for field in first_body["missing_fields"])

    filled = {field["field"]: "значение" for field in first_body["missing_fields"]}
    second = client.post("/api/ticket/draft", json={
        "request_id": request_id, "draft_id": first_body["draft_id"], "answers": filled})
    assert second.status_code == 200, second.text
    second_body = second.json()

    assert second_body["ready"] is True
    assert second_body["missing_fields"] == []
    # Контракт: уточнение не плодит черновики, id остаётся прежним.
    assert second_body["draft_id"] == first_body["draft_id"]


# --- Отправка обращения ------------------------------------------------------

def test_submit_anonymous_draft_creates_numbered_ticket(client, monkeypatch):
    draft_id = ready_draft(client, monkeypatch)

    response = client.post("/api/ticket/submit", json={
        "draft_id": draft_id, "anonymous": True})

    assert response.status_code == 200, response.text
    body = response.json()
    year = datetime.now(timezone.utc).year
    assert body["ticket_id"] == f"MISIS-{year}-0001"
    assert body["mail_status"] in {"mocked", "sent"}
    assert body["anonymous"] is True


def test_submit_subject_carries_prefix_and_ticket_id_once(client, monkeypatch):
    draft_id = ready_draft(client, monkeypatch)

    body = client.post("/api/ticket/submit", json={
        "draft_id": draft_id, "anonymous": True}).json()

    # Регресс: при пересборке темы с номером обращения префикс дублировался.
    assert body["subject"].count("[MISIS-SUPPORT]") == 1
    assert len(re.findall(re.escape(body["ticket_id"]), body["subject"])) == 1


def test_submit_rejects_draft_with_missing_fields(client, monkeypatch):
    stub_llm(monkeypatch, CERTIFICATE_ANALYSIS)
    request_id = ask(client, "Нужна справка об обучении для военкомата")["request_id"]
    draft = client.post("/api/ticket/draft", json={"request_id": request_id}).json()
    assert draft["ready"] is False

    response = client.post("/api/ticket/submit", json={
        "draft_id": draft["draft_id"], "anonymous": True})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "draft_not_ready"


def test_submit_unknown_draft_returns_404(client):
    response = client.post("/api/ticket/submit", json={
        "draft_id": "d_нет_такого", "anonymous": True})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "draft_not_found"


# --- Маршрутизация адресата и почта заявки -----------------------------------
# Схема "ящик пишет сам себе" (см. app/mailer.py, app/routing.py): реального
# адреса деканата нет, поэтому адресат по сути — в теме письма, а Reply-To —
# единственный работающий канал обратной связи с заявителем.

def _last_outbox_mail(outbox_dir):
    import glob
    from email import policy
    from email.parser import BytesParser

    files = sorted(glob.glob(str(outbox_dir / "*.eml")))
    assert files, "ожидалось письмо в outbox"
    with open(files[-1], "rb") as f:
        return BytesParser(policy=policy.default).parse(f)


def test_submit_confirmed_email_sets_reply_to(client, monkeypatch, api_env):
    import re

    draft_id = ready_draft(client, monkeypatch)

    code_request = client.post("/api/auth/request-code", json={"email": "student@misis.ru"})
    assert code_request.status_code == 200, code_request.text
    code_mail = _last_outbox_mail(api_env / "outbox")
    body = code_mail.get_body(preferencelist=("plain",)).get_content()
    code = re.search(r"код подтверждения:\s*(\d{6})", body, re.IGNORECASE).group(1)

    verify = client.post("/api/auth/verify", json={"email": "student@misis.ru", "code": code})
    assert verify.status_code == 200, verify.text
    token = verify.json()["token"]

    submit = client.post("/api/ticket/submit", json={
        "draft_id": draft_id, "token": token, "anonymous": False})
    assert submit.status_code == 200, submit.text

    ticket_mail = _last_outbox_mail(api_env / "outbox")
    assert ticket_mail["Reply-To"] == "student@misis.ru"


def test_submit_anonymous_has_no_reply_to(client, monkeypatch, api_env):
    draft_id = ready_draft(client, monkeypatch)

    submit = client.post("/api/ticket/submit", json={"draft_id": draft_id, "anonymous": True})
    assert submit.status_code == 200, submit.text

    ticket_mail = _last_outbox_mail(api_env / "outbox")
    assert ticket_mail["Reply-To"] is None


def test_subject_contains_recipient_and_stays_within_200_chars_even_with_huge_gist():
    # Юнит-уровень: длинный "хвост" темы (суть) должен резаться, а не служебная
    # часть (адресат / категория / приоритет / номер обращения).
    from app.api import _subject

    huge_gist = "очень длинный вопрос про справку " * 20
    subject = _subject("documents", "P3", huge_gist, ticket_id="MISIS-2026-0007")

    assert len(subject) <= 200
    assert subject.count("Учебный отдел") == 1
    assert subject.count("MISIS-2026-0007") == 1
    assert subject.startswith("[MISIS-SUPPORT] Учебный отдел / Документы / P3 / MISIS-2026-0007 — ")


def test_subject_routes_study_category_to_dean_office_when_institute_known():
    from app.api import _subject

    subject = _subject("study", "P3", "не пускает в личный кабинет", institute="ИТКН")

    assert "Деканат ИТКН" in subject


def test_gist_strips_new_four_segment_prefix_without_duplicating_it():
    # Регресс формата: адресат добавил четвёртый сегмент в тему, _gist обязан
    # снимать его целиком, иначе при пересборке темы префикс наклеится дважды.
    from app.api import _gist, _subject

    draft_subject = _subject("account", "P2", "не могу войти в личный кабинет")
    resubmitted = _subject("account", "P2", _gist(draft_subject), ticket_id="MISIS-2026-0001")

    assert resubmitted.count("[MISIS-SUPPORT]") == 1
    assert resubmitted.count("MISIS-2026-0001") == 1
    assert resubmitted.endswith("не могу войти в личный кабинет")


# --- Профиль и расписание ----------------------------------------------------

@pytest.mark.parametrize("path", ["/api/profile", "/api/schedule?date=2026-09-14"])
def test_optional_modules_answer_not_ready_or_demand_token(client, path):
    """Модули ещё пишутся: сейчас 503, после подключения — 401 без токена."""
    response = client.get(path)

    assert response.status_code in {200, 401, 409, 503}
    if response.status_code == 503:
        assert response.json()["error"]["code"] == "not_ready"
    elif response.status_code == 401:
        assert response.json()["error"]["code"] == "invalid_token"
