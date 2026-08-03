#!/usr/bin/env bash
# One-time local setup: venv + dependencies + config template + init.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
  || { echo "ERROR: Python >= 3.12 required (found $($PYTHON --version 2>&1))"; exit 1; }

if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt
if [ "${WITH_DEV:-0}" = "1" ]; then
  .venv/bin/pip install --quiet -r requirements-dev.txt
fi

if [ ! -f .env ]; then
  # umask before copy so the key is never briefly world-readable on disk.
  (umask 077 && cp .env.example .env)
  echo ">>> .env created from template — edit it and set LUNELLE_IMAGE_API_KEY."
fi

# .env holds a live API key; owner-only regardless of how it got here.
if [ -f .env ]; then
  chmod 600 .env || echo ">>> WARNING: could not chmod 600 .env"
  perms=$(stat -f "%Lp" .env 2>/dev/null || stat -c "%a" .env 2>/dev/null || echo "?")
  if [ "$perms" != "600" ]; then
    echo ">>> WARNING: .env permissions are $perms, expected 600 (it contains an API key)"
  fi
fi

.venv/bin/python -m lunelle.cli init || {
  echo ">>> init reported configuration problems; edit .env then re-run scripts/setup.sh"
  exit 1
}
echo ">>> setup complete. Start with scripts/start.sh"
