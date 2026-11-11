"""A small Redis cache for results that cost a paid AI call. Postgres stays the source of truth;
Redis can be flushed at any time and nothing is lost.

What is cached (and why only this):

- **Embeddings** (`embedding`): one vector per text, for chunks and for chat questions. A
  re-uploaded file, an overlapping document or a repeated question skips the embedding call.
  The key is a hash of the provider URL, model, dimensions and the text, so it cannot return a
  vector from another model; the value depends on nothing but that text. Not scoped to a user:
  the key is a one-way hash, so only someone who already has the text can ask for its vector.
- **Summaries** (`summary`): the summary of a document's text, so uploading the same file again
  skips a paid summary. Scoped to the owner (`<user id>:<hash>`), because a summary is derived
  from one user's document. Deleting a document deletes its cached summary.

Not cached: chat answers (they must come from the user's current documents) and retrieval
results (a database query costs milliseconds, and every upload and delete would have to
invalidate it).

Settings:

- `CACHE_ENABLED` (default true; false/0/off/no turn the cache off). The cache also needs a
  Redis: `CACHE_REDIS_URL`, or else `REDIS_URL` with database 1 (the queue uses database 0), so
  without either (Vercel today) it is simply off.
- `CACHE_EMBEDDING_TTL_SECONDS`, `CACHE_SUMMARY_TTL_SECONDS` (default 7 days each).
- Bump `SUMMARY_CACHE_VERSION` when the summary prompt or its output limit changes.

If Redis is unreachable the request carries on without the cache: a warning is logged and the
cache is skipped for FAILURE_PAUSE_SECONDS, so a Redis outage doesn't add a timeout to every
request. A value that can't be read is treated as a miss.

Each lookup logs one `cache kind=… hits=… misses=…` line; `stats` keeps the totals.
"""

import asyncio
import hashlib
import json
import logging
import os
import threading
import time
from collections import Counter
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import redis

logger = logging.getLogger(__name__)

KEY_PREFIX = "documind:cache:"
FAILURE_PAUSE_SECONDS = 10
SOCKET_TIMEOUT_SECONDS = 0.5
DEFAULT_TTL_SECONDS = 7 * 24 * 3600
SUMMARY_CACHE_VERSION = "1"

stats: Counter = Counter()  # "<kind>.hit", "<kind>.miss", "<kind>.set", "<kind>.error"

_FALSE = {"false", "0", "off", "no"}
_TRUE = {"", "true", "1", "on", "yes"}

_client: Any = None
_client_url: str | None = None
_down_until = 0.0
_lock = threading.Lock()


# --- Settings -------------------------------------------------------------------------------


def enabled() -> bool:
    """CACHE_ENABLED. A value that is neither true nor false is an error, not a guess."""
    raw = os.getenv("CACHE_ENABLED", "").strip().lower()
    if raw in _FALSE:
        return False
    if raw in _TRUE:
        return True
    raise ValueError(f"CACHE_ENABLED must be true or false, got {raw!r}.")


def ttl_seconds(kind: str) -> int:
    variable = f"CACHE_{kind.upper()}_TTL_SECONDS"
    raw = os.getenv(variable, "").strip()
    if not raw:
        return DEFAULT_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{variable} must be a whole number of seconds, got {raw!r}.")
    if value <= 0:
        raise ValueError(f"{variable} must be positive, got {value}.")
    return value


def check_cache_settings() -> None:
    """Called at startup, so a bad setting stops the app instead of failing every request."""
    enabled()
    ttl_seconds("embedding")
    ttl_seconds("summary")


def redis_url() -> str | None:
    """CACHE_REDIS_URL, or REDIS_URL switched to database 1; None when neither is set."""
    explicit = os.getenv("CACHE_REDIS_URL", "").strip()
    if explicit:
        return explicit
    queue_url = os.getenv("REDIS_URL", "").strip()
    if not queue_url:
        return None
    parts = urlsplit(queue_url)
    return urlunsplit(parts._replace(path="/1"))


