import asyncio
import logging
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

import app.services.ai_calls as ai_calls
import app.services.embeddings as embeddings
import app.services.summarizer as summarizer
from app.services.summarizer import AIRateLimitError, AIServiceError, AIServiceNotConfiguredError

DIMENSIONS = embeddings.EMBEDDING_DIMENSIONS
REQUEST = httpx.Request("POST", "https://openrouter.ai/api/v1/embeddings")


def vector_for(text: str, size: int = DIMENSIONS) -> list[float]:
    # A recognizable vector: the first value encodes which text it belongs to.
    return [float(int(text.split()[-1]))] + [0.0] * (size - 1)


class FakeEmbeddings:
    """Stands in for client.embeddings; returns data in reverse order, like a shuffled API."""

    def __init__(self):
        self.requests = []
        self.error = None
        self.size = DIMENSIONS
        self.drop_one = False
        self.cost = 0.00002

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        if self.error:
            raise self.error
        texts = kwargs["input"]
        data = [
            SimpleNamespace(index=i, embedding=vector_for(text, self.size))
            for i, text in enumerate(texts)
        ]
        if self.drop_one:
            data = data[:-1]
        tokens = 5 * len(texts)
        return SimpleNamespace(
            data=list(reversed(data)),
            usage=SimpleNamespace(prompt_tokens=tokens, total_tokens=tokens, cost=self.cost),
        )


@pytest.fixture
def fake(monkeypatch):
    fake_embeddings = FakeEmbeddings()
    client = SimpleNamespace(embeddings=fake_embeddings)
    monkeypatch.setattr(embeddings, "_get_async_client", lambda: client)
    return fake_embeddings


@pytest.fixture
def usage_logs(caplog):
    caplog.set_level(logging.INFO, logger="app.services.usage")
    return lambda: [r.ai_usage for r in caplog.records if hasattr(r, "ai_usage")]


def texts(count):
    return [f"chunk number {i}" for i in range(count)]


def test_texts_are_sent_in_batches_of_64_and_order_is_kept(fake):
    vectors = asyncio.run(embeddings.embed_texts(texts(130)))

    assert [len(r["input"]) for r in fake.requests] == [64, 64, 2]
    assert [v[0] for v in vectors] == [float(i) for i in range(130)]
    assert all(len(v) == DIMENSIONS for v in vectors)


def test_request_uses_the_embedding_model_and_float_vectors(fake):
    asyncio.run(embeddings.embed_texts(["hello 1"]))

    assert fake.requests == [
        {"model": embeddings.EMBEDDING_MODEL, "input": ["hello 1"], "encoding_format": "float"}
    ]


def test_embed_query_returns_one_vector(fake):
    vector = asyncio.run(embeddings.embed_query("question 7"))

    assert vector[0] == 7.0 and len(vector) == DIMENSIONS


def test_no_texts_means_no_request(fake):
    assert asyncio.run(embeddings.embed_texts([])) == []
    assert fake.requests == []


@pytest.mark.parametrize("bad", ["", "   \n"])
def test_empty_text_is_rejected_before_any_request(fake, bad):
    with pytest.raises(ValueError, match="empty text"):
        asyncio.run(embeddings.embed_texts(["fine 1", bad]))
    assert fake.requests == []


@pytest.mark.parametrize(
    ("provider_error", "expected", "message"),
    [
        (APITimeoutError(request=REQUEST), TimeoutError, "took too long"),
        (APIConnectionError(request=REQUEST), ConnectionError, "Could not connect"),
        (
            RateLimitError("limited", response=httpx.Response(429, request=REQUEST), body=None),
            AIRateLimitError,
            "rate limit",
        ),
        (
            APIStatusError("bad", response=httpx.Response(402, request=REQUEST), body=None),
            AIServiceError,
            "API error: 402",
        ),
    ],
    ids=["timeout", "connection", "rate-limit", "status"],
)
def test_provider_errors_are_mapped_like_the_summarizer(fake, usage_logs, provider_error, expected, message):
    fake.error = provider_error

    with pytest.raises(expected, match=message):
        asyncio.run(embeddings.embed_texts(["text 1"]))

    [logged] = usage_logs()
    assert logged["kind"] == "embedding"
    assert logged["outcome"] == "error"


def test_wrong_vector_size_raises_and_is_logged(fake, usage_logs, caplog):
    fake.size = DIMENSIONS - 1

    with pytest.raises(embeddings.EmbeddingDimensionError, match=f"Expected {DIMENSIONS}"):
        asyncio.run(embeddings.embed_texts(["text 1"]))

    assert issubclass(embeddings.EmbeddingDimensionError, AIServiceError)  # mapped to 502
    assert "Embedding request failed" in caplog.text
    assert usage_logs()[0]["outcome"] == "error"


def test_missing_vectors_raise(fake):
    fake.drop_one = True

    with pytest.raises(AIServiceError, match="returned 1 embeddings for 2 texts"):
        asyncio.run(embeddings.embed_texts(["text 1", "text 2"]))


def test_each_batch_is_usage_logged(fake, usage_logs):
    asyncio.run(embeddings.embed_texts(texts(70)))

    logged = usage_logs()
    assert len(logged) == 2
    first = {k: v for k, v in logged[0].items() if k != "duration_ms"}
    assert first == {
        "kind": "embedding",
        "model": embeddings.EMBEDDING_MODEL,
        "outcome": "ok",
        "prompt_tokens": 320,
        "completion_tokens": 0,
        "total_tokens": 320,
        "cost_usd": 0.00002,
        "cost_source": "openrouter",
    }
    assert logged[1]["prompt_tokens"] == 30


def test_missing_api_key_gives_not_configured_error(monkeypatch):
    def fail_if_client_created(*args, **kwargs):
        raise AssertionError("An AI client must not be created without an API key.")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(ai_calls, "AsyncOpenAI", fail_if_client_created)
    ai_calls._get_async_client.cache_clear()
    try:
        with pytest.raises(AIServiceNotConfiguredError):
            asyncio.run(embeddings.embed_texts(["text 1"]))
    finally:
        ai_calls._get_async_client.cache_clear()
