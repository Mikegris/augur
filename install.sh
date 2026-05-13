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
#   AUGUR_NO_LINK=1           # skip ~/.local/bin/augur symlink

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

# ── helper: best-effort install of an `augur` command on PATH ───────────────
maybe_install_path_shim() {
  if [ "${AUGUR_NO_LINK:-0}" = "1" ]; then
    return
  fi
  local bin_dir="$HOME/.local/bin"
  local link="$bin_dir/augur"
  mkdir -p "$bin_dir"
  ln -sf "$TARGET_DIR/run.sh" "$link"
  ok "Installed launcher: $link → $TARGET_DIR/run.sh"

  # Is ~/.local/bin already on the user's PATH?
  case ":$PATH:" in
    *":$bin_dir:"*) return ;;
  esac

  warn "$bin_dir is not on your PATH."
  # Figure out which shell rc file to point them at
  local rc=""
  case "$(basename "${SHELL:-}")" in
    zsh)  rc="$HOME/.zshrc" ;;
    bash) rc="$HOME/.bashrc" ;;
    fish) rc="$HOME/.config/fish/config.fish" ;;
  esac
  echo
  echo "  Add this to ${rc:-your shell rc file} so \`augur\` works from anywhere:"
  if [ "$(basename "${SHELL:-}")" = "fish" ]; then
    echo "    set -Ux PATH \$HOME/.local/bin \$PATH"
  else
    echo "    export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
  echo "  Then restart your shell."
  echo
}

# ── target directory ────────────────────────────────────────────────────────
# If it already exists and is an augur git checkout, offer to update in place.
# Otherwise, bail with the existing helpful message.
if [ -e "$TARGET_DIR" ]; then
  if [ -d "$TARGET_DIR/.git" ] && command -v git >/dev/null 2>&1 \
     && git -C "$TARGET_DIR" remote get-url origin 2>/dev/null \
        | grep -Eqi "[/:]${REPO}(\.git)?$"; then
    say "Existing AUGUR install found at $TARGET_DIR"
    DO_UPDATE="no"
    if (exec </dev/tty) 2>/dev/null; then
      printf "Update in place (git pull + re-run setup)? [Y/n] "
      if read -r REPLY < /dev/tty 2>/dev/null; then
        case "$REPLY" in
          n|N|no|No) DO_UPDATE="no" ;;
          *)         DO_UPDATE="yes" ;;
        esac
      fi
    else
      # Non-interactive: default to update — that's the obvious intent when
      # someone re-runs the installer.
      DO_UPDATE="yes"
    fi
    if [ "$DO_UPDATE" = "yes" ]; then
      say "Updating $TARGET_DIR ..."
      git -C "$TARGET_DIR" pull --ff-only origin "$BRANCH"
      cd "$TARGET_DIR"
      bash ./setup.sh
      maybe_install_path_shim
      ok "Update complete."
      cat <<EOF

To start AUGUR:
  cd $TARGET_DIR
  ./run.sh
EOF
      exit 0
    fi
    err "Aborted."
    exit 1
  fi

  err "$TARGET_DIR already exists (not an augur checkout)."
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

# ── install ~/.local/bin/augur launcher ─────────────────────────────────────
maybe_install_path_shim

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

(or just \`augur\` if ~/.local/bin is on your PATH)

The app will open at http://localhost:5001
EOF
fi
