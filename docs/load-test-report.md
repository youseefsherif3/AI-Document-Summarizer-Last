# Load test report (Phase 3, Step 10)

**Date:** 2026-11-11 · **Scenario:** `loadtest/locustfile.py` · **How to rerun:** `loadtest/README.md`

## Setup

One laptop ran everything in Docker: Postgres + pgvector, Redis, the API (2 uvicorn processes), the
Celery worker, and Locust on the host. **Fake LLM** (`FAKE_LLM=true`): no AI call, no money; answers
come after fixed delays that are about a small hosted model's speed (embeddings 0.25 s, summary
about 2 s, chat 1.5 s to complete). Queue mode, cache off, per-user daily limits off, separate
database, session cookies instead of sign-in calls.

Each simulated user pauses 1–3 s between actions and does one of: list documents (weight 4), ask a
chat question and read the whole SSE answer (4), upload a ~2 KB file and wait for its job (1).
A user waits for their upload's job to finish before doing anything else (like the page does), so
the request rate **depends on how fast jobs finish**. Each user also uploads one file when it
starts, so every run begins with a burst of one upload per user. 60 seconds per run.

## Results

| | 5 users | 20 users | 50 users | 50 users, worker ×8 | 100 users, worker ×8 |
|---|---|---|---|---|---|
| Requests/s (all) | 3.6 | 14.8 | 34.9 | 37.2 | 72.6 |
| Failed requests | 0 of 216 | 0 of 863 | 0 of 2,069 | 0 of 2,202 | 0 of 4,304 |
| `GET /documents` p50 / p95 ms | 8 / 48 | 7 / 50 | 8 / 47 | 8 / 18 | 10 / 50 |
| Upload request (202) p50 / p95 ms | 20 / 120 | 20 / 60 | 50 / 95 | 21 / 90 | 45 / 160 |
| Chat first event p50 / p95 ms | 290 / 320 | 280 / 320 | 280 / 330 | 280 / 320 | 280 / 340 |
| Chat complete p50 / p95 ms | 1,500 / 1,600 | 1,500 / 1,600 | 1,500 / 1,600 | 1,500 / 1,600 | 1,500 / 1,700 |
| **Upload → job done** p50 / p95 s | 3.1 / 3.1 | 4.6 / 15 | **26 / 40** | **3.1 / 7.7** | 7.6 / 18 |
| Jobs waiting or running, peak | 2 | 17 | 45 | 36 | 84 |
| Jobs unfinished when the run ended | 0 | 2 | 21 | 5 | 7 |
| CPU % avg / peak: API · worker · db | 4/12 · 0/2 · 2/7 | 10/18 · 2/4 · 5/14 | 22/42 · 3/6 · 8/20 | 24/36 · 7/26 · 10/21 | **54/100** · 11/19 · 22/42 |

(Worker ×8 = `WORKER_CONCURRENCY=8`; the other columns use the default 2. CPU is Docker's
per-container figure, 100 % = one core. The "peak" of waiting jobs includes the start-up burst.)

Zero errors in every run, and the request paths that don't wait for the worker stay flat: chat
answers and the document list have the same latency at 5 and 100 users.

## Where it slows down first

**The worker, not the API.** With the default 2 worker processes the worker can finish about one
job a second (each takes about 2 s of work, seen as about 3 s because the page checks every 1.5 s). Past roughly 20 users uploads arrive faster than that, so
jobs queue: at 50 users the time from upload to finished job went from 3 s to **26 s (p50), 40 s
(p95)**, the Redis queue reached 43 waiting tasks (`documind_queue_depth` in `/metrics`, the
Tasks page in Flower), and 21 jobs were still unfinished when the run stopped. Meanwhile the API,
database and Redis were nearly idle (API 22 % CPU, db 8 %, Redis 1 %) and the worker's own CPU
was 3 %: it was waiting on the (fake) AI, not computing. With a real provider the work is just as
much waiting, so the limit is *how many jobs run at once*, not CPU.

After that the next limit is the **API process**: at 100 users it averages 54 % CPU with peaks at
100 % (one core), still with zero errors and unchanged latency, so it has room but is the next
thing to grow.

## Fixes

1. **Run more jobs at once (measured).** `WORKER_CONCURRENCY` is now a setting (it was fixed at 2).
   At 8, the 50-user run drops from 26 s to **3.1 s** p50 and 40 s to **7.7 s** p95 upload-to-done,
   finishes 2.4× more jobs in the minute (2.7/s instead of 1.1/s), and 100 users (7.6 s p50) are
   still better than 50 users were at 2. Cost: more memory (each process is a full Python
   process), and the provider's own rate limit becomes the next ceiling, so raise it step by step
   and watch `ai_retry` lines and 429s. A cheaper way to hold many jobs per process is a threads
   or gevent pool; that is a larger change and was not tried. The default is still 2.
2. **Poll jobs less often as they get older (not measured, proposed).** Status checks
   (`GET /jobs/{id}`) were already 22 requests a second at 100 users, 31 % of all requests, each
   cheap (p50 10 ms) but growing with the number of waiting jobs. The page polls every 1.5 s; a
   job still waiting after 10 s could be checked every 3 s and after 30 s every 5 s. This halves
   those requests when the queue is long, and costs at most a few seconds of delay in showing a
   job that was queued for a long time anyway.

## Limits of this test

- Locust is a closed loop: users that wait for a slow job do less of everything else, which is why
  "all requests per second" barely changed between 20 and 50 users at 2 workers.
- Everything shares one machine, the delays are fixed, and the first seconds of each run are the
  upload burst, so peaks of waiting jobs are higher than steady state.
- Not measured: the frontend proxy, real provider speed, rate limits and failures, the Redis cache
  (switched off on purpose), large files and PDF parsing (CPU-heavy in the real worker), and
  documents lists that are much longer (`GET /documents` returns every document with its full
  summary, unpaged).
- A real-model run was not done (not asked for); Step 10 stays fully on the fake.
