from __future__ import annotations

import io
import zipfile

import pytest

from app import errors
from app.validation import validate_docx

CT = (
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.'
    'openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)
DOC = (
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body><w:p/></w:body></w:document>"
)


def zip_bytes(items: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in items.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_valid_docx_passes():
    data = zip_bytes({"[Content_Types].xml": CT.encode(), "word/document.xml": DOC.encode()})
    validate_docx(data)  # no exception


def test_not_a_zip():
    with pytest.raises(ValueError) as exc:
        validate_docx(b"definitely not a zip" + b"\x00" * 50)
    assert str(exc.value) == errors.NOT_A_ZIP


def test_empty_payload():
    with pytest.raises(ValueError) as exc:
        validate_docx(b"")
    assert str(exc.value) == errors.NOT_A_ZIP


def test_zip_without_ooxml_parts():
    data = zip_bytes({"readme.txt": b"hello"})
    with pytest.raises(ValueError) as exc:
        validate_docx(data)
    assert str(exc.value) == errors.DOCX_MISSING_PARTS


def test_zip_only_content_types_missing_document():
    data = zip_bytes({"[Content_Types].xml": CT.encode()})
    with pytest.raises(ValueError) as exc:
        validate_docx(data)
    assert str(exc.value) == errors.DOCX_MISSING_PARTS


def test_truncated_zip_is_not_a_zip():
    data = zip_bytes({"[Content_Types].xml": CT.encode(), "word/document.xml": DOC.encode()})
    with pytest.raises(ValueError) as exc:
        validate_docx(data[: len(data) // 2])
    assert str(exc.value) == errors.NOT_A_ZIP


def test_corrupt_crc_member_rejected():
    good = zip_bytes({"[Content_Types].xml": CT.encode(), "word/document.xml": DOC.encode()})
    zf = zipfile.ZipFile(io.BytesIO(good))
    # Flip a byte inside the compressed stream; CRC must fail.
    damaged = bytearray(good)
    damaged[40] ^= 0xFF
    with pytest.raises(ValueError) as exc:
        validate_docx(bytes(damaged))
    assert str(exc.value) == errors.NOT_A_ZIP
    zf.close()
