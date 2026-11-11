"""Alembic migrations: the baseline reproduces the old startup schema exactly, existing
databases can be stamped without changes, and migrations run up and down cleanly.

Each test builds its own scratch database (name ending in `_test`) on the same server as
TEST_DATABASE_URL and drops it afterwards.
"""

import os
import uuid

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import app.database as database
import app.main as main
from app import schema_check
from app.migrate import alembic_config, head_revision, reset_and_upgrade
from tests.legacy_startup_schema import build_legacy_schema


def _admin_engine():
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(
        drivername="postgresql+psycopg", database="postgres"
    )
    return create_engine(url, isolation_level="AUTOCOMMIT")


@pytest.fixture
def scratch_database():
    """Factory: each call creates an empty database and returns its URL."""
    created = []
    admin = _admin_engine()

    def create() -> str:
        name = f"docsum_mig_{uuid.uuid4().hex[:10]}_test"
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        created.append(name)
        return make_url(os.environ["TEST_DATABASE_URL"]).set(database=name).render_as_string(
            hide_password=False
        )

    yield create

    for engine in list(database._engines.values()):
        engine.dispose()
    with admin.connect() as connection:
        for name in created:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def build_with_old_startup_code(url: str, monkeypatch=None) -> None:
    """The schema as the app created it before Alembic (a frozen copy of that code)."""
    build_legacy_schema(url)


@pytest.fixture
def app_database(monkeypatch):
    """Point the app at another database for one test; the test database comes back after."""

    def use(url: str) -> None:
        monkeypatch.setattr(database, "DATABASE_URL", url)

    return use


def explain(expected: dict, actual: dict) -> str:
    return "\n".join(schema_check.differences(expected, actual))


def test_there_is_one_head():
    script = ScriptDirectory.from_config(alembic_config())
    assert len(script.get_heads()) == 1
    assert head_revision() == script.get_heads()[0]


def test_baseline_migration_matches_the_old_startup_schema_exactly(scratch_database, monkeypatch):
    old_url = scratch_database()
    migrated_url = scratch_database()
    build_with_old_startup_code(old_url, monkeypatch)
    command.upgrade(alembic_config(migrated_url), "0001_baseline")

    old = schema_check.snapshot_of(old_url)
    migrated = schema_check.snapshot_of(migrated_url)
    reference = schema_check.expected_snapshot("0001_baseline")

    assert migrated == old, explain(old, migrated)
    assert old == reference, explain(reference, old)
    # The parts that are easy to get wrong, spelled out:
    chunks = migrated["tables"]["chunks"]
    assert "vector" in migrated["extensions"]
    assert chunks["columns"]["embedding"]["type"] == "vector(1536)"
    assert chunks["columns"]["content_tsv"]["generated"] == "to_tsvector('simple'::regconfig, content)"
    assert "USING hnsw (embedding vector_cosine_ops)" in chunks["indexes"]["ix_chunks_embedding_hnsw"]
    assert "USING gin (content_tsv)" in chunks["indexes"]["ix_chunks_content_tsv"]
    assert chunks["constraints"]["chunks_document_id_fkey"].endswith("ON DELETE CASCADE")


def test_upgrade_downgrade_upgrade_on_an_empty_database(scratch_database):
    url = scratch_database()
    config = alembic_config(url)

    command.upgrade(config, "0001_baseline")
    assert schema_check.snapshot_of(url) == schema_check.expected_snapshot("0001_baseline")

    command.upgrade(config, "head")
    at_head = schema_check.snapshot_of(url)

    command.downgrade(config, "base")
    after_downgrade = schema_check.snapshot_of(url)
    assert after_downgrade["tables"] == {}
    assert after_downgrade["extensions"] == ["vector"]  # kept on purpose

    command.upgrade(config, "head")
    assert schema_check.snapshot_of(url) == at_head, explain(at_head, schema_check.snapshot_of(url))


def test_auth_tables_migration_adds_four_tables_and_downgrades_to_the_exact_baseline(
    scratch_database,
):
    url = scratch_database()
    config = alembic_config(url)
    command.upgrade(config, "0002_auth_tables")

    tables = schema_check.snapshot_of(url)["tables"]
    assert set(tables) == {"documents", "chunks", "users", "sessions", "login_attempts", "daily_usage"}
    # The existing tables are untouched by this migration.
    reference = schema_check.expected_snapshot("0001_baseline")["tables"]
    assert tables["documents"] == reference["documents"]
    assert tables["chunks"] == reference["chunks"]

    command.downgrade(config, "0001_baseline")
    after = schema_check.snapshot_of(url)
    assert after == schema_check.expected_snapshot("0001_baseline"), explain(
        schema_check.expected_snapshot("0001_baseline"), after
    )


