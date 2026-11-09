# AI Document Summarizer

Next.js frontend and FastAPI backend for uploading PDF, DOCX, and TXT files, streaming AI summaries through OpenRouter, and saving completed documents in SQLite.

## Local development

1. Create `.env.local` from `.env.example` and set `NEXT_PUBLIC_API_URL=http://localhost:8000`.
2. Create `backend/.env` from `backend/.env.example` and set your OpenRouter key and model.
3. Start the API from `backend/` with `uvicorn app.main:app --reload`.
4. In the project root, run `npm run dev`.

The default SQLite database is `backend/documents.db`. Set `AI_DOCUMENTS_DB` to choose another path. Completed summaries are persisted only after the stream ends successfully; canceling or a provider failure leaves no partial document record.

## Checks

- Frontend types: `npx tsc --noEmit`
- Frontend production build: `npm run build`
- Backend tests: from `backend/`, run `python -m pytest`

## Deployment

The frontend can be imported into Vercel as a standard Next.js project. Set `NEXT_PUBLIC_API_URL` to the public URL of the deployed API, then deploy the frontend. This variable is public by design; never put provider credentials in the frontend.

`render.yaml` prepares a separate FastAPI service with a persistent disk mounted at `/var/data`. During service setup, provide `OPENROUTER_API_KEY` and set `CORS_ORIGINS` to the exact Vercel origin (for example, `https://your-app.vercel.app`). Add the deployed API URL to Vercel as `NEXT_PUBLIC_API_URL` and redeploy. Confirm `/health`, upload/streaming, and refresh persistence after both services are live.

The API service uses one SQLite database on a persistent disk. Keep it at one instance; SQLite on a mounted disk is not suitable for horizontally scaled or multi-region API instances. Back up the disk before changing providers or storage.
