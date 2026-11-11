"""The Celery worker: `celery -A app.worker worker` (docker-compose.yml runs it as `worker`).

The broker is Redis (REDIS_URL, default redis://127.0.0.1:6379/0). The only task is
`documents.ingest`, which runs one upload job (app/ingestion.py). The API enqueues it with
`ingest_document.delay(job_id)` when INGESTION_MODE=queue.

In production the broker is a managed Redis over TLS (`rediss://`, Upstash) and the worker runs on
Azure Container Apps, scaled to zero by KEDA when the queue is empty (docs/phase3-plan.md, section
14). That shapes the settings below:

- `rediss://` always verifies the server certificate (kombu would otherwise silently skip it).
- Every Redis command is billed there, so an idle worker waits longer between polls
  (BROKER_POLLING_SECONDS, default 5; kombu uses it as the BRPOP timeout).
- KEDA scales to zero 300 s after it last sees a job in the queue, and it cannot see a job a
  worker is running. So one attempt must end before that: soft limit 200 s, hard limit 240 s.
- A message a killed worker never acknowledged returns to the queue after the visibility timeout
  (default 1 hour in Celery). It is 600 s here, longer than the hard limit plus the longest retry
  wait, and shorter than the 15 minutes after which an unfinished job is declared dead (app/jobs.py).
"""

import os
import ssl

from celery import Celery
from celery.signals import worker_init, worker_process_init, worker_process_shutdown, worker_shutdown

from app import context, ingestion, usage_store

# Importing app.database (through ingestion) loads backend/.env.
REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

SOFT_TIME_LIMIT_SECONDS = 200
HARD_TIME_LIMIT_SECONDS = 240
VISIBILITY_TIMEOUT_SECONDS = 600
SOFT_SHUTDOWN_SECONDS = 20
# The BRPOP timeout. The socket must wait longer than this for the reply, or every idle poll
# would fail with a timeout.
POLLING_SECONDS = float(os.getenv("BROKER_POLLING_SECONDS", "5"))
SOCKET_TIMEOUT_SECONDS = POLLING_SECONDS + 5

context.install_log_context()


def broker_ssl_options(url: str) -> dict | None:
    """Certificate verification for a `rediss://` broker URL; None for plain `redis://`."""
    return {"ssl_cert_reqs": ssl.CERT_REQUIRED} if url.lower().startswith("rediss://") else None


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
    # Jobs a worker process runs at the same time (the Dockerfile sets no --concurrency).
    worker_concurrency=int(os.getenv("WORKER_CONCURRENCY", "2")),
    task_soft_time_limit=SOFT_TIME_LIMIT_SECONDS,
    task_time_limit=HARD_TIME_LIMIT_SECONDS,
    # On SIGTERM (scale-in, deploy) a running job gets this long to finish before it is cut off.
    worker_soft_shutdown_timeout=SOFT_SHUTDOWN_SECONDS,
    broker_connection_retry_on_startup=True,
    # The API waits for this when Redis is down; fail fast so the user gets a clear error.
    broker_connection_timeout=3,
    broker_use_ssl=broker_ssl_options(REDIS_URL),
    broker_transport_options={
        "socket_connect_timeout": 3,
        "socket_timeout": SOCKET_TIMEOUT_SECONDS,
        "visibility_timeout": VISIBILITY_TIMEOUT_SECONDS,
        "polling_interval": POLLING_SECONDS,
    },
    task_publish_retry_policy={"max_retries": 1, "interval_start": 0, "interval_step": 0.2,
                               "interval_max": 0.5},
)


@worker_init.connect
def _check_settings(**_):
    from app.services import fake_llm

    fake_llm.check_fake_llm_settings()  # refuses fake mode on Vercel, announces it otherwise


@worker_process_init.connect
def _fresh_database_connections(**_):
    """Each forked worker process opens its own database connections: the ones inherited
    from the parent process must never be shared."""
    from app import database

    for engine in database._engines.values():
        engine.dispose(close=False)
    database._engines.clear()


@worker_process_shutdown.connect
@worker_shutdown.connect
def _flush_usage(**_):
    """Write the AI usage rows still queued in this process before it exits."""
    usage_store.flush(timeout=10)


@celery_app.task(bind=True, name="documents.ingest", max_retries=ingestion.MAX_ATTEMPTS)
def ingest_document(self, job_id: str) -> None:
    try:
        retry_in = ingestion.process_job(job_id)
    finally:
        # The replica can be stopped any time after the job (scale to zero): don't leave the
        # job's ai_usage rows in memory.
        usage_store.flush(timeout=5)
    if retry_in is not None:
        # The job is pending again; run the task again after the wait.
        raise self.retry(countdown=retry_in, max_retries=ingestion.MAX_ATTEMPTS)
