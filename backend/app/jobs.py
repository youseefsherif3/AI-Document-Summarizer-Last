"""Upload jobs (`processing_jobs`): one row per upload, owned by one user.

States: pending → processing → completed | failed. A retried job goes back to pending.

- `INGESTION_MODE=queue`: the API creates a `pending` job and enqueues it; a Celery worker
  claims it, runs the ingestion and ends it (app/ingestion.py, app/worker.py).
- `INGESTION_MODE=inline`: the upload runs inside the request; its job (created as
  `processing`) only exists so the limit of MAX_ACTIVE_JOBS uploads at the same time per
  user applies in both modes.

Every function that reads a job for a user takes the owner as a required keyword, like the
document queries: another user's job is "not found".
"""

import logging
from typing import Any

from sqlalchemy import text

from app.database import get_connection

logger = logging.getLogger(__name__)

# At most this many uploads per user running at the same time (the browser enforces it too).
MAX_ACTIVE_JOBS = 3
# A job that has not changed for this long is not counted as running: a serverless function
# that was stopped mid-upload, or a lost queue message, must not block the user forever.
STALE_AFTER_MINUTES = 15
ACTIVE_STATUSES = ("pending", "processing")


class JobLimitReached(Exception):
    """The user already has MAX_ACTIVE_JOBS uploads running."""

    def __init__(self):
        super().__init__(
            f"You can upload up to {MAX_ACTIVE_JOBS} files at a time. "
            "Wait for the current uploads to finish."
        )


class JobAlreadyFinished(Exception):
    """The job was already completed (a task delivered twice): nothing must be saved again."""


def create_job(
    owner_user_id: int,
    *,
    mode: str,
    filename: str,
    file_type: str,
    file_size: int,
    file_content: bytes | None = None,
    blob_pathname: str | None = None,
) -> str:
    """Create a job and return its id, or raise JobLimitReached.

    A `queue` job starts `pending`; an `inline` job starts `processing`. The count and the
    insert happen under a per-user advisory lock, so two uploads at the same moment can't
    both pass the limit.
    """
    status = "pending" if mode == "queue" else "processing"
    with get_connection() as connection:
        # Released when the transaction ends.
        connection.execute(
            text("SELECT pg_advisory_xact_lock(hashtext('processing_jobs'), :user_id)"),
            {"user_id": owner_user_id},
        )
        active = connection.execute(
            text(
                "SELECT count(*) FROM processing_jobs "
                "WHERE owner_user_id = :user_id AND status IN ('pending', 'processing') "
                "AND updated_at > now() - make_interval(mins => :stale)"
            ),
            {"user_id": owner_user_id, "stale": STALE_AFTER_MINUTES},
        ).scalar_one()
        if active >= MAX_ACTIVE_JOBS:
            raise JobLimitReached()
        return str(
            connection.execute(
                text(
                    "INSERT INTO processing_jobs "
                    "(owner_user_id, mode, status, filename, file_type, file_size, "
                    " file_content, blob_pathname) "
                    "VALUES (:user_id, :mode, :status, :filename, :file_type, :file_size, "
                    " :file_content, :blob_pathname) RETURNING id"
                ),
                {
                    "user_id": owner_user_id,
                    "mode": mode,
                    "status": status,
                    "filename": filename,
                    "file_type": file_type,
                    "file_size": file_size,
                    "file_content": file_content,
                    "blob_pathname": blob_pathname,
                },
            ).scalar_one()
        )


def delete_job(job_id: str) -> None:
    """Remove a job that never started (for example the daily limit refused the upload)."""
    with get_connection() as connection:
        connection.execute(text("DELETE FROM processing_jobs WHERE id = :id"), {"id": job_id})


def get_job(job_id: str, *, owner_user_id: int) -> dict[str, Any] | None:
    """The user's job, with the saved document's summary once it is completed."""
    with get_connection() as connection:
        row = connection.execute(
            text(
                "SELECT j.id, j.status, j.filename, j.file_type, j.file_size, j.attempts, "
                "       j.error_code, j.error_message, j.document_id, d.summary, "
                "       j.created_at, j.updated_at, j.finished_at "
                "FROM processing_jobs AS j "
                "LEFT JOIN documents AS d ON d.id = j.document_id "
                "WHERE j.id = :id AND j.owner_user_id = :owner_user_id"
            ),
            {"id": job_id, "owner_user_id": owner_user_id},
        ).mappings().one_or_none()
    return dict(row) if row else None


def claim_job(job_id: str) -> dict[str, Any] | None:
    """Worker: mark a pending (or interrupted, `processing`) job as processing and count the
    attempt. None if the job does not exist or has already ended: a task delivered a second
    time does nothing."""
    with get_connection() as connection:
        row = connection.execute(
            text(
                "UPDATE processing_jobs SET status = 'processing', attempts = attempts + 1, "
                "       updated_at = now() "
                "WHERE id = :id AND status IN ('pending', 'processing') "
                "RETURNING id, owner_user_id, filename, file_type, file_size, file_content, "
                "          blob_pathname, attempts"
            ),
            {"id": job_id},
        ).mappings().one_or_none()
    return dict(row) if row else None


def release_for_retry(job_id: str, code: str, message: str) -> None:
    """Worker: a transient failure will be retried; the job waits as pending."""
    with get_connection() as connection:
        connection.execute(
            text(
                "UPDATE processing_jobs SET status = 'pending', error_code = :code, "
                "       error_message = :message, updated_at = now() "
                "WHERE id = :id AND status = 'processing'"
            ),
            {"id": job_id, "code": code, "message": message},
        )


def mark_failed(job_id: str, code: str, message: str) -> None:
    """End a running job as failed (a job that already ended is left alone). The file is
    no longer needed, so it is cleared."""
    with get_connection() as connection:
        connection.execute(
            text(
                "UPDATE processing_jobs SET status = 'failed', error_code = :code, "
                "       error_message = :message, file_content = NULL, "
                "       updated_at = now(), finished_at = now() "
                "WHERE id = :id AND status IN ('pending', 'processing')"
            ),
            {"id": job_id, "code": code, "message": message},
        )


def complete_job_in_transaction(connection, job_id: str, document_id: int) -> None:
    """Mark the job completed inside the transaction that saves the document, so the document
    and the job's end are one step. Raises JobAlreadyFinished (and the caller's transaction
    rolls back) if the job has already ended: a duplicate delivery saves nothing."""
    ended = connection.execute(
        text(
            "UPDATE processing_jobs SET status = 'completed', document_id = :document_id, "
            "       error_code = NULL, error_message = NULL, file_content = NULL, "
            "       updated_at = now(), finished_at = now() "
            "WHERE id = :id AND status IN ('pending', 'processing')"
        ),
        {"id": job_id, "document_id": document_id},
    ).rowcount
    if ended == 0:
        raise JobAlreadyFinished(job_id)
