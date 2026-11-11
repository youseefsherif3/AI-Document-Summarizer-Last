"""Run the evals (docs/design.md, step 14). See evals/README.md for usage.

    python -m evals.run_evals --estimate-only               # retrieval mode: print the cost estimate
    python -m evals.run_evals                               # retrieval mode (default), asks first
    python -m evals.run_evals --mode answers --estimate-only
    python -m evals.run_evals --mode answers                # free answer + judge models by default
    python -m evals.run_evals --resume <run-id>             # continue a stopped run

The corpus is indexed into a separate *_eval database, never the dev or test database.
"""

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Awaitable, Callable

import httpx
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# Evals measure the models and have their own embedding cache: never the app's Redis cache.
os.environ["CACHE_ENABLED"] = "false"

import app.database as database  # noqa: E402
from app.extraction import Page, extract_pages, join_pages
from app.services import chat
from app.services.chunking import (
    CHUNK_MAX_TOKENS,
    CHUNK_OVERLAP_TOKENS,
    Chunk,
    chunk_pages,
    estimate_tokens,
)
from app.services.embeddings import EMBEDDING_BATCH_SIZE, EMBEDDING_MODEL, embed_texts
from app.services.llm import complete
from app.services.query_rewrite import (
    build_rewrite_messages,
    query_rewrite_enabled,
    rewrite_question,
)
from app.services.summarizer import AIRateLimitError, AIServiceError, AIServiceNotConfiguredError

from . import scoring
from .build_corpus import build_document
from .corpus_documents import DOCUMENTS, page_texts
from .embedding_cache import EmbeddingCache
from .judge import (
    JUDGE_PROMPT_VERSION,
    JUDGE_SYSTEM_PROMPT,
    JudgeResult,
    build_judge_messages,
    parse_judge_response,
)
from .render_questions import QUESTIONS_PATH, load_questions
from .text import contains_any

EVALS_DIR = Path(__file__).resolve().parent
RUNS_DIR = EVALS_DIR / "runs"
REPORTS_DIR = EVALS_DIR / "reports"
CACHE_PATH = EVALS_DIR / ".cache" / "embeddings.jsonl"

# Free OpenRouter models chosen in step 14; override in backend/.env.
DEFAULT_ANSWER_MODEL = "google/gemma-4-31b-it:free"
DEFAULT_JUDGE_MODEL = "nvidia/nemotron-3-ultra-550b-a55b:free"
# Free model for query rewriting in evals (EVAL_REWRITE_MODEL overrides it). Gemma 4 was
# rate-limited for minutes at a time when this was chosen (2026-11-09).
DEFAULT_REWRITE_MODEL = "nvidia/nemotron-3.5-lightning:free"
ESTIMATED_REWRITE_OUTPUT_TOKENS = 40
# text-embedding-3-small through OpenRouter, as measured in step 6 ($1.44e-6 for 72 tokens).
FALLBACK_EMBEDDING_PRICE_PER_TOKEN = 0.02 / 1_000_000

ARABIC_PDFS = {"waha_library_ar.pdf", "nursin_port_ar.pdf"}
ARABIC_PDF_EXCLUSION_REASON = (
    "Sourced from an Arabic PDF. pypdf extraction of the two Arabic PDFs is broken "
    "(CLAUDE.md, Known issues), so these questions are known to fail; they were left out "
    "with --skip-arabic-pdf."
)


def uses_arabic_pdf(item: dict) -> bool:
    return any(source["document"] in ARABIC_PDFS for source in item["sources"])


def history_of(item: dict) -> list[chat.HistoryMessage]:
    """The earlier conversation of a follow-up question (empty for the others)."""
    return [chat.HistoryMessage(m["role"], m["content"]) for m in item.get("history", [])]
ESTIMATED_ANSWER_OUTPUT_TOKENS = 300
ESTIMATED_JUDGE_OUTPUT_TOKENS = 1500  # the judge is a reasoning model
JUDGE_MAX_TOKENS = 4000
MAX_RATE_LIMIT_ATTEMPTS = 6
MAX_NETWORK_ATTEMPTS = 3

AnswerFn = Callable[[chat.PreparedAnswer, str], Awaitable[str]]
RewriteFn = Callable[[str, list, str], Awaitable[str]]
JudgeFn = Callable[[list[dict], str], Awaitable[str]]
SleepFn = Callable[[float], Awaitable[None]]


class RunStopped(Exception):
    """The run cannot continue now (daily limit, provider error); progress is saved."""


# --- Corpus and settings -----------------------------------------------------------


@dataclass
class CorpusFile:
    filename: str
    data: bytes
    pages: list[Page]
    text: str
    chunks: list[Chunk]

    @property
    def extension(self) -> str:
        return "." + self.filename.rsplit(".", 1)[1]


def prepare_corpus() -> list[CorpusFile]:
    """Build, extract and chunk every corpus document locally (no AI calls)."""
    files = []
    for document in DOCUMENTS:
        data = build_document(document)
        extension = "." + document.filename.rsplit(".", 1)[1]
        pages = extract_pages(data, extension)
        files.append(
            CorpusFile(document.filename, data, pages, join_pages(pages, extension), chunk_pages(pages))
        )
    return files


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


@dataclass(frozen=True)
class Settings:
    mode: str
    answer_model: str | None
    judge_model: str | None
    embedding_model: str
    retrieval_mode: str
    top_k: int
    min_similarity: float
    chunk_max_tokens: int
    chunk_overlap_tokens: int
    questions_sha: str
    corpus_sha: str
    system_prompt_sha: str
    judge_prompt_version: str | None
    excluded_questions: tuple[str, ...] = ()
    exclusion_reason: str | None = None
    # Added after step 15. Left out of the fingerprint while unset, so runs started
    # before they existed can still be resumed.
    categories: tuple[str, ...] | None = None
    query_rewrite_model: str | None = None  # set when query rewriting is on

    def fingerprint(self) -> str:
        return _sha(json.dumps(self.as_dict(), sort_keys=True).encode())

    def as_dict(self) -> dict:
        data = asdict(self)
        for key in OPTIONAL_SETTINGS:
            if data[key] is None:
                del data[key]
        return data


