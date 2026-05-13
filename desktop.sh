#!/usr/bin/env bash
# AUGUR — launch as a native desktop app (macOS / Linux).
# Wraps desktop.py with venv activation and first-run dependency install.

set -e
cd "$(dirname "$0")"

if [ ! -d "venv" ]; then
  echo "► First run — bootstrapping via setup.sh ..."
  bash ./setup.sh
fi

# shellcheck disable=SC1091
source venv/bin/activate

# Install desktop deps on first launch (cheap no-op afterward).
if ! python3 -c "import webview" 2>/dev/null; then
  echo "► Installing desktop dependencies (pywebview)..."
  pip install -r requirements-desktop.txt
fi

exec python3 desktop.py
