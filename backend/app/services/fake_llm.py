"""Fake LLM mode (`FAKE_LLM=true`): the AI provider is replaced by a local stand-in, so load tests
measure the system (API, queue, worker, database, Redis) without spending any money.

`ai_calls._get_async_client()` returns `FakeClient` instead of the real OpenAI client, so
everything above it is the real code: retries, usage logging, `ai_usage` rows, the summary
stream, chunk embedding, the chat answer. The fake only answers like a provider would:

- **Embeddings**: a deterministic vector per text (the same text always gives the same vector),
  after a delay; usage with input tokens.
- **Summary / chat streams**: text in small pieces with a delay before the first token and
  between tokens, then a last chunk with token usage (cost 0). The summary starts with
  FAKE_MARKER, so a test can tell it is not a real summary; the chat answer cites `[1]`.
- **Plain completions** (query rewrite, judge): a short fixed text.

Settings: `FAKE_LLM` (true/false, default false) and `FAKE_LLM_DELAY_SCALE` (default 1; 0 means no
waiting, which the unit tests use; 2 means twice as slow). The default delays are about what a
small hosted model takes: embeddings 0.25 s, first summary token 0.8 s then 30 ms per piece,
first chat token 0.5 s then 30 ms per piece, each ±20 %.

Safety: the app refuses to start in fake mode on Vercel (`VERCEL` is set), so a deployment can
never answer with fake summaries; fake mode never reads or sends an API key; `/health` says
`"fake_llm": true` (only then), and the load test refuses to run against a backend that doesn't.
"""

import asyncio
import hashlib
import logging
import os
import random
from types import SimpleNamespace

logger = logging.getLogger(__name__)

FAKE_MARKER = "[FAKE LLM]"
SUMMARY_PIECES = 40
ANSWER_PIECES = 25
PIECE_SECONDS = 0.03
EMBEDDING_SECONDS = 0.25
SUMMARY_FIRST_TOKEN_SECONDS = 0.8
CHAT_FIRST_TOKEN_SECONDS = 0.5

_FALSE = {"", "false", "0", "off", "no"}
_TRUE = {"true", "1", "on", "yes"}


def fake_llm_enabled() -> bool:
    """FAKE_LLM. A value that is neither true nor false is an error, not a guess."""
    raw = os.getenv("FAKE_LLM", "").strip().lower()
    if raw in _FALSE:
        return False
    if raw in _TRUE:
        return True
    raise ValueError(f"FAKE_LLM must be true or false, got {raw!r}.")


def delay_scale() -> float:
    raw = os.getenv("FAKE_LLM_DELAY_SCALE", "").strip()
    if not raw:
        return 1.0
    try:
        scale = float(raw)
    except ValueError:
        raise ValueError(f"FAKE_LLM_DELAY_SCALE must be a number, got {raw!r}.")
    if scale < 0:
        raise ValueError(f"FAKE_LLM_DELAY_SCALE must not be negative, got {scale}.")
    return scale


def check_fake_llm_settings() -> None:
    """At startup (API and worker): a bad setting stops the process; fake mode on Vercel is
    refused; fake mode is announced in the log."""
    delay_scale()
    if not fake_llm_enabled():
        return
    if os.getenv("VERCEL"):
        raise RuntimeError("FAKE_LLM is on, but this is a Vercel deployment: refusing to start.")
    logger.warning("FAKE_LLM is on: no real AI calls are made; summaries and answers are fake.")


async def _wait(seconds: float) -> None:
    scale = delay_scale()
    if scale > 0 and seconds > 0:
        await asyncio.sleep(seconds * scale * random.uniform(0.8, 1.2))


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def fake_vector(text: str, dimensions: int) -> list[float]:
    """The same text always gives the same unit-length vector."""
    seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    rng = random.Random(seed)
    values = [rng.gauss(0, 1) for _ in range(dimensions)]
    norm = sum(v * v for v in values) ** 0.5 or 1.0
    return [v / norm for v in values]


def _prompt_text(messages: list[dict]) -> str:
    return " ".join(str(m.get("content", "")) for m in messages)


def _chunk(model: str, content: str | None = None, usage=None):
    return SimpleNamespace(
        model=model,
        choices=[SimpleNamespace(delta=SimpleNamespace(content=content), finish_reason=None)]
        if usage is None else [],
        usage=usage,
    )


class FakeStream:
    """What `client.chat.completions.create(stream=True)` returns: async-iterable, closable."""

    def __init__(self, model: str, pieces: list[str], first_token_seconds: float, usage):
        self._model = model
        self._pieces = pieces
        self._first = first_token_seconds
        self._usage = usage
        self.closed = False

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        await _wait(self._first)
        for index, piece in enumerate(self._pieces):
            if self.closed:
                return
            if index:
                await _wait(PIECE_SECONDS)
            yield _chunk(self._model, piece)
        yield _chunk(self._model, usage=self._usage)

    async def close(self):
        self.closed = True


def _usage(prompt: str, completion: str):
    prompt_tokens, completion_tokens = _tokens(prompt), _tokens(completion)
    return SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                           total_tokens=prompt_tokens + completion_tokens, cost=0.0)


def _pieces(text: str, count: int) -> list[str]:
    words = text.split(" ")
    size = max(1, -(-len(words) // count))
    return [" ".join(words[i:i + size]) + (" " if i + size < len(words) else "")
            for i in range(0, len(words), size)]


class _Completions:
    async def create(self, *, model, messages, stream=False, **kwargs):
        prompt = _prompt_text(messages)
        system = str(messages[0].get("content", "")) if messages else ""
        if system.startswith("You are a professional document summarizer"):
            kind, text, first = "summary", (
                f"{FAKE_MARKER} Summary of a {len(prompt)}-character document. " + (
                    "It covers the main points, details and conclusions of the text. " * 8)
            ).strip(), SUMMARY_FIRST_TOKEN_SECONDS
            count = SUMMARY_PIECES
        else:
            kind, text, first = "chat", (
                f"{FAKE_MARKER} According to the documents, the answer is in the sources [1]. " + (
                    "This is a made-up answer used for load testing. " * 4)
            ).strip(), CHAT_FIRST_TOKEN_SECONDS
            count = ANSWER_PIECES
        if stream:
            return FakeStream(model, _pieces(text, count), first, _usage(prompt, text))
        await _wait(first)
        return SimpleNamespace(
            model=model, usage=_usage(prompt, text),
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        )


class _Embeddings:
    async def create(self, *, model, input, encoding_format="float", **kwargs):
        from app.services.embeddings import EMBEDDING_DIMENSIONS

        texts = list(input)
        await _wait(EMBEDDING_SECONDS + 0.002 * len(texts))
        tokens = sum(_tokens(t) for t in texts)
        return SimpleNamespace(
            model=model,
            data=[SimpleNamespace(index=i, embedding=fake_vector(t, EMBEDDING_DIMENSIONS))
                  for i, t in enumerate(texts)],
            usage=SimpleNamespace(prompt_tokens=tokens, total_tokens=tokens, cost=0.0),
        )


class FakeClient:
    """Stands in for AsyncOpenAI: no network, no key, usable from any event loop."""

    def __init__(self):
        self.chat = SimpleNamespace(completions=_Completions())
        self.embeddings = _Embeddings()
