"""Daily limits for uploads and chat questions (app/limits.py): counted per user per UTC day,
atomically, before any AI work, with a clear 429 when the limit is reached."""

import threading
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.database as database
import app.limits as limits
import app.main as main
import app.services.chat as chat
from tests.default_user import DEFAULT_USER_ID

DAY = date(2026, 11, 10)


def sql(query, params=None):
    with database.get_connection() as connection:
        return connection.execute(text(query), params or {})


def add_user(email: str) -> int:
    return sql("INSERT INTO users (email, password_hash) VALUES (:email, 'x') RETURNING id",
               {"email": email}).scalar_one()


def usage(user_id=DEFAULT_USER_ID, day=DAY):
    row = sql("SELECT uploads, chat_questions FROM daily_usage WHERE user_id = :u AND day = :d",
              {"u": user_id, "d": day}).first()
    return tuple(row) if row else (0, 0)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    clock = SimpleNamespace(now=datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(limits, "_now", lambda: clock.now)
    monkeypatch.delenv("DAILY_UPLOAD_LIMIT", raising=False)
    monkeypatch.delenv("DAILY_CHAT_LIMIT", raising=False)
    return clock


# --- Settings -----------------------------------------------------------------------------


def test_defaults_are_20_uploads_and_100_chat_questions():
    assert limits.daily_limit("upload") == 20
    assert limits.daily_limit("chat") == 100


def test_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("DAILY_UPLOAD_LIMIT", "3")
    monkeypatch.setenv("DAILY_CHAT_LIMIT", "0")
    assert limits.daily_limit("upload") == 3
    assert limits.daily_limit("chat") == 0


@pytest.mark.parametrize("value", ["abc", "1.5", "-1"])
def test_a_bad_setting_stops_the_app_from_starting(monkeypatch, value):
    monkeypatch.setenv("DAILY_UPLOAD_LIMIT", value)
    with pytest.raises(ValueError, match="DAILY_UPLOAD_LIMIT must"):
        with TestClient(main.app):
            pass


# --- Counting -----------------------------------------------------------------------------


def test_uses_are_counted_up_to_the_limit(monkeypatch):
    monkeypatch.setenv("DAILY_CHAT_LIMIT", "3")
    results = [limits.use_daily_quota(DEFAULT_USER_ID, "chat", DAY) for _ in range(5)]
    assert results == [True, True, True, False, False]
    assert usage() == (0, 3)  # a refused use is not counted


def test_counters_are_separate_per_user_and_per_kind(monkeypatch):
    monkeypatch.setenv("DAILY_CHAT_LIMIT", "1")
    monkeypatch.setenv("DAILY_UPLOAD_LIMIT", "1")
    other = add_user("other@example.com")

    assert limits.use_daily_quota(DEFAULT_USER_ID, "chat", DAY)
    assert not limits.use_daily_quota(DEFAULT_USER_ID, "chat", DAY)
    assert limits.use_daily_quota(DEFAULT_USER_ID, "upload", DAY)  # another kind
    assert limits.use_daily_quota(other, "chat", DAY)              # another user
    assert usage() == (1, 1) and usage(other) == (0, 1)


def test_a_new_utc_day_starts_from_zero(monkeypatch):
    monkeypatch.setenv("DAILY_UPLOAD_LIMIT", "1")
    assert limits.use_daily_quota(DEFAULT_USER_ID, "upload", DAY)
    assert not limits.use_daily_quota(DEFAULT_USER_ID, "upload", DAY)
    assert limits.use_daily_quota(DEFAULT_USER_ID, "upload", DAY + timedelta(days=1))


def test_zero_means_no_limit_but_uses_are_still_counted(monkeypatch):
    monkeypatch.setenv("DAILY_CHAT_LIMIT", "0")
    assert all(limits.use_daily_quota(DEFAULT_USER_ID, "chat", DAY) for _ in range(150))
    assert usage() == (0, 150)


def test_concurrent_requests_never_go_past_the_limit(monkeypatch):
    monkeypatch.setenv("DAILY_CHAT_LIMIT", "5")
    sql("INSERT INTO daily_usage (user_id, day, chat_questions) VALUES (:u, :d, 4)",
        {"u": DEFAULT_USER_ID, "d": DAY})
    start = threading.Barrier(10)
    results = []

    def request():
        start.wait()
        results.append(limits.use_daily_quota(DEFAULT_USER_ID, "chat", DAY))

    threads = [threading.Thread(target=request) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False] * 9 + [True]
    assert usage() == (0, 5)


def test_the_first_use_of_a_new_user_creates_the_row():
    other = add_user("new@example.com")
    assert limits.use_daily_quota(other, "upload", DAY)
    assert usage(other) == (1, 0)


# --- Through the endpoints ----------------------------------------------------------------


@pytest.fixture
def embedded(monkeypatch):
    """Records chat questions that reached retrieval (the first AI call)."""
    questions = []

    async def fake_embed_query(text):
        questions.append(text)
        return [1.0] + [0.0] * (database.EMBEDDING_DIMENSIONS - 1)

    monkeypatch.setattr(chat, "embed_query", fake_embed_query)
    return questions


@pytest.fixture
def summaries(monkeypatch):
    """A fake summary model; records each summary started."""
    started = []

    async def fake_summary(text):
        started.append(text)
        yield "A summary."

    monkeypatch.setattr(main, "stream_summary", fake_summary)
    return started


def ask(client, message="Question?"):
    return client.post("/chat/stream", json={"message": message})


def test_chat_stops_at_the_limit_before_any_ai_call(monkeypatch, embedded, fixed_clock):
    monkeypatch.setenv("DAILY_CHAT_LIMIT", "2")
    fixed_clock.now = datetime(2026, 11, 10, 23, 0, tzinfo=timezone.utc)

    with TestClient(main.app) as client:
        first, second, third = ask(client, "One?"), ask(client, "Two?"), ask(client, "Three?")

    assert first.status_code == second.status_code == 200
    assert third.status_code == 429
    assert third.json() == {
        "detail": "You've reached today's limit of 2 chat questions. It resets at midnight UTC.",
        "code": "daily_limit",
    }
    assert third.headers["retry-after"] == "3600"  # one hour to midnight UTC
    assert embedded == ["One?", "Two?"]  # the third question never reached the AI


def test_chat_works_again_the_next_utc_day(monkeypatch, embedded, fixed_clock):
    monkeypatch.setenv("DAILY_CHAT_LIMIT", "1")
    with TestClient(main.app) as client:
        assert ask(client).status_code == 200
        assert ask(client).status_code == 429
        fixed_clock.now += timedelta(days=1)
        assert ask(client).status_code == 200


def test_an_invalid_chat_request_does_not_use_the_allowance(monkeypatch, embedded):
    monkeypatch.setenv("DAILY_CHAT_LIMIT", "1")
    with TestClient(main.app) as client:
        assert ask(client, "   ").status_code == 400                          # empty question
        response = client.post("/chat/stream", json={"message": "Q?", "document_ids": [999]})
        assert response.status_code == 404                                   # unknown document
        assert ask(client).status_code == 200
    assert usage() == (0, 1)


def test_uploads_stop_at_the_limit_before_the_summary(monkeypatch, summaries):
    monkeypatch.setenv("DAILY_UPLOAD_LIMIT", "1")
    with TestClient(main.app) as client:
        first = client.post("/documents/upload/stream",
                            files={"file": ("one.txt", b"First document.", "text/plain")})
        second = client.post("/documents/upload/stream",
                             files={"file": ("two.txt", b"Second document.", "text/plain")})

    assert '"type": "done"' in first.text
    assert second.status_code == 429
    assert second.json() == {
        "detail": "You've reached today's limit of 1 upload. It resets at midnight UTC.",
        "code": "daily_limit",
    }
    assert summaries == ["First document."]
    assert sql("SELECT filename FROM documents").scalars().all() == ["one.txt"]


def test_an_invalid_file_does_not_use_the_allowance(monkeypatch, summaries):
    monkeypatch.setenv("DAILY_UPLOAD_LIMIT", "1")
    with TestClient(main.app) as client:
        rejected = client.post("/documents/upload/stream",
                               files={"file": ("photo.jpg", b"not text", "image/jpeg")})
        empty = client.post("/documents/upload/stream",
                            files={"file": ("empty.txt", b"", "text/plain")})
        accepted = client.post("/documents/upload/stream",
                               files={"file": ("doc.txt", b"Some text.", "text/plain")})
    assert rejected.status_code == 400 and empty.status_code == 400
    assert '"type": "done"' in accepted.text
    assert usage() == (1, 0)


def test_a_blob_upload_over_the_limit_deletes_the_temporary_blob(monkeypatch, summaries):
    monkeypatch.setenv("DAILY_UPLOAD_LIMIT", "1")
    limits.use_daily_quota(DEFAULT_USER_ID, "upload", DAY)  # today's only upload is used
    content = b"Blob text."
    deleted = []

    async def head(pathname):
        return SimpleNamespace(size=len(content))

    async def get(pathname, *, access):
        return SimpleNamespace(body=content)

    async def delete(pathname):
        deleted.append(pathname)

    monkeypatch.setattr(main.blob, "head_blob", head)
    monkeypatch.setattr(main.blob, "get_blob", get)
    monkeypatch.setattr(main.blob, "delete_blob", delete)

    with TestClient(main.app) as client:
        response = client.post("/documents/upload/stream/blob", json={
            "pathname": "documind/uploads/1/over-limit.txt", "filename": "over.txt",
            "file_size": len(content),
        })

    assert response.status_code == 429
    assert response.json()["code"] == "daily_limit"
    assert deleted == ["documind/uploads/1/over-limit.txt"]
    assert summaries == []


def test_uploads_and_chat_have_separate_allowances(monkeypatch, summaries, embedded):
    monkeypatch.setenv("DAILY_UPLOAD_LIMIT", "1")
    monkeypatch.setenv("DAILY_CHAT_LIMIT", "1")
    with TestClient(main.app) as client:
        upload = client.post("/documents/upload/stream",
                             files={"file": ("doc.txt", b"Some text.", "text/plain")})
        question = ask(client)
    assert '"type": "done"' in upload.text
    assert question.status_code == 200
    assert usage() == (1, 1)
