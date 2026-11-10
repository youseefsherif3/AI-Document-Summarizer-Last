"""Alembic from code: the current head revision (for the startup check), and upgrading the
disposable test and eval databases. Production is migrated by hand with the alembic
command (see README, "Database migrations")."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.database import sqlalchemy_url

BACKEND_DIR = Path(__file__).resolve().parents[1]
DISPOSABLE_SUFFIXES = ("_test", "_eval")


def alembic_config(url: str | None = None) -> Config:
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.attributes["configure_logging"] = False
    if url:
        config.attributes["url"] = url
    return config


def head_revision() -> str:
    """The newest migration in migrations/versions (there is exactly one head)."""
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("No Alembic migrations found in backend/migrations/versions.")
    return head


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def reset_and_upgrade(url: str) -> None:
    """Drop everything in a disposable database and build it from the migrations.

    Only for databases whose name ends in _test or _eval (their data is thrown away anyway),
    so a test or eval run always uses exactly the schema the migrations create.
    """
    name = make_url(url).database or ""
    if not name.endswith(DISPOSABLE_SUFFIXES):
        raise RuntimeError(
            f"Refusing to reset database {name!r}: only *_test and *_eval databases are reset."
        )
    engine = create_engine(sqlalchemy_url(url))
    try:
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()
    upgrade(url)
