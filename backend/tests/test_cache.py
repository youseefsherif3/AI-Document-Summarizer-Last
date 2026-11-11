"""The Redis cache (app/cache.py) with a fake Redis: no server, no AI calls."""

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import text

import app.cache as cache
import app.database as database
import app.ingestion as ingestion
import app.jobs as jobs
import app.main as main
import app.services.chat as chat
import app.services.embeddings as embeddings
from app.services.ai_calls import ai_base_url
from tests.default_user import DEFAULT_USER_ID

DIMS = embeddings.EMBEDDING_DIMENSIONS


class FakeRedis:
    """Just the commands the cache uses. `down` makes every command fail like a dead server."""

    def __init__(self):
        self.data: dict[str, tuple[str, int | None]] = {}
        self.hashes: dict[str, dict[str, int]] = {}
        self.down = False
        self.commands = 0

    def _check(self):
        self.commands += 1
        if self.down:
            raise redis.ConnectionError("connection refused")

    def mget(self, keys):
        self._check()
        return [self.data[k][0] if k in self.data else None for k in keys]

    def hgetall(self, key):
        self._check()
        return {k: str(v) for k, v in self.hashes.get(key, {}).items()}

    def delete(self, key):
        self._check()
        self.data.pop(key, None)

    def pipeline(self, transaction=False):
        outer = self

        class Pipe:
            def __init__(self):
                self.ops = []

            def set(self, key, value, ex=None):
                self.ops.append((key, value, ex))

            def hincrby(self, key, field, amount):
                self.ops.append((key, field, amount))

            def execute(self):
                outer._check()
                for op in self.ops:
                    if len(op) == 3 and isinstance(op[1], str) and isinstance(op[2], int)                             and op[0].endswith("stats"):
                        outer.hashes.setdefault(op[0], {})
                        outer.hashes[op[0]][op[1]] = outer.hashes[op[0]].get(op[1], 0) + op[2]
                    else:
                        key, value, ex = op
                        outer.data[key] = (value, ex)

        return Pipe()


@pytest.fixture
def fake_redis(monkeypatch):
    """The cache switched on, talking to a FakeRedis."""
    fake = FakeRedis()
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(cache, "_make_client", lambda url: fake)
    cache.reset_for_tests()
    return fake


@pytest.fixture
def provider(monkeypatch):
    """A fake embedding provider that counts what it is asked. Vector = [len(text), 0, ...]."""
    state = SimpleNamespace(batches=[])

    async def fake_batch(batch):
        state.batches.append(list(batch))
        return [[float(len(t))] + [0.0] * (DIMS - 1) for t in batch]

    monkeypatch.setattr(embeddings, "_embed_batch", fake_batch)
    return state


@pytest.fixture
def summary(monkeypatch):
    state = SimpleNamespace(calls=0)

    async def fake(text):
        state.calls += 1
        yield "A fake summary."

    monkeypatch.setattr(ingestion, "stream_summary", fake)
    return state


def run(coro):
    return asyncio.run(coro)


def count(table):
    with database.get_connection() as connection:
        return connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()


def new_job(owner=DEFAULT_USER_ID, content=b"The same document text. " * 10):
    return jobs.create_job(owner, mode="queue", filename="d.txt", file_type=".txt",
                           file_size=len(content), file_content=content)


# --- Settings ---------------------------------------------------------------------------------


def test_the_cache_is_off_without_a_redis_url(monkeypatch):
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.delenv("CACHE_REDIS_URL", raising=False)
    assert cache.redis_url() is None
    assert cache.get_many_sync("embedding", ["k"]) == [None]


@pytest.mark.parametrize("value", ["false", "0", "OFF", "no"])
def test_the_cache_can_be_turned_off(fake_redis, monkeypatch, value):
    monkeypatch.setenv("CACHE_ENABLED", value)
    cache.set_many_sync("embedding", {"k": [1.0]})
    assert cache.get_many_sync("embedding", ["k"]) == [None]
    assert fake_redis.commands == 0  # Redis is not even contacted


