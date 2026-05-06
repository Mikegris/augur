#!/usr/bin/env bash
# AUGUR // WEALTH INTELLIGENCE SYSTEM — Run
# First-time install? Run ./setup.sh first. This script just starts the app.

set -e
cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   AUGUR // WEALTH INTELLIGENCE SYSTEM            ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# Bootstrap if venv missing
if [ ! -d "venv" ]; then
  echo "► First run — bootstrapping via setup.sh ..."
  bash ./setup.sh
fi

# shellcheck disable=SC1091
source venv/bin/activate

PORT="${PORT:-5001}"
echo "► Starting server on http://localhost:${PORT}"
echo ""

# Open browser after a short delay (Mac uses 'open', Linux uses 'xdg-open')
(sleep 1.5 && (open "http://localhost:${PORT}" 2>/dev/null \
            || xdg-open "http://localhost:${PORT}" 2>/dev/null)) &

PORT="${PORT}" python3 app.py
