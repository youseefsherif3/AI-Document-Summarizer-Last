import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    inspect,
    text,
)
from sqlalchemy.engine import Connection, Engine
from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


# Postgres with pgvector: Neon in production, Docker locally (docker-compose.yml).
DATABASE_URL = os.getenv("DATABASE_URL")

metadata = MetaData()
documents = Table(
    "documents",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("filename", String, nullable=False),
    Column("file_type", String, nullable=False),
    Column("file_size", Integer, nullable=False),
    Column("file_content", LargeBinary, nullable=False),
    Column("extracted_text", Text, nullable=False),
    Column("summary", Text, nullable=True),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
)

_engines: dict[str, Engine] = {}


def _database_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Start the local database with "
            "`docker compose up -d` and set DATABASE_URL in backend/.env (see README)."
        )

    # Neon and Docker give a PostgreSQL URL without a SQLAlchemy driver suffix.
    url = DATABASE_URL
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _engine() -> Engine:
    url = _database_url()
    if url not in _engines:
        _engines[url] = create_engine(url, pool_pre_ping=True)
    return _engines[url]


@contextmanager
def get_connection() -> Iterator[Connection]:
    with _engine().begin() as connection:
        yield connection


def initialize_database():
    engine = _engine()
    with engine.begin() as connection:
        # pgvector, used by the RAG chunks table (Phase 2).
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    metadata.create_all(engine)

    # Support existing databases created before summaries were stored.
    columns = {column["name"] for column in inspect(engine).get_columns("documents")}
    if "summary" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE documents ADD COLUMN summary TEXT"))


def save_document(
    filename: str,
    file_type: str,
    file_size: int,
    file_content: bytes,
    extracted_text: str,
    summary: str | None = None,
):
    with get_connection() as connection:
        result = connection.execute(
            documents.insert().values(
                filename=filename,
                file_type=file_type,
                file_size=file_size,
                file_content=file_content,
                extracted_text=extracted_text,
                summary=summary,
            )
        )
        return result.inserted_primary_key[0]


def delete_document(document_id: int):
    with get_connection() as connection:
        result = connection.execute(
            documents.delete().where(documents.c.id == document_id)
        )
        return result.rowcount > 0
