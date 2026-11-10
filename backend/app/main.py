
from contextlib import aclosing, asynccontextmanager

import asyncio
import json
import logging
import os
import re
from typing import Literal
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from pydantic import BaseModel, Field
from vercel import blob

from . import auth
from .extraction import extract_pages, join_pages
from .database import (
    check_database,
    save_document_with_chunks,
    get_connection,
    delete_document,
    existing_document_ids,
)
from app.services.chat import HistoryMessage, prepare_answer, stream_answer
from app.services.chunking import Chunk, chunk_pages
from app.services.embeddings import embed_texts
from app.services.summarizer import (
    AIRateLimitError,
    AIServiceError,
    AIServiceNotConfiguredError,
    stream_summary,
)

logger = logging.getLogger(__name__)

# Print this app's INFO logs (AI usage lines, errors) next to uvicorn's own logs.
_app_logger = logging.getLogger("app")
if not _app_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s %(message)s"))
    _app_logger.addHandler(_handler)
_app_logger.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Read-only: the schema comes from Alembic migrations, never from startup code.
    check_database()
    yield


app = FastAPI(
    title="AI Document Summarizer API",
    description="API for summarizing documents using AI",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS",
            "http://localhost:3000,http://127.0.0.1:3000,http://localhost:3001,http://127.0.0.1:3001",
        ).split(",")
        if origin.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Re-sends the session cookie when a visit renewed the session (also on streamed responses).
app.add_middleware(auth.SessionCookieMiddleware)
app.include_router(auth.router)

MAX_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}
BLOB_UPLOAD_PREFIX = "documind/uploads/"


class BlobSummaryRequest(BaseModel):
    pathname: str = Field(min_length=1, max_length=950)
    filename: str = Field(min_length=1, max_length=255)
    file_size: int = Field(gt=0, le=MAX_FILE_SIZE)


class ChatHistoryItem(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=20_000)


class ChatRequest(BaseModel):
    message: str = Field(max_length=4_000)
    # Earlier turns of the conversation; only the last few are sent to the model.
    history: list[ChatHistoryItem] = Field(default_factory=list, max_length=50)
    # None or [] = search all documents; otherwise only these.
    document_ids: list[int] | None = Field(default=None, max_length=200)


async def _delete_temporary_blob(pathname: str):
    try:
        await blob.delete_blob(pathname)
    except Exception:
        # The upload itself is not affected; the temporary file stays in Blob storage
        # until it is cleaned up later. Logged so leftover files can be found.
        logger.warning("Could not delete temporary upload %r from Blob storage.", pathname,
                       exc_info=True)


