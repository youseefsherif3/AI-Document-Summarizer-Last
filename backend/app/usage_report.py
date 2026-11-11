"""The signed-in user's own usage, for the Usage page. No migration: it only reads tables that
exist (`daily_usage` for today's limits, `ai_usage` for tokens and calls).

- `GET /usage/today`: uploads and chat questions used today, their daily limits (the same
  counters the limits use; nothing is counted twice), and when they reset (midnight UTC).
- `GET /usage/summary?days=7|30`: tokens by day and by kind, calls, an estimated cost, and the
  last 20 AI calls.

Every query is filtered by the signed-in user's id, taken from the session and never from the
request, so another user's numbers cannot appear. Only the user's own rows are read.

**Cost** is an estimate in EGP from a price table, per million tokens, by model name. The table
is built in for the models in use (SovereignEG prices) and can be replaced with
`USAGE_PRICES_EGP_PER_MILLION_TOKENS`, a JSON object such as
`{"gpt-4o-mini": {"prompt": 8.80, "completion": 35.19}, "text-embedding-3-small": {"prompt": 1.17}}`.
A model with no price adds nothing to the cost and is counted in `unpriced_calls`, so the page can
say that the estimate is incomplete. A model name is matched without a provider prefix
(`openai/gpt-4o-mini` → `gpt-4o-mini`) and with a version suffix (`gpt-4o-mini-2024-07-18`).
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import text

from app import limits
from app.auth import User, current_user
from app.database import get_connection

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/usage", tags=["usage"])

CURRENCY = "EGP"
RECENT_CALLS = 20
PERIODS = (7, 30)
PRICES_ENV = "USAGE_PRICES_EGP_PER_MILLION_TOKENS"
# EGP per million tokens (SovereignEG). Embeddings have input tokens only.
DEFAULT_PRICES: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"prompt": 8.80, "completion": 35.19},
    "text-embedding-3-small": {"prompt": 1.17, "completion": 0.0},
}


def price_table() -> dict[str, dict[str, float]]:
    """The price table: USAGE_PRICES_EGP_PER_MILLION_TOKENS if set, else the built-in one. A value
    that is not a valid table is an error (checked at startup), not a silent fallback."""
    raw = os.getenv(PRICES_ENV, "").strip()
    if not raw:
        return DEFAULT_PRICES
    try:
        table = json.loads(raw)
        if not isinstance(table, dict):
            raise ValueError("must be a JSON object")
        clean = {}
        for model, price in table.items():
            if not isinstance(price, dict):
                raise ValueError(f"the price of {model!r} must be an object")
            clean[str(model).lower()] = {
                "prompt": float(price.get("prompt", 0)),
                "completion": float(price.get("completion", 0)),
            }
            if min(clean[str(model).lower()].values()) < 0:
                raise ValueError(f"the price of {model!r} must not be negative")
        return clean
    except (ValueError, TypeError) as error:
        raise ValueError(f"{PRICES_ENV} is not a valid price table: {error}") from error


def check_usage_settings() -> None:
    """Called at startup, so a bad price table stops the app instead of failing the page."""
    price_table()


def price_for(model: str, table: dict[str, dict[str, float]]) -> dict[str, float] | None:
    """The prices for a model name as providers report it, or None if it has none."""
    name = model.lower().rsplit("/", 1)[-1]
    if name in table:
        return table[name]
    # A dated version such as gpt-4o-mini-2024-07-18: the longest table name it starts with.
    matches = [key for key in table if name.startswith(key + "-")]
    return table[max(matches, key=len)] if matches else None


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int,
                  table: dict[str, dict[str, float]]) -> float | None:
    price = price_for(model, table)
    if price is None:
        return None
    return (prompt_tokens * price["prompt"] + completion_tokens * price["completion"]) / 1_000_000


# --- Today ----------------------------------------------------------------------------------


def _limit_view(kind: str, used: int) -> dict:
    limit = limits.daily_limit(kind)
    return {
        "used": used,
        "limit": limit,
        "unlimited": limit == 0,
        "remaining": None if limit == 0 else max(0, limit - used),
        "percent": None if limit == 0 else min(100, round(used * 100 / limit)),
    }


def usage_today(user_id: int) -> dict:
    now = limits._now()
    with get_connection() as connection:
        row = connection.execute(
            text("SELECT uploads, chat_questions FROM daily_usage "
                 "WHERE user_id = :user_id AND day = :day"),
            {"user_id": user_id, "day": now.date()},
        ).first()
    uploads, chats = (row[0], row[1]) if row else (0, 0)
    seconds = limits.seconds_until_midnight_utc(now)
    return {
        "day": now.date().isoformat(),
        "uploads": _limit_view("upload", uploads),
        "chat": _limit_view("chat", chats),
        "resets_at": (now + timedelta(seconds=seconds)).replace(microsecond=0).isoformat(),
        "resets_in_seconds": seconds,
    }


@router.get("/today")
async def get_usage_today(user: User = Depends(current_user)):
    """Today's uploads and chat questions against the daily limits (0 = unlimited)."""
    return await run_in_threadpool(usage_today, user.id)


