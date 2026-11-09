"""Split extracted pages into overlapping chunks for embedding and retrieval.

Chunks never cross a page boundary, so every citation names exactly one page.
Each chunk is an exact slice of its page's text (with surrounding whitespace
trimmed), so `start_char`/`end_char` point back into the page.
"""

import math
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from typing import Iterable

from app.extraction import Page

# Tokens are estimated from characters, so no tokenizer dependency is needed.
# Billing always uses the provider's own token counts.
CHARS_PER_TOKEN = 4
CHUNK_MAX_TOKENS = 800
CHUNK_OVERLAP_TOKENS = 120  # 15%

# A blank line (possibly with spaces) between paragraphs.
_PARAGRAPH_BREAK = re.compile(r"\n[ \t]*\n")
# End of a sentence: English and Arabic end punctuation (optionally followed by a
# closing quote or bracket) before whitespace, or a single line break.
_SENTENCE_END = re.compile(r"[.!?؟۔…]+[\"'”’»)\]]*(?=\s)|\n")
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Chunk:
    index: int  # 0-based position in the whole document
    page_number: int | None
    text: str
    token_estimate: int
    start_char: int  # offsets of `text` inside the page's text
    end_char: int


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def chunk_pages(
    pages: Iterable[Page],
    max_tokens: int = CHUNK_MAX_TOKENS,
    overlap_tokens: int = CHUNK_OVERLAP_TOKENS,
) -> list[Chunk]:
    if max_tokens <= 0:
        raise ValueError("max_tokens must be positive.")
    # Under half, so every chunk ends past the previous one.
    if not 0 <= overlap_tokens * 2 < max_tokens:
        raise ValueError("overlap_tokens must be at least 0 and less than half of max_tokens.")

    max_chars = max_tokens * CHARS_PER_TOKEN
    overlap_chars = overlap_tokens * CHARS_PER_TOKEN
    chunks: list[Chunk] = []
    for page in pages:
        for start, end in _split_page(page.text, max_chars, overlap_chars):
            text = page.text[start:end]
            chunks.append(
                Chunk(
                    index=len(chunks),
                    page_number=page.number,
                    text=text,
                    token_estimate=estimate_tokens(text),
                    start_char=start,
                    end_char=end,
                )
            )
    return chunks


class _Boundaries:
    """Sorted character offsets where a chunk may end or start, by preference."""

    def __init__(self, text: str):
        self.paragraph = [m.start() for m in _PARAGRAPH_BREAK.finditer(text)]
        self.sentence = [m.end() for m in _SENTENCE_END.finditer(text)]
        self.word = [m.start() for m in _WHITESPACE.finditer(text)]

    @staticmethod
    def last_between(positions: list[int], low: int, high: int) -> int | None:
        index = bisect_right(positions, high) - 1
        if index >= 0 and positions[index] >= low:
            return positions[index]
        return None

    @staticmethod
    def first_between(positions: list[int], low: int, high: int) -> int | None:
        index = bisect_left(positions, low)
        if index < len(positions) and positions[index] < high:
            return positions[index]
        return None


def _split_page(text: str, max_chars: int, overlap_chars: int) -> list[tuple[int, int]]:
    """(start, end) offsets of each chunk of one page, whitespace trimmed."""
    length = len(text)
    boundaries = _Boundaries(text)
    spans: list[tuple[int, int]] = []

    start = _skip_whitespace(text, 0)
    while start < length:
        end = _chunk_end(start, length, max_chars, boundaries)
        trimmed = _trim(text, start, end)
        if trimmed:
            spans.append(trimmed)
        if end >= length:
            break
        start = _next_start(text, start, end, overlap_chars, boundaries)
    return spans


def _chunk_end(start: int, length: int, max_chars: int, boundaries: _Boundaries) -> int:
    limit = start + max_chars
    if limit >= length:
        return length
    # Prefer the last paragraph break, then sentence end, then word gap in the
    # second half of the window; otherwise cut mid-word (e.g. long text without
    # spaces) so a chunk never exceeds the limit.
    low = start + max_chars // 2
    for positions in (boundaries.paragraph, boundaries.sentence, boundaries.word):
        position = boundaries.last_between(positions, low, limit)
        if position is not None:
            return position
    return limit


def _next_start(
    text: str, start: int, end: int, overlap_chars: int, boundaries: _Boundaries
) -> int:
    target = max(end - overlap_chars, start + 1)
    # Start the overlap at a sentence start if one is close, else at a word start.
    position = boundaries.first_between(boundaries.sentence, target, end)
    if position is None:
        position = boundaries.first_between(boundaries.word, target, end)
    if position is None:
        position = target if overlap_chars else end
    return _skip_whitespace(text, position)


def _skip_whitespace(text: str, position: int) -> int:
    while position < len(text) and text[position].isspace():
        position += 1
    return position


def _trim(text: str, start: int, end: int) -> tuple[int, int] | None:
    start = _skip_whitespace(text, start)
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None
