"""The one place for AI provider calls: the client, error mapping and retries.

The provider is OpenRouter by default. AI_BASE_URL and AI_API_KEY point the one client at
another OpenAI-compatible API instead; the model names stay in their own settings.

Every service (summary, chat, embeddings, plain completions) creates its request through
the client from `_get_async_client()` and sends it through `call_with_retries` or
`stream_with_retries`:

- The SDK's own retries are off (`max_retries=0`); all retries happen here and are logged.
- Transient errors are retried up to MAX_RETRIES times with exponential backoff and
  jitter: timeouts (including 408 Request Timeout), connection errors, 429 and 5xx.
  Other 4xx errors (bad input, no credits, auth, 409) are never retried.
- A provider's suggested wait (`Retry-After`) is honored. If it is longer than
  MAX_RETRY_AFTER_SECONDS, the call is not retried and the rate-limit error is raised
  with `retry_after`, so the caller can decide (the eval runner waits; chat fails).
- A streamed call is retried only if it fails before its first token. Once text has been
  passed on, a failure is raised as it is: the user has already seen part of the answer.
- Provider errors are mapped to: TimeoutError (504), ConnectionError (503),
  AIRateLimitError (429), AIServiceError (502). A missing key is
  AIServiceNotConfiguredError (503).
"""

import asyncio
import email.utils
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable

from dotenv import load_dotenv
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncOpenAI,
    RateLimitError,
)

# backend/.env, wherever the process was started from.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
REQUEST_TIMEOUT_SECONDS = 60.0

# Same limits as the SDK's defaults, which these retries replace.
MAX_RETRIES = 2
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 8.0
MAX_RETRY_AFTER_SECONDS = 60.0

# The provider errors this module handles (APITimeoutError is an APIConnectionError).
PROVIDER_ERRORS = (APIConnectionError, APIStatusError)


class AIRateLimitError(Exception):
    """Raised when the AI provider rate limit is exceeded.

    `retry_after` is the provider's suggested wait in seconds, if it sent one; `detail` is
    the provider's own message (for example, which limit was hit).
    """

    def __init__(self, message: str, *, retry_after: float | None = None, detail: str = ""):
        super().__init__(message)
        self.retry_after = retry_after
        self.detail = detail


class AIServiceError(Exception):
    """Raised when the AI provider returns an API error."""


class AIOutOfCreditError(AIServiceError):
    """The provider refused the call because the account has no credit left (HTTP 402).

    A subclass of AIServiceError, so code that handles provider errors in general still
    does; the API endpoints answer it with 503 and a clear message instead of 502.
    """


OUT_OF_CREDIT_MESSAGE = "The AI service is out of credit. Please try again later."


class AIServiceNotConfiguredError(Exception):
    """Raised when no API key is set for the AI provider."""


def ai_base_url() -> str:
    """AI_BASE_URL: any OpenAI-compatible API. Empty means OpenRouter."""
    return os.getenv("AI_BASE_URL", "").strip().rstrip("/") or OPENROUTER_BASE_URL


def _require_api_key() -> str:
    """AI_API_KEY, or OPENROUTER_API_KEY when the provider is OpenRouter.

    The OpenRouter key is never used for another provider, so it is never sent to a
    different URL by mistake.
    """
    api_key = os.getenv("AI_API_KEY", "").strip()
    if not api_key and ai_base_url() == OPENROUTER_BASE_URL:
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        if ai_base_url() == OPENROUTER_BASE_URL:
            logger.error("OPENROUTER_API_KEY (or AI_API_KEY) is not set; cannot call the AI service.")
        else:
            logger.error("AI_BASE_URL is set but AI_API_KEY is not; cannot call the AI service.")
        raise AIServiceNotConfiguredError("AI service is not configured.")
    return api_key


# The client is created on first use so importing this module (e.g. in tests)
# does not require an API key. A failed key check is not cached.
#
# An AsyncOpenAI client is tied to the event loop it was first used in. The API has one loop
# for its whole life, but a Celery task runs each job in a new loop (`asyncio.run`), where a
# client from an earlier loop fails with "Event loop is closed". So the cached client is
# reused only while the same loop is running, and replaced when the loop changes.
_client_cache: dict[str, Any] = {}


def _get_async_client() -> AsyncOpenAI:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None  # created outside any loop (tests): it is not reused by a later loop
    if "client" not in _client_cache or _client_cache["loop"] is not loop:
        _client_cache["client"] = AsyncOpenAI(
            base_url=ai_base_url(),
            api_key=_require_api_key(),
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,  # retries happen in this module, where they are logged
        )
        _client_cache["loop"] = loop
    return _client_cache["client"]


_get_async_client.cache_clear = _client_cache.clear  # type: ignore[attr-defined]