OPTIONAL_SETTINGS = ("categories", "query_rewrite_model")


def make_settings(mode: str, answer_model: str | None, judge_model: str | None,
                  corpus: list[CorpusFile], excluded: list[dict] | None = None,
                  exclusion_reason: str | None = None,
                  retrieval: str | None = None,
                  categories: list[str] | None = None,
                  rewrite_model: str | None = None) -> Settings:
    answers = mode == "answers"
    excluded = excluded or []
    return Settings(
        mode=mode,
        answer_model=answer_model if answers else None,
        judge_model=judge_model if answers else None,
        embedding_model=EMBEDDING_MODEL,
        retrieval_mode=retrieval or chat.retrieval_mode(),
        top_k=chat.RETRIEVAL_TOP_K,
        min_similarity=chat.min_similarity(),
        chunk_max_tokens=CHUNK_MAX_TOKENS,
        chunk_overlap_tokens=CHUNK_OVERLAP_TOKENS,
        questions_sha=_sha(QUESTIONS_PATH.read_bytes()),
        corpus_sha=_sha(b"".join(f.data for f in corpus)),
        system_prompt_sha=_sha(chat.SYSTEM_PROMPT.encode()),
        judge_prompt_version=JUDGE_PROMPT_VERSION if answers else None,
        excluded_questions=tuple(item["id"] for item in excluded),
        exclusion_reason=exclusion_reason if excluded else None,
        categories=tuple(categories) if categories else None,
        query_rewrite_model=rewrite_model,
    )


def check_models(answer_model: str, judge_model: str) -> list[str]:
    """Refuse identical answer and judge models; warn when they share a provider."""
    if answer_model == judge_model:
        raise SystemExit(
            f"The judge must be a different model from the answer model (both are {answer_model})."
        )
    if answer_model.split("/")[0] == judge_model.split("/")[0]:
        return [f"Answer and judge models come from the same provider ({answer_model.split('/')[0]})."]
    return []


# --- Cost estimate -------------------------------------------------------------------


def fetch_prices(models: list[str]) -> dict[str, tuple[float, float]]:
    """USD per token (prompt, completion) from OpenRouter's public model list."""
    try:
        response = httpx.get("https://openrouter.ai/api/v1/models", timeout=15)
        response.raise_for_status()
        listing = {m["id"]: m.get("pricing") or {} for m in response.json()["data"]}
    except (httpx.HTTPError, KeyError, ValueError):
        return {}
    prices = {}
    for model in models:
        pricing = listing.get(model)
        if pricing:
            try:
                prices[model] = (float(pricing.get("prompt", 0)), float(pricing.get("completion", 0)))
            except (TypeError, ValueError):
                pass
    return prices


@dataclass
class EstimateLine:
    part: str
    requests: int
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    note: str = ""


def _model_cost(model: str, prices: dict, input_tokens: int, output_tokens: int) -> float | None:
    if model.endswith(":free"):
        return 0.0
    if model not in prices:
        return None
    prompt, completion = prices[model]
    return input_tokens * prompt + output_tokens * completion


def estimate(settings: Settings, corpus: list[CorpusFile], questions: list[dict],
             cache: EmbeddingCache, prices: dict) -> list[EstimateLine]:
    texts = [chunk.text for f in corpus for chunk in f.chunks] + [q["question"] for q in questions]
    missing = cache.missing(texts)
    embedding_tokens = sum(estimate_tokens(t) for t in missing)
    embedding_price = prices.get(settings.embedding_model, (FALLBACK_EMBEDDING_PRICE_PER_TOKEN, 0))[0]
    lines = [
        EstimateLine(
            "embeddings", math.ceil(len(missing) / EMBEDDING_BATCH_SIZE), embedding_tokens, 0,
            embedding_tokens * embedding_price,
            f"{len(texts) - len(missing)} of {len(texts)} texts already cached",
        )
    ]
    to_rewrite = [q for q in questions if q.get("history")] if settings.query_rewrite_model else []
    if to_rewrite:
        rewrite_input = sum(
            estimate_tokens(m["content"])
            for q in to_rewrite
            for m in build_rewrite_messages(q["question"], history_of(q))
        )
        rewrite_output = ESTIMATED_REWRITE_OUTPUT_TOKENS * len(to_rewrite)
        lines[0].note += (f"; plus up to {len(to_rewrite)} embeddings of rewritten questions "
                          "(not known until they are rewritten)")
        lines.append(EstimateLine(
            f"query rewrite ({settings.query_rewrite_model})", len(to_rewrite), rewrite_input,
            rewrite_output, _model_cost(settings.query_rewrite_model, prices, rewrite_input,
                                        rewrite_output),
            "one call per question with history; questions without history make no call",
        ))
    if settings.mode == "answers":
        chunk_tokens = [chunk.token_estimate for f in corpus for chunk in f.chunks]
        sources_tokens = settings.top_k * mean(chunk_tokens)
        answer_input = round(sum(
            estimate_tokens(chat.SYSTEM_PROMPT) + sources_tokens + estimate_tokens(q["question"]) + 40
            for q in questions
        ))
        answer_output = ESTIMATED_ANSWER_OUTPUT_TOKENS * len(questions)
        judge_input = sum(
            estimate_tokens(JUDGE_SYSTEM_PROMPT) + 200 + ESTIMATED_ANSWER_OUTPUT_TOKENS for _ in questions
        )
        judge_output = ESTIMATED_JUDGE_OUTPUT_TOKENS * len(questions)
        lines.append(EstimateLine(
            f"answers ({settings.answer_model})", len(questions), answer_input, answer_output,
            _model_cost(settings.answer_model, prices, answer_input, answer_output),
        ))
        lines.append(EstimateLine(
            f"judge ({settings.judge_model})", len(questions), int(judge_input), judge_output,
            _model_cost(settings.judge_model, prices, judge_input, judge_output),
        ))
    return lines


