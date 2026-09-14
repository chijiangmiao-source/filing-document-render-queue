from __future__ import annotations

import os
import shutil
import threading
import time

import pytest

from app import repository, storage, worker
from app.config import get_settings
from app.db import SessionLocal
from app.models import STATUS_PROCESSING, STATUS_SUCCEEDED

from .conftest import make_docx

pytestmark = pytest.mark.skipif(
    not (
        os.path.exists(get_settings().soffice_bin)
        or shutil.which("soffice") is not None
    ),
    reason="real LibreOffice unavailable",
)


class _SilentRenewer:
    """Renewer that can never extend the lease and never signals 'lost'.

    Models a stalled worker: its lease expires during a long conversion while
    the process is unaware, so it still attempts to publish on completion.
    """

    def __init__(self, *args, **kwargs):
        self.lost = threading.Event()  # never set

    def start(self):
        pass

    def stop(self):
        pass


def _slow_convert(source_path, dest_pdf, settings):
    # Produce a plausible temp result, then run past the (short) lease.
    with open(dest_pdf, "wb") as fh:
        fh.write(b"%PDF-1.5 slow-and-stale")
    time.sleep(2.2)


def test_stale_worker_rejects_its_own_late_publish_then_reconvert_wins(
    clean_db, monkeypatch
):
    settings = get_settings()
    # A lease shorter than the (mocked) conversion.
    monkeypatch.setattr(settings, "lease_seconds", 1.0)
    monkeypatch.setattr(settings, "renew_interval_seconds", 0.2)
    monkeypatch.setattr(worker, "LeaseRenewer", _SilentRenewer)
    monkeypatch.setattr(worker, "convert_docx_to_pdf", _slow_convert)

    db = SessionLocal()
    try:
        jid = storage.new_job_id()
        repository.create_job(db, jid, "pleading.docx", make_docx(paragraphs=5))
        stale_owner = worker.worker_id()
        job = repository.claim_next_job(db, stale_owner, settings)
        assert job.attempts == 1

        tmp = storage.tmp_pdf_path(jid, stale_owner, 1)
        final = storage.final_pdf_path(jid)

        # Runs the slow conversion beyond lease expiry, then must NOT publish.
        worker.process_job(db, job, stale_owner, settings)

        row = repository.get_job(db, jid)
        assert row.status == STATUS_PROCESSING
        assert row.lease_owner == stale_owner  # contender has not arrived yet
        assert row.pdf_path is None
        assert not os.path.exists(final), "late result must not become official"
        assert not os.path.exists(tmp), "late temp result must be discarded"
    finally:
        db.close()

    # A live worker with a fresh, valid lease re-converts from the original.
    monkeypatch.undo()
    live_settings = get_settings()
    db = SessionLocal()
    try:
        live_owner = worker.worker_id()
        job2 = repository.claim_next_job(db, live_owner, live_settings)
        assert job2 is not None and job2.id == jid and job2.attempts == 2
        worker.process_job(db, job2, live_owner, live_settings)

        done = repository.get_job(db, jid)
        assert done.status == STATUS_SUCCEEDED
        assert done.pdf_path == storage.final_pdf_path(jid)
        with open(done.pdf_path, "rb") as fh:
            assert fh.read(5) == b"%PDF-"
        pdfs = [f for f in os.listdir(storage.final_pdf_dir()) if f.startswith(jid)]
        assert pdfs == [f"{jid}.pdf"]
    finally:
        db.close()
