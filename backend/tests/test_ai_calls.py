"""The shared AI call wrapper (services/ai_calls.py): error mapping, retries, Retry-After,
and the no-retry-after-the-first-token rule for streams. All provider calls are fakes."""

import asyncio
import email.utils
import logging
import time
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

import app.services.ai_calls as ai_calls
import app.services.chat as chat
import app.services.embeddings as embeddings
import app.services.llm as llm
import app.services.summarizer as summarizer
from app.services.ai_calls import AIRateLimitError, AIServiceError

REQUEST = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")


def status_error(code: int, headers: dict | None = None) -> APIStatusError:
    response = httpx.Response(code, request=REQUEST, headers=headers or {})
    cls = RateLimitError if code == 429 else APIStatusError
    return cls(f"status {code}", response=response, body=None)


def timeout_error():
    return APITimeoutError(request=REQUEST)


def connection_error():
    return APIConnectionError(request=REQUEST)


class Calls:
    """A fake SDK request: raises the queued errors in order, then returns `result`."""

    def __init__(self, errors, result="ok"):
        self.errors = list(errors)
        self.result = result
        self.count = 0

    async def __call__(self):
        self.count += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


def run(make_call):
    return asyncio.run(ai_calls.call_with_retries(make_call, kind="test", model="m"))


@pytest.fixture
def retry_logs(caplog):
    caplog.set_level(logging.WARNING, logger="app.services.ai_calls")
    return lambda: [r.getMessage() for r in caplog.records if r.getMessage().startswith("ai_retry")]


# --- The client ---------------------------------------------------------------------


def test_client_turns_off_the_sdks_own_retries(monkeypatch):
    created = {}

    def fake_client(**kwargs):
        created.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(ai_calls, "AsyncOpenAI", fake_client)
    ai_calls._get_async_client.cache_clear()
    try:
        ai_calls._get_async_client()
    finally:
        ai_calls._get_async_client.cache_clear()

    assert created["max_retries"] == 0
    assert created["base_url"] == "https://openrouter.ai/api/v1"
    assert created["timeout"] == 60.0


def test_services_share_one_client_and_error_types():
    for module in (summarizer, chat, embeddings, llm):
        assert module._get_async_client is ai_calls._get_async_client
    assert summarizer.AIRateLimitError is ai_calls.AIRateLimitError
    assert summarizer.AIServiceError is ai_calls.AIServiceError
    assert summarizer.AIServiceNotConfiguredError is ai_calls.AIServiceNotConfiguredError


# --- Error mapping ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected", "message"),
    [
        (timeout_error(), TimeoutError, "took too long"),
        (connection_error(), ConnectionError, "Could not connect"),
        (status_error(429), AIRateLimitError, "rate limit has been exceeded"),
        (status_error(500), AIServiceError, "API error: 500"),
        (status_error(402), AIServiceError, "API error: 402"),
    ],
    ids=["timeout-504", "connection-503", "rate-limit-429", "server-502", "client-502"],
)
def test_provider_errors_are_mapped(error, expected, message):
    mapped = ai_calls.map_provider_error(error)
    assert isinstance(mapped, expected)
    assert message in str(mapped)


def test_rate_limit_error_keeps_retry_after_and_detail():
    mapped = ai_calls.map_provider_error(status_error(429, {"retry-after": "7"}))
    assert mapped.retry_after == 7.0
    assert "status 429" in mapped.detail


# --- Retries --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [timeout_error(), connection_error(), status_error(429), status_error(500), status_error(503)],
    ids=["timeout", "connection", "429", "500", "503"],
)
def test_transient_errors_are_retried_then_succeed(error, retry_waits, retry_logs):
    calls = Calls([error, error])

    assert run(calls) == "ok"
    assert calls.count == 3
    assert len(retry_waits) == 2
    logs = retry_logs()
    assert len(logs) == 2
    assert "kind=test model=m retry=1/2" in logs[0]
    assert "retry=2/2" in logs[1]


def test_gives_up_after_two_retries_with_the_mapped_error(retry_waits, retry_logs):
    calls = Calls([timeout_error()] * 3)

    with pytest.raises(TimeoutError, match="took too long"):
        run(calls)
    assert calls.count == 3
    assert len(retry_waits) == 2
    assert len(retry_logs()) == 2