def test_document_owner_migration_adds_the_column_and_index_and_downgrades_cleanly(
    scratch_database,
):
    url = scratch_database()
    config = alembic_config(url)
    command.upgrade(config, "0002_auth_tables")
    before = schema_check.snapshot_of(url)

    command.upgrade(config, "0003_document_owner")
    documents = schema_check.snapshot_of(url)["tables"]["documents"]
    assert documents["columns"]["owner_user_id"] == {
        "default": None, "generated": None, "not_null": False, "type": "integer",
    }
    assert documents["constraints"]["documents_owner_user_id_fkey"] == (
        "FOREIGN KEY (owner_user_id) REFERENCES users(id) ON DELETE CASCADE"
    )
    assert documents["indexes"]["ix_documents_owner_created"].endswith(
        "USING btree (owner_user_id, created_at DESC, id DESC)"
    )

    command.downgrade(config, "0002_auth_tables")
    after = schema_check.snapshot_of(url)
    assert after == before, explain(before, after)


def test_jobs_migration_adds_the_table_and_downgrades_to_the_previous_schema(scratch_database):
    url = scratch_database()
    config = alembic_config(url)
    command.upgrade(config, "0003_document_owner")
    before = schema_check.snapshot_of(url)

    command.upgrade(config, "0004_processing_jobs")
    jobs = schema_check.snapshot_of(url)["tables"]["processing_jobs"]
    assert jobs["columns"]["status"]["default"] == "'pending'::text"
    assert jobs["columns"]["owner_user_id"]["not_null"] is True
    assert jobs["constraints"]["processing_jobs_owner_user_id_fkey"].endswith("ON DELETE CASCADE")
    assert jobs["constraints"]["processing_jobs_document_id_fkey"].endswith("ON DELETE SET NULL")
    assert "(status = ANY (ARRAY['pending'::text, 'processing'::text, 'completed'::text, "        "'failed'::text]))" in " ".join(jobs["constraints"].values())
    assert "ix_processing_jobs_owner_status" in jobs["indexes"]

    command.downgrade(config, "0003_document_owner")
    after = schema_check.snapshot_of(url)
    assert after == before, explain(before, after)


def test_ai_usage_migration_adds_the_table_and_downgrades_to_the_previous_schema(
    scratch_database,
):
    url = scratch_database()
    config = alembic_config(url)
    command.upgrade(config, "0004_processing_jobs")
    before = schema_check.snapshot_of(url)

    command.upgrade(config, "0005_ai_usage")
    usage = schema_check.snapshot_of(url)["tables"]["ai_usage"]
    assert usage["columns"]["user_id"]["not_null"] is True
    assert usage["columns"]["duration_ms"]["not_null"] is True
    assert usage["constraints"]["ai_usage_user_id_fkey"].endswith("ON DELETE CASCADE")
    assert "ix_ai_usage_user_created" in usage["indexes"]

    command.downgrade(config, "0004_processing_jobs")
    after = schema_check.snapshot_of(url)
    assert after == before, explain(before, after)


def test_existing_documents_have_no_owner_after_the_migration(scratch_database):
    url = scratch_database()
    config = alembic_config(url)
    command.upgrade(config, "0002_auth_tables")
    engine = create_engine(database.sqlalchemy_url(url))
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO documents (filename, file_type, file_size, file_content, extracted_text) "
            "VALUES ('old.txt', '.txt', 4, 'text', 'text')"
        ))

    command.upgrade(config, "0003_document_owner")

    with engine.connect() as connection:
        assert connection.execute(text("SELECT owner_user_id FROM documents")).scalar_one() is None
    engine.dispose()


