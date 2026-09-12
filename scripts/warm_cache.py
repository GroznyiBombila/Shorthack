#!/usr/bin/env python
"""Прогрев кэша модели перед защитой.

Лимиты Groq общие на всю команду: 1000 запросов в сутки и 8000 токенов в минуту.
На защите проект не должен ходить в сеть вообще — все демонстрационные обращения
обязаны отдаваться из дискового кэша (var/cache). Скрипт прогоняет сценарии из
data/demo/scenarios.json через НАСТОЯЩИЕ эндпоинты: только так ключ кэша совпадёт
с тем, что построит приложение на защите (ключ = sha256 от модели, системного и
пользовательского промпта, см. app/llm_client._cache_key).

Запуск из корня проекта:
    python scripts/warm_cache.py            # прогреть локальный кэш
    python scripts/warm_cache.py --dry-run  # показать, что уже в кэше, без сети
    python scripts/warm_cache.py --base-url http://45.87.41.186:8080   # прогреть сервер

Кэш дисковый и лежит рядом с приложением, поэтому прогрев на ноутбуке никак не
помогает контейнеру на сервере: демо греем там, где оно будет показываться.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, llm_client, prompts  # noqa: E402

SCENARIOS = ROOT / "data" / "demo" / "scenarios.json"

# Пауза между сценариями. Один прогон обращения — это два вызова модели примерно
# по 1100 токенов, то есть около 2200. При лимите 8000 токенов в минуту с паузой в
# 4 секунды прогрев на сервере всё равно словил 429, поэтому пауза увеличена.
PAUSE_SEC = 10.0


def analyze_cache_key(text: str, role: str) -> str:
    """Тот же ключ, что построит app.analyze при разборе этого обращения.

    Промпт разбора собирается в app/analyze.py одной строкой; повторяем её здесь,
    чтобы --dry-run мог ответить про кэш, не поднимая приложение и не тратя лимит.
    """
    truncated = text[:4000]
    user = f"Известная роль отправителя: {role}.\n\nТекст обращения:\n{truncated}"
    return llm_client._cache_key(config.GROQ_MODEL, prompts.ANALYZE_SYSTEM, user)


def is_cached(text: str, role: str) -> bool:
    return (config.CACHE_DIR / f"{analyze_cache_key(text, role)}.json").exists()


def load_scenarios() -> list[dict]:
    if not SCENARIOS.exists():
        sys.exit(f"Нет файла сценариев {SCENARIOS}")
    data = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("scenarios", [])


def dry_run(scenarios: list[dict]) -> int:
    warm = 0
    for item in scenarios:
        cached = is_cached(item["text"], item["role"])
        warm += cached
        print(f"  {item['id']:<8} {'в кэше' if cached else 'ХОЛОДНЫЙ':<9} {item.get('note', '')}")
    print(f"\nВ кэше {warm} из {len(scenarios)}. Файлов кэша всего: "
          f"{len(list(config.CACHE_DIR.glob('*.json'))) if config.CACHE_DIR.exists() else 0}")
    return 0 if warm == len(scenarios) else 1


def _client(base_url: str | None):
    """Локально — приложение в процессе, с --base-url — обычный HTTP-клиент.

    У обоих совместимый интерфейс .post(path, json=...), поэтому прогрев не знает,
    греет он свой кэш или серверный.
    """
    if base_url:
        import httpx
        return httpx.Client(base_url=base_url, timeout=60)

    # Импорт приложения тяжёлый и не нужен для --dry-run, поэтому он здесь.
    from fastapi.testclient import TestClient

    from app.api import app
    return TestClient(app)


def warm(scenarios: list[dict], base_url: str | None = None) -> int:
    failed: list[str] = []
    # На удалённом сервере состояние кэша не видно: считаем всё холодным и
    # выдерживаем паузу после каждого обращения.
    local = base_url is None
    with _client(base_url) as client:
        for index, item in enumerate(scenarios, start=1):
            was_cached = is_cached(item["text"], item["role"]) if local else False
            label = f"[{index}/{len(scenarios)}] {item['id']}"
            try:
                response = client.post("/api/ask", json={"text": item["text"],
                                                         "role": item["role"]})
                if response.status_code != 200:
                    failed.append(f"{item['id']}: HTTP {response.status_code}")
                    print(f"{label} ОШИБКА HTTP {response.status_code}")
                    continue
                body = response.json()
                action = body["overall_action"]

                # Черновик обращения — это ещё один вызов модели, и на защите его
                # тоже нажмут. Греем только там, где интерфейс реально его предложит.
                if action == "suggest_ticket":
                    draft = client.post("/api/ticket/draft",
                                        json={"request_id": body["request_id"], "answers": {}})
                    action += f" + черновик {draft.status_code}"

                source = "уже был в кэше" if was_cached else "запрошено у модели"
                print(f"{label} {action} ({source}) — {item.get('note', '')}")
            except Exception as exc:  # один плохой сценарий не должен ронять прогрев
                failed.append(f"{item['id']}: {exc}")
                print(f"{label} ОШИБКА {exc}")

            if not was_cached and index < len(scenarios):
                time.sleep(PAUSE_SEC)

    ok = len(scenarios) - len(failed)
    print(f"\nПрогрето {ok} из {len(scenarios)}.")
    if failed:
        print("Не прогрелись:")
        for line in failed:
            print(f"  - {line}")
        return 1
    print("Проверка: запустите скрипт с --dry-run, все сценарии должны быть в кэше.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="показать состояние кэша, не обращаясь к модели")
    parser.add_argument("--base-url", default=None,
                        help="прогреть удалённый инстанс, например http://45.87.41.186:8080")
    args = parser.parse_args()

    logging.basicConfig(level="WARNING")  # логи llm_client не мешают читать отчёт
    config.ensure_dirs()
    scenarios = load_scenarios()
    print(f"Сценариев: {len(scenarios)}, модель: {config.GROQ_MODEL}, кэш: {config.CACHE_DIR}\n")
    if args.dry_run:
        if args.base_url:
            sys.exit("--dry-run смотрит локальный кэш и с --base-url несовместим")
        return dry_run(scenarios)
    return warm(scenarios, args.base_url)


if __name__ == "__main__":
    sys.exit(main())
