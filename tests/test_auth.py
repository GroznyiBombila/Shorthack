import glob
import importlib
import os

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Каждый тест получает свою SQLite-базу, свой outbox и свой SECRET_KEY,
    чтобы тесты не видели состояние друг друга и не трогали сеть/диск проекта."""
    monkeypatch.setenv("APP_DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("MAIL_MODE", "mock")
    # Письма с кодами должны падать во временный каталог, а не в рабочий том
    # приложения: mailer берёт путь из OUTBOX_DIR, иначе из app.config.
    monkeypatch.setenv("OUTBOX_DIR", "var/outbox")
    monkeypatch.chdir(tmp_path)

    import app.auth as auth
    import app.mailer as mailer

    importlib.reload(mailer)
    importlib.reload(auth)
    return auth


# ---------- classify_email ----------


def test_classify_email_corporate_domains(isolated_env):
    auth = isolated_env
    assert auth.classify_email("Ivan.Ivanov@MISIS.RU") == "student"
    assert auth.classify_email("  student@edu.misis.ru  ") == "student"


def test_classify_email_non_corporate(isolated_env):
    auth = isolated_env
    assert auth.classify_email("someone@gmail.com") == "applicant"


@pytest.mark.parametrize("garbage", ["привет", "a@b", "", "   ", "no-at-sign.com", "a@b@c.com"])
def test_classify_email_garbage_raises(isolated_env, garbage):
    auth = isolated_env
    with pytest.raises(auth.InvalidEmail):
        auth.classify_email(garbage)


# ---------- request_code / verify_code happy path ----------


def _last_code_from_outbox():
    import re
    from email import policy
    from email.parser import BytesParser

    files = sorted(glob.glob("var/outbox/*.eml"))
    assert files, "ожидалось письмо в var/outbox"
    with open(files[-1], "rb") as f:
        msg = BytesParser(policy=policy.default).parse(f)
    body = msg.get_body(preferencelist=("plain",)).get_content()

    match = re.search(r"код подтверждения:\s*(\d{6})", body, re.IGNORECASE)
    assert match, f"код не найден в письме:\n{body}"
    return match.group(1)


def test_request_code_sends_mail_and_verify_succeeds(isolated_env):
    auth = isolated_env
    now = 1_000_000.0

    result = auth.request_code("student@misis.ru", ip="1.2.3.4", now=now)
    assert result["user_type"] == "student"
    assert result["ttl_sec"] == 600
    assert result["resend_after_sec"] == 60

    code = _last_code_from_outbox()
    verified = auth.verify_code("student@misis.ru", code, now=now + 5)

    assert verified["user_type"] == "student"
    assert "token" in verified and verified["token"].count(".") == 1
    assert verified["expires_at"] == pytest.approx(now + 5 + 30 * 60)


def test_wrong_code_gives_invalid_code_with_decreasing_attempts(isolated_env):
    auth = isolated_env
    now = 2_000_000.0
    auth.request_code("student@misis.ru", ip="1.2.3.4", now=now)

    for expected_attempts_left in (4, 3, 2, 1, 0):
        with pytest.raises(auth.InvalidCode) as excinfo:
            auth.verify_code("student@misis.ru", "000000", now=now + 1)
        assert excinfo.value.attempts_left == expected_attempts_left


def test_sixth_attempt_raises_too_many_attempts(isolated_env):
    auth = isolated_env
    now = 3_000_000.0
    auth.request_code("student@misis.ru", ip="1.2.3.4", now=now)

    for _ in range(5):
        with pytest.raises(auth.InvalidCode):
            auth.verify_code("student@misis.ru", "000000", now=now + 1)

    with pytest.raises(auth.TooManyAttempts):
        auth.verify_code("student@misis.ru", "000000", now=now + 2)

    # Даже верный код после сгорания не поможет.
    correct_code = _last_code_from_outbox()
    with pytest.raises(auth.TooManyAttempts):
        auth.verify_code("student@misis.ru", correct_code, now=now + 3)


def test_expired_code_raises_code_expired(isolated_env):
    auth = isolated_env
    now = 4_000_000.0
    auth.request_code("student@misis.ru", ip="1.2.3.4", now=now)
    code = _last_code_from_outbox()

    with pytest.raises(auth.CodeExpired):
        auth.verify_code("student@misis.ru", code, now=now + 601)


def test_used_code_cannot_be_reused(isolated_env):
    auth = isolated_env
    now = 5_000_000.0
    auth.request_code("student@misis.ru", ip="1.2.3.4", now=now)
    code = _last_code_from_outbox()

    auth.verify_code("student@misis.ru", code, now=now + 1)

    with pytest.raises(Exception):
        auth.verify_code("student@misis.ru", code, now=now + 2)


def test_verify_with_no_code_requested_raises_code_expired(isolated_env):
    auth = isolated_env
    with pytest.raises(auth.CodeExpired):
        auth.verify_code("nobody@misis.ru", "123456", now=1.0)


# ---------- rate limiting ----------


def test_resend_cooldown_60_seconds(isolated_env):
    auth = isolated_env
    now = 6_000_000.0
    auth.request_code("student@misis.ru", ip="9.9.9.9", now=now)

    with pytest.raises(auth.RateLimited) as excinfo:
        auth.request_code("student@misis.ru", ip="9.9.9.9", now=now + 10)
    assert excinfo.value.retry_after_sec == pytest.approx(50, abs=1)

    # После окончания cooldown снова можно запросить код.
    auth.request_code("student@misis.ru", ip="9.9.9.9", now=now + 61)


def test_fourth_code_request_in_10_minutes_is_rate_limited(isolated_env):
    auth = isolated_env
    now = 7_000_000.0
    ip = "5.5.5.5"

    auth.request_code("student@misis.ru", ip=ip, now=now)
    auth.request_code("student@misis.ru", ip=ip, now=now + 70)
    auth.request_code("student@misis.ru", ip=ip, now=now + 140)

    with pytest.raises(auth.RateLimited):
        auth.request_code("student@misis.ru", ip=ip, now=now + 210)


def test_twenty_first_request_from_same_ip_is_rate_limited(isolated_env):
    auth = isolated_env
    now = 8_000_000.0
    ip = "7.7.7.7"

    # Обходим лимит на адрес (3 за 10 минут), используя разные адреса,
    # чтобы проверить именно лимит по IP (20 в час).
    for i in range(20):
        auth.request_code(f"user{i}@gmail.com", ip=ip, now=now + i)

    with pytest.raises(auth.RateLimited):
        auth.request_code("user20@gmail.com", ip=ip, now=now + 20)


# ---------- validate_token ----------


def test_validate_token_accepts_fresh_token(isolated_env):
    auth = isolated_env
    now = 9_000_000.0
    auth.request_code("student@misis.ru", ip="1.1.1.1", now=now)
    code = _last_code_from_outbox()
    verified = auth.verify_code("student@misis.ru", code, now=now + 1)

    payload = auth.validate_token(verified["token"], now=now + 2)
    assert payload["email"] == "student@misis.ru"
    assert payload["user_type"] == "student"


def test_validate_token_rejects_tampered_token(isolated_env):
    auth = isolated_env
    now = 10_000_000.0
    auth.request_code("student@misis.ru", ip="1.1.1.1", now=now)
    code = _last_code_from_outbox()
    verified = auth.verify_code("student@misis.ru", code, now=now + 1)

    payload_b64, signature_b64 = verified["token"].split(".")
    tampered = payload_b64 + "x." + signature_b64
    assert auth.validate_token(tampered, now=now + 2) is None


def test_validate_token_rejects_foreign_token(isolated_env, monkeypatch):
    auth = isolated_env
    now = 11_000_000.0
    auth.request_code("student@misis.ru", ip="1.1.1.1", now=now)
    code = _last_code_from_outbox()
    verified = auth.verify_code("student@misis.ru", code, now=now + 1)

    monkeypatch.setenv("SECRET_KEY", "a-different-secret")
    assert auth.validate_token(verified["token"], now=now + 2) is None


def test_validate_token_rejects_expired_token(isolated_env):
    auth = isolated_env
    now = 12_000_000.0
    auth.request_code("student@misis.ru", ip="1.1.1.1", now=now)
    code = _last_code_from_outbox()
    verified = auth.verify_code("student@misis.ru", code, now=now + 1)

    assert auth.validate_token(verified["token"], now=now + 1 + 30 * 60 + 1) is None


def test_validate_token_rejects_garbage():
    import app.auth as auth

    os.environ["SECRET_KEY"] = "test-secret-key"
    assert auth.validate_token("not-a-real-token") is None
    assert auth.validate_token("") is None
