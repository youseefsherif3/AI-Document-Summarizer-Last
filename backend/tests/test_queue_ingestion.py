"""Queue ingestion: jobs, the worker's pipeline, retries, idempotency and the upload limit.

No broker and no AI calls: the worker's `process_job` is called directly, with the summary
and embeddings faked, and `enqueue_job` is replaced by a recorder.
"""

import asyncio
from types import SimpleNamespace

import pytest
from celery.exceptions import Retry
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.database as database
import app.ingestion as ingestion
import app.jobs as jobs
import app.main as main
import app.services.ai_calls as ai_calls
import app.worker as worker
from app.services.chunking import Chunk
from tests.default_user import DEFAULT_USER_ID

CONTENT = b"A short document about queues. " * 10


def sql(query, params=None):
    with database.get_connection() as connection:
        return connection.execute(text(query), params or {})


def job_row(job_id):
    return sql("SELECT * FROM processing_jobs WHERE id = :id", {"id": job_id}).mappings().one()


def count(table):
    return sql(f"SELECT count(*) FROM {table}").scalar_one()


def add_user(email="other@example.com") -> int:
    return sql("INSERT INTO users (email, password_hash) VALUES (:e, 'x') RETURNING id",
               {"e": email}).scalar_one()


@pytest.fixture
def queue(monkeypatch):
    """INGESTION_MODE=queue, with the queue replaced by a list of enqueued job ids."""
    monkeypatch.setenv("INGESTION_MODE", "queue")
    enqueued = []
    monkeypatch.setattr(ingestion, "enqueue_job", enqueued.append)
    return enqueued


@pytest.fixture
def summary(monkeypatch):
    """A fake summary model. `calls` counts how many times it ran."""
    state = SimpleNamespace(calls=0, error=None)

    async def fake(text):
        state.calls += 1
        if state.error is not None:
            raise state.error
        yield "A fake "
        yield "summary."

    monkeypatch.setattr(ingestion, "stream_summary", fake)
    return state


def new_job(owner=DEFAULT_USER_ID, *, content=CONTENT, filename="doc.txt", file_type=".txt",
            mode="queue", **fields) -> str:
    return jobs.create_job(
        owner, mode=mode, filename=filename, file_type=file_type,
        file_size=fields.pop("file_size", len(content)),
        file_content=fields.pop("file_content", content), **fields,
    )


def upload(client, name="doc.txt", content=CONTENT):
    return client.post("/documents/upload/stream", files={"file": (name, content, "text/plain")})


# --- Settings ---------------------------------------------------------------------------------


def test_the_default_mode_is_inline(monkeypatch):
    monkeypatch.delenv("INGESTION_MODE", raising=False)
    assert ingestion.ingestion_mode() == "inline"
    monkeypatch.setenv("INGESTION_MODE", "  ")
    assert ingestion.ingestion_mode() == "inline"


@pytest.mark.parametrize("value", ["queue", "QUEUE", " queue "])
def test_queue_mode_is_chosen_by_the_setting(monkeypatch, value):
    monkeypatch.setenv("INGESTION_MODE", value)
    assert ingestion.ingestion_mode() == "queue"


def test_a_bad_mode_is_an_error_not_a_silent_fallback(monkeypatch):
    monkeypatch.setenv("INGESTION_MODE", "celery")
    with pytest.raises(ValueError, match="INGESTION_MODE"):
        ingestion.check_ingestion_settings()


def test_the_app_refuses_to_start_with_a_bad_mode(monkeypatch):
    monkeypatch.setenv("INGESTION_MODE", "nope")
    with pytest.raises(ValueError):
        with TestClient(main.app):
            pass


# --- The upload in queue mode -----------------------------------------------------------------


def test_a_queue_upload_returns_202_with_a_job_and_calls_no_ai(queue, summary, fake_embeddings):
    with TestClient(main.app) as client:
        response = upload(client)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert queue == [body["job_id"]]
    row = job_row(body["job_id"])
    assert (row["status"], row["owner_user_id"], row["attempts"]) == ("pending", DEFAULT_USER_ID, 0)
    assert (row["filename"], row["file_type"], row["mode"]) == ("doc.txt", ".txt", "queue")
    assert bytes(row["file_content"]) == CONTENT
    assert count("documents") == 0
    assert summary.calls == 0 and fake_embeddings == []


