
import os

from dotenv import load_dotenv
from openai import (
    OpenAI,
    AsyncOpenAI,
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    APIStatusError,
)

load_dotenv()

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
    timeout=30.0,
)

async_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
    timeout=60.0,
)

MODEL = os.getenv("OPENROUTER_MODEL", "openai/gpt-4o")


class AIRateLimitError(Exception):
    """Raised when the AI provider rate limit is exceeded."""


class AIServiceError(Exception):
    """Raised when the AI provider returns an API error."""


async def stream_summary(text: str):
    """Yield summary text as the provider generates it."""
    if not text.strip():
        raise ValueError("Cannot summarize empty text.")

    try:
        stream = await async_client.chat.completions.create(
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


def summarize_text(text: str) -> str:
    if not text.strip():
        raise ValueError("Cannot summarize empty text.")

    try:
        response = client.chat.completions.create(
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
                {
                    "role": "user",
                    "content": f"Summarize this document:\n\n{text}",
                },
            ],
            max_tokens=1000,
        )

        summary = response.choices[0].message.content

        if not summary:
            raise ValueError("The AI model returned an empty summary.")

        return summary.strip()

    except APITimeoutError as error:
        raise TimeoutError(
            "The AI service took too long to respond."
        ) from error

    except APIConnectionError as error:
        raise ConnectionError(
            "Could not connect to the AI service."
        ) from error

    except RateLimitError as error:
        raise AIRateLimitError(
            "The AI service rate limit has been exceeded. Please try again later."
        ) from error

    except APIStatusError as error:
        raise AIServiceError(
            f"The AI service returned an API error: {error.status_code}"
        ) from error
