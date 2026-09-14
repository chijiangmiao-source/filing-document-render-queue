from __future__ import annotations

import hashlib
import os

from fastapi.testclient import TestClient

from app import repository, storage
from app.api import app
from app.config import get_settings
from app.db import SessionLocal
from app.models import STATUS_FAILED, STATUS_PENDING, STATUS_SUCCEEDED

from .conftest import make_docx

client = TestClient(app)

PDF_BYTES = b"%PDF-1.5\nfingerprinted official artifact body\n"


def _publish_fingerprinted_job(filename: str = "pleading.docx") -> tuple[str, bytes]:
    """Run a job through claim + gated publish with a real fingerprint."""
    db = SessionLocal()
    try:
        jid = storage.new_job_id()
        repository.create_job(db, jid, filename, make_docx())
        job = repository.claim_next_job(db, "owner-fp", get_settings())
        tmp = storage.tmp_pdf_path(jid, "owner-fp", job.attempts)
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "wb") as fh:
            fh.write(PDF_BYTES)
        size, digest = storage.file_fingerprint(tmp)
        final = storage.final_pdf_path(jid)
        assert repository.publish_success(
            db, jid, "owner-fp", tmp, final, size, digest
        )
        return jid, PDF_BYTES
    finally:
        db.close()


def _legacy_succeeded_job(pdf_bytes: bytes = PDF_BYTES) -> str:
    """A succeeded row as an old build would have left it: no fingerprint."""
    db = SessionLocal()
    try:
        jid = storage.new_job_id()
        repository.create_job(db, jid, "legacy.docx", make_docx())
        final = storage.final_pdf_path(jid)
        os.makedirs(os.path.dirname(final), exist_ok=True)
        with open(final, "wb") as fh:
            fh.write(pdf_bytes)
        job = repository.get_job(db, jid)
        job.status = STATUS_SUCCEEDED
        job.pdf_path = final
        job.pdf_size = None
        job.pdf_sha256 = None
        db.commit()
        return jid
    finally:
        db.close()


def test_query_carries_fingerprint_consistent_with_download(clean_db):
    jid, pdf = _publish_fingerprinted_job()
    body = client.get(f"/jobs/{jid}").json()
    assert body["status"] == STATUS_SUCCEEDED
    assert body["artifact_size"] == len(pdf)
    assert body["artifact_sha256"] == hashlib.sha256(pdf).hexdigest()
    assert body["download_url"] == f"/jobs/{jid}/download"

    resp = client.get(f"/jobs/{jid}/download")
    assert resp.status_code == 200
    assert resp.content == pdf
    # Size and digest advertised by the query match the delivered bytes.
    assert len(resp.content) == body["artifact_size"]
    assert hashlib.sha256(resp.content).hexdigest() == body["artifact_sha256"]
    # The digest is used verbatim as the download ETag.
    assert resp.headers["etag"] == f'"{body["artifact_sha256"]}"'


def test_conditional_download_returns_304_and_then_full_body(clean_db):
    jid, pdf = _publish_fingerprinted_job()
    digest = hashlib.sha256(pdf).hexdigest()

    matched = client.get(
        f"/jobs/{jid}/download", headers={"If-None-Match": f'"{digest}"'}
    )
    assert matched.status_code == 304
    assert matched.content == b""
    assert matched.headers["etag"] == f'"{digest}"'
    # A 304 must still identify the attachment but carry no body.
    assert "X-Job-Id" in matched.headers

    # Star/wildcard validators and comma-listed etags also hit.
    star = client.get(f"/jobs/{jid}/download", headers={"If-None-Match": "*"})
    assert star.status_code == 304
    listed = client.get(
        f"/jobs/{jid}/download",
        headers={"If-None-Match": f'"deadbeef", "{digest}"'},
    )
    assert listed.status_code == 304

    # A non-matching validator fetches the bytes normally.
    miss = client.get(
        f"/jobs/{jid}/download",
        headers={"If-None-Match": f'"{"f" * 64}"'},
    )
    assert miss.status_code == 200 and miss.content == pdf


