import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError

import app.services.summarizer as summarizer

SUMMARY_SYSTEM_PROMPT = (
    "You are a professional document summarizer. "
    "Summarize the provided document clearly and accurately. "
    "Preserve its key ideas, important details, and conclusions. "
    "Do not add information that is not present in the document. "
    "Use the same language as the original document."
)


def chunk(content, usage=None):
    return SimpleNamespace(
        model=summarizer.MODEL,
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content), finish_reason=None)],
        usage=usage,
    )


def usage(cost=None):
    # Shape of OpenRouter's usage object on the last streamed chunk.
    return SimpleNamespace(prompt_tokens=120, completion_tokens=40, total_tokens=160, cost=cost)


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


@pytest.fixture
def fake_openrouter(monkeypatch):
    """Replace the OpenRouter client; returns (set_stream, requests)."""
    requests = []
    holder = {}

    async def create(**kwargs):
        requests.append(kwargs)
        return holder["stream"]

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(summarizer, "_get_async_client", lambda: client)
    monkeypatch.delenv("AI_PRICES_PER_MILLION_TOKENS", raising=False)

    def set_stream(stream):
        holder["stream"] = stream
        return stream

    return set_stream, requests


@pytest.fixture
def usage_logs(caplog):
    caplog.set_level(logging.INFO, logger="app.services.usage")

    def records():
        return [r.ai_usage for r in caplog.records if hasattr(r, "ai_usage")]

    return records


async def collect(text="A document."):
    return [part async for part in summarizer.stream_summary(text)]


def test_summary_logs_tokens_and_cost_from_openrouter(fake_openrouter, usage_logs, caplog):
    set_stream, _ = fake_openrouter
    stream = set_stream(FakeStream([chunk("Hello"), chunk(" world"), chunk("", usage(cost=0.0007))]))

    parts = asyncio.run(collect())

    assert parts == ["Hello", " world"]
    assert stream.closed
    [logged] = usage_logs()
    assert {k: v for k, v in logged.items() if k != "duration_ms"} == {
        "kind": "summary",
        "model": summarizer.MODEL,
        "outcome": "ok",
        "prompt_tokens": 120,
        "completion_tokens": 40,
        "total_tokens": 160,
        "cost_usd": 0.0007,
        "cost_source": "openrouter",
    }
    assert isinstance(logged["duration_ms"], int)
    assert "ai_usage kind=summary" in caplog.text
    assert "cost_usd=0.0007 cost_source=openrouter" in caplog.text


def test_cost_is_estimated_from_prices_when_openrouter_sends_none(
    fake_openrouter, usage_logs, monkeypatch
):
    set_stream, _ = fake_openrouter
    set_stream(FakeStream([chunk("Hi"), chunk("", usage(cost=None))]))
    monkeypatch.setenv(
        "AI_PRICES_PER_MILLION_TOKENS",
        json.dumps({summarizer.MODEL: {"prompt": 2.5, "completion": 10}}),
    )

    asyncio.run(collect())

    [logged] = usage_logs()
    # 120 * 2.5 / 1M + 40 * 10 / 1M
    assert logged["cost_usd"] == pytest.approx(0.0007)
    assert logged["cost_source"] == "estimate"


def test_cost_is_unknown_without_cost_or_prices(fake_openrouter, usage_logs, caplog):
    set_stream, _ = fake_openrouter
    set_stream(FakeStream([chunk("Hi"), chunk("", usage(cost=None))]))

    asyncio.run(collect())

    [logged] = usage_logs()
    assert logged["cost_usd"] is None
    assert logged["cost_source"] == "unknown"
    assert "cost_usd=unknown" in caplog.text


def test_invalid_prices_are_logged_and_ignored(fake_openrouter, usage_logs, monkeypatch, caplog):
    set_stream, _ = fake_openrouter
    set_stream(FakeStream([chunk("Hi"), chunk("", usage(cost=None))]))
    monkeypatch.setenv("AI_PRICES_PER_MILLION_TOKENS", "not json")

    asyncio.run(collect())

    [logged] = usage_logs()
    assert logged["cost_source"] == "unknown"
    assert "AI_PRICES_PER_MILLION_TOKENS is not valid JSON" in caplog.text


def test_cancelled_summary_is_logged_without_usage(fake_openrouter, usage_logs):
    set_stream, _ = fake_openrouter
    stream = set_stream(
        FakeStream([chunk("Partial"), chunk(" more"), chunk("", usage(cost=0.0007))])
    )

    async def cancel_after_first_part():
        generator = summarizer.stream_summary("A document.")
        first = await anext(generator)
        await generator.aclose()
        return first

    assert asyncio.run(cancel_after_first_part()) == "Partial"
    assert stream.closed
    [logged] = usage_logs()
    assert logged["outcome"] == "cancelled"
    assert logged["prompt_tokens"] is None
    assert logged["cost_source"] == "unknown"


def test_failed_summary_is_logged_and_error_still_mapped(fake_openrouter, usage_logs):
    set_stream, _ = fake_openrouter
    error = APIConnectionError(request=httpx.Request("POST", "https://openrouter.ai/api/v1"))
    set_stream(FakeStream([chunk("Partial")], error=error))

    with pytest.raises(ConnectionError, match="Could not connect to the AI service."):
        asyncio.run(collect())

    [logged] = usage_logs()
    assert logged["outcome"] == "error"
    assert logged["completion_tokens"] is None


def test_summary_request_and_prompt_are_unchanged(fake_openrouter, usage_logs):
    set_stream, requests = fake_openrouter
    set_stream(FakeStream([chunk("Hi"), chunk("", usage(cost=0.0001))]))

    asyncio.run(collect("The document text."))

    # No deprecated usage flags are sent; OpenRouter always returns usage.
    assert requests == [
        {
            "model": summarizer.MODEL,
            "messages": [
                {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                {"role": "user", "content": "Summarize this document:\n\nThe document text."},
            ],
            "max_tokens": 1000,
            "stream": True,
        }
    ]
