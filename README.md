# AI Document Summarizer

Upload PDF, DOCX and TXT files, get AI summaries streamed as they are written, and **chat with
your documents**: ask questions and get answers with numbered citations to the file and page
they came from.

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
summary model if it is not set.

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
tools) and start the API:

```bash
pip install -r requirements-dev.txt
```

```bash
uvicorn app.main:app --reload
```

The API runs on http://localhost:8000. http://localhost:8000/health should return
`{"status":"healthy"}`. Each AI call logs one `ai_usage` line with its model, tokens and cost.

### 4. Configure and start the frontend

In a second terminal, in the project root, copy `.env.example` to `.env.local` (the file name
starts with a dot). It sets `NEXT_PUBLIC_API_URL=http://localhost:8000`.

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

Open http://localhost:3000. Upload a document on the **Documents** page, then ask about it on
the **Chat** page.

## How it works

- **Upload:** the file is read page by page and split into chunks of about 800 tokens that
  never cross a page. The chunks are embedded (`openai/text-embedding-3-small`) while the
  summary streams; the document and its chunks are saved together only if both succeed.
- **Chat:** `POST /chat/stream` embeds the question, finds the 8 most similar chunks with
  pgvector, and streams an answer that cites them as `[1]`, `[2]`… The chat page shows those
  citations as buttons linked to a sources list (file, page, snippet). Answers come only from
  the documents; if the answer isn't there, it says so.

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
npm run build
```

## Evals

`backend/evals/` measures retrieval and answer quality on a fictional corpus of 9 documents
(English and Arabic) with 56 questions. Run from `backend/`. The runner always prints a cost
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
  `docker compose up -d`

## Known limitations

- **Arabic PDFs** are not supported: their text extraction comes out broken. Arabic TXT and DOCX
  files, Arabic questions, and English–Arabic search work.
- **No authentication:** everyone using the app shares one database.
- **Evals on free models** are slow because of rate limits and the daily free-model limit.

More in the "Known issues" section of [`CLAUDE.md`](CLAUDE.md).

## Deployment (outdated)

These deployment notes predate the move to PostgreSQL + pgvector and have not been updated
(a known issue, planned for Phase 3). `render.yaml` uses a Postgres `DATABASE_URL` (Neon),
which would also need the `vector` extension.

The frontend can be imported into Vercel as a standard Next.js project. Set
`NEXT_PUBLIC_API_URL` to the public URL of the deployed API, then deploy the frontend. This
variable is public by design; never put provider credentials in the frontend.

`render.yaml` prepares a separate FastAPI service. During service setup, provide
`OPENROUTER_API_KEY` and `DATABASE_URL`, and set `CORS_ORIGINS` to the exact Vercel origin (for
example, `https://your-app.vercel.app`). Add the deployed API URL to Vercel as
`NEXT_PUBLIC_API_URL` and redeploy. Confirm `/health`, upload/streaming, and chat after both
services are live.
