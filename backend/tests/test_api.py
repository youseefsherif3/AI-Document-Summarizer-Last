
import asyncio
from io import BytesIO
from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.main import app
import app.main as main


def test_health_check():
    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_stream_upload_sends_chunks_and_persists_completed_summary(monkeypatch):
    async def mock_stream(text):
        yield "A streamed "
        yield "summary."

    monkeypatch.setattr(main, "stream_summary", mock_stream)
    with TestClient(app) as client:
        response = client.post(
            "/documents/upload/stream",
            files={"file": ("stream.txt", b"Stream this document.", "text/plain")},
        )
        dashboard = client.get("/dashboard").json()

    assert response.status_code == 200
    assert '"type": "delta"' in response.text
    assert '"type": "done"' in response.text
    assert dashboard["total_documents"] == 1
    assert dashboard["completed_documents"] == 1
    assert dashboard["recent_documents"][0]["summary"] == "A streamed summary."


def test_stream_upload_reports_provider_error_without_saving_document(monkeypatch):
    async def mock_stream(text):
        yield "Partial text"
        raise main.AIRateLimitError("Provider rate limit.")

    monkeypatch.setattr(main, "stream_summary", mock_stream)
    with TestClient(app) as client:
        response = client.post(
            "/documents/upload/stream",
            files={"file": ("limited.txt", b"Some text.", "text/plain")},
        )
        documents = client.get("/documents").json()

    assert '"type": "error"' in response.text
    assert "Provider rate limit." in response.text
    assert documents == []


def test_cancelled_stream_does_not_save_partial_document(monkeypatch):
    closed = []

    async def mock_stream(text):
        try:
            yield "Partial text"
            yield "Never reached"
        finally:
            closed.append(True)

    monkeypatch.setattr(main, "stream_summary", mock_stream)

    async def cancel_after_first_chunk():
        upload = StarletteUploadFile(
            filename="cancelled.txt", file=BytesIO(b"A document to cancel.")
        )
        response = await main.upload_document_stream(upload)
        iterator = response.body_iterator
        first_event = await anext(iterator)
        await iterator.aclose()
        return first_event

    with TestClient(app) as client:
        first_event = asyncio.run(cancel_after_first_chunk())
        documents = client.get("/documents").json()

    assert '"type": "delta"' in first_event
    assert closed == [True]
    assert documents == []


def test_blob_stream_saves_summary_and_deletes_temporary_upload(monkeypatch):
    deleted = []

    async def mock_head(pathname):
        return SimpleNamespace(size=len(b"Blob document content."))

    async def mock_get(pathname, *, access):
        assert access == "private"
        return SimpleNamespace(body=b"Blob document content.")

    async def mock_delete(pathname):
        deleted.append(pathname)

    async def mock_stream(text):
        yield "A blob summary."

    monkeypatch.setattr(main.blob, "head_blob", mock_head)
    monkeypatch.setattr(main.blob, "get_blob", mock_get)
    monkeypatch.setattr(main.blob, "delete_blob", mock_delete)
    monkeypatch.setattr(main, "stream_summary", mock_stream)

    with TestClient(app) as client:
        response = client.post(
            "/documents/upload/stream/blob",
            json={
                "pathname": "documind/uploads/file-random.txt",
                "filename": "blob.txt",
                "file_size": len(b"Blob document content."),
            },
        )
        documents = client.get("/documents").json()

    assert response.status_code == 200
    assert '"type": "done"' in response.text
    assert documents[0]["summary"] == "A blob summary."
    assert deleted == ["documind/uploads/file-random.txt"]


def test_cancelled_blob_stream_deletes_temporary_upload_without_persisting(monkeypatch):
    pathname = "documind/uploads/cancel-random.txt"
    deleted = []

    async def mock_head(_pathname):
        return SimpleNamespace(size=10)

    async def mock_get(_pathname, *, access):
        return SimpleNamespace(body=b"Cancel me.")

    async def mock_delete(deleted_path):
        deleted.append(deleted_path)

    async def mock_stream(_text):
        yield "Partial summary"
        yield "Never reached"

    monkeypatch.setattr(main.blob, "head_blob", mock_head)
    monkeypatch.setattr(main.blob, "get_blob", mock_get)
    monkeypatch.setattr(main.blob, "delete_blob", mock_delete)
    monkeypatch.setattr(main, "stream_summary", mock_stream)

    async def cancel_after_first_chunk():
        upload = main.BlobSummaryRequest(
            pathname=pathname, filename="cancel.txt", file_size=10
        )
        response = await main.upload_document_stream_from_blob(upload)
        first_event = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return first_event

    with TestClient(app) as client:
        first_event = asyncio.run(cancel_after_first_chunk())
        documents = client.get("/documents").json()

    assert '"type": "delta"' in first_event
    assert deleted == [pathname]
    assert documents == []


