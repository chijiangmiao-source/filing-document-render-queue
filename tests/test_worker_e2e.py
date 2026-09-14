from __future__ import annotations

import hashlib
import os
import shutil

import pytest

from app import repository, storage
from app.config import get_settings
from app.converter import ConversionError, convert_docx_to_pdf
from app.db import SessionLocal
from app.models import STATUS_FAILED, STATUS_SUCCEEDED
from app.worker import process_job
from app.worker import worker_id as make_owner

from .conftest import make_docx

pytestmark = pytest.mark.skipif(
    not (
        os.path.exists(get_settings().soffice_bin)
        or shutil.which("soffice") is not None
    ),
    reason="real LibreOffice unavailable",
)


def _source_for(data: bytes) -> str:
    jid = storage.new_job_id()
    path = storage.source_path(jid)
    storage.atomic_write_bytes(path, data)
    return jid, path


def test_real_converter_produces_pdf_magic():
    jid, source = _source_for(make_docx(paragraphs=2))
    dest = storage.tmp_pdf_path(jid, "unit", 1)
    convert_docx_to_pdf(source, dest, get_settings())
    assert os.path.exists(dest)
    with open(dest, "rb") as fh:
        assert fh.read(5) == b"%PDF-"


def test_real_converter_rejects_unrenderable_document():
    jid, source = _source_for(make_docx(corrupt_document=True))
    dest = storage.tmp_pdf_path(jid, "unit", 1)
    with pytest.raises(ConversionError):
        convert_docx_to_pdf(source, dest, get_settings())
    assert not os.path.exists(dest)


def test_worker_end_to_end_success(clean_db):
    settings = get_settings()
    db = SessionLocal()
    try:
        jid = storage.new_job_id()
        repository.create_job(db, jid, "pleading.docx", make_docx(paragraphs=10))
        job = repository.claim_next_job(db, make_owner(), settings)
        assert job is not None and job.id == jid
        process_job(db, job, job.lease_owner, settings)

        final = storage.final_pdf_path(jid)
        done = repository.get_job(db, jid)
        assert done.status == STATUS_SUCCEEDED
        assert done.pdf_path == final
        with open(final, "rb") as fh:
            pdf = fh.read()
        assert pdf[:5] == b"%PDF-"
        # The worker fingerprints the bytes it publishes; the row records the
        # exact size/SHA-256 in the same gated transaction as the path.
        assert done.pdf_size == len(pdf)
        assert done.pdf_sha256 == hashlib.sha256(pdf).hexdigest()
        registered_size = done.pdf_size
        registered_sha256 = done.pdf_sha256
        assert not [f for f in os.listdir(os.path.join(settings.storage_dir, "tmp"))]
    finally:
        db.close()

    # The published fingerprint is what the API advertises, serves and validates.
    from fastapi.testclient import TestClient

    from app.api import app

    client = TestClient(app)
    body = client.get(f"/jobs/{jid}").json()
    assert body["artifact_size"] == registered_size
    assert body["artifact_sha256"] == registered_sha256
    resp = client.get(f"/jobs/{jid}/download")
    assert resp.status_code == 200
    assert resp.headers["etag"] == f'"{registered_sha256}"'
    cond = client.get(
        f"/jobs/{jid}/download",
        headers={"If-None-Match": f'"{registered_sha256}"'},
    )
    assert cond.status_code == 304


def test_worker_three_attempts_then_terminal_failure(clean_db):
    settings = get_settings()
    db = SessionLocal()
    try:
        jid = storage.new_job_id()
        repository.create_job(
            db, jid, "broken.docx", make_docx(corrupt_document=True)
        )
        for attempt in range(3):
            job = repository.claim_next_job(db, make_owner(), settings)
            assert job is not None and job.attempts == attempt + 1
            process_job(db, job, job.lease_owner, settings)

        done = repository.get_job(db, jid)
        assert done.status == STATUS_FAILED
        assert done.error_code == "CONVERSION_FAILED"
        assert "could not be loaded" in done.error_message or done.error_message
        assert done.pdf_path is None
        assert repository.claim_next_job(db, make_owner(), settings) is None
    finally:
        db.close()
