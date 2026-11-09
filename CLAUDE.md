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
  - database.py: SQLAlchemy Core, one `documents` table. Postgres (Neon) when DATABASE_URL is set, SQLite otherwise
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
- New LLM/embedding code must log model, prompt/completion tokens, and estimated cost
- New code must log errors instead of swallowing them (no bare `except Exception: pass`)
- CPU-heavy or blocking work (PDF parsing, embedding batches, DB writes) must not block the event loop: use run_in_threadpool or async clients

## Commands
- Backend tests: cd backend && python -m pytest
- Backend dev server: cd backend && uvicorn app.main:app --reload
- Frontend: npm run dev, npx tsc --noEmit, npm run build
- Evals (once added): cd backend && python -m evals.run_evals

## Known issues (deliberately out of scope for Phase 2 — don't fix unless asked)
- No authentication; one shared database for all users (Phase 3: security)
- Blob upload authorization only checks Origin/Host headers; orphaned blobs are never cleaned up
- Deployment docs disagree (README says Render + SQLite disk; render.yaml uses Neon; backend/vercel.json also exists)
- Raw file bytes are stored in the DB but never read

## Avoid
- Don't change prompts without running evals before and after
- Don't add dependencies without asking
- Don't rewrite working upload/summary code while adding RAG; extend it
