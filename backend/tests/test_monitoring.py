"""Monitoring: the ai_usage table, job ids in logs, and the token-protected /metrics endpoint.

No Redis server, no AI calls: usage is written through `log_ai_usage` with made-up numbers.
"""

import logging
import queue
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.cache as cache
import app.context as context
import app.database as database
import app.ingestion as ingestion
import app.jobs as jobs
import app.main as main
import app.metrics as metrics
import app.usage_store as usage_store
from app.services.usage import log_ai_usage
from tests.default_user import DEFAULT_USER_ID
from tests.test_cache import FakeRedis

TOKEN = "s3cret-metrics-token"
JOB = "7f0f2b5e-0000-4000-8000-000000000001"


def sql(query, params=None):
    with database.get_connection() as connection:
        return connection.execute(text(query), params or {})


def rows():
    usage_store.flush()
    return sql("SELECT * FROM ai_usage ORDER BY id").mappings().all()


def add_user(email="other@example.com") -> int:
    return sql("INSERT INTO users (email, password_hash) VALUES (:e, 'x') RETURNING id",
               {"e": email}).scalar_one()


def call(kind="summary", model="m1", outcome="ok", prompt=100, completion=20, cost=0.0012):
    usage = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                            total_tokens=prompt + completion, cost=cost)
    return log_ai_usage(kind=kind, model=model, usage=usage,
                        started_at=time.perf_counter() - 0.25, outcome=outcome)


@pytest.fixture(autouse=True)
def clean_metrics():
    metrics.reset()


# --- The ai_usage table -----------------------------------------------------------------------


def test_a_call_made_for_a_user_is_recorded_with_everything_a_dashboard_needs():
    with context.use_context(user_id=DEFAULT_USER_ID, job_id=JOB):
        call(kind="embedding", model="emb", prompt=50, completion=0, cost=0.0001)

    [row] = rows()
    assert row["user_id"] == DEFAULT_USER_ID and str(row["job_id"]) == JOB
    assert (row["kind"], row["model"], row["outcome"]) == ("embedding", "emb", "ok")
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (50, 0, 50)
    assert float(row["cost_usd"]) == 0.0001 and row["cost_source"] == "openrouter"
    assert 200 <= row["duration_ms"] < 5000
    assert row["created_at"] is not None


def test_a_call_with_unknown_usage_is_still_recorded():
    with context.use_context(user_id=DEFAULT_USER_ID):
        log_ai_usage(kind="chat", model="m", usage=None, started_at=time.perf_counter(),
                     outcome="error")
    [row] = rows()
    assert row["outcome"] == "error" and row["prompt_tokens"] is None and row["job_id"] is None


def test_a_call_with_no_user_is_not_recorded():
    call()
    assert rows() == []


def test_each_user_gets_their_own_rows():
    other = add_user()
    with context.use_context(user_id=DEFAULT_USER_ID):
        call(model="a")
    with context.use_context(user_id=other):
        call(model="b")
    assert [(r["user_id"], r["model"]) for r in rows()] == [(DEFAULT_USER_ID, "a"), (other, "b")]


def test_deleting_an_account_deletes_its_usage():
    other = add_user()
    with context.use_context(user_id=other):
        call()
    assert len(rows()) == 1
    sql("DELETE FROM users WHERE id = :id", {"id": other})
    assert rows() == []


def test_recording_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("RECORD_AI_USAGE", "false")
    with context.use_context(user_id=DEFAULT_USER_ID):
        call()
    assert rows() == []


def test_recording_never_waits_for_the_database(monkeypatch):
    """The write happens in a background thread: a slow database doesn't slow the call."""
    import threading

    release = threading.Event()
    real = database.get_connection

    def slow_connection():
        release.wait(5)
        return real()

    monkeypatch.setattr(database, "get_connection", slow_connection)
    started = time.perf_counter()
    with context.use_context(user_id=DEFAULT_USER_ID):
        call()
    assert time.perf_counter() - started < 0.5  # it only queued the row
    release.set()
    assert len(rows()) == 1


