"""Тесты app/tools.py — здесь только fetch_page (белый список доменов),
остальные инструменты — тонкие обёртки над faq.py, покрытым в test_faq.py.
Без сети: проверяем только отсечение по домену до похода в urllib."""
from __future__ import annotations

from app import tools


def test_fetch_page_rejects_unrelated_domain():
    assert tools.fetch_page("https://evil.com/page") is None


def test_fetch_page_rejects_lookalike_subdomain_trick():
    # misis.ru.evil.com содержит подстроку "misis.ru", но домен чужой.
    assert tools.fetch_page("https://misis.ru.evil.com/page") is None


def test_fetch_page_rejects_non_http_scheme():
    assert tools.fetch_page("ftp://misis.ru/file") is None


def test_host_allowed_accepts_exact_and_subdomain():
    assert tools._host_allowed("misis.ru") is True
    assert tools._host_allowed("www.misis.ru") is True
    assert tools._host_allowed("abit.misis.ru") is True


def test_host_allowed_rejects_other_domains():
    assert tools._host_allowed("misis.ru.evil.com") is False
    assert tools._host_allowed("notmisis.ru") is False
    assert tools._host_allowed("evil.com") is False
