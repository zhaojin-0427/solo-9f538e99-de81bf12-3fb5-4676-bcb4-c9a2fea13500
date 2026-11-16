#!/bin/sh
set -eu

# Initialise / migrate the SQLite schema before serving.
python - <<'PY'
from app import create_app
create_app()
print("database initialised")
PY

exec gunicorn \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers 1 \
    --threads 8 \
    --timeout 60 \
    --access-logfile - \
    --error-logfile - \
    wsgi:app
