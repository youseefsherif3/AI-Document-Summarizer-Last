import math

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

import app.database as database
from app.services.chunking import Chunk

DIMENSIONS = database.EMBEDDING_DIMENSIONS


def vec(*values):
    """A full-size vector whose first values are given and the rest are 0."""
    return list(values) + [0.0] * (DIMENSIONS - len(values))


def make_chunk(index, page=1, content=None):
    content = content or f"chunk {index}"
    return Chunk(
        index=index,
        page_number=page,
        text=content,
        token_estimate=database_estimate(content),
        start_char=0,
        end_char=len(content),
    )


def database_estimate(content):
    return math.ceil(len(content) / 4)


def save(filename, chunk_vectors, pages=None):
    chunks = [
        make_chunk(i, page=(pages[i] if pages else 1), content=f"{filename} chunk {i}")
        for i in range(len(chunk_vectors))
    ]
    return database.save_document_with_chunks(
        filename=filename,
        file_type=".txt",
        file_size=10,
        file_content=b"raw bytes",
        extracted_text="extracted text",
        summary="A summary.",
        document_chunks=chunks,
        embeddings=chunk_vectors,
    )


def count(table, document_id=None):
    sql = f"SELECT count(*) FROM {table}"
    params = {}
    if document_id is not None:
        sql += " WHERE document_id = :id"
        params["id"] = document_id
    with database.get_connection() as connection:
        return connection.execute(text(sql), params).scalar_one()


def test_document_and_chunks_are_saved_together():
    document_id = save("report.pdf", [vec(1), vec(0, 1), vec(0, 0, 1)], pages=[1, 2, None])

    with database.get_connection() as connection:
        rows = connection.execute(
            text(
                "SELECT chunk_index, page_number, content, token_estimate, "
                "vector_dims(embedding) AS dims FROM chunks WHERE document_id = :id "
                "ORDER BY chunk_index"
            ),
            {"id": document_id},
        ).mappings().all()

    assert [dict(r) for r in rows] == [
        {"chunk_index": 0, "page_number": 1, "content": "report.pdf chunk 0", "token_estimate": 5, "dims": DIMENSIONS},
        {"chunk_index": 1, "page_number": 2, "content": "report.pdf chunk 1", "token_estimate": 5, "dims": DIMENSIONS},
        {"chunk_index": 2, "page_number": None, "content": "report.pdf chunk 2", "token_estimate": 5, "dims": DIMENSIONS},
    ]


def test_search_returns_nearest_first_with_cosine_similarity():
    document_id = save("doc.txt", [vec(0, 1), vec(1, 1), vec(1, 0), vec(-1, 0)])

    matches = database.search_chunks(vec(1, 0), k=4)

    assert [m.chunk_index for m in matches] == [2, 1, 0, 3]
    assert [round(m.similarity, 3) for m in matches] == [1.0, 0.707, 0.0, -1.0]
    first = matches[0]
    assert (first.document_id, first.filename, first.page_number, first.content) == (
        document_id, "doc.txt", 1, "doc.txt chunk 2"
    )


def test_search_limits_results_to_k():
    save("doc.txt", [vec(1, i / 10) for i in range(10)])

    assert len(database.search_chunks(vec(1, 0), k=3)) == 3


def test_search_can_be_limited_to_some_documents():
    first = save("first.txt", [vec(1, 0), vec(0.9, 0.1)])
    second = save("second.txt", [vec(0.5, 0.5), vec(0, 1)])

    everything = database.search_chunks(vec(1, 0), k=10)
    only_second = database.search_chunks(vec(1, 0), k=10, document_ids=[second])
    both = database.search_chunks(vec(1, 0), k=10, document_ids=[first, second])

    assert {m.document_id for m in everything} == {first, second}
    assert {m.document_id for m in only_second} == {second}
    assert [m.similarity for m in only_second] == sorted(
        (m.similarity for m in only_second), reverse=True
    )
    assert len(both) == 4
    assert database.search_chunks(vec(1, 0), document_ids=[]) == []


def test_min_similarity_drops_weak_matches():
    save("doc.txt", [vec(1, 0), vec(1, 1), vec(0, 1)])

    matches = database.search_chunks(vec(1, 0), k=10, min_similarity=0.5)

    assert [round(m.similarity, 3) for m in matches] == [1.0, 0.707]


def test_deleting_a_document_deletes_its_chunks():
    keep = save("keep.txt", [vec(1, 0)])
    remove = save("remove.txt", [vec(1, 0), vec(0, 1)])

    assert database.delete_document(remove) is True

    assert count("chunks", remove) == 0
    assert count("chunks", keep) == 1
    assert {m.document_id for m in database.search_chunks(vec(1, 0), k=10)} == {keep}


def test_failed_chunk_insert_leaves_no_document():
    duplicate_index = [make_chunk(0), make_chunk(0)]

    with pytest.raises(IntegrityError):
        database.save_document_with_chunks(
            "broken.txt", ".txt", 10, b"x", "text", "summary", duplicate_index, [vec(1), vec(1)]
        )

    assert count("documents") == 0
    assert count("chunks") == 0


@pytest.mark.parametrize(
    ("chunk_list", "vectors", "message"),
    [
        ([make_chunk(0)], [vec(1), vec(1)], "1 chunks but 2 embeddings"),
        ([make_chunk(0)], [[1.0, 0.0]], f"Expected a {DIMENSIONS}-dimension vector"),
    ],
    ids=["count-mismatch", "wrong-size"],
)
def test_invalid_input_is_rejected_before_saving(chunk_list, vectors, message):
    with pytest.raises(ValueError, match=message):
        database.save_document_with_chunks(
            "bad.txt", ".txt", 10, b"x", "text", "summary", chunk_list, vectors
        )

    assert count("documents") == 0


def test_search_validates_its_input():
    with pytest.raises(ValueError, match="k must be positive"):
        database.search_chunks(vec(1), k=0)
    with pytest.raises(ValueError, match=f"Expected a {DIMENSIONS}-dimension vector"):
        database.search_chunks([1.0, 2.0])


def test_document_without_chunks_can_still_be_saved():
    document_id = save("empty.txt", [])

    assert count("documents") == 1
    assert count("chunks", document_id) == 0


def test_hnsw_index_and_cascade_are_in_the_schema():
    with database.get_connection() as connection:
        index_sql = connection.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_chunks_embedding_hnsw'")
        ).scalar_one()
        on_delete = connection.execute(
            text(
                "SELECT confdeltype FROM pg_constraint "
                "WHERE conrelid = 'chunks'::regclass AND contype = 'f'"
            )
        ).scalar_one()

    assert "USING hnsw (embedding vector_cosine_ops)" in index_sql
    assert on_delete == "c"  # c = CASCADE


def test_startup_fails_when_embedding_dimensions_change(monkeypatch):
    monkeypatch.setattr(database, "EMBEDDING_DIMENSIONS", 3)

    with pytest.raises(RuntimeError, match=f"stores {DIMENSIONS}-dimension embeddings"):
        database.initialize_database()