def test_the_cache_uses_database_1_of_the_queue_redis_unless_told_otherwise(monkeypatch):
    monkeypatch.setenv("REDIS_URL", "redis://:pw@redis:6379/0")
    monkeypatch.delenv("CACHE_REDIS_URL", raising=False)
    assert cache.redis_url() == "redis://:pw@redis:6379/1"
    monkeypatch.setenv("CACHE_REDIS_URL", "redis://other:6380/5")
    assert cache.redis_url() == "redis://other:6380/5"


@pytest.mark.parametrize(
    ("variable", "value"),
    [("CACHE_ENABLED", "maybe"), ("CACHE_EMBEDDING_TTL_SECONDS", "soon"),
     ("CACHE_SUMMARY_TTL_SECONDS", "0")],
)
def test_bad_settings_are_errors(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable):
        cache.check_cache_settings()


def test_the_app_refuses_to_start_with_a_bad_cache_setting(monkeypatch):
    monkeypatch.setenv("CACHE_ENABLED", "perhaps")
    with pytest.raises(ValueError):
        with TestClient(main.app):
            pass


def test_values_expire(fake_redis, monkeypatch):
    cache.set_many_sync("embedding", {"k": [1.0]})
    assert fake_redis.data["documind:cache:embedding:k"][1] == 7 * 24 * 3600
    monkeypatch.setenv("CACHE_SUMMARY_TTL_SECONDS", "3600")
    cache.set_many_sync("summary", {"1:k": "s"})
    assert fake_redis.data["documind:cache:summary:1:k"][1] == 3600


# --- Keys -------------------------------------------------------------------------------------


def test_embedding_keys_depend_on_provider_model_dimensions_and_text():
    base = cache.embedding_key("https://a/v1", "m", 1536, "hello")
    assert base == cache.embedding_key("https://a/v1", "m", 1536, "hello")
    assert len({base,
                cache.embedding_key("https://b/v1", "m", 1536, "hello"),
                cache.embedding_key("https://a/v1", "m2", 1536, "hello"),
                cache.embedding_key("https://a/v1", "m", 768, "hello"),
                cache.embedding_key("https://a/v1", "m", 1536, "hello!")}) == 5
    assert "hello" not in base  # a hash: the text is not in the key


def test_keys_cannot_be_confused_by_moving_characters_between_parts():
    assert cache.digest("ab", "c") != cache.digest("a", "bc")


def test_summary_keys_belong_to_one_owner_and_one_text_model_and_version(monkeypatch):
    key = cache.summary_key(1, "u", "m", "text")
    assert key.startswith("1:") and key != cache.summary_key(2, "u", "m", "text")
    assert key != cache.summary_key(1, "u", "other-model", "text")
    assert key != cache.summary_key(1, "u", "m", "other text")
    monkeypatch.setattr(cache, "SUMMARY_CACHE_VERSION", "2")
    assert key != cache.summary_key(1, "u", "m", "text")


# --- Embeddings -------------------------------------------------------------------------------


def test_a_repeated_text_is_embedded_once(fake_redis, provider):
    first = run(embeddings.embed_texts(["alpha", "beta"]))
    second = run(embeddings.embed_texts(["alpha", "beta"]))
    assert first == second
    assert provider.batches == [["alpha", "beta"]]  # the second call never reached the provider
    assert cache.stats["embedding.hit"] == 2 and cache.stats["embedding.miss"] == 2


def test_only_the_missing_texts_are_sent_and_the_order_is_kept(fake_redis, provider):
    run(embeddings.embed_texts(["beta"]))
    vectors = run(embeddings.embed_texts(["alpha", "beta", "gamma!"]))
    assert provider.batches == [["beta"], ["alpha", "gamma!"]]
    assert [v[0] for v in vectors] == [5.0, 4.0, 6.0]


def test_cached_vectors_are_exactly_the_fresh_ones(fake_redis, provider):
    fresh = run(embeddings.embed_texts(["some text"]))
    assert run(embeddings.embed_texts(["some text"])) == fresh


def test_a_chat_question_embedding_uses_the_same_cache(fake_redis, provider):
    run(embeddings.embed_texts(["What is the refund policy?"]))
    vector = run(embeddings.embed_query("What is the refund policy?"))
    assert len(provider.batches) == 1 and len(vector) == DIMS


