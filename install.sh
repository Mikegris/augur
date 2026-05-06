#!/usr/bin/env bash
# AUGUR — one-line installer for macOS / Linux
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Mikegris/augur/main/install.sh | bash
#
# Tip: to read the script before running, open the URL in your browser, or:
#   curl -fsSL https://raw.githubusercontent.com/Mikegris/augur/main/install.sh
#
# Optional environment overrides:
#   AUGUR_DIR=~/somewhere     # install location (default: ~/augur)
#   AUGUR_REPO=Mikegris/augur # source repo
#   AUGUR_BRANCH=main         # branch to clone

set -euo pipefail

REPO="${AUGUR_REPO:-Mikegris/augur}"
BRANCH="${AUGUR_BRANCH:-main}"
TARGET_DIR="${AUGUR_DIR:-$HOME/augur}"

# ── pretty output ───────────────────────────────────────────────────────────
say()  { printf "\033[1;36m▸\033[0m %s\n" "$*"; }
ok()   { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m⚠\033[0m %s\n" "$*"; }
err()  { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; }

cat <<'BANNER'

╔══════════════════════════════════════════════════╗
║   AUGUR — one-line installer                     ║
║   Wealth Intelligence System                     ║
╚══════════════════════════════════════════════════╝

BANNER

# ── refuse to run as root ───────────────────────────────────────────────────
if [ "$(id -u)" -eq 0 ]; then
  err "Don't run this as root / via sudo — AUGUR is a per-user app."
  exit 1
fi

# ── detect OS ───────────────────────────────────────────────────────────────
OS="$(uname -s)"
case "$OS" in
  Darwin) PLATFORM="macOS" ;;
  Linux)  PLATFORM="Linux" ;;
  *)      err "Unsupported OS: $OS"; exit 1 ;;
esac
say "Platform: $PLATFORM"

# ── Python check ────────────────────────────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
  err "python3 not found."
  echo
  if [ "$PLATFORM" = "macOS" ]; then
    cat <<EOF
Install Python 3.9+ via one of:
  • Homebrew:   brew install python
  • Installer:  https://www.python.org/downloads/macos/

Then re-run this installer.
EOF
  else
    cat <<EOF
Install Python 3.9+ via your package manager:
  • Debian/Ubuntu: sudo apt install python3 python3-venv python3-pip
  • Fedora:        sudo dnf install python3 python3-pip
  • Arch:          sudo pacman -S python python-pip

Then re-run this installer.
EOF
  fi
  exit 1
fi

PY_VER=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
PY_MAJ=$(python3 -c 'import sys; print(sys.version_info[0])')
PY_MIN=$(python3 -c 'import sys; print(sys.version_info[1])')
if [ "$PY_MAJ" -lt 3 ] || { [ "$PY_MAJ" -eq 3 ] && [ "$PY_MIN" -lt 9 ]; }; then
  err "Python $PY_VER detected — AUGUR needs 3.9 or newer."
  exit 1
fi
ok "Python $PY_VER"

# ── target directory ────────────────────────────────────────────────────────
if [ -e "$TARGET_DIR" ]; then
  err "$TARGET_DIR already exists."
  cat <<EOF

Choose one:
  • cd "$TARGET_DIR" && ./run.sh
        (start the existing install)
  • mv "$TARGET_DIR" "${TARGET_DIR}.backup"
        (move it out of the way, then re-run this installer)
  • AUGUR_DIR=~/path/to/another/location bash <(curl -fsSL https://raw.githubusercontent.com/$REPO/$BRANCH/install.sh)
        (install to a different location)

EOF
  exit 1
fi

# ── fetch source ────────────────────────────────────────────────────────────
if command -v git >/dev/null 2>&1; then
  say "Cloning https://github.com/$REPO into $TARGET_DIR ..."
  git clone --depth 1 --branch "$BRANCH" "https://github.com/$REPO.git" "$TARGET_DIR"
else
  say "git not found — downloading source archive instead"
  TMP=$(mktemp -d)
  trap 'rm -rf "$TMP"' EXIT
  curl -fL --progress-bar \
    "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" \
    -o "$TMP/augur.tar.gz"
  mkdir -p "$TARGET_DIR"
  tar -xzf "$TMP/augur.tar.gz" -C "$TARGET_DIR" --strip-components=1
fi
ok "Source downloaded to $TARGET_DIR"

# ── run setup.sh ────────────────────────────────────────────────────────────
cd "$TARGET_DIR"
say "Running setup.sh (creates venv + installs dependencies, ~2-5 min)..."
bash ./setup.sh

# ── offer to start now ──────────────────────────────────────────────────────
# Default: don't auto-start. We only prompt when we can actually open /dev/tty
# for reading (which fails inside non-interactive subshells like CI runners,
# the Bash tool, or daemonized contexts — even though `[ -r /dev/tty ]` may
# return true for the special device).
echo
START="no"
if (exec </dev/tty) 2>/dev/null; then
  printf "Start AUGUR now? [Y/n] "
  if read -r REPLY < /dev/tty 2>/dev/null; then
    case "$REPLY" in
      n|N|no|No) START="no" ;;
      *)         START="yes" ;;
    esac
  fi
fi

if [ "$START" = "yes" ]; then
  exec bash ./run.sh
else
  echo
  ok "Install complete."
  cat <<EOF

To start AUGUR later:
  cd $TARGET_DIR
  ./run.sh

The app will open at http://localhost:5001
EOF
fi
