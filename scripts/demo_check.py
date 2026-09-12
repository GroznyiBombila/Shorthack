#!/usr/bin/env python
"""Проверка готовности к защите: гоняет живой сервер по сценариям демо.

Не путать с scripts/warm_cache.py — тот греет кэш, этот проверяет, что после
прогрева сервер и правда ведёт себя так, как обещано на защите. Сценарии те же
(data/demo/scenarios.json), поэтому при прогретом кэше ответы должны приходить
быстро и без сети наружу.

По каждому пункту печатается одна строка «[ОК]/[ПРОВАЛ] + что именно», чтобы за
минуту до защиты можно было пробежать глазами вывод и понять, идти или чинить.

Запуск из корня проекта:
    python scripts/demo_check.py
    python scripts/demo_check.py --base-url http://localhost:8000
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = ROOT / "data" / "demo" / "scenarios.json"
DEFAULT_BASE_URL = "http://45.87.41.186:8080"

# Пауза между сценариями. Это не прогрев (там своя пауза в 10с ради лимита
# Groq на всю команду) — здесь модель уже не должна вызываться вообще, пауза
# просто не даёт проверке долбить сервер вплотную запрос за запросом.
PAUSE_SEC = 2.0

# Порог для предупреждения про кэш. Один прогон /api/ask — это обращение к
# модели плюс к faq, при прогретом дисковом кэше это доли секунды. Дольше —
# почти наверняка ответ считался заново, а на защите сети не будет вообще.
SLOW_THRESHOLD_SEC = 4.0

TICKET_ID_RE = re.compile(r"^MISIS-\d{4}-\d{4}$")


class Report:
    """Копит строки отчёта, чтобы провал одной проверки не ронял весь прогон."""

    def __init__(self) -> None:
        self.results: list[bool] = []

    def add(self, ok: bool, text: str) -> None:
        print(f"[{'ОК' if ok else 'ПРОВАЛ'}] {text}")
        self.results.append(ok)

    @property
    def passed(self) -> int:
        return sum(self.results)

    @property
    def total(self) -> int:
        return len(self.results)


def load_scenarios() -> list[dict]:
    if not SCENARIOS_PATH.exists():
        sys.exit(f"Нет файла сценариев {SCENARIOS_PATH}")
    data = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("scenarios", [])


def is_offtopic(scenario: dict) -> bool:
    # Отдельного поля "ожидаемое действие" в scenarios.json нет — сценарий
    # "не по теме вуза" узнаём по маркеру в note, а не по жёстко зашитому id,
    # чтобы проверка не рассыпалась при переупорядочивании файла.
    return "off_topic" in scenario.get("note", "").lower()


def safe_call(report: Report, label: str, func):
    """Оборачивает одну проверку: исключение = провал этой строки, не всего скрипта."""
    try:
        return func()
    except Exception as exc:  # одна проверка не должна ронять остальные (п.5 ТЗ)
        report.add(False, f"{label}: исключение {exc!r}")
        return None


# --- 1. /api/health -----------------------------------------------------------

def check_health(client: httpx.Client, report: Report) -> None:
    def run():
        r = client.get("/api/health")
        body = r.json()
        ok = (r.status_code == 200 and body.get("llm") == "ready"
              and body.get("faq_items", 0) > 0)
        report.add(ok, f"/api/health отвечает: HTTP {r.status_code}, "
                        f"llm={body.get('llm')!r}, faq_items={body.get('faq_items')}")

    safe_call(report, "/api/health", run)


# --- 2. Сценарии: HTTP 200 + ожидаемое действие --------------------------------

def check_scenarios(client: httpx.Client, scenarios: list[dict], report: Report):
    """Прогоняет каждый сценарий через /api/ask.

    Возвращает список удачных ответов (для последующих проверок confidence/
    answer/trace) и список (id, секунды) для предупреждения про холодный кэш.
    """
    responses: list[dict] = []
    durations: list[tuple[str, float]] = []

    for index, scenario in enumerate(scenarios, start=1):
        sid = scenario["id"]
        expect_reject = is_offtopic(scenario)

        def run(scenario=scenario, sid=sid, expect_reject=expect_reject):
            start = time.perf_counter()
            r = client.post("/api/ask", json={"text": scenario["text"], "role": scenario["role"]})
            elapsed = time.perf_counter() - start
            durations.append((sid, elapsed))

            if r.status_code != 200:
                report.add(False, f"{sid}: HTTP {r.status_code} (ожидали 200) — {scenario['note']}")
                return None

            body = r.json()
            action = body.get("overall_action")
            if expect_reject:
                ok, expected = action == "rejected", "rejected"
            else:
                ok, expected = action in {"answered", "suggest_ticket"}, "answered/suggest_ticket"
            report.add(
                ok,
                f"{sid} ({elapsed:.1f}с): overall_action={action!r}, ожидали {expected} — "
                f"{scenario['note']}",
            )
            return {"id": sid, "scenario": scenario, "body": body}

        result = safe_call(report, sid, run)
        if result:
            responses.append(result)
        if index < len(scenarios):
            time.sleep(PAUSE_SEC)

    return responses, durations


# --- Пункты, общие для всех ответов сразу (confidence / answer / trace) -------

def check_confidence_range(responses: list[dict], report: Report) -> None:
    def run():
        bad = [
            f"{item['id']}/{q.get('id')}={q.get('confidence')!r}"
            for item in responses
            for q in item["body"].get("questions", [])
            if not isinstance(q.get("confidence"), (int, float))
            or not (0.0 <= q["confidence"] <= 1.0)
        ]
        ok = not bad
        detail = "все confidence в 0..1" if ok else "вне диапазона: " + ", ".join(bad)
        report.add(ok, f"confidence не выходит за 0..1: {detail}")

    safe_call(report, "confidence", run)


def check_answer_structure(responses: list[dict], report: Report) -> None:
    def run():
        bad = []
        for item in responses:
            for q in item["body"].get("questions", []):
                answer = q.get("answer")
                if answer is None:
                    continue  # вопрос ушёл в заявку, ответа контракт не обещает
                has_summary = bool((answer.get("summary") or "").strip())
                has_sources = bool(answer.get("sources"))
                if not (has_summary and has_sources):
                    bad.append(f"{item['id']}/{q.get('id')}")
        ok = not bad
        detail = "у всех отвеченных вопросов есть summary и sources" if ok else \
            "пусто у: " + ", ".join(bad)
        report.add(ok, f"answer.summary и sources заполнены: {detail}")

    safe_call(report, "answer.summary/sources", run)


def check_trace(responses: list[dict], report: Report) -> None:
    def run():
        # Структуру берём из app/router.py (decision.trace = {"llm": ..., "code": ...})
        # и app/api.py (то же самое поле уходит в ответ как trace) — ключи не
        # выдуманы, это то, что реально кладёт код.
        bad = []
        for item in responses:
            trace = item["body"].get("trace") or {}
            llm_part = trace.get("llm")
            code_part = trace.get("code")
            has_llm_decision = isinstance(llm_part, dict) and bool(llm_part)
            has_code_decision = isinstance(code_part, dict) and bool(code_part.get("rule_fired"))
            if not (has_llm_decision and has_code_decision):
                bad.append(item["id"])
        ok = not bad
        detail = "у каждого есть trace.llm (решение модели) и trace.code.rule_fired " \
                 "(решение кода)" if ok else "не хватает у: " + ", ".join(bad)
        report.add(ok, f"trace фиксирует решение модели и решение кода: {detail}")

    safe_call(report, "trace", run)


# --- 6. Полный путь: ask -> ticket/draft -> ticket/submit (anonymous) ---------

def check_full_ticket_path(client: httpx.Client, responses: list[dict], report: Report) -> None:
    def run():
        # Новый вызов /api/ask не делаем — берём уже посчитанный в п.2 ответ,
        # чтобы не тратить лимит модели ещё раз на то же самое обращение.
        candidate = next(
            (item for item in responses if item["body"].get("overall_action") == "suggest_ticket"),
            None,
        )
        if candidate is None:
            report.add(False, "путь ask -> draft -> submit: среди сценариев нет ни одного "
                               "suggest_ticket, проверить не на чем")
            return

        request_id = candidate["body"]["request_id"]
        sid = candidate["id"]

        draft = client.post("/api/ticket/draft", json={"request_id": request_id}).json()
        # Реальные значения полей для проверки самого пути не важны — важно
        # дойти до ready: true тем же способом, что и настоящий пользователь.
        attempts = 0
        while draft.get("missing_fields") and attempts < 3:
            answers = {f["field"]: "демо" for f in draft["missing_fields"]}
            draft = client.post("/api/ticket/draft", json={
                "request_id": request_id, "draft_id": draft["draft_id"],
                "answers": answers}).json()
            attempts += 1

        if not draft.get("ready"):
            report.add(False, f"путь ask({sid}) -> draft -> submit: черновик не стал ready "
                               f"после {attempts} уточнени(й) — {draft.get('missing_fields')}")
            return

        submit = client.post("/api/ticket/submit", json={
            "draft_id": draft["draft_id"], "anonymous": True})
        if submit.status_code != 200:
            report.add(False, f"путь ask({sid}) -> draft -> submit: HTTP {submit.status_code} "
                               f"на /api/ticket/submit — {submit.text[:200]}")
            return

        body = submit.json()
        ticket_id = body.get("ticket_id", "")
        ok = bool(TICKET_ID_RE.match(ticket_id)) and body.get("anonymous") is True
        report.add(ok, f"путь ask({sid}) -> draft -> submit(anonymous=true) дошёл до "
                        f"ticket_id={ticket_id!r}")

    safe_call(report, "путь ask -> draft -> submit", run)


def print_cache_warning(durations: list[tuple[str, float]]) -> None:
    slow = [(sid, sec) for sid, sec in durations if sec > SLOW_THRESHOLD_SEC]
    if slow:
        details = ", ".join(f"{sid} {sec:.1f}с" for sid, sec in slow)
        print(f"\n[ПРЕДУПРЕЖДЕНИЕ] дольше {SLOW_THRESHOLD_SEC:.0f}с: {details} — "
              f"похоже, ответ считался заново, а не взят из кэша. Перед защитой "
              f"запустите scripts/warm_cache.py против того же --base-url.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL,
                        help=f"адрес сервера (по умолчанию {DEFAULT_BASE_URL})")
    args = parser.parse_args()

    scenarios = load_scenarios()
    report = Report()

    print(f"Проверка готовности к защите: {args.base_url}\n")

    # Таймаут щедрый: холодный (непрогретый) ответ модели может идти секунды,
    # а честно увидеть, что он не уложился в SLOW_THRESHOLD_SEC, важнее, чем
    # оборвать проверку по таймауту клиента.
    with httpx.Client(base_url=args.base_url, timeout=60.0) as client:
        check_health(client, report)
        responses, durations = check_scenarios(client, scenarios, report)
        check_confidence_range(responses, report)
        check_answer_structure(responses, report)
        check_trace(responses, report)
        check_full_ticket_path(client, responses, report)

    print_cache_warning(durations)

    print(f"\nГотово к защите: {report.passed} из {report.total} проверок")
    return 0 if report.passed == report.total else 1


if __name__ == "__main__":
    sys.exit(main())
