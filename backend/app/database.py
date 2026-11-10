import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

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
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.types import UserDefinedType
from dotenv import load_dotenv

from app.services.chunking import Chunk
from app.services.embeddings import EMBEDDING_DIMENSIONS


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


# Postgres with pgvector: Neon in production, Docker locally (docker-compose.yml).
DATABASE_URL = os.getenv("DATABASE_URL")


class Vector(UserDefinedType):
    """pgvector's `vector(n)` column type (used by the frozen pre-Alembic schema in tests).

    Values are written and searched with plain SQL (`CAST(:v AS vector)`), so no
    pgvector Python package is needed.
    """

    cache_ok = True

    def __init__(self, dimensions: int):
        self.dimensions = dimensions

    def get_col_spec(self, **kwargs) -> str:
        return f"vector({self.dimensions})"


# The schema is created and changed only by Alembic migrations (backend/migrations).
# This table object is used to build queries; it never creates anything. Chunks are
# written and searched with plain SQL below.
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
    Column("owner_user_id", Integer, nullable=True),
)


@dataclass(frozen=True)
class ChunkMatch:
    chunk_id: int
    document_id: int
    filename: str
    page_number: int | None
    chunk_index: int
    content: str
    similarity: float  # cosine similarity: 1 = same direction, 0 = unrelated, -1 = opposite

_engines: dict[str, Engine] = {}


def sqlalchemy_url(url: str) -> str:
    """A Neon or Docker PostgreSQL URL with SQLAlchemy's psycopg driver added.

    Also used by the Alembic migrations (migrations/env.py)."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def _database_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Start the local database with "
            "`docker compose up -d` and set DATABASE_URL in backend/.env (see README)."
        )
    # Neon and Docker give a PostgreSQL URL without a SQLAlchemy driver suffix.
    return sqlalchemy_url(DATABASE_URL)


def _engine() -> Engine:
    url = _database_url()
    if url not in _engines:
        _engines[url] = create_engine(
            url,
            pool_pre_ping=True,
            # Never use server-side prepared statements: behind a transaction pooler
            # (Neon's pooled connection string, PgBouncer) consecutive queries can run on
            # different server connections, where a prepared statement doesn't exist.
            connect_args={"prepare_threshold": None},
        )
    return _engines[url]


@contextmanager
def get_connection() -> Iterator[Connection]:
    with _engine().begin() as connection:
        yield connection


MIGRATE_HINT = (
    "Run `cd backend && python -m alembic upgrade head` "
    "(see README, \"Database migrations\")."
)


def check_database():
    """Startup check. Only reads: the schema is created and changed by Alembic migrations.

    Fails if the database is not migrated to the newest revision, or if its embedding
    column doesn't match EMBEDDING_DIMENSIONS.
    """
    from app.migrate import head_revision  # here, because app.migrate imports this module

    with _engine().connect() as connection:
        migrated = connection.execute(
            text("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        ).scalar_one()
        if not migrated:
            raise RuntimeError(
                "The database has no migration history. For a new database: "
                f"{MIGRATE_HINT} For a database created before migrations existed, check it "
                "with `python -m app.schema_check`, then mark it with "
                "`python -m alembic stamp 0001_baseline`."
            )
        current = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
        head = head_revision()
        if current != [head]:
            found = ", ".join(current) or "none"
            raise RuntimeError(
                f"The database schema is at revision {found}, but this code needs {head}. "
                f"{MIGRATE_HINT}"
            )
        _check_embedding_dimensions(connection)


def _check_embedding_dimensions(connection: Connection):
    """Fail at startup if the chunks table was created for a different embedding size."""
    # For pgvector columns, atttypmod holds the number of dimensions.
    stored = connection.execute(
        text(
            "SELECT atttypmod FROM pg_attribute "
            "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding' AND NOT attisdropped"
        )
    ).scalar_one()
    if stored != EMBEDDING_DIMENSIONS:
        raise RuntimeError(
            f"The chunks table stores {stored}-dimension embeddings, but "
            f"EMBEDDING_DIMENSIONS is {EMBEDDING_DIMENSIONS}. Changing the embedding model "
            "means re-indexing: drop the chunks table (or reset the local database) and upload "
            "the documents again."
        )


def _vector_literal(vector: Sequence[float]) -> str:
    """pgvector's text format: '[0.1,0.2,...]'."""
    if len(vector) != EMBEDDING_DIMENSIONS:
        raise ValueError(
            f"Expected a {EMBEDDING_DIMENSIONS}-dimension vector, got {len(vector)}."
        )
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


def _insert_document(
    connection: Connection,
    filename: str,
    file_type: str,
    file_size: int,
    file_content: bytes,
    extracted_text: str,
    summary: str | None,
    owner_user_id: int,
) -> int:
    result = connection.execute(
        documents.insert().values(
            filename=filename,
            file_type=file_type,
            file_size=file_size,
            file_content=file_content,
            extracted_text=extracted_text,
            summary=summary,
            owner_user_id=owner_user_id,
        )
    )
    return result.inserted_primary_key[0]


