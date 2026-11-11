"""Queue ingestion: what a Celery worker does for one upload job.

`INGESTION_MODE` (set per deployment):

- `inline` (default): the upload is processed inside the request and its summary streams over
  SSE (app/main.py). Used on Vercel, which can't run a worker.
- `queue`: the upload stores the file in a job, enqueues it and returns 202. The worker (this
  module, started by app/worker.py) runs the same pipeline: read the file, summary and
  embeddings in parallel, then save the document, its chunks and the job's end in one
  transaction. The temporary Blob file is deleted when the job ends.

Retries: only transient provider failures (timeouts, no connection, rate limits) are retried,
up to MAX_ATTEMPTS attempts in total, with a growing wait. Everything else fails the job at
once with a safe `error_code` and message; the full error is logged, never shown. A task that
is delivered twice does nothing the second time (the job has already ended), and a document is
saved together with its job's end, so it can't be saved twice.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from vercel import blob

from app import cache, context, jobs
from app.database import save_document_with_chunks
from app.extraction import extract_pages, join_pages
from app.services.chunking import chunk_pages
from app.services.embeddings import embed_texts
from app.services.summarizer import (
    AIOutOfCreditError,
    AIRateLimitError,
    AIServiceError,
    AIServiceNotConfiguredError,
    MODEL as SUMMARY_MODEL,
    stream_summary,
)
from app.services.ai_calls import ai_base_url

logger = logging.getLogger(__name__)

MODES = ("inline", "queue")
MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 10
RETRY_MAX_SECONDS = 300


def ingestion_mode() -> str:
    """INGESTION_MODE: "inline" (default) or "queue". Any other value is an error, not a
    silent fallback."""
    mode = os.getenv("INGESTION_MODE", "").strip().lower() or "inline"
    if mode not in MODES:
        raise ValueError(f"INGESTION_MODE must be 'inline' or 'queue', got {mode!r}.")
    return mode


def check_ingestion_settings() -> None:
    """Called at startup, so a bad setting stops the app instead of failing every upload."""
    ingestion_mode()


class QueueUnavailable(Exception):
    """The job could not be handed to the queue (Redis is down or unreachable)."""


def enqueue_job(job_id: str) -> None:
    """Hand a created job to the worker. Raises QueueUnavailable if Redis can't be reached."""
    from app.worker import ingest_document  # imports Celery only when the queue is used

    try:
        ingest_document.delay(job_id)
    except Exception as error:
        logger.exception("Could not enqueue job %s.", job_id)
        raise QueueUnavailable("The processing queue is unavailable.") from error


# --- Errors ---------------------------------------------------------------------------------


class IngestionError(Exception):
    """A job failure with a safe code and message (shown to the user as they are)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Failure:
    code: str
    message: str
    transient: bool  # worth retrying
    retry_after: float | None = None


def describe_failure(error: Exception) -> Failure:
    """The safe code and message for an error, and whether it is worth retrying. The messages
    are the ones the inline upload shows for the same errors."""
    if isinstance(error, IngestionError):
        return Failure(error.code, error.message, transient=False)
    if isinstance(error, TimeoutError):
        return Failure("ai_timeout", str(error), transient=True)
    if isinstance(error, ConnectionError):
        return Failure("ai_unavailable", str(error), transient=True)
    if isinstance(error, AIRateLimitError):
        return Failure("ai_rate_limited", str(error), transient=True,
                       retry_after=error.retry_after)
    if isinstance(error, AIServiceNotConfiguredError):
        return Failure("ai_not_configured", str(error), transient=False)
    if isinstance(error, AIOutOfCreditError):
        return Failure("ai_out_of_credit", str(error), transient=False)
    if isinstance(error, AIServiceError):
        return Failure("ai_error", str(error), transient=False)
    return Failure("internal_error", "Could not process the document.", transient=False)


def retry_delay_seconds(attempt: int, retry_after: float | None = None) -> float:
    """Seconds to wait after failed attempt number `attempt` (1-based): 10, 20, 40… capped, or
    the provider's own `Retry-After` when that is longer."""
    delay = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** (attempt - 1))
    return max(delay, min(retry_after or 0, RETRY_MAX_SECONDS))


# --- The pipeline ---------------------------------------------------------------------------


async def delete_temporary_blob(pathname: str) -> None:
    try:
        await blob.delete_blob(pathname)
    except Exception:
        # The job's result is not affected; the temporary file stays in Blob storage until
        # it is cleaned up. Logged so leftover files can be found.
        logger.warning("Could not delete temporary upload %r from Blob storage.", pathname,
                       exc_info=True)


async def _load_file(job: dict[str, Any]) -> bytes:
    if job["blob_pathname"]:
        try:
            downloaded = await blob.get_blob(job["blob_pathname"], access="private")
        except blob.BlobNotFoundError as error:
            raise IngestionError("file_not_found", "Uploaded file was not found.") from error
        file_content = downloaded.body
    else:
        file_content = bytes(job["file_content"] or b"")
    if not file_content or len(file_content) != job["file_size"]:
        raise IngestionError("file_incomplete", "Uploaded file is empty or incomplete.")
    return file_content