def format_estimate(lines: list[EstimateLine], settings: Settings, questions: int,
                    pace_seconds: float, warnings: list[str]) -> str:
    out = [
        f"Eval run estimate: mode={settings.mode}, {questions} questions, "
        f"retrieval={settings.retrieval_mode}, top_k={settings.top_k}, "
        f"min_similarity={settings.min_similarity}, "
        f"query rewrite={settings.query_rewrite_model or 'off'}",
        "",
        f"{'part':52} {'requests':>8} {'in tokens':>10} {'out tokens':>10} {'cost (USD)':>12}",
    ]
    for line in lines:
        cost = "unknown" if line.cost_usd is None else f"{line.cost_usd:.6f}"
        out.append(f"{line.part:52} {line.requests:>8} {line.input_tokens:>10} "
                   f"{line.output_tokens:>10} {cost:>12}")
        if line.note:
            out.append(f"    {line.note}")
    known = [line.cost_usd for line in lines if line.cost_usd is not None]
    unknown = any(line.cost_usd is None for line in lines)
    out.append(f"{'TOTAL':52} {'':>8} {'':>10} {'':>10} {sum(known):>12.6f}"
               + ("  (+ unknown)" if unknown else ""))
    model_requests = sum(line.requests for line in lines if line.part != "embeddings")
    if model_requests:
        out.append("")
        out.append(f"Model requests: {model_requests}. Free models are rate-limited; with "
                   f"{pace_seconds:g}s between calls this takes at least "
                   f"{model_requests * pace_seconds / 60:.0f} minutes, more if limits are hit.")
    out.append("Token counts are estimates (characters / 4); the report shows actual usage.")
    out += [f"WARNING: {warning}" for warning in warnings]
    return "\n".join(out)


# --- Eval database -----------------------------------------------------------------


def _location(url: str) -> tuple:
    parsed = make_url(url)
    return (parsed.host, parsed.port or 5432, parsed.database)


def eval_database_url() -> str:
    url = os.getenv("EVAL_DATABASE_URL", "").strip()
    if not url:
        if not database.DATABASE_URL:
            raise SystemExit("Set DATABASE_URL or EVAL_DATABASE_URL in backend/.env.")
        parsed = make_url(database.DATABASE_URL)
        url = parsed.set(database=f"{parsed.database}_eval").render_as_string(hide_password=False)
    name = make_url(url).database or ""
    if not name.endswith("_eval"):
        raise SystemExit("The eval database name must end in '_eval' (its tables are emptied).")
    for other in (database.DATABASE_URL, os.getenv("TEST_DATABASE_URL")):
        if other and _location(other) == _location(url):
            raise SystemExit("The eval database must not be the dev or test database.")
    return url


def ensure_database(url: str) -> None:
    """Create the eval database if it doesn't exist yet."""
    parsed = make_url(url)
    name = parsed.database
    if not name or not name.replace("_", "").isalnum():
        raise SystemExit(f"Unexpected eval database name: {name!r}")
    admin_url = parsed.set(drivername="postgresql+psycopg", database="postgres")
    engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            exists = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).scalar()
            if not exists:
                connection.execute(text(f'CREATE DATABASE "{name}"'))
                print(f"Created eval database {name}.")
    finally:
        engine.dispose()


# The eval corpus belongs to this account; retrieval searches only its documents, as the app
# does for a signed-in user.
EVAL_USER_EMAIL = "eval-runner@documind.invalid"


def reset_tables() -> int:
    """Rebuild the eval database from the Alembic migrations (it is emptied every run) and
    create the eval account. Returns the account's ID."""
    from app.migrate import reset_and_upgrade

    reset_and_upgrade(database.DATABASE_URL)
    database.check_database()
    with database.get_connection() as connection:
        return connection.execute(
            text("INSERT INTO users (email, password_hash) VALUES (:email, 'no sign-in') "
                 "RETURNING id"),
            {"email": EVAL_USER_EMAIL},
        ).scalar_one()


async def index_corpus(corpus: list[CorpusFile], cache: EmbeddingCache, owner_user_id: int) -> None:
    for corpus_file in corpus:
        vectors = await cache.embed([chunk.text for chunk in corpus_file.chunks])
        database.save_document_with_chunks(
            filename=corpus_file.filename,
            file_type=corpus_file.extension,
            file_size=len(corpus_file.data),
            file_content=corpus_file.data,
            extracted_text=corpus_file.text,
            summary=None,
            document_chunks=corpus_file.chunks,
            embeddings=vectors,
            owner_user_id=owner_user_id,
        )


# --- Run log (progress and resume) -------------------------------------------------


class RunLog:
    """evals/runs/<run-id>.jsonl: a header line, then one line per finished question."""

    def __init__(self, path: Path):
        self.path = path

    @property
    def run_id(self) -> str:
        return self.path.stem

    @classmethod
    def start(cls, path: Path, settings: Settings) -> "RunLog":
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {
            "type": "header",
            "fingerprint": settings.fingerprint(),
            "settings": settings.as_dict(),
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        path.write_text(json.dumps(header, ensure_ascii=False) + "\n", encoding="utf-8")
        return cls(path)

    @classmethod
    def resume(cls, path: Path, settings: Settings) -> "RunLog":
        if not path.exists():
            raise SystemExit(f"No run to resume at {path}.")
        log = cls(path)
        header = log.header()
        if header["fingerprint"] != settings.fingerprint():
            # Compare as saved (JSON turns tuples into lists), including keys only one side has.
            current = json.loads(json.dumps(settings.as_dict()))
            saved = header["settings"]
            changed = [key for key in sorted(set(current) | set(saved))
                       if saved.get(key) != current.get(key)]
            raise SystemExit(
                f"Cannot resume {log.run_id}: settings changed ({', '.join(changed)}). "
                "Start a new run instead."
            )
        return log

    def lines(self) -> list[dict]:
        with self.path.open(encoding="utf-8") as file:
            return [json.loads(line) for line in file if line.strip()]

    def header(self) -> dict:
        return self.lines()[0]

    def results(self) -> list[dict]:
        return [line for line in self.lines() if line["type"] == "result"]

    def done_ids(self) -> set[str]:
        return {result["id"] for result in self.results()}

    def append(self, record: dict) -> None:
        with self.path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(record, ensure_ascii=False) + "\n")


