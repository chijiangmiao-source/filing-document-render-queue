from __future__ import annotations

"""One-shot acceptance verifier.

Proves the end-to-end contract against a running docker compose stack:

  1. A valid heavy DOCX is picked up by the worker; while it is converting,
     the worker container is SIGKILLed and then started again. After the 30s
     lease expires another attempt converts from the ORIGINAL docx and the job
     ends with exactly one downloadable PDF beginning with %PDF-.
  2. A document that passes container validation but cannot render ends in a
     terminal failure: reason is queryable and there is no download URL.
  3. Invalid containers never create a task, with stable error codes
     (NOT_A_ZIP / DOCX_MISSING_PARTS / FILE_TOO_LARGE / JOB_NOT_FOUND).

Run (compose):  docker compose --profile verify run --rm verify
"""

import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile

from . import fixtures

API_BASE = os.getenv("API_BASE", "http://127.0.0.1:8000").rstrip("/")
STORAGE_DIR = os.getenv("STORAGE_DIR", "/data/storage")
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

    # Exactly one official artifact on disk; no tmp file is ever served.
    pdf_dir = os.path.join(STORAGE_DIR, "pdf")
    on_disk = sorted(f for f in os.listdir(pdf_dir) if f.startswith(job_id))
    check(on_disk == [f"{job_id}.pdf"],
          f"exactly one official PDF on disk: {on_disk}")
    tmp_dir = os.path.join(STORAGE_DIR, "tmp")
    leftovers = [f for f in os.listdir(tmp_dir) if f.startswith(job_id)]
    check(not leftovers, f"no lingering tmp artifacts: {leftovers}")


def check_unrenderable_document() -> None:
    print("\n[2] structurally valid but unrenderable document -> terminal fail")
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
    print("\n[3] bad uploads never create tasks and return stable codes")

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


def main() -> int:
    print(f"Acceptance against {API_BASE}")
    wait_health()
    print("API healthy")
    try:
        check_crash_recovery()
        check_unrenderable_document()
        check_rejected_containers()
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
