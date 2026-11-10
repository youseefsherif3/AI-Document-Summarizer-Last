
import asyncio
import logging
import os
import time
from contextlib import aclosing

from .ai_calls import (  # noqa: F401  (re-exported: the app imports the errors from here)
    OPENROUTER_BASE_URL,
    AIOutOfCreditError,
    AIRateLimitError,
    AIServiceError,
    AIServiceNotConfiguredError,
    _get_async_client,
    _require_api_key,
    rate_limit_error,
    stream_with_retries,
)
from .usage import log_ai_usage

logger = logging.getLogger(__name__)

MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o")


async def stream_summary(text: str):
    """Yield summary text as the provider generates it."""
    if not text.strip():
        raise ValueError("Cannot summarize empty text.")

    client = _get_async_client()
    started_at = time.perf_counter()
    # OpenRouter sends usage (tokens and cost) only in the last chunk, so a
    # cancelled or failed stream is logged without it.
    usage = None
    model = MODEL
    outcome = "error"

    messages = [
        {
            "role": "system",
            "content": (
                "You are a professional document summarizer. "
                "Summarize the provided document clearly and accurately. "
                "Preserve its key ideas, important details, and conclusions. "
                "Do not add information that is not present in the document. "
                "Use the same language as the original document."
            ),
        },
        {"role": "user", "content": f"Summarize this document:\n\n{text}"},
    ]

    try:
        chunks = stream_with_retries(
            lambda: client.chat.completions.create(
                model=MODEL, messages=messages, max_tokens=1000, stream=True
            ),
            kind="summary",
            model=MODEL,
        )
        async with aclosing(chunks):
            async for chunk in chunks:
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage
                model = getattr(chunk, "model", None) or model
                content = chunk.choices[0].delta.content if chunk.choices else None
                if isinstance(content, str) and content:
                    yield content
        outcome = "ok"
    except (GeneratorExit, asyncio.CancelledError):
        outcome = "cancelled"
        raise
    finally:
        log_ai_usage(
            kind="summary", model=model, usage=usage, started_at=started_at, outcome=outcome
        )
