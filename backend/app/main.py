
from contextlib import aclosing, asynccontextmanager

import asyncio
import json
import logging
import os
import re
from functools import partial
from typing import Literal
from uuid import UUID
from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import text
from pydantic import BaseModel, Field
from vercel import blob

from . import auth, ingestion, jobs, limits
from .auth import User, current_user, require_trusted_origin
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
    AIOutOfCreditError,
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
    limits.check_limit_settings()
    ingestion.check_ingestion_settings()
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


@app.exception_handler(limits.DailyLimitReached)
async def daily_limit_reached(request, error: limits.DailyLimitReached):
    return JSONResponse(
        status_code=429,
        content={"detail": str(error), "code": "daily_limit"},
        headers={"Retry-After": str(error.retry_after)},
    )


@app.exception_handler(jobs.JobLimitReached)
async def upload_limit_reached(request, error: jobs.JobLimitReached):
    return JSONResponse(
        status_code=429,
        content={"detail": str(error), "code": "upload_limit"},
        headers={"Retry-After": "5"},
    )


MAX_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}
BLOB_UPLOAD_PREFIX = "documind/uploads/"


def user_upload_prefix(user_id: int) -> str:
    """The Blob folder for one user's temporary uploads (see app/api/document-uploads)."""
    return f"{BLOB_UPLOAD_PREFIX}{user_id}/"


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


QUEUE_UNAVAILABLE_MESSAGE = "The processing queue is unavailable. Please try again later."


async def _start_job(user_id: int, *, mode: str, **fields) -> str:
    """Create the upload's job (this enforces the limit of 3 uploads at the same time), then
    count it against the daily limit. Raises JobLimitReached or DailyLimitReached; a job that
    was refused by the daily limit is removed again."""
    job_id = await run_in_threadpool(partial(jobs.create_job, user_id, mode=mode, **fields))
    try:
        await run_in_threadpool(limits.enforce_daily_limit, user_id, "upload")
    except limits.DailyLimitReached:
        await run_in_threadpool(jobs.delete_job, job_id)
        raise
    return job_id


async def _enqueue_job(job_id: str) -> JSONResponse:
    """Hand the job to the worker and answer 202 with its id. If the queue can't be reached,
    the job fails and the user gets a 503."""
    try:
        await run_in_threadpool(ingestion.enqueue_job, job_id)
    except ingestion.QueueUnavailable:
        await run_in_threadpool(
            jobs.mark_failed, job_id, "queue_unavailable", QUEUE_UNAVAILABLE_MESSAGE
        )
        raise HTTPException(status_code=503, detail=QUEUE_UNAVAILABLE_MESSAGE)
    return JSONResponse(status_code=202, content={"job_id": job_id, "status": "pending"})


@app.get("/")
def root():
    return {"message": "AI Document Summarizer API is running"}


@app.get("/health")
def health_check():
    return {"status": "healthy"}


