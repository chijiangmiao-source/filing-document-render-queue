from __future__ import annotations

import zipfile

_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.'
    'wordprocessingml.document.main+xml"/></Types>'
)

_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    'relationships"><Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    'officeDocument" Target="word/document.xml"/></Relationships>'
)


def _wrap_document(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
        '2006/main"><w:body>' + body + "</w:body></w:document>"
    ).encode("utf-8")


def valid_docx(paragraphs: int = 3) -> bytes:
    import io

    body = "".join(
        f'<w:p><w:r><w:t>Paragraph {i} - 诉讼文书验收测试。</w:t></w:r></w:p>'
        for i in range(paragraphs)
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", _wrap_document(body))
    return buf.getvalue()


def heavy_docx(paragraphs: int = 20000) -> bytes:
    """A large but valid document that takes LibreOffice several seconds.

    The long conversion gives the verifier a wide window to SIGKILL the worker
    mid-conversion (the core crash/recovery acceptance scenario).
    """
    import io

    body = "".join(
        f'<w:p><w:r><w:t>{i:05d} 诉讼文书正文内容，争议焦点与事实认定。'
        f"{'abcdefgh' * 12}</w:t></w:r></w:p>"
        for i in range(paragraphs)
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr("word/document.xml", _wrap_document(body))
    return buf.getvalue()


def structurally_present_but_corrupt_docx() -> bytes:
    """Passes ZIP/DOCX container validation but cannot be rendered."""
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _CONTENT_TYPES)
        zf.writestr("_rels/.rels", _RELS)
        zf.writestr(
            "word/document.xml",
            b"\x00\x01\x02 this is not XML <<<unparseable>>>",
        )
    return buf.getvalue()


def plain_zip_missing_parts() -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", b"not a docx container")
    return buf.getvalue()


def not_a_zip() -> bytes:
    return b"this is definitely not a zip file" + b"\x00" * 64
