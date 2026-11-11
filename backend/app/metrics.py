"""`GET /metrics`: the app's own numbers in the Prometheus text format, for the operator.

Off unless `METRICS_TOKEN` is set; then it needs `Authorization: Bearer <token>`. The numbers are
system-wide (all users), which is why this is not a user endpoint and is hidden from the API docs.

Where each number comes from (so they are right with several API instances and a worker):

- HTTP requests and latency: counted in this API process (`MetricsMiddleware`), so they cover
  the instance that answers the scrape.
- AI calls, tokens, cost, duration: summed from the `ai_usage` table (every process writes it).
- Jobs by mode and state: counted from `processing_jobs`.
- Cache hits and misses: totals kept in Redis (API and worker both add to them).
- Queue depth: the length of the worker's Redis list.

AI and job numbers are read from Postgres, so they are exact but cost a query per scrape; a
scrape every 15–60 seconds is fine. A collector that fails is logged and reported in
`documind_metrics_collector_errors`; it never fails the scrape.
"""

import logging
import os
import secrets
import threading
import time
from collections import defaultdict

from sqlalchemy import text

from app import cache
from app.database import get_connection

logger = logging.getLogger(__name__)

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300)
JOB_STATES = ("pending", "processing", "completed", "failed")
QUEUE_NAME = "documind"

_lock = threading.Lock()
_requests: dict[tuple, int] = defaultdict(int)  # (method, route, status) -> count
_durations: dict[tuple, dict] = {}  # (method, route) -> {"buckets": [...], "sum": s, "count": n}


def reset() -> None:
    with _lock:
        _requests.clear()
        _durations.clear()


def observe_request(method: str, route: str, status: int, seconds: float) -> None:
    with _lock:
        _requests[(method, route, str(status))] += 1
        entry = _durations.setdefault(
            (method, route), {"buckets": [0] * len(BUCKETS), "sum": 0.0, "count": 0}
        )
        entry["sum"] += seconds
        entry["count"] += 1
        for index, bound in enumerate(BUCKETS):
            if seconds <= bound:
                entry["buckets"][index] += 1


