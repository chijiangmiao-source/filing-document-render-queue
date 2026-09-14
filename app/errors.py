"""Stable error codes returned by the API and stored on failed jobs."""

# Upload / validation (no job is created).
FILE_TOO_LARGE = "FILE_TOO_LARGE"
NO_FILE = "NO_FILE"
NOT_A_ZIP = "NOT_A_ZIP"
DOCX_MISSING_PARTS = "DOCX_MISSING_PARTS"

# Job lifecycle.
JOB_NOT_FOUND = "JOB_NOT_FOUND"
JOB_NOT_READY = "JOB_NOT_READY"
ARTIFACT_MISSING = "ARTIFACT_MISSING"
# The registered artifact exists on disk but its byte size or SHA-256 digest
# no longer matches the fingerprint recorded at publish time (shared storage
# was modified/corrupted). The job row is never rewritten when this is raised.
ARTIFACT_CORRUPTED = "ARTIFACT_CORRUPTED"

# Conversion.
CONVERSION_FAILED = "CONVERSION_FAILED"

# Generic request validation (malformed multipart, bad field types).
VALIDATION_ERROR = "VALIDATION_ERROR"
INTERNAL_ERROR = "INTERNAL_ERROR"

# HTTP status paired with each code.
HTTP_STATUS = {
    FILE_TOO_LARGE: 413,
    NO_FILE: 400,
    NOT_A_ZIP: 400,
    DOCX_MISSING_PARTS: 400,
    JOB_NOT_FOUND: 404,
    JOB_NOT_READY: 409,
    ARTIFACT_MISSING: 404,
    ARTIFACT_CORRUPTED: 500,
    CONVERSION_FAILED: 500,
    VALIDATION_ERROR: 400,
    INTERNAL_ERROR: 500,
}
