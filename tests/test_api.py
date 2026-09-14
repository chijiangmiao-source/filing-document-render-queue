from __future__ import annotations

import io
import os
import zipfile

from fastapi.testclient import TestClient

from app import repository, storage
from app.api import app
from app.db import SessionLocal
from app.models import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SUCCEEDED,
)

from .conftest import make_docx

client = TestClient(app)


def _upload(content: bytes, name: str = "doc.docx"):
    return client.post(
        "/jobs",
        files={"file": (name, content,
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document")},
    )


def test_health(clean_db):
    assert client.get("/healthz").status_code == 200


def test_upload_creates_job_and_returns_id(clean_db):
    resp = _upload(make_docx())
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == STATUS_PENDING and body["id"]
    assert client.get(f"/jobs/{body['id']}").json()["status"] == STATUS_PENDING


def test_non_zip_rejected_with_stable_code_and_no_job(clean_db):
    resp = _upload(b"not a zip" + b"\x00" * 40)
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "NOT_A_ZIP"
    assert "code" in resp.json()["error"] and "message" in resp.json()["error"]


def test_zip_missing_parts_rejected(clean_db):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("note.txt", b"hi")
    resp = _upload(buf.getvalue())
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "DOCX_MISSING_PARTS"


def test_oversize_rejected_before_persisting(clean_db):
    pad = b"0" * (10 * 1024 * 1024 + 1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", "<x/>")
        zf.writestr("word/document.xml", pad)
    resp = _upload(buf.getvalue())
    assert resp.status_code == 413
    assert resp.json()["error"]["code"] == "FILE_TOO_LARGE"
    # Nothing persisted: no source file, no job rows.
    assert not os.listdir(storage.final_pdf_dir())
    db = SessionLocal()
    try:
        assert repository.get_job(db, "anything") is None
        from app.models import Job
        assert db.query(Job).count() == 0
    finally:
        db.close()


def test_exactly_ten_mib_is_accepted(clean_db):
    target = 10 * 1024 * 1024

    def build(pad_len: int) -> bytes:
        base = make_docx()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
            with zipfile.ZipFile(io.BytesIO(base)) as zin:
                for item in zin.infolist():
                    zf.writestr(item, zin.read(item.filename))
            if pad_len > 0:
                zf.writestr("pad.bin", b"0" * pad_len)
        return buf.getvalue()

    # With ZIP_STORED, final size is a fixed overhead plus the pad length.
    sample = build(1024)
    overhead = len(sample) - 1024
    pad_len = target - overhead
    assert pad_len > 0
    payload = build(pad_len)
    assert len(payload) == target
    resp = _upload(payload)
    assert resp.status_code == 201

    # One byte over the limit is rejected, regardless of container validity.
    resp = _upload(build(pad_len + 1))
    assert resp.status_code == 413


def test_missing_field_is_no_file(clean_db):
    resp = client.post("/jobs", files={"other": ("a.docx", b"x")})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "NO_FILE"


def test_unknown_job_404_stable_code(clean_db):
    resp = client.get("/jobs/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "JOB_NOT_FOUND"


def test_pending_job_download_is_not_ready(clean_db):
    jid = _upload(make_docx()).json()["id"]
    resp = client.get(f"/jobs/{jid}/download")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "JOB_NOT_READY"
    assert client.get(f"/jobs/{jid}").json()["download_url"] is None


def test_failed_job_reports_reason_and_no_download(clean_db):
    jid = _upload(make_docx()).json()["id"]
    db = SessionLocal()
    try:
        job = repository.get_job(db, jid)
        job.status = STATUS_FAILED
        job.error_code = "CONVERSION_FAILED"
        job.error_message = "source file could not be loaded"
        job.pdf_path = None
        db.commit()
    finally:
        db.close()
    body = client.get(f"/jobs/{jid}").json()
    assert body["status"] == "failed"
    assert body["error_code"] == "CONVERSION_FAILED"
    assert body["error_message"]
    assert body["download_url"] is None
    assert client.get(f"/jobs/{jid}/download").status_code == 409


def test_download_never_serves_tmp_even_if_registered_outside_official_dir(clean_db):
    jid = _upload(make_docx()).json()["id"]
    tmp = storage.tmp_pdf_path(jid, "intruder", 1)
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    with open(tmp, "wb") as fh:
        fh.write(b"%PDF-fake")
    db = SessionLocal()
    try:
        job = repository.get_job(db, jid)
        # Tamper attempt: a row pointing into the tmp directory.
        job.status = STATUS_SUCCEEDED
        job.pdf_path = tmp
        db.commit()
    finally:
        db.close()
    resp = client.get(f"/jobs/{jid}/download")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ARTIFACT_MISSING"


def test_download_serves_only_official_pdf(clean_db):
    jid = _upload(make_docx()).json()["id"]
    final = storage.final_pdf_path(jid)
    os.makedirs(os.path.dirname(final), exist_ok=True)
    with open(final, "wb") as fh:
        fh.write(b"%PDF-1.5\nrealpdf")
    db = SessionLocal()
    try:
        job = repository.get_job(db, jid)
        job.status = STATUS_SUCCEEDED
        job.pdf_path = final
        db.commit()
    finally:
        db.close()
    resp = client.get(f"/jobs/{jid}/download")
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF-")
    assert resp.headers["content-type"] == "application/pdf"
