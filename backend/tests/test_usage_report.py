"""GET /usage/today and GET /usage/summary: the signed-in user's own numbers."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.auth as auth
import app.database as database
import app.limits as limits
import app.main as main
import app.usage_report as usage_report
from tests.default_user import DEFAULT_USER_ID

NOW = datetime(2026, 11, 12, 15, 30, tzinfo=timezone.utc)
TODAY = NOW.date()


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(limits, "_now", lambda: NOW)
    for name in ("DAILY_UPLOAD_LIMIT", "DAILY_CHAT_LIMIT", usage_report.PRICES_ENV):
        monkeypatch.delenv(name, raising=False)


def sql(query, params=None):
    with database.get_connection() as connection:
        return connection.execute(text(query), params or {})


def add_user(email="other@example.com") -> int:
    return sql("INSERT INTO users (email, password_hash) VALUES (:e, 'x') RETURNING id",
               {"e": email}).scalar_one()


def counters(user_id, uploads, chats, day=TODAY):
    sql("INSERT INTO daily_usage (user_id, day, uploads, chat_questions) "
        "VALUES (:u, :d, :up, :c)", {"u": user_id, "d": day, "up": uploads, "c": chats})


def call(kind="chat", model="gpt-4o-mini", prompt=100, completion=20, outcome="ok",
         when=NOW, user_id=DEFAULT_USER_ID):
    sql("INSERT INTO ai_usage (user_id, kind, model, outcome, prompt_tokens, completion_tokens, "
        "total_tokens, duration_ms, created_at) "
        "VALUES (:u, :k, :m, :o, :p, :c, :t, 100, :w)",
        {"u": user_id, "k": kind, "m": model, "o": outcome, "p": prompt, "c": completion,
         "t": None if prompt is None else prompt + (completion or 0), "w": when})


def get(path, as_user=DEFAULT_USER_ID, **params):
    """Ask as the given user (the session check is replaced, like in most tests)."""
    previous = main.app.dependency_overrides[auth.current_user]
    main.app.dependency_overrides[auth.current_user] = lambda: auth.User(id=as_user, email="x@y.z")
    try:
        with TestClient(main.app) as client:
            return client.get(path, params=params)
    finally:
        main.app.dependency_overrides[auth.current_user] = previous


# --- /usage/today -----------------------------------------------------------------------------


def test_today_with_no_usage_shows_zero_against_the_default_limits():
    body = get("/usage/today").json()
    assert body["day"] == "2026-11-12"
    assert body["uploads"] == {"used": 0, "limit": 20, "unlimited": False, "remaining": 20,
                               "percent": 0}
    assert body["chat"] == {"used": 0, "limit": 100, "unlimited": False, "remaining": 100,
                            "percent": 0}


def test_today_reads_the_daily_counters():
    counters(DEFAULT_USER_ID, uploads=5, chats=62)
    body = get("/usage/today").json()
    assert (body["uploads"]["used"], body["uploads"]["percent"], body["uploads"]["remaining"]) == (5, 25, 15)
    assert (body["chat"]["used"], body["chat"]["percent"], body["chat"]["remaining"]) == (62, 62, 38)


def test_the_limits_follow_the_settings_and_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("DAILY_UPLOAD_LIMIT", "0")
    monkeypatch.setenv("DAILY_CHAT_LIMIT", "40")
    counters(DEFAULT_USER_ID, uploads=7, chats=30)
    body = get("/usage/today").json()
    assert body["uploads"] == {"used": 7, "limit": 0, "unlimited": True, "remaining": None,
                               "percent": None}
    assert body["chat"]["limit"] == 40 and body["chat"]["percent"] == 75


def test_at_the_limit_the_percent_is_100_and_nothing_remains(monkeypatch):
    monkeypatch.setenv("DAILY_CHAT_LIMIT", "3")
    counters(DEFAULT_USER_ID, uploads=0, chats=3)
    chat = get("/usage/today").json()["chat"]
    assert (chat["used"], chat["percent"], chat["remaining"]) == (3, 100, 0)


def test_the_percent_is_rounded_and_never_above_100(monkeypatch):
    monkeypatch.setenv("DAILY_CHAT_LIMIT", "3")
    counters(DEFAULT_USER_ID, uploads=0, chats=1)
    assert get("/usage/today").json()["chat"]["percent"] == 33
    sql("UPDATE daily_usage SET chat_questions = 9")  # more than the limit (the limit was lowered)
    chat = get("/usage/today").json()["chat"]
    assert chat["percent"] == 100 and chat["remaining"] == 0 and chat["used"] == 9


def test_it_resets_at_the_next_midnight_utc():
    body = get("/usage/today").json()
    assert body["resets_at"] == "2026-11-13T00:00:00+00:00"
    assert body["resets_in_seconds"] == 8 * 3600 + 30 * 60  # 15:30 → 24:00


def test_only_todays_counters_count_and_only_mine():
    other = add_user()
    counters(DEFAULT_USER_ID, uploads=9, chats=9, day=TODAY - timedelta(days=1))
    counters(other, uploads=4, chats=4)
    body = get("/usage/today").json()
    assert (body["uploads"]["used"], body["chat"]["used"]) == (0, 0)
    assert get("/usage/today", as_user=other).json()["chat"]["used"] == 4


def test_reading_the_usage_counts_nothing():
    counters(DEFAULT_USER_ID, uploads=2, chats=2)
    for _ in range(3):
        get("/usage/today")
        get("/usage/summary")
    assert tuple(sql("SELECT uploads, chat_questions FROM daily_usage").one()) == (2, 2)


def test_it_shows_what_the_limits_themselves_counted():
    limits.enforce_daily_limit(DEFAULT_USER_ID, "chat")
    limits.enforce_daily_limit(DEFAULT_USER_ID, "chat")
    limits.enforce_daily_limit(DEFAULT_USER_ID, "upload")
    body = get("/usage/today").json()
    assert (body["chat"]["used"], body["uploads"]["used"]) == (2, 1)


# --- /usage/summary ---------------------------------------------------------------------------


def test_an_empty_summary_has_every_day_with_zeros_and_no_calls():
    body = get("/usage/summary").json()
    assert body["days"] == 7 and body["currency"] == "EGP"
    assert [d["date"] for d in body["by_day"]] == [
        (TODAY - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    assert body["from"] == "2026-11-06" and body["to"] == "2026-11-12"
    assert body["totals"] == {"calls": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
                              "total_tokens": 0, "cost": 0.0, "unpriced_calls": 0}
    assert body["by_kind"] == {} and body["recent_calls"] == []
    assert all(d["total_tokens"] == 0 and d["tokens_by_kind"] == {} for d in body["by_day"])


def test_the_period_defaults_to_7_and_can_be_30():
    assert len(get("/usage/summary").json()["by_day"]) == 7
    assert len(get("/usage/summary", days=7).json()["by_day"]) == 7
    thirty = get("/usage/summary", days=30).json()
    assert len(thirty["by_day"]) == 30 and thirty["from"] == "2026-10-14"


@pytest.mark.parametrize("days", ["0", "1", "14", "31", "-7", "abc", ""])
def test_other_periods_are_refused(days):
    assert get("/usage/summary", days=days).status_code == 422


def test_tokens_are_added_up_by_day_and_by_kind():
    yesterday = NOW - timedelta(days=1)
    call(kind="chat", prompt=1000, completion=200)
    call(kind="chat", prompt=500, completion=100)
    call(kind="embedding", model="text-embedding-3-small", prompt=300, completion=0)
    call(kind="summary", prompt=2000, completion=400, when=yesterday)

    body = get("/usage/summary").json()
    today, before = body["by_day"][-1], body["by_day"][-2]
    assert (today["prompt_tokens"], today["completion_tokens"], today["total_tokens"],
            today["calls"]) == (1800, 300, 2100, 3)
    assert today["tokens_by_kind"] == {"chat": 1800, "embedding": 300}
    assert (before["total_tokens"], before["tokens_by_kind"]) == (2400, {"summary": 2400})
    assert body["totals"]["total_tokens"] == 4500 and body["totals"]["calls"] == 4
    assert body["by_kind"]["chat"]["calls"] == 2
    assert body["by_kind"]["chat"]["prompt_tokens"] == 1500
    assert body["by_kind"]["summary"]["completion_tokens"] == 400
    assert body["by_kind"]["embedding"]["total_tokens"] == 300


def test_cost_is_estimated_in_egp_from_the_price_table():
    call(kind="summary", model="gpt-4o-mini", prompt=1_000_000, completion=100_000)
    call(kind="embedding", model="text-embedding-3-small", prompt=500_000, completion=0)
    body = get("/usage/summary").json()
    # 1M × 8.80 + 0.1M × 35.19 = 12.319 ; 0.5M × 1.17 = 0.585
    assert body["by_kind"]["summary"]["cost"] == pytest.approx(12.319)
    assert body["by_kind"]["embedding"]["cost"] == pytest.approx(0.585)
    assert body["totals"]["cost"] == pytest.approx(12.904)
    assert body["by_day"][-1]["cost"] == pytest.approx(12.904)
    assert body["totals"]["unpriced_calls"] == 0


def test_model_names_match_with_a_provider_prefix_or_a_version_suffix():
    table = usage_report.DEFAULT_PRICES
    for name in ("gpt-4o-mini", "openai/gpt-4o-mini", "GPT-4O-MINI", "gpt-4o-mini-2024-07-18"):
        assert usage_report.price_for(name, table) == table["gpt-4o-mini"], name
    assert usage_report.price_for("gpt-4o", table) is None  # not a prefix match by accident
    assert usage_report.price_for("gpt-4o-minimal", table) is None
    assert usage_report.price_for("text-embedding-3-large", table) is None


def test_the_longest_matching_name_wins():
    table = {"gpt-4o": {"prompt": 1.0, "completion": 1.0},
             "gpt-4o-mini": {"prompt": 9.0, "completion": 9.0}}
    assert usage_report.price_for("gpt-4o-mini-2024", table)["prompt"] == 9.0
    assert usage_report.price_for("gpt-4o-2024", table)["prompt"] == 1.0


def test_a_model_without_a_price_adds_no_cost_and_is_reported():
    call(model="mystery-model", prompt=1_000_000, completion=1_000_000)
    call(model="gpt-4o-mini", prompt=1_000_000, completion=0)
    body = get("/usage/summary").json()
    assert body["totals"]["cost"] == pytest.approx(8.80)
    assert body["totals"]["unpriced_calls"] == 1
    assert body["totals"]["total_tokens"] == 3_000_000  # the tokens still count


def test_the_price_table_is_configurable(monkeypatch):
    monkeypatch.setenv(usage_report.PRICES_ENV,
                       '{"Mystery-Model": {"prompt": 2, "completion": 4}}')
    call(model="mystery-model", prompt=1_000_000, completion=1_000_000)
    call(model="gpt-4o-mini", prompt=1_000_000, completion=0)  # not in the custom table now
    body = get("/usage/summary").json()
    assert body["totals"]["cost"] == pytest.approx(6.0)
    assert body["totals"]["unpriced_calls"] == 1


@pytest.mark.parametrize("value", ["not json", "[1]", '{"m": 5}', '{"m": {"prompt": -1}}',
                                   '{"m": {"prompt": "cheap"}}'])
def test_a_bad_price_table_stops_the_app_from_starting(monkeypatch, value):
    monkeypatch.setenv(usage_report.PRICES_ENV, value)
    with pytest.raises(ValueError, match=usage_report.PRICES_ENV):
        usage_report.check_usage_settings()
    with pytest.raises(ValueError):
        with TestClient(main.app):
            pass


def test_the_period_includes_today_and_the_first_day_but_not_the_day_before():
    midnight = datetime.combine(TODAY - timedelta(days=6), datetime.min.time(), timezone.utc)
    call(prompt=10, completion=0, when=midnight)  # 00:00 UTC on the first day: in
    call(prompt=20, completion=0, when=midnight - timedelta(seconds=1))  # 23:59:59 before: out
    call(prompt=40, completion=0, when=NOW)  # today: in
    seven = get("/usage/summary", days=7).json()
    assert seven["totals"]["prompt_tokens"] == 50
    assert seven["by_day"][0]["prompt_tokens"] == 10
    assert get("/usage/summary", days=30).json()["totals"]["prompt_tokens"] == 70


def test_days_are_utc_days():
    call(prompt=1, completion=0, when=datetime(2026, 11, 11, 23, 59, 59, tzinfo=timezone.utc))
    call(prompt=2, completion=0, when=datetime(2026, 11, 12, 0, 0, 1, tzinfo=timezone.utc))
    by_day = {d["date"]: d["prompt_tokens"] for d in get("/usage/summary").json()["by_day"]}
    assert by_day["2026-11-11"] == 1 and by_day["2026-11-12"] == 2


def test_failed_calls_are_counted_and_missing_tokens_count_as_zero():
    call(outcome="ok", prompt=100, completion=10)
    call(outcome="error", prompt=None, completion=None)
    call(outcome="cancelled", prompt=50, completion=None)
    totals = get("/usage/summary").json()["totals"]
    assert (totals["calls"], totals["errors"]) == (3, 2)
    assert (totals["prompt_tokens"], totals["completion_tokens"]) == (150, 10)


def test_the_last_20_calls_come_newest_first_with_no_ids():
    for i in range(25):
        call(prompt=i, completion=1, when=NOW - timedelta(minutes=25 - i))
    call(prompt=999, completion=1, when=NOW - timedelta(days=40))  # outside both periods
    body = get("/usage/summary").json()
    recent = body["recent_calls"]
    assert len(recent) == 20
    assert [c["prompt_tokens"] for c in recent[:3]] == [24, 23, 22]
    assert recent[0] == {"time": (NOW - timedelta(minutes=1)).isoformat(), "kind": "chat",
                         "model": "gpt-4o-mini", "prompt_tokens": 24, "completion_tokens": 1,
                         "outcome": "ok"}
    assert "user_id" not in str(body) and "job_id" not in str(body)


def test_the_recent_calls_can_be_older_than_the_period():
    call(prompt=7, completion=1, when=NOW - timedelta(days=40))
    body = get("/usage/summary").json()
    assert body["totals"]["calls"] == 0 and len(body["recent_calls"]) == 1


# --- Isolation --------------------------------------------------------------------------------


def test_another_users_usage_never_appears():
    other = add_user()
    call(kind="chat", prompt=1_000_000, completion=1_000_000, user_id=other)
    call(kind="summary", model="secret-model", prompt=5, completion=5, user_id=other)
    counters(other, uploads=11, chats=77)
    call(prompt=3, completion=1)  # mine

    mine = get("/usage/summary").json()
    assert mine["totals"]["total_tokens"] == 4 and mine["totals"]["calls"] == 1
    assert list(mine["by_kind"]) == ["chat"]
    assert [c["prompt_tokens"] for c in mine["recent_calls"]] == [3]
    assert "secret-model" not in str(mine)
    today = get("/usage/today").json()
    assert (today["uploads"]["used"], today["chat"]["used"]) == (0, 0)

    theirs = get("/usage/summary", as_user=other).json()
    assert theirs["totals"]["calls"] == 2
    assert get("/usage/today", as_user=other).json()["chat"]["used"] == 77


def test_a_user_id_in_the_request_is_ignored():
    other = add_user()
    call(prompt=1_000_000, completion=0, user_id=other)
    for path in ("/usage/summary", "/usage/today"):
        body = get(path, **{"user_id": other, "userId": other}).json()
        assert "1000000" not in str(body)
    assert get("/usage/summary", **{"user_id": other}).json()["totals"]["calls"] == 0


def test_usage_follows_the_account_when_it_is_deleted():
    other = add_user()
    call(user_id=other)
    sql("DELETE FROM users WHERE id = :u", {"u": other})
    assert sql("SELECT count(*) FROM ai_usage").scalar_one() == 0
