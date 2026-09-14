from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import storage
from .config import Settings
from .models import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_SUCCEEDED,
    Job,
    utcnow,
)


def now_utc() -> datetime:
    """Naive UTC, matching SQLAlchemy's SQLite DATETIME storage format."""
    return utcnow()


def create_job(db: Session, job_id: str, original_filename: str, data: bytes) -> Job:
    storage.ensure_layout()
    path = storage.source_path(job_id)
    storage.atomic_write_bytes(path, data)
    job = Job(
        id=job_id,
        original_filename=original_filename,
        source_path=path,
        status=STATUS_PENDING,
        attempts=0,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def get_job(db: Session, job_id: str) -> Optional[Job]:
    return db.get(Job, job_id)


def _reap_exhausted(db: Session, now: datetime, max_attempts: int) -> None:
    """Expired leases that already used every attempt become a terminal failure."""
    db.execute(
        text(
            """
            UPDATE jobs
               SET status = :failed,
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   error_code = 'CONVERSION_FAILED',
                   error_message = :msg,
                   updated_at = :now
             WHERE status = :processing
               AND lease_expires_at < :now
               AND attempts >= :max
            """
        ),
        {
            "failed": STATUS_FAILED,
            "processing": STATUS_PROCESSING,
            "msg": f"worker lost after {max_attempts} attempts",
            "now": now,
            "max": max_attempts,
        },
    )


def claim_next_job(
    db: Session, owner: str, settings: Settings
) -> Optional[Job]:
    """Atomically claim one job without row locks.

    Eligible jobs are either never-started pending jobs, or processing jobs
    whose lease has expired while fewer than ``max_attempts`` attempts have
    been made. The single conditional UPDATE is the compare-and-set: under
    SQLite WAL + busy_timeout exactly one contender wins each row.
    """
    now = now_utc()
    expires = now + timedelta(seconds=settings.lease_seconds)

    _reap_exhausted(db, now, settings.max_attempts)

    row = db.execute(
        text(
            """
            UPDATE jobs
               SET status = :processing,
                   lease_owner = :owner,
                   lease_expires_at = :expires,
                   attempts = attempts + 1,
                   updated_at = :now
             WHERE id = (
                     SELECT id
                       FROM jobs
                      WHERE status = :pending
                         OR (status = :processing
                             AND lease_expires_at < :now
                             AND attempts < :max)
                      ORDER BY created_at, id
                      LIMIT 1
                 )
         RETURNING id
            """
        ),
        {
            "processing": STATUS_PROCESSING,
            "pending": STATUS_PENDING,
            "owner": owner,
            "expires": expires,
            "now": now,
            "max": settings.max_attempts,
        },
    ).first()
    db.commit()
    if row is None:
        return None
    return db.get(Job, row[0])


def renew_lease(db: Session, job_id: str, owner: str, settings: Settings) -> bool:
    """Extend the lease.

    Renewal is granted only while the lease is still *valid*. Once
    ``lease_expires_at`` has passed, the original owner has lost the job even
    if no one has overwritten ``lease_owner`` yet; it must not be able to
    extend a dead lease.
    """
    now = now_utc()
    expires = now + timedelta(seconds=settings.lease_seconds)
    result = db.execute(
        text(
            """
            UPDATE jobs
               SET lease_expires_at = :expires, updated_at = :now
             WHERE id = :id
               AND lease_owner = :owner
               AND status = :processing
               AND lease_expires_at > :now
            """
        ),
        {
            "expires": expires,
            "now": now,
            "id": job_id,
            "owner": owner,
            "processing": STATUS_PROCESSING,
        },
    )
    db.commit()
    return result.rowcount == 1


def publish_success(
    db: Session,
    job_id: str,
    owner: str,
    tmp_pdf: str,
    final_pdf: str,
    pdf_size: int,
    pdf_sha256: str,
) -> bool:
    """Register the official artifact as the *current, non-expired* holder.

    The gate has two tests:

    * ``lease_owner = :owner`` — this worker still owns the job; and
    * ``lease_expires_at > :now`` — the lease has not actually expired.

    The second test is essential: there is a window after expiry and before
    another worker overwrites ``lease_owner`` during which the owner field
    still names the original process. Without the time fence that stale
    process could still publish its late PDF. With it, publication is refused
    the instant the lease lapses; the row stays ``processing`` with the old
    owner until a live worker claims it and converts again from the source.
    The file is moved onto the single deterministic official path only after
    this gate succeeds, inside the same transaction that persists the
    artifact fingerprint (byte size + SHA-256 of ``tmp_pdf``), so the
    registered path and its fingerprint can never diverge.
    """
    now = now_utc()
    try:
        result = db.execute(
            text(
                """
                UPDATE jobs
                   SET status = :succeeded,
                       pdf_path = :pdf_path,
                       pdf_size = :pdf_size,
                       pdf_sha256 = :pdf_sha256,
                       lease_owner = NULL,
                       lease_expires_at = NULL,
                       error_code = NULL,
                       error_message = NULL,
                       updated_at = :now
                 WHERE id = :id
                   AND lease_owner = :owner
                   AND status = :processing
                   AND lease_expires_at > :now
                   AND pdf_path IS NULL
                """
            ),
            {
                "succeeded": STATUS_SUCCEEDED,
                "processing": STATUS_PROCESSING,
                "pdf_path": final_pdf,
                "pdf_size": pdf_size,
                "pdf_sha256": pdf_sha256,
                "now": now,
                "id": job_id,
                "owner": owner,
            },
        )
        if result.rowcount != 1:
            db.rollback()
            return False
        # Gate held: materialize the unique official path, then commit.
        moved = False
        try:
            os.replace(tmp_pdf, final_pdf)
            moved = True
            db.commit()
        except Exception:
            db.rollback()
            # Undo the move so we never leave an uncommitted official file.
            if moved and os.path.exists(final_pdf):
                try:
                    os.replace(final_pdf, tmp_pdf)
                except OSError:
                    pass
            raise
        return True
    except Exception:
        db.rollback()
        raise


def record_failure(
    db: Session,
    job_id: str,
    owner: str,
    settings: Settings,
    error_code: str,
    error_message: str,
) -> str:
    """Record a conversion attempt failure; returns the resulting status.

    Only the *current, non-expired* lease holder may record. The last allowed
    attempt lands the job in the terminal ``failed`` state; earlier attempts
    release the job back to ``pending`` so another worker can retry from the
    original. If the lease has expired the call returns "" and the caller
    discards its attempt artifacts (a live worker will take over).
    """
    now = now_utc()
    job = db.get(Job, job_id)
    if (
        job is None
        or job.lease_owner != owner
        or job.status != STATUS_PROCESSING
        or job.lease_expires_at is None
        or job.lease_expires_at <= now
    ):
        db.rollback()
        return ""  # lost/expired lease; caller discards its attempt artifacts

    terminal = job.attempts >= settings.max_attempts
    new_status = STATUS_FAILED if terminal else STATUS_PENDING
    db.execute(
        text(
            """
            UPDATE jobs
               SET status = :status,
                   lease_owner = NULL,
                   lease_expires_at = NULL,
                   error_code = :code,
                   error_message = :message,
                   updated_at = :now
             WHERE id = :id
               AND lease_owner = :owner
               AND status = :processing
               AND lease_expires_at > :now
            """
        ),
        {
            "status": new_status,
            "processing": STATUS_PROCESSING,
            "code": error_code,
            "message": error_message[:4000],
            "now": now,
            "id": job_id,
            "owner": owner,
        },
    )
    db.commit()
    return new_status