def retry_after_seconds(error: Exception) -> float | None:
    """The provider's suggested wait in seconds, from the error's response headers."""
    response = getattr(error, "response", None)
    headers = response.headers if response is not None else {}
    try:
        if headers.get("retry-after-ms"):
            return max(0.0, float(headers["retry-after-ms"]) / 1000)
        if headers.get("retry-after"):
            value = headers["retry-after"]
            try:
                return max(0.0, float(value))
            except ValueError:
                # An HTTP date instead of a number of seconds.
                when = email.utils.parsedate_to_datetime(value)
                return max(0.0, when.timestamp() - time.time())
        if headers.get("x-ratelimit-reset"):
            # Epoch milliseconds when the limit resets (OpenRouter).
            return max(0.0, float(headers["x-ratelimit-reset"]) / 1000 - time.time())
    except (TypeError, ValueError):
        logger.warning("Could not read the provider's retry-after headers.")
    return None


def rate_limit_error(error: RateLimitError) -> AIRateLimitError:
    """Map OpenRouter's 429 to AIRateLimitError, keeping the suggested wait and its message."""
    return AIRateLimitError(
        "The AI service rate limit has been exceeded. Please try again later.",
        retry_after=retry_after_seconds(error),
        detail=str(error),
    )


def map_provider_error(error: Exception) -> Exception:
    """The app's error for a provider (SDK) error; other errors are returned unchanged."""
    if isinstance(error, APITimeoutError):
        return TimeoutError("The AI service took too long to respond.")
    if isinstance(error, APIConnectionError):
        return ConnectionError("Could not connect to the AI service.")
    if isinstance(error, RateLimitError):
        return rate_limit_error(error)
    if isinstance(error, APIStatusError) and error.status_code == 402:
        return AIOutOfCreditError(OUT_OF_CREDIT_MESSAGE)
    if isinstance(error, APIStatusError):
        return AIServiceError(f"The AI service returned an API error: {error.status_code}")
    return error


def _final_error(error: Exception, kind: str, model: str) -> Exception:
    """The app's error for a provider error that is not retried, logging the causes that
    need someone to act."""
    mapped = map_provider_error(error)
    if isinstance(mapped, AIOutOfCreditError):
        logger.error(
            "AI call refused: the OpenRouter account is out of credit (HTTP 402). "
            "kind=%s model=%s. Add credit to the account; the call was not retried.",
            kind, model,
        )
    return mapped


def is_transient(error: Exception) -> bool:
    """Timeouts (including 408), connection errors, 429 and 5xx. Other 4xx errors (409
    included) are not transient."""
    if isinstance(error, APIConnectionError):  # includes APITimeoutError
        return True
    if isinstance(error, APIStatusError):
        return error.status_code in (408, 429) or error.status_code >= 500
    return False


def retry_delay(error: Exception, retries_done: int) -> float | None:
    """Seconds to wait before the next attempt, or None if the error must not be retried."""
    if retries_done >= MAX_RETRIES or not is_transient(error):
        return None
    suggested = retry_after_seconds(error)
    if suggested is not None:
        return suggested if suggested <= MAX_RETRY_AFTER_SECONDS else None
    backoff = min(BACKOFF_MAX_SECONDS, BACKOFF_BASE_SECONDS * 2**retries_done)
    return backoff * (1 - 0.25 * random.random())  # jitter, like the SDK


async def _sleep(seconds: float) -> None:
    # A separate function so tests can skip the wait.
    await asyncio.sleep(seconds)


def _log_retry(kind: str, model: str, retry: int, error: Exception, delay: float) -> None:
    status = getattr(error, "status_code", None)
    logger.warning(
        "ai_retry kind=%s model=%s retry=%d/%d error=%s status=%s wait_s=%.2f",
        kind, model, retry, MAX_RETRIES, type(error).__name__,
        "none" if status is None else status, delay,
    )


async def call_with_retries(
    make_call: Callable[[], Awaitable[Any]], *, kind: str, model: str
) -> Any:
    """Run `make_call()` (one SDK request), retrying transient errors. Provider errors
    that are not retried are raised as the app's errors (see map_provider_error)."""
    retries = 0
    while True:
        try:
            return await make_call()
        except PROVIDER_ERRORS as error:
            delay = retry_delay(error, retries)
            if delay is None:
                raise _final_error(error, kind, model) from error
            retries += 1
            _log_retry(kind, model, retries, error, delay)
            await _sleep(delay)


def _has_text(chunk: Any) -> bool:
    choices = getattr(chunk, "choices", None)
    if not choices:
        return False
    content = getattr(getattr(choices[0], "delta", None), "content", None)
    return isinstance(content, str) and bool(content)


async def stream_with_retries(
    open_stream: Callable[[], Awaitable[Any]], *, kind: str, model: str
) -> AsyncIterator[Any]:
    """Yield the chunks of a streamed SDK request opened by `open_stream()`.

    A transient error before the first chunk with text retries the whole request. After
    that, errors are raised (mapped) without a retry. The stream is always closed.
    """
    retries = 0
    while True:
        stream = None
        started = False
        try:
            stream = await open_stream()
            async for chunk in stream:
                if not started and _has_text(chunk):
                    started = True
                yield chunk
            return
        except PROVIDER_ERRORS as error:
            delay = None if started else retry_delay(error, retries)
            if delay is None:
                raise _final_error(error, kind, model) from error
            retries += 1
            _log_retry(kind, model, retries, error, delay)
        finally:
            if stream is not None:
                await stream.close()
        await _sleep(delay)
