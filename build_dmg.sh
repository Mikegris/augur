#!/usr/bin/env bash
# Build a distributable DMG containing AUGUR.app.
#
# Usage:
#   ./build_dmg.sh
#
# Produces dist/AUGUR.dmg — a compressed disk image with the bundled app
# and an Applications symlink (so users can drag-to-install).
#
# Requires:
#   • dist/AUGUR.app — run `python setup_app.py py2app` first.
#   • hdiutil + SetFile — both ship with macOS / Xcode CLT.
#
# Heads up — distribution outside your own Mac:
#   The .app is NOT codesigned or notarized, so Gatekeeper will flag it on
#   other users' machines. Right-click → Open works as a one-time bypass.
#   For frictionless distribution you need an Apple Developer ID + a
#   codesign + xcrun notarytool flow.

set -euo pipefail
cd "$(dirname "$0")"

APP_PATH="dist/AUGUR.app"
DMG_PATH="dist/AUGUR.dmg"
VOLNAME="AUGUR"
STAGING="dist/dmg-staging"

say() { printf "\033[1;36m▸\033[0m %s\n" "$*"; }
ok()  { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; }

# ── preflight ───────────────────────────────────────────────────────────────
if [ ! -d "$APP_PATH" ]; then
  err "$APP_PATH not found — run 'python setup_app.py py2app' first."
  exit 1
fi
if ! command -v hdiutil >/dev/null 2>&1; then
  err "hdiutil not found — this script is macOS-only."
  exit 1
fi

# Unmount any stale mount from a prior run, otherwise hdiutil refuses.
if mount | grep -q "/Volumes/$VOLNAME"; then
  say "Detaching stale /Volumes/$VOLNAME ..."
  hdiutil detach "/Volumes/$VOLNAME" -force >/dev/null
fi

# ── stage layout ────────────────────────────────────────────────────────────
say "Staging DMG contents in $STAGING ..."
rm -rf "$STAGING" "$DMG_PATH"
mkdir -p "$STAGING"
# ditto preserves resource forks, extended attributes, and code-signing
# metadata. cp -R would strip some of that and break the bundle on launch.
ditto "$APP_PATH" "$STAGING/AUGUR.app"
ln -s /Applications "$STAGING/Applications"

# ── build the DMG ───────────────────────────────────────────────────────────
# UDZO = zlib-compressed read-only image (the standard distribution format).
# Size auto-fits to staging content thanks to -srcfolder.
say "Building $DMG_PATH ..."
hdiutil create \
  -volname "$VOLNAME" \
  -srcfolder "$STAGING" \
  -ov \
  -format UDZO \
  -imagekey zlib-level=9 \
  "$DMG_PATH" \
  >/dev/null

# ── stamp the volume icon ───────────────────────────────────────────────────
# Mount the DMG read-write to copy the .icns in as .VolumeIcon.icns, then
# re-flatten it back to a read-only compressed image. Without this dance the
# mounted volume shows a generic disk icon instead of the AUGUR mark.
if [ -f "icon.icns" ]; then
  say "Embedding volume icon ..."
  TMP_RW="dist/AUGUR-rw.dmg"
  hdiutil convert "$DMG_PATH" -format UDRW -o "$TMP_RW" -ov >/dev/null
  MOUNT_DIR=$(mktemp -d)
  hdiutil attach "$TMP_RW" -mountpoint "$MOUNT_DIR" -nobrowse -quiet
  cp "icon.icns" "$MOUNT_DIR/.VolumeIcon.icns"
  SetFile -c icnC "$MOUNT_DIR/.VolumeIcon.icns" 2>/dev/null || true
  SetFile -a C "$MOUNT_DIR" 2>/dev/null || true
  sync
  hdiutil detach "$MOUNT_DIR" -quiet
  rmdir "$MOUNT_DIR"
  hdiutil convert "$TMP_RW" -format UDZO -imagekey zlib-level=9 -o "$DMG_PATH" -ov >/dev/null
  rm -f "$TMP_RW"
fi

# ── cleanup + report ────────────────────────────────────────────────────────
rm -rf "$STAGING"
SIZE=$(du -h "$DMG_PATH" | awk '{print $1}')
ok "Built $DMG_PATH ($SIZE)"
echo
echo "Test it:"
echo "  open $DMG_PATH"
echo
echo "Distribute it: upload to GitHub Releases, send via Drop, etc."
echo "Reminder: unsigned .app triggers Gatekeeper on other Macs —"
echo "users can right-click → Open as a one-time bypass."
