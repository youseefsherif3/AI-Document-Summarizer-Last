
from io import BytesIO
from contextlib import asynccontextmanager

import json
import os
import re
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from pypdf import PdfReader
from docx import Document
from pydantic import BaseModel, Field
from vercel import blob

from .database import (
    initialize_database,
    save_document,
    update_document_summary,
    get_connection,
    delete_document,
)
from app.services.summarizer import (
    summarize_text,
    AIRateLimitError,
    AIServiceError,
    stream_summary,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    initialize_database()
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

MAX_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_EXTENSIONS = {".pdf", ".docx", ".txt"}
BLOB_UPLOAD_PREFIX = "documind/uploads/"


class BlobSummaryRequest(BaseModel):
    pathname: str = Field(min_length=1, max_length=950)
    filename: str = Field(min_length=1, max_length=255)
    file_size: int = Field(gt=0, le=MAX_FILE_SIZE)


async def _delete_temporary_blob(pathname: str):
    try:
        await blob.delete_blob(pathname)
    except Exception:
        # A temporary upload can be cleaned up later if Blob is unavailable.
        pass


@app.get("/")
def root():
    return {"message": "AI Document Summarizer API is running"}


@app.get("/health")
def health_check():
    return {"status": "healthy"}


def extract_text(file_content: bytes, file_extension: str) -> str:
    if file_extension == ".pdf":
        pdf_reader = PdfReader(BytesIO(file_content))

        return "\n".join(
            page.extract_text() or ""
            for page in pdf_reader.pages
        )

    if file_extension == ".docx":
        document = Document(BytesIO(file_content))

        return "\n".join(
            paragraph.text
            for paragraph in document.paragraphs
        )

    if file_extension == ".txt":
        return file_content.decode("utf-8-sig")

    return ""


@app.post("/documents/upload")
async def upload_document(file: UploadFile = File(...)):
    filename = file.filename or ""

    file_extension = (
        "." + filename.rsplit(".", 1)[-1].lower()
        if "." in filename else ""
    )

    if file_extension not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="Only PDF, DOCX, and TXT files are allowed.",
        )

    file_content = await file.read(MAX_FILE_SIZE + 1)

    if len(file_content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail="File size must not exceed 10 MB.",
        )

    if not file_content:
        raise HTTPException(
            status_code=400,
            detail="The uploaded file is empty.",
        )

    try:
        extracted_text = extract_text(file_content, file_extension)

    except Exception:
        raise HTTPException(
            status_code=400,
            detail="Could not read the uploaded file.",
        )

    if not extracted_text.strip():
        raise HTTPException(
            status_code=400,
            detail="No readable text found in the uploaded file.",
        )

    document_id = save_document(
        filename=filename,
        file_type=file_extension,
        file_size=len(file_content),
        file_content=file_content,
        extracted_text=extracted_text,
    )

    try:
        summary = summarize_text(extracted_text)

        update_document_summary(
            document_id=document_id,
            summary=summary,
        )

    except TimeoutError as error:
        raise HTTPException(status_code=504, detail=str(error))

    except ConnectionError as error:
        raise HTTPException(status_code=503, detail=str(error))

    except AIRateLimitError as error:
        raise HTTPException(status_code=429, detail=str(error))

    except AIServiceError as error:
        raise HTTPException(status_code=502, detail=str(error))

    return {
        "message": "File uploaded and processed successfully.",
        "filename": filename,
        "file_size": len(file_content),
        "file_type": file_extension,
        "extracted_text": extracted_text,
        "summary": summary,
        "document_id": document_id,
    }


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
        extracted_text = extract_text(file_content, file_extension)
    except Exception as error:
        raise HTTPException(status_code=400, detail="Could not read the uploaded file.") from error
    if not extracted_text.strip():
        raise HTTPException(status_code=400, detail="No readable text found in the uploaded file.")

    async def events():
        parts = []
        try:
            async for part in stream_summary(extracted_text):
                parts.append(part)
                yield _sse({"type": "delta", "content": part})
            summary = "".join(parts).strip()
            if not summary:
                raise AIServiceError("The AI model returned an empty summary.")
            document_id = save_document(
                filename=filename,
                file_type=file_extension,
                file_size=len(file_content),
                file_content=file_content,
                extracted_text=extracted_text,
                summary=summary,
            )
            yield _sse({"type": "done", "document_id": document_id, "summary": summary})
        except TimeoutError as error:
            yield _sse({"type": "error", "status": 504, "message": str(error)})
        except ConnectionError as error:
            yield _sse({"type": "error", "status": 503, "message": str(error)})
        except AIRateLimitError as error:
            yield _sse({"type": "error", "status": 429, "message": str(error)})
        except AIServiceError as error:
            yield _sse({"type": "error", "status": 502, "message": str(error)})
        except Exception:
            yield _sse({"type": "error", "status": 500, "message": "Summary generation failed."})

    return StreamingResponse(
        events(),
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
        extracted_text = extract_text(file_content, file_extension)
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
        parts = []
        try:
            async for part in stream_summary(extracted_text):
                parts.append(part)
                yield _sse({"type": "delta", "content": part})

            summary = "".join(parts).strip()
            if not summary:
                raise AIServiceError("The AI model returned an empty summary.")

            document_id = save_document(
                filename=upload.filename,
                file_type=file_extension,
                file_size=len(file_content),
                file_content=file_content,
                extracted_text=extracted_text,
                summary=summary,
            )
            yield _sse({"type": "done", "document_id": document_id, "summary": summary})
        except TimeoutError as error:
            yield _sse({"type": "error", "status": 504, "message": str(error)})
        except ConnectionError as error:
            yield _sse({"type": "error", "status": 503, "message": str(error)})
        except AIRateLimitError as error:
            yield _sse({"type": "error", "status": 429, "message": str(error)})
        except AIServiceError as error:
            yield _sse({"type": "error", "status": 502, "message": str(error)})
        except Exception:
            yield _sse({"type": "error", "status": 500, "message": "Summary generation failed."})
        finally:
            await _delete_temporary_blob(upload.pathname)

    return StreamingResponse(
        events(),
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
