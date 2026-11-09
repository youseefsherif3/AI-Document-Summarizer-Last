"""Checks that the eval questions and corpus are consistent (step 13).

These check the question file itself, not answer quality. Arabic PDF extraction
quality is measured by the eval runner (step 14), not tested here.

The corpus and questions.md are generated (and gitignored), so the corpus is built
into a temporary folder here: the tests pass on a fresh clone.
"""

import re
from collections import Counter

import pytest

from app.extraction import extract_pages
from evals.build_corpus import build_corpus, build_document
from evals.corpus_documents import DOCUMENTS, by_filename, page_texts
from evals.render_questions import CATEGORIES, load_questions, render
from evals.text import contains_any, normalize

QUESTIONS = load_questions()
DOCS = by_filename()
ARABIC = re.compile(r"[؀-ۿ]")
FIELDS = {
    "id", "category", "language", "question", "answerable",
    "key_facts", "sources", "must_not_contain", "judge_checks", "notes",
}
# Only follow-up questions carry the earlier conversation.
OPTIONAL_FIELDS = {"history"}
# Categories allowed to have fewer than 3 questions, with their minimum: the corpus
# currently has only one true conflict and two newer-updates-older facts.
SMALL_CATEGORIES = {"conflict": 1, "newer_updates_older": 2}
# Questions whose sources can include the onboarding memo with the injected line.
INJECTION_EXPOSED = {"sf-13", "in-01", "in-02", "in-03"}


@pytest.fixture(scope="session")
def corpus_dir(tmp_path_factory):
    """The corpus, generated fresh for this test run."""
    directory = tmp_path_factory.mktemp("eval_corpus")
    build_corpus(directory)
    return directory


def extract(corpus_dir, document):
    path = corpus_dir / document.filename
    return extract_pages(path.read_bytes(), path.suffix)


def test_there_are_at_least_50_questions_in_every_category():
    counts = Counter(item["category"] for item in QUESTIONS)

    assert len(QUESTIONS) >= 50
    assert set(counts) == set(CATEGORIES)
    for category, count in counts.items():
        assert count >= SMALL_CATEGORIES.get(category, 3), (category, count)


def test_ids_are_unique():
    ids = [item["id"] for item in QUESTIONS]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("item", QUESTIONS, ids=lambda item: item["id"])
def test_question_is_well_formed(item):
    assert FIELDS <= set(item) <= FIELDS | OPTIONAL_FIELDS
    assert item["language"] in ("en", "ar")
    assert bool(ARABIC.search(item["question"])) == (item["language"] == "ar")
    assert isinstance(item["notes"], str)
    assert all(isinstance(text, str) and text for text in item["must_not_contain"])
    assert all(isinstance(check, str) and check for check in item["judge_checks"])

    if item["answerable"]:
        assert item["key_facts"] and item["sources"]
        assert all(group and all(isinstance(alt, str) and alt for alt in group)
                   for group in item["key_facts"])
    else:
        assert item["category"] == "unanswerable"
        assert item["key_facts"] == [] and item["sources"] == []


@pytest.mark.parametrize("item", [q for q in QUESTIONS if q["answerable"]], ids=lambda q: q["id"])
def test_expected_sources_exist_and_contain_every_key_fact(item):
    expected_text = []
    for source in item["sources"]:
        document = DOCS.get(source["document"])
        assert document is not None, f"unknown document {source['document']}"
        texts = page_texts(document)
        if document.numbered_pages:
            pages = source["pages"]
            assert pages, "numbered documents need page numbers"
            assert all(1 <= page <= len(texts) for page in pages), pages
            expected_text += [texts[page - 1] for page in pages]
        else:
            assert source["pages"] is None, "this document has no page numbers"
            expected_text += texts

    combined = "\n".join(expected_text)
    for group in item["key_facts"]:
        # At least one alternative must be in the source text itself (authoring check).
        assert contains_any(combined, group), f"{group} not found on the expected pages"


@pytest.mark.parametrize("item", QUESTIONS, ids=lambda item: item["id"])
def test_follow_up_questions_and_only_they_have_a_history(item):
    if item["category"] != "follow_up":
        assert "history" not in item
        return
    history = item["history"]
    # Earlier turns alternate user / assistant and end with the assistant's reply.
    assert history and len(history) % 2 == 0
    assert [m["role"] for m in history] == ["user", "assistant"] * (len(history) // 2)
    assert all(set(m) == {"role", "content"} and m["content"].strip() for m in history)


def test_two_page_questions_really_span_two_pages():
    for item in QUESTIONS:
        if item["category"] == "two_page":
            [source] = item["sources"]
            assert len(source["pages"]) == 2


def test_conflict_and_cross_document_questions_use_two_documents():
    for item in QUESTIONS:
        if item["category"] in ("conflict", "cross_document"):
            assert len({source["document"] for source in item["sources"]}) == 2


def test_update_questions_expect_the_newer_document():
    for item in QUESTIONS:
        if item["category"] == "newer_updates_older":
            assert [source["document"] for source in item["sources"]] == ["northwind_q3_report.txt"]


def test_following_the_injection_is_judged_not_text_matched():
    # An answer may quote the injected "60 days" in order to reject it, so a text match
    # cannot decide; the judge checks whether the answer followed the injection.
    for item in QUESTIONS:
        assert "60 days" not in item["must_not_contain"], item["id"]
        if item["id"] in INJECTION_EXPOSED:
            assert any("60 days" in check for check in item["judge_checks"]), item["id"]


def test_corpus_generation_is_deterministic(corpus_dir):
    for document in DOCUMENTS:
        assert (corpus_dir / document.filename).read_bytes() == build_document(document)


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda d: d.filename)
def test_extracted_page_numbering_matches_the_plan(corpus_dir, document):
    pages = extract(corpus_dir, document)

    if document.numbered_pages:
        assert [page.number for page in pages] == list(range(1, len(document.pages) + 1))
    else:
        assert [page.number for page in pages] == [None]


@pytest.mark.parametrize(
    "document",
    [d for d in DOCUMENTS if not (d.language == "ar" and d.filename.endswith(".pdf"))],
    ids=lambda d: d.filename,
)
def test_non_arabic_pdf_text_extracts_faithfully(corpus_dir, document):
    """English PDFs, DOCX and TXT: every source line comes back from extraction."""
    extracted = normalize(" ".join(page.text for page in extract(corpus_dir, document)))
    for text in page_texts(document):
        for line in text.split("\n"):
            # PDF extraction can wrap lines differently, so compare words in order.
            assert normalize(line) in extracted or all(
                word in extracted for word in normalize(line).split()
            ), line


def test_questions_markdown_lists_every_question_and_document():
    markdown = render(QUESTIONS)

    for item in QUESTIONS:
        assert f"### {item['id']}: {item['question']}" in markdown
    for document in DOCUMENTS:
        assert f"`{document.filename}`" in markdown
