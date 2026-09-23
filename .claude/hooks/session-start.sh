#!/bin/bash
set -euo pipefail

if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

cd "$CLAUDE_PROJECT_DIR"

# Debian system packages have no RECORD file so pip cannot upgrade them; cryptography also panics under databricks-sdk (CLAUDE.md §11).
pip install --quiet --ignore-installed cryptography cffi blinker
pip install --quiet -r requirements.txt

if [ -f .env ] && [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  grep -E '^[A-Za-z_][A-Za-z0-9_]*=' .env | sed 's/^/export /' >> "$CLAUDE_ENV_FILE"
fi
