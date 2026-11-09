import pytest
from sqlalchemy import text

import app.database as database


def test_tests_use_the_separate_test_database():
    with database.get_connection() as connection:
        name = connection.execute(text("SELECT current_database()")).scalar_one()

    assert name.endswith("_test")


def test_vector_extension_is_installed():
    with database.get_connection() as connection:
        version = connection.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one_or_none()

    assert version is not None


def test_no_server_side_prepared_statements_for_connection_poolers():
    # psycopg prepares a query on the server after 5 runs by default; behind PgBouncer
    # (Neon's pooled URL) that breaks. The engine turns it off.
    with database.get_connection() as connection:
        for _ in range(8):
            connection.execute(text("SELECT count(*) FROM documents")).scalar_one()
        prepared = connection.execute(
            text("SELECT count(*) FROM pg_prepared_statements")
        ).scalar_one()
        threshold = connection.connection.driver_connection.prepare_threshold

    assert threshold is None
    assert prepared == 0


def test_database_url_is_required(monkeypatch):
    monkeypatch.setattr(database, "DATABASE_URL", None)

    with pytest.raises(RuntimeError, match="DATABASE_URL is not set"):
        database._database_url()
