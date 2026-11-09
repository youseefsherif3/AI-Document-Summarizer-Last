"""Hybrid retrieval (step 15): vector search plus Postgres full-text search, merged with
reciprocal rank fusion. Chosen with RETRIEVAL_MODE=hybrid|vector (default: hybrid).
"""

import os
import re
from typing import Sequence

from app.database import ChunkMatch, full_text_search_chunks, search_chunks

RETRIEVAL_MODES = ("vector", "hybrid")
HYBRID_CANDIDATES = 20  # chunks taken from each list before fusion
RRF_K = 60  # the usual reciprocal rank fusion constant

# Words too common to help full-text search. The 'simple' config keeps them, and a
# question is mostly made of them ("What is the ... of the ...?").
STOP_WORDS = frozenset(
    """
    a an the is are was were be been being do does did what which who whom whose when where
    why how many much of in on at to for from by with about and or if it its this that these
    those there their they them can could should would will may might must has have had i my
    me we our you your he she his her as than then so not no any all each per get gets into
    out up down over under more most some tell please
    في من على إلى الى عن ما ماذا هل هو هي كم متى كيف أين اين لماذا أي اي التي الذي الذين هذا
    هذه ذلك تلك مع أو او و ثم لا لم لن قد كان كانت يكون تكون بين عند كل بعد قبل
    """.split()
)
_ARABIC_MARKS = re.compile(r"[ً-ْٰـ]")
_WORD = re.compile(r"[^\W_]+")


def retrieval_mode() -> str:
    mode = os.getenv("RETRIEVAL_MODE", "hybrid").strip().lower() or "hybrid"
    if mode not in RETRIEVAL_MODES:
        raise ValueError(f"RETRIEVAL_MODE must be one of {', '.join(RETRIEVAL_MODES)}, got {mode!r}.")
    return mode


def build_tsquery(question: str) -> str | None:
    """A to_tsquery expression that matches chunks containing ANY of the question's
    meaningful words (`word1 | word2 | ...`), or None if there are none.

    Only plain letters and digits are kept, so the expression can't contain tsquery
    operators. More matching words rank higher (ts_rank_cd).
    """
    words = _WORD.findall(_ARABIC_MARKS.sub("", question).lower())
    terms = dict.fromkeys(word for word in words if len(word) > 1 and word not in STOP_WORDS)
    return " | ".join(terms) or None


def reciprocal_rank_fusion(ranked_lists: Sequence[Sequence[int]], k: int = RRF_K) -> list[int]:
    """Merge ranked lists of IDs: each item scores the sum of 1 / (k + rank) over the lists
    it appears in. Ties keep the order of first appearance (the vector list comes first)."""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, item in enumerate(ranked, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda item: -scores[item])


def hybrid_search(
    question: str,
    query_embedding: Sequence[float],
    k: int,
    document_ids: Sequence[int] | None = None,
    min_similarity: float = 0.0,
) -> list[ChunkMatch]:
    """Top k chunks from vector search and full-text search, merged with RRF.

    The similarity threshold (if any) is applied to each chunk's vector similarity after
    fusion; 0 disables it, as in vector mode.
    """
    candidates = max(k, HYBRID_CANDIDATES)
    vector_matches = search_chunks(query_embedding, k=candidates, document_ids=document_ids)
    tsquery = build_tsquery(question)
    text_matches = (
        full_text_search_chunks(query_embedding, tsquery, k=candidates, document_ids=document_ids)
        if tsquery
        else []
    )
    by_id = {match.chunk_id: match for match in [*vector_matches, *text_matches]}
    fused = reciprocal_rank_fusion(
        [[m.chunk_id for m in vector_matches], [m.chunk_id for m in text_matches]]
    )
    matches = [by_id[chunk_id] for chunk_id in fused]
    if min_similarity > 0:
        matches = [match for match in matches if match.similarity >= min_similarity]
    return matches[:k]
