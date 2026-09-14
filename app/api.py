from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, File, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from sqlalchemy.orm import Session

from . import errors, repository, storage
from .config import get_settings
from .db import get_session, init_db
from .exceptions import APIError
from .models import STATUS_SUCCEEDED, Job
from .schemas import JobCreated, JobStatus
from .validation import validate_docx

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    storage.ensure_layout()
    init_db()
    yield


app = FastAPI(title="docx2pdf", version="1.0.0", lifespan=lifespan)


@app.exception_handler(APIError)
async def _api_error_handler(_request: Request, exc: APIError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "code": errors.VALIDATION_ERROR,
                "message": "request validation failed",
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = {404: errors.JOB_NOT_FOUND, 405: errors.VALIDATION_ERROR}.get(
        exc.status_code, errors.INTERNAL_ERROR
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": code, "message": str(exc.detail)}},
    )


@app.exception_handler(Exception)
async def _unexpected_handler(_request: Request, exc: Exception) -> JSONResponse:
    logging.getLogger("api").exception("unhandled error: %s", exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": errors.INTERNAL_ERROR,
                "message": "internal server error",
            }
        },
    )


def _job_to_status(job: Job) -> JobStatus:
    download_url = f"/jobs/{job.id}/download" if job.status == STATUS_SUCCEEDED else None
    return JobStatus(
        id=job.id,
        status=job.status,
        attempts=job.attempts,
        error_code=job.error_code,
        error_message=job.error_message,
        download_url=download_url,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _read_capped(upload: UploadFile, limit: int) -> bytes:
    """Read at most ``limit`` bytes; raise FILE_TOO_LARGE if more are sent."""
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = upload.file.read(1024 * 256)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise APIError(
                errors.FILE_TOO_LARGE,
                f"uploaded file exceeds maximum of {limit} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@app.post("/jobs", response_model=JobCreated, status_code=201)
def create_job(
    file: UploadFile | None = File(default=None),
    db: Session = Depends(get_session),
) -> JobCreated:
    if file is None or not file.filename:
        raise APIError(errors.NO_FILE, "multipart field 'file' is required")

    data = _read_capped(file, settings.max_upload_bytes)
    if not data:
        raise APIError(errors.NO_FILE, "uploaded file is empty")

    # Container validation happens BEFORE any task is persisted.
    try:
        validate_docx(data)
    except ValueError as exc:
        code = str(exc)
        message = {
            errors.NOT_A_ZIP: "file is not a valid ZIP container",
            errors.DOCX_MISSING_PARTS: (
                "DOCX container must contain [Content_Types].xml "
                "and word/document.xml"
            ),
        }.get(code, "invalid DOCX container")
        raise APIError(code, message)

    job_id = storage.new_job_id()
    filename = storage.safe_filename(file.filename)
    job = repository.create_job(db, job_id, filename, data)
    return JobCreated(id=job.id, status=job.status, created_at=job.created_at)


def _load_job_or_404(db: Session, job_id: str) -> Job:
    job = repository.get_job(db, job_id)
    if job is None:
        raise APIError(errors.JOB_NOT_FOUND, "job not found")
    return job


@app.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str, db: Session = Depends(get_session)) -> JobStatus:
    return _job_to_status(_load_job_or_404(db, job_id))


@app.get("/jobs/{job_id}/download")
def download_job(job_id: str, db: Session = Depends(get_session)) -> Response:
    job = _load_job_or_404(db, job_id)
    if job.status != STATUS_SUCCEEDED or not job.pdf_path:
        # Failed / in-flight jobs expose a queryable reason but never a URL.
        raise APIError(
            errors.JOB_NOT_READY,
            f"job is {job.status}, no PDF available",
        )

    # Defense in depth: only ever serve the single registered official path
    # that lives in the official directory — never a tmp/ artifact.
    official_dir = os.path.realpath(storage.final_pdf_dir())
    pdf_path = os.path.realpath(job.pdf_path)
    if (
        os.path.dirname(pdf_path) != official_dir
        or os.path.basename(pdf_path) != f"{job.id}.pdf"
    ):
        raise APIError(errors.ARTIFACT_MISSING, "registered artifact is unavailable")
    if not os.path.isfile(pdf_path):
        raise APIError(errors.ARTIFACT_MISSING, "registered artifact is unavailable")

    with open(pdf_path, "rb") as fh:
        content = fh.read()
    if not content.startswith(b"%PDF-"):
        # A registered file that lost its magic is corruption, not a download.
        raise APIError(errors.ARTIFACT_MISSING, "registered artifact is unavailable")

    download_name = f"{os.path.splitext(job.original_filename)[0] or job.id}.pdf"
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{download_name}"',
            "X-Job-Id": job.id,
        },
    )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
