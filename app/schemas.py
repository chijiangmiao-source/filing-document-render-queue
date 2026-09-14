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
    created_at: datetime
    updated_at: datetime


class ErrorBody(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorBody
