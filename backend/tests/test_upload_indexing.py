import asyncio
import json
from io import BytesIO
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.datastructures import UploadFile as StarletteUploadFile

import app.database as database
import app.main as main
from tests.default_user import DEFAULT_USER_EMAIL, DEFAULT_USER_ID
from app.auth import User
from tests.pdf_builder import build_text_pdf

DIMENSIONS = database.EMBEDDING_DIMENSIONS


def sse_events(body: str) -> list[dict]:
    return [
        json.loads(line[len("data: "):])
        for frame in body.split("\n\n")
        for line in frame.split("\n")
        if line.startswith("data: ")
    ]


def rows(sql):
    with database.get_connection() as connection:
        return [dict(r) for r in connection.execute(text(sql)).mappings().all()]


def saved_counts():
    return (
        rows("SELECT count(*) AS n FROM documents")[0]["n"],
        rows("SELECT count(*) AS n FROM chunks")[0]["n"],
    )


class Summary:
    """A fake summary stream that records whether it was closed."""

    def __init__(self, parts=("A summary.",), error=None, delay=0.0):
        self.parts = parts
        self.error = error
        self.delay = delay
        self.sent = 0
        self.closed = False

    async def __call__(self, extracted_text):
        try:
            for part in self.parts:
                if self.delay:
                    await asyncio.sleep(self.delay)
                self.sent += 1
                yield part
            if self.error:
                raise self.error
        finally:
            self.closed = True


class SlowEmbeddings:
    """Fake embed_texts that waits, and records whether it was cancelled."""

    def __init__(self, seconds=30.0, error=None):
        self.seconds = seconds
        self.error = error
        self.started = False
        self.cancelled = False

    async def __call__(self, texts):
        self.started = True
        try:
            await asyncio.sleep(self.seconds)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if self.error:
            raise self.error
        return [[1.0] + [0.0] * (DIMENSIONS - 1) for _ in texts]


def upload(client, filename, content, content_type="text/plain"):
    response = client.post(
        "/documents/upload/stream", files={"file": (filename, content, content_type)}
    )
    return sse_events(response.text)


def test_pdf_upload_saves_chunks_with_page_numbers(monkeypatch, fake_embeddings):
    monkeypatch.setattr(main, "stream_summary", Summary())
    pdf = build_text_pdf(["Alpha is on page one.", "Beta is on page two.", "Gamma is on page three."])

    with TestClient(main.app) as client:
        events = upload(client, "pages.pdf", pdf, "application/pdf")

    assert events[-1]["type"] == "done"
    document_id = events[-1]["document_id"]
    saved = rows("SELECT document_id, chunk_index, page_number, content FROM chunks ORDER BY chunk_index")
    assert [(r["document_id"], r["chunk_index"], r["page_number"]) for r in saved] == [
        (document_id, 0, 1),
        (document_id, 1, 2),
        (document_id, 2, 3),
    ]
    assert "Alpha" in saved[0]["content"] and "Gamma" in saved[2]["content"]
    # One embedding call with exactly the chunk texts.
    assert fake_embeddings == [[r["content"] for r in saved]]


def test_txt_upload_without_pages_saves_unnumbered_chunks(monkeypatch):
    monkeypatch.setattr(main, "stream_summary", Summary())

    with TestClient(main.app) as client:
        events = upload(client, "notes.txt", b"Just one page of notes.")

    assert events[-1]["type"] == "done"
    assert rows("SELECT page_number, content FROM chunks") == [
        {"page_number": None, "content": "Just one page of notes."}
    ]


def test_embedding_failure_saves_nothing(monkeypatch):
    summary = Summary(parts=("Part one. ", "Part two."))
    monkeypatch.setattr(main, "stream_summary", summary)

    async def failing_embeddings(texts):
        raise ConnectionError("Could not connect to the AI service.")

    monkeypatch.setattr(main, "embed_texts", failing_embeddings)

    with TestClient(main.app) as client:
        events = upload(client, "doc.txt", b"Some text.")

    assert events[-1] == {
        "type": "error", "status": 503, "message": "Could not connect to the AI service."
    }
    assert not any(e["type"] == "done" for e in events)
    assert saved_counts() == (0, 0)
    assert summary.closed


def test_embedding_failure_stops_the_summary_early(monkeypatch):
    summary = Summary(parts=[f"part {i} " for i in range(50)], delay=0.005)
    monkeypatch.setattr(main, "stream_summary", summary)

    async def failing_embeddings(texts):
        raise main.AIRateLimitError("The AI service rate limit has been exceeded.")

    monkeypatch.setattr(main, "embed_texts", failing_embeddings)

    with TestClient(main.app) as client:
        events = upload(client, "doc.txt", b"Some text.")

    deltas = [e for e in events if e["type"] == "delta"]
    assert 0 < len(deltas) < 50
    assert summary.sent < 50 and summary.closed
    assert events[-1]["type"] == "error" and events[-1]["status"] == 429
    assert saved_counts() == (0, 0)