def test_a_queue_upload_still_validates_the_file(queue):
    with TestClient(main.app) as client:
        assert upload(client, "x.exe").status_code == 400
        assert upload(client, "empty.txt", b"").status_code == 400
        assert upload(client, "big.txt", b"x" * (main.MAX_FILE_SIZE + 1)).status_code == 413
    assert queue == [] and count("processing_jobs") == 0


def test_the_daily_limit_applies_and_leaves_no_job_behind(queue, monkeypatch):
    monkeypatch.setenv("DAILY_UPLOAD_LIMIT", "1")
    with TestClient(main.app) as client:
        assert upload(client).status_code == 202
        refused = upload(client, "second.txt")
    assert refused.status_code == 429 and refused.json()["code"] == "daily_limit"
    assert count("processing_jobs") == 1


def test_when_the_queue_is_down_the_upload_fails_clearly_and_the_job_ends(monkeypatch):
    monkeypatch.setenv("INGESTION_MODE", "queue")

    def down(job_id):
        raise ingestion.QueueUnavailable("no redis")

    monkeypatch.setattr(ingestion, "enqueue_job", down)
    with TestClient(main.app) as client:
        response = upload(client)

    assert response.status_code == 503
    assert response.json()["detail"] == main.QUEUE_UNAVAILABLE_MESSAGE
    row = sql("SELECT status, error_code, file_content FROM processing_jobs").one()
    assert tuple(row) == ("failed", "queue_unavailable", None)


def test_enqueue_reports_an_unreachable_broker_as_queue_unavailable(monkeypatch):
    # Nothing listens on this port: Celery's publish must fail fast, not hang.
    monkeypatch.setattr(worker.celery_app.conf, "broker_url", "redis://127.0.0.1:1/0")
    with pytest.raises(ingestion.QueueUnavailable):
        ingestion.enqueue_job("00000000-0000-0000-0000-000000000001")


# --- Blob uploads in queue mode ---------------------------------------------------------------


@pytest.fixture
def blob(monkeypatch):
    state = SimpleNamespace(content=CONTENT, deleted=[], downloads=0)

    async def head(pathname):
        return SimpleNamespace(size=len(state.content))

    async def get(pathname, *, access):
        state.downloads += 1
        return SimpleNamespace(body=state.content)

    async def delete(pathname):
        state.deleted.append(pathname)

    for module in (main.blob, ingestion.blob):  # the same module object
        monkeypatch.setattr(module, "head_blob", head)
        monkeypatch.setattr(module, "get_blob", get)
        monkeypatch.setattr(module, "delete_blob", delete)
    return state


def blob_upload(client, *, size=len(CONTENT), user=DEFAULT_USER_ID, name="b1.txt"):
    return client.post("/documents/upload/stream/blob", json={
        "pathname": f"documind/uploads/{user}/{name}", "filename": "blob.txt", "file_size": size,
    })


def test_a_queue_blob_upload_makes_a_job_without_downloading_the_file(queue, blob):
    with TestClient(main.app) as client:
        response = blob_upload(client)

    assert response.status_code == 202
    row = job_row(response.json()["job_id"])
    assert row["blob_pathname"] == f"documind/uploads/{DEFAULT_USER_ID}/b1.txt"
    assert row["file_content"] is None
    assert queue == [response.json()["job_id"]]
    assert blob.downloads == 0 and blob.deleted == []


def test_the_worker_downloads_the_blob_and_deletes_it_when_the_job_ends(queue, blob, summary):
    with TestClient(main.app) as client:
        job_id = blob_upload(client).json()["job_id"]

    assert ingestion.process_job(job_id) is None

    assert job_row(job_id)["status"] == "completed"
    assert blob.downloads == 1
    assert blob.deleted == [f"documind/uploads/{DEFAULT_USER_ID}/b1.txt"]


def test_a_failed_queue_blob_job_also_deletes_the_blob(queue, blob, summary):
    summary.error = ai_calls.AIServiceError("bad")
    with TestClient(main.app) as client:
        job_id = blob_upload(client).json()["job_id"]
    ingestion.process_job(job_id)
    assert job_row(job_id)["status"] == "failed"
    assert len(blob.deleted) == 1


