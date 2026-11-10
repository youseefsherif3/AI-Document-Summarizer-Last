"""A plain (non-streaming) chat completion through OpenRouter, with usage logging.

Used where the whole reply is needed at once, such as the eval judge.
"""

import asyncio
import time

from .ai_calls import _get_async_client, call_with_retries
from .usage import log_ai_usage


async def complete(
    *,
    model: str,
    messages: list[dict],
    kind: str,
    temperature: float = 0,
    max_tokens: int = 1000,
    extra_body: dict | None = None,
) -> str:
    """The model's reply text. Errors are mapped and retried in services/ai_calls.py.

    `extra_body` adds OpenRouter request fields, such as `reasoning`."""
    client = _get_async_client()
    started_at = time.perf_counter()
    usage = None
    outcome = "error"
    try:
        response = await call_with_retries(
            lambda: client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                **({"extra_body": extra_body} if extra_body else {}),
            ),
            kind=kind,
            model=model,
        )
        usage = getattr(response, "usage", None)
        choices = response.choices or []
        text = (choices[0].message.content or "") if choices else ""
        outcome = "ok"
        return text
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        log_ai_usage(kind=kind, model=model, usage=usage, started_at=started_at, outcome=outcome)
