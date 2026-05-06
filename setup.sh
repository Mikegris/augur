#!/usr/bin/env bash
# AUGUR — one-time install (macOS / Linux)
# Creates a Python virtual environment, installs dependencies, and
# initializes the local SQLite database.

set -e
cd "$(dirname "$0")"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   AUGUR — Setup                                  ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── Python check ────────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "ERROR: python3 not found."
  echo ""
  echo "Install Python 3.9 or newer:"
  echo "  macOS:  brew install python3   (or download from python.org)"
  echo "  Linux:  sudo apt install python3 python3-venv python3-pip"
  exit 1
fi

PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info[1])')

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 9 ]; }; then
  echo "ERROR: Python 3.9+ required (you have ${PY_VERSION})."
  exit 1
fi
echo "✓ Python ${PY_VERSION} detected"

# ── Virtual environment ─────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
  echo "► Creating virtual environment..."
  python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

# ── Dependencies ────────────────────────────────────────────────────────────
echo "► Upgrading pip..."
pip install -q --upgrade pip

echo "► Installing dependencies (this may take a few minutes)..."
pip install -q -r requirements.txt

# ── Local config ────────────────────────────────────────────────────────────
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
  echo "✓ Created .env from .env.example (edit it to add an OPENAI_API_KEY if you want AI theses)"
fi

# ── Initialize the database ─────────────────────────────────────────────────
echo "► Initializing local database..."
python3 -c "import database; database.init_db(); print('✓ wealth.db ready')"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   ✓ Setup complete                               ║"
echo "║   Run ./run.sh to start the app                  ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
