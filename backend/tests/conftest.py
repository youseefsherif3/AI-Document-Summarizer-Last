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
    """The test database, rebuilt from the Alembic migrations once per test run."""
    from app.migrate import reset_and_upgrade

    url = _test_database_url()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(database, "DATABASE_URL", url)
        try:
            reset_and_upgrade(url)
            database.check_database()
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
        # CASCADE also empties chunks (via documents) and sessions and daily_usage (via users).
        connection.execute(text(
            "TRUNCATE TABLE documents, users, login_attempts RESTART IDENTITY CASCADE"
        ))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_auth: the test uses real sign-in and CSRF checks, with no default signed-in user",
    )


@pytest.fixture(autouse=True)
def default_user(request, clean_database):
    """Most tests act as one signed-in user (DEFAULT_USER_ID), with the session and CSRF
    checks replaced, so they can focus on what they test.

    Tests of sign-in, CSRF and data isolation are marked `real_auth`: they get no default
    user and go through the real checks. The isolation tests also check that every route
    requires a session, so this shortcut can't hide a missing check.
    """
    import app.auth as auth
    from tests.default_user import DEFAULT_USER_EMAIL, DEFAULT_USER_ID

    if request.node.get_closest_marker("real_auth"):
        yield None
        return
    with database.get_connection() as connection:
        user_id = connection.execute(
            text("INSERT INTO users (email, password_hash) VALUES (:email, 'not a real hash') "
                 "RETURNING id"),
            {"email": DEFAULT_USER_EMAIL},
        ).scalar_one()
    assert user_id == DEFAULT_USER_ID
    user = auth.User(id=user_id, email=DEFAULT_USER_EMAIL)
    main.app.dependency_overrides[auth.current_user] = lambda: user
    main.app.dependency_overrides[auth.require_trusted_origin] = lambda: None
    try:
        yield user
    finally:
        main.app.dependency_overrides.pop(auth.current_user, None)
        main.app.dependency_overrides.pop(auth.require_trusted_origin, None)
