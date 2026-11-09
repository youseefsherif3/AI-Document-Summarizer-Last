"""Hybrid search (step 15): full-text search + vector search, merged with RRF."""

import pytest
from sqlalchemy import text

import app.database as database
import app.services.chat as chat
import app.services.retrieval as retrieval
from app.services.chunking import Chunk

DIMENSIONS = database.EMBEDDING_DIMENSIONS


def vec(*values):
    return list(values) + [0.0] * (DIMENSIONS - len(values))


def save(filename, contents_and_vectors, pages=None):
    chunks = [
        Chunk(index=i, page_number=(pages[i] if pages else i + 1), text=content,
              token_estimate=5, start_char=0, end_char=len(content))
        for i, (content, _) in enumerate(contents_and_vectors)
    ]
    return database.save_document_with_chunks(
        filename, ".txt", 10, b"x", "text", "summary", chunks,
        [vector for _, vector in contents_and_vectors],
    )


# --- Pure parts -----------------------------------------------------------------------


def test_tsquery_keeps_meaningful_words_joined_with_or():
    assert retrieval.build_tsquery("Which team owns the RouteWise tool at Brightwater?") == (
        "team | owns | routewise | tool | brightwater"
    )


def test_tsquery_drops_operators_punctuation_and_duplicates():
    query = retrieval.build_tsquery("Northwind's EUR 1,200 budget & (budget) | !budget: 10:00")
    assert query == "northwind | eur | 200 | budget | 10 | 00"


def test_tsquery_handles_arabic_and_strips_diacritics():
    assert retrieval.build_tsquery("كم كتابًا يمكن استعارته من مكتبة الواحة؟") == (
        "كتابا | يمكن | استعارته | مكتبة | الواحة"
    )


def test_tsquery_is_none_when_only_stop_words_remain():
    assert retrieval.build_tsquery("What is it?") is None


def test_reciprocal_rank_fusion_order():
    # a: 1/61 (vector #1); b: 1/62 + 1/61 (vector #2, text #1); c: 1/62 (text #2).
    assert retrieval.reciprocal_rank_fusion([["a", "b"], ["b", "c"]]) == ["b", "a", "c"]


def test_fusion_ties_keep_the_vector_order():
    assert retrieval.reciprocal_rank_fusion([["a", "b"], ["c", "d"]]) == ["a", "c", "b", "d"]


@pytest.mark.parametrize(("value", "mode"), [(None, "hybrid"), ("", "hybrid"), ("VECTOR", "vector"), ("HYBRID", "hybrid")])
def test_retrieval_mode_setting(monkeypatch, value, mode):
    if value is None:
        monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    else:
        monkeypatch.setenv("RETRIEVAL_MODE", value)
    assert retrieval.retrieval_mode() == mode


def test_invalid_retrieval_mode_is_rejected(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_MODE", "keyword")
    with pytest.raises(ValueError, match="RETRIEVAL_MODE"):
        retrieval.retrieval_mode()


# --- With the test database --------------------------------------------------------


def test_full_text_column_and_gin_index_exist():
    save("doc.txt", [("Approved software: RouteWise", vec(1))])
    with database.get_connection() as connection:
        tsv = connection.execute(text("SELECT content_tsv::text FROM chunks")).scalar_one()
        index = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chunks_content_tsv'")
        ).scalar_one()

    assert tsv == "'approved':1 'routewise':3 'software':2"
    assert "USING gin (content_tsv)" in index


def test_hybrid_finds_an_exact_term_that_vector_search_misses():
    save("doc.txt", [
        ("General remarks about many tools.", vec(1, 0)),
        ("More general remarks.", vec(0.9, 0.1)),
        ("The RouteWise tool is owned by Operations.", vec(0, 1)),  # far from the query vector
    ])
    query = vec(1, 0)

    vector_only = chat.find_matches("Who owns RouteWise?", query, k=2, mode="vector")
    hybrid = chat.find_matches("Who owns RouteWise?", query, k=2, mode="hybrid")

    assert "RouteWise" not in " ".join(m.content for m in vector_only)
    assert hybrid[0].content == "The RouteWise tool is owned by Operations."
    assert hybrid[0].similarity == pytest.approx(0.0)  # still reports vector similarity


def test_an_arabic_term_matches_with_the_simple_config():
    save("ar.txt", [("تقع مكتبة الواحة في وسط المدينة.", vec(0, 1)), ("نص آخر.", vec(1, 0))])

    matches = database.full_text_search_chunks(vec(1, 0), retrieval.build_tsquery("أين مكتبة الواحة؟"))

    assert [m.content for m in matches] == ["تقع مكتبة الواحة في وسط المدينة."]


def test_more_matching_words_rank_higher_in_full_text_search():
    save("doc.txt", [
        ("Brightwater has a tool.", vec(1)),
        ("Brightwater RouteWise route planning tool.", vec(1)),
    ])

    matches = database.full_text_search_chunks(
        vec(1), retrieval.build_tsquery("Brightwater RouteWise route planning")
    )

    assert matches[0].content == "Brightwater RouteWise route planning tool."


def test_document_filter_applies_to_both_lists():
    keep = save("keep.txt", [("Nothing relevant here.", vec(1, 0))])
    save("other.txt", [("RouteWise RouteWise RouteWise", vec(1, 0))])

    matches = chat.find_matches("RouteWise?", vec(1, 0), document_ids=[keep], k=8, mode="hybrid")

    assert {m.document_id for m in matches} == {keep}


def test_hybrid_returns_at_most_k_and_applies_the_threshold():
    save("doc.txt", [(f"Chunk {i} about RouteWise.", vec(1, i / 10)) for i in range(12)]
         + [("RouteWise only by words.", vec(0, 1))])

    top = chat.find_matches("RouteWise", vec(1, 0), k=8, mode="hybrid")
    filtered = chat.find_matches("RouteWise", vec(1, 0), k=8, mode="hybrid", threshold=0.5)

    assert len(top) == 8
    assert all(m.similarity >= 0.5 for m in filtered)


def test_question_without_meaningful_words_falls_back_to_vector_order():
    save("doc.txt", [("First.", vec(1, 0)), ("Second.", vec(0.5, 0.5))])

    hybrid = chat.find_matches("What is it?", vec(1, 0), k=2, mode="hybrid")
    vector = chat.find_matches("What is it?", vec(1, 0), k=2, mode="vector")

    assert [m.chunk_id for m in hybrid] == [m.chunk_id for m in vector]


def test_hybrid_mode_is_the_default(monkeypatch):
    monkeypatch.delenv("RETRIEVAL_MODE", raising=False)
    save("doc.txt", [("The RouteWise tool is owned by Operations.", vec(0, 1)),
                     ("Unrelated text.", vec(1, 0))])

    # Vector search alone ranks the unrelated chunk first; full-text search lifts RouteWise.
    assert chat.find_matches("Who owns RouteWise?", vec(1, 0), k=2)[0].content == (
        "The RouteWise tool is owned by Operations."
    )


def test_vector_mode_never_runs_full_text_search(monkeypatch):
    monkeypatch.setenv("RETRIEVAL_MODE", "vector")
    monkeypatch.setattr(retrieval, "full_text_search_chunks",
                        lambda *a, **k: pytest.fail("full-text search must not run in vector mode"))
    save("doc.txt", [("RouteWise", vec(1))])

    assert len(chat.find_matches("RouteWise", vec(1))) == 1
