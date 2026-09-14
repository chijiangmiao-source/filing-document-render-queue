from __future__ import annotations

import io
import zipfile

from . import errors

REQUIRED_ZIP_MEMBERS = ("[Content_Types].xml", "word/document.xml")


def validate_docx(data: bytes) -> None:
    """Validate an OOXML (.docx) ZIP container.

    The container must be a readable ZIP archive that contains both
    ``[Content_Types].xml`` and ``word/document.xml``. Any failure raises
    :class:`ValueError` carrying a stable error code; no job is created.
    """
    if not zipfile.is_zipfile(io.BytesIO(data)):
        raise ValueError(errors.NOT_A_ZIP)
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise ValueError(errors.NOT_A_ZIP)
            names = set(zf.namelist())
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError(errors.NOT_A_ZIP) from exc

    missing = [name for name in REQUIRED_ZIP_MEMBERS if name not in names]
    if missing:
        raise ValueError(errors.DOCX_MISSING_PARTS)
