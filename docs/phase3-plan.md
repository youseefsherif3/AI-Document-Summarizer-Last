# DocuMind — Phase 3 Implementation Plan

**Production Readiness & Observability**
**Document purpose:** Scope definition and implementation plan for review
**Project:** DocuMind — AI Document Summarizer & Chat with your Docs (RAG)
**Date:** November 2026
**Revision:** 5 (scope reduced to the production-project requirements; Steps 3 and 4 merged; monitoring decided; Azure deferred; `INGESTION_MODE`)

> **Important:** This document describes the agreed target implementation. It does not mean that the listed components have already been implemented.

## Current Scope (revision 5, 2026-11-10)

To save time, Phase 3 now builds only what the production project asks for: **background workers, queue-based ingestion, caching, monitoring and load testing** (§1). This section overrides anything later in the document that says otherwise.

| Step | Status |
|---|---|
| 0, 1, 2 | Done (merged into `main`; production migrated) |
| 3+4. Redis, Celery worker and queue ingestion | **Kept, as one part:** Redis and the worker in Docker Compose, the job table, queue ingestion and the `INGESTION_MODE` setting (below). `content_hash` is **dropped** |
| 6. Caching | **Kept, Redis cache only** (§6.2). The database review (indexes, connection pooling) is dropped |
| 7. Monitoring | **Kept, decided:** Flower for the worker, a `/metrics` endpoint, and the existing structured logs. No OpenTelemetry, no Langfuse |
| 10. Load testing | **Kept, small:** one Locust scenario against the fake LLM mode, and one short report |
| 4b. Backfill | **Dropped:** production has no documents, so nothing needs chunks |
| 5. LLM production layer | **Dropped** (persisted usage, fallback models, circuit breaker, graceful degradation) |
| 8. CI/CD | **Dropped** |
| 9. Production deployment on Azure | **Deferred.** No Azure resources are created in Phase 3 |
| 11. My AI Analytics | **Dropped** |

**Local setup instead of Azure.** The full queue setup runs locally with Docker Compose: Postgres + pgvector (existing), Redis and the Celery worker.

**`INGESTION_MODE` (`queue` or `inline`).** A backend setting chosen per deployment:

- `queue`: the upload stores the file, creates a job, enqueues it and returns `202`; the worker runs the ingestion (§6.1). Used locally with Docker Compose.
- `inline`: today's upload flow, unchanged (one streaming request with the summary over SSE). The Vercel deployment uses it, so it keeps working without a worker or Redis.

The default is `inline`, so a deployment that doesn't set it behaves as today. The backend's concurrent-upload limit applies in both modes; duplicate detection stays in the browser (no `content_hash`). How the frontend learns the mode (so it either reads the SSE stream or polls the job) is decided in Step 4, without a second setting that could disagree with the backend.