def test_a_wrong_sized_cached_vector_is_ignored_and_replaced(fake_redis, provider):
    key = cache.embedding_key(ai_base_url(), embeddings.EMBEDDING_MODEL, DIMS, "x")
    fake_redis.data[f"documind:cache:embedding:{key}"] = (json.dumps([1.0, 2.0]), None)
    vector = run(embeddings.embed_texts(["x"]))
    assert len(vector[0]) == DIMS and provider.batches == [["x"]]
    assert len(json.loads(fake_redis.data[f"documind:cache:embedding:{key}"][0])) == DIMS


def test_an_unreadable_cached_value_is_a_miss(fake_redis, provider, caplog):
    key = cache.embedding_key(ai_base_url(), embeddings.EMBEDDING_MODEL, DIMS, "x")
    fake_redis.data[f"documind:cache:embedding:{key}"] = ("{not json", None)
    with caplog.at_level(logging.WARNING):
        run(embeddings.embed_texts(["x"]))
    assert provider.batches == [["x"]]
    assert any("cache_corrupt" in r.getMessage() for r in caplog.records)


def test_batches_over_64_are_cached_as_they_arrive(fake_redis, provider):
    texts = [f"text {i}" for i in range(70)]
    run(embeddings.embed_texts(texts))
    assert [len(b) for b in provider.batches] == [64, 6]
    run(embeddings.embed_texts(texts))
    assert len(provider.batches) == 2  # all 70 came from the cache


def test_a_failed_batch_keeps_what_earlier_batches_cached(fake_redis, monkeypatch):
    calls = []

    async def flaky(batch):
        calls.append(len(batch))
        if len(calls) == 2:
            raise embeddings.AIServiceError("provider down")
        return [[1.0] + [0.0] * (DIMS - 1) for _ in batch]

    monkeypatch.setattr(embeddings, "_embed_batch", flaky)
    texts = [f"text {i}" for i in range(70)]
    with pytest.raises(embeddings.AIServiceError):
        run(embeddings.embed_texts(texts))
    assert len(fake_redis.data) == 64  # the first batch was paid for, so it is kept


def test_embedding_cache_is_not_used_when_off(fake_redis, provider, monkeypatch):
    monkeypatch.setenv("CACHE_ENABLED", "false")
    run(embeddings.embed_texts(["a"]))
    run(embeddings.embed_texts(["a"]))
    assert len(provider.batches) == 2 and fake_redis.data == {}


# --- Redis down: the app keeps working --------------------------------------------------------


def test_embeddings_still_work_when_redis_is_down_and_a_warning_is_logged(
    fake_redis, provider, caplog
):
    fake_redis.down = True
    with caplog.at_level(logging.WARNING):
        first = run(embeddings.embed_texts(["alpha"]))
        second = run(embeddings.embed_texts(["alpha"]))
    assert first == second and len(provider.batches) == 2  # no cache, no failure
    warnings = [r for r in caplog.records if "cache_unavailable" in r.getMessage()]
    assert len(warnings) == 1  # one warning, not one per request


def test_after_a_failure_redis_is_left_alone_for_a_while_then_tried_again(
    fake_redis, provider, monkeypatch
):
    fake_redis.down = True
    run(embeddings.embed_texts(["a"]))
    commands = fake_redis.commands
    run(embeddings.embed_texts(["b"]))
    assert fake_redis.commands == commands  # paused: no new timeout for every request

    fake_redis.down = False
    now = cache.time.monotonic()
    monkeypatch.setattr(cache.time, "monotonic", lambda: now + cache.FAILURE_PAUSE_SECONDS + 1)
    run(embeddings.embed_texts(["c"]))
    run(embeddings.embed_texts(["c"]))
    assert provider.batches[-1] == ["c"] and provider.batches.count(["c"]) == 1


def test_an_os_error_from_the_socket_is_handled_too(monkeypatch, provider):
    class Broken(FakeRedis):
        def mget(self, keys):
            raise TimeoutError("timed out")

    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    monkeypatch.setattr(cache, "_make_client", lambda url: Broken())
    cache.reset_for_tests()
    assert len(run(embeddings.embed_texts(["a"]))) == 1


