"""Text embeddings through OpenRouter, for indexing chunks and embedding questions."""

import asyncio
import logging
import os
import time

from .ai_calls import AIServiceError, _get_async_client, call_with_retries
from .usage import log_ai_usage

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = os.getenv("OPENROUTER_EMBEDDING_MODEL", "openai/text-embedding-3-small")
# Must match the vector column in the chunks table; changing models means re-indexing.
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "1536"))
EMBEDDING_BATCH_SIZE = 64


class EmbeddingDimensionError(AIServiceError):
    """Raised when the provider returns vectors of an unexpected size."""


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """One vector per text, in the same order. Sent in batches of 64."""
    if any(not text.strip() for text in texts):
        raise ValueError("Cannot embed empty text.")

    vectors: list[list[float]] = []
    for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
        vectors.extend(await _embed_batch(texts[start:start + EMBEDDING_BATCH_SIZE]))
    return vectors


async def embed_query(text: str) -> list[float]:
    [vector] = await embed_texts([text])
    return vector


async def _embed_batch(batch: list[str]) -> list[list[float]]:
    client = _get_async_client()
    started_at = time.perf_counter()
    usage = None
    outcome = "error"
    try:
        response = await call_with_retries(
            lambda: client.embeddings.create(
                model=EMBEDDING_MODEL, input=batch, encoding_format="float"
            ),
            kind="embedding",
            model=EMBEDDING_MODEL,
        )
        usage = getattr(response, "usage", None)
        data = sorted(response.data or [], key=lambda item: item.index)
        if len(data) != len(batch):
            raise AIServiceError(
                f"The AI service returned {len(data)} embeddings for {len(batch)} texts."
            )
        vectors = [list(item.embedding) for item in data]
        sizes = {len(vector) for vector in vectors}
        if sizes != {EMBEDDING_DIMENSIONS}:
            raise EmbeddingDimensionError(
                f"Expected {EMBEDDING_DIMENSIONS}-dimension embeddings from "
                f"{EMBEDDING_MODEL}, got {sorted(sizes)}."
            )
        outcome = "ok"
        return vectors
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except AIServiceError as error:
        logger.error("Embedding request failed: %s", error)
        raise
    finally:
        log_ai_usage(
            kind="embedding",
            model=EMBEDDING_MODEL,
            usage=usage,
            started_at=started_at,
            outcome=outcome,
        )