# --- Calling models with retries -----------------------------------------------------


async def call_with_retries(call: Callable[[], Awaitable[str]], label: str, sleep: SleepFn) -> str:
    """Retry rate limits (waiting as the provider asks) and brief network errors.

    Raises RunStopped when waiting won't help (daily limit, provider error), so the run
    can be resumed later.
    """
    backoff = 10.0
    network_failures = 0
    for attempt in range(1, MAX_RATE_LIMIT_ATTEMPTS + 1):
        try:
            return await call()
        except AIRateLimitError as error:
            detail = error.detail.lower()
            if "per-day" in detail or "per day" in detail:
                raise RunStopped(f"{label}: daily limit reached ({error.detail})") from error
            if attempt == MAX_RATE_LIMIT_ATTEMPTS:
                break
            wait = min(max(error.retry_after or backoff, 1.0), 600.0)
            print(f"  rate limited ({label}); waiting {wait:.0f}s "
                  f"(attempt {attempt}/{MAX_RATE_LIMIT_ATTEMPTS})", flush=True)
            await sleep(wait)
            backoff = min(backoff * 2, 300.0)
        except (TimeoutError, ConnectionError) as error:
            network_failures += 1
            if network_failures >= MAX_NETWORK_ATTEMPTS:
                raise RunStopped(f"{label}: {error}") from error
            await sleep(backoff)
            backoff = min(backoff * 2, 300.0)
        except (AIServiceNotConfiguredError, AIServiceError) as error:
            raise RunStopped(f"{label}: {error}") from error
    raise RunStopped(f"{label}: still rate limited after {MAX_RATE_LIMIT_ATTEMPTS} attempts")


async def default_answer(prepared: chat.PreparedAnswer, model: str) -> str:
    return "".join([part async for part in chat.stream_answer(prepared, model=model)])


async def default_rewrite(question: str, history: list, model: str) -> str:
    return await rewrite_question(question, history, model)


async def default_judge(messages: list[dict], model: str) -> str:
    return await complete(model=model, messages=messages, kind="judge", max_tokens=JUDGE_MAX_TOKENS)


