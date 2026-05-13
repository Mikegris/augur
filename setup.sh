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

# ── pip sanity check ────────────────────────────────────────────────────────
# Some Python builds (notably the macOS Xcode CLT shim) lack a working pip /
# ensurepip. Fail fast here with actionable guidance instead of after the
# venv is half-built.
if ! python3 -m pip --version &>/dev/null; then
  echo "ERROR: python3 is installed but pip is not working."
  echo ""
  echo "Try one of:"
  echo "  • python3 -m ensurepip --upgrade"
  echo "  • macOS:  brew install python3"
  echo "  • Linux:  sudo apt install python3-pip python3-venv"
  exit 1
fi
echo "✓ pip available"

# ── Virtual environment ─────────────────────────────────────────────────────
if [ ! -d "venv" ]; then
  echo "► Creating virtual environment..."
  python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate

# ── Dependencies ────────────────────────────────────────────────────────────
echo "► Upgrading pip..."
pip install --upgrade pip

# Prefer the lock file for reproducible installs; fall back to the loose
# requirements.txt if it's missing (e.g. dev clones without a lock).
if [ -f "requirements.lock" ]; then
  echo "► Installing pinned dependencies from requirements.lock (this may take a few minutes)..."
  pip install -r requirements.lock
else
  echo "► Installing dependencies from requirements.txt (this may take a few minutes)..."
  pip install -r requirements.txt
fi

# ── Local config ────────────────────────────────────────────────────────────
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
  echo "✓ Created .env from .env.example (edit it to add an OPENAI_API_KEY if you want AI theses)"
fi

# ── Initialize the database ─────────────────────────────────────────────────
echo "► Initializing local database..."
if ! python3 - <<'PY'
import sys, traceback
try:
    import database
    database.init_db()
    print("✓ wealth.db ready")
except Exception as e:
    print(f"DB init failed: {type(e).__name__}: {e}", file=sys.stderr)
    traceback.print_exc(file=sys.stderr)
    sys.exit(1)
PY
then
  echo ""
  echo "✗ Database initialization failed. The error above usually means a"
  echo "  missing dependency or a corrupted wealth.db file. Try:"
  echo "    rm -f wealth.db wealth.db-shm wealth.db-wal && ./setup.sh"
  echo "  If it persists, file an issue at:"
  echo "    https://github.com/Mikegris/augur/issues"
  exit 1
fi

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   ✓ Setup complete                               ║"
echo "║   Run ./run.sh to start the app                  ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""
