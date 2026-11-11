# AI Document Summarizer

Upload PDF, DOCX and TXT files, get AI summaries streamed as they are written, and **chat with
your documents**: ask questions and get answers with numbered citations to the file and page
they came from. Each person signs in with an email and password and sees only their own
documents.

- **Frontend:** Next.js (`app/`, `components/`, `context/`, `lib/`).
- **Backend:** FastAPI (`backend/app/`), PostgreSQL with pgvector for document chunks and vector
  search, and OpenRouter for the summary, chat and embedding models.
- **Design and decisions:** [`docs/design.md`](docs/design.md). Notes for AI coding assistants:
  [`CLAUDE.md`](CLAUDE.md).

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (for the database)
- Python 3.13
- Node.js 20 or newer
- An [OpenRouter](https://openrouter.ai/) API key

## Run it from scratch

All commands start in the project root unless a step says otherwise.

### 1. Start the database

```bash
docker compose up -d
```

This starts Postgres with pgvector in a container and creates three databases: `docsum`
(development), `docsum_test` (tests) and `docsum_eval` (evals). Data is kept in a Docker volume,
so it survives restarts. Check that it shows `(healthy)`:

```bash
docker compose ps
```

### 2. Configure the backend

Copy `backend/.env.example` to `backend/.env`.

On Windows:

```bash
copy backend\.env.example backend\.env
```

On macOS or Linux:

```bash
cp backend/.env.example backend/.env
```

Open `backend/.env` and set `OPENROUTER_API_KEY`. The other values already work with the
Docker database: `DATABASE_URL` (required; the API does not start without it),
`TEST_DATABASE_URL`, the summary model (`OPENROUTER_MODEL`, default `openai/gpt-4o`), the
embedding model and the eval settings. `OPENROUTER_CHAT_MODEL` is optional; chat uses the
summary model if it is not set. The account settings (`SESSION_DAYS`, `DAILY_UPLOAD_LIMIT`,
`DAILY_CHAT_LIMIT`, `ALLOWED_ORIGINS`) are optional and have working defaults.

### 3. Install and start the backend

```bash
cd backend
python -m venv .venv
```

Activate the virtual environment. On Windows:

```bash
.venv\Scripts\activate
```

On macOS or Linux:

```bash
source .venv/bin/activate
```

Install the packages (`requirements-dev.txt` includes the app's packages plus the test and eval
tools), create the tables with the database migrations, and start the API:

```bash
pip install -r requirements-dev.txt
```

```bash
python -m alembic upgrade head
```

```bash
uvicorn app.main:app --reload
```

The API does not create or change tables itself: it checks at startup that the database is
migrated and refuses to start otherwise. If your local database was created before migrations
existed, see [Database migrations](#database-migrations).

The API runs on http://localhost:8000. http://localhost:8000/health should return
`{"status":"healthy"}`. Each AI call logs one `ai_usage` line with its model, tokens and cost.

### 4. Configure and start the frontend

In a second terminal, in the project root, copy `.env.example` to `.env.local` (the file name
starts with a dot). It sets `BACKEND_URL=http://127.0.0.1:8000`: the browser calls this app's
own `/api/*`, and Next.js forwards those requests to the backend.

On Windows:

```bash
copy .env.example .env.local
```

On macOS or Linux:

```bash
cp .env.example .env.local
```

Then install and start:

```bash
npm ci
```

```bash
npm run dev
```

Open http://localhost:3000. You are sent to the sign-in page: choose **Create one** and register
with any email and a password of 10 to 128 characters. Then upload a document on the
**Documents** page and ask about it on the **Chat** page.

## How it works

- **Upload:** the file is read page by page and split into chunks of about 800 tokens that
  never cross a page. The chunks are embedded (`openai/text-embedding-3-small`) while the
  summary streams; the document and its chunks are saved together only if both succeed.
- **Chat:** `POST /chat/stream` embeds the question and finds the best chunks with **hybrid
  search**: pgvector similarity search and Postgres full-text search (20 candidates each),
  merged with reciprocal rank fusion into the top 8 (`RETRIEVAL_MODE=vector` switches to vector
  search only). It then streams an answer that cites them as `[1]`, `[2]`… The chat page shows
  those citations as buttons linked to a sources list (file, page, snippet). Answers come only
  from the documents; if the answer isn't there, it says so.
- **AI calls:** every call to OpenRouter (summary, chat, embeddings) goes through
  `backend/app/services/ai_calls.py`. Timeouts (including 408), connection errors, rate
  limits (429) and provider errors (5xx) are retried up to 2 times with backoff, honoring
  `Retry-After`; each retry logs an `ai_retry` line. A streamed answer is never retried once
  text has been sent. When the OpenRouter account is out of credit (402), users get a clear
  503 message and the error is logged; it is never retried.
- **Accounts:** email and password (hashed with Argon2id). Signing in sets an `httpOnly`,
  `Secure` session cookie that lasts 14 days from the last visit (`SESSION_DAYS`). The browser
  only talks to the frontend's own `/api/*`, which Next.js forwards to the backend, so the
  cookie belongs to the frontend's domain. Every document, chunk and search belongs to one
  account; another user's document is "not found". Failed sign-ins are limited per email and
  per network, and requests that change something must come from the frontend (`Origin`
  check).
- **Daily limits:** each account can upload 20 documents and ask 100 chat questions per UTC
  day (`DAILY_UPLOAD_LIMIT`, `DAILY_CHAT_LIMIT`; `0` means no limit). Over the limit, the app
  shows "You've reached today's limit…". Registration is open and emails are not verified, so
  these limits cap what one account can spend.

## Tests

The database must be running. Backend tests use the separate `docsum_test` database (its tables
are emptied before each test) and make no real AI calls:

```bash
cd backend
python -m pytest
```

Frontend checks, from the project root:

```bash
npx tsc --noEmit
```

```bash
npm run lint
```

```bash
npm run build
```

## Evals

`backend/evals/` measures retrieval and answer quality on a fictional corpus of 9 documents
(English and Arabic) with 66 questions. Run from `backend/`. The runner always prints a cost
estimate first and asks before running; see the estimate with:

```bash
python -m evals.run_evals --estimate-only
```

Retrieval mode (the default, embeddings only, about $0.0001 for the first run and free after
that because embeddings are cached):

```bash
python -m evals.run_evals
```

Answers mode also generates answers and grades them with an LLM judge, using free OpenRouter
models (rate-limited; free accounts get 50 free-model requests a day). `--skip-arabic-pdf`
leaves out the 14 questions that depend on the Arabic PDFs, which are known to fail:

```bash
python -m evals.run_evals --mode answers --skip-arabic-pdf
```

A stopped run continues with `--resume <run-id>`; reports go to `backend/evals/reports/`.
Details: [`backend/evals/README.md`](backend/evals/README.md).

## Database commands

From the project root:

- Stop: `docker compose stop` · start again: `docker compose start`
- Check it's running: `docker compose ps`
- Reset all databases (deletes all local documents): `docker compose down -v`, then
  `docker compose up -d`, then `cd backend && python -m alembic upgrade head`

## Database migrations

The schema is created and changed only by [Alembic](https://alembic.sqlalchemy.org/)
migrations in `backend/migrations/versions/`, never by the app. Run these from `backend/`;
they use `MIGRATION_DATABASE_URL` if set, otherwise `DATABASE_URL`.

- New database: `python -m alembic upgrade head`
- See where a database is: `python -m alembic current` (newest available: `python -m alembic heads`)
- A database created **before migrations existed** (by the app's old startup code), such as an
  older local database or production: first check that it matches the baseline with
  `python -m app.schema_check` (it only reads; exit code 0 means identical), then record it as
  migrated with `python -m alembic stamp 0001_baseline` (changes nothing but the version
  table), then `python -m alembic upgrade head`. If `schema_check` lists differences, stop and
  look at them before stamping.
- Tests and evals rebuild their own `_test` / `_eval` databases from the migrations on every
  run.

**Never run `alembic downgrade` on production:** the baseline's downgrade drops all tables.
Production is rolled back with the Neon backup branch taken before the migration.

## Known limitations

- **Arabic PDFs** are not supported: their text extraction comes out broken. Arabic TXT and DOCX
  files, Arabic questions, and English–Arabic search work.
- **No password reset or email verification:** there is no email sending yet. A forgotten
  password can only be reset by hand in the database.
- **Evals on free models** are slow because of rate limits and the daily free-model limit.

More in the "Known issues" section of [`CLAUDE.md`](CLAUDE.md).

## Deployment (Vercel + Neon)

Production runs as **two Vercel projects from this repository** and a **Neon** Postgres
database:

| Project | Root directory | What it is |
|---|---|---|
| Frontend | `/` (repo root) | Next.js app |
| Backend | `backend` | FastAPI app (`app/main.py`), one Vercel Function; `backend/vercel.json` sets a 300-second limit and leaves `tests/` and `evals/` out of the bundle |

**Uploads go through Vercel Blob.** Vercel Functions accept request bodies up to 4.5 MB, but
the app allows files up to 10 MB, so the browser uploads the file to a private Blob store and the
backend reads it from there (`NEXT_PUBLIC_VERCEL_BLOB_UPLOADS=true`). Create one **private** Blob
store and connect it to **both** projects; that adds `BLOB_READ_WRITE_TOKEN` to each.

### Environment variables

Frontend project:

| Variable | Value |
|---|---|
| `BACKEND_URL` | the backend's URL, for example `https://your-api.vercel.app`. Server-only: `/api/*` on the frontend is forwarded there, so the browser never calls the backend directly. Read at build time, so redeploy after changing it; a Vercel build without it fails. |
| `NEXT_PUBLIC_VERCEL_BLOB_UPLOADS` | `true` |
| `BLOB_READ_WRITE_TOKEN` | added by connecting the Blob store (secret) |

`NEXT_PUBLIC_API_URL` is no longer used: the browser always calls the frontend's own `/api/*`.
In an existing frontend project, set `BACKEND_URL` (for Production and Preview) **before**
deploying this version, or the build fails, and then remove `NEXT_PUBLIC_API_URL`.

Backend project:

| Variable | Value |
|---|---|
| `DATABASE_URL` | Neon's **pooled** connection string (secret) |
| `OPENROUTER_API_KEY` | your OpenRouter key (secret) |
| `AI_BASE_URL` / `AI_API_KEY` | optional: another OpenAI-compatible provider instead of OpenRouter (its base URL, and its key as a secret). With `AI_BASE_URL` set, `AI_API_KEY` is required and the model settings must use that provider's model IDs |
| `BLOB_READ_WRITE_TOKEN` | the same Blob store, connected to this project too (secret) |
| `CORS_ORIGINS` | the frontend's exact URL, for example `https://your-app.vercel.app`; comma-separate several. Requests that change something (sign-in, upload, chat, delete) are refused unless their `Origin` is listed here, so a preview deployment of the frontend needs its URL added |
| `ALLOWED_ORIGINS` | optional; the origins allowed to send requests that change something. Empty means `CORS_ORIGINS` |
| `SESSION_DAYS` | optional, default `14`: days a sign-in lasts after the last visit |
| `DAILY_UPLOAD_LIMIT` / `DAILY_CHAT_LIMIT` | optional, defaults `20` and `100` per account per UTC day; `0` means no limit. A value that isn't a whole number stops the backend from starting |
| `OPENROUTER_MODEL` | optional, default `openai/gpt-4o` (the summary model) |
| `OPENROUTER_CHAT_MODEL` | optional, default: the summary model |
| `OPENROUTER_EMBEDDING_MODEL` | optional, default `openai/text-embedding-3-small` |
| `EMBEDDING_DIMENSIONS` | optional, default `1536`; must match the embedding model (changing it means re-indexing) |
| `RETRIEVAL_MODE` | optional, default `hybrid` (vector + full-text search); `vector` switches back |
| `RETRIEVAL_MIN_SIMILARITY` | optional, default `0` (no threshold; see `docs/design.md`, step 16) |
| `QUERY_REWRITE_ENABLED` | optional, default off. Leave it unset: rewriting of follow-up questions is not yet measured with the production model (see `docs/design.md`) |
| `QUERY_REWRITE_MODEL` | optional, default: the chat model |
| `AI_PRICES_PER_MILLION_TOKENS` | optional JSON, used only to estimate cost when OpenRouter returns none |

Never put `OPENROUTER_API_KEY` or `DATABASE_URL` in the frontend project. `TEST_DATABASE_URL`
and the `EVAL_*` settings are for local development only.

### Database

The database is migrated **from your machine, before deploying** the backend: the backend only
checks at startup that the database is at the newest migration (and that stored vectors have
1,536 dimensions), and refuses to start otherwise. See [Database migrations](#database-migrations).

1. Create a **Neon branch of production as a backup**, and try every step on a Neon branch
   first.
2. Set `MIGRATION_DATABASE_URL` to Neon's **direct** (unpooled) connection string of the
   database you migrate; schema changes should not go through the pooler.
3. For a database created before migrations existed (production today):
   `python -m app.schema_check --url <direct URL>` must report no differences, then
   `python -m alembic stamp 0001_baseline`. Then, for every database:
   `python -m alembic upgrade head`.
4. Deploy the backend.

**Never run `alembic downgrade` on production** (the baseline's downgrade drops all tables).
To roll back, redeploy the previous backend and, if the data must go back too, restore the
Neon backup branch.

The migrations enable the `vector` extension (available on every Neon plan). Filtering chat to
selected documents needs pgvector 0.8 or newer (`SELECT extversion FROM pg_extension WHERE
extname = 'vector';`).

**Documents uploaded before accounts existed** have no owner, so nobody sees them. After
deploying, register your account in the app, then give them to it from your machine, with
`DATABASE_URL` pointing at the database (from `backend/`):

```bash
python -m app.claim_documents --email you@example.com --dry-run
```

The dry run lists the documents it would claim and changes nothing. Then run it without
`--dry-run`. It only touches documents with no owner, so running it again claims nothing.

**Documents uploaded before chat existed** keep their summaries but have no chunks, so chat
can't find them. Upload them again to make them searchable.

The backend disables server-side prepared statements, so Neon's pooled connection string
(PgBouncer) works.

### Order of deployment

1. Create the Neon backup branch; note the pooled connection string (for `DATABASE_URL`) and
   the direct one (for migrating). Migrate the database as described above.
2. Create the private Blob store.
3. Create the backend project (root `backend`), connect the Blob store, set its variables with
   `CORS_ORIGINS` left for later, and deploy. Check `https://your-api.vercel.app/health`.
4. Create the frontend project (repo root), connect the Blob store, set `BACKEND_URL` to the
   backend's URL and `NEXT_PUBLIC_VERCEL_BLOB_UPLOADS=true`, and deploy.
5. Set the backend's `CORS_ORIGINS` to the frontend's URL and redeploy the backend. Until
   then, sign-in, uploads and chat are refused (403), because the `Origin` check doesn't know
   the frontend yet.
6. Check in the browser: register an account, then claim the existing documents (above). Upload
   a small TXT and a PDF over 4.5 MB, ask a question on the Chat page and get an answer with
   citations, delete a document, sign out and back in. A second account must see none of the
   first account's documents. The backend's Vercel logs show one `ai_usage` line per AI call.

Limits on Vercel: one upload (summary plus indexing) must finish within the function's time
limit (300 seconds on the Hobby plan, up to 800 on Pro), and very large documents can exceed
the summary model's input size.
