"""Documents get an owner.

`documents.owner_user_id` points to the account that owns the document. It is nullable:
documents uploaded before accounts existed have no owner until someone runs
`python -m app.claim_documents --email ...`, and until then nobody can see them. Deleting an
account deletes its documents (and, through the existing foreign key, their chunks).

The index serves the per-user document list, which is ordered newest first.

Revision ID: 0003_document_owner
Revises: 0002_auth_tables
Create Date: 2026-11-10
"""

from alembic import op

revision = "0003_document_owner"
down_revision = "0002_auth_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE documents
            ADD COLUMN owner_user_id INTEGER,
            ADD CONSTRAINT documents_owner_user_id_fkey FOREIGN KEY (owner_user_id)
                REFERENCES users (id) ON DELETE CASCADE
        """
    )
    op.execute(
        "CREATE INDEX ix_documents_owner_created "
        "ON documents USING btree (owner_user_id, created_at DESC, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_documents_owner_created")
    op.execute("ALTER TABLE documents DROP COLUMN owner_user_id")
