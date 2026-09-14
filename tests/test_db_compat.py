from __future__ import annotations

import os
import tempfile

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app.db import _add_pdf_fingerprint_columns
from app.models import Job

# Exact table shape shipped by builds before artifact fingerprints existed.
_OLD_SCHEMA = """
CREATE TABLE jobs (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    original_filename VARCHAR(512) NOT NULL,
    source_path VARCHAR(1024) NOT NULL,
    status VARCHAR(16) NOT NULL,
    attempts INTEGER NOT NULL,
    lease_owner VARCHAR(64),
    lease_expires_at DATETIME,
    pdf_path VARCHAR(1024),
    error_code VARCHAR(64),
    error_message VARCHAR(4096),
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL,
    CONSTRAINT uq_jobs_pdf_path UNIQUE (pdf_path)
)
"""


def test_migration_adds_nullable_fingerprint_columns_to_legacy_db():
    tmp_dir = tempfile.mkdtemp(prefix="docx2pdf-legacy-")
    db_path = os.path.join(tmp_dir, "legacy.db")
    legacy_engine = create_engine(f"sqlite:///{db_path}", future=True)
    with legacy_engine.begin() as conn:
        conn.execute(text(_OLD_SCHEMA))
        conn.execute(
            text(
                "INSERT INTO jobs (id, original_filename, source_path, status, "
                "attempts, pdf_path, created_at, updated_at) VALUES "
                "('jid-legacy', 'old.docx', '/data/source/old.docx', 'succeeded', "
                "1, '/data/storage/pdf/jid-legacy.pdf', '2026-01-01 00:00:00', "
                "'2026-01-01 00:00:05')"
            )
        )

    # Before migration the new columns are absent.
    cols = {c["name"] for c in inspect(legacy_engine).get_columns("jobs")}
    assert "pdf_size" not in cols and "pdf_sha256" not in cols

    # The startup migration is additive and idempotent.
    _add_pdf_fingerprint_columns(legacy_engine)
    _add_pdf_fingerprint_columns(legacy_engine)

    cols = {c["name"] for c in inspect(legacy_engine).get_columns("jobs")}
    assert {"pdf_size", "pdf_sha256"} <= cols

    with Session(legacy_engine) as session:
        row = session.get(Job, "jid-legacy")
        # Historical succeeded records are read normally and carry no fingerprint.
        assert row is not None
        assert row.status == "succeeded"
        assert row.pdf_path == "/data/storage/pdf/jid-legacy.pdf"
        assert row.pdf_size is None and row.pdf_sha256 is None

        # New writes persist the fingerprint on the migrated schema.
        row.pdf_size = 4242
        row.pdf_sha256 = "a" * 64
        session.commit()

    with legacy_engine.begin() as conn:
        stored = conn.execute(
            text("SELECT pdf_size, pdf_sha256 FROM jobs WHERE id = 'jid-legacy'")
        ).one()
        assert stored == (4242, "a" * 64)
