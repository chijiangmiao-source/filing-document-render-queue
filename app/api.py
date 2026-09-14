from __future__ import annotations

import hashlib
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
    succeeded = job.status == STATUS_SUCCEEDED
    download_url = f"/jobs/{job.id}/download" if succeeded else None
    # Fingerprints are reported only for successful jobs that were published
    # with one (new records). Historical rows and unfinished jobs stay null,
    # so older clients polling the same fields are unaffected.
    artifact_size = job.pdf_size if (succeeded and job.pdf_sha256) else None
    artifact_sha256 = job.pdf_sha256 if succeeded else None
    return JobStatus(
        id=job.id,
        status=job.status,
        attempts=job.attempts,
        error_code=job.error_code,
        error_message=job.error_message,
        download_url=download_url,
        artifact_size=artifact_size,
        artifact_sha256=artifact_sha256,
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
def download_job(
    job_id: str, request: Request, db: Session = Depends(get_session)
) -> Response:
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

    # When the job carries a fingerprint (every job published after the
    # fingerprint feature shipped), the bytes on the shared store must still
    # match exactly. A mismatch means the official artifact was modified after
    # publication: refuse delivery rather than serve a corrupted PDF. This is
    # checked before anything else (including the %PDF- magic) so any tamper —
    # even one that also destroys the header — reports the same stable
    # ARTIFACT_CORRUPTED code. The job row is intentionally left untouched:
    # corruption is a storage event, not a task state transition. Historical
    # rows without a fingerprint keep the original, unverified behavior.
    fingerprinted = bool(job.pdf_sha256)
    if fingerprinted:
        actual_size = len(content)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if actual_size != job.pdf_size or actual_sha256 != job.pdf_sha256:
            raise APIError(
                errors.ARTIFACT_CORRUPTED,
                "stored artifact is corrupted: size or checksum "
                "does not match the registered fingerprint",
            )

    if not content.startswith(b"%PDF-"):
        # Only reachable for legacy, fingerprint-free records: a registered
        # file that lost its magic is unavailable rather than downloadable.
        raise APIError(errors.ARTIFACT_MISSING, "registered artifact is unavailable")

    download_name = f"{os.path.splitext(job.original_filename)[0] or job.id}.pdf"
    common_headers = {
        "Content-Disposition": f'attachment; filename="{download_name}"',
        "X-Job-Id": job.id,
    }

    # The registered digest is the strong validator for the artifact. A
    # matching If-None-Match answers 304 without re-sending the bytes; legacy
    # fingerprint-free records simply carry no ETag and always return 200.
    if fingerprinted:
        etag = f'"{job.pdf_sha256}"'
        common_headers["ETag"] = etag
        if _etag_matches(request.headers.get("if-none-match"), job.pdf_sha256):
            return Response(status_code=304, headers=common_headers)

    return Response(
        content=content,
        media_type="application/pdf",
        headers=common_headers,
    )


def _etag_matches(header_value: str | None, expected_sha256: str) -> bool:
    """Match an If-None-Match header against the artifact digest.

    Accepts the exact quoted strong validator and ``*``; tolerates a list of
    comma-separated etags and surrounding whitespace. Weak (``W/``) validators
    are not produced by this service and are ignored.
    """
    if not header_value:
        return False
    target = f'"{expected_sha256}"'
    for token in header_value.split(","):
        token = token.strip()
        if token == "*" or token == target:
            return True
    return False


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