def test_tampered_artifact_is_refused_and_status_is_not_rewritten(clean_db):
    jid, pdf = _publish_fingerprinted_job()
    final = storage.final_pdf_path(jid)

    # Shared storage is modified out of band: append bytes (digest AND size
    # now disagree with the registered fingerprint), magic preserved.
    with open(final, "ab") as fh:
        fh.write(b"\nTAMPERED")

    resp = client.get(f"/jobs/{jid}/download")
    assert resp.status_code == 500
    err = resp.json()["error"]
    assert err["code"] == "ARTIFACT_CORRUPTED"
    assert err["message"]

    # The task row is untouched: still succeeded with its original fingerprint,
    # so the failure describes storage corruption rather than a state change.
    body = client.get(f"/jobs/{jid}").json()
    assert body["status"] == STATUS_SUCCEEDED
    assert body["artifact_size"] == len(pdf)
    assert body["artifact_sha256"] == hashlib.sha256(pdf).hexdigest()
    assert body["download_url"] == f"/jobs/{jid}/download"

    db = SessionLocal()
    try:
        row = repository.get_job(db, jid)
        assert row.status == STATUS_SUCCEEDED
        assert row.pdf_size == len(pdf)
        assert row.pdf_sha256 == hashlib.sha256(pdf).hexdigest()
        assert row.error_code is None and row.error_message is None
    finally:
        db.close()

    # A conditional request against a corrupted file must not 304 either.
    cond = client.get(
        f"/jobs/{jid}/download",
        headers={"If-None-Match": f'"{hashlib.sha256(pdf).hexdigest()}"'},
    )
    assert cond.status_code == 500
    assert cond.json()["error"]["code"] == "ARTIFACT_CORRUPTED"


def test_size_only_tamper_is_detected(clean_db):
    jid, pdf = _publish_fingerprinted_job()
    final = storage.final_pdf_path(jid)
    # Replace with same-length garbage that preserves the header: digest
    # mismatch must independently trigger the guard.
    same_len = PDF_BYTES[:5] + b"X" * (len(PDF_BYTES) - 5)
    assert len(same_len) == len(PDF_BYTES)
    with open(final, "wb") as fh:
        fh.write(same_len)
    resp = client.get(f"/jobs/{jid}/download")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "ARTIFACT_CORRUPTED"


def test_legacy_succeeded_record_keeps_original_download_behavior(clean_db):
    jid = _legacy_succeeded_job()
    body = client.get(f"/jobs/{jid}").json()
    # Old successful records expose no fingerprint fields-populated values.
    assert body["status"] == STATUS_SUCCEEDED
    assert body["artifact_size"] is None
    assert body["artifact_sha256"] is None
    assert body["download_url"] == f"/jobs/{jid}/download"

    resp = client.get(f"/jobs/{jid}/download")
    assert resp.status_code == 200
    assert resp.content == PDF_BYTES
    # No ETag is emitted for fingerprint-free historical records...
    assert "etag" not in {k.lower() for k in resp.headers.keys()}
    # ...so a conditional request cannot produce a 304.
    cond = client.get(
        f"/jobs/{jid}/download",
        headers={"If-None-Match": f'"{hashlib.sha256(PDF_BYTES).hexdigest()}"'},
    )
    assert cond.status_code == 200 and cond.content == PDF_BYTES


def test_legacy_record_with_destroyed_magic_still_reports_artifact_missing(clean_db):
    # Legacy path: without a fingerprint, the original magic check governs.
    jid = _legacy_succeeded_job(pdf_bytes=b"not a pdf anymore" + b"\x00" * 20)
    resp = client.get(f"/jobs/{jid}/download")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ARTIFACT_MISSING"


def test_unfinished_jobs_never_report_fingerprint(clean_db):
    # Pending.
    resp = client.post(
        "/jobs",
        files={"file": ("d.docx", make_docx(),
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document")},
    )
    jid = resp.json()["id"]
    body = client.get(f"/jobs/{jid}").json()
    assert body["status"] == STATUS_PENDING
    assert body["artifact_size"] is None
    assert body["artifact_sha256"] is None
    assert body["download_url"] is None

    # Failed.
    db = SessionLocal()
    try:
        job = repository.get_job(db, jid)
        job.status = STATUS_FAILED
        job.error_code = "CONVERSION_FAILED"
        job.error_message = "boom"
        db.commit()
    finally:
        db.close()
    body = client.get(f"/jobs/{jid}").json()
    assert body["status"] == STATUS_FAILED
    assert body["artifact_size"] is None
    assert body["artifact_sha256"] is None
    assert body["download_url"] is None
    assert client.get(f"/jobs/{jid}/download").status_code == 409
