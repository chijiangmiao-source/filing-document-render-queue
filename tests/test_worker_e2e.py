from __future__ import annotations

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
            assert fh.read(5) == b"%PDF-"
        assert not [f for f in os.listdir(os.path.join(settings.storage_dir, "tmp"))]
    finally:
        db.close()


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