def test_upload_txt_file(monkeypatch):
    def mock_summarize_text(text):
        return "This is a test summary."

    monkeypatch.setattr(
        main,
        "summarize_text",
        mock_summarize_text,
    )

    file_content = b"This is a test document."

    with TestClient(app) as client:
        response = client.post(
            "/documents/upload",
            files={
                "file": (
                    "test.txt",
                    file_content,
                    "text/plain",
                )
            },
        )

    assert response.status_code == 200

    data = response.json()

    assert data["filename"] == "test.txt"
    assert data["extracted_text"] == "This is a test document."
    assert data["summary"] == "This is a test summary."
    assert data["document_id"] is not None

def test_upload_unsupported_file():
    file_content = b"Fake image content"

    with TestClient(app) as client:
        response = client.post(
            "/documents/upload",
            files={
                "file": (
                    "test.jpg",
                    file_content,
                    "image/jpeg",
                )
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "Only PDF, DOCX, and TXT files are allowed."
    }
    
def test_upload_file_exceeding_size_limit():
    file_content = b"A" * (10 * 1024 * 1024 + 1)

    with TestClient(app) as client:
        response = client.post(
            "/documents/upload",
            files={
                "file": (
                    "large.txt",
                    file_content,
                    "text/plain",
                )
            },
        )

    assert response.status_code == 413
    assert response.json() == {
        "detail": "File size must not exceed 10 MB."
    }
    
def test_upload_returns_504_when_ai_times_out(monkeypatch):
    def mock_summarize_text(text):
        raise TimeoutError("The AI service took too long to respond.")

    monkeypatch.setattr(
        main,
        "summarize_text",
        mock_summarize_text,
    )

    with TestClient(app) as client:
        response = client.post(
            "/documents/upload",
            files={
                "file": (
                    "test.txt",
                    b"This is a test document.",
                    "text/plain",
                )
            },
        )

    assert response.status_code == 504
    assert response.json() == {
        "detail": "The AI service took too long to respond."
    }

def test_upload_returns_503_when_ai_connection_fails(monkeypatch):
    def mock_summarize_text(text):
        raise ConnectionError("Could not connect to the AI service.")

    monkeypatch.setattr(
        main,
        "summarize_text",
        mock_summarize_text,
    )

    with TestClient(app) as client:
        response = client.post(
            "/documents/upload",
            files={
                "file": (
                    "test.txt",
                    b"This is a test document.",
                    "text/plain",
                )
            },
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Could not connect to the AI service."
    }

def test_upload_returns_429_when_ai_rate_limit_exceeded(monkeypatch):
    def mock_summarize_text(text):
        raise main.AIRateLimitError(
            "The AI service rate limit has been exceeded. Please try again later."
        )

    monkeypatch.setattr(
        main,
        "summarize_text",
        mock_summarize_text,
    )

    with TestClient(app) as client:
        response = client.post(
            "/documents/upload",
            files={
                "file": (
                    "test.txt",
                    b"This is a test document.",
                    "text/plain",
                )
            },
        )

    assert response.status_code == 429
    assert response.json() == {
        "detail": "The AI service rate limit has been exceeded. Please try again later."
    }
    
def test_upload_returns_502_when_ai_api_fails(monkeypatch):
    def mock_summarize_text(text):
        raise main.AIServiceError(
            "The AI service returned an API error: 402"
        )

    monkeypatch.setattr(
        main,
        "summarize_text",
        mock_summarize_text,
    )

    with TestClient(app) as client:
        response = client.post(
            "/documents/upload",
            files={
                "file": (
                    "test.txt",
                    b"This is a test document.",
                    "text/plain",
                )
            },
        )

    assert response.status_code == 502
    assert response.json() == {
        "detail": "The AI service returned an API error: 402"
    }
