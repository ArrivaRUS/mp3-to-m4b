#!/bin/bash
# Package build/dist/mp3-to-m4b.app into a BRANDED, distributable .dmg.
#
# Uses dmgbuild (Python) — NOT create-dmg / scripted Finder. create-dmg lays out the
# window by scripting Finder over Apple Events; a headless build has no Automation
# (TCC) grant, so that step silently no-ops and you get a broken window (wrong size →
# white gap, generic icon positions). dmgbuild writes the .DS_Store directly
# (ds_store/mac_alias), so the layout is baked in without any Finder, working headless.
# Layout/geometry live in build/dmg-settings.py; this script feeds it the concrete
# paths and the version. Ported from the fb2-to-epub neighbor (.patches/001,003).
#
# This is the BRANDED path (background art + baked icon layout). The dependency-free
# build/build-dmg.sh remains as a fallback (plain hdiutil, no background) — see its
# header. Prefer make-dmg.sh for releases; it produces the branded window.
#
# Layout (design-agreed, enforced in dmg-settings.py, synced with the SVG background):
#   window 920x440 (macOS 26 opens the DMG window ~920 wide; matched → no white gap)
#   background 960x440 (>= window, dark to the edges)
#   app icon at (290,190), /Applications drop link at (630,190), icon size 120 (centered)
#   background = branding/dmg-background.png (+ @2x sibling → Retina-crisp TIFF)
#   app .app extension hidden; no toolbar/sidebar/statusbar/pathbar
#   volume icon = the app icon (.icns)
#
# Ad-hoc signature note: the bundled app is ad-hoc signed (codesign -s -), NOT
# notarized (no Apple Developer account — out of scope). Gatekeeper warns on first
# launch; the user opens it via right-click → Open (one time). The DMG itself is not
# signed/notarized either; that is expected for a personal build.
#
# Output: build/dist/mp3-to-m4b-<version>.dmg  (+ .sha256)
#
# Usage:
#   build/make-dmg.sh [version]          # normal release build (volname mp3-to-m4b)
#   build/make-dmg.sh [version] --test   # unique volname → dodge Finder's remembered
#                                        # window size when test-mounting repeatedly

set -euo pipefail

VERSION="${1:-0.9}"
MODE="${2:-}"
APP_NAME="mp3-to-m4b"
VOLNAME="mp3-to-m4b"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$REPO_DIR/build"
DIST_DIR="$BUILD_DIR/dist"
APP="$DIST_DIR/$APP_NAME.app"
DMG="$DIST_DIR/$APP_NAME-$VERSION.dmg"
BG="$REPO_DIR/branding/dmg-background.png"        # 1x; @2x sibling auto-picked
ICON="$APP/Contents/Resources/AppIcon.icns"      # volume icon
SETTINGS="$BUILD_DIR/dmg-settings.py"
VENV_DMGBUILD="$BUILD_DIR/.venv/bin/dmgbuild"

# Frozen-helper byte guard — border 4/4 (the .app inside the finished image).
# shellcheck source=helper-guard.sh
. "$BUILD_DIR/helper-guard.sh"

# Test mode: unique volume name so Finder can't reuse a stale remembered window
# geometry for a volume it has seen before (neighbor .patches/003). Writes a
# throwaway dmg next to dist.
if [[ "$MODE" == "--test" ]]; then
  VOLNAME="mp3-to-m4b-test-$(date +%s)"
  DMG="$DIST_DIR/$APP_NAME-$VERSION-TEST.dmg"
fi

# --- preconditions ---------------------------------------------------------
if [[ -x "$VENV_DMGBUILD" ]]; then
  DMGBUILD="$VENV_DMGBUILD"
elif command -v dmgbuild >/dev/null 2>&1; then
  DMGBUILD="$(command -v dmgbuild)"
else
  echo "make-dmg: dmgbuild not found. Install it:" >&2
  echo "    python3 -m venv build/.venv && build/.venv/bin/pip install dmgbuild" >&2
  exit 1
fi
command -v hdiutil >/dev/null 2>&1 || { echo "make-dmg: 'hdiutil' not found" >&2; exit 1; }
command -v shasum  >/dev/null 2>&1 || { echo "make-dmg: 'shasum' not found"  >&2; exit 1; }
[[ -d "$APP"      ]] || { echo "make-dmg: $APP not found — run build/build-app.sh first" >&2; exit 1; }
[[ -f "$SETTINGS" ]] || { echo "make-dmg: missing $SETTINGS" >&2; exit 1; }
[[ -f "$BG"       ]] || { echo "make-dmg: missing background $BG (render branding/dmg-background.svg first)" >&2; exit 1; }
[[ -f "$ICON"     ]] || { echo "make-dmg: missing volume icon $ICON" >&2; exit 1; }
if [[ ! -f "${BG%.png}@2x.png" ]]; then
  echo "make-dmg: WARNING — ${BG%.png}@2x.png missing; background will be 1x only (blurry on Retina)" >&2
