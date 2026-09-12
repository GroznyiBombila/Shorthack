import glob
import importlib
from email import policy
from email.parser import BytesParser

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_MODE", "mock")
    monkeypatch.chdir(tmp_path)

    import app.mailer as mailer

    importlib.reload(mailer)
    return mailer


def test_mock_mode_creates_file_in_outbox(isolated_env):
    mailer = isolated_env
    result = mailer.send(
        to="student@misis.ru",
        subject="Код подтверждения",
        body_text="Ваш код: 123456",
    )

    assert result["status"] == "mocked"
    assert result["error"] is None
    assert result["message_id"]

    files = glob.glob("var/outbox/*.eml")
    assert len(files) == 1
    assert "student@misis.ru" in files[0]


def test_cyrillic_subject_is_readable(isolated_env):
    mailer = isolated_env
    subject = "Код подтверждения был выслан на корпоративную почту"
    mailer.send(to="applicant@gmail.com", subject=subject, body_text="текст письма")

    files = glob.glob("var/outbox/*.eml")
    with open(files[0], "rb") as f:
        parsed = BytesParser(policy=policy.default).parse(f)

    assert parsed["Subject"] == subject
    assert "текст письма" in parsed.get_body(preferencelist=("plain",)).get_content()


def test_html_alternative_is_included_when_provided(isolated_env):
    mailer = isolated_env
    mailer.send(
        to="applicant@gmail.com",
        subject="Тема",
        body_text="просто текст",
        body_html="<p>html-версия</p>",
    )

    files = glob.glob("var/outbox/*.eml")
    with open(files[0], "rb") as f:
        parsed = BytesParser(policy=policy.default).parse(f)

    html_part = parsed.get_body(preferencelist=("html",))
    assert html_part is not None
    assert "html-версия" in html_part.get_content()


def test_multiple_sends_do_not_overwrite_each_other(isolated_env):
    mailer = isolated_env
    for i in range(5):
        mailer.send(to=f"user{i}@gmail.com", subject="Код", body_text=f"код {i}")

    files = glob.glob("var/outbox/*.eml")
    assert len(files) == 5


def test_smtp_mode_failure_is_caught_not_raised(isolated_env, monkeypatch):
    mailer = isolated_env
    monkeypatch.setenv("MAIL_MODE", "smtp")
    # Намеренно не задаём SMTP_HOST/USER/PASSWORD — код должен поймать
    # KeyError изнутри и вернуть 'failed', а не бросить исключение наружу.

    result = mailer.send(to="student@misis.ru", subject="Код", body_text="123456")

    assert result["status"] == "failed"
    assert result["message_id"] is None
    assert result["error"]
