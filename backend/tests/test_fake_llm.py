"""Fake LLM mode: the real services run against a local stand-in, with no network and no key."""

import asyncio
import json
import logging
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.context as context
import app.database as database
import app.ingestion as ingestion
import app.jobs as jobs
import app.main as main
import app.services.ai_calls as ai_calls
import app.services.chat as chat
import app.services.embeddings as embeddings
import app.services.llm as llm
import app.services.summarizer as summarizer
import app.usage_store as usage_store
import app.worker as worker
from app.services import fake_llm
from tests.default_user import DEFAULT_USER_ID

DIMS = embeddings.EMBEDDING_DIMENSIONS


@pytest.fixture(autouse=True)
def fake_mode(monkeypatch):
    """Fake mode, no waiting, and proof that no real client or key is ever touched."""
    monkeypatch.setenv("FAKE_LLM", "true")
    monkeypatch.setenv("FAKE_LLM_DELAY_SCALE", "0")
    for name in ("AI_API_KEY", "OPENROUTER_API_KEY", "AI_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    def forbidden(**kwargs):
        raise AssertionError("the real AI client was created in fake mode")

    monkeypatch.setattr(ai_calls, "AsyncOpenAI", forbidden)
    ai_calls._get_async_client.cache_clear()


def run(coro):
    return asyncio.run(coro)


# --- Settings and safety ----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "on", "yes"])
def test_on_values(monkeypatch, value):
    monkeypatch.setenv("FAKE_LLM", value)
    assert fake_llm.fake_llm_enabled() is True


@pytest.mark.parametrize("value", ["", "false", "0", "off", "no"])
def test_off_values_and_the_default(monkeypatch, value):
    monkeypatch.setenv("FAKE_LLM", value)
    assert fake_llm.fake_llm_enabled() is False
    monkeypatch.delenv("FAKE_LLM")
    assert fake_llm.fake_llm_enabled() is False


def test_a_bad_value_is_an_error_not_a_guess(monkeypatch):
    monkeypatch.setenv("FAKE_LLM", "sure")
    with pytest.raises(ValueError, match="FAKE_LLM"):
        fake_llm.check_fake_llm_settings()
    monkeypatch.setenv("FAKE_LLM", "true")
    monkeypatch.setenv("FAKE_LLM_DELAY_SCALE", "fast")
    with pytest.raises(ValueError, match="DELAY_SCALE"):
        fake_llm.check_fake_llm_settings()
    monkeypatch.setenv("FAKE_LLM_DELAY_SCALE", "-1")
    with pytest.raises(ValueError):
        fake_llm.check_fake_llm_settings()


def test_fake_mode_is_refused_on_vercel(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(RuntimeError, match="Vercel"):
        fake_llm.check_fake_llm_settings()
    with pytest.raises(RuntimeError):
        with TestClient(main.app):
            pass


def test_fake_mode_is_off_on_vercel_when_not_asked_for(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("FAKE_LLM", "false")
    fake_llm.check_fake_llm_settings()  # nothing to refuse


def test_the_worker_checks_the_same_settings_at_startup(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    with pytest.raises(RuntimeError):
        worker._check_settings()


def test_starting_in_fake_mode_is_announced(caplog):
    with caplog.at_level(logging.WARNING):
        fake_llm.check_fake_llm_settings()
    assert any("FAKE_LLM is on" in r.getMessage() for r in caplog.records)


def test_health_says_so_only_in_fake_mode(monkeypatch):
    with TestClient(main.app) as client:
        assert client.get("/health").json() == {"status": "healthy", "fake_llm": True}
        monkeypatch.setenv("FAKE_LLM", "false")
        assert client.get("/health").json() == {"status": "healthy"}


def test_no_key_is_needed_and_no_real_client_is_made():
    client = ai_calls._get_async_client()
    assert isinstance(client, fake_llm.FakeClient)


def test_with_fake_mode_off_the_real_client_is_used_again(monkeypatch):
    monkeypatch.setenv("FAKE_LLM", "false")
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    with pytest.raises(AssertionError, match="real AI client"):
        ai_calls._get_async_client()


def test_delays_follow_the_scale(monkeypatch):
    monkeypatch.setenv("FAKE_LLM_DELAY_SCALE", "1")
    started = time.perf_counter()
    run(fake_llm._wait(0.2))
    assert 0.12 <= time.perf_counter() - started < 0.6
    monkeypatch.setenv("FAKE_LLM_DELAY_SCALE", "0")
    started = time.perf_counter()
    run(fake_llm._wait(5))
    assert time.perf_counter() - started < 0.1


# --- The real services on the fake provider ---------------------------------------------------


def test_embeddings_are_deterministic_unit_vectors_of_the_right_size():
    first = run(embeddings.embed_texts(["alpha", "beta"]))
    again = run(embeddings.embed_texts(["alpha", "beta"]))
    assert first == again and first[0] != first[1]
    assert all(len(v) == DIMS for v in first)
    assert sum(x * x for x in first[0]) == pytest.approx(1.0)


def test_the_summary_streams_in_pieces_and_is_marked_fake():
    async def collect():
        return [part async for part in summarizer.stream_summary("A document. " * 50)]

    parts = run(collect())
    assert len(parts) > 5
    assert "".join(parts).startswith(fake_llm.FAKE_MARKER)


def test_a_plain_completion_works():
    text_ = run(llm.complete(model="m", messages=[{"role": "user", "content": "Hi"}], kind="rewrite"))
    assert text_.startswith(fake_llm.FAKE_MARKER)


def test_usage_is_logged_with_tokens_and_zero_cost(caplog):
    with caplog.at_level(logging.INFO, logger="app"):
        run(embeddings.embed_texts(["some text to embed"]))
    line = next(r.getMessage() for r in caplog.records if "ai_usage" in r.getMessage())
    assert "kind=embedding" in line and "outcome=ok" in line
    assert "prompt_tokens=4" in line and "cost_usd=0.0" in line


def test_the_whole_queue_pipeline_runs_on_the_fake(monkeypatch):
    monkeypatch.setattr(ingestion, "embed_texts", embeddings.embed_texts)  # undo the test double
    content = b"A short document about load testing. " * 20
    job_id = jobs.create_job(DEFAULT_USER_ID, mode="queue", filename="d.txt", file_type=".txt",
                             file_size=len(content), file_content=content)
    assert ingestion.process_job(job_id) is None
    with database.get_connection() as connection:
        summary = connection.execute(text("SELECT summary FROM documents")).scalar_one()
        chunks = connection.execute(text("SELECT count(*) FROM chunks")).scalar_one()
    assert summary.startswith(fake_llm.FAKE_MARKER) and chunks >= 1
    usage_store.flush()
    with database.get_connection() as connection:
        kinds = connection.execute(text("SELECT DISTINCT kind FROM ai_usage ORDER BY 1")).scalars().all()
    assert kinds == ["embedding", "summary"]


def test_chat_answers_from_the_fake_with_a_citation():
    content = b"Refunds are paid within 14 days of the request. " * 10

    async def go():
        job_id = jobs.create_job(DEFAULT_USER_ID, mode="queue", filename="d.txt", file_type=".txt",
                                 file_size=len(content), file_content=content)
        await asyncio.to_thread(ingestion.process_job, job_id)
        # The test suite replaces the question embedding; here the fake one is used end to end.
        return None

    run(go())
    with TestClient(main.app) as client:
        response = client.post("/chat/stream", json={"message": "When are refunds paid?"})
    events = [json.loads(line[6:]) for line in response.text.split("\n") if line.startswith("data: ")]
    assert events[0]["type"] == "sources" and events[0]["sources"]
    answer = "".join(e["content"] for e in events if e["type"] == "delta")
    assert answer.startswith(fake_llm.FAKE_MARKER) and "[1]" in answer
    assert events[-1]["type"] == "done"


def test_closing_a_fake_stream_stops_it():
    async def go():
        stream = await ai_calls._get_async_client().chat.completions.create(
            model="m", messages=[{"role": "user", "content": "q"}], stream=True)
        seen = []
        async for chunk in stream:
            seen.append(chunk)
            await stream.close()
        return seen

    assert len(run(go())) == 1
