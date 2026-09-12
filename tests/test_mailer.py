import glob
import importlib
from email import policy
from email.parser import BytesParser

import pytest


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MAIL_MODE", "mock")
    # Каталог отправленных писем берётся из окружения, иначе mailer уйдёт в общий
    # том приложения (app.config.OUTBOX_DIR) и тесты начнут писать в рабочий var/.
    monkeypatch.setenv("OUTBOX_DIR", "var/outbox")
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
    # Намеренно не задаём SMTP_HOST/USER/PASSWORD/MAIL_FROM — код должен поймать
    # ошибку изнутри и вернуть 'failed', а не бросить исключение наружу.

    result = mailer.send(to="student@misis.ru", subject="Код", body_text="123456")

    assert result["status"] == "failed"
    assert result["message_id"] is None
    assert result["error"]


def test_smtp_mode_without_mail_from_fails_with_clear_error(isolated_env, monkeypatch):
    mailer = isolated_env
    monkeypatch.setenv("MAIL_MODE", "smtp")
    monkeypatch.delenv("MAIL_FROM", raising=False)

    result = mailer.send(to="student@misis.ru", subject="Код", body_text="123456")

    # Регресс: раньше пустой MAIL_FROM тихо подменялся на no-reply@example.com,
    # и письмо на 587/465 порту уходило от чужого имени вместо явной ошибки.
    assert result["status"] == "failed"
    assert "MAIL_FROM" in result["error"]


# --- Схема "ящик пишет сам себе" ---------------------------------------------

def test_default_recipient_is_the_sender_mailbox(isolated_env, monkeypatch):
    mailer = isolated_env
    monkeypatch.setenv("MAIL_FROM", "support@misis.ru")

    result = mailer.send(subject="Обращение", body_text="текст обращения")

    assert result["status"] == "mocked"
    files = glob.glob("var/outbox/*.eml")
    assert len(files) == 1
    with open(files[0], "rb") as f:
        parsed = BytesParser(policy=policy.default).parse(f)

    assert parsed["From"] == "support@misis.ru"
    assert parsed["To"] == "support@misis.ru"


def test_reply_to_is_set_for_confirmed_applicant_email(isolated_env, monkeypatch):
    mailer = isolated_env
    monkeypatch.setenv("MAIL_FROM", "support@misis.ru")

    mailer.send(subject="Обращение", body_text="текст",
               reply_to="ivanov@edu.misis.ru")

    files = glob.glob("var/outbox/*.eml")
    with open(files[0], "rb") as f:
        parsed = BytesParser(policy=policy.default).parse(f)

    assert parsed["Reply-To"] == "ivanov@edu.misis.ru"


def test_reply_to_is_absent_for_anonymous_submission(isolated_env, monkeypatch):
    mailer = isolated_env
    monkeypatch.setenv("MAIL_FROM", "support@misis.ru")

    mailer.send(subject="Обращение", body_text="текст", reply_to=None)

    files = glob.glob("var/outbox/*.eml")
    with open(files[0], "rb") as f:
        parsed = BytesParser(policy=policy.default).parse(f)

    assert parsed["Reply-To"] is None


# --- Выбор SMTP-транспорта по порту ------------------------------------------

class _FakeSMTPBase:
    """Подменяет smtplib: реального соединения тесты делать не должны."""

    instances: list["_FakeSMTPBase"] = []

    def __init__(self, host, port, timeout=10, context=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.logged_in = False
        self.sent = None
        type(self).instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = True

    def send_message(self, msg):
        self.sent = msg


def test_smtp_port_465_uses_implicit_tls(isolated_env, monkeypatch):
    import smtplib

    mailer = isolated_env
    fake_ssl = type("FakeSMTP_SSL", (_FakeSMTPBase,), {"instances": []})
    fake_plain = type("FakeSMTP", (_FakeSMTPBase,), {"instances": []})
    monkeypatch.setattr(smtplib, "SMTP_SSL", fake_ssl)
    monkeypatch.setattr(smtplib, "SMTP", fake_plain)
    monkeypatch.setenv("MAIL_MODE", "smtp")
    monkeypatch.setenv("MAIL_FROM", "support@misis.ru")
    monkeypatch.setenv("SMTP_HOST", "smtp.misis.ru")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "support@misis.ru")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")

    result = mailer.send(subject="Код", body_text="123456")

    assert result["status"] == "sent"
    assert len(fake_ssl.instances) == 1
    assert not fake_plain.instances
    assert fake_ssl.instances[0].logged_in is True


def test_smtp_port_587_uses_starttls(isolated_env, monkeypatch):
    import smtplib

    mailer = isolated_env
    fake_ssl = type("FakeSMTP_SSL", (_FakeSMTPBase,), {"instances": []})
    fake_plain = type("FakeSMTP", (_FakeSMTPBase,), {"instances": []})
    monkeypatch.setattr(smtplib, "SMTP_SSL", fake_ssl)
    monkeypatch.setattr(smtplib, "SMTP", fake_plain)
    monkeypatch.setenv("MAIL_MODE", "smtp")
    monkeypatch.setenv("MAIL_FROM", "support@misis.ru")
    monkeypatch.setenv("SMTP_HOST", "smtp.misis.ru")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "support@misis.ru")
    monkeypatch.setenv("SMTP_PASSWORD", "secret")

    # Регресс: раньше код всегда открывал SMTP_SSL, а на 587 (обычный SMTP +
    # STARTTLS) это соединение просто не поднимается.
    result = mailer.send(subject="Код", body_text="123456")

    assert result["status"] == "sent"
    assert not fake_ssl.instances
    assert len(fake_plain.instances) == 1
    assert fake_plain.instances[0].started_tls is True
    assert fake_plain.instances[0].logged_in is True