class UsageCollector(logging.Handler):
    """Collects the ai_usage fields that every AI call logs."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records: list[dict] = []

    def emit(self, record: logging.LogRecord) -> None:
        if hasattr(record, "ai_usage"):
            self.records.append(dict(record.ai_usage))

    def take(self) -> list[dict]:
        records, self.records = self.records, []
        return records


# --- One question --------------------------------------------------------------------


async def evaluate_question(item: dict, vector: list[float], settings: Settings,
                            answer_fn: AnswerFn, judge_fn: JudgeFn, sleep: SleepFn,
                            pace_seconds: float, query: str | None = None, *,
                            owner_user_id: int) -> dict:
    """`query` is what retrieval searches with (the rewritten question, if any); `vector`
    is its embedding."""
    query = query or item["question"]
    matches = chat.find_matches(
        query, vector, k=settings.top_k, mode=settings.retrieval_mode,
        threshold=settings.min_similarity, owner_user_id=owner_user_id,
    )
    retrieved = [
        scoring.Retrieved(rank, match.filename, match.page_number, match.similarity)
        for rank, match in enumerate(matches, start=1)
    ]
    record = {
        "type": "result",
        "id": item["id"],
        "category": item["category"],
        "language": item["language"],
        "answerable": item["answerable"],
        "arabic_pdf": uses_arabic_pdf(item),
        "retrieved": [asdict(r) for r in retrieved],
        "top_similarity": retrieved[0].similarity if retrieved else None,
    }
    if query != item["question"]:
        record["query"] = query
    if item["answerable"]:
        record["retrieval"] = scoring.retrieval_metrics(item, retrieved)

    if settings.mode == "answers":
        sources = chat.sources_from_matches(matches)
        prepared = chat.PreparedAnswer(
            item["question"], sources,
            chat.build_messages(item["question"], sources, history_of(item)),
        )
        answer = await call_with_retries(
            lambda: answer_fn(prepared, settings.answer_model), f"answer {item['id']}", sleep
        )
        await sleep(pace_seconds)
        judge_text = await call_with_retries(
            lambda: judge_fn(build_judge_messages(item, answer), settings.judge_model),
            f"judge {item['id']}", sleep,
        )
        await sleep(pace_seconds)
        judged = parse_judge_response(judge_text, len(item["judge_checks"]))
        numbered = [{"n": s.n, "document": s.filename, "page": s.page} for s in sources]
        record["answer"] = {
            "text": answer,
            "refusal": scoring.looks_like_refusal(answer),
            "key_facts": scoring.key_fact_result(item, answer),
            "forbidden_found": scoring.forbidden_found(item, answer),
            "citations": scoring.citation_correctness(item, answer, numbered),
            "coverage": scoring.citation_coverage(answer),
            "judge": asdict(judged),
            "judge_raw": judge_text if judged.error else None,
        }
    return record


# --- Report --------------------------------------------------------------------------


def resume_flags(skip_arabic_pdf: bool, categories, rewrite_model: str | None = None) -> str:
    """The options a resume must repeat so its settings match the original run."""
    flags = " --skip-arabic-pdf" if skip_arabic_pdf else ""
    flags += "".join(f" --category {c}" for c in categories or [])
    if rewrite_model:
        flags += f" --query-rewrite on --rewrite-model {rewrite_model}"
    return flags


def _rate(values: list[bool]) -> float | None:
    return None if not values else sum(values) / len(values)


def _ratio(parts: list[tuple[int, int]]) -> float | None:
    total = sum(t for _, t in parts)
    return None if total == 0 else sum(p for p, _ in parts) / total


def extraction_quality(questions: list[dict], corpus: list[CorpusFile]) -> list[dict]:
    """Share of expected key facts that survive extraction, per document."""
    by_name = {f.filename: f for f in corpus}
    sources_text = {d.filename: page_texts(d) for d in DOCUMENTS}
    stats: dict[str, list[bool]] = {}
    for item in questions:
        if not item["answerable"]:
            continue
        for group in item["key_facts"]:
            for source in item["sources"]:
                name = source["document"]
                pages = source["pages"]
                original = sources_text[name]
                original_text = "\n".join(original if pages is None else [original[p - 1] for p in pages])
                if not contains_any(original_text, group):
                    continue  # this fact belongs to another source of the question
                extracted = by_name[name].pages
                extracted_text = "\n".join(
                    p.text for p in extracted if pages is None or p.number in pages
                )
                stats.setdefault(name, []).append(contains_any(extracted_text, group))
                break
    rows = []
    for document in DOCUMENTS:
        found = stats.get(document.filename, [])
        rows.append({
            "document": document.filename,
            "format": document.filename.rsplit(".", 1)[1],
            "language": document.language,
            "facts": len(found),
            "found": sum(found),
            "rate": _rate(found),
        })
    return rows


def _retrieval_summary(results: list[dict]) -> dict:
    scored = [r for r in results if "retrieval" in r]
    summary = {"questions": len(scored)}
    for k in scoring.RETRIEVAL_KS:
        summary[f"hit@{k}"] = _rate([r["retrieval"][f"hit@{k}"] for r in scored])
    summary["mrr"] = mean([r["retrieval"]["reciprocal_rank"] for r in scored]) if scored else None
    multi = [r for r in scored if r["category"] in ("conflict", "cross_document")]
    summary["all_sources_found"] = _rate([r["retrieval"]["all_sources_found"] for r in multi])
    return summary


def _answer_summary(results: list[dict]) -> dict:
    answered = [r for r in results if "answer" in r]
    if not answered:
        return {}
    answerable = [r for r in answered if r["answerable"]]
    unanswerable = [r for r in answered if not r["answerable"]]
    scores = [r["answer"]["judge"]["score"] for r in answered if r["answer"]["judge"]["score"]]
    checks = [c for r in answered for c in r["answer"]["judge"]["checks"]]
    return {
        "questions": len(answered),
        "judge_mean": mean(scores) if scores else None,
        "judge_errors": sum(1 for r in answered if r["answer"]["judge"]["error"]),
        "key_fact_recall": _ratio([(r["answer"]["key_facts"]["found"], r["answer"]["key_facts"]["total"])
                                   for r in answerable]),
        "refusal_accuracy": _rate([r["answer"]["refusal"] for r in unanswerable]),
        "false_refusals": _rate([r["answer"]["refusal"] for r in answerable]),
        "citation_correctness": _ratio([(r["answer"]["citations"]["correct"],
                                         r["answer"]["citations"]["total"]) for r in answerable]),
        "citation_coverage": _ratio([(r["answer"]["coverage"]["cited"], r["answer"]["coverage"]["total"])
                                     for r in answerable]),
        "forbidden_text": sum(1 for r in answered if r["answer"]["forbidden_found"]),
        "judge_checks_passed": _rate(checks),
    }


def _similarity_summary(results: list[dict]) -> dict:
    def stats(values):
        values = [v for v in values if v is not None]
        return None if not values else {"mean": mean(values), "min": min(values), "max": max(values)}
    return {
        "answerable": stats([r["top_similarity"] for r in results if r["answerable"]]),
        "unanswerable": stats([r["top_similarity"] for r in results if not r["answerable"]]),
    }


def _actual_cost(log: RunLog) -> dict:
    totals: dict[str, dict] = {}
    for line in log.lines():
        for usage in line.get("usage", []):
            entry = totals.setdefault(usage["kind"], {"calls": 0, "cost_usd": 0.0, "unknown_cost": 0,
                                                      "prompt_tokens": 0, "completion_tokens": 0})
            entry["calls"] += 1
            entry["prompt_tokens"] += usage.get("prompt_tokens") or 0
            entry["completion_tokens"] += usage.get("completion_tokens") or 0
            if usage.get("cost_usd") is None:
                entry["unknown_cost"] += 1
            else:
                entry["cost_usd"] += usage["cost_usd"]
    return totals


def summarize(log: RunLog, questions: list[dict], corpus: list[CorpusFile],
              stop_reason: str | None = None) -> dict:
    """`questions` are the questions this run should evaluate (after exclusions).

    If some are not done yet, the summary is marked partial, with the reason and the
    command that continues the run.
    """
    header = log.header()
    results = log.results()
    categories = list(dict.fromkeys(q["category"] for q in questions))
    without_arabic_pdf = [r for r in results if not r["arabic_pdf"]]
    all_questions = {q["id"]: q for q in load_questions()}
    excluded_ids = header["settings"].get("excluded_questions") or []
    done = {r["id"] for r in results}
    remaining = [q["id"] for q in questions if q["id"] not in done]
    partial = None
    if remaining:
        flags = resume_flags(bool(excluded_ids), header["settings"].get("categories"),
                             header["settings"].get("query_rewrite_model"))
        partial = {
            "evaluated": len(done),
            "target": len(questions),
            "remaining": remaining,
            "reason": stop_reason or next(
                (line["reason"] for line in reversed(log.lines()) if line["type"] == "stopped"),
                "The run has not finished.",
            ),
            "resume_command": f"python -m evals.run_evals --mode {header['settings']['mode']}"
                              f"{flags} --resume {log.run_id}",
        }
    return {
        "run_id": log.run_id,
        "started_at": header["started_at"],
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "settings": header["settings"],
        "partial": partial,
        "evaluated_questions": len(results),
        "total_questions": len(all_questions),
        "excluded": {
            "reason": header["settings"].get("exclusion_reason"),
            "questions": [
                {
                    "id": qid,
                    "category": all_questions[qid]["category"],
                    "question": all_questions[qid]["question"],
                    "sources": [s["document"] for s in all_questions[qid]["sources"]],
                }
                for qid in excluded_ids
            ],
        },
        "estimate": next((l["estimate"] for l in log.lines() if l["type"] == "estimate"), None),
        "extraction": extraction_quality(questions, corpus),
        "retrieval": {
            "all": _retrieval_summary(results),
            "without_arabic_pdf": _retrieval_summary(without_arabic_pdf),
            "by_category": {c: _retrieval_summary([r for r in results if r["category"] == c])
                            for c in categories},
            "by_language": {lang: _retrieval_summary([r for r in results if r["language"] == lang])
                            for lang in ("en", "ar")},
        },
        "top_similarity": _similarity_summary(results),
        "answers": {
            "all": _answer_summary(results),
            "without_arabic_pdf": _answer_summary(without_arabic_pdf),
            "by_category": {c: _answer_summary([r for r in results if r["category"] == c])
                            for c in categories},
        },
        "actual_cost": _actual_cost(log),
        "questions": [
            {k: r[k] for k in ("id", "category", "language", "arabic_pdf", "top_similarity")}
            | ({"query": r["query"]} if "query" in r else {})
            | {"first_hit_rank": r.get("retrieval", {}).get("first_hit_rank"),
               "judge_score": r.get("answer", {}).get("judge", {}).get("score")}
            for r in results
        ],
    }


def _fmt(value, percent=True) -> str:
    if value is None:
        return "–"
    if isinstance(value, float):
        return f"{value:.0%}" if percent else f"{value:.3f}"
    return str(value)


def render_report(summary: dict) -> str:
    s = summary["settings"]
    partial = summary.get("partial")
    lines = [
        f"# Eval report: {summary['run_id']}" + (" (PARTIAL)" if partial else ""),
        "",
    ]
    if partial:
        lines += [
            f"> **Partial report.** {partial['evaluated']} of the {partial['target']} questions "
            f"in this run were evaluated; the other {len(partial['remaining'])} were not.",
            f"> **Why:** {partial['reason']}",
            f"> **Not evaluated yet:** {', '.join(partial['remaining'])}",
            f"> **To finish the run:** `{partial['resume_command']}` (from `backend/`; the "
            "progress file in `evals/runs/` is needed). All numbers below cover only the "
            "evaluated questions.",
            "",
        ]
    lines += [
        f"- Mode: **{s['mode']}** · retrieval: {s['retrieval_mode']} · top_k: {s['top_k']} · "
        f"min_similarity: {s['min_similarity']}",
        f"- Embedding model: `{s['embedding_model']}`",
    ]
    if s.get("categories"):
        lines.append(f"- Categories evaluated: {', '.join(s['categories'])} (--category)")
    lines.append(f"- Query rewrite: "
                 + (f"on, `{s['query_rewrite_model']}`" if s.get("query_rewrite_model") else "off"))
    if s["mode"] == "answers":
        lines.append(f"- Answer model: `{s['answer_model']}` · judge: `{s['judge_model']}` "
                     f"({s['judge_prompt_version']}). Answer scores describe this answer model, "
                     "not the production chat model.")
    lines += [
        f"- Questions evaluated: **{summary['evaluated_questions']} of {summary['total_questions']}**"
        + (f" ({len(summary['excluded']['questions'])} excluded, listed below)"
           if summary["excluded"]["questions"] else ""),
        f"- Questions sha: `{s['questions_sha']}` · corpus sha: `{s['corpus_sha']}` · "
        f"system prompt sha: `{s['system_prompt_sha']}`",
        f"- Started {summary['started_at']}, finished {summary['finished_at']}",
    ]
    excluded = summary["excluded"]
    if excluded["questions"]:
        lines += [
            "",
            f"## Excluded questions ({len(excluded['questions'])})",
            "",
            f"**Why:** {excluded['reason']} All other numbers in this report are for the "
            f"{summary['evaluated_questions']} evaluated questions only.",
            "",
            "| ID | Category | Question | Expected source(s) |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| {q['id']} | {q['category']} | {q['question']} | "
            f"{', '.join(f'`{d}`' for d in q['sources'])} |"
            for q in excluded["questions"]
        ]
    lines += [
        "",
        "## Extraction quality (expected key facts found in the extracted text)",
        "",
        "| Document | Format | Language | Facts found |",
        "|---|---|---|---|",
    ]
    for row in summary["extraction"]:
        lines.append(f"| `{row['document']}` | {row['format']} | {row['language']} | "
                     f"{row['found']}/{row['facts']} ({_fmt(row['rate'])}) |")

    def retrieval_row(name, data):
        return (f"| {name} | {data['questions']} | {_fmt(data['hit@1'])} | {_fmt(data['hit@3'])} | "
                f"{_fmt(data['hit@8'])} | {_fmt(data['mrr'], percent=False)} |")

    retrieval = summary["retrieval"]
    lines += [
        "",
        "## Retrieval (answerable questions)",
        "",
        "| Group | Questions | hit@1 | hit@3 | hit@8 | MRR |",
        "|---|---|---|---|---|---|",
        retrieval_row("**All**", retrieval["all"]),
        retrieval_row("All except Arabic-PDF questions", retrieval["without_arabic_pdf"]),
    ]
    lines += [retrieval_row(f"lang: {k}", v) for k, v in retrieval["by_language"].items() if v["questions"]]
    lines += [retrieval_row(k, v) for k, v in retrieval["by_category"].items() if v["questions"]]
    lines.append("")
    lines.append(f"All expected sources in the top {s['top_k']} (conflict + cross-document): "
                 f"{_fmt(retrieval['all']['all_sources_found'])}")

    rewritten = [q for q in summary["questions"] if "query" in q]
    if rewritten:
        all_questions = {q["id"]: q for q in load_questions()}
        lines += ["", "## Rewritten questions (used for retrieval)", "",
                  "| ID | Question | Rewritten | First hit rank |", "|---|---|---|---|"]
        lines += [f"| {q['id']} | {all_questions[q['id']]['question']} | {q['query']} | "
                  f"{q['first_hit_rank'] or 'not in top ' + str(s['top_k'])} |" for q in rewritten]

    sims = summary["top_similarity"]
    lines += ["", "## Top-1 similarity (for threshold tuning)", "",
              "| Questions | Mean | Min | Max |", "|---|---|---|---|"]
    for name, data in sims.items():
        if data:
            lines.append(f"| {name} | {data['mean']:.3f} | {data['min']:.3f} | {data['max']:.3f} |")

    answers = summary["answers"]
    if answers["all"]:
        def answer_row(name, data):
            return (f"| {name} | {data['questions']} | {_fmt(data['judge_mean'], percent=False)} | "
                    f"{_fmt(data['key_fact_recall'])} | {_fmt(data['refusal_accuracy'])} | "
                    f"{_fmt(data['false_refusals'])} | {_fmt(data['citation_correctness'])} | "
                    f"{_fmt(data['citation_coverage'])} | {_fmt(data['judge_checks_passed'])} | "
                    f"{data['forbidden_text']} | {data['judge_errors']} |")
        lines += [
            "", "## Answers", "",
            "| Group | Qs | Judge (1–5) | Key facts | Refusal acc. | False refusals | "
            "Citation correct | Citation coverage | Judge checks | Forbidden text | Judge errors |",
            "|---|---|---|---|---|---|---|---|---|---|---|",
            answer_row("**All**", answers["all"]),
            answer_row("All except Arabic-PDF questions", answers["without_arabic_pdf"]),
        ]
        lines += [answer_row(k, v) for k, v in answers["by_category"].items() if v]

    lines += ["", "## Cost", "", "| Part | Calls | Prompt tokens | Completion tokens | Cost (USD) |",
              "|---|---|---|---|---|"]
    for kind, data in summary["actual_cost"].items():
        unknown = f" (+{data['unknown_cost']} unknown)" if data["unknown_cost"] else ""
        lines.append(f"| {kind} | {data['calls']} | {data['prompt_tokens']} | "
                     f"{data['completion_tokens']} | {data['cost_usd']:.6f}{unknown} |")
    total = sum(d["cost_usd"] for d in summary["actual_cost"].values())
    lines.append(f"| **Total** | | | | **{total:.6f}** |")
    if summary["estimate"]:
        estimated = sum(e["cost_usd"] or 0 for e in summary["estimate"])
        lines.append(f"\nEstimated before the run: {estimated:.6f} USD.")
    lines += [
        "",
        "Questions sourced from the two Arabic PDFs are expected to fail (pypdf extraction is "
        "broken; see CLAUDE.md, Known issues).",
    ]
    return "\n".join(lines) + "\n"


def write_report(summary: dict, label: str | None) -> tuple[Path, Path]:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{label or summary['settings']['mode']}"
    md_path, json_path = REPORTS_DIR / f"{name}.md", REPORTS_DIR / f"{name}.json"
    md_path.write_text(render_report(summary), encoding="utf-8")
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return md_path, json_path


# --- Main ------------------------------------------------------------------------------


async def run(log: RunLog, settings: Settings, questions: list[dict], corpus: list[CorpusFile],
              cache: EmbeddingCache, answer_fn: AnswerFn = default_answer,
              judge_fn: JudgeFn = default_judge, sleep: SleepFn = asyncio.sleep,
              pace_seconds: float = 0.0, database_url: str | None = None,
              rewrite_fn: RewriteFn = default_rewrite) -> None:
    """Index the corpus, then evaluate every question not yet in the run log."""
    collector = UsageCollector()
    usage_logger = logging.getLogger("app.services.usage")
    usage_logger.addHandler(collector)
    usage_logger.setLevel(logging.INFO)
    try:
        if database_url:
            ensure_database(database_url)
            database.DATABASE_URL = database_url
        eval_user_id = reset_tables()
        await index_corpus(corpus, cache, eval_user_id)
        vectors = await cache.embed([q["question"] for q in questions])
        log.append({"type": "indexing", "usage": collector.take()})

        done = log.done_ids()
        remaining = [(q, v) for q, v in zip(questions, vectors) if q["id"] not in done]
        for index, (item, vector) in enumerate(remaining, start=1):
            print(f"[{index}/{len(remaining)}] {item['id']}", flush=True)
            query = item["question"]
            if settings.query_rewrite_model and item.get("history"):
                query = await call_with_retries(
                    lambda: rewrite_fn(item["question"], history_of(item),
                                       settings.query_rewrite_model),
                    f"rewrite {item['id']}", sleep,
                )
                await sleep(pace_seconds)
                print(f"    rewritten: {query}", flush=True)
                [vector] = await cache.embed([query])
            record = await evaluate_question(
                item, vector, settings, answer_fn, judge_fn, sleep, pace_seconds, query=query,
                owner_user_id=eval_user_id,
            )
            record["usage"] = collector.take()
            log.append(record)
    finally:
        usage_logger.removeHandler(collector)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m evals.run_evals", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=["retrieval", "answers"], default="retrieval")
    parser.add_argument("--estimate-only", action="store_true",
                        help="print the cost estimate and stop (no AI calls)")
    parser.add_argument("--yes", action="store_true",
                        help="skip the confirmation (only after the estimate was approved)")
    parser.add_argument("--resume", metavar="RUN_ID", help="continue a stopped run")
    parser.add_argument("--answer-model", help="override EVAL_ANSWER_MODEL")
    parser.add_argument("--judge-model", help="override OPENROUTER_JUDGE_MODEL")
    parser.add_argument("--label", help="name for the report files")
    parser.add_argument("--report-only", action="store_true",
                        help="with --resume: write a (possibly partial) report from the saved "
                             "progress, without calling any model")
    parser.add_argument("--stop-reason",
                        help="with --report-only: why the run stopped, shown in a partial report")
    parser.add_argument("--skip-arabic-pdf", action="store_true",
                        help="leave out the questions sourced from the two Arabic PDFs "
                             "(known to fail); the report lists them")
    parser.add_argument("--category", action="append", metavar="CATEGORY",
                        help="evaluate only this question category (repeatable), "
                             "e.g. --category follow_up")
    parser.add_argument("--query-rewrite", choices=["on", "off"],
                        help="rewrite follow-up questions before retrieval "
                             "(default: QUERY_REWRITE_ENABLED, off if unset)")
    parser.add_argument("--rewrite-model",
                        help=f"override EVAL_REWRITE_MODEL (default: {DEFAULT_REWRITE_MODEL})")
    parser.add_argument("--retrieval-mode", choices=["vector", "hybrid"],
                        help="override RETRIEVAL_MODE (default: hybrid)")
    return parser.parse_args(argv)


async def main_async(args: argparse.Namespace) -> int:
    questions = load_questions()
    if args.category:
        unknown = sorted(set(args.category) - {q["category"] for q in questions})
        if unknown:
            raise SystemExit(f"No questions in category: {', '.join(unknown)}")
        questions = [q for q in questions if q["category"] in args.category]
    excluded = [q for q in questions if uses_arabic_pdf(q)] if args.skip_arabic_pdf else []
    questions = [q for q in questions if q not in excluded]
    corpus = prepare_corpus()
    answer_model = args.answer_model or os.getenv("EVAL_ANSWER_MODEL") or DEFAULT_ANSWER_MODEL
    judge_model = args.judge_model or os.getenv("OPENROUTER_JUDGE_MODEL") or DEFAULT_JUDGE_MODEL
    warnings = check_models(answer_model, judge_model) if args.mode == "answers" else []
    rewrite_on = (args.query_rewrite == "on") if args.query_rewrite else query_rewrite_enabled()
    # Evals rewrite with a free model, never the production QUERY_REWRITE_MODEL.
    rewrite_model = (args.rewrite_model or os.getenv("EVAL_REWRITE_MODEL")
                     or DEFAULT_REWRITE_MODEL) if rewrite_on else None
    settings = make_settings(args.mode, answer_model, judge_model, corpus,
                             excluded=excluded, exclusion_reason=ARABIC_PDF_EXCLUSION_REASON,
                             retrieval=args.retrieval_mode, categories=args.category,
                             rewrite_model=rewrite_model)
    if excluded:
        print(f"Excluding {len(excluded)} Arabic-PDF questions: "
              f"{', '.join(q['id'] for q in excluded)}\n")
    pace_seconds = float(os.getenv("EVAL_MIN_SECONDS_BETWEEN_CALLS", "4"))

    resumed = None
    if args.resume:
        resumed = RunLog.resume(RUNS_DIR / f"{args.resume}.jsonl", settings)
    elif args.report_only:
        raise SystemExit("--report-only needs --resume <run-id>.")
    if args.report_only:
        summary = summarize(resumed, questions, corpus, stop_reason=args.stop_reason)
        md_path, json_path = write_report(summary, args.label)
        state = "partial" if summary["partial"] else "complete"
        print(f"Wrote a {state} report ({summary['evaluated_questions']} of {len(questions)} "
              f"questions):\n  {md_path}\n  {json_path}")
        return 0
    pending = [q for q in questions if not resumed or q["id"] not in resumed.done_ids()]

    cache = EmbeddingCache(CACHE_PATH, settings.embedding_model, embed_texts)
    models = ([settings.embedding_model] + ([answer_model, judge_model] if args.mode == "answers" else [])
              + ([rewrite_model] if rewrite_model else []))
    estimate_lines = estimate(settings, corpus, pending, cache, fetch_prices(models))
    print(format_estimate(estimate_lines, settings, len(pending), pace_seconds, warnings))
    if resumed:
        print(f"\nResuming {resumed.run_id}: {len(questions) - len(pending)} questions already done.")

    if args.estimate_only:
        return 0
    if not args.yes:
        if not sys.stdin.isatty():
            print("\nNot running: confirm interactively, or pass --yes after approving the estimate.")
            return 1
        if input("\nRun it? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Cancelled.")
            return 1

    url = eval_database_url()
    if resumed:
        log = resumed
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        log = RunLog.start(RUNS_DIR / f"{stamp}-{args.mode}.jsonl", settings)
        log.append({"type": "estimate", "estimate": [asdict(line) for line in estimate_lines]})
    print(f"\nRun {log.run_id} (progress: {log.path})")
    try:
        await run(log, settings, questions, corpus, cache, pace_seconds=pace_seconds,
                  database_url=url)
    except RunStopped as stopped:
        log.append({"type": "stopped", "reason": str(stopped),
                    "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        print(f"\nStopped: {stopped}\nProgress is saved. Continue later with:\n"
              f"  python -m evals.run_evals --mode {args.mode}"
              f"{resume_flags(args.skip_arabic_pdf, args.category, rewrite_model)} "
              f"--resume {log.run_id}")
        return 2

    md_path, json_path = write_report(summarize(log, questions, corpus), args.label)
    print(f"\nReport: {md_path}\n        {json_path}")
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(main_async(parse_args())))
    except KeyboardInterrupt:
        # Every finished question is already saved in evals/runs/.
        print("\nInterrupted. Progress is saved; continue with --resume <run-id> "
              "(the newest file in evals/runs/).")
        sys.exit(2)


if __name__ == "__main__":
    main()
