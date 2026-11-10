import os

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

# Importing app.database loads backend/.env, so TEST_DATABASE_URL can live there.
import app.database as database
import app.main as main

SETUP_HINT = (
    "Start the database with `docker compose up -d` (from the project root) and set "
    "TEST_DATABASE_URL in backend/.env, for example "
    "postgresql://docsum:docsum@127.0.0.1:5432/docsum_test"
)


def _location(url: str) -> tuple:
    parsed = make_url(url)
    return (parsed.host, parsed.port or 5432, parsed.database)


def _test_database_url() -> str:
    url = os.getenv("TEST_DATABASE_URL", "").strip()
    if not url:
        pytest.exit(f"TEST_DATABASE_URL is not set. {SETUP_HINT}", returncode=2)

    # Every test empties the tables, so never point the tests at real data.
    if not (make_url(url).database or "").endswith("_test"):
        pytest.exit(
            "TEST_DATABASE_URL must point to a database whose name ends in '_test'; "
            "tests delete all of its data.",
            returncode=2,
        )
    if database.DATABASE_URL and _location(url) == _location(database.DATABASE_URL):
        pytest.exit(
            "TEST_DATABASE_URL must not be the same database as DATABASE_URL; "
            "tests delete all of its data.",
            returncode=2,
        )
    return url


@pytest.fixture(scope="session", autouse=True)
def test_database():
    url = _test_database_url()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(database, "DATABASE_URL", url)
        try:
            database.initialize_database()
        except OperationalError as error:
            pytest.exit(
                f"Cannot connect to the test database ({error.orig}). {SETUP_HINT}",
                returncode=2,
            )
        yield url
        for engine in database._engines.values():
            engine.dispose()


@pytest.fixture(autouse=True)
def fake_embeddings(monkeypatch):
    """No real embedding calls in tests: uploads get a fixed vector per chunk.

    Returns the list of calls (each a list of texts). Tests can override it with
    their own monkeypatch of main.embed_texts.
    """
    calls = []

    async def fake_embed_texts(texts):
        calls.append(list(texts))
        return [[1.0] + [0.0] * (database.EMBEDDING_DIMENSIONS - 1) for _ in texts]

    monkeypatch.setattr(main, "embed_texts", fake_embed_texts)
    return calls


@pytest.fixture(autouse=True)
def retry_waits(monkeypatch):
    """AI call retries (services/ai_calls.py) don't really wait in tests.

    Returns the list of waits, in seconds, that would have happened.
    """
    import app.services.ai_calls as ai_calls

    waits = []

    async def no_wait(seconds):
        waits.append(seconds)

    monkeypatch.setattr(ai_calls, "_sleep", no_wait)
    return waits


@pytest.fixture(autouse=True)
def query_rewrite_off(monkeypatch):
    """Query rewriting calls a model, so it is off unless a test turns it on (with a fake)."""
    monkeypatch.delenv("QUERY_REWRITE_ENABLED", raising=False)


@pytest.fixture(autouse=True)
def fake_query_embedding(monkeypatch):
    """No real embedding calls for chat questions: the same fixed vector as fake chunks."""
    import app.services.chat as chat

    async def fake_embed_query(text):
        return [1.0] + [0.0] * (database.EMBEDDING_DIMENSIONS - 1)

    monkeypatch.setattr(chat, "embed_query", fake_embed_query)


@pytest.fixture(autouse=True)
def clean_database(test_database):
    with database.get_connection() as connection:
        connection.execute(text("TRUNCATE TABLE documents RESTART IDENTITY CASCADE"))
