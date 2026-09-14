from __future__ import annotations

from datetime import datetime

from sqlalchemy import Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcnow() -> datetime:
    # Naive UTC everywhere: SQLite stores DATETIME as text and lease
    # comparisons rely on a single, tz-less canonical representation.
    from datetime import timezone

    return datetime.now(timezone.utc).replace(tzinfo=None)


# Status values (stable strings part of the public API contract).
STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("pdf_path", name="uq_jobs_pdf_path"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(512))
    source_path: Mapped[str] = mapped_column(String(1024))

    status: Mapped[str] = mapped_column(String(16), default=STATUS_PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    # Lease: NULL when no one holds the job (pending or terminal).
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(nullable=True)

    # Only registered after a successful, lease-checked publish.
    pdf_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    # Fingerprint of the official artifact, recorded in the same transaction
    # that registers ``pdf_path``. NULL for rows published before fingerprints
    # existed (and for any non-succeeded job): such historical records keep the
    # original, fingerprint-free download behavior.
    pdf_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pdf_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
