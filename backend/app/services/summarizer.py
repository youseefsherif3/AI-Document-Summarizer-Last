
import logging
import os
from functools import cache
from pathlib import Path

from dotenv import load_dotenv
from openai import (
    AsyncOpenAI,
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    APIStatusError,
)

# backend/.env, wherever the process was started from.
load_dotenv(Path(__file__).resolve().parents[2] / ".env")

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o")


class AIRateLimitError(Exception):
    """Raised when the AI provider rate limit is exceeded."""


class AIServiceError(Exception):
    """Raised when the AI provider returns an API error."""


class AIServiceNotConfiguredError(Exception):
    """Raised when OPENROUTER_API_KEY is not set."""


def _require_api_key() -> str:
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        logger.error("OPENROUTER_API_KEY is not set; cannot call the AI service.")
        raise AIServiceNotConfiguredError("AI service is not configured.")
    return api_key


# The client is created on first use so importing this module (e.g. in tests)
# does not require OPENROUTER_API_KEY. A failed key check is not cached.
@cache
def _get_async_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=_require_api_key(),
        timeout=60.0,
    )


async def stream_summary(text: str):
    """Yield summary text as the provider generates it."""
    if not text.strip():
        raise ValueError("Cannot summarize empty text.")

    try:
        stream = await _get_async_client().chat.completions.create(
            model=MODEL,
            messages=[
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
            ],
            max_tokens=1000,
            stream=True,
        )

        try:
            async for chunk in stream:
                content = chunk.choices[0].delta.content if chunk.choices else None
                if isinstance(content, str) and content:
                    yield content
        finally:
            await stream.close()
    except APITimeoutError as error:
        raise TimeoutError("The AI service took too long to respond.") from error
    except APIConnectionError as error:
        raise ConnectionError("Could not connect to the AI service.") from error
    except RateLimitError as error:
        raise AIRateLimitError(
            "The AI service rate limit has been exceeded. Please try again later."
        ) from error
    except APIStatusError as error:
        raise AIServiceError(
            f"The AI service returned an API error: {error.status_code}"
        ) from error