def _read_document(file_content: bytes, file_extension: str):
    pages = extract_pages(file_content, file_extension)
    return join_pages(pages, file_extension), chunk_pages(pages)


async def _summary_text(extracted_text: str) -> str:
    parts = [part async for part in stream_summary(extracted_text)]
    summary = "".join(parts).strip()
    if not summary:
        raise AIServiceError("The AI model returned an empty summary.")
    return summary


async def _cached_summary(owner_user_id: int, extracted_text: str) -> str:
    """The summary, from the cache when this user's same text was summarized before."""
    key = cache.summary_key(owner_user_id, ai_base_url(), SUMMARY_MODEL, extracted_text)
    [cached] = await cache.get_many("summary", [key])
    if isinstance(cached, str) and cached.strip():
        return cached
    summary = await _summary_text(extracted_text)
    await cache.set_many("summary", {key: summary})
    return summary


async def run_pipeline(job: dict[str, Any]) -> int:
    """The Phase 2 ingestion for one job; returns the saved document's id."""
    file_content = await _load_file(job)
    file_extension = job["file_type"]
    try:
        extracted_text, document_chunks = await asyncio.to_thread(
            _read_document, file_content, file_extension
        )
    except Exception as error:
        raise IngestionError("unreadable_file", "Could not read the uploaded file.") from error
    if not extracted_text.strip():
        raise IngestionError("no_text", "No readable text found in the uploaded file.")

    # Summary and embeddings run at the same time. If one fails, stop paying for the other.
    summary_task = asyncio.create_task(
        _cached_summary(job["owner_user_id"], extracted_text)
    )
    embedding_task = asyncio.create_task(embed_texts([chunk.text for chunk in document_chunks]))
    try:
        await asyncio.wait({summary_task, embedding_task}, return_when=asyncio.FIRST_EXCEPTION)
        summary = await summary_task  # raises the summary error, if any
        embeddings = await embedding_task
    finally:
        for task in (summary_task, embedding_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(summary_task, embedding_task, return_exceptions=True)

    return await asyncio.to_thread(
        save_document_with_chunks,
        filename=job["filename"],
        file_type=file_extension,
        file_size=len(file_content),
        file_content=file_content,
        extracted_text=extracted_text,
        summary=summary,
        document_chunks=document_chunks,
        embeddings=embeddings,
        owner_user_id=job["owner_user_id"],
        job_id=str(job["id"]),
    )


def process_job(job_id: str, *, max_attempts: int = MAX_ATTEMPTS) -> float | None:
    """Run one attempt of a job. Returns None when the job has ended (completed, failed, or
    was already finished), or the number of seconds after which to try again.

    Called by the Celery task (app/worker.py); a separate function so it can be tested without
    a broker.
    """
    job = jobs.claim_job(job_id)
    if job is None:
        logger.info("job_skipped job_id=%s reason=not_pending", job_id)
        return None
    attempt = job["attempts"]
    # Everything logged below, in this module and in the AI calls, names the job; the AI calls
    # are recorded for the job's owner.
    with context.use_context(user_id=job["owner_user_id"], job_id=job_id):
        return _run_claimed_job(job, job_id, attempt, max_attempts)


def _run_claimed_job(job: dict[str, Any], job_id: str, attempt: int, max_attempts: int):
    logger.info("job_started job_id=%s attempt=%d/%d", job_id, attempt, max_attempts)
    blob_pathname = job["blob_pathname"]
    retry_in: float | None = None
    try:
        document_id = asyncio.run(run_pipeline(job))
        logger.info("job_completed job_id=%s document_id=%s", job_id, document_id)
    except jobs.JobAlreadyFinished:
        # Another delivery of the same task saved this job first; nothing was saved twice.
        logger.warning("job_duplicate job_id=%s: already completed, nothing saved.", job_id)
    except Exception as error:
        failure = describe_failure(error)
        if failure.code == "internal_error":
            logger.exception("job_failed job_id=%s: unexpected error.", job_id)
        else:
            logger.warning("job_error job_id=%s attempt=%d code=%s error=%s",
                           job_id, attempt, failure.code, type(error).__name__)
        if failure.transient and attempt < max_attempts:
            retry_in = retry_delay_seconds(attempt, failure.retry_after)
            jobs.release_for_retry(job_id, failure.code, failure.message)
            logger.warning("job_retry job_id=%s next_attempt=%d wait_s=%.0f",
                           job_id, attempt + 1, retry_in)
        else:
            jobs.mark_failed(job_id, failure.code, failure.message)
            logger.error("job_failed job_id=%s code=%s attempts=%d",
                         job_id, failure.code, attempt)
    if retry_in is None and blob_pathname:
        asyncio.run(delete_temporary_blob(blob_pathname))  # the job has ended
    return retry_in