@pytest.mark.parametrize("code", [400, 401, 402, 404, 413, 422])
def test_client_errors_are_never_retried(code, retry_waits, retry_logs):
    calls = Calls([status_error(code)])

    with pytest.raises(AIServiceError, match=f"API error: {code}"):
        run(calls)
    assert calls.count == 1
    assert retry_waits == []
    assert retry_logs() == []


def test_a_client_error_after_a_transient_one_stops_the_retries(retry_waits):
    calls = Calls([status_error(503), status_error(400)])

    with pytest.raises(AIServiceError, match="API error: 400"):
        run(calls)
    assert calls.count == 2
    assert len(retry_waits) == 1


def test_other_exceptions_pass_through_without_retry(retry_waits):
    calls = Calls([ValueError("bug")])

    with pytest.raises(ValueError, match="bug"):
        run(calls)
    assert calls.count == 1
    assert retry_waits == []


def test_backoff_grows_with_jitter(retry_waits):
    run(Calls([status_error(500), status_error(500)]))

    first, second = retry_waits
    assert 0.375 <= first <= 0.5
    assert 0.75 <= second <= 1.0


# --- Retry-After ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "3"}, 3.0),
        ({"retry-after-ms": "1500"}, 1.5),
        ({"retry-after": "0.25"}, 0.25),
    ],
    ids=["seconds", "milliseconds", "fraction"],
)
def test_retry_after_is_honored(headers, expected, retry_waits):
    assert run(Calls([status_error(429, headers)])) == "ok"
    assert retry_waits == [expected]


def test_retry_after_as_an_http_date_is_honored(retry_waits):
    when = email.utils.formatdate(time.time() + 5, usegmt=True)
    run(Calls([status_error(503, {"retry-after": when})]))

    [wait] = retry_waits
    assert 3 <= wait <= 5


def test_openrouter_reset_time_is_honored(retry_waits):
    reset_ms = str(int((time.time() + 4) * 1000))
    run(Calls([status_error(429, {"x-ratelimit-reset": reset_ms})]))

    [wait] = retry_waits
    assert 2.5 <= wait <= 4


def test_a_long_retry_after_is_not_waited_for(retry_waits, retry_logs):
    calls = Calls([status_error(429, {"retry-after": "3600"})])

    with pytest.raises(AIRateLimitError) as raised:
        run(calls)
    assert raised.value.retry_after == 3600.0  # the caller (e.g. the eval runner) decides
    assert calls.count == 1
    assert retry_waits == []
    assert retry_logs() == []


# --- Streams ----------------------------------------------------------------------------


def text_chunk(content):
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=content))],
                           usage=None)


def role_chunk():
    # The first chunk of a stream often has a role but no text yet.
    return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))],
                           usage=None)


class FakeStream:
    def __init__(self, chunks, error=None):
        self.chunks = chunks
        self.error = error
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for item in self.chunks:
            yield item
        if self.error:
            raise self.error

    async def close(self):
        self.closed = True


class Opens:
    """A fake streamed request: each attempt takes the next item, an error or a stream."""

    def __init__(self, attempts):
        self.attempts = list(attempts)
        self.count = 0
        self.streams = []

    async def __call__(self):
        self.count += 1
        attempt = self.attempts.pop(0)
        if isinstance(attempt, Exception):
            raise attempt
        self.streams.append(attempt)
        return attempt


def texts_from(open_stream):
    async def collect():
        stream = ai_calls.stream_with_retries(open_stream, kind="test", model="m")
        return [chunk.choices[0].delta.content for chunk in [c async for c in stream]
                if chunk.choices[0].delta.content]
    return asyncio.run(collect())


def test_stream_is_retried_when_opening_fails(retry_waits, retry_logs):
    opens = Opens([status_error(503), FakeStream([text_chunk("Hello")])])

    assert texts_from(opens) == ["Hello"]
    assert opens.count == 2
    assert len(retry_logs()) == 1


def test_stream_is_retried_when_it_fails_before_the_first_token(retry_waits):
    broken = FakeStream([role_chunk()], error=connection_error())
    good = FakeStream([role_chunk(), text_chunk("Hello"), text_chunk(" world")])
    opens = Opens([broken, good])

    assert texts_from(opens) == ["Hello", " world"]
    assert opens.count == 2
    assert broken.closed and good.closed


