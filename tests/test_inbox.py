"""Тесты внутренней очереди обращений (замена отправке почтой).

Почему очередь, а не письмо: администратор домена edu.misis.ru (Google
Workspace) запретил пароли приложений, поэтому слать почту от корпоративного
ящика мы не можем в принципе (см. Decision log в README). Обращение теперь
остаётся в SQLite, сотрудник отвечает в своей консоли, студент видит ответ
через свой интерфейс. Почта осталась только для кода подтверждения.
"""
from __future__ import annotations

import pytest

from app import config

# Тот же набор данных, что и в test_api.py: категория documents с обязательными
# полями — удобно получить ready-черновик за два вызова.
CERTIFICATE_ANALYSIS = {
    "questions": ["Нужна справка об обучении для военкомата"],
    "category": "documents",
    "audience": "student",
    "keywords": ["справка об обучении", "военкомат"],
    "missing": ["ФИО"],
    "tone": "neutral",
    "off_topic": False,
}

COMPOSE_REPLY = {
    "answers": {"q1": "Ответ из базы знаний."},
    "subject_hint": "Нужна справка об обучении",
    "body": "Карточка обращения: нужна справка об обучении.",
}


def stub_llm(monkeypatch, analysis: dict = CERTIFICATE_ANALYSIS) -> None:
    monkeypatch.setattr("app.analyze.complete_json", lambda *a, **kw: analysis)
    monkeypatch.setattr("app.compose.complete_json", lambda *a, **kw: COMPOSE_REPLY)


def submit_ticket(client, monkeypatch, *, anonymous: bool = True, token: str | None = None) -> str:
    """Доводит обращение до отправленного тикета, возвращает ticket_id."""
    stub_llm(monkeypatch)
    request_id = client.post("/api/ask", json={
        "text": "Нужна справка об обучении для военкомата", "role": "student"}).json()["request_id"]

    first = client.post("/api/ticket/draft", json={"request_id": request_id}).json()
    filled = {field["field"]: "значение" for field in first["missing_fields"]}
    draft = client.post("/api/ticket/draft", json={
        "request_id": request_id, "draft_id": first["draft_id"], "answers": filled}).json()
    assert draft["ready"] is True

    payload = {"draft_id": draft["draft_id"], "anonymous": anonymous}
    if token:
        payload["token"] = token
    submit = client.post("/api/ticket/submit", json=payload)
    assert submit.status_code == 200, submit.text
    return submit.json()["ticket_id"]


