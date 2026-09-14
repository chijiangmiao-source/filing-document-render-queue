#!/bin/sh
set -eu

role="${1:-api}"

case "$role" in
  api)
    exec uvicorn app.api:app --host 0.0.0.0 --port 8000
    ;;
  worker)
    exec python -m app.worker
    ;;
  verify)
    shift || true
    exec python -m verify.acceptance "$@"
    ;;
  *)
    echo "usage: entrypoint.sh {api|worker|verify} [args]" >&2
    exit 2
    ;;
esac