@app.get("/")
def root():
    return {"message": "AI Document Summarizer API is running"}


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.get("/documents")
def get_documents():
    with get_connection() as connection:
        rows = connection.execute(
            text("""
            SELECT
                id,
                filename,
                file_type,
                file_size,
                summary,
                created_at
            FROM documents
            ORDER BY created_at DESC, id DESC
            """)
        ).mappings().all()

    return [
        {
            "id": row["id"],
            "filename": row["filename"],
            "file_type": row["file_type"].lstrip("."),
            "file_size": row["file_size"],
            "summary": row["summary"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


@app.get("/dashboard")
def get_dashboard():
    with get_connection() as connection:
        rows = connection.execute(
            text("""SELECT id, filename, file_type, file_size, summary, created_at
                    FROM documents ORDER BY created_at DESC, id DESC""")
        ).mappings().all()

    types = {}
    for row in rows:
        file_type = row["file_type"].lstrip(".").lower()
        types[file_type] = types.get(file_type, 0) + 1
    completed = sum(1 for row in rows if row["summary"])
    failed = sum(1 for row in rows if not row["summary"])
    return {
        "total_documents": len(rows),
        "completed_documents": completed,
        "processing_documents": 0,
        "failed_documents": failed,
        "file_types": types,
        "recent_documents": [
            {
                "id": row["id"],
                "filename": row["filename"],
                "file_type": row["file_type"].lstrip("."),
                "file_size": row["file_size"],
                "summary": row["summary"],
                "created_at": row["created_at"],
            }
            for row in rows[:5]
        ],
    }


def _sse(event):
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _read_document(file_content: bytes, file_extension: str) -> tuple[str, list[Chunk]]:
    """The whole text (for the summary) and its chunks (for search). CPU-heavy: run in a threadpool."""
    pages = extract_pages(file_content, file_extension)
    return join_pages(pages, file_extension), chunk_pages(pages)


def _failed(task: asyncio.Task) -> bool:
    return task.done() and not task.cancelled() and task.exception() is not None


async def _summarize_and_index(
    filename: str,
    file_extension: str,
    file_content: bytes,
    extracted_text: str,
    document_chunks: list[Chunk],
):
    """SSE events for one upload: summary deltas, then `done` or `error`.

    The chunks are embedded in parallel with the summary stream. The document and its
    chunks are saved together, in one transaction, only if both succeed.
    """
    embedding_task = asyncio.create_task(
        embed_texts([chunk.text for chunk in document_chunks])
    )
    summary_stream = stream_summary(extracted_text)
    parts = []
    try:
        async for part in summary_stream:
            parts.append(part)
            yield _sse({"type": "delta", "content": part})
            if _failed(embedding_task):
                break  # indexing already failed: stop paying for the summary
        if _failed(embedding_task):
            await embedding_task  # raises the embedding error

        summary = "".join(parts).strip()
        if not summary:
            raise AIServiceError("The AI model returned an empty summary.")

        embeddings = await embedding_task
        document_id = await run_in_threadpool(
            save_document_with_chunks,
            filename=filename,
            file_type=file_extension,
            file_size=len(file_content),
            file_content=file_content,
            extracted_text=extracted_text,
            summary=summary,
            document_chunks=document_chunks,
            embeddings=embeddings,
        )
        yield _sse({"type": "done", "document_id": document_id, "summary": summary})
    except TimeoutError as error:
        yield _sse({"type": "error", "status": 504, "message": str(error)})
    except ConnectionError as error:
        yield _sse({"type": "error", "status": 503, "message": str(error)})
    except AIServiceNotConfiguredError as error:
        yield _sse({"type": "error", "status": 503, "message": str(error)})
    except AIRateLimitError as error:
        yield _sse({"type": "error", "status": 429, "message": str(error)})
    except AIServiceError as error:
        yield _sse({"type": "error", "status": 502, "message": str(error)})
    except Exception:
        logger.exception("Processing upload %r failed.", filename)
        yield _sse({"type": "error", "status": 500, "message": "Could not process the document."})
    finally:
        # Runs on success, on errors, and when the client disconnects mid-stream.
        await summary_stream.aclose()
        if not embedding_task.done():
            embedding_task.cancel()
        # Wait for the task to finish and collect its result or error, so nothing is left running.
        await asyncio.gather(embedding_task, return_exceptions=True)


@app.post("/documents/upload/stream")
async def upload_document_stream(file: UploadFile = File(...)):
    filename = file.filename or ""
    file_extension = (
        "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    )
    if file_extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail="Only PDF, DOCX, and TXT files are allowed.")
    file_content = await file.read(MAX_FILE_SIZE + 1)
    if len(file_content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File size must not exceed 10 MB.")
    if not file_content:
        raise HTTPException(status_code=400, detail="The uploaded file is empty.")
    try:
        # PDF parsing and chunking are CPU-heavy; keep them off the event loop.
        extracted_text, document_chunks = await run_in_threadpool(
            _read_document, file_content, file_extension
        )
    except Exception as error:
        raise HTTPException(status_code=400, detail="Could not read the uploaded file.") from error
    if not extracted_text.strip():
        raise HTTPException(status_code=400, detail="No readable text found in the uploaded file.")

    return StreamingResponse(
        _summarize_and_index(
            filename, file_extension, file_content, extracted_text, document_chunks
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.post("/documents/upload/stream/blob")
async def upload_document_stream_from_blob(upload: BlobSummaryRequest):
    """Read a private Vercel Blob upload, then stream and persist its summary."""
    if not re.fullmatch(
        rf"{re.escape(BLOB_UPLOAD_PREFIX)}[A-Za-z0-9._-]+\.(pdf|docx|txt)",
        upload.pathname,
        flags=re.IGNORECASE,
    ):
        raise HTTPException(status_code=400, detail="Invalid upload reference.")

    file_extension = (
        "." + upload.filename.rsplit(".", 1)[-1].lower()
        if "." in upload.filename else ""
    )
    if file_extension not in ALLOWED_EXTENSIONS:
        await _delete_temporary_blob(upload.pathname)
        raise HTTPException(
            status_code=400,
            detail="Only PDF, DOCX, and TXT files are allowed.",
        )
    if not upload.pathname.lower().endswith(file_extension):
        await _delete_temporary_blob(upload.pathname)
        raise HTTPException(status_code=400, detail="Upload reference does not match the file type.")

    try:
        metadata = await blob.head_blob(upload.pathname)
        if metadata.size > MAX_FILE_SIZE or metadata.size != upload.file_size:
            await _delete_temporary_blob(upload.pathname)
            raise HTTPException(status_code=413, detail="File size must not exceed 10 MB.")

        downloaded = await blob.get_blob(upload.pathname, access="private")
        file_content = downloaded.body
    except blob.BlobNotFoundError as error:
        raise HTTPException(status_code=404, detail="Uploaded file was not found.") from error
    except HTTPException:
        raise
    except Exception as error:
        await _delete_temporary_blob(upload.pathname)
        raise HTTPException(status_code=502, detail="Could not retrieve uploaded file.") from error

    if len(file_content) != upload.file_size or not file_content:
        await _delete_temporary_blob(upload.pathname)
        raise HTTPException(status_code=400, detail="Uploaded file is empty or incomplete.")

    try:
        # PDF parsing and chunking are CPU-heavy; keep them off the event loop.
        extracted_text, document_chunks = await run_in_threadpool(
            _read_document, file_content, file_extension
        )
    except Exception as error:
        await _delete_temporary_blob(upload.pathname)
        raise HTTPException(status_code=400, detail="Could not read the uploaded file.") from error

    if not extracted_text.strip():
        await _delete_temporary_blob(upload.pathname)
        raise HTTPException(
            status_code=400,
            detail="No readable text found in the uploaded file.",
        )

    async def events():
        try:
            # aclosing: if the client disconnects, the inner stream is closed too.
            async with aclosing(
                _summarize_and_index(
                    upload.filename, file_extension, file_content, extracted_text, document_chunks
                )
            ) as stream:
                async for event in stream:
                    yield event
        finally:
            await _delete_temporary_blob(upload.pathname)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def _chat_events(
    message: str, history: list[HistoryMessage], document_ids: list[int] | None
):
    """SSE events for one question: `sources`, then `delta`s, then `done` (or `error`)."""
    try:
        prepared = await prepare_answer(message, history, document_ids)
        yield _sse(
            {
                "type": "sources",
                "sources": [
                    {
                        "n": source.n,
                        "document_id": source.document_id,
                        "filename": source.filename,
                        "page": source.page,
                        "chunk_id": source.chunk_id,
                        "snippet": source.snippet,
                        "similarity": round(source.similarity, 4),
                    }
                    for source in prepared.sources
                ],
            }
        )
        # aclosing: if the client disconnects, the model stream is closed too.
        async with aclosing(stream_answer(prepared)) as answer:
            async for part in answer:
                yield _sse({"type": "delta", "content": part})
        yield _sse({"type": "done", "usage": prepared.usage})
    except TimeoutError as error:
        yield _sse({"type": "error", "status": 504, "message": str(error)})
    except ConnectionError as error:
        yield _sse({"type": "error", "status": 503, "message": str(error)})
    except AIServiceNotConfiguredError as error:
        yield _sse({"type": "error", "status": 503, "message": str(error)})
    except AIRateLimitError as error:
        yield _sse({"type": "error", "status": 429, "message": str(error)})
    except AIServiceError as error:
        yield _sse({"type": "error", "status": 502, "message": str(error)})
    except Exception:
        logger.exception("Answering a chat question failed.")
        yield _sse({"type": "error", "status": 500, "message": "Could not answer the question."})


@app.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Answer a question about the stored documents, with numbered sources, as SSE."""
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="The message is empty.")

    document_ids = sorted(set(request.document_ids or [])) or None
    if document_ids:
        found = await run_in_threadpool(existing_document_ids, document_ids)
        unknown = [document_id for document_id in document_ids if document_id not in found]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown document IDs: {', '.join(map(str, unknown))}.",
            )

    history = [HistoryMessage(item.role, item.content) for item in request.history]
    return StreamingResponse(
        _chat_events(message, history, document_ids),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.delete("/documents/{document_id}")
def remove_document(document_id: int):
    deleted = delete_document(document_id)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Document not found.",
        )

    return {
        "message": "Document deleted successfully.",
        "document_id": document_id,
    }
