
import asyncio
import json
import logging
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile as StarletteUploadFile

import app.services.ai_calls as ai_calls
from app.main import app
import app.main as main
from tests.default_user import DEFAULT_USER_EMAIL, DEFAULT_USER_ID
from app.auth import User
import app.services.summarizer as summarizer


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


def _sse_events(body: str) -> list[dict]:
    return [
        json.loads(line[len("data: "):])
        for frame in body.split("\n\n")
        for line in frame.split("\n")
        if line.startswith("data: ")
    ]


@pytest.mark.parametrize(
    ("error", "status", "message"),
    [
        (TimeoutError("The AI service took too long to respond."), 504,
         "The AI service took too long to respond."),
        (ConnectionError("Could not connect to the AI service."), 503,
         "Could not connect to the AI service."),
        (main.AIServiceError("The AI service returned an API error: 400"), 502,
         "The AI service returned an API error: 400"),
        (main.AIOutOfCreditError("The AI service is out of credit. Please try again later."), 503,
         "The AI service is out of credit. Please try again later."),
    ],
    ids=["timeout-504", "connection-503", "provider-error-502", "out-of-credit-503"],
)
def test_stream_upload_maps_ai_errors_without_saving_document(
    monkeypatch, error, status, message
):
    async def mock_stream(text):
        yield "Partial text"
        raise error

    monkeypatch.setattr(main, "stream_summary", mock_stream)
    with TestClient(app) as client:
        response = client.post(
            "/documents/upload/stream",
            files={"file": ("failing.txt", b"Some text.", "text/plain")},
        )
        documents = client.get("/documents").json()

    events = _sse_events(response.text)
    assert response.status_code == 200
    assert events[0] == {"type": "delta", "content": "Partial text"}
    assert events[-1] == {"type": "error", "status": status, "message": message}
    assert not any(event["type"] == "done" for event in events)
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
        response = await main.upload_document_stream(
            upload, user=User(DEFAULT_USER_ID, DEFAULT_USER_EMAIL)
        )
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
                "pathname": "documind/uploads/1/file-random.txt",
                "filename": "blob.txt",
                "file_size": len(b"Blob document content."),
            },
        )
        documents = client.get("/documents").json()

    assert response.status_code == 200
    assert '"type": "done"' in response.text
    assert documents[0]["summary"] == "A blob summary."
    assert deleted == ["documind/uploads/1/file-random.txt"]


def test_failed_temporary_upload_delete_is_logged_and_upload_still_succeeds(monkeypatch, caplog):
    content = b"Blob document content."

    async def mock_head(pathname):
        return SimpleNamespace(size=len(content))

    async def mock_get(pathname, *, access):
        return SimpleNamespace(body=content)

    async def failing_delete(pathname):
        raise RuntimeError("Blob storage is unavailable.")

    async def mock_stream(text):
        yield "A blob summary."

    monkeypatch.setattr(main.blob, "head_blob", mock_head)
    monkeypatch.setattr(main.blob, "get_blob", mock_get)
    monkeypatch.setattr(main.blob, "delete_blob", failing_delete)
    monkeypatch.setattr(main, "stream_summary", mock_stream)
    caplog.set_level(logging.WARNING, logger="app.main")

    with TestClient(app) as client:
        response = client.post(
            "/documents/upload/stream/blob",
            json={
                "pathname": "documind/uploads/1/keep-random.txt",
                "filename": "blob.txt",
                "file_size": len(content),
            },
        )
        documents = client.get("/documents").json()

    assert response.status_code == 200
    assert '"type": "done"' in response.text
    assert documents[0]["summary"] == "A blob summary."
    [record] = [r for r in caplog.records if "Could not delete temporary upload" in r.getMessage()]
    assert "documind/uploads/1/keep-random.txt" in record.getMessage()
    assert "Blob storage is unavailable" in caplog.text  # the error itself is logged too


def test_cancelled_blob_stream_deletes_temporary_upload_without_persisting(monkeypatch):
    pathname = "documind/uploads/1/cancel-random.txt"
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
        response = await main.upload_document_stream_from_blob(
            upload, user=User(DEFAULT_USER_ID, DEFAULT_USER_EMAIL)
        )
        first_event = await anext(response.body_iterator)
        await response.body_iterator.aclose()
        return first_event

    with TestClient(app) as client:
        first_event = asyncio.run(cancel_after_first_chunk())
        documents = client.get("/documents").json()

    assert '"type": "delta"' in first_event
    assert deleted == [pathname]
    assert documents == []


def test_upload_unsupported_file():
    file_content = b"Fake image content"

    with TestClient(app) as client:
        response = client.post(
            "/documents/upload/stream",
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
            "/documents/upload/stream",
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


@pytest.fixture
def missing_api_key(monkeypatch):
    """Simulate a missing key, even if backend/.env provides a real one."""

    def fail_if_client_created(*args, **kwargs):
        raise AssertionError("An AI client must not be created without an API key.")

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(ai_calls, "AsyncOpenAI", fail_if_client_created)
    ai_calls._get_async_client.cache_clear()
    yield
    ai_calls._get_async_client.cache_clear()


def test_stream_upload_returns_503_when_ai_not_configured(missing_api_key, caplog):
    with TestClient(app) as client:
        response = client.post(
            "/documents/upload/stream",
            files={"file": ("test.txt", b"This is a test document.", "text/plain")},
        )
        documents = client.get("/documents").json()

    assert response.status_code == 200
    assert '"type": "error"' in response.text
    assert '"status": 503' in response.text
    assert "AI service is not configured." in response.text
    assert documents == []
    assert "OPENROUTER_API_KEY (or AI_API_KEY) is not set" in caplog.text
