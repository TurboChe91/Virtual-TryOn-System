#!/usr/bin/env bash
# Development mode: auto-reload on code changes (worker restarts with it).
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m uvicorn --factory lunelle.server:create_app \
  --host "${LUNELLE_HOST:-127.0.0.1}" --port "${LUNELLE_PORT:-8300}" --reload