fi

# The bundle must pass a non-strict signature check (the folder-access grant and
# the Gatekeeper launch both rely on it). RELEASE-BLOCKING, with a retry loop for
# the iCloud/fileprovider FinderInfo race — see guard_bundle_signature() for why
# --deep without --strict is the correct gauge here, and why this is a failure
# rather than the warning it used to be.
echo "==> app signature (release-blocking)"
guard_bundle_signature "$APP" "dist .app"

# The .app being packaged may have been built before packaging/installer.sh last
# changed. Re-assert here, on the copy that is actually about to ship, that the
# bundled installer is looking for the same helper bytes PROVENANCE.md pins.
guard_installer_constant "$APP/Contents/Resources/installer.sh" "bundled in dist .app"

# Background must be >= the window (920x440) so the Finder window (macOS 26 opens the
# DMG window ~920 wide, ignoring the remembered size) is fully covered — dark filler to
# the edges, never a white gap. dmgbuild anchors the bg top-left and does NOT scale/center
# it, so a larger image is safe; a smaller one risks white. Warn (don't fail) if under.
BG_W=$(sips -g pixelWidth  "$BG" 2>/dev/null | awk '/pixelWidth/{print $2}')
BG_H=$(sips -g pixelHeight "$BG" 2>/dev/null | awk '/pixelHeight/{print $2}')
echo "==> background 1x: ${BG_W:-?}x${BG_H:-?}  (window 920x440; bg >= window avoids white gap on macOS 26)"
if [[ -n "$BG_W" && -n "$BG_H" ]] && { [[ "$BG_W" -lt 920 ]] || [[ "$BG_H" -lt 440 ]]; }; then
  echo "make-dmg: WARNING — background ${BG_W}x${BG_H} is smaller than the 920x440 window" >&2
fi

# --- build the dmg ---------------------------------------------------------
# From here on an image FILE exists on disk. Every gate below is release-blocking,
# and a rejected image must not survive the failure: a .dmg sitting in build/dist
# is indistinguishable from a release candidate, and picking up the wrong one is
# exactly how a bad build reaches a user. Arm the cleanup before creating it.
DMG_COMMITTED=0
_on_exit_make_dmg() {
  local rc=$?
  if [[ "$rc" -ne 0 && "$DMG_COMMITTED" -ne 1 && -f "$DMG" ]]; then
    rm -f "$DMG" "$DMG.sha256"
    echo "make-dmg: removed the rejected image $(basename "$DMG") — it is not a release candidate." >&2
  fi
}
trap _on_exit_make_dmg EXIT

rm -f "$DMG"
echo "==> dmgbuild  vol='$VOLNAME'  (window 920x440, app@(290,190), /Applications@(630,190))"
"$DMGBUILD" \
  -s "$SETTINGS" \
  -D app="$APP" \
  -D appname="$APP_NAME" \
  -D bg="$BG" \
  -D icon="$ICON" \
  "$VOLNAME" \
  "$DMG"

# --- verify the image is well-formed + mountable (release-blocking) --------
echo "==> verifying image (hdiutil verify)"
guard_image_verify "$DMG"

# --- BORDER 4/4: the frozen helper INSIDE the finished image -----------------
# The last border, and the only one that looks at what a user actually receives.
# hdiutil verify above proves the image is well-formed, and codesign proves the
# bundle is signed — neither says a word about the helper's bytes. Mount the
# image, read the helper back out of it, compare, unmount. Release-blocking.
echo "==> frozen helper: border 4/4 (.app extracted from the mounted DMG)"
guard_helper_in_dmg "$DMG" "$APP_NAME" "mounted final DMG"

# --- checksum --------------------------------------------------------------
# Reached only with every gate green, so the .sha256 means "this image passed",
# not merely "this image exists".
( cd "$DIST_DIR" && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256" )
DMG_COMMITTED=1

echo ""
echo "Built: $DMG"
echo "  volume: $VOLNAME"
echo "  size:   $(du -h "$DMG" | cut -f1)"
echo "  sha256: $(cut -d' ' -f1 < "$DMG.sha256")"
echo ""
echo "Install: open the .dmg, drag mp3-to-m4b.app onto Applications."
echo "First launch (ad-hoc signed, not notarized): right-click the app → Open → Open."
