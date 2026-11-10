"""Alembic migrations: the baseline reproduces the old startup schema exactly, existing
databases can be stamped without changes, and migrations run up and down cleanly.

Each test builds its own scratch database (name ending in `_test`) on the same server as
TEST_DATABASE_URL and drops it afterwards.
"""

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

import app.database as database
from app import schema_check

BACKEND_DIR = Path(__file__).resolve().parents[1]


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


def alembic_config(url: str) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["url"] = url
    config.attributes["configure_logging"] = False
    return config


def build_with_old_startup_code(url: str, monkeypatch) -> None:
    """The schema as the app created it before Alembic: initialize_database() at startup."""
    monkeypatch.setattr(database, "DATABASE_URL", url)
    database.initialize_database()
    database._engines.pop(database.sqlalchemy_url(url)).dispose()


def explain(expected: dict, actual: dict) -> str:
    return "\n".join(schema_check.differences(expected, actual))


def test_there_is_one_head_and_it_is_the_baseline():
    script = ScriptDirectory.from_config(alembic_config("postgresql://unused"))
    assert script.get_heads() == ["0001_baseline"]


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

    command.upgrade(config, "head")
    reference = schema_check.expected_snapshot("0001_baseline")
    assert schema_check.snapshot_of(url) == reference

    command.downgrade(config, "base")
    after_downgrade = schema_check.snapshot_of(url)
    assert after_downgrade["tables"] == {}
    assert after_downgrade["extensions"] == ["vector"]  # kept on purpose

    command.upgrade(config, "head")
    assert schema_check.snapshot_of(url) == reference


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
    # Upgrading a stamped database is a no-op.
    command.upgrade(alembic_config(url), "head")
    assert schema_check.snapshot_of(url) == before


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
