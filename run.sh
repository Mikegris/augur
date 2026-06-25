#!/usr/bin/env bash
# AUGUR // WEALTH INTELLIGENCE SYSTEM — Run
# First-time install? Run ./setup.sh first. This script just starts the app.

set -e
cd "$(dirname "$0")"
SELF="$PWD/$(basename "$0")"

echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   AUGUR // WEALTH INTELLIGENCE SYSTEM            ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── self-update ───────────────────────────────────────────────────────────────
# Fast-forward the current branch to its pushed upstream so each launch runs the
# latest version, then relaunch (in case run.sh itself changed). Safe + quiet:
# skipped when opted out, not a git checkout, no upstream, a dirty working tree,
# or offline; never clobbers local work (--ff-only only). Opt out with
# AUGUR_NO_SELF_UPDATE=1. AUGUR_SELF_UPDATED guards against a relaunch loop.
if [ "${AUGUR_NO_SELF_UPDATE:-0}" != "1" ] && [ "${AUGUR_SELF_UPDATED:-0}" != "1" ] \
   && command -v git >/dev/null 2>&1 \
   && git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if [ -n "$(git status --porcelain 2>/dev/null)" ]; then
    echo "► Local changes present — skipping auto-update (commit or 'git pull' yourself)."
  elif git rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
    echo "► Checking for updates ..."
    if git -c gc.auto=0 fetch --quiet --prune origin 2>/dev/null \
       && [ "$(git rev-parse @ 2>/dev/null)" != "$(git rev-parse '@{u}' 2>/dev/null)" ]; then
      if git merge --ff-only --quiet '@{u}' 2>/dev/null; then
        echo "✓ Updated to $(git rev-parse --short @) — relaunching."
        export AUGUR_SELF_UPDATED=1
        exec "$SELF" "$@"
      else
        echo "⚠ Branch diverged from upstream — skipping auto-update (resolve manually)."
      fi
    else
      echo "✓ Up to date."
    fi
  fi
fi

# Bootstrap if venv missing
if [ ! -d "venv" ]; then
  echo "► First run — bootstrapping via setup.sh ..."
  bash ./setup.sh
fi

# shellcheck disable=SC1091
source venv/bin/activate

# ── port selection ──────────────────────────────────────────────────────────
# Returns 0 if $1 is free, non-zero otherwise. Prefers lsof, falls back to a
# Python one-liner so we don't hard-depend on lsof / nc being installed.
port_in_use() {
  local p="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$p" -sTCP:LISTEN >/dev/null 2>&1
  else
    python3 - "$p" <<'PY'
import socket, sys
p = int(sys.argv[1])
s = socket.socket()
try:
    s.bind(("127.0.0.1", p))
except OSError:
    sys.exit(0)  # in use
finally:
    s.close()
sys.exit(1)  # free
PY
  fi
}

REQUESTED_PORT="${PORT:-5001}"
PORT="$REQUESTED_PORT"
if port_in_use "$PORT"; then
  echo "⚠ Port $PORT is in use — searching for a free port..."
  for candidate in $(seq $((REQUESTED_PORT + 1)) $((REQUESTED_PORT + 20))); do
    if ! port_in_use "$candidate"; then
      PORT="$candidate"
      break
    fi
  done
  if [ "$PORT" = "$REQUESTED_PORT" ]; then
    echo "✗ Could not find a free port in range ${REQUESTED_PORT}-$((REQUESTED_PORT + 20))."
    echo "  Set PORT=<free-port> ./run.sh and try again."
    exit 1
  fi
  echo "✓ Using port $PORT instead"
fi

echo "► Starting server on http://localhost:${PORT}"
echo ""

# ── browser auto-open ───────────────────────────────────────────────────────
# Poll the port until the server is actually answering, then open the
# browser. Beats a fixed sleep — slow machines and cold imports were causing
# the old `sleep 1.5` to open the browser before Flask was ready.
(
  for _ in $(seq 1 60); do
    if port_in_use "$PORT"; then
      open "http://localhost:${PORT}" 2>/dev/null \
        || xdg-open "http://localhost:${PORT}" 2>/dev/null \
        || true
      exit 0
    fi
    sleep 0.5
  done
) &

PORT="${PORT}" python3 app.py
