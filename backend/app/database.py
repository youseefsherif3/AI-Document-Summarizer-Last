import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Sequence

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    inspect,
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
    """pgvector's `vector(n)` column type, for table creation only.

    Values are written and searched with plain SQL (`CAST(:v AS vector)`), so no
    pgvector Python package is needed.
    """

    cache_ok = True

    def __init__(self, dimensions: int):
        self.dimensions = dimensions

    def get_col_spec(self, **kwargs) -> str:
        return f"vector({self.dimensions})"


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

chunks = Table(
    "chunks",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    # Deleting a document deletes its chunks in the same statement.
    Column(
        "document_id",
        Integer,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("chunk_index", Integer, nullable=False),
    Column("page_number", Integer, nullable=True),
    Column("content", Text, nullable=False),
    Column("token_estimate", Integer, nullable=False),
    Column("embedding", Vector(EMBEDDING_DIMENSIONS), nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("document_id", "chunk_index", name="uq_chunks_document_chunk"),
    Index("ix_chunks_document_id", "document_id"),
    # Approximate nearest-neighbour index for cosine distance (the <=> operator).
    Index(
        "ix_chunks_embedding_hnsw",
        "embedding",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
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

    with engine.begin() as connection:
        _check_embedding_dimensions(connection)
        _add_full_text_search(connection)


def _add_full_text_search(connection: Connection):
    """Full-text search for hybrid retrieval (step 15), added to new and existing databases.

    The 'simple' config lowercases words but does not stem or drop stop words, so Arabic
    and English words match as written.
    """
    connection.execute(text(
        "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS content_tsv tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED"
    ))
    connection.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_chunks_content_tsv ON chunks USING gin (content_tsv)"
    ))


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
) -> int:
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


def save_document_with_chunks(
    filename: str,
    file_type: str,
    file_size: int,
    file_content: bytes,
    extracted_text: str,
    summary: str | None,
    document_chunks: Sequence[Chunk],
    embeddings: Sequence[Sequence[float]],
) -> int:
    """Save a document and its chunks in one transaction: all of it or nothing."""
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
            connection, filename, file_type, file_size, file_content, extracted_text, summary
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
    WHERE CAST(:document_ids AS integer[]) IS NULL
       OR c.document_id = ANY(CAST(:document_ids AS integer[]))
    ORDER BY c.embedding <=> CAST(:query AS vector)
    LIMIT :k
    """
)


def search_chunks(
    query_embedding: Sequence[float],
    k: int = 8,
    document_ids: Sequence[int] | None = None,
    min_similarity: float = 0.0,
) -> list[ChunkMatch]:
    """The k chunks closest to the query, most similar first.

    `document_ids=None` searches every document; a list limits the search to those.
    Chunks below `min_similarity` are dropped. 0 (the default) disables the threshold
    and keeps everything, including negative similarities.
    """
    if k <= 0:
        raise ValueError("k must be positive.")
    if document_ids is not None and not document_ids:
        return []

    with get_connection() as connection:
        if document_ids is not None:
            # With a filter, let the HNSW index keep scanning until it finds k matching
            # chunks (pgvector >= 0.8), instead of returning fewer than k.
            connection.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
        rows = connection.execute(
            _SEARCH_CHUNKS_SQL,
            {
                "query": _vector_literal(query_embedding),
                "document_ids": list(document_ids) if document_ids is not None else None,
                "k": k,
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
) -> list[ChunkMatch]:
    """The k chunks whose words best match `tsquery` (Postgres full-text search), best first.

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
            },
        ).mappings().all()
    return [ChunkMatch(**row) for row in rows]


def existing_document_ids(document_ids: Sequence[int]) -> set[int]:
    """The subset of `document_ids` that exist."""
    if not document_ids:
        return set()
    with get_connection() as connection:
        rows = connection.execute(
            text("SELECT id FROM documents WHERE id = ANY(CAST(:ids AS integer[]))"),
            {"ids": list(document_ids)},
        ).scalars()
        return set(rows)


def delete_document(document_id: int):
    # The chunks foreign key is ON DELETE CASCADE, so this also deletes the chunks.
    with get_connection() as connection:
        result = connection.execute(
            documents.delete().where(documents.c.id == document_id)
        )
        return result.rowcount > 0