def test_stream_is_never_retried_after_the_first_token(retry_waits, retry_logs):
    stream = FakeStream([text_chunk("Partial")], error=connection_error())
    opens = Opens([stream, FakeStream([text_chunk("never used")])])

    with pytest.raises(ConnectionError, match="Could not connect"):
        texts_from(opens)
    assert opens.count == 1
    assert stream.closed
    assert retry_waits == []
    assert retry_logs() == []


def test_stream_gives_up_after_two_retries(retry_waits):
    opens = Opens([timeout_error()] * 3)

    with pytest.raises(TimeoutError):
        texts_from(opens)
    assert opens.count == 3


def test_stream_client_errors_are_not_retried(retry_waits):
    opens = Opens([status_error(400)])

    with pytest.raises(AIServiceError, match="API error: 400"):
        texts_from(opens)
    assert opens.count == 1


# --- Through the services -------------------------------------------------------------


def streaming_client(opens):
    async def create(**kwargs):
        return await opens()

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


def usage_lines(caplog):
    return [r.ai_usage for r in caplog.records if hasattr(r, "ai_usage")]


def test_summary_retries_before_its_first_token_and_logs_one_usage_line(
    monkeypatch, caplog, retry_waits
):
    caplog.set_level(logging.INFO, logger="app.services.usage")
    opens = Opens([status_error(502), FakeStream([text_chunk("A summary.")])])
    monkeypatch.setattr(summarizer, "_get_async_client", lambda: streaming_client(opens))

    async def collect():
        return [part async for part in summarizer.stream_summary("A document.")]

    assert asyncio.run(collect()) == ["A summary."]
    assert opens.count == 2
    [line] = usage_lines(caplog)
    assert line["kind"] == "summary" and line["outcome"] == "ok"


def test_chat_answer_is_not_retried_after_it_started(monkeypatch, retry_waits):
    stream = FakeStream([text_chunk("Partial")], error=timeout_error())
    opens = Opens([stream])
    monkeypatch.setattr(chat, "_get_async_client", lambda: streaming_client(opens))
    source = chat.Source(n=1, chunk_id=1, document_id=1, filename="a.txt", page=None,
                         content="Text.", similarity=0.9)
    prepared = chat.PreparedAnswer(question="Q?", sources=[source],
                                   messages=chat.build_messages("Q?", [source]))
    parts = []

    async def collect():
        async for part in chat.stream_answer(prepared):
            parts.append(part)

    with pytest.raises(TimeoutError):
        asyncio.run(collect())
    assert parts == ["Partial"]
    assert opens.count == 1
    assert prepared.usage["outcome"] == "error"


def test_embeddings_are_retried(monkeypatch, caplog, retry_waits):
    caplog.set_level(logging.INFO, logger="app.services.usage")
    size = embeddings.EMBEDDING_DIMENSIONS
    calls = Calls(
        [status_error(429, {"retry-after": "1"})],
        result=SimpleNamespace(
            data=[SimpleNamespace(index=0, embedding=[0.5] * size)],
            usage=SimpleNamespace(prompt_tokens=3, total_tokens=3, cost=0.0),
        ),
    )

    async def create(**kwargs):
        return await calls()

    monkeypatch.setattr(embeddings, "_get_async_client",
                        lambda: SimpleNamespace(embeddings=SimpleNamespace(create=create)))

    assert asyncio.run(embeddings.embed_texts(["text"])) == [[0.5] * size]
    assert calls.count == 2
    assert retry_waits == [1.0]
    [line] = usage_lines(caplog)
    assert line["kind"] == "embedding" and line["outcome"] == "ok"


def test_plain_completions_are_retried(monkeypatch, retry_waits):
    reply = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Done."))], usage=None
    )
    calls = Calls([connection_error()], result=reply)

    async def create(**kwargs):
        return await calls()

    monkeypatch.setattr(llm, "_get_async_client", lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))

    text = asyncio.run(llm.complete(model="m", messages=[], kind="judge"))
    assert text == "Done."
    assert calls.count == 2
