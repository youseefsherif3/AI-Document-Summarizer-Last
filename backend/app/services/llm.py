"""A plain (non-streaming) chat completion through OpenRouter, with usage logging.

Used where the whole reply is needed at once, such as the eval judge.
"""

import asyncio
import time

from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError

from .summarizer import AIServiceError, _get_async_client, rate_limit_error
from .usage import log_ai_usage


async def complete(
    *,
    model: str,
    messages: list[dict],
    kind: str,
    temperature: float = 0,
    max_tokens: int = 1000,
) -> str:
    """The model's reply text. Errors are mapped like the summarizer's (504/503/429/502)."""
    client = _get_async_client()
    started_at = time.perf_counter()
    usage = None
    outcome = "error"
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        usage = getattr(response, "usage", None)
        choices = response.choices or []
        text = (choices[0].message.content or "") if choices else ""
        outcome = "ok"
        return text
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except APITimeoutError as error:
        raise TimeoutError("The AI service took too long to respond.") from error
    except APIConnectionError as error:
        raise ConnectionError("Could not connect to the AI service.") from error
    except RateLimitError as error:
        raise rate_limit_error(error) from error
    except APIStatusError as error:
        raise AIServiceError(
            f"The AI service returned an API error: {error.status_code}"
        ) from error
    finally:
        log_ai_usage(kind=kind, model=model, usage=usage, started_at=started_at, outcome=outcome)
