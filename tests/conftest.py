from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import zipfile

import pytest

# Point the app at an isolated temp database/storage BEFORE it is imported.
_TMP = tempfile.mkdtemp(prefix="docx2pdf-test-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/test.db")
os.environ.setdefault("STORAGE_DIR", f"{_TMP}/storage")
# The real pinned LibreOffice is provided via LOCAL_SOFFICE when running
# outside the container. Inside the image SOFFICE_BIN already points at it.
_local_soffice = os.environ.get("LOCAL_SOFFICE")
if _local_soffice and not os.environ.get("SOFFICE_BIN"):
    os.environ["SOFFICE_BIN"] = _local_soffice

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import storage  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import Base, engine  # noqa: E402


CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
    'package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
    'officedocument.wordprocessingml.document.main+xml"/></Types>'
)
RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
    'relationships/officeDocument" Target="word/document.xml"/></Relationships>'
)


def make_docx(paragraphs=3, corrupt_document: bool = False) -> bytes:
    body = "".join(
        f'<w:p><w:r><w:t>测试段落 {i} court pleading.</w:t></w:r></w:p>'
        for i in range(paragraphs)
    )
    if corrupt_document:
        document = b"\x00\x01 not XML <<<unparseable>>>"
    else:
        document = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body}</w:body></w:document>"
        ).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", document)
    return buf.getvalue()


@pytest.fixture
def clean_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    root = get_settings().storage_dir
    if os.path.isdir(root):
        shutil.rmtree(root)
    storage.ensure_layout()
    yield engine
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def settings():
    return get_settings()
