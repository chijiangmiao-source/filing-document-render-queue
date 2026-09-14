from __future__ import annotations

import os
import threading
from datetime import timedelta

from app import repository, storage
from app.config import get_settings
from app.db import SessionLocal
from app.models import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PROCESSING,
    STATUS_SUCCEEDED,
    utcnow,
)

from .conftest import make_docx


def _make_job():
    db = SessionLocal()
    try:
        jid = storage.new_job_id()
        job = repository.create_job(db, jid, "a.docx", make_docx())
        return job.id
    finally:
        db.close()


def test_claim_pending_sets_lease_and_attempt(clean_db):
    jid = _make_job()
    s = get_settings()
    db = SessionLocal()
    try:
        job = repository.claim_next_job(db, "owner-A", s)
        assert job is not None and job.id == jid
        assert job.status == STATUS_PROCESSING
        assert job.lease_owner == "owner-A"
        assert job.attempts == 1
        assert job.lease_expires_at is not None
        # A second immediate claim finds nothing (lease is valid).
        assert repository.claim_next_job(db, "owner-B", s) is None
    finally:
        db.close()


def test_concurrent_claimers_only_one_wins(clean_db):
    for _ in range(5):
        _make_job()
    s = get_settings()
    winners: list[str] = []
    lock = threading.Lock()

    def claim(owner):
        db = SessionLocal()
        try:
            job = repository.claim_next_job(db, owner, s)
            if job:
                with lock:
                    winners.append((job.id, owner))
        finally:
            db.close()

    threads = [threading.Thread(target=claim, args=(f"w{i}",)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    job_ids = [j for j, _ in winners]
    assert len(job_ids) == 5
    assert len(set(job_ids)) == 5  # no job claimed twice


def test_expired_lease_is_reclaimable_and_increments_attempt(clean_db):
    jid = _make_job()
    s = get_settings()
    db = SessionLocal()
    try:
        first = repository.claim_next_job(db, "owner-A", s)
        assert first.attempts == 1
        # Simulate owner-A dying: force the lease into the past.
        first.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

        second = repository.claim_next_job(db, "owner-B", s)
        assert second is not None and second.id == jid
        assert second.lease_owner == "owner-B"
        assert second.attempts == 2
    finally:
        db.close()


def test_renew_requires_ownership(clean_db):
    jid = _make_job()
    s = get_settings()
    db = SessionLocal()
    try:
        repository.claim_next_job(db, "owner-A", s)
        assert repository.renew_lease(db, jid, "owner-A", s) is True
        assert repository.renew_lease(db, jid, "owner-impostor", s) is False
    finally:
        db.close()


def test_publish_gate_rejects_stale_owner(clean_db):
    jid = _make_job()
    s = get_settings()
    db = SessionLocal()
    try:
        job = repository.claim_next_job(db, "owner-A", s)
        tmp = storage.tmp_pdf_path(jid, "owner-A", job.attempts)
        final = storage.final_pdf_path(jid)
        with open(tmp, "wb") as fh:
            fh.write(b"%PDF-1.5 late")

        # owner-A loses the lease (e.g. crash + takeover by owner-B).
        job.lease_owner = "owner-B"
        db.commit()

        assert repository.publish_success(db, jid, "owner-A", tmp, final) is False
        # Late result must be discarded by caller; nothing moved/registered.
        assert not os.path.exists(final)
        again = db.get(type(job), jid)
        assert again.status == STATUS_PROCESSING and again.pdf_path is None
    finally:
        db.close()


def test_expired_lease_still_named_me_fences_every_write(clean_db):
    # The critical fencing case: the lease has expired, but no contender has
    # overwritten lease_owner yet. The stale original process must still be
    # refused renew / publish / record-failure.
    jid = _make_job()
    s = get_settings()
    db = SessionLocal()
    try:
        job = repository.claim_next_job(db, "owner-A", s)
        assert job.attempts == 1
        tmp = storage.tmp_pdf_path(jid, "owner-A", job.attempts)
        final = storage.final_pdf_path(jid)
        with open(tmp, "wb") as fh:
            fh.write(b"%PDF-1.5 late")

        # Expire the lease WITHOUT changing owner (contender not arrived yet).
        job.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

        assert repository.renew_lease(db, jid, "owner-A", s) is False
        assert repository.publish_success(db, jid, "owner-A", tmp, final) is False
        assert (
            repository.record_failure(db, jid, "owner-A", s,
                                      "CONVERSION_FAILED", "late") == ""
        )

        # Nothing landed: no official file, row still processing with old owner.
        assert not os.path.exists(final)
        row = repository.get_job(db, jid)
        assert row.status == STATUS_PROCESSING
        assert row.lease_owner == "owner-A"
        assert row.pdf_path is None
    finally:
        db.close()


def test_late_publish_rejected_then_fresh_claim_converts_and_wins(clean_db):
    jid = _make_job()
    s = get_settings()
    db = SessionLocal()
    try:
        first = repository.claim_next_job(db, "slow", s)
        late_tmp = storage.tmp_pdf_path(jid, "slow", first.attempts)
        with open(late_tmp, "wb") as fh:
            fh.write(b"%PDF-1.5 slow-late")
        # slow's lease expires; it tries (and fails) to publish.
        first.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()
        final = storage.final_pdf_path(jid)
        assert repository.publish_success(db, jid, "slow", late_tmp, final) is False

        # A live worker re-claims from the expired lease and re-converts.
        second = repository.claim_next_job(db, "fast", s)
        assert second.lease_owner == "fast" and second.attempts == 2
        fresh_tmp = storage.tmp_pdf_path(jid, "fast", second.attempts)
        with open(fresh_tmp, "wb") as fh:
            fh.write(b"%PDF-1.5 fresh")
        assert repository.publish_success(db, jid, "fast", fresh_tmp, final) is True

        done = repository.get_job(db, jid)
        assert done.status == STATUS_SUCCEEDED and done.pdf_path == final
        with open(final, "rb") as fh:
            assert fh.read() == b"%PDF-1.5 fresh"  # late result never overwrote
    finally:
        db.close()


def test_publish_success_by_current_owner(clean_db):
    jid = _make_job()
    s = get_settings()
    db = SessionLocal()
    try:
        job = repository.claim_next_job(db, "owner-A", s)
        tmp = storage.tmp_pdf_path(jid, "owner-A", job.attempts)
        final = storage.final_pdf_path(jid)
        with open(tmp, "wb") as fh:
            fh.write(b"%PDF-1.5 real")
        assert repository.publish_success(db, jid, "owner-A", tmp, final) is True
        assert os.path.exists(final) and not os.path.exists(tmp)
        again = db.get(type(job), jid)
        assert again.status == STATUS_SUCCEEDED
        assert again.pdf_path == final
        assert again.lease_owner is None
    finally:
        db.close()


def test_three_failures_reach_terminal_then_not_reclaimed(clean_db):
    jid = _make_job()
    s = get_settings()
    owners = ["w1", "w2", "w3"]
    db = SessionLocal()
    try:
        for i, owner in enumerate(owners):
            job = repository.claim_next_job(db, owner, s)
            assert job is not None and job.attempts == i + 1
            status = repository.record_failure(
                db, jid, owner, s, "CONVERSION_FAILED", "boom"
            )
            if i < 2:
                assert status == STATUS_PENDING
            else:
                assert status == STATUS_FAILED
        # Terminal job is never claimed again.
        assert repository.claim_next_job(db, "w4", s) is None
        job = repository.get_job(db, jid)
        assert job.status == STATUS_FAILED
        assert job.error_code == "CONVERSION_FAILED"
        assert job.pdf_path is None
    finally:
        db.close()


def test_dead_worker_after_third_claim_is_reaped_terminal(clean_db):
    # Two failures requeue; the third claim crashes without recording. When the
    # lease expires the job must be moved to terminal failed automatically.
    jid = _make_job()
    s = get_settings()
    db = SessionLocal()
    try:
        for i, owner in enumerate(["w1", "w2"]):
            repository.claim_next_job(db, owner, s)
            assert (
                repository.record_failure(db, jid, owner, s, "CONVERSION_FAILED", "x")
                == STATUS_PENDING
            )
        third = repository.claim_next_job(db, "w3", s)
        assert third.attempts == 3
        # w3 dies: expire its lease.
        third.lease_expires_at = utcnow() - timedelta(seconds=1)
        db.commit()

        # A claiming worker runs the reap path; the exhausted job goes terminal.
        assert repository.claim_next_job(db, "w4", s) is None
        job = repository.get_job(db, jid)
        assert job.status == STATUS_FAILED
        assert job.attempts == 3
    finally:
        db.close()
