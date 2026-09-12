import pytest

from app import limits


@pytest.fixture(autouse=True)
def _clean_state():
    """Каждый тест стартует с чистым состоянием модуля — иначе тесты видели
    бы обращения, засчитанные другими тестами (счётчики модульные, не per-instance)."""
    limits.reset()
    yield
    limits.reset()


# ---------- минутный лимит ----------


def test_minute_limit_allows_up_to_max_then_blocks():
    now = 1_000_000.0
    key = "1.2.3.4"

    for _ in range(limits.MAX_PER_MINUTE):
        limits.check(key, now=now)

    with pytest.raises(limits.RateLimited) as excinfo:
        limits.check(key, now=now)

    assert excinfo.value.retry_after_sec > 0
    assert "минут" in str(excinfo.value)


def test_minute_limit_recovers_after_window_slides():
    now = 2_000_000.0
    key = "1.2.3.4"

    for _ in range(limits.MAX_PER_MINUTE):
        limits.check(key, now=now)
    with pytest.raises(limits.RateLimited):
        limits.check(key, now=now)

    # 61 секунда спустя самое старое обращение уже выпало из минутного окна.
    limits.check(key, now=now + 61)


# ---------- часовой лимит ----------


def test_hour_limit_blocks_without_ever_hitting_minute_limit():
    now = 3_000_000.0
    key = "5.6.7.8"

    # Один запрос ровно раз в минуту — минутный лимит (10/мин) никогда не
    # заденем (предыдущее обращение всегда ровно на границе минутного окна,
    # а не строго внутри него), а часовой (60/час) выберем ровно за 59 минут.
    for i in range(limits.MAX_PER_HOUR):
        limits.check(key, now=now + i * 60)

    # Секунда спустя после последнего успешного обращения: все 60 предыдущих
    # ещё внутри часового окна.
    last = now + (limits.MAX_PER_HOUR - 1) * 60 + 1
    with pytest.raises(limits.RateLimited) as excinfo:
        limits.check(key, now=last)

    assert excinfo.value.retry_after_sec > 0
    assert "час" in str(excinfo.value)


def test_hour_limit_recovers_after_window_slides():
    now = 4_000_000.0
    key = "5.6.7.8"

    for i in range(limits.MAX_PER_HOUR):
        limits.check(key, now=now + i * 60)
    last = now + (limits.MAX_PER_HOUR - 1) * 60 + 1
    with pytest.raises(limits.RateLimited):
        limits.check(key, now=last)

    # Первое обращение сделано в момент now — час с этого момента истёк.
    limits.check(key, now=now + 3601)


# ---------- суточный общий предохранитель ----------


def test_daily_global_safety_valve_trips_across_different_keys():
    now = 5_000_000.0

    # 600 разных ключей по одному обращению — ни минутный, ни часовой лимит
    # ни для одного ключа не срабатывает, но общий суточный счёт достигает предела.
    for i in range(limits.MAX_PER_DAY_GLOBAL):
        limits.check(f"10.0.0.{i}", now=now)

    with pytest.raises(limits.RateLimited) as excinfo:
        limits.check("10.0.0.999", now=now)

    assert excinfo.value.retry_after_sec > 0
    assert "суточ" in str(excinfo.value)


def test_daily_global_safety_valve_recovers_after_24h():
    now = 6_000_000.0

    for i in range(limits.MAX_PER_DAY_GLOBAL):
        limits.check(f"10.1.0.{i}", now=now)
    with pytest.raises(limits.RateLimited):
        limits.check("10.1.0.999", now=now)

    # Сутки спустя самое старое обращение выпало из окна.
    limits.check("10.1.0.999", now=now + 86401)


# ---------- retry_after_sec ----------


