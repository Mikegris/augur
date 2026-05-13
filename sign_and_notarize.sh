#!/usr/bin/env bash
# Codesign AUGUR.app + the DMG, submit for Apple notarization, staple the
# ticket so the result opens without Gatekeeper warnings on any Mac.
#
# Usage:
#   ./sign_and_notarize.sh
#
# Required environment:
#   DEVELOPER_ID_APPLICATION   "Developer ID Application: Your Name (ABCDE12345)"
#                              From `security find-identity -v -p codesigning`
#
#   NOTARYTOOL_PROFILE         A keychain profile name you've stored with:
#                                xcrun notarytool store-credentials AUGUR-notarize \
#                                  --apple-id you@example.com \
#                                  --team-id ABCDE12345 \
#                                  --password <app-specific-password>
#                              Then export NOTARYTOOL_PROFILE=AUGUR-notarize.
#                              (App-specific passwords come from
#                              https://account.apple.com → App-Specific Passwords.)
#
# Optional environment:
#   SKIP_NOTARIZE=1            Sign locally but skip the Apple submission.
#                              Useful for iterating on entitlements.
#
# Output:
#   dist/AUGUR.app             Codesigned, hardened-runtime, ready to ship.
#   dist/AUGUR.dmg             Codesigned + notarized + stapled.

set -euo pipefail
cd "$(dirname "$0")"

APP_PATH="dist/AUGUR.app"
DMG_PATH="dist/AUGUR.dmg"
ENTITLEMENTS="entitlements.plist"

say() { printf "\033[1;36m▸\033[0m %s\n" "$*"; }
ok()  { printf "\033[1;32m✓\033[0m %s\n" "$*"; }
warn(){ printf "\033[1;33m⚠\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m✗\033[0m %s\n" "$*" >&2; }

# ── preflight ───────────────────────────────────────────────────────────────
need_var() {
  local name="$1"
  if [ -z "${!name:-}" ]; then
    err "$name is not set. See the header of this script for setup."
    exit 1
  fi
}

if [ ! -d "$APP_PATH" ]; then
  err "$APP_PATH not found — run 'python setup_app.py py2app' first."
  exit 1
fi
if [ ! -f "$ENTITLEMENTS" ]; then
  err "$ENTITLEMENTS not found — was this script run from the project root?"
  exit 1
fi

need_var DEVELOPER_ID_APPLICATION

# Verify the identity is actually present in the keychain. Mistyped or
# expired identities are the #1 first-time failure here.
if ! security find-identity -v -p codesigning 2>/dev/null \
     | grep -q "$DEVELOPER_ID_APPLICATION"; then
  err "Codesigning identity not found in keychain: $DEVELOPER_ID_APPLICATION"
  echo
  echo "  Available identities:"
  security find-identity -v -p codesigning | sed 's/^/    /'
  exit 1
fi
ok "Identity present: $DEVELOPER_ID_APPLICATION"

if [ "${SKIP_NOTARIZE:-0}" != "1" ]; then
  need_var NOTARYTOOL_PROFILE
  if ! xcrun notarytool history --keychain-profile "$NOTARYTOOL_PROFILE" \
        --output-format json >/dev/null 2>&1; then
    err "notarytool profile '$NOTARYTOOL_PROFILE' not usable."
    echo "  Create one with:"
    echo "    xcrun notarytool store-credentials $NOTARYTOOL_PROFILE \\"
    echo "      --apple-id <email> --team-id <ID> --password <app-specific-pw>"
    exit 1
  fi
  ok "notarytool profile usable: $NOTARYTOOL_PROFILE"
fi

# ── sign the .app ───────────────────────────────────────────────────────────
# --deep: walk into nested bundles (Python.framework, pyobjc helpers)
# --options runtime: hardened runtime, required for notarization
# --timestamp: secure timestamp (also required)
# --force: re-sign any pre-existing signatures
say "Codesigning $APP_PATH (this can take ~30s for a Python bundle) ..."
codesign \
  --sign "$DEVELOPER_ID_APPLICATION" \
  --deep \
  --force \
  --options runtime \
  --timestamp \
  --entitlements "$ENTITLEMENTS" \
  "$APP_PATH"

say "Verifying signature ..."
codesign --verify --deep --strict --verbose=2 "$APP_PATH" 2>&1 | tail -5
ok ".app signed"

# Gatekeeper local assessment — what a fresh Mac would see (pre-notarize).
# Will print "rejected" until notarization is stapled; that's expected.
spctl --assess --type execute --verbose "$APP_PATH" 2>&1 | head -3 || true

# ── repack the DMG with the signed .app ─────────────────────────────────────
say "Rebuilding DMG with the signed bundle ..."
./build_dmg.sh >/dev/null
ok "DMG rebuilt: $DMG_PATH"

# ── sign the DMG itself ─────────────────────────────────────────────────────
say "Codesigning the DMG ..."
codesign --sign "$DEVELOPER_ID_APPLICATION" --timestamp "$DMG_PATH"
ok "DMG signed"

# ── notarize ────────────────────────────────────────────────────────────────
if [ "${SKIP_NOTARIZE:-0}" = "1" ]; then
  warn "SKIP_NOTARIZE=1 — stopping after local signing."
  echo "  Run again without SKIP_NOTARIZE to submit to Apple."
  exit 0
fi

say "Submitting to Apple notary service (this typically takes 2-5 min) ..."
# --wait blocks until the submission is processed and prints the verdict.
# notarytool exits non-zero on rejection, so set -e catches it.
xcrun notarytool submit "$DMG_PATH" \
  --keychain-profile "$NOTARYTOOL_PROFILE" \
  --wait
ok "Notarization accepted"

# ── staple the ticket ───────────────────────────────────────────────────────
# Stapling embeds the notarization ticket so the DMG opens offline on
# users' Macs (Gatekeeper otherwise needs network to check).
say "Stapling notarization ticket ..."
xcrun stapler staple "$DMG_PATH"
ok "Stapled"

# ── final assessment ────────────────────────────────────────────────────────
say "Final Gatekeeper check (what fresh Macs will see):"
spctl --assess --type open --context context:primary-signature --verbose "$DMG_PATH" 2>&1
echo
ok "Done. $DMG_PATH is ready to distribute without Gatekeeper warnings."