def test_a_blob_that_waits_for_a_retry_is_kept(queue, blob, summary):
    summary.error = TimeoutError("The AI service took too long to respond.")
    with TestClient(main.app) as client:
        job_id = blob_upload(client).json()["job_id"]
    assert ingestion.process_job(job_id) is not None
    assert blob.deleted == []


def test_queue_blob_uploads_are_refused_outside_the_users_folder_or_when_refused(queue, blob):
    with TestClient(main.app) as client:
        assert blob_upload(client, user=DEFAULT_USER_ID + 1).status_code == 400
        wrong_size = blob_upload(client, size=len(CONTENT) + 1)
    assert wrong_size.status_code == 413
    assert len(blob.deleted) == 1  # the mismatching file is removed
    assert queue == [] and count("processing_jobs") == 0


def test_a_blob_upload_over_the_limit_deletes_the_blob(queue, blob, monkeypatch):
    for _ in range(jobs.MAX_ACTIVE_JOBS):
        new_job()
    with TestClient(main.app) as client:
        response = blob_upload(client)
    assert response.status_code == 429 and response.json()["code"] == "upload_limit"
    assert len(blob.deleted) == 1 and queue == []


# --- The worker's pipeline --------------------------------------------------------------------


def test_the_worker_saves_the_document_with_its_chunks_and_ends_the_job(summary, fake_embeddings):
    job_id = new_job()

    assert ingestion.process_job(job_id) is None

    row = job_row(job_id)
    assert (row["status"], row["attempts"], row["error_code"]) == ("completed", 1, None)
    assert row["file_content"] is None  # no longer needed
    assert row["finished_at"] is not None
    document = sql("SELECT id, owner_user_id, filename, file_type, summary FROM documents").one()
    assert document.id == row["document_id"]
    assert (document.owner_user_id, document.filename, document.file_type, document.summary) == (
        DEFAULT_USER_ID, "doc.txt", ".txt", "A fake summary.")
    chunks = count("chunks")
    assert chunks >= 1 and sum(len(call) for call in fake_embeddings) == chunks


def test_the_job_endpoint_shows_the_state_and_the_summary_when_completed(summary):
    job_id = new_job()
    with TestClient(main.app) as client:
        pending = client.get(f"/jobs/{job_id}").json()
        ingestion.process_job(job_id)
        done = client.get(f"/jobs/{job_id}").json()

    assert (pending["status"], pending["summary"], pending["document_id"]) == ("pending", None, None)
    assert (done["status"], done["summary"]) == ("completed", "A fake summary.")
    assert done["document_id"] == sql("SELECT id FROM documents").scalar_one()
    assert done["error"] is None and "file_content" not in done


def test_a_task_delivered_twice_saves_nothing_the_second_time(summary):
    job_id = new_job()
    ingestion.process_job(job_id)
    assert ingestion.process_job(job_id) is None

    assert count("documents") == 1 and summary.calls == 1
    assert job_row(job_id)["attempts"] == 1


def test_two_deliveries_racing_cannot_save_the_document_twice(summary):
    """The second worker claimed the job before the first finished: its save is refused in
    the same transaction, so no second document or chunks exist."""
    job_id = new_job()
    claimed = jobs.claim_job(job_id)  # the second delivery has claimed it...
    ingestion.process_job(job_id)  # ...and the first one finishes the job
    assert count("documents") == 1
    chunks_before = count("chunks")

    with pytest.raises(jobs.JobAlreadyFinished):
        asyncio.run(ingestion.run_pipeline(claimed))

    assert count("documents") == 1 and count("chunks") == chunks_before


def test_a_failed_embedding_saves_nothing(monkeypatch, summary):
    async def broken(texts):
        raise ai_calls.AIServiceError("embedding failed")

    monkeypatch.setattr(ingestion, "embed_texts", broken)
    job_id = new_job()

    assert ingestion.process_job(job_id) is None

    assert count("documents") == 0 and count("chunks") == 0
    row = job_row(job_id)
    assert (row["status"], row["error_code"]) == ("failed", "ai_error")


def test_a_failed_summary_saves_nothing_and_stops_the_embeddings_work(summary):
    summary.error = ai_calls.AIServiceError("summary failed")
    job_id = new_job()
    ingestion.process_job(job_id)
    assert count("documents") == 0 and count("chunks") == 0
    assert job_row(job_id)["status"] == "failed"