def confirmed_token(client, api_env, email: str) -> str:
    """Полный цикл подтверждения почты, код достаём из outbox — так же, как
    тесты app/auth.py и app/api.py достают его в демо-режиме."""
    import glob
    import re
    from email import policy
    from email.parser import BytesParser

    request = client.post("/api/auth/request-code", json={"email": email})
    assert request.status_code == 200, request.text

    files = sorted(glob.glob(str(api_env / "outbox" / "*.eml")))
    assert files, "ожидалось письмо с кодом в outbox"
    with open(files[-1], "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)
    body = msg.get_body(preferencelist=("plain",)).get_content()
    code = re.search(r"код подтверждения:\s*(\d{6})", body, re.IGNORECASE).group(1)

    verify = client.post("/api/auth/verify", json={"email": email, "code": code})
    assert verify.status_code == 200, verify.text
    return verify.json()["token"]


@pytest.fixture
def operator_token(monkeypatch):
    """Включает консоль сотрудника с известным тестам токеном."""
    token = "test-operator-token"
    monkeypatch.setattr(config, "OPERATOR_TOKEN", token)
    return token


# --- 1. Обращение попадает в очередь со статусом new -------------------------

def test_submit_puts_ticket_in_queue_with_new_status(client, monkeypatch, operator_token):
    ticket_id = submit_ticket(client, monkeypatch)

    response = client.get("/api/operator/tickets",
                          headers={"X-Operator-Token": operator_token})
    assert response.status_code == 200, response.text
    tickets = response.json()["tickets"]
    match = next(t for t in tickets if t["ticket_id"] == ticket_id)
    assert match["status"] == "new"


# --- 2. Консоль без токена -----------------------------------------------

def test_operator_console_without_operator_token_configured_is_503(client, monkeypatch):
    # OPERATOR_TOKEN не задан в окружении теста (api_env его не выставляет).
    monkeypatch.setattr(config, "OPERATOR_TOKEN", "")

    response = client.get("/api/operator/tickets")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "not_configured"


def test_operator_console_with_wrong_header_is_401_when_configured(client, operator_token):
    response = client.get("/api/operator/tickets")  # заголовок вовсе не передан

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"

    wrong = client.get("/api/operator/tickets",
                       headers={"X-Operator-Token": "wrong-token"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "invalid_token"


# --- 3. Ответ сотрудника меняет статус и виден студенту -----------------------

def test_operator_reply_changes_status_and_is_visible_to_student(
    client, monkeypatch, api_env, operator_token
):
    token = confirmed_token(client, api_env, "student@misis.ru")
    ticket_id = submit_ticket(client, monkeypatch, anonymous=False, token=token)

    reply = client.post(f"/api/operator/tickets/{ticket_id}/reply",
                        json={"text": "Справка будет готова завтра."},
                        headers={"X-Operator-Token": operator_token})
    assert reply.status_code == 200, reply.text
    assert reply.json()["status"] == "answered"

    card = client.get(f"/api/ticket/{ticket_id}",
                      headers={"Authorization": f"Bearer {token}"})
    assert card.status_code == 200, card.text
    body = card.json()
    assert body["status"] == "answered"
    assert any(m["author"] == "operator" and "завтра" in m["text"] for m in body["messages"])


def test_student_message_returns_status_to_in_progress(
    client, monkeypatch, api_env, operator_token
):
    token = confirmed_token(client, api_env, "student2@misis.ru")
    ticket_id = submit_ticket(client, monkeypatch, anonymous=False, token=token)
    client.post(f"/api/operator/tickets/{ticket_id}/reply", json={"text": "Уточните ФИО."},
               headers={"X-Operator-Token": operator_token})

    message = client.post(f"/api/ticket/{ticket_id}/message",
                          json={"text": "Иванов Иван Иванович"},
                          headers={"Authorization": f"Bearer {token}"})

    assert message.status_code == 200, message.text
    assert message.json()["status"] == "in_progress"


# --- 4. Чужое именное обращение недоступно без токена -------------------------

def test_named_ticket_is_forbidden_without_matching_token(client, monkeypatch, api_env):
    token = confirmed_token(client, api_env, "owner@misis.ru")
    ticket_id = submit_ticket(client, monkeypatch, anonymous=False, token=token)

    no_token = client.get(f"/api/ticket/{ticket_id}")
    assert no_token.status_code == 403
    assert no_token.json()["error"]["code"] == "forbidden"

    stranger_token = confirmed_token(client, api_env, "stranger@misis.ru")
    wrong_owner = client.get(f"/api/ticket/{ticket_id}",
                             headers={"Authorization": f"Bearer {stranger_token}"})
    assert wrong_owner.status_code == 403
    assert wrong_owner.json()["error"]["code"] == "forbidden"


# --- 5. Анонимное обращение доступно по ticket_id -----------------------------

def test_anonymous_ticket_is_reachable_by_id_alone(client, monkeypatch):
    ticket_id = submit_ticket(client, monkeypatch, anonymous=True)

    response = client.get(f"/api/ticket/{ticket_id}")

    assert response.status_code == 200, response.text
    assert response.json()["anonymous"] is True


# --- 6. Фильтр по статусу -----------------------------------------------------

def test_operator_tickets_filter_by_status(client, monkeypatch, api_env, operator_token):
    answered_id = submit_ticket(client, monkeypatch, anonymous=True)
    client.post(f"/api/operator/tickets/{answered_id}/reply", json={"text": "Ответ."},
               headers={"X-Operator-Token": operator_token})
    new_id = submit_ticket(client, monkeypatch, anonymous=True)

    only_new = client.get("/api/operator/tickets?status=new",
                          headers={"X-Operator-Token": operator_token}).json()["tickets"]
    ids = {t["ticket_id"] for t in only_new}
    assert new_id in ids
    assert answered_id not in ids

    only_answered = client.get("/api/operator/tickets?status=answered",
                               headers={"X-Operator-Token": operator_token}).json()["tickets"]
    assert {t["ticket_id"] for t in only_answered} == {answered_id}


# --- 7. Неизвестный статус -----------------------------------------------------

def test_unknown_status_filter_returns_400(client, operator_token):
    response = client.get("/api/operator/tickets?status=archived",
                          headers={"X-Operator-Token": operator_token})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_status"


def test_unknown_status_update_returns_400(client, monkeypatch, operator_token):
    ticket_id = submit_ticket(client, monkeypatch, anonymous=True)

    response = client.post(f"/api/operator/tickets/{ticket_id}/status",
                           json={"status": "archived"},
                           headers={"X-Operator-Token": operator_token})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_status"


def test_valid_status_update_is_reflected_in_card(client, monkeypatch, operator_token):
    ticket_id = submit_ticket(client, monkeypatch, anonymous=True)

    response = client.post(f"/api/operator/tickets/{ticket_id}/status",
                           json={"status": "closed"},
                           headers={"X-Operator-Token": operator_token})

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "closed"


# --- Здоровье и AUTH_SHOW_CODE -------------------------------------------------

def test_health_reports_ticket_counts_and_limits_snapshot(client, monkeypatch):
    submit_ticket(client, monkeypatch, anonymous=True)

    body = client.get("/api/health").json()

    assert body["tickets"]["new"] >= 1
    assert set(body["tickets"]) == {"new", "in_progress", "answered"}
    assert "day_used" in body["limits"] and "day_limit" in body["limits"]


def test_request_code_returns_demo_code_only_when_enabled(client, monkeypatch):
    monkeypatch.setattr(config, "AUTH_SHOW_CODE", True)

    response = client.post("/api/auth/request-code", json={"email": "demo@misis.ru"})

    assert response.status_code == 200, response.text
    assert response.json()["demo_code"].isdigit()


def test_request_code_hides_demo_code_by_default(client):
    response = client.post("/api/auth/request-code", json={"email": "demo2@misis.ru"})

    assert response.status_code == 200, response.text
    assert "demo_code" not in response.json()
