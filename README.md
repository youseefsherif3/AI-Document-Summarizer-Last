# AI Document Summarizer

Next.js frontend and FastAPI backend for uploading PDF, DOCX, and TXT files, streaming AI summaries through OpenRouter, and saving completed documents in PostgreSQL with pgvector.

## Local development

1. Start the local database (needs Docker Desktop), from the project root: `docker compose up -d`. It creates a `docsum` database for development and a `docsum_test` database for tests. Data is kept in a Docker volume, so it survives restarts.
2. Create `.env.local` from `.env.example` and set `NEXT_PUBLIC_API_URL=http://localhost:8000`.
3. Create `backend/.env` from `backend/.env.example` and set your OpenRouter key and model. `DATABASE_URL` and `TEST_DATABASE_URL` in the example already point to the Docker databases.
4. Install the backend packages from `backend/`: `pip install -r requirements-dev.txt` (includes the test tools).
5. Start the API from `backend/` with `uvicorn app.main:app --reload`.
6. In the project root, run `npm run dev`.

`DATABASE_URL` is required; the API does not start without it. Completed summaries are persisted only after the stream ends successfully; canceling or a provider failure leaves no partial document record.

Day-to-day database commands (from the project root):

- Stop: `docker compose stop` · Start again: `docker compose start`
- Check it's running and healthy: `docker compose ps`
- Reset both databases (deletes all local documents): `docker compose down -v`, then `docker compose up -d`

## Checks

- Frontend types: `npx tsc --noEmit`
- Frontend production build: `npm run build`
- Backend tests: start the database, then from `backend/` run `python -m pytest`. Tests use `TEST_DATABASE_URL` (a separate `*_test` database whose tables are emptied before each test) and never touch `DATABASE_URL`.

## Deployment

The frontend can be imported into Vercel as a standard Next.js project. Set `NEXT_PUBLIC_API_URL` to the public URL of the deployed API, then deploy the frontend. This variable is public by design; never put provider credentials in the frontend.

`render.yaml` prepares a separate FastAPI service with a persistent disk mounted at `/var/data`. During service setup, provide `OPENROUTER_API_KEY` and set `CORS_ORIGINS` to the exact Vercel origin (for example, `https://your-app.vercel.app`). Add the deployed API URL to Vercel as `NEXT_PUBLIC_API_URL` and redeploy. Confirm `/health`, upload/streaming, and refresh persistence after both services are live.

The API service uses one SQLite database on a persistent disk. Keep it at one instance; SQLite on a mounted disk is not suitable for horizontally scaled or multi-region API instances. Back up the disk before changing providers or storage.