@pytest.mark.parametrize(
    ("content", "file_type", "code", "message"),
    [
        (b"   \n  ", ".txt", "no_text", "No readable text found in the uploaded file."),
        (b"this is not a pdf", ".pdf", "unreadable_file", "Could not read the uploaded file."),
    ],
)
def test_a_file_the_worker_cannot_use_fails_the_job_without_an_ai_call(
    summary, content, file_type, code, message
):
    job_id = new_job(content=content, file_type=file_type)
    ingestion.process_job(job_id)
    row = job_row(job_id)
    assert (row["status"], row["error_code"], row["error_message"]) == ("failed", code, message)
    assert summary.calls == 0 and count("documents") == 0


def test_a_file_that_does_not_match_the_job_is_incomplete(summary):
    job_id = new_job(file_size=len(CONTENT) + 5)
    ingestion.process_job(job_id)
    assert job_row(job_id)["error_code"] == "file_incomplete"


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ai_calls.AIServiceError("The AI service returned an API error: 400"), "ai_error"),
        (ai_calls.AIOutOfCreditError(ai_calls.OUT_OF_CREDIT_MESSAGE), "ai_out_of_credit"),
        (ai_calls.AIServiceNotConfiguredError("AI service is not configured."),
         "ai_not_configured"),
    ],
)
def test_errors_that_will_not_get_better_are_not_retried(summary, error, code):
    summary.error = error
    job_id = new_job()

    assert ingestion.process_job(job_id) is None  # no retry

    row = job_row(job_id)
    assert (row["status"], row["error_code"], row["attempts"]) == ("failed", code, 1)
    assert row["error_message"] == str(error)
    assert summary.calls == 1


def test_an_unexpected_error_fails_the_job_with_a_safe_message_and_is_logged(
    summary, caplog
):
    summary.error = RuntimeError("secret internal detail: password=hunter2")
    job_id = new_job()

    ingestion.process_job(job_id)

    row = job_row(job_id)
    assert (row["status"], row["error_code"]) == ("failed", "internal_error")
    assert row["error_message"] == "Could not process the document."
    assert "hunter2" not in row["error_message"]
    assert any(r.levelname == "ERROR" and "job_failed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "error",
    [TimeoutError("The AI service took too long to respond."),
     ConnectionError("Could not connect to the AI service."),
     ai_calls.AIRateLimitError("The AI service rate limit has been exceeded.")],
)
def test_transient_errors_wait_and_try_again_then_succeed(summary, error):
    summary.error = error
    job_id = new_job()

    wait = ingestion.process_job(job_id)

    assert wait == ingestion.RETRY_BASE_SECONDS
    row = job_row(job_id)
    assert (row["status"], row["attempts"]) == ("pending", 1)
    assert row["error_code"] is not None and row["finished_at"] is None
    assert bytes(row["file_content"]) == CONTENT  # kept for the retry
    assert count("documents") == 0

    summary.error = None  # the provider is back
    assert ingestion.process_job(job_id) is None
    row = job_row(job_id)
    assert (row["status"], row["attempts"], row["error_code"]) == ("completed", 2, None)
    assert count("documents") == 1  # one document, not two


def test_after_the_last_attempt_a_transient_error_fails_the_job(summary):
    summary.error = TimeoutError("The AI service took too long to respond.")
    job_id = new_job()

    waits = [ingestion.process_job(job_id) for _ in range(ingestion.MAX_ATTEMPTS)]

    assert waits == [10, 20, None]
    row = job_row(job_id)
    assert (row["status"], row["attempts"], row["error_code"]) == (
        "failed", ingestion.MAX_ATTEMPTS, "ai_timeout")
    assert row["file_content"] is None
    assert summary.calls == ingestion.MAX_ATTEMPTS and count("documents") == 0


def test_the_wait_grows_and_honors_retry_after_up_to_a_cap():
    assert [ingestion.retry_delay_seconds(n) for n in (1, 2, 3, 4)] == [10, 20, 40, 80]
    assert ingestion.retry_delay_seconds(1, retry_after=45) == 45
    assert ingestion.retry_delay_seconds(1, retry_after=10_000) == ingestion.RETRY_MAX_SECONDS
    assert ingestion.retry_delay_seconds(20) == ingestion.RETRY_MAX_SECONDS


