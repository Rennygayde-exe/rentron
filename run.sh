#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_BIN="$SCRIPT_DIR/venv/bin/python"

if [[ ! -x "$VENV_BIN" ]]; then
  echo "Virtual environment not found at $VENV_BIN" >&2
  exit 1
fi

cd "$SCRIPT_DIR"
exec "$VENV_BIN" bot.py "$@"
