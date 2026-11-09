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

`corpus/` and `questions.md` are generated and gitignored. Regenerate them with the commands
below; the output is deterministic.

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
