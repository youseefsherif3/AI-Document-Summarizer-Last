"""Baseline: the schema as it was before Alembic (Phase 2).

Reproduces exactly what app.database.initialize_database() created at startup:
`documents`, `chunks` with a pgvector embedding and its HNSW index, and the generated
full-text column with its GIN index (tests/test_migrations.py checks it is identical).

Existing databases built by that startup code are not migrated with this revision; they
are stamped (`alembic stamp 0001_baseline`) after `python -m app.schema_check` confirms
they match.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-11-10
"""

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

# Fixed here on purpose: a migration must not change with settings. The app checks at
# startup that EMBEDDING_DIMENSIONS matches the column.
EMBEDDING_DIMENSIONS = 1536


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute(
        """
        CREATE TABLE documents (
            id SERIAL NOT NULL,
            filename VARCHAR NOT NULL,
            file_type VARCHAR NOT NULL,
            file_size INTEGER NOT NULL,
            file_content BYTEA NOT NULL,
            extracted_text TEXT NOT NULL,
            summary TEXT,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT documents_pkey PRIMARY KEY (id)
        )
        """
    )

    op.execute(
        f"""
        CREATE TABLE chunks (
            id SERIAL NOT NULL,
            document_id INTEGER NOT NULL,
            chunk_index INTEGER NOT NULL,
            page_number INTEGER,
            content TEXT NOT NULL,
            token_estimate INTEGER NOT NULL,
            embedding vector({EMBEDDING_DIMENSIONS}) NOT NULL,
            created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            content_tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
            CONSTRAINT chunks_pkey PRIMARY KEY (id),
            CONSTRAINT chunks_document_id_fkey FOREIGN KEY (document_id)
                REFERENCES documents (id) ON DELETE CASCADE,
            CONSTRAINT uq_chunks_document_chunk UNIQUE (document_id, chunk_index)
        )
        """
    )
    op.execute("CREATE INDEX ix_chunks_document_id ON chunks USING btree (document_id)")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX ix_chunks_content_tsv ON chunks USING gin (content_tsv)")


def downgrade() -> None:
    # The vector extension stays: other objects may use it, and dropping it needs more
    # rights than the app has.
    op.execute("DROP TABLE chunks")
    op.execute("DROP TABLE documents")