@app.get("/documents")
def get_documents(user: User = Depends(current_user)):
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
            WHERE owner_user_id = :owner_user_id
            ORDER BY created_at DESC, id DESC
            """),
            {"owner_user_id": user.id},
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


@app.get("/jobs/{job_id}")
def get_job(job_id: UUID, user: User = Depends(current_user)):
    """The state of one of the user's upload jobs; another user's job is "not found"."""
    job = jobs.get_job(str(job_id), owner_user_id=user.id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    failed = job["status"] == "failed"
    return {
        "id": str(job["id"]),
        "status": job["status"],
        "filename": job["filename"],
        "attempts": job["attempts"],
        "document_id": job["document_id"],
        "summary": job["summary"] if job["status"] == "completed" else None,
        "error": {"code": job["error_code"], "message": job["error_message"]} if failed else None,
        "created_at": job["created_at"],
        "finished_at": job["finished_at"],
    }


@app.get("/dashboard")
def get_dashboard(user: User = Depends(current_user)):
    with get_connection() as connection:
        rows = connection.execute(
            text("""SELECT id, filename, file_type, file_size, summary, created_at
                    FROM documents WHERE owner_user_id = :owner_user_id
                    ORDER BY created_at DESC, id DESC"""),
            {"owner_user_id": user.id},
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
    owner_user_id: int,
    job_id: str | None = None,
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
            owner_user_id=owner_user_id,
            job_id=job_id,
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
    except AIOutOfCreditError as error:
        # The service is unavailable to everyone until credit is added: 503, not 502.
        yield _sse({"type": "error", "status": 503, "message": str(error)})
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
        if job_id:
            # A job that ended (the save completed it) is left alone; one that didn't (an
            # error, or the client left) stops counting as a running upload.
            await run_in_threadpool(
                jobs.mark_failed, job_id, "failed", "The upload did not finish."
            )


@app.post("/documents/upload/stream", dependencies=[Depends(require_trusted_origin)])
async def upload_document_stream(
    file: UploadFile = File(...), user: User = Depends(current_user)
):
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
    if ingestion.ingestion_mode() == "queue":
        # The worker reads the file, so a file it can't read fails the job, not this request.
        job_id = await _start_job(
            user.id, mode="queue", filename=filename, file_type=file_extension,
            file_size=len(file_content), file_content=file_content,
        )
        return await _enqueue_job(job_id)
    try:
        # PDF parsing and chunking are CPU-heavy; keep them off the event loop.
        extracted_text, document_chunks = await run_in_threadpool(
            _read_document, file_content, file_extension
        )
    except Exception as error:
        raise HTTPException(status_code=400, detail="Could not read the uploaded file.") from error
    if not extracted_text.strip():
        raise HTTPException(status_code=400, detail="No readable text found in the uploaded file.")
    # Counted before any AI call; an invalid file above doesn't use up the allowance.
    job_id = await _start_job(
        user.id, mode="inline", filename=filename, file_type=file_extension,
        file_size=len(file_content),
    )

    return StreamingResponse(
        _summarize_and_index(
            filename, file_extension, file_content, extracted_text, document_chunks, user.id,
            job_id,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def _queue_blob_upload(
    upload: BlobSummaryRequest, file_extension: str, user: User
) -> JSONResponse:
    """INGESTION_MODE=queue: check the Blob file's size, create the job and enqueue it. The
    worker downloads the file and deletes it when the job ends."""
    try:
        metadata = await blob.head_blob(upload.pathname)
    except blob.BlobNotFoundError as error:
        raise HTTPException(status_code=404, detail="Uploaded file was not found.") from error
    except Exception as error:
        await _delete_temporary_blob(upload.pathname)
        raise HTTPException(status_code=502, detail="Could not retrieve uploaded file.") from error
    if metadata.size > MAX_FILE_SIZE or metadata.size != upload.file_size:
        await _delete_temporary_blob(upload.pathname)
        raise HTTPException(status_code=413, detail="File size must not exceed 10 MB.")
    try:
        job_id = await _start_job(
            user.id, mode="queue", filename=upload.filename, file_type=file_extension,
            file_size=upload.file_size, blob_pathname=upload.pathname,
        )
        return await _enqueue_job(job_id)
    except (limits.DailyLimitReached, jobs.JobLimitReached, HTTPException):
        await _delete_temporary_blob(upload.pathname)
        raise


@app.post("/documents/upload/stream/blob", dependencies=[Depends(require_trusted_origin)])
async def upload_document_stream_from_blob(
    upload: BlobSummaryRequest, user: User = Depends(current_user)
):
    """Read a private Vercel Blob upload, then stream and persist its summary."""
    # Each user's uploads live under documind/uploads/{user_id}/ (the upload-token route only
    # issues such paths). Anything else, including another user's folder, is refused before
    # the blob is read or deleted.
    if not re.fullmatch(
        rf"{re.escape(user_upload_prefix(user.id))}[A-Za-z0-9._-]+\.(pdf|docx|txt)",
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

    if ingestion.ingestion_mode() == "queue":
        return await _queue_blob_upload(upload, file_extension, user)

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
    try:
        job_id = await _start_job(
            user.id, mode="inline", filename=upload.filename, file_type=file_extension,
            file_size=len(file_content),
        )
    except (limits.DailyLimitReached, jobs.JobLimitReached):
        await _delete_temporary_blob(upload.pathname)
        raise

    async def events():
        try:
            # aclosing: if the client disconnects, the inner stream is closed too.
            async with aclosing(
                _summarize_and_index(
                    upload.filename, file_extension, file_content, extracted_text,
                    document_chunks, user.id, job_id,
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
    message: str, history: list[HistoryMessage], document_ids: list[int] | None,
    owner_user_id: int,
):
    """SSE events for one question: `sources`, then `delta`s, then `done` (or `error`)."""
    try:
        prepared = await prepare_answer(
            message, history, document_ids, owner_user_id=owner_user_id
        )
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
    except AIOutOfCreditError as error:
        # The service is unavailable to everyone until credit is added: 503, not 502.
        yield _sse({"type": "error", "status": 503, "message": str(error)})
    except AIServiceError as error:
        yield _sse({"type": "error", "status": 502, "message": str(error)})
    except Exception:
        logger.exception("Answering a chat question failed.")
        yield _sse({"type": "error", "status": 500, "message": "Could not answer the question."})


@app.post("/chat/stream", dependencies=[Depends(require_trusted_origin)])
async def chat_stream(request: ChatRequest, user: User = Depends(current_user)):
    """Answer a question about the user's documents, with numbered sources, as SSE."""
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="The message is empty.")

    document_ids = sorted(set(request.document_ids or [])) or None
    if document_ids:
        found = await run_in_threadpool(
            partial(existing_document_ids, document_ids, owner_user_id=user.id)
        )
        if set(document_ids) - found:
            # Unknown and other users' documents get the same answer, so the response never
            # reveals whether a document exists.
            raise HTTPException(status_code=404, detail="Document not found.")

    await run_in_threadpool(limits.enforce_daily_limit, user.id, "chat")

    history = [HistoryMessage(item.role, item.content) for item in request.history]
    return StreamingResponse(
        _chat_events(message, history, document_ids, user.id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


@app.delete("/documents/{document_id}", dependencies=[Depends(require_trusted_origin)])
def remove_document(document_id: int, user: User = Depends(current_user)):
    # Another user's document is "not found", like a document that doesn't exist.
    deleted = delete_document(document_id, owner_user_id=user.id)

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Document not found.",
        )

    return {
        "message": "Document deleted successfully.",
        "document_id": document_id,
    }