def digest(*parts: str) -> str:
    """A stable key from several strings. Each part is length-prefixed, so different splits of
    the same characters never give the same key."""
    hasher = hashlib.sha256()
    for part in parts:
        encoded = part.encode("utf-8")
        hasher.update(str(len(encoded)).encode() + b":" + encoded)
    return hasher.hexdigest()


# --- Redis access (never raises for a Redis problem) ----------------------------------------


def _make_client(url: str):
    # A separate function so tests can replace Redis with a fake.
    return redis.Redis.from_url(
        url,
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        decode_responses=True,
    )


def _get_client():
    global _client, _client_url
    url = redis_url()
    with _lock:
        if _client is None or _client_url != url:
            _client = _make_client(url)
            _client_url = url
        return _client


def _usable() -> bool:
    return enabled() and redis_url() is not None and time.monotonic() >= _down_until


def _failed(kind: str, error: Exception) -> None:
    global _down_until
    stats[f"{kind}.error"] += 1
    if time.monotonic() >= _down_until:
        logger.warning(
            "cache_unavailable kind=%s error=%s: continuing without the cache for %ds.",
            kind, type(error).__name__, FAILURE_PAUSE_SECONDS,
        )
    _down_until = time.monotonic() + FAILURE_PAUSE_SECONDS


def reset_for_tests() -> None:
    global _client, _client_url, _down_until
    _client, _client_url, _down_until = None, None, 0.0
    stats.clear()


def get_many_sync(kind: str, keys: list[str]) -> list[Any | None]:
    """The cached values for the keys, `None` for a miss (or when the cache is unavailable)."""
    if not keys or not _usable():
        return [None] * len(keys)
    try:
        raw = _get_client().mget([f"{KEY_PREFIX}{kind}:{key}" for key in keys])
    except (redis.RedisError, OSError) as error:
        _failed(kind, error)
        return [None] * len(keys)
    values: list[Any | None] = []
    for key, item in zip(keys, raw):
        if item is None:
            values.append(None)
            continue
        try:
            values.append(json.loads(item))
        except ValueError:
            logger.warning("cache_corrupt kind=%s: ignoring an unreadable value.", kind)
            values.append(None)
    hits = sum(value is not None for value in values)
    stats[f"{kind}.hit"] += hits
    stats[f"{kind}.miss"] += len(values) - hits
    logger.info("cache kind=%s hits=%d misses=%d", kind, hits, len(values) - hits)
    return values


def set_many_sync(kind: str, items: dict[str, Any]) -> None:
    """Store values (JSON) with the kind's expiry. Failures are logged, never raised."""
    if not items or not _usable():
        return
    ttl = ttl_seconds(kind)
    try:
        pipe = _get_client().pipeline(transaction=False)
        for key, value in items.items():
            pipe.set(f"{KEY_PREFIX}{kind}:{key}", json.dumps(value), ex=ttl)
        pipe.execute()
    except (redis.RedisError, OSError) as error:
        _failed(kind, error)
        return
    stats[f"{kind}.set"] += len(items)


def delete_sync(kind: str, key: str) -> None:
    if not _usable():
        return
    try:
        _get_client().delete(f"{KEY_PREFIX}{kind}:{key}")
    except (redis.RedisError, OSError) as error:
        _failed(kind, error)


async def get_many(kind: str, keys: list[str]) -> list[Any | None]:
    # Redis calls are blocking, so they run in a thread: the event loop is never held up, and
    # the client doesn't depend on any one loop (a worker runs each job in a new one).
    return await asyncio.to_thread(get_many_sync, kind, keys)


async def set_many(kind: str, items: dict[str, Any]) -> None:
    await asyncio.to_thread(set_many_sync, kind, items)


# --- Keys -----------------------------------------------------------------------------------


def embedding_key(provider_url: str, model: str, dimensions: int, text: str) -> str:
    return digest("embedding", provider_url, model, str(dimensions), text)


def summary_key(owner_user_id: int, provider_url: str, model: str, text: str) -> str:
    """`<owner>:<hash>`: the owner is readable so one user's entries are easy to find."""
    return f"{owner_user_id}:{digest('summary', SUMMARY_CACHE_VERSION, provider_url, model, text)}"
