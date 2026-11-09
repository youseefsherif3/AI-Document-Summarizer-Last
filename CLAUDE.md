# AI Document Summarizer — Chat with your docs

@AGENTS.md

## Architecture
- Frontend: Next.js. Pages: app/page.tsx (dashboard), app/documents/page.tsx (upload/list), app/chat/page.tsx (chat)
- All upload, streaming and delete logic lives in context/DocumentsContext.tsx (wrapped around the app in app/layout.tsx)
- Upload paths, chosen by NEXT_PUBLIC_VERCEL_BLOB_UPLOADS:
  - Default: multipart POST to /documents/upload/stream
  - Blob mode: browser → Vercel Blob (authorized by app/api/document-uploads/route.ts) → POST /documents/upload/stream/blob
- Backend: FastAPI in backend/app/
  - main.py: endpoints, text extraction (pypdf, python-docx, utf-8), validation
  - services/summarizer.py: the only place that calls OpenRouter (OpenAI SDK, base_url=https://openrouter.ai/api/v1)
  - database.py: SQLAlchemy Core, one `documents` table. Postgres + pgvector only (Neon in production, docker-compose.yml locally); DATABASE_URL is required, there is no SQLite fallback
- Streaming uses SSE events: {"type": "delta" | "done" | "error", ...}. A document is saved only after the stream finishes successfully.
- Provider errors are mapped: TimeoutError→504, ConnectionError→503, AIServiceNotConfiguredError (OPENROUTER_API_KEY missing)→503, AIRateLimitError→429, AIServiceError→502
- The chat page does NOT call the model yet; it only displays upload summaries.

## In progress: Phase 2 (RAG)
- Goal: turn the chat page into real "chat with your docs" with citations (file + page), plus an eval suite of 50+ questions
- Production database is Neon Postgres, so the vector store is pgvector. Local dev uses Postgres + pgvector in Docker.
- All Phase 2 work happens on the branch feature/rag, one small commit per step
- Once the plan is approved it will be saved in docs/design.md. After that, implement ONE step at a time, then stop and show the diff.

## Conventions
- All calls to OpenRouter (chat and embeddings) live in backend/app/services/ — never from main.py or the frontend
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
- Frontend: npm run dev, npx tsc --noEmit, npm run build
- Evals: cd backend && python -m evals.run_evals --estimate-only (then run without it; default mode is retrieval only; --mode answers uses free models; --resume <run-id> continues a stopped run). Never run for real without showing the estimate and getting OK.

## Known issues (deliberately out of scope for Phase 2 — don't fix unless asked)
- No authentication; one shared database for all users (Phase 3: security)
- Blob upload authorization only checks Origin/Host headers; orphaned blobs are never cleaned up
- Deployment is two Vercel projects (frontend at the repo root, backend in `backend/`) plus Neon; see README "Deployment". In production, uploads must use Vercel Blob (Vercel Functions accept at most 4.5 MB per request) and one upload must finish within the function time limit (300 s on Hobby). Documents uploaded before RAG have no chunks until re-uploaded.
- Raw file bytes are stored in the DB but never read
- Arabic **PDF** extraction with pypdf came out badly broken in our test (stray control characters, split words, scrambled word order) and was not investigated. Arabic PDFs are not supported for now, and eval questions whose source is an Arabic PDF (`waha_library_ar.pdf`, `nursin_port_ar.pdf`) are expected to fail. Arabic TXT and DOCX extraction, Arabic questions and English–Arabic retrieval worked in our tests.
- `npm run lint` fails on code from the original import, not changed in Phase 2: `app/page.tsx` (dashboard) calls a setState-ing function directly in `useEffect` (error: `react-hooks/set-state-in-effect`), and `components/documents/FileCard.tsx` imports `LoaderCircle` without using it (warning). `tsc --noEmit` and `npm run build` pass.

## Avoid
- Don't change prompts without running evals before and after
- Don't add dependencies without asking
- Don't rewrite working upload/summary code while adding RAG; extend it
