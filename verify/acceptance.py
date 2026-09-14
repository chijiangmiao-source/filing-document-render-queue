from __future__ import annotations

"""One-shot acceptance verifier.

Proves the end-to-end contract against a running docker compose stack:

  1. A valid heavy DOCX is picked up by the worker; while it is converting,
     the worker container is SIGKILLed and then started again. After the 30s
     lease expires another attempt converts from the ORIGINAL docx and the job
     ends with exactly one downloadable PDF beginning with %PDF-. That new job
     reports size + SHA-256 matching the downloaded bytes; the digest is the
     download ETag and a matching If-None-Match returns 304.
  2. Tampering with the official PDF on the shared store makes the download
     fail with a stable ARTIFACT_CORRUPTED envelope (never a corrupt body,
     never a 304) without rewriting the task status; the run then continues.
  3. A document that passes container validation but cannot render ends in a
     terminal failure: reason is queryable and there is no download URL.
  4. Invalid containers never create a task, with stable error codes
     (NOT_A_ZIP / DOCX_MISSING_PARTS / FILE_TOO_LARGE / JOB_NOT_FOUND).
  5. Historical succeeded records without a fingerprint keep the original
     query/download behavior: the fingerprint keys are absent, no ETag is
     emitted and no conditional request returns 304.

Run (compose):  docker compose --profile verify run --rm verify
"""

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone

from . import fixtures

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000").rstrip("/")
STORAGE_DIR = os.getenv("STORAGE_DIR", "/data/storage")
DB_PATH = os.getenv("DB_PATH", "/data/docx2pdf.db")
WORKER_LABEL = os.getenv("WORKER_SERVICE", "worker")

# Timing is derived from the lease contract (30s) plus real conversion time.
LEASE_SECONDS = float(os.getenv("LEASE_SECONDS", "30"))
DEADLINE_SECONDS = 180.0


class CheckFailed(AssertionError):
    pass


def check(cond: bool, msg: str) -> None:
    if not cond:
        raise CheckFailed(msg)
    print(f"  PASS: {msg}")


# ---------------------------------------------------------------- HTTP client