**Production database:** the Neon project `broad-feather-89382117` (the backend's `DATABASE_URL`). The older Neon project `documind-rag-dev` is no longer used.

## How to Use This Document

Review the scope and project decisions first. Once approved, implement the work incrementally, one step at a time, with one commit per step and the diff reviewed before each commit. Update the checklist based on actual implementation and test results, not merely because a topic has been discussed.

Read `CLAUDE.md` and `docs/design.md` (Phase 2) before starting. This plan builds on them and does not replace them.

---

## 1. Phase 3 Reference and Requirements

The goal of Phase 3 is to operate AI systems reliably, cost-effectively, and securely under real-world traffic. The development plan identifies the following areas:

- **System Design:** REST and gRPC APIs, queues such as Redis, Kafka, and SQS, caching, database design and indexing, and horizontal scaling.
- **LLM Production Concerns:** prompt caching, batching, cost tracking per request, fallback models, rate-limit handling, and latency budgets.
- **Reliability:** retries with backoff, idempotency, circuit breakers, and graceful degradation when a model fails.
- **Observability:** structured logs, tracing with OpenTelemetry, LLM tracing tools such as Langfuse and LangSmith, and dashboards for cost and quality.
- **Security:** prompt injection, PII handling, secrets management, and least privilege for agents.
- **Infrastructure:** Docker, CI/CD with GitHub Actions, in-depth knowledge of one cloud provider, and Kubernetes basics.
- **Production project:** take the Phase 2 application to production shape with **background workers, queue-based ingestion, caching, monitoring, and load testing**.

> **Scope note:** The plan identifies learning and implementation areas; it does not require using every available technology or deploying the application on Kubernetes. Technology choices should serve DocuMind's actual needs.

---

## 2. Current State (Phase 2 Baseline)

Phase 3 builds on a working, deployed application. The following already exists and must be **extended, not rewritten**:

| Area | Current state |
|---|---|
| Frontend | Next.js 16 + TypeScript, deployed on Vercel. Dashboard, Documents and Chat pages; responsive layout. |
| Backend | FastAPI, deployed on Vercel as serverless functions (300 s limit). |
| Database | PostgreSQL + pgvector: Docker locally (`docsum`, `docsum_test`, `docsum_eval`), Neon in production via the pooled connection string. |
| File uploads | Vercel Blob in production (to bypass Vercel's 4.5 MB request limit); direct multipart locally. |
| Upload flow | One streaming request: extract pages → chunk → summary (streamed to the user over SSE) in parallel with embeddings → save document + chunks in **one transaction** (all or nothing). |
| Chat | `POST /chat/stream` over SSE: hybrid search (vector + full-text, RRF) → top 8 chunks → cited answer. Optional query rewriting (off by default). |
| Upload limit | **3 files uploading at the same time**, enforced **only in the browser** (`MAX_CONCURRENT_UPLOADS` in `lib/constants.ts`, per tab). The backend has no limit. Duplicate detection (same name and size) is also browser-only. Stored documents are unlimited. |
| Cost tracking | One `ai_usage` log line per AI call: kind, model, outcome, prompt/completion/total tokens, `cost_usd`, `cost_source`, `duration_ms`. Cost comes from OpenRouter's returned `usage.cost`; when it is missing, an optional price table (`AI_PRICES_PER_MILLION_TOKENS`) estimates it and `cost_source` says `estimate` or `unknown`. The line is `key=value` text, **not JSON**, and is not stored in the database. |
| Error handling | Provider errors mapped to 504 / 503 / 429 / 502; missing key → 503. The mapping is **copied in four places** (`summarizer.py`, `embeddings.py`, `llm.py`, `chat.py`). **Two places swallow errors:** `_delete_temporary_blob` in `main.py` (`except Exception: pass`) and the `catch {}` in `app/api/document-uploads/route.ts` (returns 400 without logging). |
| Retries | The eval runner retries 429s with backoff and resumes. The app has no retry logic of its own, **but the OpenAI SDK retries silently**: `max_retries` is never set, so each call can make up to 3 attempts (60 s timeout each), and the logged `duration_ms` includes them with no retry count. `Retry-After` is kept only by `llm.py` and `chat.py` (`rate_limit_error()`); `summarizer.py` and `embeddings.py` drop it. |
| Prompt injection | Sources wrapped in `<source>` tags with tag-breaking neutralised; system prompt treats sources as data. Covered by eval questions. |
| Tests | 429 backend tests (pytest, all AI calls mocked) on real Postgres + pgvector, all passing; `tsc` and build pass. **`npm run lint` fails** (1 error in `app/page.tsx`, `react-hooks/set-state-in-effect`; 1 warning, unused `LoaderCircle` in `FileCard.tsx`). |
| Evals | 66 questions over 9 synthetic documents; retrieval mode is near-free thanks to an embedding cache. |
| Schema changes | `initialize_database()` runs on every startup (every Vercel cold start): `CREATE EXTENSION vector`, `create_all`, a manual `ALTER` for `documents.summary`, the embedding-dimension check, and `ADD COLUMN IF NOT EXISTS content_tsv` + GIN index. It needs rights to create extensions. **No migration tool yet.** |
| Stored data | `documents` stores the raw file bytes (`file_content`, never read) and the full `extracted_text`. This grows Neon storage, but makes re-indexing possible without the original file. |
| Documents before RAG | Documents uploaded before Phase 2 have summaries but **no chunks**, so chat cannot find them. The backfill script (Phase 2 step 9) was moved to Phase 3 and is now Step 4b below. |
| Endpoints | `GET /`, `GET /health`, `GET /documents` (all rows with full summaries, no paging), `GET /dashboard` (loads all rows; `processing_documents` hard-coded to 0), `POST /documents/upload/stream`, `POST /documents/upload/stream/blob`, `POST /chat/stream`, `DELETE /documents/{id}`. There is no `GET /documents/{id}` yet. |
| Indexes | `documents`: primary key only (no index on `created_at`). `chunks`: unique (`document_id`, `chunk_index`), btree on `document_id`, HNSW on `embedding`, GIN on `content_tsv`. |
| Process-wide singletons | One cached `AsyncOpenAI` client (`summarizer._get_async_client`, `@cache`) and one SQLAlchemy engine per process (default pool 5 + 10 overflow). Most model settings are read once at import; there is no central settings module. |
| Infrastructure | `docker-compose.yml` has Postgres only. No Dockerfile, no `.github/workflows`. |
| Docs drift | README says 56 eval questions and describes chat as vector-only; CLAUDE.md says `summarizer.py` is the only place that calls OpenRouter (four service files do, all inside `services/`); `OPENROUTER_CHAT_MODEL` and `RETRIEVAL_MIN_SIMILARITY` are missing from `backend/.env.example`. |
| Auth | **None.** All visitors share one database. Document IDs are sequential integers. |
| Git | All Phase 2 branches are merged into `main`. Production is expected to deploy from `main` (Vercel default; confirm in each project's Git settings). |

---

## 3. Agreed Project Decisions

| Area | Agreed choice / direction |
|---|---|
| Frontend | Next.js + TypeScript (unchanged) |
| Backend API | FastAPI (Python) on Vercel (unchanged) |
| Database | PostgreSQL + pgvector: Docker locally, Neon in production (unchanged): the Neon project `broad-feather-89382117`; `documind-rag-dev` is no longer used |
| Migrations | **Alembic** (new) |
| Authentication | **Decided:** own email + password (Argon2id or bcrypt), session in an `httpOnly` `Secure` cookie, no external provider (see §4.1). The cross-domain cookie approach is chosen at the start of Step 2. |
| Queue / background jobs | Redis + Celery |
| Cache | Redis, logically separate from the queue (separate key prefixes or DB numbers) |
| Worker + Redis hosting (production) | **Deferred** (revision 5). Locally, Redis and the worker run in Docker Compose; the Vercel deployment uses `INGESTION_MODE=inline` and needs neither. The earlier choice (Azure Container Apps + Azure Managed Redis, §4.2) is kept for later. |
| LLM observability | Langfuse (**dropped** in revision 5) |
| Tracing | OpenTelemetry (**dropped** in revision 5) |
| Monitoring | **Decided (revision 5):** Flower for the Celery worker, a `/metrics` endpoint, and the existing structured `key=value` logs (`ai_usage`, `ai_retry`, …) |
| Testing | Pytest + Locust (one scenario, with a fake LLM mode) |
| CI/CD | GitHub Actions (**dropped** in revision 5) |
| Cloud | Azure fundamentals are intentionally skipped because the user already knows Azure. Hosting the worker and Redis on Azure is deferred (revision 5). |

### Constraints

- **Existing code changes only when Phase 3 requires it.** Working Phase 2 code is extended, not rewritten or refactored for its own sake. Each change to existing code must name the Phase 3 requirement or risk (§13) it serves. There are two approved exceptions where existing code is deliberately reworked:
  1. **The upload flow moves to the job queue** (in `INGESTION_MODE=queue`; `inline` keeps today's flow), both the frontend (`context/DocumentsContext.tsx`: the SSE stream reader is replaced by job creation and status polling) and the backend (`POST /documents/upload/stream` and `/documents/upload/stream/blob` become a `202` upload that enqueues a job).
  2. **Every query is scoped by owner**, including chat search (`search_chunks`, `full_text_search_chunks`, `existing_document_ids`), `GET /documents`, `/dashboard` and `DELETE /documents/{id}`.

  Step 0 (§9) is preparation that Phase 3 requires; it changes no user-visible behavior.
- **Upload limit:** a user can have at most **3 files uploading at the same time**. Stored documents are unlimited. Do not reintroduce a limit on stored documents.
- Do not replace Neon, PostgreSQL, pgvector, Next.js or FastAPI, or rewrite working parts of the application, without a clear technical reason.
- **Chat streaming stays exactly as it is.** `POST /chat/stream` keeps streaming the answer token by token over SSE (`sources` → `delta`… → `done` / `error`), directly from the API, and **never goes through the queue or the worker**. Phase 3 may add things around it (owner scoping of the search, auth, usage records, retries before the first token, caching of the question embedding or retrieval results), but not change how the answer streams. **Only upload processing moves to the worker.** During upload the summary is shown when the job completes; streaming it from the worker is a possible later step (§4.3).
- Keep the Phase 2 rule: a document is saved only with its chunks, never half-indexed.
- Do not repeat Azure fundamentals or force a migration of the whole project to Azure.
- Kubernetes basics are part of the learning scope; running DocuMind on Kubernetes is not required.
- Redis + Celery is the starting point. Kafka, SQS and gRPC are not needed merely because they appear in the development plan.
- **No paid load tests without approval.** Load tests run against a fake LLM by default (§6.3).
- Every paid AI run (evals, load tests, real smoke tests) shows an estimate first and waits for approval, as in Phase 2.
- No silent fallbacks in AI code: every fallback is logged, and in evals it stops the run.

---

## 4. Decisions Required Before Implementation

### 4.1 Authentication (decided: own email + password)

Per-user data isolation, job ownership and per-user usage all depend on authentication, so it must exist before them.

**Decision:** our own email + password accounts in FastAPI and Postgres. **No external provider** (no Auth.js providers, Clerk, Google or GitHub sign-in).

**Rules:**

- **Password hashing:** Argon2id (preferred, `argon2-cffi`) or bcrypt. Never stored or compared in plain text. The hashing library is a new dependency and is approved in Step 2 before it is added.
- **Login rate limiting:** limit failed attempts per email and per client IP (for example 5 per 15 minutes, then a temporary block), and return `429` with `Retry-After`. Step 2 comes before Redis (Step 3), so the first version stores attempts in Postgres; it can move to Redis later.
- **One generic error** for a wrong email or a wrong password ("Email or password is incorrect"), with the same status and similar timing (when the email doesn't exist, the password is still checked against a dummy hash), so the response doesn't reveal which accounts exist. Registration doesn't reveal existing emails either, beyond what is unavoidable.
- **No passwords or tokens in logs**, traces or error messages: request bodies of auth endpoints are never logged, and session tokens are never logged in full.
- **Session in an `httpOnly`, `Secure` cookie.** The cookie holds an opaque random session token; the database stores only its hash, with the user, expiry and last use, so logout and revocation work (`sessions` table). The token is never readable by JavaScript and never stored in `localStorage`. **Decided:** sessions slide, lasting 14 days (`SESSION_DAYS`) from the last visit.
- **CSRF:** with cookie sessions, every state-changing request is protected (`SameSite` cookie plus an `Origin` check against the allowed origins, or a CSRF token where `SameSite=None` is unavoidable).
- FastAPI reads the cookie on every request and derives `user_id` from the session; a `user_id` sent by the frontend is never trusted.

**Cross-domain cookie problem.** The frontend and the backend are separate Vercel projects on different `*.vercel.app` domains. `vercel.app` is on the Public Suffix List, so the two are **different sites**: a cookie set by the backend is a third-party cookie from the frontend's point of view, and it cannot be shared through a parent domain. Options, to be chosen at the start of Step 2:

| Option | How | Pros | Cons |
|---|---|---|---|
| **A. Proxy the API through the frontend domain** | Next.js rewrites (for example `/api/backend/:path*` → the backend URL); the browser only talks to the frontend's domain, so the cookie is first-party (`SameSite=Lax`) | No custom domain needed; no third-party cookies; CORS no longer needed for the browser | Every request goes through the frontend; **SSE chat streaming through the rewrite must be tested** (no buffering, time limits); the backend sees the proxy, so client-IP for rate limiting comes from forwarded headers |
| **B. Custom domain for both** | e.g. `app.example.com` and `api.example.com`; cookie `Domain=.example.com`, `SameSite=Lax` | Clean, standard, same site; no proxy hop | Needs a domain the user owns and DNS setup on both Vercel projects |
| C. Cross-site cookie | `SameSite=None; Secure`, `fetch(..., {credentials: "include"})`, CORS with credentials | Works with the current domains, no proxy | Third-party cookies are blocked by Safari and Firefox by default and increasingly by Chrome: **unreliable**, and needs CSRF tokens |
| D. Bearer token in JavaScript | Token in memory or `localStorage`, `Authorization` header | No cookie issues | **Rejected:** breaks the `httpOnly` cookie rule; XSS can steal the token |

A or B are the realistic choices; C and D are listed only to record why they are not used.

**Decided: option A** (no custom domain). The browser calls only the frontend's own `/api/*`; `next.config.ts` rewrites it to the backend Vercel URL (`BACKEND_URL`, server-only, read at build time).

**Spike result (2026-11-10, throwaway branch `phase3/step-2-spike`, not merged):** free test endpoints sent timed SSE events (one every 200 ms) through the rewrite. No AI calls were made.

| Check | Local `next dev` | Local `next build` + `start` | Vercel previews |
|---|---|---|---|
| 30 events via `/api` | 201–217 ms apart (direct: 201–216) | 201–216 ms apart, no compression | 183–263 ms apart, none bunched (direct: 191–206) |
| 2-minute stream (600 events) | — | complete, largest gap 283 ms | complete in 121 s, largest gap 281 ms |
| `httpOnly` `Secure` cookie from the backend | stored on the frontend domain, hidden from JS, sent back | — | same |
| `Origin` reaching the backend | unchanged | — | unchanged (the frontend preview URL) |
| Client IP | not set locally | — | `X-Forwarded-For`, `X-Real-IP` and `X-Vercel-Forwarded-For` = the real IP; forged values are overwritten by Vercel |
| Request body | 10 MB multipart intact | — | 1 MB intact (Vercel's 4.5 MB function limit still applies, so production keeps using Blob) |
| `app/api/document-uploads` | still served by Next.js | — | still served by Next.js |

Conclusions: chat keeps streaming token by token through the rewrite, so the fallbacks aren't needed. Rate limiting reads the client IP from `X-Vercel-Forwarded-For` (falling back to the socket address locally). The paid end-to-end check (a real upload and chat question) is done in the preview rehearsal before production.

**Known gap: password reset by email is out of scope for now.** There is no email sending, so a forgotten password cannot be reset by the user; an admin resets it manually (a documented command) until reset by email is added. Email verification on sign-up is out of scope for the same reason.

**Existing data (decided):** documents uploaded before auth have no owner and are visible to nobody. After registering, the user assigns them to their account with a one-off command run by hand: `python -m app.claim_documents --email … --dry-run`, then without `--dry-run`. Registration never assigns documents.

### 4.2 Production hosting for Redis and the worker (deferred)

> **Deferred in revision 5.** Nothing in this section is built in Phase 3; it is kept for later. Production runs with `INGESTION_MODE=inline`.

Vercel functions stop after each response (at most 300 s on Hobby, 800 s on Pro), with no persistent processes and no built-in Redis, so **a Celery worker cannot run on Vercel**. Enqueuing from the API is fine there (one Redis write per request).

**Decision:**

| Component | Hosting |
|---|---|
| Frontend (Next.js) | Vercel, unchanged |
| API (FastAPI) | Vercel, unchanged. Moving it to Azure Container Apps is a **possible later step**, not part of this plan |
| Database | Neon Postgres + pgvector, unchanged |
| File storage | Vercel Blob, unchanged. The worker reads it with `BLOB_READ_WRITE_TOKEN` |
| Celery worker | **Azure Container Apps**: an app with no ingress, image in Azure Container Registry, scaled on queue length with KEDA's Redis scaler (scale to zero if idle cost matters) |
| Redis (queue + cache) | **Azure Managed Redis**, over TLS (`rediss://`). Queue and cache kept logically separate. (Microsoft is retiring Azure Cache for Redis in favor of Azure Managed Redis.) |

Secrets live in Container Apps secrets or Key Vault references; the registry is pulled with a managed identity; GitHub Actions deploys with Azure OIDC (a federated credential), with no long-lived keys. Every worker transaction crosses the internet to Neon, so the Azure region should be close to the Neon region.

**Azure details still needed from the user (before Step 9):**

- [ ] Subscription and resource group names
- [ ] Azure region (close to the Neon project's region; also needed: the Neon region)
- [ ] Monthly budget, and whether idle cost matters (worker scale-to-zero, Redis tier)
- [ ] Who can create the GitHub OIDC federated credential, and with which role scope
- [ ] Registry: a new Azure Container Registry, or an existing one
- [ ] Confirmation that Vercel Blob stays (moving files to Azure Blob would change the frontend upload path)

### 4.3 Upload experience (confirmed)

**Confirmed:** moving ingestion to a worker means the summary is **no longer streamed live during upload**. The frontend shows job status (`pending` → `processing` → `completed` / `failed`) by polling `GET /jobs/{job_id}`, and shows the whole summary when the job completes. Streaming the summary from the worker (via Redis pub/sub and SSE) is a possible later step, out of scope for now.

This applies to upload only. Chat answers keep streaming token by token from `POST /chat/stream`, unchanged (§3 Constraints).

---

## 5. Implementation Scope by Phase 3 Area

### 5.1 System Design

- Organize REST endpoints for documents, jobs, chat and analytics.
- Add a queue so document processing runs in workers instead of keeping the HTTP request open.
- Use Redis for queueing and caching, keeping the two responsibilities logically separate.
- Introduce **Alembic** and move schema changes (including the existing full-text column) into migrations.
- Review models, relationships, constraints and indexes based on actual query patterns, including the new ownership filters (`owner_user_id` must be indexed and part of every user-scoped query).
- Account for connection pooling and Neon's connection limits, now that workers also open connections. Keep the existing pooler safeguard (`prepare_threshold=None`).
- Keep the API and workers stateless so multiple instances can run, sharing only Postgres and Redis.

**Not required:** gRPC. REST + SSE is sufficient for DocuMind.

### 5.2 LLM Production Concerns

- **Extend** the existing `ai_usage` logging: add `user_id`, `request_id`, `feature`, `provider`, `retry_count` and `fallback_used`, and persist each record to an `llm_usage` table (logs alone are not queryable for dashboards).
- **Cost:** keep using OpenRouter's returned `usage.cost` as the source of truth. A configurable price table is only a fallback when cost is missing, with `cost_source` recorded.
- **Prompt caching:** the provider applies automatic prompt caching for long repeated prefixes. Keep the system prompt and stable content at the start of the prompt so it can be cached, and record cached-token counts when returned. This is distinct from the Redis cache.
- **Batching:** embeddings are already batched (64 per request). Look for other compatible workloads; do not batch latency-sensitive chat calls.
- **Fallback models:** define a primary and a fallback model per feature. Fall back only on transient provider failures (timeouts, 5xx, repeated 429), never on 4xx input errors. Record the reason.
- **Rate limits in the app:** honor `Retry-After`, use exponential backoff with jitter and a maximum retry count. Reuse the approach already proven in the eval runner.
- **Latency budgets:** define budgets for time-to-first-token in chat, job pickup time, and total ingestion time; measure P50/P95/P99.

### 5.3 Reliability

- Retry transient errors only, with backoff (in the app and in Celery tasks).
- **Idempotency:** a task delivered twice must not create a duplicate document or chunks: the worker saves the document and marks its job completed in one transaction, and skips a job that is already completed. (Revision 5: no content hash, so re-uploading the same file creates a second document, as today.)
- **Circuit breaker** for repeated provider failures (Closed → Open → Half-open). While open, fail fast with a clear message instead of waiting on timeouts.
- **Graceful degradation:** when the AI service is down, users can still list, open and delete documents and read existing summaries; new uploads are queued or fail clearly; chat shows a clear "AI unavailable" message.
- **Job states:** `pending`, `processing`, `completed`, `failed`, with attempts, a safe error code and timestamps.
- **Atomic ingestion:** the worker saves the document, summary and chunks in one transaction, as in Phase 2.

### 5.4 Observability

- Structured JSON logs with `request_id`, `user_id`, `job_id` and `document_id` where relevant. The existing `ai_usage` line becomes one of these.
- OpenTelemetry tracing across FastAPI → Redis queue → Celery worker → LLM/embedding calls → Postgres, with the trace context passed through the task.
- Langfuse traces for LLM generations: model, tokens, latency, cost, and links to retrieval (which chunks were used).
- **Quality signals:** feed the Phase 2 eval results into the same dashboards where practical (e.g. retrieval hit@k per release).
- An internal dashboard for cost, quality and performance (Langfuse plus a metrics backend), with time filters and defined metrics.
- Never log API keys, passwords, tokens, or full document contents by default.

### 5.5 Security

- Keep the existing prompt-injection defenses and their eval questions; add any new defenses with eval coverage.
- **Authentication, authorization and data isolation:** every document, chunk, job, usage record and chat search is scoped to the authenticated user in backend logic **and** in database queries (including the vector and full-text search).
- PII minimization: no document text or user content in logs or traces by default; redact where needed.
- Secrets in environment configuration (Vercel, worker host) or a secrets manager, never in code or the repo.
- Least privilege: the worker, API and CI each get only the credentials they need (e.g. separate database roles if practical, scoped Blob tokens).
- Existing known issues to address: Blob upload authorization (currently checks only Origin/Host) and orphaned blobs.

### 5.6 Infrastructure

- Docker Compose for local development: Postgres + pgvector (existing), Redis, the Celery worker.
- A Dockerfile for the worker (and the API if needed outside Vercel).
- **GitHub Actions** on every push and pull request:
  - backend tests with a `pgvector/pgvector` service container and Redis,
  - frontend `tsc`, lint and build,
  - Alembic migrations applied to a fresh database,
  - the Phase 2 **retrieval eval** as a regression gate (near-free with the embedding cache; requires an API key secret; can be limited to `main`).
- Separate development and production configuration.
- Kubernetes: learn the basics (Pods, Deployments, Services, ConfigMaps/Secrets, scaling); deploying DocuMind on it is not required.

---

## 6. Practical Production-Shape Requirements

These are the five practical project areas explicitly identified in the Phase 3 plan.

### 6.1 Background Workers and Queue-Based Ingestion

- Add Redis and a Celery worker to Docker Compose.
- `POST` upload: validate the file and the concurrent-upload limit, store the file (Blob in production, local storage in development), create a job, enqueue it, and return `202 Accepted` with the job ID.
- The worker runs the **full Phase 2 ingestion pipeline**: extract pages → chunk → summary and embeddings (in parallel) → save document, summary and chunks in one transaction → update the job → delete the temporary blob.
- `GET /jobs/{job_id}` returns status and, when completed, the document and summary. The frontend polls it and shows progress per file.
- Retries for transient errors only, with backoff and a maximum attempt count; idempotent by job (§5.3), so a retried or duplicated task does no duplicate work.
- Errors are recorded on the job with a safe message.
- The concurrent-upload limit (3 per user) moves to the backend (counted from the job table); the browser check stays only as a convenience. Duplicate detection stays in the browser (revision 5).

### 6.1b Backfill for documents without chunks

> **Dropped in revision 5:** production has no documents, so no document lacks chunks.

Documents uploaded before Phase 2 have summaries but no chunks, so chat cannot find them. A backfill script (Phase 2 step 9, moved here) indexes them:

- Selects documents with no rows in `chunks` (scoped to an owner once ownership exists).
- Re-extracts pages from the stored `file_content`, chunks them, embeds them, and saves the chunks in one transaction per document. The existing summary is kept; no summary call is made.
- Shows the document count and an estimated embedding cost first and waits for approval (`--estimate-only`, then `--yes`), like the eval runner.
- Idempotent: a document that already has chunks is skipped, so a stopped run can simply be run again.
- Logs every embedding call (`ai_usage`) and every failed document; a failure does not stop the other documents, and the summary at the end lists failures.
- Runs as a command (`python -m app.scripts.backfill_chunks`) and, after Step 4, can enqueue one worker task per document instead.
- Runs on a Neon branch first, then production.

### 6.2 Caching

Redis cache only (revision 5): the database review (indexes, connection pooling) is dropped. Candidate caches (choose based on measurement, not all at once):

| Cache | Key | Why |
|---|---|---|
| Question embeddings | model + normalized question text | Repeated questions skip an embedding call |
| Retrieval results | user + selected documents + question + retrieval settings | Repeated questions skip the search |
| Summaries | hash of the file bytes (computed in memory; no column) + model + prompt version | Re-uploading the same file skips a paid summary |

- Every key includes the settings that change the result (model, prompt version, retrieval mode).
- Define TTLs, and invalidate on document deletion and on settings or prompt changes.
- Cache entries are scoped per user where the data is user-owned; no cross-user leakage.
- Measure hits/misses and the effect on latency and cost.
- Postgres remains the durable source of truth; Redis can be flushed at any time without data loss.

### 6.3 Monitoring and Load Testing

**Monitoring (decided, revision 5):**

- **Flower** for the Celery worker (tasks, states, retries, workers), as a service in Docker Compose.
- **`/metrics`:** the app's own numbers, for example job counts by state, job durations, queue depth, cache hits and misses, and request counts. It shows system-wide numbers, so it is not a user endpoint: it stays off unless a metrics token is set, and requires that token (exact format chosen in Step 7; the isolation test learns about the route).
- **Logs:** the existing structured `key=value` lines (`ai_usage`, `ai_retry`, errors), plus job IDs where relevant.
- No OpenTelemetry, Langfuse, dashboards or alerts.

**Load testing (revision 5):**

- **Fake LLM mode** (a setting) that replaces chat, summary and embedding calls with fake responses with realistic delays and token counts, so the test measures the system (API, queue, worker, database) without spending money.
- **One Locust scenario** against the local Docker Compose setup with the fake LLM (for example: sign in, upload, poll the job, ask a chat question).
- **One short report** with response times (P50/P95), throughput, error rate and queue backlog, and any bottleneck found. No run against real models.

---

## 7. Optional Extension — My AI Analytics (Per-User Dashboard)

> **Dropped in revision 5** (was Step 11). Kept here for reference only.

> **This goes beyond the Phase 3 requirements**, which ask for dashboards for cost and quality (satisfied by the internal dashboard in §5.4). It is a large feature and is scheduled **last**, only after all required work is done.

Each authenticated user sees **their own usage data only**. One user must never be able to view another user's data.

**Security principle:** the backend derives the user's identity from the verified token and enforces user scoping in every endpoint and query. A `user_id` sent by the frontend is never trusted.

**Sections (keep the first version small):**

- **Usage overview:** number of AI requests, input/output tokens, estimated cost, average response time.
- **Cost over time** and by feature (summary, chat).
- **Performance:** P50/P95 latency, clearly labeled (API latency vs complete LLM operation).
- **Reliability:** success rate, failed requests, fallback usage.
- **Activity:** the user's recent requests with safe details (request ID, time, feature, model, tokens, cost, latency, status).

Not included for users: cache analytics, queue backlog, worker errors, full traces, or any system-wide numbers. Those belong to the internal dashboard.

Placeholder numbers are allowed during UI development only when clearly labeled as demo data. Once connected, all numbers come from real backend data.

---

## 8. Proposed API Contracts and Data

Initial proposals, to be confirmed after the architecture audit. Existing routes (`/documents`, `/chat/stream`, `/dashboard`) are kept or renamed only with a clear reason.

### Endpoints

| Method / Route | Purpose |
|---|---|
| `POST /auth/register` | Create an account (email + password); sets the session cookie |
| `POST /auth/login` | Check email + password, rate-limited, generic error; sets the session cookie |
| `POST /auth/logout` | Revoke the session and clear the cookie |
| `GET /auth/me` | The signed-in user (ID, email), or `401` |
| `POST /documents/upload` | Validate, store the file, create and enqueue a job; returns `202` + job ID |
| `GET /jobs/{job_id}` | Job status and result, owned by the authenticated user |
| `GET /documents` | Documents owned by the authenticated user |
| `GET /documents/{id}` | One owned document |
| `DELETE /documents/{id}` | Delete an owned document (chunks cascade) |
| `POST /chat/stream` | Unchanged SSE chat, scoped to the user's documents |
| `GET /analytics/overview` | (Optional, §7) per-user usage summary |
| `GET /analytics/activity` | (Optional, §7) per-user request history |

### Data entities

- **User:** ID, email (unique, stored lowercased), password hash, `created_at`.
- **Session:** hash of the session token, `user_id`, `created_at`, `expires_at`, `last_used_at`, `revoked_at`.
- **Login attempt:** email, client IP, time, success (for rate limiting; may move to Redis later).
- **Document:** existing table + `owner_user_id` (indexed). (`content_hash` dropped in revision 5.)
- **Chunk:** existing table; ownership via its document. Search queries join on the owner.
- **ProcessingJob:** `document_id`, `owner_user_id`, Celery task ID, status, attempts, error code, timestamps.
- **LLMUsage:** `user_id`, `document_id`, `request_id`, feature, provider, model, input/output tokens, cached tokens, cost, cost source, latency, status, retry count, fallback flag, `created_at`.
- **Trace references:** trace/span IDs linked to `request_id`, without copying sensitive content.

All schema changes are Alembic migrations, tested on a fresh database in CI and on a Neon branch before production.

---

## 9. Implementation Sequence

Each step: one branch or commit series, tests passing, diff reviewed, then merged.

| Step | Work |
|---|---|
| **0. Preparation** | **Done** (merged into `main`). No user-visible behavior change. (a) One shared OpenRouter error wrapper in `services/` replaces the four copies of the mapping, and keeps `Retry-After` for every 429 (summary and embeddings too). (b) Turn off the SDK's silent retries (`max_retries=0`) on the shared client, so later app-level retries are the only retries and are counted. (c) Stop the two swallowed errors: `_delete_temporary_blob` logs a failed delete; `app/api/document-uploads/route.ts` logs the error before returning 400. (d) Fix the lint error in `app/page.tsx` and the unused import in `FileCard.tsx`, so `npm run lint` passes (needed for CI). (e) Update README (66 questions, hybrid chat, all optional settings), CLAUDE.md (where OpenRouter is called, known issues) and `backend/.env.example` (`OPENROUTER_CHAT_MODEL`, `RETRIEVAL_MIN_SIMILARITY`). Tests: existing suite passes; new tests for the wrapper (each error type, `Retry-After` kept, client built with `max_retries=0`) and for the logged blob-delete failure. |
| **1. Architecture audit** | **Done** (revision 3 of this plan). Reviewed the code, CLAUDE.md and docs/design.md; §2 corrected; risks listed in §13. No code changes. |
| **2. Auth, migrations and ownership** | **Done** (merged into `main`; production migrated, see "Step 2 as built" below). `content_hash` moved to Step 4 (then dropped in revision 5). First choose the cross-domain cookie option (§4.1, A or B) and approve the hashing dependency. Add email + password auth per §4.1: `users` and `sessions` tables, register / login / logout / me endpoints, login rate limiting, generic login error, CSRF protection, no secrets in logs, login and sign-up pages. Tests: hashing, generic error, rate limit, cookie flags, logout revokes the session, no password or token in captured logs. Add Alembic and a baseline migration. Add `owner_user_id` and `content_hash`; scope every existing endpoint and search query to the user. Handle existing data. Tests that one user cannot read, search or delete another user's data. |
| **3+4. Redis, Celery worker and queue ingestion** (one part) | Redis and the worker in Docker Compose; job table and states; `GET /jobs/{id}`; `INGESTION_MODE` (`queue` or `inline`, default `inline`; see "Current Scope"). In `queue` mode, the full ingestion pipeline runs in the worker; `202` upload; frontend progress via polling; retries; idempotency by job (§5.3). `inline` keeps today's upload flow. In both modes: the concurrent-upload limit is enforced in the backend. No `content_hash`. |
| ~~**4b. Backfill**~~ | **Dropped** (revision 5): production has no documents. |
| ~~**5. LLM production layer**~~ | **Dropped** (revision 5). |
| **6. Caching** | Redis caches from §6.2 with TTL and invalidation; measure hits and impact. (The database review is dropped.) |
| **7. Monitoring** | Flower in Docker Compose, `/metrics` behind a token, job IDs in the existing logs (§6.3). |
| ~~**8. CI/CD**~~ | **Dropped** (revision 5). |
| ~~**9. Production deployment**~~ | **Deferred** (revision 5): no Azure resources in Phase 3. The full setup runs locally with Docker Compose; Vercel keeps `INGESTION_MODE=inline`. |
| **10. Load testing** | Fake LLM mode; one Locust scenario against the local Docker Compose setup; one short report (§6.3). |
| ~~**11. (Optional) My AI Analytics**~~ | **Dropped** (revision 5). |

### Step 2 as built (branch `phase3/step-2`)

| Area | What was built |
|---|---|
| Cookies | Option A: the browser calls only the frontend's `/api/*`, rewritten to `BACKEND_URL`. A Vercel build without `BACKEND_URL` fails. |
| Migrations | Alembic. `0001_baseline` matches the old startup schema exactly (checked against a frozen copy of the old code and a saved snapshot); `0002_auth_tables`; `0003_document_owner`. The app only checks the revision at startup. `python -m app.schema_check` verifies a database before it is stamped. |
| Accounts | Email + password, Argon2id; `httpOnly` `Secure` `SameSite=Lax` cookie, 14-day sliding sessions, only the token's hash stored, ended sessions deleted after a day; login limits (5 per email, 20 per IP in 15 minutes) and 5 registrations per IP per day; one generic login error; CSRF check on every POST/DELETE; no passwords, tokens or full emails in logs. |
| Ownership | `documents.owner_user_id`; every query (lists, dashboard, delete, uploads, both chat searches, the chat document check) takes the owner; another user's document is a 404. Existing documents are claimed with `app.claim_documents`. |
| Daily limits | 20 uploads and 100 chat questions per user per UTC day, counted atomically before any AI call; 429 with a clear message. |
| Blob | Upload tokens only for `documind/uploads/{user_id}/…`; the backend refuses blobs outside the user's folder. |
| Frontend | `/login`, `/register`, sign-out in the header (email in the header from 1024 px, in the sidebar below), `proxy.ts` redirects, a 401 sends the user to sign in without loops, `?next=` limited to same-site paths. |
| Also | Provider 402 (out of credit) → 503 with a clear message, logged, never retried. |
| Checks | Backend tests (isolation tests read every route from the OpenAPI schema), tsc, lint, build; browser checks against the test database at 320–1280 px; retrieval eval before and after identical (`20261110-180003-step2-before-main`, `20261110-180015-step2-after-owner-scoping`). |
| Production | Migrated straight to production (no rehearsal branch, after a Neon backup branch): `schema_check`, `stamp 0001_baseline`, `upgrade head`; now at `0003_document_owner`. `schema_check` also listed 12 Postgres 18 named NOT NULL constraints (on columns already marked not null); handling them is a separate small commit. Production had no documents, so `claim_documents` was not needed. Merged into `main` at `56cf29e`. |
| Not yet | The upload and chat checks in production wait until the OpenRouter account has credit again (it currently returns 402). |

---

## 10. Acceptance Criteria — Definition of Done

- [ ] Uploading a document returns quickly with a job ID; processing happens in a worker, and job states transition correctly.
- [ ] The worker runs the full ingestion pipeline; a document is never saved without its chunks.
- [ ] Retries do not cause duplicate documents, chunks or charges; only transient failures are retried.
- [ ] At most 3 files upload at the same time per user; stored documents are unlimited.
- [ ] Chat still streams over SSE and searches only the user's documents.
- [ ] A user cannot read, search, delete or see usage for another user's data, even by changing IDs in requests.
- [ ] All schema changes are Alembic migrations, applied in production.
- [ ] Caches work with TTL and invalidation; hits/misses and impact are measured; flushing Redis loses no data.
- [ ] Flower shows the worker's tasks; `/metrics` returns the app's numbers only with the token; logs contain no secrets or document contents.
- [ ] The full setup (Postgres, Redis, worker) runs locally with Docker Compose in `INGESTION_MODE=queue`, and the Vercel deployment keeps working in `INGESTION_MODE=inline`.
- [ ] One Locust scenario (fake LLM mode) has run, with a short report of real measurements.
- [ ] The Phase 2 eval suite still passes at or above its baseline.
- [x] The app makes no hidden retries: SDK retries are off, and every retry is the app's own and is counted. (Step 0)

---

## 11. Out of Scope for Now

- Rebuilding DocuMind from scratch or replacing Next.js, FastAPI, PostgreSQL, pgvector or Neon without a clear reason.
- Kafka, SQS or gRPC merely because they are mentioned in the plan.
- Moving the whole application to Azure, or deploying it on Kubernetes. Moving the API to Azure Container Apps is a possible later step, after Phase 3.
- (Revision 5) `content_hash` and backend duplicate detection; OpenTelemetry and Langfuse; more than one Locust scenario and any load test against real models; hosting the worker and Redis on Azure (Step 9, deferred); the backfill (Step 4b); the LLM production layer (Step 5: persisted usage, fallback models, circuit breaker, graceful degradation); CI/CD (Step 8); the database review in Step 6; My AI Analytics (Step 11).
- Moving chat into the queue.
- Streaming the summary live from the worker during upload.
- Showing mock numbers as real metrics.
- Password reset by email and email verification (known gap, §4.1); external sign-in providers.
- Showing system-wide costs, queue details or other users' data in any user-facing view.
- The remaining Phase 2 items (reranking, the full answers-mode baseline, few-shot citation fix) unless picked up separately.

---

## 12. Review and Approval Checklist

### Decisions
- [x] Authentication approach chosen (§4.1): own email + password, `httpOnly` `Secure` cookie session.
- [x] Cross-domain cookie option chosen (§4.1): A, the `/api` rewrite, confirmed by the spike.
- [x] Handling of existing documents without an owner decided: the `claim_documents` command, run by hand after registering.
- [x] Upload experience without live summary streaming accepted (§4.3); chat streaming unchanged.
- [x] Production hosting for Redis and the worker: deferred (revision 5); local Docker Compose, Vercel in `INGESTION_MODE=inline`.

### Processing and reliability
- [ ] Redis + Celery + worker.
- [ ] Queue-based ingestion with the full pipeline, job status and polling.
- [ ] Retries, backoff and idempotency.

### Data and security
- [x] Alembic migrations and ownership columns. (Step 2)
- [x] Authentication, authorization and data isolation. (Step 2)
- [ ] Redis caches with TTL and invalidation, measured.

### Delivery and verification
- [ ] Docker Compose with Postgres, Redis and the worker; `INGESTION_MODE` (`queue` / `inline`).
- [ ] Monitoring: Flower, `/metrics`, existing logs.
- [ ] One Locust scenario (fake LLM mode) and a short report.
- [ ] Phase 2 evals still at or above baseline.
- [ ] Documentation updated (README, CLAUDE.md, docs/design.md).

---

## 13. Risks Found in the Audit

Things in the current code that would make the queue, workers, auth or caching harder, with the fix and the step that applies it.

| # | Risk | Fix | Step |
|---|---|---|---|
| 1 | **Cached async OpenRouter client.** `summarizer._get_async_client()` is `@cache`d per process, so the `AsyncOpenAI` client is tied to the first event loop. Celery tasks that call `asyncio.run()` get a new loop each time and will fail with "event loop is closed". | Create the client per event loop (cache keyed by the running loop) or per task, and close it when the loop ends. Test: two `asyncio.run()` calls in a row both succeed. | 3 |
| 2 | **Module-level SQLAlchemy engine.** With Celery's pre-fork pool, child processes inherit the parent's open connections. The default pool (5 + 10 overflow per process) across API instances and workers can exceed Neon's connection limits. | Call `engine.dispose()` in Celery's `worker_process_init` signal; set explicit `pool_size` / `max_overflow` for API and worker; keep `prepare_threshold=None` and the pooled connection string. | 3 |
| 3 | **Schema code at startup.** `initialize_database()` runs DDL on every cold start and needs rights to create extensions. It would race with Alembic and conflicts with least-privilege database roles. | Alembic baseline migration that reproduces the current schema (extension, tables, indexes, `content_tsv`); then remove the startup DDL, keeping only a read-only check (embedding dimensions, Alembic revision is current). The app role no longer needs DDL rights. | 2 |
| 4 | **Silent SDK retries and copied error mapping.** The SDK retries up to twice per call, hidden from logs; adding app retries, fallback models or a circuit breaker on top would multiply attempts. The mapping is copied in four files. | One shared error wrapper and `max_retries=0` (Step 0); app-level retries are built once, in that wrapper (done in Step 0). Fallback and the circuit breaker are dropped (revision 5). | 0 |
| 5 | **Upload limit and duplicate check are browser-only.** Any client can bypass them. | Enforce 3 concurrent jobs per user in the backend (counted from the job table). Duplicate detection stays in the browser (`content_hash` dropped in revision 5). | 3+4 |
| 6 | **Sequential integer IDs.** Once auth exists, changing an ID in a request reaches another user's document unless every query is scoped. | Scope every query by `owner_user_id`, including both chat search legs and the `document_ids` check; return 404 (not 403) for other users' IDs; tests for each endpoint. | 2 |
| 7 | **Upload rework conflicts with "don't rewrite working upload code".** The queue requires replacing the SSE upload reader in `DocumentsContext.tsx` and the two streaming endpoints. | Approved exception (§3 Constraints), limited to the upload flow. The extraction, chunking, embedding and save functions are reused unchanged by the worker. | 4 |
| 8 | **Orphaned and silently undeleted blobs.** A failed worker or a swallowed delete error leaves temporary blobs behind. | Log failed deletes (Step 0); the worker deletes the blob when the job reaches a final state; a periodic cleanup task removes `documind/uploads/` blobs older than a set age with no active job. | 0, 4 |
| 9 | **No input size cap for the summary.** Very large documents are sent whole to the summary model; the model's error comes back as a 502. A worker removes the time limit but not this. | Cap the summary input by estimated tokens (e.g. summarize the first N tokens, or summarize per section and combine), and fail the job with a clear, safe error code when over the limit. Measured with evals before and after if the prompt changes. | 4 |
| 10 | **Cookies across two `vercel.app` domains, and Next.js 16.** The frontend and backend are different sites, so a backend session cookie is a third-party cookie (§4.1). If option A is chosen, Next 16 rewrites (with `cacheComponents` and experimental flags) must pass SSE chat through without buffering. | Choose option A or B at the start of Step 2. For A, a small spike first: chat SSE streams token by token through the rewrite, the cookie is set and sent, and the client IP reaches the backend (read `node_modules/next/dist/docs/` per AGENTS.md). | 2 |

Known issues from CLAUDE.md that matter for Phase 3: no auth (Step 2); Blob authorization that only compares Origin and Host headers, which any script can fake (Step 2: the upload-token route requires a signed-in user); orphaned blobs (risk 8); documents without chunks (§6.1b, dropped: production has none); the 4.5 MB request limit (still applies, Blob stays) and the 300 s limit (no longer applies to ingestion once it runs in the worker); the lint failure (Step 0); Arabic PDFs (expected failures in the eval gate, not a regression).

---

## Source and Scope Notes

The Phase 3 learning areas and production-project requirements come from the *Junior AI Engineer Development Plan*. The current-state baseline comes from the Phase 2 codebase and `docs/design.md`. The Redis + Celery choice, the API contracts, data entities, implementation order and acceptance criteria are implementation proposals agreed with the user. My AI Analytics is an optional extension beyond the plan's requirements.

**Next action:** Steps 0–2 are done and merged. Next is Steps 3+4 (Redis, Celery worker and queue ingestion), within the reduced scope in "Current Scope" at the top.
