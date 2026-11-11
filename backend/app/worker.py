"""The Celery worker: `celery -A app.worker worker` (docker-compose.yml runs it as `worker`).

The broker is Redis (REDIS_URL, default redis://127.0.0.1:6379/0). The only task is
`documents.ingest`, which runs one upload job (app/ingestion.py). The API enqueues it with
`ingest_document.delay(job_id)` when INGESTION_MODE=queue.
"""

import os

from celery import Celery
from celery.signals import worker_process_init

from app import context, ingestion

# Importing app.database (through ingestion) loads backend/.env.
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

context.install_log_context()

celery_app = Celery("documind", broker=REDIS_URL)
celery_app.conf.update(
    task_default_queue="documind",
    # A job is acknowledged only after it has run, so a worker that dies mid-job gets it
    # again (the job table makes a second delivery harmless), and one job at a time per
    # worker process, so a long job doesn't hold back reserved ones.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # No results backend: the job table is the result.
    task_ignore_result=True,
    broker_connection_retry_on_startup=True,
    # The API waits for this when Redis is down; fail fast so the user gets a clear error.
    broker_connection_timeout=3,
    broker_transport_options={"socket_connect_timeout": 3, "socket_timeout": 3},
    task_publish_retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0.2,
                               "interval_max": 0.5},
)


@worker_process_init.connect
def _fresh_database_connections(**_):
    """Each forked worker process opens its own database connections: the ones inherited
    from the parent process must never be shared."""
    from app import database

    for engine in database._engines.values():
        engine.dispose(close=False)
    database._engines.clear()


@celery_app.task(bind=True, name="documents.ingest", max_retries=ingestion.MAX_ATTEMPTS)
def ingest_document(self, job_id: str) -> None:
    retry_in = ingestion.process_job(job_id)
    if retry_in is not None:
        # The job is pending again; run the task again after the wait.
        raise self.retry(countdown=retry_in, max_retries=ingestion.MAX_ATTEMPTS)
