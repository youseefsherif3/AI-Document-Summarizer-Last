"""Upload jobs.

`processing_jobs` has one row per upload. In `INGESTION_MODE=queue` the API creates the row
(`pending`), a Celery worker processes it (`processing`) and ends it as `completed` (with the
saved `document_id`) or `failed` (with a safe `error_code` and `error_message`). In `inline`
mode the row only records that an upload is running, so the limit of 3 uploads at the same
time can be enforced in the backend in both modes.

The file waits in the row (`file_content`) or in Vercel Blob (`blob_pathname`) until the job
ends; the worker clears `file_content` when it is done. Deleting an account deletes its jobs;
deleting a document keeps the job (`document_id` becomes NULL).

Revision ID: 0004_processing_jobs
Revises: 0003_document_owner
Create Date: 2026-11-11
"""

from alembic import op

revision = "0004_processing_jobs"
down_revision = "0003_document_owner"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE processing_jobs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            owner_user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
            mode TEXT NOT NULL CHECK (mode IN ('queue', 'inline')),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
            filename TEXT NOT NULL,
            file_type TEXT NOT NULL,
            file_size INTEGER NOT NULL,
            file_content BYTEA,
            blob_pathname TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            error_code TEXT,
            error_message TEXT,
            document_id INTEGER REFERENCES documents (id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            finished_at TIMESTAMPTZ
        )
        """
    )
    # Counts a user's running uploads (the limit of 3 at a time) without reading old jobs.
    op.execute(
        "CREATE INDEX ix_processing_jobs_owner_status "
        "ON processing_jobs USING btree (owner_user_id, status)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE processing_jobs")
