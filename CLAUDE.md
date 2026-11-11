# AI Document Summarizer — Chat with your docs

@AGENTS.md

## Architecture
- Frontend: Next.js. Pages: app/page.tsx (dashboard), app/documents/page.tsx (upload/list), app/chat/page.tsx (chat), app/login and app/register (components/auth/)
- The browser only calls this app's own `/api/*`; next.config.ts rewrites it to the backend (BACKEND_URL, server-only, read at build time; lib/backend-url.ts). So the backend's session cookie is first-party. Backend calls go through `apiFetch` in lib/api.ts, which reports a 401 (session ended) to AuthContext
- context/AuthContext.tsx: the signed-in user (`/auth/me`), logout, and sending the user to /login when a session ends (it clears the stale cookie first, or proxy.ts would loop). Sign-in and sign-out do a full page load (`loadPage`) so no account's data stays in memory
- proxy.ts (Next 16's middleware): no session cookie → /login?next=…; a cookie on /login or /register → /. Pages only (never /api: it would buffer upload bodies). It only checks that a cookie exists; the backend decides whether it is valid
- All upload, streaming and delete logic lives in context/DocumentsContext.tsx (wrapped around the app in app/layout.tsx, inside AuthProvider); it loads documents once the user is known
- Upload paths, chosen by NEXT_PUBLIC_VERCEL_BLOB_UPLOADS:
  - Default: multipart POST to /documents/upload/stream
  - Blob mode: browser → Vercel Blob (token from app/api/document-uploads/route.ts, which checks the session with the backend and only signs paths in the user's own folder `documind/uploads/{user_id}/`, lib/upload-paths.ts) → POST /documents/upload/stream/blob (refuses any path outside that folder)
- Backend: FastAPI in backend/app/
  - main.py: endpoints, validation; extraction.py: page-by-page text extraction (pypdf, python-docx, utf-8)
  - auth.py: accounts. POST /auth/register, /auth/login, /auth/logout, GET /auth/me; Argon2id hashing (argon2-cffi), sessions in the `documind_session` cookie (httpOnly, Secure, SameSite=Lax; only the token's SHA-256 is stored; 14 days sliding, renewed at most every 5 minutes by SessionCookieMiddleware, which also works on streamed responses), login and registration rate limits (client IP from X-Vercel-Forwarded-For only on Vercel), CSRF check `require_trusted_origin` (ALLOWED_ORIGINS, falling back to CORS_ORIGINS)
  - limits.py: per-user daily limits (DAILY_UPLOAD_LIMIT 20, DAILY_CHAT_LIMIT 100 per UTC day; 0 = none), counted atomically in `daily_usage` before any AI call; over the limit → 429 `{"detail", "code": "daily_limit"}`
  - claim_documents.py: `python -m app.claim_documents --email … [--dry-run]` gives documents without an owner (from before accounts) to one account
  - services/ai_calls.py: the shared AI provider layer (OpenAI SDK; OpenRouter by default, or any OpenAI-compatible API with AI_BASE_URL + AI_API_KEY; the OpenRouter key is never sent to another URL): the one client, the error types, error mapping and retries (`call_with_retries`, `stream_with_retries`)
  - Four services call OpenRouter, all through ai_calls.py: services/summarizer.py (streamed summary), services/chat.py (streamed answer), services/embeddings.py (chunk and question embeddings), services/llm.py (plain completion: query rewrite, eval judge). summarizer.py re-exports the error types, so `from app.services.summarizer import AIServiceError` still works
  - services/query_rewrite.py: rewrites a follow-up question into a standalone one before retrieval (one model call, only when there is history). QUERY_REWRITE_ENABLED is off by default
  - services/retrieval.py: chat retrieval. RETRIEVAL_MODE=hybrid (default: vector search + Postgres full-text search, merged with reciprocal rank fusion) or vector (vector search only)
  - database.py: SQLAlchemy Core queries on `documents` and `chunks`; every function that reads or writes documents takes a required keyword `owner_user_id`. Postgres + pgvector only (Neon in production, docker-compose.yml locally); DATABASE_URL is required, there is no SQLite fallback. At startup `check_database()` only reads: the app refuses to start unless the database is at the newest Alembic revision and the embedding column matches EMBEDDING_DIMENSIONS
  - Schema: Alembic migrations in backend/migrations/versions/ (hand-written SQL). app/schema_check.py compares a database with a reference snapshot (migrations/snapshots/); app/migrate.py runs migrations from code (tests, evals)
- Upload jobs and the worker (Steps 3+4): `INGESTION_MODE=inline|queue` (default inline; checked at startup).
  - jobs.py: the `processing_jobs` table (pending → processing → completed | failed), the limit of 3 running uploads per user (`MAX_ACTIVE_JOBS`, counted under a per-user advisory lock; a job unchanged for 15 minutes is dead: marked failed `timed_out` when its owner uploads or asks for it, never counted), claim/retry/fail helpers. Both modes create a job: inline ones only count toward the limit (429 `{"detail", "code": "upload_limit"}`)
  - queue mode: the two upload endpoints store the file (job row, or the Blob path), enqueue and return `202 {"job_id"}`; `GET /jobs/{id}` (owner-scoped, 404 for others) shows status, error and the summary. The frontend sees the 202 and polls, so it needs no second setting
  - ingestion.py: `process_job` is what the Celery task runs: `claim_job` → the Phase 2 pipeline (read file, summary + embeddings in parallel, `save_document_with_chunks(job_id=…)` saves document, chunks and the job's end in one transaction, so a duplicate delivery saves nothing) → on error `describe_failure` (safe code/message). Only timeouts, connection errors and rate limits are retried (3 attempts, 10 s then 20 s or `Retry-After`); the full error is logged, never stored. The worker deletes the temporary Blob when the job ends
  - worker.py: the Celery app (Redis broker, queue `documind`, acks_late, prefetch 1, no results backend, database engines reset in each worker process). The AI client is made per event loop (`ai_calls._get_async_client`) because each job runs in its own `asyncio.run`
  - docker-compose.yml: `redis` and `worker` (backend/Dockerfile) next to `db`. Vercel stays `inline`, needs neither
- Streaming uses SSE events: {"type": "delta" | "done" | "error", ...}. A document is saved only after the stream finishes successfully.
- Provider errors are mapped (in services/ai_calls.py): TimeoutError→504, ConnectionError→503, AIServiceNotConfiguredError (OPENROUTER_API_KEY missing)→503, AIRateLimitError→429, AIOutOfCreditError (provider 402: "The AI service is out of credit. Please try again later.", logged as an error, never retried; a subclass of AIServiceError)→503, AIServiceError→502
- Retry rules (services/ai_calls.py): the SDK's own retries are off (`max_retries=0`). Timeouts (including 408), connection errors, 429 and 5xx are retried up to 2 times with exponential backoff and jitter; `Retry-After` is honored up to 60 s (longer: no retry, the AIRateLimitError carries `retry_after`); other 4xx errors are never retried. Every retry logs an `ai_retry` warning. A streamed call (summary, chat) is retried only if it fails before its first token; once text has reached the user it is never retried. Tests skip the waits (`retry_waits` fixture in tests/conftest.py)
- The chat page calls `POST /chat/stream` (sources → delta… → done/error) and shows citations; chat answers always stream directly from the API.

## In progress: Phase 3 (production readiness)
- Plan: docs/phase3-plan.md (scope, decisions, steps, risks). Its "Current Scope" section (revision 5) overrides the rest: Steps 3+4 as one part (Redis, Celery worker, queue ingestion, INGESTION_MODE=queue|inline, default inline; no content_hash), 6 (Redis cache only), 7 (Flower, /metrics, the existing logs; no OpenTelemetry or Langfuse), 10 (one Locust scenario with the fake LLM, one short report). Dropped: 4b, 5, 8, 11. Azure (9) is deferred; the full setup runs locally with Docker Compose, and Vercel keeps INGESTION_MODE=inline.
- Way of working (replaces "show each diff and wait for OK"):
  - Each part on its own branch (phase3/<part>); commit as you go without waiting for the user.
  - Stop only at the end of a part, or for a real decision or anything risky (production, secrets, deleting data, paid AI calls). At the end of a part: a short summary, the test results and how to try it.
  - Tests for the important behavior; mutation checks (break the code on purpose, confirm a test fails) only for security-related code.
  - All rules about secrets, paid calls, production and new dependencies stay the same. Merge into main and push only when the user says so.
- Rule: existing code changes only when Phase 3 requires it. Approved exceptions: the upload flow moves to the job queue (Steps 3+4, INGESTION_MODE=queue only), and every query is scoped by owner (done in Step 2).
- Part in progress: Steps 3+4 on branch phase3/queue-ingestion (done: jobs table, worker, INGESTION_MODE, 202 upload and polling, Compose; documented in README "Background worker"). Not merged yet. **Before merging/deploying: production needs migration 0004 first (backup branch, alembic upgrade head, then deploy); remind the user before any merge.**
- Done: Step 0 (shared AI call wrapper, logged errors, lint, docs), Step 1 (audit), Step 2 (cookie option A with the /api rewrite, Alembic, accounts, ownership, daily limits, Blob folders per user, sign-in pages). All merged into main; production (Neon project broad-feather-89382117; documind-rag-dev is no longer used) is migrated to 0003_document_owner. schema_check lists Postgres 18 named NOT NULL constraints as differences; a separate small fix is pending.
- Chat streaming stays exactly as it is: `POST /chat/stream` streams over SSE straight from the API and never goes through a queue.

## Phase 2 (RAG), done
- Goal: turn the chat page into real "chat with your docs" with citations (file + page), plus an eval suite of 50+ questions
- Production database is Neon Postgres, so the vector store is pgvector. Local dev uses Postgres + pgvector in Docker.
- Design and results: docs/design.md
- Follow-up questions: 8 eval questions (category follow_up) and query rewriting are built; rewriting stays off until it is measured with the production chat model (docs/design.md, "Follow-up questions and query rewriting").
- Step 15 (hybrid search) is done and hybrid is the default retrieval mode; results and the decision are in docs/design.md ("Step 15 results and decision"). Arabic normalization was skipped. Step 17 (reranking) has not started.

## Conventions
- All calls to OpenRouter (chat and embeddings) live in backend/app/services/ — never from main.py or the frontend — and go through services/ai_calls.py (`call_with_retries` / `stream_with_retries`); never call the client directly or add retries elsewhere
- Never put API keys in the frontend
- Ownership: every query that touches documents, chunks or usage is scoped by the signed-in user's `owner_user_id`, including both chat searches; the owner parameter is a required keyword argument so a forgotten owner fails loudly. Another user's document is a 404, like a missing one (never 403, never a different message)
- Every endpoint except /, /health, /auth/register, /auth/login and /auth/logout depends on `current_user`; every POST/DELETE also on `require_trusted_origin`. tests/test_isolation.py reads all routes from the OpenAPI schema, so a new route without the session check fails the tests
- Never log passwords, session tokens or full emails (failed sign-ins log the email's domain only)
- Tests mock every AI call (see tests/test_api.py with monkeypatch). No real API calls in pytest.
- Tests run against the Docker test database (TEST_DATABASE_URL, name must end in `_test`); tests/conftest.py rebuilds it from the migrations once per run, empties its tables before each test and refuses to run against DATABASE_URL.
- Most tests act as one signed-in user (`default_user` fixture, DEFAULT_USER_ID in tests/default_user.py) with the session and CSRF checks replaced. Tests of sign-in, CSRF and isolation are marked `pytest.mark.real_auth` and go through the real checks. Use https://testserver as the TestClient base URL when a test needs the Secure cookie.
- New LLM/embedding code must log model, prompt/completion tokens, and estimated cost
- New code must log errors instead of swallowing them (no bare `except Exception: pass`)
- CPU-heavy or blocking work (PDF parsing, embedding batches, DB writes) must not block the event loop: use run_in_threadpool or async clients
- Every schema change is a new Alembic migration (never DDL in app code), tested with upgrade → downgrade → upgrade (tests/test_migrations.py). Never edit a migration that has run on production; add a new one. tests/legacy_startup_schema.py is a frozen record of the pre-Alembic schema: never change it
- **Never run `alembic downgrade` on production** (the baseline's downgrade drops all tables); production rollback uses the Neon backup branch taken before the migration

## Commands
- Local database: docker compose up -d (stop: docker compose stop; status: docker compose ps; reset: docker compose down -v)
- Backend packages: cd backend && pip install -r requirements-dev.txt
- Backend tests (database must be running): cd backend && python -m pytest
- Migrations (from backend/): python -m alembic upgrade head · python -m alembic current · python -m app.schema_check (read-only; before stamping a pre-Alembic database: python -m alembic stamp 0001_baseline). Production migrations use MIGRATION_DATABASE_URL (Neon direct URL), on a Neon branch first, and only with the user's OK
- Documents without an owner (from before accounts): cd backend && python -m app.claim_documents --email you@example.com --dry-run (then without --dry-run; only with the user's OK on production)
- Backend dev server: cd backend && uvicorn app.main:app --reload
- Queue mode locally: docker compose up -d --build (db + redis + worker), INGESTION_MODE=queue in backend/.env; worker logs: docker compose logs -f worker. The worker makes real AI calls with backend/.env's settings; for a free check blank AI_API_KEY for the worker (a compose override file) and jobs fail with ai_not_configured
- Frontend: npm run dev, npx tsc --noEmit, npm run lint (must pass with no errors), npm run build
- Evals: cd backend && python -m evals.run_evals --estimate-only (then run without it; default mode is retrieval only; --mode answers uses free models; --resume <run-id> continues a stopped run; --category follow_up runs one category; --query-rewrite on rewrites follow-ups with the free EVAL_REWRITE_MODEL). Never run for real without showing the estimate and getting OK.

## Known issues (don't fix unless asked)
- No password reset or email verification (no email sending); registration is open, with per-account daily limits
- Orphaned temporary blobs (uploads whose processing never finished) are never cleaned up (risk 8 in docs/phase3-plan.md)
- Deployment is two Vercel projects (frontend at the repo root, backend in `backend/`) plus Neon; see README "Deployment". In production, uploads must use Vercel Blob (Vercel Functions accept at most 4.5 MB per request) and one upload must finish within the function time limit (300 s on Hobby). Documents uploaded before RAG have no chunks until re-uploaded.
- Raw file bytes are stored in the DB but never read
- Arabic **PDF** extraction with pypdf came out badly broken in our test (stray control characters, split words, scrambled word order) and was not investigated. Arabic PDFs are not supported for now, and eval questions whose source is an Arabic PDF (`waha_library_ar.pdf`, `nursin_port_ar.pdf`) are expected to fail. Arabic TXT and DOCX extraction, Arabic questions and English–Arabic retrieval worked in our tests, but Arabic is not a supported language: Arabic normalization for full-text search was skipped (step 15), and with hybrid search, English questions about Arabic documents rank worse than with vector-only search.

## Avoid
- Don't change prompts without running evals before and after (this includes the query rewrite prompt)
- Don't fall back silently in AI code. A fallback must log, and in evals it must stop the run. Once, an empty query rewrite (a free reasoning model spent its whole reply on reasoning) silently fell back to the original question, and the eval report looked like a real "with rewriting" result while matching the no-rewrite run exactly. Check that an "after" run actually changed something before trusting it.
- Don't add dependencies without asking
- Don't rewrite working upload/summary code while adding RAG; extend it
