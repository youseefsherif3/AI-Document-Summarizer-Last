# Evals

Evaluation suite for "chat with your documents" (see `docs/design.md`, steps 13–17).

## What's here

| File | What it is |
|---|---|
| `corpus_documents.py` | The corpus as data: 9 fictional documents (English and Arabic), page by page. |
| `build_corpus.py` | Generates the corpus files (PDF, DOCX, TXT) from that data. |
| `questions.jsonl` | The eval questions: expected key facts, sources and pages, judge checks. |
| `render_questions.py` | Writes `questions.md`, a readable view of the questions for review. |
| `text.py` | Tolerant text matching (case, Arabic diacritics, digit styles). |
| `fonts/` | Noto Naskh Arabic, used for the Arabic PDFs (SIL Open Font License, see `fonts/OFL.txt`). |
| `run_evals.py` | The eval runner: estimate, run, resume, report. |
| `scoring.py` | Retrieval, key-fact, refusal and citation scoring (no AI calls). |
| `judge.py` | The LLM judge's fixed, versioned prompt and its reply parser. |
| `embedding_cache.py` | Caches embeddings on disk so repeat runs cost nothing. |
| `reports/` | Committed eval reports (`.md` and `.json`). |

`corpus/` and `questions.md` are generated and gitignored. Regenerate them with the commands
below; the output is deterministic. `runs/` (progress of each run) and `.cache/` (embeddings)
are gitignored too.

## Commands

Run from `backend/`, after installing the dev dependencies (`pip install -r requirements-dev.txt`).

Generate the corpus into `evals/corpus/`:

```bash
python -m evals.build_corpus
```

Write `evals/questions.md` from `questions.jsonl`:

```bash
python -m evals.render_questions
```

Check the questions and corpus. The tests generate their own corpus in a temporary folder,
so they don't need the commands above (the Docker test database must be running):

```bash
python -m pytest tests/test_eval_questions.py
```

After editing `corpus_documents.py`, regenerate the corpus; after editing `questions.jsonl`,
regenerate `questions.md`.

## Running the evals

The runner needs the Docker database and `OPENROUTER_API_KEY`. It indexes the corpus into a
separate `*_eval` database (created if missing, emptied on every run), never the dev or test
database. It always prints a cost estimate first and asks before running.

See the estimate only (no AI calls, nothing written):

```bash
python -m evals.run_evals --estimate-only
```

Retrieval mode (the default): extraction and retrieval metrics, embeddings only. Embeddings
are cached in `.cache/`, so repeat runs on the same corpus cost nothing:

```bash
python -m evals.run_evals
```

Answers mode: also generates answers and runs the LLM judge, with the free models from
`EVAL_ANSWER_MODEL` and `OPENROUTER_JUDGE_MODEL` (see `backend/.env.example`):

```bash
python -m evals.run_evals --mode answers
```

Free models are rate-limited. The runner waits and retries, and saves every finished question
to `runs/<run-id>.jsonl`. If a run stops (daily limit, Ctrl+C), continue it with:

```bash
python -m evals.run_evals --mode answers --resume <run-id>
```

A resume is refused if the settings changed (models, retrieval settings, questions or corpus).

Evaluate one category only (repeatable), for example the follow-up questions, whose
`history` field holds the earlier conversation:

```bash
python -m evals.run_evals --category follow_up
```

When the run finishes, the report is written to `reports/<timestamp>-<label>.md` and `.json`;
`--label` names it. Pass `--yes` to skip the confirmation only after the estimate was approved.
