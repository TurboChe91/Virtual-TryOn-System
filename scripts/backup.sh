#!/usr/bin/env bash
# Consistent online backup of the SQLite database (works while serving).
# Usage: scripts/backup.sh [dest.db]
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python -m lunelle.cli backup ${1:+--dest "$1"}