def test_retry_after_sec_matches_remaining_window_time():
    now = 7_000_000.0
    key = "2.2.2.2"

    for i in range(limits.MAX_PER_MINUTE):
        limits.check(key, now=now + i)

    # Первое обращение было в момент now, 10-е — в now + 9. Лимит бьётся
    # сразу же следующим вызовом; окно освобождается через 60 - 9 = 51 c.
    with pytest.raises(limits.RateLimited) as excinfo:
        limits.check(key, now=now + 9)

    assert excinfo.value.retry_after_sec == pytest.approx(51, abs=1)


# ---------- независимость ключей ----------


def test_different_keys_are_independent():
    now = 8_000_000.0

    for _ in range(limits.MAX_PER_MINUTE):
        limits.check("1.1.1.1", now=now)
    with pytest.raises(limits.RateLimited):
        limits.check("1.1.1.1", now=now)

    # У другого ключа лимит свой — обращение проходит.
    limits.check("2.2.2.2", now=now)


# ---------- очистка старых записей ----------


def test_stale_keys_are_swept_from_memory():
    now = 9_000_000.0

    for i in range(100):
        limits.check(f"3.3.3.{i}", now=now)

    assert limits.snapshot(now=now)["active_keys"] == 100

    # Спустя час с лишним все прежние обращения устарели; snapshot() делает
    # полную подметку и должен обнулить счётчик активных ключей.
    assert limits.snapshot(now=now + 3601)["active_keys"] == 0


def test_check_itself_sweeps_other_stale_keys_over_time():
    now = 10_000_000.0

    for i in range(50):
        limits.check(f"4.4.4.{i}", now=now)
    assert limits.snapshot(now=now)["active_keys"] == 50

    # Обращения новым, отдельным ключом спустя интервал подметки: старые
    # 50 ключей должны быть выметены попутно, без явного вызова snapshot().
    later = now + 3601 + 1  # окно истекло и прошёл интервал подметки
    limits.check("fresh-key", now=later)

    assert limits.snapshot(now=later)["active_keys"] == 1


# ---------- snapshot ----------


def test_snapshot_reports_day_used_and_active_keys():
    now = 11_000_000.0

    assert limits.snapshot(now=now) == {
        "day_used": 0,
        "day_limit": limits.MAX_PER_DAY_GLOBAL,
        "active_keys": 0,
    }

    limits.check("1.1.1.1", now=now)
    limits.check("2.2.2.2", now=now)
    limits.check("2.2.2.2", now=now + 1)

    snap = limits.snapshot(now=now + 1)
    assert snap["day_used"] == 3
    assert snap["active_keys"] == 2
    assert snap["day_limit"] == limits.MAX_PER_DAY_GLOBAL


# ---------- reset ----------


def test_reset_clears_all_state():
    now = 12_000_000.0
    limits.check("1.1.1.1", now=now)
    limits.reset()

    snap = limits.snapshot(now=now)
    assert snap["day_used"] == 0
    assert snap["active_keys"] == 0


# ---------- client_key ----------


def test_client_key_uses_first_address_from_forwarded_header():
    assert limits.client_key("203.0.113.5, 10.0.0.1, 10.0.0.2", "10.0.0.9") == "203.0.113.5"


def test_client_key_trims_whitespace_around_addresses():
    assert limits.client_key("  203.0.113.5  ,10.0.0.1", "10.0.0.9") == "203.0.113.5"


def test_client_key_falls_back_to_remote_addr_when_header_missing():
    assert limits.client_key(None, "198.51.100.7") == "198.51.100.7"
    assert limits.client_key("", "198.51.100.7") == "198.51.100.7"


def test_client_key_skips_garbage_entries_in_forwarded_header():
    # Пустые звенья ("а, ,b"), сплошные запятые — типичный мусор от кривых
    # прокси/клиентов, первым непустым адресом должно стать "9.9.9.9".
    assert limits.client_key(" , ,9.9.9.9, 1.1.1.1", "10.0.0.9") == "9.9.9.9"


def test_client_key_falls_back_to_unknown_when_nothing_usable():
    assert limits.client_key(None, None) == "unknown"
    assert limits.client_key(" , , ", "") == "unknown"
    assert limits.client_key("", "  ") == "unknown"
