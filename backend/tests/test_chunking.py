import pytest

from app.extraction import Page
from app.services.chunking import CHARS_PER_TOKEN, Chunk, chunk_pages, estimate_tokens

# Small sizes keep the test texts short: 50 tokens = 200 chars, overlap 8 tokens = 32 chars.
MAX_TOKENS = 50
OVERLAP_TOKENS = 8
MAX_CHARS = MAX_TOKENS * CHARS_PER_TOKEN


def chunk(pages, **kwargs):
    kwargs.setdefault("max_tokens", MAX_TOKENS)
    kwargs.setdefault("overlap_tokens", OVERLAP_TOKENS)
    return chunk_pages(pages, **kwargs)


def sentences(count, word="word"):
    return " ".join(f"Sentence {i} has a {word} in it." for i in range(count))


def assert_valid_chunks_of(page: Page, chunks: list[Chunk]):
    """Each chunk is an exact slice of its page; together they cover the page with overlap."""
    text = page.text
    assert chunks, "expected at least one chunk"
    for item in chunks:
        assert item.page_number == page.number
        assert text[item.start_char:item.end_char] == item.text
        assert item.text == item.text.strip()
        assert len(item.text) <= MAX_CHARS
        assert item.token_estimate == estimate_tokens(item.text) <= MAX_TOKENS
    assert chunks[0].start_char == len(text) - len(text.lstrip())
    assert chunks[-1].end_char == len(text.rstrip())
    for previous, current in zip(chunks, chunks[1:]):
        assert previous.start_char < current.start_char
        # No gap: the next chunk starts before (overlap) or right at the previous end.
        gap = text[previous.end_char:current.start_char]
        assert current.start_char <= previous.end_char or gap.strip() == ""


def test_short_page_is_one_chunk():
    page = Page(number=3, text="  A short page.\n")

    [only] = chunk([page])

    assert only == Chunk(
        index=0, page_number=3, text="A short page.", token_estimate=4, start_char=2, end_char=15
    )


def test_empty_pages_are_skipped_and_indexes_have_no_gaps():
    pages = [
        Page(1, "First page."),
        Page(2, ""),
        Page(3, "   \n\n  "),
        Page(4, "Fourth page."),
    ]

    chunks = chunk(pages)

    assert [(c.index, c.page_number, c.text) for c in chunks] == [
        (0, 1, "First page."),
        (1, 4, "Fourth page."),
    ]


def test_chunks_never_cross_pages():
    pages = [Page(1, sentences(20, "cat")), Page(2, sentences(20, "dog"))]

    chunks = chunk(pages)

    first = [c for c in chunks if c.page_number == 1]
    second = [c for c in chunks if c.page_number == 2]
    assert len(first) > 1 and len(second) > 1
    assert all("dog" not in c.text for c in first)
    assert all("cat" not in c.text for c in second)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert_valid_chunks_of(pages[0], first)
    assert_valid_chunks_of(pages[1], second)


def test_consecutive_chunks_overlap():
    page = Page(1, sentences(30))

    chunks = chunk([page])

    assert len(chunks) > 2
    for previous, current in zip(chunks, chunks[1:]):
        overlap = previous.end_char - current.start_char
        assert 0 < overlap <= OVERLAP_TOKENS * CHARS_PER_TOKEN
        assert previous.text.endswith(page.text[current.start_char:previous.end_char])


def test_no_overlap_when_disabled():
    page = Page(1, sentences(30))

    chunks = chunk([page], overlap_tokens=0)

    for previous, current in zip(chunks, chunks[1:]):
        assert current.start_char >= previous.end_char
    assert_valid_chunks_of(page, chunks)


def test_chunks_end_at_paragraph_breaks_when_possible():
    paragraphs = [f"Paragraph {i}. " + "Filler text goes here. " * 5 for i in range(6)]
    page = Page(1, "\n\n".join(p.strip() for p in paragraphs))

    chunks = chunk([page])

    assert len(chunks) > 1
    for item in chunks[:-1]:
        assert page.text[item.end_char:item.end_char + 2] == "\n\n"
    assert_valid_chunks_of(page, chunks)


def test_long_paragraph_splits_at_sentence_ends():
    page = Page(1, sentences(30))  # one paragraph, no blank lines

    chunks = chunk([page])

    assert all(item.text.endswith(".") for item in chunks)
    assert all(item.text.startswith("Sentence") for item in chunks)
    assert_valid_chunks_of(page, chunks)


def test_arabic_text_splits_at_sentence_ends_and_whole_words():
    text = " ".join(f"هذه الجملة رقم {i} تتحدث عن المكتبة العامة في المدينة؟" for i in range(20))
    page = Page(1, text)

    chunks = chunk([page])

    assert len(chunks) > 1
    assert all(item.text.endswith("؟") for item in chunks)
    original_words = set(text.split())
    for item in chunks:
        assert set(item.text.split()) <= original_words  # no word was cut in half
    assert_valid_chunks_of(page, chunks)


def test_text_without_spaces_is_still_split_within_the_limit():
    page = Page(None, "مكتبة" * 200)  # 1000 characters, no spaces or punctuation

    chunks = chunk([page])

    assert len(chunks) > 1
    assert all(len(item.text) <= MAX_CHARS for item in chunks)
    assert_valid_chunks_of(page, chunks)


def test_a_single_overlong_word_is_split():
    page = Page(1, "Start. " + "x" * 450 + " End.")

    chunks = chunk([page])

    assert all(len(item.text) <= MAX_CHARS for item in chunks)
    assert_valid_chunks_of(page, chunks)


def test_chunking_is_deterministic():
    pages = [Page(1, sentences(25)), Page(2, "Short.")]

    assert chunk(pages) == chunk(pages)


def test_default_sizes_are_800_tokens_with_15_percent_overlap():
    page = Page(1, "\n\n".join(sentences(12) for _ in range(24)))  # about 8,400 chars

    chunks = chunk_pages([page])

    assert len(chunks) > 2
    assert all(item.token_estimate <= 800 for item in chunks)
    assert all(500 <= item.token_estimate for item in chunks[:-1])
    for previous, current in zip(chunks, chunks[1:]):
        assert 0 < previous.end_char - current.start_char <= 120 * CHARS_PER_TOKEN


@pytest.mark.parametrize(
    ("max_tokens", "overlap_tokens"),
    [(0, 0), (-5, 0), (50, -1), (50, 25), (50, 60)],
)
def test_invalid_sizes_are_rejected(max_tokens, overlap_tokens):
    with pytest.raises(ValueError):
        chunk_pages([Page(1, "Text.")], max_tokens=max_tokens, overlap_tokens=overlap_tokens)
