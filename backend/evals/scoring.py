"""Pure scoring functions for the evals (no I/O, no AI calls)."""

import re
from dataclasses import dataclass

from .text import contains_any, normalize

CITATION = re.compile(r"\[(\d+)\]")
RETRIEVAL_KS = (1, 3, 8)

# Phrases that mean "this is not in the documents" (matched after normalize()).
REFUSAL_PHRASES = [
    "could not find", "couldn't find", "can't find", "cannot find", "did not find",
    "not in the documents", "not in your documents", "not in the provided",
    "not mentioned", "not specified", "not stated", "no information",
    "do not contain", "does not contain", "doesn't contain", "don't contain",
    "do not mention", "does not mention", "doesn't mention", "not provided",
    "not available in", "unable to find",
    "لم اجد", "لا تتضمن", "لا تحتوي", "غير مذكور", "لا توجد معلومات", "لم يتم ذكر", "لم تذكر",
]


@dataclass(frozen=True)
class Retrieved:
    rank: int  # 1-based
    document: str
    page: int | None
    similarity: float


def source_matches(source: dict, document: str, page: int | None) -> bool:
    """A retrieved chunk matches an expected source: same document, and an expected page
    (documents without page numbers match on the document alone)."""
    if source["document"] != document:
        return False
    return source["pages"] is None or page in source["pages"]


def retrieval_metrics(item: dict, retrieved: list[Retrieved]) -> dict:
    """hit@k, reciprocal rank, and (for several expected sources) whether all were found."""
    sources = item["sources"]
    first_rank = next(
        (r.rank for r in retrieved if any(source_matches(s, r.document, r.page) for s in sources)),
        None,
    )
    metrics = {f"hit@{k}": first_rank is not None and first_rank <= k for k in RETRIEVAL_KS}
    metrics["reciprocal_rank"] = 0.0 if first_rank is None else 1.0 / first_rank
    metrics["first_hit_rank"] = first_rank
    metrics["all_sources_found"] = all(
        any(source_matches(s, r.document, r.page) for r in retrieved) for s in sources
    )
    return metrics


def key_fact_result(item: dict, answer: str) -> dict:
    groups = item["key_facts"]
    missing = [group for group in groups if not contains_any(answer, group)]
    return {"found": len(groups) - len(missing), "total": len(groups), "missing": missing}


def forbidden_found(item: dict, answer: str) -> list[str]:
    return [text for text in item["must_not_contain"] if contains_any(answer, [text])]


def looks_like_refusal(answer: str) -> bool:
    text = normalize(answer)
    return any(normalize(phrase) in text for phrase in REFUSAL_PHRASES)


def cited_numbers(answer: str) -> list[int]:
    return [int(n) for n in CITATION.findall(answer)]


def citation_correctness(item: dict, answer: str, sources: list[dict]) -> dict:
    """How many [n] citations point to a source that matches an expected document/page.

    `sources` are the numbered sources the model saw: {n, document, page}.
    """
    by_number = {source["n"]: source for source in sources}
    citations = cited_numbers(answer)
    correct = sum(
        1
        for n in citations
        if n in by_number
        and any(source_matches(s, by_number[n]["document"], by_number[n]["page"]) for s in item["sources"])
    )
    unknown = sum(1 for n in citations if n not in by_number)
    return {"correct": correct, "total": len(citations), "unknown_numbers": unknown}


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟])\s+")
_LIST_MARKER = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def answer_units(answer: str) -> list[str]:
    """Claims to check for a citation: list items and sentences, skipping headings and
    very short fragments."""
    units = []
    for line in answer.splitlines():
        line = line.strip()
        if not line:
            continue
        if _LIST_MARKER.match(line):
            units.append(line)
            continue
        units += [part for part in _SENTENCE_SPLIT.split(line) if part.strip()]
    return [
        unit for unit in units
        if len(CITATION.sub("", unit).strip()) >= 15 and not unit.rstrip().endswith(":")
    ]


def citation_coverage(answer: str) -> dict:
    units = answer_units(answer)
    cited = sum(1 for unit in units if CITATION.search(unit))
    return {"cited": cited, "total": len(units)}
