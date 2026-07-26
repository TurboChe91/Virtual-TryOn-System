#!/usr/bin/env bash
# Start the server in the foreground (production local mode).
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m lunelle.cli serve