def _request(method: str, path: str, data: bytes | None = None,
             headers: dict | None = None) -> tuple[int, dict, bytes]:
    req = urllib.request.Request(
        API_BASE + path, data=data, method=method, headers=headers or {}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def _multipart(filename: str, content: bytes,
               field: str = "file") -> tuple[bytes, str]:
    boundary = "----verify" + uuid.uuid4().hex
    crlf = b"\r\n"
    body = io.BytesIO()
    body.write(f"--{boundary}".encode() + crlf)
    body.write(
        f'Content-Disposition: form-data; name="{field}"; '
        f'filename="{filename}"'.encode() + crlf
    )
    body.write(b"Content-Type: application/vnd.openxmlformats-officedocument."
               b"wordprocessingml.document" + crlf + crlf)
    body.write(content + crlf)
    body.write(f"--{boundary}--".encode() + crlf)
    return body.getvalue(), boundary


def upload(content: bytes, filename: str = "doc.docx"):
    body, boundary = _multipart(filename, content)
    return _request(
        "POST", "/jobs", body,
        {"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )


def get(path: str):
    return _request("GET", path)


def _header(headers: dict, name: str) -> str | None:
    """Case-insensitive response/header lookup."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def wait_health() -> None:
    for _ in range(60):
        try:
            status, _, _ = get("/healthz")
            if status == 200:
                return
        except OSError:
            pass
        time.sleep(1)
    raise CheckFailed("API never became healthy")


def wait_status(job_id: str, targets, deadline: float) -> dict:
    end = time.time() + deadline
    last = {}
    while time.time() < end:
        status, _, raw = get(f"/jobs/{job_id}")
        check(status == 200, f"job {job_id} is queryable")
        last = json.loads(raw)
        if last["status"] in targets:
            return last
        time.sleep(0.25)
    raise CheckFailed(
        f"job {job_id} never reached {targets}; last={last}"
    )


# --------------------------------------------------------------- docker control

def _docker(*args: str) -> str:
    proc = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=30
    )
    if proc.returncode != 0:
        raise CheckFailed(
            f"docker {' '.join(args)} failed: {proc.stderr.strip()}"
        )
    return proc.stdout


def worker_container_id() -> str:
    def find(project: str | None) -> list[str]:
        args = [
            "ps", "-q",
            "--filter", f"label=com.docker.compose.service={WORKER_LABEL}",
            "--filter", "status=running",
        ]
        if project:
            args[4:4] = ["--filter",
                         f"label=com.docker.compose.project={project}"]
        return [ln for ln in _docker(*args).split() if ln]

    ids = find(os.getenv("COMPOSE_PROJECT_NAME"))
    if not ids:  # tolerate a -p project-name override
        ids = find(None)
    check(len(ids) >= 1, "a running worker container exists")
    return ids[0]


def kill_worker_mid_conversion(job_id: str) -> str:
    """Return the killed worker container id once the job is processing."""
    # Wait for the worker to claim the job, then kill immediately, while
    # LibreOffice is still rendering the heavy document.
    wait_status(job_id, {"processing"}, DEADLINE_SECONDS)
    cid = worker_container_id()
    print(f"  ... killing worker {cid[:12]} mid-conversion")
    _docker("kill", cid)
    return cid


def restart_worker(cid: str) -> None:
    # Explicit start; the service restart policy may already have brought it
    # back, in which case start is a harmless no-op.
    _docker("start", cid)
    print(f"  ... started worker {cid[:12]} again")


# --------------------------------------------------------------------- checks

def check_crash_recovery() -> None:
    print("\n[1] kill worker mid-conversion, expect one %PDF- artifact")
    doc = fixtures.heavy_docx()
    status, _, raw = upload(doc, "pleading.docx")
    check(status == 201, f"heavy doc accepted (HTTP 201), got {status}")
    job = json.loads(raw)
    job_id = job["id"]

    cid = kill_worker_mid_conversion(job_id)
    restart_worker(cid)

    # Lease (30s) must expire before anyone else may touch the job. Right after
    # the crash the partial result must not be downloadable.
    time.sleep(2)
    st, _, _ = get(f"/jobs/{job_id}/download")
    check(st in (404, 409),
          f"no download while lease unexpired/in-flight (got {st})")

    final = wait_status(job_id, {"succeeded", "failed"}, DEADLINE_SECONDS)
    check(final["status"] == "succeeded",
          f"job succeeded after recovery, got {final['status']}")
    check(final["attempts"] >= 2,
          f"recovery required a fresh attempt (attempts={final['attempts']})")
    check(final["download_url"] == f"/jobs/{job_id}/download",
          "exactly one canonical download URL is exposed")
    check(not final.get("error_code"), "no error on recovered job")

    st, headers, pdf = get(final["download_url"])
    check(st == 200, "download returns 200")
    check(pdf[:5] == b"%PDF-", "downloaded artifact starts with %PDF-")
    check(len(pdf) > 1000, f"artifact is a real rendered PDF ({len(pdf)} bytes)")

    # The query response advertises a fingerprint that matches the bytes
    # actually delivered (new jobs are published with size + SHA-256).
    want_size = len(pdf)
    want_digest = hashlib.sha256(pdf).hexdigest()
    check(final.get("artifact_size") == want_size,
          f"query artifact_size matches download ({want_size} bytes)")
    check(final.get("artifact_sha256") == want_digest,
          "query artifact_sha256 matches the downloaded PDF digest")
    check(_header(headers, "etag") == f'"{want_digest}"',
          "download ETag is the registered SHA-256")

    # Conditional download: a matching If-None-Match yields 304 with no body
    # but keeps the validator; a wrong validator re-serves the bytes.
    st304, h304, body304 = _request(
        "GET", final["download_url"],
        headers={"If-None-Match": f'"{want_digest}"'},
    )
    check(st304 == 304 and body304 == b"",
          f"matching If-None-Match returns 304 with empty body (got {st304})")
    check(_header(h304, "etag") == f'"{want_digest}"',
          "304 response carries the same ETag")
    st200, _, pdf2 = _request(
        "GET", final["download_url"],
        headers={"If-None-Match": f'"{"f" * 64}"'},
    )
    check(st200 == 200 and pdf2 == pdf,
          "non-matching validator re-downloads the identical PDF")

    # Exactly one official artifact on disk; no tmp file is ever served.
    pdf_dir = os.path.join(STORAGE_DIR, "pdf")
    on_disk = sorted(f for f in os.listdir(pdf_dir) if f.startswith(job_id))
    check(on_disk == [f"{job_id}.pdf"],
          f"exactly one official PDF on disk: {on_disk}")
    tmp_dir = os.path.join(STORAGE_DIR, "tmp")
    leftovers = [f for f in os.listdir(tmp_dir) if f.startswith(job_id)]
    check(not leftovers, f"no lingering tmp artifacts: {leftovers}")

    return job_id, want_digest


def check_tampered_artifact_rejected(job_id: str, digest: str) -> None:
    print("\n[2] tamper the official PDF on shared storage -> ARTIFACT_CORRUPTED")
    pdf_path = os.path.join(STORAGE_DIR, "pdf", f"{job_id}.pdf")
    check(os.path.isfile(pdf_path), f"official artifact exists: {pdf_path}")
    with open(pdf_path, "rb") as fh:
        original = fh.read()
    try:
        # Out-of-band modification of the shared store (preserve %PDF- magic so
        # the test isolates the fingerprint guard, not the legacy magic check).
        with open(pdf_path, "ab") as fh:
            fh.write(b"\n%% shared storage was modified out of band\n")

        st, _, raw = get(f"/jobs/{job_id}/download")
        body = json.loads(raw)
        check(st == 500, f"tampered artifact is not delivered (got {st})")
        check(body["error"]["code"] == "ARTIFACT_CORRUPTED",
              f"stable code ARTIFACT_CORRUPTED, got {body['error'].get('code')}")

        # A conditional request must not be tricked into 304 either.
        st304, _, _ = _request(
            "GET", f"/jobs/{job_id}/download",
            headers={"If-None-Match": f'"{digest}"'},
        )
        check(st304 == 500,
              f"tampered artifact never answers 304 (got {st304})")

        # The task status must not be rewritten by the storage event.
        status, _, sraw = get(f"/jobs/{job_id}")
        state = json.loads(sraw)
        check(status == 200 and state["status"] == "succeeded",
              "job remains succeeded after corruption is detected")
        check(state.get("artifact_sha256") == digest,
              "registered fingerprint is unchanged in the query response")
        check(state["download_url"] == f"/jobs/{job_id}/download",
              "download URL is still advertised")

        conn = _db()
        try:
            row = conn.execute(
                "SELECT status, pdf_size, pdf_sha256, error_code FROM jobs "
                "WHERE id = ?",
                (job_id,),
            ).fetchone()
            check(row is not None, "job row still exists after tamper detection")
            check(row[0] == "succeeded" and row[2] == digest,
                  f"job row is untouched (status={row[0]})")
            check(row[3] is None, "no error code is written onto the job")
        finally:
            conn.close()
    finally:
        # Restore so later manual inspection/downloads behave normally.
        with open(pdf_path, "wb") as fh:
            fh.write(original)

    st, hdr, restored = get(f"/jobs/{job_id}/download")
    check(st == 200 and hashlib.sha256(restored).hexdigest() == digest,
          "restored artifact downloads again with the registered digest")
    check(_header(hdr, "etag") == f'"{digest}"', "ETag restored with the file")


def check_unrenderable_document() -> None:
    print("\n[3] structurally valid but unrenderable document -> terminal fail")
    status, _, raw = upload(
        fixtures.structurally_present_but_corrupt_docx(), "broken.docx"
    )
    check(status == 201,
          f"container-valid (but corrupt) doc creates a task, got {status}")
    job_id = json.loads(raw)["id"]
    final = wait_status(job_id, {"succeeded", "failed"}, DEADLINE_SECONDS)
    check(final["status"] == "failed", "job reaches terminal failed state")
    check(final["error_code"] == "CONVERSION_FAILED",
          f"stable error_code CONVERSION_FAILED, got {final['error_code']}")
    check(bool(final.get("error_message")), "a human-readable reason is stored")
    check(final["download_url"] is None, "failed job exposes no download URL")

    st, _, raw = get(f"/jobs/{job_id}/download")
    body = json.loads(raw)
    check(st == 409, f"download of failed job is 409, got {st}")
    check(body["error"]["code"] == "JOB_NOT_READY",
          "stable code JOB_NOT_READY on premature download")


def check_rejected_containers() -> None:
    print("\n[4] bad uploads never create tasks and return stable codes")

    st, _, raw = upload(fixtures.not_a_zip(), "fake.docx")
    body = json.loads(raw)
    check(st == 400 and body["error"]["code"] == "NOT_A_ZIP",
          f"non-ZIP -> 400 NOT_A_ZIP (got {st})")

    st, _, raw = upload(fixtures.plain_zip_missing_parts(), "empty.docx")
    body = json.loads(raw)
    check(st == 400 and body["error"]["code"] == "DOCX_MISSING_PARTS",
          f"ZIP missing OOXML parts -> 400 DOCX_MISSING_PARTS (got {st})")

    # Build a container larger than 10 MiB (padding stored uncompressed).
    buf = io.BytesIO()
    pad = b"0" * (11 * 1024 * 1024)
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("[Content_Types].xml", "<x/>")
        zf.writestr("word/document.xml", pad)
    st, _, raw = upload(buf.getvalue(), "huge.docx")
    body = json.loads(raw)
    check(st == 413 and body["error"]["code"] == "FILE_TOO_LARGE",
          f">10 MiB upload -> 413 FILE_TOO_LARGE (got {st})")

    st, _, raw = get("/jobs/does-not-exist")
    body = json.loads(raw)
    check(st == 404 and body["error"]["code"] == "JOB_NOT_FOUND",
          f"unknown job -> 404 JOB_NOT_FOUND (got {st})")


def check_legacy_records_compatible(original_job_id: str) -> None:
    print("\n[5] historical fingerprint-free records keep the old behavior")
    src_pdf = os.path.join(STORAGE_DIR, "pdf", f"{original_job_id}.pdf")
    with open(src_pdf, "rb") as fh:
        pdf = fh.read()

    legacy_id = str(uuid.uuid4())
    legacy_pdf = os.path.join(STORAGE_DIR, "pdf", f"{legacy_id}.pdf")
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(sep=" ")
    try:
        with open(legacy_pdf, "wb") as fh:
            fh.write(pdf)
        conn = _db()
        try:
            # A succeeded row exactly as an older build would have committed it:
            # official path registered, fingerprint columns left NULL.
            conn.execute(
                "INSERT INTO jobs (id, original_filename, source_path, status, "
                "attempts, lease_owner, lease_expires_at, pdf_path, pdf_size, "
                "pdf_sha256, error_code, error_message, created_at, updated_at) "
                "VALUES (?, ?, ?, 'succeeded', 1, NULL, NULL, ?, NULL, NULL, "
                "NULL, NULL, ?, ?)",
                (legacy_id, f"{legacy_id}.docx",
                 f"/data/storage/source/{legacy_id}.docx",
                 legacy_pdf, now, now),
            )
            conn.commit()
        finally:
            conn.close()

        st, _, raw = get(f"/jobs/{legacy_id}")
        body = json.loads(raw)
        check(st == 200 and body["status"] == "succeeded",
              "legacy succeeded job is still queryable")
        check("artifact_size" not in body and "artifact_sha256" not in body,
              "legacy query omits the fingerprint keys entirely")
        check(body["download_url"] == f"/jobs/{legacy_id}/download",
              "legacy job keeps its original download URL")

        st, headers, delivered = get(f"/jobs/{legacy_id}/download")
        check(st == 200 and delivered == pdf,
              "legacy artifact downloads with the original behavior")
        check(_header(headers, "etag") is None,
              "no ETag is emitted for fingerprint-free records")

        digest = hashlib.sha256(pdf).hexdigest()
        st, _, _ = _request(
            "GET", f"/jobs/{legacy_id}/download",
            headers={"If-None-Match": f'"{digest}"'},
        )
        check(st == 200, "conditional request never 304s a legacy record")
    finally:
        conn = _db()
        try:
            conn.execute("DELETE FROM jobs WHERE id = ?", (legacy_id,))
            conn.commit()
        finally:
            conn.close()
        try:
            os.remove(legacy_pdf)
        except FileNotFoundError:
            pass


def main() -> int:
    print(f"Acceptance against {API_BASE}")
    wait_health()
    print("API healthy")
    try:
        recovered_id, recovered_digest = check_crash_recovery()
        check_tampered_artifact_rejected(recovered_id, recovered_digest)
        check_unrenderable_document()
        check_rejected_containers()
        check_legacy_records_compatible(recovered_id)
    except CheckFailed as exc:
        print(f"\nACCEPTANCE FAILED: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"\nACCEPTANCE ERROR: {exc!r}", file=sys.stderr)
        return 2
    print("\nALL ACCEPTANCE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
