"""Settings that make the worker safe on a managed Redis (TLS, billed per command) and on a
scale-to-zero host: see the docstring of app/worker.py and docs/phase3-plan.md, section 14."""

import asyncio
import fnmatch
import logging
import pathlib
import ssl

import pytest
from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded
from sqlalchemy import text

import app.database as database
import app.ingestion as ingestion
import app.jobs as jobs
import app.worker as worker
from tests.default_user import DEFAULT_USER_ID

BACKEND = pathlib.Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
KEDA_COOLDOWN_SECONDS = 300  # Azure Container Apps' default; docs/phase3-plan.md 14.5


def job_row(job_id):
    with database.get_connection() as connection:
        return connection.execute(
            text("SELECT * FROM processing_jobs WHERE id = :id"), {"id": job_id}
        ).mappings().one()


def new_job():
    return jobs.create_job(DEFAULT_USER_ID, mode="queue", filename="d.txt", file_type=".txt",
                           file_size=11, file_content=b"hello world")


# --- TLS --------------------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["rediss://default:pw@host.upstash.io:6379",
                                 "REDISS://default:pw@host:6379?ssl_cert_reqs=required"])
def test_a_rediss_url_verifies_the_certificate(url):
    assert worker.broker_ssl_options(url) == {"ssl_cert_reqs": ssl.CERT_REQUIRED}


@pytest.mark.parametrize("url", ["redis://127.0.0.1:6379/0", "redis://redis:6379/0"])
def test_a_plain_redis_url_gets_no_tls_options(url):
    assert worker.broker_ssl_options(url) is None


def test_without_our_setting_kombu_would_skip_the_certificate_check(caplog):
    """The reason for the setting: kombu's own default for rediss:// is no verification."""
    url = "rediss://default:pw@host.upstash.io:6379"
    with caplog.at_level(logging.WARNING):
        unsafe = Celery("t", broker=url).connection_for_read()
        assert unsafe.ssl == {"ssl_cert_reqs": ssl.CERT_NONE}
        assert any("insecure SSL" in r.getMessage() for r in caplog.records)

        caplog.clear()
        app = Celery("t", broker=url)
        app.conf.broker_use_ssl = worker.broker_ssl_options(url)
        safe = app.connection_for_read()
    assert safe.ssl == {"ssl_cert_reqs": ssl.CERT_REQUIRED}
    assert not any("insecure SSL" in r.getMessage() for r in caplog.records)


def test_the_query_option_on_the_url_does_not_break_the_connection_settings():
    url = "rediss://default:pw@host.upstash.io:6379?ssl_cert_reqs=required"
    app = Celery("t", broker=url)
    app.conf.broker_use_ssl = worker.broker_ssl_options(url)
    connection = app.connection_for_read()
    assert connection.ssl == {"ssl_cert_reqs": ssl.CERT_REQUIRED}
    assert connection.hostname == "host.upstash.io" and connection.port == 6379


# --- Polling, timeouts, visibility ------------------------------------------------------------


def test_the_worker_polls_slowly_so_an_idle_redis_is_not_hammered():
    connection = worker.celery_app.connection_for_write()
    # kombu turns polling_interval into the BRPOP timeout: one command per 5 s, not per second.
    assert connection.transport.brpop_timeout == worker.POLLING_SECONDS == 5.0


def test_the_socket_waits_longer_than_one_blocking_poll():
    options = worker.celery_app.conf.broker_transport_options
    assert options["socket_timeout"] > options["polling_interval"]
    assert options["socket_connect_timeout"] <= 3  # an unreachable Redis fails fast


def test_the_polling_interval_can_be_changed_with_a_setting(monkeypatch):
    import importlib

    monkeypatch.setenv("BROKER_POLLING_SECONDS", "2")
    try:
        reloaded = importlib.reload(worker)
        assert reloaded.POLLING_SECONDS == 2.0
        assert reloaded.celery_app.conf.broker_transport_options["socket_timeout"] == 7.0
    finally:
        monkeypatch.delenv("BROKER_POLLING_SECONDS")
        importlib.reload(worker)


def test_the_limits_fit_inside_the_scale_to_zero_cooldown():
    conf = worker.celery_app.conf
    assert conf.task_soft_time_limit == worker.SOFT_TIME_LIMIT_SECONDS == 200
    assert conf.task_time_limit == worker.HARD_TIME_LIMIT_SECONDS == 240
    assert worker.SOFT_TIME_LIMIT_SECONDS < worker.HARD_TIME_LIMIT_SECONDS
    # KEDA can't see a running job, so one attempt (plus the wait before a retry) must end
    # before its cool-down, or scale-to-zero could cut it off.
    longest_wait = max(ingestion.retry_delay_seconds(n) for n in range(1, ingestion.MAX_ATTEMPTS))
    assert worker.HARD_TIME_LIMIT_SECONDS + longest_wait < KEDA_COOLDOWN_SECONDS
    assert 0 < conf.worker_soft_shutdown_timeout <= 30  # a SIGKILL follows SIGTERM after 30 s