class MetricsMiddleware:
    """Counts every HTTP request by method, route template and status, and times it.

    Pure ASGI (not BaseHTTPMiddleware), so streamed responses (chat, uploads) pass untouched. The
    duration of a stream is the time until it ends."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = {"code": 500}

        async def send_and_note(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_and_note)
        finally:
            # The router puts the matched route in the scope; a path no route matched is one
            # label, so scanners can't create endless series.
            route = getattr(scope.get("route"), "path", None) or "unmatched"
            observe_request(scope["method"], route, status["code"],
                            time.perf_counter() - started)


# --- Access ---------------------------------------------------------------------------------


def metrics_token() -> str | None:
    return os.getenv("METRICS_TOKEN", "").strip() or None


def token_matches(header: str | None, token: str) -> bool:
    scheme, _, supplied = (header or "").partition(" ")
    return scheme.lower() == "bearer" and secrets.compare_digest(
        supplied.strip().encode(), token.encode()
    )


# --- Rendering ------------------------------------------------------------------------------


def _escape(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(**labels: object) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"


class _Output:
    def __init__(self):
        self.lines: list[str] = []

    def metric(self, name: str, kind: str, help_text: str) -> None:
        self.lines += [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"]

    def sample(self, name: str, value: float | int, **labels: object) -> None:
        self.lines.append(f"{name}{_labels(**labels)} {value:g}")

    def text(self) -> str:
        return "\n".join(self.lines) + "\n"


def _http(out: _Output) -> None:
    with _lock:
        requests = dict(_requests)
        durations = {key: {**v, "buckets": list(v["buckets"])} for key, v in _durations.items()}
    out.metric("documind_http_requests_total", "counter",
               "HTTP requests handled by this API process, by method, route and status.")
    for (method, route, status), count in sorted(requests.items()):
        out.sample("documind_http_requests_total", count, method=method, route=route, status=status)
    out.metric("documind_http_request_duration_seconds", "histogram",
               "HTTP request duration in this API process (a stream counts until it ends).")
    for (method, route), entry in sorted(durations.items()):
        for bound, count in zip(BUCKETS, entry["buckets"]):
            out.sample("documind_http_request_duration_seconds_bucket", count,
                       method=method, route=route, le=bound)
        out.sample("documind_http_request_duration_seconds_bucket", entry["count"],
                   method=method, route=route, le="+Inf")
        out.sample("documind_http_request_duration_seconds_sum", entry["sum"],
                   method=method, route=route)
        out.sample("documind_http_request_duration_seconds_count", entry["count"],
                   method=method, route=route)


def _ai(out: _Output) -> None:
    with get_connection() as connection:
        rows = connection.execute(text(
            "SELECT kind, model, outcome, count(*) AS calls, "
            "       coalesce(sum(prompt_tokens), 0) AS prompt_tokens, "
            "       coalesce(sum(completion_tokens), 0) AS completion_tokens, "
            "       coalesce(sum(cost_usd), 0) AS cost_usd, "
            "       coalesce(sum(duration_ms), 0) AS duration_ms "
            "FROM ai_usage GROUP BY kind, model, outcome ORDER BY kind, model, outcome"
        )).mappings().all()
    series = (
        ("documind_ai_calls_total", "AI calls recorded, by kind, model and outcome.", "calls", 1),
        ("documind_ai_prompt_tokens_total", "Prompt tokens of recorded AI calls.", "prompt_tokens", 1),
        ("documind_ai_completion_tokens_total", "Completion tokens of recorded AI calls.",
         "completion_tokens", 1),
        ("documind_ai_cost_usd_total", "Cost in USD reported or estimated for AI calls.",
         "cost_usd", 1),
        ("documind_ai_duration_seconds_total", "Time spent in AI calls.", "duration_ms", 0.001),
    )
    for name, help_text, column, factor in series:
        out.metric(name, "counter", help_text)
        for row in rows:
            out.sample(name, float(row[column]) * factor, kind=row["kind"], model=row["model"],
                       outcome=row["outcome"])


def _jobs(out: _Output) -> None:
    with get_connection() as connection:
        rows = connection.execute(text(
            "SELECT mode, status, count(*) AS jobs FROM processing_jobs GROUP BY mode, status"
        )).mappings().all()
    found = {(row["mode"], row["status"]): row["jobs"] for row in rows}
    out.metric("documind_jobs", "gauge", "Upload jobs by mode and state.")
    for mode in ("queue", "inline"):
        for state in JOB_STATES:
            out.sample("documind_jobs", found.get((mode, state), 0), mode=mode, state=state)


def _cache(out: _Output) -> None:
    totals = cache.totals_sync()
    out.metric("documind_cache_lookups_total", "counter",
               "Cache lookups by kind and result (shared by the API and the worker).")
    for kind in ("embedding", "summary"):
        for result in ("hit", "miss"):
            out.sample("documind_cache_lookups_total", totals.get(f"{kind}.{result}", 0),
                       kind=kind, result=result)


def queue_depth() -> int | None:
    """Jobs waiting in the worker's Redis queue, or None if Redis can't be read."""
    import redis

    # Same default as the worker; only asked for when this deployment uses the queue.
    from app.ingestion import ingestion_mode

    url = os.getenv("REDIS_URL", "").strip()
    if not url and ingestion_mode() == "queue":
        url = "redis://127.0.0.1:6379/0"
    if not url:
        return None
    client = redis.Redis.from_url(url, socket_connect_timeout=0.5, socket_timeout=0.5)
    try:
        return int(client.llen(QUEUE_NAME))
    except (redis.RedisError, OSError):
        return None
    finally:
        client.close()


def _queue(out: _Output) -> None:
    depth = queue_depth()
    if depth is not None:
        out.metric("documind_queue_depth", "gauge", "Tasks waiting in the worker queue.")
        out.sample("documind_queue_depth", depth)


def render() -> str:
    out = _Output()
    errors: dict[str, int] = {}
    _http(out)
    for name, collector in (("ai", _ai), ("jobs", _jobs), ("cache", _cache), ("queue", _queue)):
        try:
            collector(out)
        except Exception:
            errors[name] = 1
            logger.warning("metrics_collector_failed collector=%s", name, exc_info=True)
    out.metric("documind_metrics_collector_errors", "gauge",
               "1 for a collector that failed in this scrape.")
    for name in ("ai", "jobs", "cache", "queue"):
        out.sample("documind_metrics_collector_errors", errors.get(name, 0), collector=name)
    return out.text()
