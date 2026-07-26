#!/usr/bin/env bash
# Full verification: lint, types, tests. Add RUN_REAL_E2E=1 for the paid e2e test.
set -euo pipefail
cd "$(dirname "$0")/.."
.venv/bin/ruff check lunelle tests
.venv/bin/python -m mypy lunelle
.venv/bin/python -m pytest tests