def test_a_missing_or_ended_job_is_skipped(summary):
    assert ingestion.process_job("00000000-0000-0000-0000-000000000009") is None
    job_id = new_job()
    jobs.mark_failed(job_id, "x", "y")
    assert ingestion.process_job(job_id) is None
    assert summary.calls == 0 and job_row(job_id)["attempts"] == 0


def test_the_job_survives_the_deletion_of_its_document(summary):
    job_id = new_job()
    ingestion.process_job(job_id)
    document_id = job_row(job_id)["document_id"]
    with TestClient(main.app) as client:
        assert client.delete(f"/documents/{document_id}").status_code == 200
        job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "completed" and job["document_id"] is None and job["summary"] is None


# --- The Celery task --------------------------------------------------------------------------


def test_the_task_runs_the_job_and_asks_celery_to_retry_after_the_wait(monkeypatch):
    monkeypatch.setattr(ingestion, "process_job", lambda job_id: None)
    worker.ingest_document.run("some-job")  # returns normally

    asked = {}

    def fake_retry(**kwargs):
        asked.update(kwargs)
        return Retry()

    monkeypatch.setattr(ingestion, "process_job", lambda job_id: 42.0)
    monkeypatch.setattr(worker.ingest_document, "retry", fake_retry)
    with pytest.raises(Retry):
        worker.ingest_document.run("some-job")
    assert asked["countdown"] == 42.0


def test_the_worker_is_set_up_for_safe_redelivery():
    conf = worker.celery_app.conf
    assert conf.task_acks_late and conf.task_reject_on_worker_lost
    assert conf.worker_prefetch_multiplier == 1
    assert worker.ingest_document.name == "documents.ingest"