def test_a_failing_write_is_logged_and_dropped_without_failing_anything(monkeypatch, caplog):
    def broken():
        raise RuntimeError("database is down")

    monkeypatch.setattr(database, "get_connection", broken)
    before = usage_store.dropped
    with caplog.at_level(logging.WARNING):
        with context.use_context(user_id=DEFAULT_USER_ID):
            result = call()  # the caller gets its normal result
        usage_store.flush()
    assert result["kind"] == "summary"
    assert usage_store.dropped == before + 1
    assert any("ai_usage_dropped" in r.getMessage() and "write_failed" in r.getMessage()
               for r in caplog.records)
    monkeypatch.undo()
    with context.use_context(user_id=DEFAULT_USER_ID):
        call()  # the writer survived the failure
    assert len(rows()) == 1


def test_a_full_queue_drops_rows_and_never_blocks_or_raises(monkeypatch, caplog):
    class Full:
        def put_nowait(self, row):
            raise queue.Full

    monkeypatch.setattr(usage_store, "_queue", Full())
    monkeypatch.setattr(usage_store, "_ensure_writer", lambda: None)
    with caplog.at_level(logging.WARNING):
        with context.use_context(user_id=DEFAULT_USER_ID):
            assert usage_store.record({"kind": "k", "model": "m", "outcome": "ok",
                                       "duration_ms": 1}) is False
    assert any("queue_full" in r.getMessage() for r in caplog.records)


def test_a_malformed_record_is_dropped_not_raised():
    with context.use_context(user_id=DEFAULT_USER_ID):
        assert usage_store.record({"kind": "k"}) is False  # missing fields


def test_the_worker_records_its_calls_for_the_jobs_owner_and_job(monkeypatch):
    async def summary(text):
        log_ai_usage(kind="summary", model="m", usage=None, started_at=time.perf_counter())
        yield "A summary."

    monkeypatch.setattr(ingestion, "stream_summary", summary)
    job_id = jobs.create_job(DEFAULT_USER_ID, mode="queue", filename="d.txt", file_type=".txt",
                             file_size=11, file_content=b"hello world")
    ingestion.process_job(job_id)
    [row] = rows()
    assert (row["user_id"], str(row["job_id"])) == (DEFAULT_USER_ID, job_id)
    assert context.current_user_id.get() is None  # the context ended with the job


def test_a_chat_questions_calls_are_recorded_for_the_asking_user(monkeypatch):
    async def prepare(message, history, document_ids, *, owner_user_id):
        log_ai_usage(kind="embedding", model="emb", usage=None, started_at=time.perf_counter())
        raise main.AIServiceError("stop here")

    monkeypatch.setattr(main, "prepare_answer", prepare)
    with TestClient(main.app) as client:
        response = client.post("/chat/stream", json={"message": "Hi?"})
    assert '"type": "error"' in response.text
    [row] = rows()
    assert (row["user_id"], row["kind"]) == (DEFAULT_USER_ID, "embedding")


def test_an_inline_upload_records_its_calls_with_the_job(monkeypatch):
    async def summary(text):
        log_ai_usage(kind="summary", model="m", usage=None, started_at=time.perf_counter())
        yield "A summary."

    monkeypatch.setattr(main, "stream_summary", summary)
    with TestClient(main.app) as client:
        client.post("/documents/upload/stream",
                    files={"file": ("a.txt", b"Some text to read.", "text/plain")})
    [row] = rows()
    job = sql("SELECT id FROM processing_jobs").scalar_one()
    assert (row["user_id"], str(row["job_id"])) == (DEFAULT_USER_ID, str(job))


# --- Job ids in logs --------------------------------------------------------------------------


@pytest.fixture
def log_context():
    context.install_log_context()


