from __future__ import annotations

import logging
import os
import signal
import threading
import time
import uuid

from sqlalchemy.orm import Session

from . import errors, repository, storage
from .config import Settings, get_settings
from .converter import ConversionError, convert_docx_to_pdf
from .db import SessionLocal, init_db
from .models import STATUS_FAILED

log = logging.getLogger("worker")

STOP = threading.Event()


def _install_signal_handlers() -> None:
    def _handler(signum, _frame):
        log.info("signal %s received, stopping after current iteration", signum)
        STOP.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


class LeaseRenewer:
    """Background thread that extends the lease while conversion runs."""

    def __init__(self, db_factory, job_id: str, owner: str, settings: Settings):
        self._db_factory = db_factory
        self._job_id = job_id
        self._owner = owner
        self._settings = settings
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._thread = threading.Thread(target=self._run, name="lease-renewer", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        interval = max(1.0, self._settings.renew_interval_seconds)
        while not self._stop.wait(interval):
            db: Session = self._db_factory()
            try:
                ok = repository.renew_lease(db, self._job_id, self._owner, self._settings)
                if not ok:
                    log.warning("lease for job %s lost while renewing", self._job_id)
                    self.lost.set()
                    return
            except Exception:  # a transient DB error is retried next tick
                log.exception("lease renewal error for job %s", self._job_id)
            finally:
                db.close()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


def _sweep_orphan_tmp(max_age_seconds: float = 3600.0) -> None:
    """Best-effort removal of tmp artifacts abandoned by killed processes."""
    tmp_dir = os.path.join(os.path.abspath(get_settings().storage_dir), "tmp")
    if not os.path.isdir(tmp_dir):
        return
    cutoff = time.time() - max_age_seconds
    try:
        for name in os.listdir(tmp_dir):
            path = os.path.join(tmp_dir, name)
            try:
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    storage.remove_quiet(path)
            except OSError:
                continue
    except OSError:
        pass


def process_job(db: Session, job, owner: str, settings: Settings) -> None:
    attempt = job.attempts
    # Any temp file from a previous attempt belongs to a lease that has
    # expired (its owner is dead); remove it before we write our own.
    storage.cleanup_job_tmp(job.id)
    tmp_pdf = storage.tmp_pdf_path(job.id, owner, attempt)
    log.info(
        "job %s attempt %d/%d: converting original %s",
        job.id, attempt, settings.max_attempts, job.source_path,
    )

    renewer = LeaseRenewer(SessionLocal, job.id, owner, settings)
    renewer.start()
    try:
        try:
            convert_docx_to_pdf(job.source_path, tmp_pdf, settings)
        except ConversionError as exc:
            if renewer.lost.is_set():
                log.warning("job %s: conversion failed and lease already lost; dropping", job.id)
                storage.remove_quiet(tmp_pdf)
                return
            new_status = repository.record_failure(
                db,
                job.id,
                owner,
                settings,
                errors.CONVERSION_FAILED,
                str(exc),
            )
            storage.remove_quiet(tmp_pdf)
            if new_status == "":
                # Lease expired (or was taken) before we could record: the
                # attempt no longer belongs to us; a live worker will retry.
                log.warning(
                    "job %s: lease expired before recording failure; dropping", job.id
                )
            elif new_status == STATUS_FAILED:
                log.error("job %s terminal failure after %d attempts", job.id, attempt)
            else:
                log.warning("job %s attempt %d failed; requeued: %s", job.id, attempt, exc)
            return

        if renewer.lost.is_set():
            # Another worker owns the lease now; our result must never land.
            log.warning("job %s: late conversion result discarded (lease lost)", job.id)
            storage.remove_quiet(tmp_pdf)
            return

        final_pdf = storage.final_pdf_path(job.id)
        published = repository.publish_success(db, job.id, owner, tmp_pdf, final_pdf)
        if not published:
            # The lease expired (or was taken) between the last renewal and
            # the publish commit. Reject the late result; a live holder that
            # still owns a valid lease converts again and is the sole winner.
            log.warning(
                "job %s: rejected at publish gate (lease expired/taken); "
                "discarding late result",
                job.id,
            )
            storage.remove_quiet(tmp_pdf)
            return
        log.info("job %s succeeded -> %s", job.id, final_pdf)
    finally:
        renewer.stop()


def worker_id() -> str:
    return f"{os.uname().nodename}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def run_forever(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    _install_signal_handlers()
    storage.ensure_layout()
    init_db()
    owner = worker_id()
    log.info("worker %s started (lease=%ss renew=%ss max_attempts=%d)",
             owner, settings.lease_seconds, settings.renew_interval_seconds,
             settings.max_attempts)

    last_sweep = 0.0
    while not STOP.is_set():
        db = SessionLocal()
        try:
            job = repository.claim_next_job(db, owner, settings)
            if job is None:
                if time.time() - last_sweep > 600:
                    _sweep_orphan_tmp()
                    last_sweep = time.time()
                STOP.wait(settings.poll_interval_seconds)
                continue
            # Process within the same session so the job stays bound while the
            # renewal thread uses its own short-lived sessions. The claim is
            # already committed, so this session is idle during conversion.
            try:
                process_job(db, job, owner, settings)
            except Exception:
                log.exception("unexpected error processing job %s", job.id)
        except Exception:
            log.exception("claim/processing loop error")
        finally:
            db.close()

    log.info("worker %s stopped", owner)


if __name__ == "__main__":
    run_forever()
