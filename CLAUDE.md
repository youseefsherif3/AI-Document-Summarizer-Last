# AI Document Summarizer — Chat with your docs

@AGENTS.md

## Architecture
- Frontend: Next.js. Pages: app/page.tsx (dashboard), app/documents/page.tsx (upload/list), app/chat/page.tsx (chat)
- All upload, streaming and delete logic lives in context/DocumentsContext.tsx (wrapped around the app in app/layout.tsx)
- Upload paths, chosen by NEXT_PUBLIC_VERCEL_BLOB_UPLOADS:
  - Default: multipart POST to /documents/upload/stream
  - Blob mode: browser → Vercel Blob (authorized by app/api/document-uploads/route.ts) → POST /documents/upload/stream/blob
- Backend: FastAPI in backend/app/
  - main.py: endpoints, validation; extraction.py: page-by-page text extraction (pypdf, python-docx, utf-8)
  - services/ai_calls.py: the shared OpenRouter layer (OpenAI SDK, base_url=https://openrouter.ai/api/v1): the one client, the error types, error mapping and retries (`call_with_retries`, `stream_with_retries`)
  - Four services call OpenRouter, all through ai_calls.py: services/summarizer.py (streamed summary), services/chat.py (streamed answer), services/embeddings.py (chunk and question embeddings), services/llm.py (plain completion: query rewrite, eval judge). summarizer.py re-exports the error types, so `from app.services.summarizer import AIServiceError` still works
  - services/query_rewrite.py: rewrites a follow-up question into a standalone one before retrieval (one model call, only when there is history). QUERY_REWRITE_ENABLED is off by default
  - services/retrieval.py: chat retrieval. RETRIEVAL_MODE=hybrid (default: vector search + Postgres full-text search, merged with reciprocal rank fusion) or vector (vector search only)
  - database.py: SQLAlchemy Core, one `documents` table. Postgres + pgvector only (Neon in production, docker-compose.yml locally); DATABASE_URL is required, there is no SQLite fallback
- Streaming uses SSE events: {"type": "delta" | "done" | "error", ...}. A document is saved only after the stream finishes successfully.
- Provider errors are mapped (in services/ai_calls.py): TimeoutError→504, ConnectionError→503, AIServiceNotConfiguredError (OPENROUTER_API_KEY missing)→503, AIRateLimitError→429, AIServiceError→502
- Retry rules (services/ai_calls.py): the SDK's own retries are off (`max_retries=0`). Timeouts (including 408), connection errors, 429 and 5xx are retried up to 2 times with exponential backoff and jitter; `Retry-After` is honored up to 60 s (longer: no retry, the AIRateLimitError carries `retry_after`); other 4xx errors are never retried. Every retry logs an `ai_retry` warning. A streamed call (summary, chat) is retried only if it fails before its first token; once text has reached the user it is never retried. Tests skip the waits (`retry_waits` fixture in tests/conftest.py)
- The chat page calls `POST /chat/stream` (sources → delta… → done/error) and shows citations; chat answers always stream directly from the API.

## In progress: Phase 2 (RAG)
- Goal: turn the chat page into real "chat with your docs" with citations (file + page), plus an eval suite of 50+ questions
- Production database is Neon Postgres, so the vector store is pgvector. Local dev uses Postgres + pgvector in Docker.
- All Phase 2 work happens on the branch feature/rag, one small commit per step
- Once the plan is approved it will be saved in docs/design.md. After that, implement ONE step at a time, then stop and show the diff.
- Follow-up questions: 8 eval questions (category follow_up) and query rewriting are built; rewriting stays off until it is measured with the production chat model (docs/design.md, "Follow-up questions and query rewriting").
- Step 15 (hybrid search) is done and hybrid is the default retrieval mode; results and the decision are in docs/design.md ("Step 15 results and decision"). Arabic normalization was skipped. Step 17 (reranking) has not started.

## Conventions
- All calls to OpenRouter (chat and embeddings) live in backend/app/services/ — never from main.py or the frontend — and go through services/ai_calls.py (`call_with_retries` / `stream_with_retries`); never call the client directly or add retries elsewhere
- Never put API keys in the frontend
- Tests mock every AI call (see tests/test_api.py with monkeypatch). No real API calls in pytest.
- Tests run against the Docker test database (TEST_DATABASE_URL, name must end in `_test`); tests/conftest.py empties its tables before each test and refuses to run against DATABASE_URL.
- New LLM/embedding code must log model, prompt/completion tokens, and estimated cost
- New code must log errors instead of swallowing them (no bare `except Exception: pass`)
- CPU-heavy or blocking work (PDF parsing, embedding batches, DB writes) must not block the event loop: use run_in_threadpool or async clients

## Commands
- Local database: docker compose up -d (stop: docker compose stop; status: docker compose ps; reset: docker compose down -v)
- Backend packages: cd backend && pip install -r requirements-dev.txt
- Backend tests (database must be running): cd backend && python -m pytest
- Backend dev server: cd backend && uvicorn app.main:app --reload
- Frontend: npm run dev, npx tsc --noEmit, npm run lint (must pass with no errors), npm run build
- Evals: cd backend && python -m evals.run_evals --estimate-only (then run without it; default mode is retrieval only; --mode answers uses free models; --resume <run-id> continues a stopped run; --category follow_up runs one category; --query-rewrite on rewrites follow-ups with the free EVAL_REWRITE_MODEL). Never run for real without showing the estimate and getting OK.

## Known issues (deliberately out of scope for Phase 2 — don't fix unless asked)
- No authentication; one shared database for all users (Phase 3: security)
- Blob upload authorization only checks Origin/Host headers; orphaned blobs are never cleaned up
- Deployment is two Vercel projects (frontend at the repo root, backend in `backend/`) plus Neon; see README "Deployment". In production, uploads must use Vercel Blob (Vercel Functions accept at most 4.5 MB per request) and one upload must finish within the function time limit (300 s on Hobby). Documents uploaded before RAG have no chunks until re-uploaded.
- Raw file bytes are stored in the DB but never read
- Arabic **PDF** extraction with pypdf came out badly broken in our test (stray control characters, split words, scrambled word order) and was not investigated. Arabic PDFs are not supported for now, and eval questions whose source is an Arabic PDF (`waha_library_ar.pdf`, `nursin_port_ar.pdf`) are expected to fail. Arabic TXT and DOCX extraction, Arabic questions and English–Arabic retrieval worked in our tests, but Arabic is not a supported language: Arabic normalization for full-text search was skipped (step 15), and with hybrid search, English questions about Arabic documents rank worse than with vector-only search.

## Avoid
- Don't change prompts without running evals before and after (this includes the query rewrite prompt)
- Don't fall back silently in AI code. A fallback must log, and in evals it must stop the run. Once, an empty query rewrite (a free reasoning model spent its whole reply on reasoning) silently fell back to the original question, and the eval report looked like a real "with rewriting" result while matching the no-rewrite run exactly. Check that an "after" run actually changed something before trusting it.
- Don't add dependencies without asking
- Don't rewrite working upload/summary code while adding RAG; extend it
