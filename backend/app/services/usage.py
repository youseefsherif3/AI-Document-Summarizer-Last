"""One log line with tokens and cost for every LLM and embedding call."""

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# Optional fallback prices, used only when OpenRouter returns no `usage.cost`.
# JSON, USD per million tokens, e.g.
# {"openai/gpt-4o": {"prompt": 2.5, "completion": 10}}
PRICES_ENV = "AI_PRICES_PER_MILLION_TOKENS"


def _prices() -> dict[str, dict[str, float]]:
    raw = os.getenv(PRICES_ENV, "").strip()
    if not raw:
        return {}
    try:
        prices = json.loads(raw)
    except json.JSONDecodeError:
        logger.error("%s is not valid JSON; cost estimates are disabled.", PRICES_ENV)
        return {}
    if not isinstance(prices, dict):
        logger.error("%s must be a JSON object; cost estimates are disabled.", PRICES_ENV)
        return {}
    return prices


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    price = _prices().get(model)
    if not isinstance(price, dict):
        return None
    try:
        return (
            prompt_tokens * float(price.get("prompt", 0))
            + completion_tokens * float(price.get("completion", 0))
        ) / 1_000_000
    except (TypeError, ValueError):
        logger.error("%s has an invalid price for %s.", PRICES_ENV, model)
        return None


def _get(source: Any, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(name)
    return getattr(source, name, None)


def log_ai_usage(
    *,
    kind: str,
    model: str,
    usage: Any,
    started_at: float,
    outcome: str = "ok",
) -> dict[str, Any]:
    """Log one AI call. `usage` is OpenRouter's usage object (or None if it never arrived).

    `started_at` is a time.perf_counter() value taken just before the request.
    Returns the logged fields, which is handy for tests and eval reports.
    """
    prompt_tokens = _get(usage, "prompt_tokens")
    completion_tokens = _get(usage, "completion_tokens")
    if prompt_tokens is not None and completion_tokens is None:
        completion_tokens = 0  # embeddings have input tokens only
    cost = _get(usage, "cost")

    if cost is not None:
        cost_source = "openrouter"
    elif prompt_tokens is not None and completion_tokens is not None:
        cost = estimate_cost(model, prompt_tokens, completion_tokens)
        cost_source = "estimate" if cost is not None else "unknown"
    else:
        cost_source = "unknown"

    fields = {
        "kind": kind,
        "model": model,
        "outcome": outcome,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": _get(usage, "total_tokens"),
        "cost_usd": None if cost is None else round(float(cost), 8),
        "cost_source": cost_source,
        "duration_ms": round((time.perf_counter() - started_at) * 1000),
    }
    message = " ".join(
        f"{key}={'unknown' if value is None else value}" for key, value in fields.items()
    )
    logger.info("ai_usage %s", message, extra={"ai_usage": fields})
    return fields
