"""Persist every AI call in `ai_usage` without slowing or failing the request.

`record()` only puts the row on an in-memory queue (it never waits and never raises); one
background thread inserts the rows in batches. If the database is slow or down, or the queue is
full, the row is dropped with a warning: the user's request is never affected, and the
`ai_usage` log line still exists. Rows still queued when a process stops are lost; on Vercel a
function that is frozen right after its response may lose its last rows (best effort).

Only calls made for a user are recorded (see app/context.py); `RECORD_AI_USAGE=false` turns
the table off.
"""

import logging
import os
import queue
import threading
from typing import Any

from sqlalchemy import text

from app.context import current_job_id, current_user_id

logger = logging.getLogger(__name__)

MAX_QUEUED = 1000
BATCH_SIZE = 50

_queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=MAX_QUEUED)
_thread: threading.Thread | None = None
_lock = threading.Lock()
dropped = 0

_INSERT = text(
    "INSERT INTO ai_usage (user_id, job_id, kind, model, outcome, prompt_tokens, "
    "completion_tokens, total_tokens, cost_usd, cost_source, duration_ms) "
    "VALUES (:user_id, CAST(:job_id AS uuid), :kind, :model, :outcome, :prompt_tokens, "
    ":completion_tokens, :total_tokens, :cost_usd, :cost_source, :duration_ms)"
)


def recording_enabled() -> bool:
    return os.getenv("RECORD_AI_USAGE", "").strip().lower() not in ("false", "0", "off", "no")


def record(fields: dict[str, Any]) -> bool:
    """Queue one AI call (the fields `log_ai_usage` logs) for the current user. False if it was
    not recorded: no user, recording off, or the queue is full. Never blocks, never raises."""
    global dropped
    try:
        user_id = current_user_id.get()
        if user_id is None or not recording_enabled():
            return False
        row = {
            "user_id": user_id,
            "job_id": current_job_id.get(),
            "kind": fields["kind"],
            "model": fields["model"],
            "outcome": fields["outcome"],
            "prompt_tokens": fields.get("prompt_tokens"),
            "completion_tokens": fields.get("completion_tokens"),
            "total_tokens": fields.get("total_tokens"),
            "cost_usd": fields.get("cost_usd"),
            "cost_source": fields.get("cost_source"),
            "duration_ms": fields["duration_ms"],
        }
        _ensure_writer()
        _queue.put_nowait(row)
        return True
    except queue.Full:
        dropped += 1
        logger.warning("ai_usage_dropped reason=queue_full dropped_total=%d", dropped)
    except Exception:
        dropped += 1
        logger.warning("ai_usage_dropped reason=error", exc_info=True)
    return False


def _ensure_writer() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_write_forever, name="ai-usage-writer", daemon=True)
            _thread.start()


def _write_forever() -> None:
    global dropped
    while True:
        batch = [_queue.get()]
        while len(batch) < BATCH_SIZE:
            try:
                batch.append(_queue.get_nowait())
            except queue.Empty:
                break
        try:
            from app.database import get_connection  # app.database imports the AI services

            with get_connection() as connection:
                connection.execute(_INSERT, batch)
        except Exception as error:
            dropped += len(batch)
            logger.warning("ai_usage_dropped reason=write_failed rows=%d error=%s",
                           len(batch), type(error).__name__)
        finally:
            for _ in batch:
                _queue.task_done()


def flush(timeout: float = 5.0) -> bool:
    """Wait until everything queued has been written (or has failed). For tests and shutdown."""
    done = threading.Event()

    def wait():
        _queue.join()
        done.set()

    threading.Thread(target=wait, daemon=True).start()
    return done.wait(timeout)
