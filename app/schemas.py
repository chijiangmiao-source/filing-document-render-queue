from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class JobCreated(BaseModel):
    id: str
    status: str
    created_at: datetime


class JobStatus(BaseModel):
    id: str
    status: str
    attempts: int
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    download_url: Optional[str] = None
    # Fingerprint of the finished artifact. Populated only for jobs that
    # succeeded after fingerprints were introduced (older succeeded rows and
    # every unfinished job carry neither value); the GET /jobs endpoint omits
    # both keys entirely for such jobs rather than emitting null, so the wire
    # shape is unchanged for pre-fingerprint clients. artifact_sha256 is also
    # the download ETag.
    artifact_size: Optional[int] = None
    artifact_sha256: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
