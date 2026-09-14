# docx2pdf — crash-safe DOCX → PDF conversion service

A litigation-filing style service that accepts a DOCX and converts it to PDF
with a **pinned LibreOffice**, while guaranteeing that a crash mid-conversion
never exposes a partial document and never produces two finished artifacts.

* **FastAPI** receives uploads (max **10 MiB**), validates the OOXML container
  (`[Content_Types].xml` **and** `word/document.xml`), persists the task and
  immediately returns an id.
* A **single worker** claims tasks with a **lock-free database lease**
  (compare-and-set `UPDATE … RETURNING` on SQLite/WAL) and converts with a
  **fixed LibreOffice version** installed in the image.
* Lease lasts **30 s**; the holder renews every **10 s** while converting.
* If the worker dies, the lease expires and another worker re-converts from
  the **original DOCX**, at most **3 attempts**; the third failure is terminal.
* The result is first written to a **temp path**. Only the current lease
  holder can register the single official path and mark success **inside a
  transaction**; a late competitor's output is discarded. The download
  endpoint **never reads a temp file**.

There is **no fake converter and no bundled/fixed PDF** anywhere in the repo;
PDFs are produced by the real LibreOffice at runtime.

## Layout

```
app/            FastAPI app, worker, lease repository, converter, models
  api.py            HTTP endpoints and stable error envelope
  validation.py     ZIP / OOXML container validation (runs before a job exists)
  repository.py     lock-free claim, renewal, holder-checked publish/failure
  converter.py      real pinned LibreOffice invocation + %PDF- magic check
  worker.py         poll loop, 10 s lease-renewal thread, temp->official gate
verify/         one-shot acceptance service (kills/restarts the worker)
tests/          pytest suite incl. real LibreOffice E2E + SIGKILL recovery
Dockerfile      pinned image (Debian snapshot + exact LibreOffice build)
docker-compose.yml   api + worker + one-shot verify; API_PORT overridable
```

## Run with Docker Compose

```bash
# Build and start API + worker. Override the host port if you like:
API_PORT=9000 docker compose up --build

# Submit a document:
curl -f -X POST http://localhost:9000/jobs \
  -F "file=@pleading.docx" -H "Accept: application/json"
# -> {"id":"...","status":"pending", ...}

# Poll status (failed jobs carry error_code/error_message and no download_url):
curl -f http://localhost:9000/jobs/<id>

# Download only exists after success, always the one official %PDF- file:
curl -f -OJ http://localhost:9000/jobs/<id>/download
```

`API_PORT` (default `8080`) sets the **host** port; the container always
listens on 8000.

## One-shot acceptance (`verify`)

The `verify` profile builds a heavy real DOCX, waits for the worker to start
converting it, **SIGKILLs the worker container mid-conversion, starts it
again**, waits for the 30 s lease to expire and a re-run to finish, then
asserts:

* exactly **one** downloadable artifact exists and it starts with `%PDF-`;
* a structurally-valid-but-unrenderable document ends **failed**, with a
  queryable `CONVERSION_FAILED` reason and **no** download URL;
* invalid containers never create a task and return stable codes
  (`NOT_A_ZIP`, `DOCX_MISSING_PARTS`, `FILE_TOO_LARGE`, `JOB_NOT_FOUND`).

```bash
docker compose build
docker compose --profile verify run --rm verify
```

It drives the worker container over the mounted Docker socket using a
checksum-pinned static `docker` CLI baked into the image.

## Reproducible converter version

The image freezes `apt` to Debian snapshot `20260701T084025Z` and installs an
exact build — `libreoffice-writer-nogui=4:7.4.7-1+deb12u14` — rather than a
floating candidate, so the converter is concrete and reproducible.

## Stable error codes

All errors use `{"error":{"code": ..., "message": ...}}` with fixed codes:

| Code                 | HTTP | Meaning                                    |
|----------------------|------|--------------------------------------------|
| `FILE_TOO_LARGE`     | 413  | upload > 10 MiB                            |
| `NO_FILE`            | 400  | missing/empty multipart `file`             |
| `NOT_A_ZIP`          | 400  | not a valid ZIP container                  |
| `DOCX_MISSING_PARTS` | 400  | ZIP lacks OOXML required parts             |
| `JOB_NOT_FOUND`      | 404  | unknown id                                 |
| `JOB_NOT_READY`      | 409  | no finished PDF yet (failed/in-flight)     |
| `ARTIFACT_MISSING`   | 404  | registered artifact unavailable on disk    |
| `CONVERSION_FAILED`  | 500  | LibreOffice could not render (stored reason)|
| `VALIDATION_ERROR`   | 400  | malformed request                          |
| `INTERNAL_ERROR`     | 500  | unexpected server error                    |

## Local development / tests

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt

# Unit tests run without LibreOffice. E2E/crash tests use the real binary:
# point LOCAL_SOFFICE at a `soffice` (or wrapper) on your machine.
LOCAL_SOFFICE=/path/to/soffice .venv/bin/pytest
```

Tests never ship a PDF: they generate DOCX fixtures in memory and assert the
real converter emits `%PDF-`.
