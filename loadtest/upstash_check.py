"""Step A of docs/phase3-plan.md section 14.9: measure what the queue costs on a real Upstash Redis,
with no Azure and no paid AI call.

    python loadtest/upstash_check.py ping                  connect over TLS (certificate verified)
    python loadtest/upstash_check.py worker                a local Celery worker on that Redis; leave it running
    python loadtest/upstash_check.py enqueue --count 10    10 small jobs through the same Redis
    python loadtest/upstash_check.py stats                 command counters the server reports (if it does)
    python loadtest/upstash_check.py clean                 delete this tool's test users (and their jobs)

Read the "commands" count on the Upstash dashboard before and after each phase (10 idle minutes,
then the jobs); `stats` prints the server's own counter when Upstash exposes one.

The Redis URL comes from backend/.env as UPSTASH_REDIS_URL (rediss://default:<token>@<host>:6379?ssl_cert_reqs=required).
It is read from there and never printed. Everything else is forced to be safe: the fake LLM, blank
AI keys (whatever backend/.env says), and the local `docsum_load` database (any other database is
refused), so the only thing this talks to over the internet is your Upstash database.
Run it with the backend virtualenv: backend\\.venv\\Scripts\\python loadtest\\upstash_check.py ...
"""

import argparse
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(HERE))

from dotenv import dotenv_values  # noqa: E402

LOCAL_DB = "postgresql://docsum:docsum@127.0.0.1:5432/docsum_load"
EMAIL_DOMAIN = "loadtest.invalid"
EMAIL_PREFIX = "upstash-check-"


def read_url() -> str:
    url = (dotenv_values(ROOT / "backend" / ".env").get("UPSTASH_REDIS_URL") or "").strip()
    if not url:
        raise SystemExit("Put UPSTASH_REDIS_URL=rediss://default:<token>@<host>:6379?ssl_cert_reqs=required "
                         "in backend/.env first (it is git-ignored and never printed).")
    if not url.lower().startswith("rediss://"):
        raise SystemExit("UPSTASH_REDIS_URL must start with rediss:// (TLS).")
    return url


def scrub(message: str, url: str) -> str:
    """An error message without the Redis password."""
    password = urlsplit(url).password or ""
    return message.replace(password, "<hidden>") if password else message


def prepare_environment() -> str:
    """Set the environment BEFORE the app is imported (an existing variable beats backend/.env)."""
    url = read_url()
    # Before anything from the app is imported: app.database reads DATABASE_URL at import, and an
    # existing variable beats backend/.env (which points at the dev database and holds a real AI key).
    os.environ.update({
        "REDIS_URL": url,
        "INGESTION_MODE": "queue",
        "FAKE_LLM": "true",
        "AI_API_KEY": "",
        "OPENROUTER_API_KEY": "",
        "AI_BASE_URL": "",
        "CACHE_ENABLED": "false",
        "DATABASE_URL": LOCAL_DB,
        "RECORD_AI_USAGE": "true",
    })
    from seed_users import check_url

    check_url(LOCAL_DB)
    host = urlsplit(url).hostname
    print(f"Redis: {host} (TLS, token hidden) | fake LLM: on | AI keys: blank | database: docsum_load (local)")
    return url


def ensure_database() -> None:
    from seed_users import ensure_database as create
    from app.migrate import upgrade

    create(LOCAL_DB)
    upgrade(LOCAL_DB)


def cmd_ping(url: str) -> None:
    import app.worker as worker

    options = worker.celery_app.conf.broker_use_ssl
    print(f"TLS options: {options}")
    try:
        with worker.celery_app.connection_for_read() as connection:
            connection.ensure_connection(max_retries=1, timeout=10)
            client = connection.default_channel.client
            print("PING:", client.ping())
    except Exception as error:  # noqa: BLE001
        raise SystemExit(f"Could not connect: {type(error).__name__}: {scrub(str(error), url)}")
    print("Connected over TLS with the certificate verified. (PING is not billed.)")


def cmd_worker(url: str) -> None:
    ensure_database()
    import app.worker as worker

    started = time.time()

    def minutes():
        while True:
            time.sleep(60)
            print(f"[upstash_check] worker has been idle/running for {round((time.time() - started) / 60)} min", flush=True)

    threading.Thread(target=minutes, daemon=True).start()
    print("Starting a local worker (solo pool: no AI calls, no tasks until you run `enqueue`). Ctrl+C to stop.")
    worker.celery_app.worker_main([
        "worker", "--pool=solo", "-Q", "documind", "--loglevel=INFO",
        "--without-gossip", "--without-mingle", "--without-heartbeat",
    ])


