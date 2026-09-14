#!/usr/bin/env python3
"""Build-time LibreOffice gate fixture (stdlib only).

Generates a minimal, valid .docx at the given path so the image build can run
a real headless DOCX->PDF conversion and fail early if a runtime library is
missing. No PDF is produced or shipped by this script; it only writes DOCX.
"""
import sys
import zipfile

CONTENT_TYPES = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    b'<Default Extension="rels" '
    b'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    b'<Default Extension="xml" ContentType="application/xml"/>'
    b'<Override PartName="/word/document.xml" '
    b'ContentType="application/vnd.openxmlformats-officedocument.'
    b'wordprocessingml.document.main+xml"/></Types>'
)
RELS = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
    b'relationships"><Relationship Id="rId1" '
    b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    b'officeDocument" Target="word/document.xml"/></Relationships>'
)
DOCUMENT = (
    b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
    b'2006/main"><w:body><w:p><w:r><w:t>build gate</w:t></w:r></w:p></w:body>'
    b'</w:document>'
)


def main(path: str = "/tmp/gate.docx") -> int:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", CONTENT_TYPES)
        zf.writestr("_rels/.rels", RELS)
        zf.writestr("word/document.xml", DOCUMENT)
    print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/gate.docx"))