def test_an_unacknowledged_job_comes_back_before_it_would_be_declared_dead():
    visibility = worker.celery_app.conf.broker_transport_options["visibility_timeout"]
    assert visibility == worker.VISIBILITY_TIMEOUT_SECONDS == 600
    assert visibility < jobs.STALE_AFTER_MINUTES * 60  # redelivery wins over "timed_out"
    longest_wait = max(ingestion.retry_delay_seconds(n) for n in range(1, ingestion.MAX_ATTEMPTS))
    assert visibility > worker.HARD_TIME_LIMIT_SECONDS + longest_wait  # no redelivery while it runs


def test_the_redelivery_settings_are_still_on():
    conf = worker.celery_app.conf
    assert conf.task_acks_late and conf.task_reject_on_worker_lost
    assert conf.worker_prefetch_multiplier == 1


# --- The soft time limit ----------------------------------------------------------------------


def test_a_job_that_hits_the_soft_limit_fails_with_a_clear_message_and_is_not_retried(monkeypatch):
    async def too_slow(job):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(ingestion, "run_pipeline", too_slow)
    job_id = new_job()

    assert ingestion.process_job(job_id) is None  # no retry: it would take as long again

    row = job_row(job_id)
    assert (row["status"], row["error_code"], row["attempts"]) == (
        "failed", "processing_timeout", 1)
    assert row["error_message"] == "Processing took too long. Try a smaller file."
    assert row["file_content"] is None


# --- Usage rows are not lost at shutdown ------------------------------------------------------


def test_the_usage_queue_is_flushed_after_every_job_even_a_failing_one(monkeypatch):
    flushed = []
    monkeypatch.setattr(worker.usage_store, "flush", lambda timeout=5.0: flushed.append(timeout))
    monkeypatch.setattr(ingestion, "process_job", lambda job_id: None)
    worker.ingest_document.run("job")
    assert flushed == [5]

    def boom(job_id):
        raise RuntimeError("worker bug")

    monkeypatch.setattr(ingestion, "process_job", boom)
    with pytest.raises(RuntimeError):
        worker.ingest_document.run("job")
    assert flushed == [5, 5]


def test_the_usage_queue_is_flushed_when_the_worker_shuts_down(monkeypatch):
    flushed = []
    monkeypatch.setattr(worker.usage_store, "flush", lambda timeout=5.0: flushed.append(timeout))
    worker._flush_usage()
    assert flushed == [10]
    # And it is connected to both shutdown signals (the main process and each pool process).
    from celery.signals import worker_process_shutdown, worker_shutdown

    flushed.clear()
    worker_shutdown.send(sender="test")
    worker_process_shutdown.send(sender="test", pid=1, exitcode=0)
    assert flushed == [10, 10]


# --- The image: production command, and no secrets in it --------------------------------------


def test_the_image_starts_the_worker_the_cheap_way_and_compose_adds_events_for_flower():
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    command = dockerfile[dockerfile.index("CMD ["):]
    for flag in ("--without-gossip", "--without-mingle", "--without-heartbeat", '"-Q", "documind"'):
        assert flag in command
    assert '"-E"' not in command and "--concurrency" not in command
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    worker_block = compose[compose.index("  worker:"):compose.index("  flower:")]
    assert '"-E"' in worker_block


def test_the_docker_context_excludes_secrets_at_any_depth():
    lines = {line.strip() for line in (BACKEND / ".dockerignore").read_text(encoding="utf-8").splitlines()}
    for pattern in ("**/.env", "**/.env.*", "**/*.pem", "**/*.key", "**/users.json", ".venv", ".git"):
        assert pattern in lines, pattern


def test_nothing_the_dockerfile_copies_looks_like_a_secret():
    """The Dockerfile copies only these paths (plus pip-installed packages)."""
    dockerfile = (BACKEND / "Dockerfile").read_text(encoding="utf-8")
    copies = [line.split()[1] for line in dockerfile.splitlines() if line.startswith("COPY ")]
    assert set(copies) == {"requirements.txt", "app", "migrations", "alembic.ini"}  # no COPY . .
    secret_patterns = [".env", ".env.*", "*.pem", "*.key", "*.p12", "*secret*", "*credential*",
                       "users.json", "id_rsa*"]
    found = []
    for name in copies:
        path = BACKEND / name
        files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
        for file in files:
            if "__pycache__" in file.parts:
                continue
            if any(fnmatch.fnmatch(file.name.lower(), pattern) for pattern in secret_patterns):
                found.append(str(file.relative_to(BACKEND)))
    assert found == []