def test_stamping_an_existing_database_changes_nothing(scratch_database, monkeypatch, capsys):
    url = scratch_database()
    build_with_old_startup_code(url, monkeypatch)
    engine = create_engine(database.sqlalchemy_url(url))
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO documents (filename, file_type, file_size, file_content, extracted_text, summary) "
            "VALUES ('kept.txt', '.txt', 4, 'kept', 'kept', 'A summary.')"
        ))

    assert schema_check.main(["--url", url]) == 0
    assert "matches 0001_baseline" in capsys.readouterr().out
    before = schema_check.snapshot_of(url)

    command.stamp(alembic_config(url), "0001_baseline")

    assert schema_check.snapshot_of(url) == before
    with engine.connect() as connection:
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0001_baseline"
        assert connection.execute(text("SELECT filename FROM documents")).scalars().all() == ["kept.txt"]
    engine.dispose()
    # Upgrading a stamped database to the baseline is a no-op.
    command.upgrade(alembic_config(url), "0001_baseline")
    assert schema_check.snapshot_of(url) == before


def test_a_stamped_old_database_upgrades_to_head_and_the_app_starts(
    scratch_database, app_database
):
    """The production path: old schema, stamp, upgrade head, and the startup check passes."""
    url = scratch_database()
    build_with_old_startup_code(url)
    command.stamp(alembic_config(url), "0001_baseline")
    command.upgrade(alembic_config(url), "head")

    app_database(url)
    database.check_database()
    with TestClient(main.app) as client:
        assert client.get("/health").json() == {"status": "healthy"}


# --- The startup check -------------------------------------------------------------------


def test_startup_fails_on_a_database_that_was_never_migrated(scratch_database, app_database):
    url = scratch_database()
    build_with_old_startup_code(url)  # tables exist, but no migration history
    app_database(url)

    with pytest.raises(RuntimeError, match="no migration history") as raised:
        database.check_database()
    message = str(raised.value)
    assert "python -m alembic upgrade head" in message
    assert "python -m app.schema_check" in message
    assert "python -m alembic stamp 0001_baseline" in message

    # The app refuses to start, rather than serving requests on an unknown schema.
    with pytest.raises(RuntimeError, match="no migration history"):
        with TestClient(main.app):
            pass


def test_startup_fails_on_an_empty_database(scratch_database, app_database):
    app_database(scratch_database())

    with pytest.raises(RuntimeError, match="no migration history"):
        database.check_database()


def test_startup_fails_when_the_database_is_behind(scratch_database, app_database):
    url = scratch_database()
    command.upgrade(alembic_config(url), "0001_baseline")
    engine = create_engine(database.sqlalchemy_url(url))
    with engine.begin() as connection:  # simulate a database one revision behind the code
        connection.execute(text("UPDATE alembic_version SET version_num = 'an_older_revision'"))
    engine.dispose()
    app_database(url)

    with pytest.raises(RuntimeError, match=(
        f"at revision an_older_revision, but this code needs {head_revision()}"
    )) as raised:
        database.check_database()
    assert "python -m alembic upgrade head" in str(raised.value)


def test_startup_check_never_changes_the_database(scratch_database, app_database):
    url = scratch_database()
    build_with_old_startup_code(url)
    before = schema_check.snapshot_of(url)
    app_database(url)

    with pytest.raises(RuntimeError):
        database.check_database()
    assert schema_check.snapshot_of(url) == before


def test_reset_refuses_databases_that_are_not_disposable():
    with pytest.raises(RuntimeError, match="only \\*_test and \\*_eval"):
        reset_and_upgrade("postgresql://docsum:docsum@127.0.0.1:5432/docsum")


def test_schema_check_reports_each_difference(scratch_database, monkeypatch, capsys):
    url = scratch_database()
    build_with_old_startup_code(url, monkeypatch)
    engine = create_engine(database.sqlalchemy_url(url))
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE documents ADD COLUMN extra INTEGER"))
        connection.execute(text("DROP INDEX ix_chunks_content_tsv"))
        connection.execute(text("ALTER TABLE documents ALTER COLUMN summary SET NOT NULL"))
    engine.dispose()

    assert schema_check.main(["--url", url]) == 1
    output = capsys.readouterr().out
    assert "3 differences" in output
    assert "unexpected: tables.documents.columns.extra (not_null=False, type='integer')" in output
    assert "missing:    tables.chunks.indexes.ix_chunks_content_tsv" in output
    assert "different:  tables.documents.columns.summary.not_null: expected False, found True" in output


def test_schema_check_never_prints_the_database_url(scratch_database, monkeypatch, capsys):
    url = scratch_database()
    build_with_old_startup_code(url, monkeypatch)
    schema_check.main(["--url", url])
    password = make_url(url).password
    out = capsys.readouterr()
    assert url not in out.out + out.err
    if password:
        assert password not in out.out + out.err
