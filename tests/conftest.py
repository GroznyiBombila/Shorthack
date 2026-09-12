"""Общие фикстуры HTTP-тестов: свой том на каждый тест и запрет на сеть."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    """Уводит приложение с рабочего var/ на временный каталог.

    config держит пути модулями-константами, а auth и mailer про config не знают
    и читают свои пути из окружения — поэтому переопределять приходится дважды.
    """
    from app import config

    data_dir = tmp_path / "var"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "FAQ_DIR", data_dir / "faq")
    monkeypatch.setattr(config, "SCHEDULE_DIR", data_dir / "schedule")
    monkeypatch.setattr(config, "CACHE_DIR", data_dir / "cache")
    monkeypatch.setattr(config, "OUTBOX_DIR", data_dir / "outbox")
    monkeypatch.setattr(config, "DB_PATH", data_dir / "app.db")
    monkeypatch.setenv("APP_DB_PATH", str(data_dir / "app.db"))
    monkeypatch.setenv("OUTBOX_DIR", str(data_dir / "outbox"))
    monkeypatch.setenv("MAIL_MODE", "mock")
    monkeypatch.setenv("SECRET_KEY", "test-secret-key")

    from app import faq, storage

    # Кэш базы знаний живёт в процессе и помнит путь, по которому её нашли.
    faq._load_cached.cache_clear()
    storage.init()
    return data_dir


@pytest.fixture
def client(api_env, monkeypatch):
    import httpx

    def _refuse(*args, **kwargs):
        raise AssertionError("тест ушёл в сеть: complete_json должна быть подменена")

    # Единственный выход в сеть у приложения — httpx.post в llm_client.
    monkeypatch.setattr(httpx, "post", _refuse)

    # Импорт только после подмены путей: app.api на импорте создаёт схему базы.
    from app.api import app

    with TestClient(app) as test_client:
        yield test_client