# --- Summary --------------------------------------------------------------------------------


def usage_summary(user_id: int, days: int) -> dict:
    now = limits._now()
    today = now.date()
    first_day = today - timedelta(days=days - 1)
    since = datetime(first_day.year, first_day.month, first_day.day, tzinfo=timezone.utc)
    table = price_table()

    with get_connection() as connection:
        grouped = connection.execute(
            text(
                "SELECT (created_at AT TIME ZONE 'UTC')::date AS day, kind, model, "
                "       count(*) AS calls, "
                "       count(*) FILTER (WHERE outcome <> 'ok') AS errors, "
                "       coalesce(sum(prompt_tokens), 0) AS prompt_tokens, "
                "       coalesce(sum(completion_tokens), 0) AS completion_tokens "
                "FROM ai_usage WHERE user_id = :user_id AND created_at >= :since "
                "GROUP BY 1, 2, 3"
            ),
            {"user_id": user_id, "since": since},
        ).mappings().all()
        recent = connection.execute(
            text(
                "SELECT created_at, kind, model, prompt_tokens, completion_tokens, outcome "
                "FROM ai_usage WHERE user_id = :user_id "
                "ORDER BY created_at DESC, id DESC LIMIT :limit"
            ),
            {"user_id": user_id, "limit": RECENT_CALLS},
        ).mappings().all()

    by_day = {
        (first_day + timedelta(days=i)).isoformat(): {
            "date": (first_day + timedelta(days=i)).isoformat(),
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
            "cost": 0.0, "tokens_by_kind": {},
        }
        for i in range(days)
    }
    by_kind: dict[str, dict] = {}
    totals = {"calls": 0, "errors": 0, "prompt_tokens": 0, "completion_tokens": 0,
              "total_tokens": 0, "cost": 0.0, "unpriced_calls": 0}

    for row in grouped:
        prompt, completion = int(row["prompt_tokens"]), int(row["completion_tokens"])
        cost = estimate_cost(row["model"], prompt, completion, table)
        day = by_day[row["day"].isoformat()]
        kind = by_kind.setdefault(row["kind"], {
            "calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "cost": 0.0})
        for target in (day, kind, totals):
            target["calls"] += row["calls"]
            target["prompt_tokens"] += prompt
            target["completion_tokens"] += completion
            target["total_tokens"] += prompt + completion
            target["cost"] += cost or 0.0
        day["tokens_by_kind"][row["kind"]] = (
            day["tokens_by_kind"].get(row["kind"], 0) + prompt + completion
        )
        totals["errors"] += row["errors"]
        if cost is None:
            totals["unpriced_calls"] += row["calls"]

    def money(value: float) -> float:
        return round(value, 4)

    for entry in (*by_day.values(), *by_kind.values(), totals):
        entry["cost"] = money(entry["cost"])
    return {
        "days": days,
        "from": first_day.isoformat(),
        "to": today.isoformat(),
        "currency": CURRENCY,
        "totals": totals,
        "by_day": list(by_day.values()),
        "by_kind": by_kind,
        "recent_calls": [
            {
                "time": row["created_at"].isoformat(),
                "kind": row["kind"],
                "model": row["model"],
                "prompt_tokens": row["prompt_tokens"],
                "completion_tokens": row["completion_tokens"],
                "outcome": row["outcome"],
            }
            for row in recent
        ],
    }


@router.get("/summary")
async def get_usage_summary(
    days: int = Query(7, description="7 or 30"), user: User = Depends(current_user)
):
    """Tokens by day and kind, calls and an estimated cost over the last 7 or 30 days (UTC days,
    today included), and the last 20 AI calls."""
    if days not in PERIODS:
        raise HTTPException(status_code=422, detail="days must be 7 or 30.")
    return await run_in_threadpool(usage_summary, user.id, days)
