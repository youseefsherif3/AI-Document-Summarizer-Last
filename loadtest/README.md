# Load test

One Locust scenario against the local Docker stack, with the **fake LLM** (`FAKE_LLM=true`):
**no AI provider is ever called and no money is spent.** Each simulated user is signed in and,
with a 1–3 s pause between actions, does one of these (weights in brackets):

- lists their documents, `GET /documents` (4)
- uploads a small, different text file in queue mode and polls `GET /jobs/{id}` every 1.5 s until
  the job is done, like the browser (1)
- asks a chat question and reads the SSE answer to its end (4)

The report of the last run is in [`docs/load-test-report.md`](../docs/load-test-report.md).

## Safety (why it cannot spend money)

- The compose override blanks `AI_API_KEY`, `OPENROUTER_API_KEY` and `AI_BASE_URL` for the API and
  the worker, whatever `backend/.env` says, and sets `FAKE_LLM=true`.
- The app refuses to start in fake mode on Vercel (`VERCEL` set).
- The Locust file refuses to run unless the host is `127.0.0.1`/`localhost`, `/health` reports
  `"fake_llm": true`, and the first finished job's summary carries the `[FAKE LLM]` marker (a worker
  that used a real provider would stop the test at once).
- It uses its own database, `docsum_load` (the seed script refuses any other database), and
  session tokens instead of sign-in calls, so the sign-in and registration limits stay as they are.
  The per-user daily limits are switched off for the API container only.

## Run it yourself

From the project root (Docker running; the backend virtualenv has Locust, see
`backend/requirements-dev.txt`). On Windows use `backend/.venv/Scripts/python`.

```bash
# 1. Users and database (creates docsum_load, migrates it, writes loadtest/users.json)
backend/.venv/Scripts/python loadtest/seed_users.py --users 100

# 2. The stack: db, redis, worker, flower, and the API container in fake mode
docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml up -d --build
curl http://127.0.0.1:8000/health          # {"status":"healthy","fake_llm":true}

# 3. One run (open http://localhost:8089 instead by dropping --headless and the -u/-r/-t options)
backend/.venv/Scripts/python -m locust -f loadtest/locustfile.py --headless \
    -u 20 -r 4 -t 60s --host http://127.0.0.1:8000

# 4. Or the whole sweep with a table (queue depth and container CPU included)
backend/.venv/Scripts/python loadtest/run_sweep.py --users 5 20 50 --seconds 60 --label mine
```

Results go to `loadtest/results/` (not committed). Watch while it runs: Flower at
<http://127.0.0.1:5555>, and the numbers at `/metrics`:

```bash
curl -H "Authorization: Bearer loadtest-token" http://127.0.0.1:8000/metrics
```

Things to change: `WORKER_CONCURRENCY` (jobs the worker runs at once, default 2) and `API_WORKERS`
(API processes, default 2) as environment variables of the `docker compose up` command;
`FAKE_LLM_DELAY_SCALE` (1 = about a small hosted model's speed; see `app/services/fake_llm.py`).

Clean up:

```bash
docker compose -f docker-compose.yml -f loadtest/docker-compose.loadtest.yml down   # keeps the volumes
backend/.venv/Scripts/python loadtest/seed_users.py --clean
```

## What it does not measure

The frontend (Next.js and its `/api` rewrite) is not in the path: Locust talks to the API
directly. The AI provider's real speed and limits are not measured (the fake answers after fixed
delays), and the cache is switched off so every request does the work. One machine runs the
stack and Locust, so absolute numbers depend on it; compare runs with each other, not with
production.
