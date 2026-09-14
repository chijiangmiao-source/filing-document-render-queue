from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time

import pytest

from app import repository, storage
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

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _start_worker() -> subprocess.Popen:
    env = os.environ.copy()
    # Short lease for the test; the production/verify contract remains 30/10.
    env["LEASE_SECONDS"] = "3"
    env["RENEW_INTERVAL_SECONDS"] = "1"
    env["POLL_INTERVAL_SECONDS"] = "0.25"
    env["PYTHONPATH"] = REPO_ROOT
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.worker"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # own process group incl. the soffice child
    )
    return proc


def _kill_group(proc: subprocess.Popen) -> None:
    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    proc.wait(timeout=10)


def _wait_status(job_id: str, targets, timeout: float = 60.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        db = SessionLocal()
        try:
            job = repository.get_job(db, job_id)
            last = job.status if job else None
            if job and job.status in targets:
                return job
        finally:
            db.close()
        time.sleep(0.2)
    raise AssertionError(f"job never reached {targets}; last={last}")


def test_killed_worker_mid_conversion_recovers_to_single_pdf(clean_db):
    db = SessionLocal()
    try:
        jid = storage.new_job_id()
        # Heavy document gives a multi-second conversion window wider than
        # the 3s test lease, so the kill reliably lands mid-conversion.
        repository.create_job(db, jid, "pleading.docx", make_docx(paragraphs=20000))
    finally:
        db.close()

    w1 = _start_worker()
    try:
        _wait_status(jid, {STATUS_PROCESSING})
        # Let LibreOffice actually start rendering, then hard-kill the whole
        # group (simulates container SIGKILL / host failure).
        time.sleep(1.0)
        _kill_group(w1)
    finally:
        if w1.poll() is None:
            _kill_group(w1)

    # The 3s lease must outlive the crash: nothing is downloadable yet and no
    # official file exists.
    db = SessionLocal()
    try:
        job = repository.get_job(db, jid)
        assert job.status == STATUS_PROCESSING
        assert job.pdf_path is None
    finally:
        db.close()
    assert not os.path.exists(storage.final_pdf_path(jid))

    # Wait for the lease to expire, then a fresh worker re-runs from original.
    time.sleep(3.2)
    w2 = _start_worker()
    try:
        done = _wait_status(jid, {STATUS_SUCCEEDED}, timeout=90)
        assert done.status == STATUS_SUCCEEDED
        assert done.attempts >= 2
        assert done.pdf_path == storage.final_pdf_path(jid)

        final = storage.final_pdf_path(jid)
        assert os.path.isfile(final)
        with open(final, "rb") as fh:
            assert fh.read(5) == b"%PDF-"

        # Exactly one official artifact for the job.
        pdfs = [f for f in os.listdir(storage.final_pdf_dir())
                if f.startswith(jid)]
        assert pdfs == [f"{jid}.pdf"]
    finally:
        _kill_group(w2)

    # The download endpoint serves the single official file, never tmp.
    from fastapi.testclient import TestClient
    from app.api import app

    client = TestClient(app)
    resp = client.get(f"/jobs/{jid}/download")
    assert resp.status_code == 200
    assert resp.content[:5] == b"%PDF-"