def test_the_ai_usage_line_names_the_user_and_the_job(log_context, caplog):
    with caplog.at_level(logging.INFO, logger="app"):
        with context.use_context(user_id=5, job_id=JOB):
            call()
        call()
    inside, outside = [r.getMessage() for r in caplog.records if "ai_usage" in r.getMessage()]
    assert f"user_id=5 job_id={JOB}" in inside.replace("  ", " ") or (
        "user_id=5" in inside and f"job_id={JOB}" in inside)
    assert "user_id" not in outside and "job_id" not in outside


def test_every_app_log_line_made_during_a_job_carries_its_id(log_context, caplog):
    other = logging.getLogger("app.services.ai_calls")
    with caplog.at_level(logging.INFO, logger="app"):
        with context.use_context(job_id=JOB):
            other.warning("ai_retry kind=%s retry=%d/%d", "summary", 1, 2)
            logging.getLogger("app.ingestion").info("job_started job_id=%s", JOB)
        other.warning("ai_retry kind=%s", "summary")
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0] == f"ai_retry kind=summary retry=1/2 job_id={JOB}"
    assert messages[1] == f"job_started job_id={JOB}"  # already named: not repeated
    assert messages[2] == "ai_retry kind=summary"  # outside a job: unchanged


def test_logs_of_other_libraries_are_left_alone(log_context, caplog):
    with caplog.at_level(logging.INFO):
        with context.use_context(job_id=JOB):
            logging.getLogger("celery.app.trace").info("Task done")
    assert [r.getMessage() for r in caplog.records if r.name == "celery.app.trace"] == ["Task done"]


def test_a_job_context_does_not_leak_between_jobs(log_context):
    with context.use_context(user_id=1, job_id="a"):
        pass
    assert context.current_job_id.get() is None and context.current_user_id.get() is None


def test_the_worker_logs_name_the_job_even_for_failures(log_context, caplog, monkeypatch):
    async def broken(text):
        logging.getLogger("app.services.ai_calls").warning("provider said no")
        raise ingestion.AIServiceError("bad")
        yield  # pragma: no cover

    monkeypatch.setattr(ingestion, "stream_summary", broken)
    job_id = jobs.create_job(DEFAULT_USER_ID, mode="queue", filename="d.txt", file_type=".txt",
                             file_size=5, file_content=b"hello")
    with caplog.at_level(logging.INFO, logger="app"):
        ingestion.process_job(job_id)
    for record in caplog.records:
        if record.name.startswith("app"):
            assert f"job_id={job_id}" in record.getMessage(), record.getMessage()


# --- /metrics: access -------------------------------------------------------------------------


@pytest.fixture
def with_token(monkeypatch):
    monkeypatch.setenv("METRICS_TOKEN", TOKEN)


def get_metrics(headers=None):
    with TestClient(main.app) as client:
        return client.get("/metrics", headers=headers or {})


def bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_metrics_is_off_without_a_token(monkeypatch):
    monkeypatch.delenv("METRICS_TOKEN", raising=False)
    assert get_metrics(bearer()).status_code == 404
    monkeypatch.setenv("METRICS_TOKEN", "   ")
    assert get_metrics(bearer("")).status_code == 404


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN},
     {"Authorization": f"Basic {TOKEN}"}, {"Authorization": "Bearer "},
     {"Authorization": f"Bearer {TOKEN}x"}, {"Authorization": f"Bearer {TOKEN[:-1]}"}],
)
def test_metrics_refuses_a_missing_or_wrong_token(with_token, headers):
    response = get_metrics(headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert TOKEN not in response.text and "documind_" not in response.text


def test_metrics_accepts_the_token(with_token):
    response = get_metrics(bearer())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "documind_http_requests_total" in response.text


def test_a_signed_in_user_cannot_read_metrics_without_the_token(with_token):
    # The default user is signed in for every request in these tests.
    assert get_metrics().status_code == 401


def test_the_token_is_compared_in_constant_time(monkeypatch):
    used = []
    real = metrics.secrets.compare_digest
    monkeypatch.setattr(metrics.secrets, "compare_digest",
                        lambda a, b: used.append(1) or real(a, b))
    assert metrics.token_matches("Bearer abc", "abc") is True
    assert used


def test_metrics_is_not_in_the_api_documentation(with_token):
    with TestClient(main.app) as client:
        assert "/metrics" not in client.get("/openapi.json").text


# --- /metrics: content ------------------------------------------------------------------------


def scrape() -> dict[str, float]:
    with TestClient(main.app) as client:
        body = client.get("/metrics", headers=bearer()).text
    values = {}
    for line in body.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            values[name] = float(value)
    return values


def test_requests_are_counted_by_route_template_and_status(with_token):
    with TestClient(main.app) as client:
        client.get("/health")
        client.get("/health")
        client.delete("/documents/12345")  # 404 for this user
        client.get("/no/such/page")
    values = scrape()
    assert values['documind_http_requests_total{method="GET",route="/health",status="200"}'] == 2
    assert values['documind_http_requests_total{method="DELETE",route="/documents/{document_id}",status="404"}'] == 1
    assert values['documind_http_requests_total{method="GET",route="unmatched",status="404"}'] == 1
    assert not any("12345" in name for name in values)  # ids never become labels


def test_request_latency_is_a_histogram(with_token):
    with TestClient(main.app) as client:
        client.get("/health")
    values = scrape()
    base = 'documind_http_request_duration_seconds_%s{method="GET",route="/health"'
    assert values[(base % "count") + "}"] == 1
    assert values[(base % "bucket") + ',le="+Inf"}'] == 1
    assert values[(base % "bucket") + ',le="300"}'] == 1
    assert 0 <= values[(base % "sum") + "}"] < 5


def test_a_streamed_response_is_counted_and_still_streams(with_token, monkeypatch):
    async def summary(text):
        yield "Part one. "
        yield "Part two."

    monkeypatch.setattr(main, "stream_summary", summary)
    with TestClient(main.app) as client:
        response = client.post("/documents/upload/stream",
                               files={"file": ("a.txt", b"Some text.", "text/plain")})
    assert response.text.count('"type": "delta"') == 2 and '"type": "done"' in response.text
    key = 'documind_http_requests_total{method="POST",route="/documents/upload/stream",status="200"}'
    assert scrape()[key] == 1


def test_ai_calls_tokens_cost_and_time_are_summed_by_kind_model_and_outcome(with_token):
    other = add_user()
    with context.use_context(user_id=DEFAULT_USER_ID):
        call(kind="summary", model="m1", prompt=100, completion=20, cost=0.01)
        call(kind="summary", model="m1", prompt=50, completion=10, cost=0.005)
        call(kind="chat", model="m2", outcome="error", prompt=7, completion=0, cost=0.0)
    with context.use_context(user_id=other):
        call(kind="summary", model="m1", prompt=1, completion=1, cost=0.0)
    usage_store.flush()
    values = scrape()
    label = 'kind="summary",model="m1",outcome="ok"'
    assert values[f"documind_ai_calls_total{{{label}}}"] == 3  # all users together
    assert values[f"documind_ai_prompt_tokens_total{{{label}}}"] == 151
    assert values[f"documind_ai_completion_tokens_total{{{label}}}"] == 31
    assert values[f"documind_ai_cost_usd_total{{{label}}}"] == pytest.approx(0.015)
    assert values[f"documind_ai_duration_seconds_total{{{label}}}"] == pytest.approx(0.75, abs=0.2)
    assert values['documind_ai_calls_total{kind="chat",model="m2",outcome="error"}'] == 1


def test_jobs_are_counted_by_mode_and_state(with_token):
    counter = iter(range(100))

    def job(mode, status):
        # A new user each time, so the limit of 3 running uploads per user doesn't interfere.
        job_id = jobs.create_job(add_user(f"u{next(counter)}@e.com"), mode=mode, filename="d",
                                 file_type=".txt", file_size=1, file_content=b"x")
        sql("UPDATE processing_jobs SET status = :s WHERE id = :id", {"s": status, "id": job_id})

    job("queue", "pending")
    job("queue", "pending")
    job("queue", "processing")
    job("queue", "failed")
    job("inline", "processing")
    values = scrape()
    assert values['documind_jobs{mode="queue",state="pending"}'] == 2
    assert values['documind_jobs{mode="queue",state="processing"}'] == 1
    assert values['documind_jobs{mode="queue",state="failed"}'] == 1
    assert values['documind_jobs{mode="queue",state="completed"}'] == 0
    assert values['documind_jobs{mode="inline",state="processing"}'] == 1


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(cache, "_make_client", lambda url: fake)
    cache.reset_for_tests()
    return fake


def test_cache_hits_and_misses_are_shared_totals(with_token, fake_redis):
    cache.get_many_sync("embedding", ["a", "b"])  # 2 misses
    cache.set_many_sync("embedding", {"a": [1.0]})
    cache.get_many_sync("embedding", ["a", "b"])  # 1 hit, 1 miss
    cache.stats.clear()  # another process's counts live only in Redis
    values = scrape()
    assert values['documind_cache_lookups_total{kind="embedding",result="hit"}'] == 1
    assert values['documind_cache_lookups_total{kind="embedding",result="miss"}'] == 3
    assert values['documind_cache_lookups_total{kind="summary",result="hit"}'] == 0


def test_cache_numbers_fall_back_to_this_process_when_redis_is_down(with_token, fake_redis):
    cache.get_many_sync("summary", ["k"])  # one miss, counted
    fake_redis.down = True
    values = scrape()
    assert values['documind_cache_lookups_total{kind="summary",result="miss"}'] == 1


def test_queue_depth_is_shown_when_redis_answers(with_token, monkeypatch):
    monkeypatch.setattr(metrics, "queue_depth", lambda: 4)
    assert scrape()["documind_queue_depth"] == 4
    monkeypatch.setattr(metrics, "queue_depth", lambda: None)
    assert "documind_queue_depth" not in scrape()


def test_an_unreachable_redis_means_no_queue_depth_not_a_failed_scrape(with_token, monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:1/0")
    assert metrics.queue_depth() is None
    assert scrape()['documind_metrics_collector_errors{collector="queue"}'] == 0


def test_a_failing_collector_is_reported_but_the_scrape_still_works(with_token, monkeypatch, caplog):
    def broken(out):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(metrics, "_jobs", broken)
    with caplog.at_level(logging.WARNING):
        values = scrape()
    assert values['documind_metrics_collector_errors{collector="jobs"}'] == 1
    assert values['documind_metrics_collector_errors{collector="ai"}'] == 0
    assert "documind_jobs{" not in "".join(values)
    assert any("metrics_collector_failed" in r.getMessage() for r in caplog.records)


def test_metrics_contain_no_personal_data(with_token):
    with context.use_context(user_id=DEFAULT_USER_ID):
        call()
    usage_store.flush()
    with TestClient(main.app) as client:
        client.post("/documents/upload/stream",
                    files={"file": ("secret-name.txt", b"confidential words", "text/plain")})
        body = client.get("/metrics", headers=bearer()).text
    for forbidden in ("secret-name", "confidential", "@example.com", "password", TOKEN):
        assert forbidden not in body


def test_label_values_are_escaped():
    out = metrics._Output()
    out.sample("x", 1, model='a"b\\c\nd')
    assert out.lines == ['x{model="a\\"b\\\\c\\nd"} 1']


def test_the_flower_and_metrics_pieces_exist_in_compose():
    import pathlib

    compose = (pathlib.Path(__file__).parents[2] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "flower:" in compose and "mher/flower:2.0.1" in compose
    assert '"127.0.0.1:5555:5555"' in compose  # local only
    # The local worker sends the events Flower shows (the image itself doesn't: see test_prod_worker).
    worker_block = compose[compose.index("  worker:"):compose.index("  flower:")]
    assert '"-E"' in worker_block
