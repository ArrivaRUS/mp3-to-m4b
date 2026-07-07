#!/bin/bash
# Package build/dist/mp3-to-m4b.app into a distributable, drag-install .dmg.
#
# NOTE: this is the dependency-free FALLBACK (plain image, NO branded window). The
# branded installer (background art + baked icon layout) is build/make-dmg.sh
# (dmgbuild) — prefer that for releases. This script stays as a zero-dep safety net.
#
# Uses `hdiutil create` (no external deps) to build a compressed read-only image
# whose root holds the .app plus a symlink to /Applications, so the user opens the
# DMG and drags the app onto Applications. This is the zero-dependency path the
# brief asks for; it always produces a clean, mountable, drag-installable image.
#
# WHY NOT a scripted Finder layout here:
#   The fb2-to-epub neighbor learned (.patches/001, 003) that laying out the DMG
#   window via Finder/Apple Events is fragile headless (no Automation grant → the
#   layout silently no-ops → wrong window size, white gaps, hidden labels). Its fix
#   was `dmgbuild` (writes .DS_Store directly) PLUS a real Finder screenshot before
#   shipping. We deliberately keep THIS script dependency-free and functional
#   (open → drag to Applications works). A branded background + baked icon layout
#   is a separate polish step (clone the neighbor's dmgbuild + dmg-settings.py and
#   verify with a real screenshot — that visual sign-off is Yurka's job, per the
#   "real render, not structure" rule). See REPORT/HEARTBEAT.
#
# Ad-hoc signature note: the bundled app is ad-hoc signed (codesign -s -), NOT
# notarized (no Apple Developer account — out of scope). Gatekeeper will warn on
# first launch; the user opens it via right-click → Open (one time). The DMG
# itself is not signed/notarized either; that is expected for a personal build.
#
# Output: build/dist/mp3-to-m4b-<version>.dmg  (+ .sha256)
#
# Usage:
#   build/build-dmg.sh [version]          # normal build (volname mp3-to-m4b)
#   build/build-dmg.sh [version] --test   # unique volname → dodge Finder's cached
#                                         # window geometry when test-mounting

set -euo pipefail

VERSION="${1:-0.1.0}"
MODE="${2:-}"
APP_NAME="mp3-to-m4b"
VOLNAME="mp3-to-m4b"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$REPO_DIR/build"
DIST_DIR="$BUILD_DIR/dist"
APP="$DIST_DIR/$APP_NAME.app"
DMG="$DIST_DIR/$APP_NAME-$VERSION.dmg"

# Test mode: unique volume name so Finder can't reuse a stale remembered window
# geometry for a volume it has seen before (neighbor .patches/003). Writes a
# throwaway dmg next to dist.
if [[ "$MODE" == "--test" ]]; then
  VOLNAME="mp3-to-m4b-test-$(date +%s)"
  DMG="$DIST_DIR/$APP_NAME-$VERSION-TEST.dmg"
fi

# --- preconditions ---------------------------------------------------------
for t in hdiutil ditto codesign shasum; do
  command -v "$t" >/dev/null 2>&1 || { echo "build-dmg: required tool '$t' not found" >&2; exit 1; }
done
[[ -d "$APP" ]] || { echo "build-dmg: $APP not found — run build/build-app.sh first" >&2; exit 1; }

# Sanity: the bundle should at least pass a non-strict signature check (the FDA
# grant / Gatekeeper launch relies on this). Warn (don't fail) so a still-usable
# bundle can be packaged; the neighbor proved --deep (without --strict) is the
# right gauge for an ad-hoc bundle that may carry a FinderInfo xattr.
if codesign --verify --deep "$APP" >/dev/null 2>&1; then
  echo "==> app signature verifies (--deep)"
else
  echo "build-dmg: WARNING — '$APP' did not pass 'codesign --verify --deep'." >&2
  echo "           Packaging anyway, but the app may be rejected by Gatekeeper." >&2
fi

# --- stage the DMG payload --------------------------------------------------
# Assemble a clean staging dir (the future DMG root): the .app plus an
# /Applications symlink for drag-install. ditto preserves the signed bundle
# faithfully. Staging under TMPDIR keeps the iCloud/fileprovider daemon from
# re-stamping xattrs onto the payload mid-build (neighbor .patches/003).
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/mp3tom4b-dmg.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

echo "==> staging payload (.app + /Applications symlink)"
ditto "$APP" "$STAGE/$APP_NAME.app"
ln -s /Applications "$STAGE/Applications"

# Strip Finder/quarantine cruft from the staged copy so the image is clean.
find "$STAGE" -name '.DS_Store' -delete 2>/dev/null || true
find "$STAGE" -name '._*' -delete 2>/dev/null || true
xattr -cr "$STAGE/$APP_NAME.app" 2>/dev/null || true

# --- build the dmg ---------------------------------------------------------
# hdiutil create from a source folder: UDZO = zlib-compressed read-only (the
# standard distributable format). -ov overwrites; -volname sets the mounted name.
rm -f "$DMG"
echo "==> hdiutil create  vol='$VOLNAME'  ->  $(basename "$DMG")"
hdiutil create \
  -volname "$VOLNAME" \
  -srcfolder "$STAGE" \
  -fs HFS+ \
  -format UDZO \
  -ov \
  "$DMG" >/dev/null

# --- verify the image is well-formed + mountable ---------------------------
echo "==> verifying image (hdiutil verify)"
hdiutil verify "$DMG" >/dev/null 2>&1 \
  && echo "    image verifies" \
  || echo "    WARNING: hdiutil verify reported an issue" >&2

# --- checksum --------------------------------------------------------------
( cd "$DIST_DIR" && shasum -a 256 "$(basename "$DMG")" > "$(basename "$DMG").sha256" )

echo ""
echo "Built: $DMG"
echo "  volume: $VOLNAME"
echo "  size:   $(du -h "$DMG" | cut -f1)"
echo "  sha256: $(cut -d' ' -f1 < "$DMG.sha256")"
echo ""
echo "Install: open the .dmg, drag mp3-to-m4b.app onto Applications."
echo "First launch (ad-hoc signed, not notarized): right-click the app → Open → Open."