def save_document_with_chunks(
    filename: str,
    file_type: str,
    file_size: int,
    file_content: bytes,
    extracted_text: str,
    summary: str | None,
    document_chunks: Sequence[Chunk],
    embeddings: Sequence[Sequence[float]],
    *,
    owner_user_id: int,
) -> int:
    """Save a user's document and its chunks in one transaction: all of it or nothing."""
    if len(document_chunks) != len(embeddings):
        raise ValueError(
            f"Got {len(document_chunks)} chunks but {len(embeddings)} embeddings."
        )
    rows = [
        {
            "chunk_index": chunk.index,
            "page_number": chunk.page_number,
            "content": chunk.text,
            "token_estimate": chunk.token_estimate,
            "embedding": _vector_literal(embedding),
        }
        for chunk, embedding in zip(document_chunks, embeddings)
    ]

    with get_connection() as connection:
        document_id = _insert_document(
            connection, filename, file_type, file_size, file_content, extracted_text, summary,
            owner_user_id,
        )
        if rows:
            connection.execute(
                text(
                    "INSERT INTO chunks "
                    "(document_id, chunk_index, page_number, content, token_estimate, embedding) "
                    "VALUES (:document_id, :chunk_index, :page_number, :content, "
                    ":token_estimate, CAST(:embedding AS vector))"
                ),
                [{**row, "document_id": document_id} for row in rows],
            )
        return document_id


_SEARCH_CHUNKS_SQL = text(
    """
    SELECT
        c.id AS chunk_id,
        c.document_id,
        d.filename,
        c.page_number,
        c.chunk_index,
        c.content,
        1 - (c.embedding <=> CAST(:query AS vector)) AS similarity
    FROM chunks AS c
    JOIN documents AS d ON d.id = c.document_id
    WHERE d.owner_user_id = :owner_user_id
      AND (CAST(:document_ids AS integer[]) IS NULL
           OR c.document_id = ANY(CAST(:document_ids AS integer[])))
    ORDER BY c.embedding <=> CAST(:query AS vector)
    LIMIT :k
    """
)


def search_chunks(
    query_embedding: Sequence[float],
    k: int = 8,
    document_ids: Sequence[int] | None = None,
    min_similarity: float = 0.0,
    *,
    owner_user_id: int,
) -> list[ChunkMatch]:
    """The k chunks of the user's documents closest to the query, most similar first.

    Only documents owned by `owner_user_id` are searched. `document_ids=None` searches all
    of them; a list limits the search to those.
    Chunks below `min_similarity` are dropped. 0 (the default) disables the threshold
    and keeps everything, including negative similarities.
    """
    if k <= 0:
        raise ValueError("k must be positive.")
    if document_ids is not None and not document_ids:
        return []

    with get_connection() as connection:
        # The owner filter (and any document filter) is applied after the HNSW index finds
        # its nearest chunks. Let the index keep scanning until it finds k chunks that pass
        # (pgvector >= 0.8); otherwise other users' chunks could leave fewer than k.
        connection.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        rows = connection.execute(
            _SEARCH_CHUNKS_SQL,
            {
                "query": _vector_literal(query_embedding),
                "document_ids": list(document_ids) if document_ids is not None else None,
                "k": k,
                "owner_user_id": owner_user_id,
            },
        ).mappings().all()

    matches = [ChunkMatch(**row) for row in rows]
    if min_similarity <= 0:
        return matches
    return [match for match in matches if match.similarity >= min_similarity]


_FULL_TEXT_SEARCH_SQL = text(
    """
    SELECT
        c.id AS chunk_id,
        c.document_id,
        d.filename,
        c.page_number,
        c.chunk_index,
        c.content,
        1 - (c.embedding <=> CAST(:query AS vector)) AS similarity
    FROM chunks AS c
    JOIN documents AS d ON d.id = c.document_id
    WHERE c.content_tsv @@ to_tsquery('simple', :tsquery)
      AND d.owner_user_id = :owner_user_id
      AND (CAST(:document_ids AS integer[]) IS NULL
           OR c.document_id = ANY(CAST(:document_ids AS integer[])))
    ORDER BY ts_rank_cd(c.content_tsv, to_tsquery('simple', :tsquery)) DESC, c.id
    LIMIT :k
    """
)


def full_text_search_chunks(
    query_embedding: Sequence[float],
    tsquery: str,
    k: int = 20,
    document_ids: Sequence[int] | None = None,
    *,
    owner_user_id: int,
) -> list[ChunkMatch]:
    """The k chunks of the user's documents whose words best match `tsquery` (Postgres
    full-text search), best first. Only documents owned by `owner_user_id` are searched.

    `tsquery` must be a to_tsquery expression of plain words (see services/retrieval.py).
    `query_embedding` is only used to report each chunk's cosine similarity.
    """
    if k <= 0:
        raise ValueError("k must be positive.")
    if document_ids is not None and not document_ids:
        return []
    with get_connection() as connection:
        rows = connection.execute(
            _FULL_TEXT_SEARCH_SQL,
            {
                "query": _vector_literal(query_embedding),
                "tsquery": tsquery,
                "document_ids": list(document_ids) if document_ids is not None else None,
                "k": k,
                "owner_user_id": owner_user_id,
            },
        ).mappings().all()
    return [ChunkMatch(**row) for row in rows]


def existing_document_ids(document_ids: Sequence[int], *, owner_user_id: int) -> set[int]:
    """The subset of `document_ids` that exist and belong to the user."""
    if not document_ids:
        return set()
    with get_connection() as connection:
        rows = connection.execute(
            text("SELECT id FROM documents "
                 "WHERE id = ANY(CAST(:ids AS integer[])) AND owner_user_id = :owner_user_id"),
            {"ids": list(document_ids), "owner_user_id": owner_user_id},
        ).scalars()
        return set(rows)


def delete_document(document_id: int, *, owner_user_id: int) -> bool:
    """Delete one of the user's documents. False if there is no such document for this user
    (another user's document counts as not found)."""
    # The chunks foreign key is ON DELETE CASCADE, so this also deletes the chunks.
    with get_connection() as connection:
        result = connection.execute(
            documents.delete().where(
                documents.c.id == document_id,
                documents.c.owner_user_id == owner_user_id,
            )
        )
        return result.rowcount > 0
