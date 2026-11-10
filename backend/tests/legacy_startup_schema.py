"""A frozen copy of the schema code the app ran at startup before Alembic (Phase 2).

Production and existing local databases were built by this code, so the migration tests
use it to prove that the baseline migration matches and that such databases can be
stamped. It must never change: it records history, not the current schema.
"""

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

from app.database import Vector, sqlalchemy_url

_metadata = MetaData()
Table(
    "documents",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("filename", String, nullable=False),
    Column("file_type", String, nullable=False),
    Column("file_size", Integer, nullable=False),
    Column("file_content", LargeBinary, nullable=False),
    Column("extracted_text", Text, nullable=False),
    Column("summary", Text, nullable=True),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
)
Table(
    "chunks",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("document_id", Integer, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
    Column("chunk_index", Integer, nullable=False),
    Column("page_number", Integer, nullable=True),
    Column("content", Text, nullable=False),
    Column("token_estimate", Integer, nullable=False),
    Column("embedding", Vector(1536), nullable=False),
    Column("created_at", DateTime, server_default=text("CURRENT_TIMESTAMP")),
    UniqueConstraint("document_id", "chunk_index", name="uq_chunks_document_chunk"),
    Index("ix_chunks_document_id", "document_id"),
    Index(
        "ix_chunks_embedding_hnsw",
        "embedding",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    ),
)


def build_legacy_schema(url: str) -> None:
    """What initialize_database() did at startup, step by step."""
    engine = create_engine(sqlalchemy_url(url))
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        _metadata.create_all(engine)
        columns = {column["name"] for column in inspect(engine).get_columns("documents")}
        if "summary" not in columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE documents ADD COLUMN summary TEXT"))
        with engine.begin() as connection:
            connection.execute(text(
                "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS content_tsv tsvector "
                "GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED"
            ))
            connection.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_chunks_content_tsv ON chunks USING gin (content_tsv)"
            ))
    finally:
        engine.dispose()
