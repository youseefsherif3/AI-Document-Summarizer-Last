"""Alembic environment: connects to the database and runs the migrations.

The URL comes from, in order:
1. `config.attributes["url"]`, set by code that runs Alembic itself (tests);
2. MIGRATION_DATABASE_URL, for production: Neon's direct (unpooled) connection string,
   because schema changes should not go through the transaction pooler;
3. DATABASE_URL, which is fine locally (Docker).
"""

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine

# Importing app.database loads backend/.env, so the URLs can live there.
from app.database import sqlalchemy_url

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logging", True):
    fileConfig(config.config_file_name)

# Migrations are written by hand (raw SQL for pgvector, HNSW and the generated column),
# so there is no metadata to compare against and no autogenerate.
target_metadata = None


def _url() -> str:
    url = (
        config.attributes.get("url")
        or os.getenv("MIGRATION_DATABASE_URL", "").strip()
        or os.getenv("DATABASE_URL", "").strip()
    )
    if not url:
        raise RuntimeError(
            "No database to migrate: set MIGRATION_DATABASE_URL or DATABASE_URL "
            "(see backend/.env.example)."
        )
    return sqlalchemy_url(url)


def run_migrations_offline() -> None:
    """`alembic upgrade --sql`: print the SQL instead of running it."""
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Same safeguard as the app: no server-side prepared statements, so a pooled
    # connection string (PgBouncer) also works.
    engine = create_engine(_url(), connect_args={"prepare_threshold": None})
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
