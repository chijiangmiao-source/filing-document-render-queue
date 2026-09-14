from __future__ import annotations

import os
import re
import uuid

from .config import get_settings

_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def new_job_id() -> str:
    return str(uuid.uuid4())


def safe_filename(name: str | None) -> str:
    """Reduce an uploaded filename to a non-traversing basename."""
    base = os.path.basename(name or "")
    base = base.replace("\x00", "")
    cleaned = _SAFE_RE.sub("_", base).strip("._")
    return cleaned[:200] or "upload.docx"


def _storage_root() -> str:
    root = os.path.abspath(get_settings().storage_dir)
    return root


def ensure_layout() -> None:
    root = _storage_root()
    for sub in ("source", "tmp", "pdf"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)


def source_path(job_id: str) -> str:
    return os.path.join(_storage_root(), "source", f"{job_id}.docx")


def tmp_pdf_path(job_id: str, owner: str, attempt: int) -> str:
    safe_owner = _SAFE_RE.sub("_", owner)[:48]
    return os.path.join(
        _storage_root(), "tmp", f"{job_id}.a{attempt}.{safe_owner}.pdf"
    )


def final_pdf_path(job_id: str) -> str:
    return os.path.join(_storage_root(), "pdf", f"{job_id}.pdf")


def final_pdf_dir() -> str:
    return os.path.join(_storage_root(), "pdf")


def atomic_write_bytes(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{uuid.uuid4().hex}.part"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def remove_quiet(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def cleanup_job_tmp(job_id: str) -> None:
    """Remove temp artifacts a previous (dead) lease holder left for a job.

    Only the current lease holder converts a given job, so by the time it runs
    any prior owner's lease has expired and its partial output is garbage.
    Also removes an unreferenced orphan at the official path left by a crash
    between the file move and the transaction commit (safe: a job reaches this
    code only via claim, which never returns an already-succeeded job).
    """
    root = _storage_root()
    tmp_dir = os.path.join(root, "tmp")
    if os.path.isdir(tmp_dir):
        for name in os.listdir(tmp_dir):
            if name.startswith(f"{job_id}."):
                remove_quiet(os.path.join(tmp_dir, name))
    remove_quiet(final_pdf_path(job_id))