def cmd_enqueue(url: str, count: int, spacing: float) -> None:
    ensure_database()
    from sqlalchemy import text

    import app.database as database
    import app.ingestion as ingestion
    import app.jobs as jobs

    job_ids = []
    with database.get_connection() as connection:
        connection.execute(text("DELETE FROM users WHERE email LIKE :p"), {"p": f"{EMAIL_PREFIX}%@{EMAIL_DOMAIN}"})
    for i in range(count):
        # One user per job: a user may only have 3 uploads running at the same time.
        with database.get_connection() as connection:
            user_id = connection.execute(
                text("INSERT INTO users (email, password_hash) VALUES (:e, 'not-a-login') RETURNING id"),
                {"e": f"{EMAIL_PREFIX}{i}@{EMAIL_DOMAIN}"},
            ).scalar_one()
        content = f"Upstash check document {i}. A short text about queues and billing. " * 8
        job_id = jobs.create_job(user_id, mode="queue", filename=f"check-{i}.txt", file_type=".txt",
                                 file_size=len(content.encode()), file_content=content.encode())
        started = time.perf_counter()
        try:
            ingestion.enqueue_job(job_id)
        except ingestion.QueueUnavailable as error:
            raise SystemExit(f"Could not enqueue job {i}: {scrub(str(error.__cause__ or error), url)}")
        print(f"enqueued {i + 1}/{count} in {(time.perf_counter() - started) * 1000:.0f} ms")
        job_ids.append(job_id)
        time.sleep(spacing)

    print("Waiting for the worker (it must be running: `upstash_check.py worker`)...")
    deadline = time.time() + 180
    while time.time() < deadline:
        with database.get_connection() as connection:
            rows = connection.execute(
                text("SELECT status, count(*) FROM processing_jobs WHERE id = ANY(CAST(:ids AS uuid[])) GROUP BY status"),
                {"ids": job_ids},
            ).all()
        states = {status: n for status, n in rows}
        print("  ", states)
        if states.get("completed", 0) + states.get("failed", 0) == count:
            break
        time.sleep(3)
    print("Done. Read the commands used on the Upstash dashboard now.")


def cmd_stats(url: str) -> None:
    import app.worker as worker

    try:
        with worker.celery_app.connection_for_read() as connection:
            client = connection.default_channel.client
            info = client.info()
            print("total_commands_processed:", info.get("total_commands_processed", "(not reported)"))
            try:
                stats = client.info("commandstats")
                top = sorted(stats.items(), key=lambda item: -item[1].get("calls", 0))[:12]
                for name, values in top:
                    print(f"  {name}: {values.get('calls')} calls")
            except Exception as error:  # noqa: BLE001
                print("(commandstats not available:", type(error).__name__ + ")")
            print("keys:", sorted(k.decode() if isinstance(k, bytes) else k for k in client.scan_iter(count=100))[:20])
    except Exception as error:  # noqa: BLE001
        raise SystemExit(f"{type(error).__name__}: {scrub(str(error), url)}")


def cmd_clean() -> None:
    from sqlalchemy import text

    import app.database as database

    with database.get_connection() as connection:
        deleted = connection.execute(text("DELETE FROM users WHERE email LIKE :p"),
                                     {"p": f"{EMAIL_PREFIX}%@{EMAIL_DOMAIN}"}).rowcount
    print(f"Deleted {deleted} test users (and their jobs).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("ping", "worker", "stats", "clean"):
        sub.add_parser(name)
    enqueue = sub.add_parser("enqueue")
    enqueue.add_argument("--count", type=int, default=10)
    enqueue.add_argument("--spacing", type=float, default=1.0, help="seconds between jobs")
    args = parser.parse_args()

    redis_url = prepare_environment()
    if args.command == "ping":
        cmd_ping(redis_url)
    elif args.command == "worker":
        cmd_worker(redis_url)
    elif args.command == "enqueue":
        cmd_enqueue(redis_url, args.count, args.spacing)
    elif args.command == "stats":
        cmd_stats(redis_url)
    else:
        cmd_clean()