def test_a_new_worker_process_does_not_reuse_inherited_database_connections():
    database._engine()  # the parent process has an engine
    assert database._engines
    worker._fresh_database_connections()
    assert database._engines == {}
    database._engine()  # and makes its own when needed
    with database.get_connection() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_the_ai_client_is_made_again_for_each_new_event_loop(monkeypatch):
    """A Celery task runs each job in a new loop; a client from a closed loop must not be
    reused ("Event loop is closed")."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    created = []
    monkeypatch.setattr(ai_calls, "AsyncOpenAI", lambda **kwargs: created.append(kwargs) or object())
    ai_calls._get_async_client.cache_clear()

    async def twice():
        return ai_calls._get_async_client(), ai_calls._get_async_client()

    try:
        first = asyncio.run(twice())
        second = asyncio.run(twice())
    finally:
        ai_calls._get_async_client.cache_clear()

    assert first[0] is first[1]  # reused inside one loop
    assert second[0] is second[1]
    assert first[0] is not second[0]  # but not across loops
    assert len(created) == 2


def test_two_jobs_in_a_row_in_one_worker_process_both_work(summary):
    """The worker runs asyncio.run() per job; the second job must not trip over the first."""
    first, second = new_job(), new_job(filename="two.txt")
    ingestion.process_job(first)
    ingestion.process_job(second)
    assert count("documents") == 2


# --- The limit of 3 uploads at the same time --------------------------------------------------


def test_a_fourth_queued_upload_is_refused_with_a_clear_429(queue):
    with TestClient(main.app) as client:
        responses = [upload(client, f"d{i}.txt") for i in range(4)]
    assert [r.status_code for r in responses] == [202, 202, 202, 429]
    refused = responses[3]
    assert refused.json() == {
        "detail": "You can upload up to 3 files at a time. Wait for the current uploads to finish.",
        "code": "upload_limit",
    }
    assert refused.headers["retry-after"] == "5"
    assert len(queue) == 3 and count("processing_jobs") == 3


def test_a_finished_job_frees_its_slot(queue, summary):
    with TestClient(main.app) as client:
        ids = [upload(client, f"d{i}.txt").json()["job_id"] for i in range(3)]
        assert upload(client, "again.txt").status_code == 429
        ingestion.process_job(ids[0])
        assert upload(client, "again.txt").status_code == 202


def test_a_job_waiting_for_a_retry_still_counts(queue, summary):
    summary.error = TimeoutError("slow")
    with TestClient(main.app) as client:
        first = upload(client).json()["job_id"]
        ingestion.process_job(first)  # pending again
        upload(client, "b.txt")
        upload(client, "c.txt")
        assert upload(client, "d.txt").status_code == 429


def test_the_limit_is_per_user(queue):
    other = add_user()
    for _ in range(3):
        new_job(other)
    with TestClient(main.app) as client:
        assert upload(client).status_code == 202  # the default user has none running


def test_an_old_unfinished_job_does_not_block_the_user_forever(queue):
    ids = [new_job() for _ in range(3)]
    sql("UPDATE processing_jobs SET updated_at = now() - interval '16 minutes' WHERE id = :id",
        {"id": ids[0]})
    with TestClient(main.app) as client:
        assert upload(client).status_code == 202
        assert upload(client, "b.txt").status_code == 429


def test_the_limit_holds_when_uploads_arrive_at_the_same_moment():
    from concurrent.futures import ThreadPoolExecutor

    def attempt(_):
        try:
            new_job()
            return True
        except jobs.JobLimitReached:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    assert sum(results) == jobs.MAX_ACTIVE_JOBS
    assert count("processing_jobs") == jobs.MAX_ACTIVE_JOBS


# --- Inline mode keeps working, and its uploads count too -------------------------------------


def test_an_inline_upload_still_streams_and_leaves_a_completed_job(monkeypatch):
    async def mock_stream(text):
        yield "Inline summary."

    monkeypatch.setattr(main, "stream_summary", mock_stream)
    with TestClient(main.app) as client:
        response = upload(client)

    assert response.status_code == 200 and '"type": "done"' in response.text
    row = sql("SELECT mode, status, document_id, file_content FROM processing_jobs").one()
    assert (row.mode, row.status, row.file_content) == ("inline", "completed", None)
    assert row.document_id == sql("SELECT id FROM documents").scalar_one()


def test_a_failed_inline_upload_leaves_a_failed_job_not_a_running_one(monkeypatch):
    async def mock_stream(text):
        raise main.AIRateLimitError("Provider rate limit.")
        yield  # pragma: no cover

    monkeypatch.setattr(main, "stream_summary", mock_stream)
    with TestClient(main.app) as client:
        response = upload(client)
        assert '"type": "error"' in response.text
        assert client.get("/documents").json() == []
    assert sql("SELECT status FROM processing_jobs").scalar_one() == "failed"


def test_an_inline_upload_over_the_limit_is_refused_before_any_ai_call(monkeypatch):
    called = []

    async def mock_stream(text):
        called.append(1)
        yield "x"

    monkeypatch.setattr(main, "stream_summary", mock_stream)
    for _ in range(3):
        new_job(mode="inline")
    with TestClient(main.app) as client:
        response = upload(client)
    assert response.status_code == 429 and response.json()["code"] == "upload_limit"
    assert called == []


def test_an_invalid_inline_file_does_not_use_a_slot():
    with TestClient(main.app) as client:
        assert upload(client, "empty.txt", b"   ").status_code == 400
    assert count("processing_jobs") == 0


# --- Saving with a job ------------------------------------------------------------------------


def test_save_with_a_job_completes_the_job_in_the_same_transaction():
    job_id = new_job()
    chunk = Chunk(index=0, page_number=1, text="hello", token_estimate=2, start_char=0, end_char=5)
    vector = [1.0] + [0.0] * (database.EMBEDDING_DIMENSIONS - 1)
    document_id = database.save_document_with_chunks(
        "a.txt", ".txt", 5, b"hello", "hello", "s", [chunk], [vector],
        owner_user_id=DEFAULT_USER_ID, job_id=job_id,
    )
    row = job_row(job_id)
    assert (row["status"], row["document_id"]) == ("completed", document_id)


def test_save_rolls_everything_back_when_the_job_update_fails():
    job_id = new_job()
    jobs.mark_failed(job_id, "x", "y")  # already ended
    chunk = Chunk(index=0, page_number=1, text="hello", token_estimate=2, start_char=0, end_char=5)
    vector = [1.0] + [0.0] * (database.EMBEDDING_DIMENSIONS - 1)
    with pytest.raises(jobs.JobAlreadyFinished):
        database.save_document_with_chunks(
            "a.txt", ".txt", 5, b"hello", "hello", "s", [chunk], [vector],
            owner_user_id=DEFAULT_USER_ID, job_id=job_id,
        )
    assert count("documents") == 0 and count("chunks") == 0