def test_summary_failure_cancels_embedding_and_saves_nothing(monkeypatch):
    monkeypatch.setattr(
        main,
        "stream_summary",
        Summary(parts=("Partial",), error=main.AIServiceError("The AI service returned an API error: 402")),
    )
    embeddings = SlowEmbeddings(seconds=30)
    monkeypatch.setattr(main, "embed_texts", embeddings)

    with TestClient(main.app) as client:
        events = upload(client, "doc.txt", b"Some text.")

    assert events[-1] == {
        "type": "error", "status": 502, "message": "The AI service returned an API error: 402"
    }
    assert embeddings.cancelled
    assert saved_counts() == (0, 0)


def test_cancelling_mid_stream_cancels_embedding_and_saves_nothing(monkeypatch):
    summary = Summary(parts=("First part. ", "Never sent."))
    monkeypatch.setattr(main, "stream_summary", summary)
    embeddings = SlowEmbeddings(seconds=30)
    monkeypatch.setattr(main, "embed_texts", embeddings)

    async def cancel_after_first_event():
        file = StarletteUploadFile(filename="cancel.txt", file=BytesIO(b"Text to cancel."))
        response = await main.upload_document_stream(
            file, user=User(DEFAULT_USER_ID, DEFAULT_USER_EMAIL)
        )
        first = await anext(response.body_iterator)
        await asyncio.sleep(0.05)  # embedding is now in progress
        await response.body_iterator.aclose()  # what a client disconnect does
        return first

    with TestClient(main.app):
        first = asyncio.run(cancel_after_first_event())

    assert '"type": "delta"' in first
    assert summary.closed
    assert embeddings.started and embeddings.cancelled
    assert saved_counts() == (0, 0)


def test_done_waits_for_slower_embeddings(monkeypatch):
    monkeypatch.setattr(main, "stream_summary", Summary())
    monkeypatch.setattr(main, "embed_texts", SlowEmbeddings(seconds=0.05))

    with TestClient(main.app) as client:
        events = upload(client, "doc.txt", b"Some text.")

    assert events[-1]["type"] == "done"
    assert saved_counts() == (1, 1)


def test_database_failure_is_reported_and_logged(monkeypatch, caplog):
    monkeypatch.setattr(main, "stream_summary", Summary())

    def broken_save(**kwargs):
        raise RuntimeError("database is down")

    monkeypatch.setattr(main, "save_document_with_chunks", broken_save)

    with TestClient(main.app) as client:
        events = upload(client, "doc.txt", b"Some text.")

    assert events[-1] == {"type": "error", "status": 500, "message": "Could not process the document."}
    assert "Processing upload 'doc.txt' failed." in caplog.text
    assert "database is down" in caplog.text


def mock_blob(monkeypatch, content: bytes):
    deleted = []

    async def head(pathname):
        return SimpleNamespace(size=len(content))

    async def get(pathname, *, access):
        return SimpleNamespace(body=content)

    async def delete(pathname):
        deleted.append(pathname)

    monkeypatch.setattr(main.blob, "head_blob", head)
    monkeypatch.setattr(main.blob, "get_blob", get)
    monkeypatch.setattr(main.blob, "delete_blob", delete)
    return deleted


def blob_upload(client, content: bytes):
    response = client.post(
        "/documents/upload/stream/blob",
        json={
            "pathname": "documind/uploads/1/file-random.txt",
            "filename": "blob.txt",
            "file_size": len(content),
        },
    )
    return sse_events(response.text)


def test_blob_upload_indexes_and_deletes_the_temporary_file(monkeypatch):
    content = b"Blob document content."
    deleted = mock_blob(monkeypatch, content)
    monkeypatch.setattr(main, "stream_summary", Summary())

    with TestClient(main.app) as client:
        events = blob_upload(client, content)

    assert events[-1]["type"] == "done"
    assert rows("SELECT content FROM chunks") == [{"content": "Blob document content."}]
    assert deleted == ["documind/uploads/1/file-random.txt"]


def test_blob_embedding_failure_still_deletes_the_temporary_file(monkeypatch):
    content = b"Blob document content."
    deleted = mock_blob(monkeypatch, content)
    monkeypatch.setattr(main, "stream_summary", Summary())

    async def failing_embeddings(texts):
        raise TimeoutError("The AI service took too long to respond.")

    monkeypatch.setattr(main, "embed_texts", failing_embeddings)

    with TestClient(main.app) as client:
        events = blob_upload(client, content)

    assert events[-1]["status"] == 504
    assert saved_counts() == (0, 0)
    assert deleted == ["documind/uploads/1/file-random.txt"]
