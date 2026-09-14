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
    # every non-terminal job report null); clients that predate the fields
    # simply ignore them. artifact_sha256 is also the download ETag.
    artifact_size: Optional[int] = None
    artifact_sha256: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
