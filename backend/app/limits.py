"""Per-user daily limits for uploads and chat questions.

Registration is open and emails aren't verified, so these limits cap how much any one
account can spend per day. They are set by environment variables (0 means no limit):

- DAILY_UPLOAD_LIMIT (default 20)
- DAILY_CHAT_LIMIT (default 100)

A day is a UTC calendar day. Each use is counted in `daily_usage` before any AI call, with
one atomic statement, so two requests at the same moment can never both pass the limit.
Uploads that fail after that point still count: they may already have cost money.
"""

import math
import os
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import text

from app.database import get_connection

# kind → (column in daily_usage, environment variable, default, singular, plural)
KINDS = {
    "upload": ("uploads", "DAILY_UPLOAD_LIMIT", 20, "upload", "uploads"),
    "chat": ("chat_questions", "DAILY_CHAT_LIMIT", 100, "chat question", "chat questions"),
}


class DailyLimitReached(Exception):
    """The user has used up today's allowance for this kind of request."""

    def __init__(self, kind: str, limit: int, retry_after: int):
        _, _, _, singular, plural = KINDS[kind]
        noun = singular if limit == 1 else plural
        super().__init__(
            f"You've reached today's limit of {limit} {noun}. It resets at midnight UTC."
        )
        self.kind = kind
        self.limit = limit
        self.retry_after = retry_after


def _now() -> datetime:
    """The current time; a separate function so tests can move the clock."""
    return datetime.now(timezone.utc)


def daily_limit(kind: str) -> int:
    """The configured limit for this kind; 0 means no limit. A bad value is an error, not
    silently replaced by the default."""
    _, variable, default, _, _ = KINDS[kind]
    raw = os.getenv(variable, "").strip()
    if not raw:
        return default
    try:
        limit = int(raw)
    except ValueError:
        raise ValueError(f"{variable} must be a whole number (0 means no limit), got {raw!r}.")
    if limit < 0:
        raise ValueError(f"{variable} must not be negative (0 means no limit), got {limit}.")
    return limit


def check_limit_settings() -> None:
    """Called at startup, so a bad setting stops the app instead of failing every request."""
    for kind in KINDS:
        daily_limit(kind)


def seconds_until_midnight_utc(now: datetime) -> int:
    midnight = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), timezone.utc)
    return max(1, math.ceil((midnight - now).total_seconds()))


def use_daily_quota(user_id: int, kind: str, day: date) -> bool:
    """Count one use for the user on that day. False (and nothing counted) if the limit is
    already reached.

    One statement: insert the day's row or add 1, but only while under the limit. The row
    lock makes concurrent requests wait for each other, so the count never passes the limit.
    """
    column, _, _, _, _ = KINDS[kind]  # from a fixed table, never from input
    limit = daily_limit(kind)
    with get_connection() as connection:
        counted = connection.execute(
            text(
                f"INSERT INTO daily_usage (user_id, day, {column}) VALUES (:user_id, :day, 1) "
                f"ON CONFLICT (user_id, day) DO UPDATE "
                f"SET {column} = daily_usage.{column} + 1 "
                f"WHERE :limit = 0 OR daily_usage.{column} < :limit "
                f"RETURNING {column}"
            ),
            {"user_id": user_id, "day": day, "limit": limit},
        ).scalar_one_or_none()
    return counted is not None


def enforce_daily_limit(user_id: int, kind: str) -> None:
    """Count one use today, or raise DailyLimitReached. Call it before any AI work."""
    now = _now()
    if not use_daily_quota(user_id, kind, now.date()):
        raise DailyLimitReached(kind, daily_limit(kind), seconds_until_midnight_utc(now))
