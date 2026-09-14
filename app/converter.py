from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import uuid

from .config import Settings

PDF_MAGIC = b"%PDF-"


class ConversionError(Exception):
    """Real conversion failure (LibreOffice exited badly or produced no PDF)."""


def _candidate_soffice(settings: Settings) -> str:
    configured = settings.soffice_bin
    if os.path.exists(configured):
        return configured
    # Helpful local fallback when running tests outside the pinned image.
    found = shutil.which("soffice")
    if found:
        return found
    return configured


def _run_soffice(
    soffice_bin: str,
    source: str,
    outdir: str,
    profile_url: str,
    timeout: float,
) -> subprocess.CompletedProcess:
    cmd = [
        soffice_bin,
        "--headless",
        "--invisible",
        "--nodefault",
        "--norestore",
        "--nolockcheck",
        "--nologo",
        "--nofirststartwizard",
        f"-env:UserInstallation={profile_url}",
        "--convert-to",
        "pdf:writer_pdf_Export",
        "--outdir",
        outdir,
        source,
    ]
    # Isolated environment: do not leak the host/venv Python settings into
    # LibreOffice's bundled Python (it prints prefix warnings otherwise).
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV")
    }
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def convert_docx_to_pdf(source_path: str, dest_pdf: str, settings: Settings) -> None:
    """Convert the original DOCX to PDF using the pinned LibreOffice.

    Writes the result to ``dest_pdf`` (a temporary path until publication).
    Raises :class:`ConversionError` for every non-success; never writes a
    placeholder or fixed PDF.
    """
    soffice_bin = _candidate_soffice(settings)
    workdir = tempfile.mkdtemp(prefix="conv-")
    # Unique profile per invocation lets several workers convert at once.
    profile_root = tempfile.mkdtemp(prefix="loprofile-")
    profile_url = "file://" + profile_root
    try:
        proc = _run_soffice(
            soffice_bin,
            source_path,
            workdir,
            profile_url,
            settings.conversion_timeout_seconds,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
            raise ConversionError(f"libreoffice exit {proc.returncode}: {detail}")

        produced = os.path.join(
            workdir, os.path.splitext(os.path.basename(source_path))[0] + ".pdf"
        )
        if not os.path.exists(produced):
            detail = (proc.stderr or proc.stdout or "").strip()[-2000:]
            raise ConversionError(f"no PDF produced: {detail}")

        with open(produced, "rb") as fh:
            head = fh.read(5)
        if head != PDF_MAGIC:
            raise ConversionError("converter output is not a PDF (bad magic header)")

        os.makedirs(os.path.dirname(os.path.abspath(dest_pdf)), exist_ok=True)
        staged = f"{dest_pdf}.{uuid.uuid4().hex}.part"
        shutil.move(produced, staged)
        os.replace(staged, dest_pdf)
    except subprocess.TimeoutExpired as exc:
        raise ConversionError(
            f"conversion timed out after {settings.conversion_timeout_seconds}s"
        ) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(profile_root, ignore_errors=True)