def test_a_real_unreachable_redis_does_not_break_or_slow_a_request(monkeypatch, provider):
    monkeypatch.setenv("CACHE_ENABLED", "true")
    monkeypatch.setenv("CACHE_REDIS_URL", "redis://127.0.0.1:1/0")  # nothing listens here
    cache.reset_for_tests()
    started = cache.time.monotonic()
    assert len(run(embeddings.embed_texts(["a"]))) == 1
    assert len(run(embeddings.embed_texts(["a"]))) == 1
    assert cache.time.monotonic() - started < 3
    assert cache.stats["embedding.error"] >= 1


def test_chat_still_streams_when_redis_is_down(fake_redis, monkeypatch):
    # The autouse fake replaces embed_query; use the real one on a broken provider call path.
    async def real_query(question):
        return (await embeddings.embed_texts([question]))[0]

    async def batch(b):
        return [[1.0] + [0.0] * (DIMS - 1) for _ in b]

    monkeypatch.setattr(chat, "embed_query", real_query)
    monkeypatch.setattr(embeddings, "_embed_batch", batch)
    fake_redis.down = True

    async def go():
        return await chat.retrieve_sources("anything", None, owner_user_id=DEFAULT_USER_ID)

    assert run(go()) == []  # no documents: an empty search, not an error


# --- Summaries --------------------------------------------------------------------------------


def test_the_same_text_is_summarized_once_per_user(fake_redis, summary):
    first, second = new_job(), new_job()
    ingestion.process_job(first)
    ingestion.process_job(second)
    assert summary.calls == 1  # the second job reused the summary
    with database.get_connection() as connection:
        rows = connection.execute(text("SELECT summary FROM documents")).scalars().all()
    assert rows == ["A fake summary.", "A fake summary."]
    assert cache.stats["summary.hit"] == 1


def test_another_users_same_text_is_summarized_again(fake_redis, summary):
    with database.get_connection() as connection:
        other = connection.execute(text(
            "INSERT INTO users (email, password_hash) VALUES ('o@example.com', 'x') RETURNING id"
        )).scalar_one()
    ingestion.process_job(new_job())
    ingestion.process_job(new_job(owner=other))
    assert summary.calls == 2
    assert {key.split(":")[3] for key in fake_redis.data if "summary" in key} == {
        str(DEFAULT_USER_ID), str(other)}


def test_different_text_is_summarized_again(fake_redis, summary):
    ingestion.process_job(new_job(content=b"First document text. " * 10))
    ingestion.process_job(new_job(content=b"Second document text. " * 10))
    assert summary.calls == 2


def test_a_failed_summary_is_never_cached(fake_redis, monkeypatch):
    async def broken(text):
        raise ingestion.AIServiceError("bad")
        yield  # pragma: no cover

    monkeypatch.setattr(ingestion, "stream_summary", broken)
    ingestion.process_job(new_job())
    assert [k for k in fake_redis.data if "summary" in k] == []


def test_a_job_still_completes_when_redis_is_down(fake_redis, summary):
    fake_redis.down = True
    job_id = new_job()
    assert ingestion.process_job(job_id) is None
    assert count("documents") == 1 and summary.calls == 1


def test_deleting_a_document_deletes_its_cached_summary(fake_redis, summary):
    job_id = new_job()
    ingestion.process_job(job_id)
    assert [k for k in fake_redis.data if "summary" in k]
    with TestClient(main.app) as client:
        document_id = client.get(f"/jobs/{job_id}").json()["document_id"]
        assert client.delete(f"/documents/{document_id}").status_code == 200
    assert [k for k in fake_redis.data if "summary" in k] == []
    ingestion.process_job(new_job())
    assert summary.calls == 2  # a new upload of the same text is summarized again


def test_deleting_a_document_works_when_redis_is_down(fake_redis, summary):
    job_id = new_job()
    ingestion.process_job(job_id)
    fake_redis.down = True
    with TestClient(main.app) as client:
        document_id = client.get(f"/jobs/{job_id}").json()["document_id"]
        assert client.delete(f"/documents/{document_id}").status_code == 200
    assert count("documents") == 0


def test_deleting_another_users_document_does_not_touch_my_cache(fake_redis, summary):
    ingestion.process_job(new_job())
    with TestClient(main.app) as client:
        assert client.delete("/documents/99999").status_code == 404
    assert [k for k in fake_redis.data if "summary" in k]
