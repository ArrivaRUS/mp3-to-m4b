#!/bin/bash
# Build mp3-to-m4b.app — NATIVE SwiftUI app.
#
# Adapted from the fb2-to-epub neighbor's proven build (same universal-swiftc +
# ad-hoc-codesign-with-retry shape). M0.1 scope: compile the empty-window app,
# bundle the python `agent` package + the FDA runner, build an icon if we can,
# and produce a codesigned bundle that opens a dark window.
#
# Steps:
#   1. compile app/*.swift for arm64 + x86_64 (xcrun swiftc) and lipo them into a
#      universal Contents/MacOS/mp3-to-m4b
#   2. copy bin/runner.sh + the python `agent/` package into Contents/Resources
#   3. build AppIcon.icns from branding/icon-app.svg (cairosvg → else sips → else
#      skip with a warning — see ICON section)
#   4. write a clean Info.plist: CFBundleIdentifier=com.arrivarus.mp3tom4b
#      (stable! a drifting id breaks TCC grants on every rebuild),
#      CFBundleExecutable=mp3-to-m4b, LSMinimumSystemVersion=11.0
#   5. ad-hoc codesign (-s -) + strict verify, inside a retry loop (iCloud/
#      fileprovider FinderInfo race — neighbor's .patches/003 lesson)
#
# Unsandboxed, no external Swift deps (SwiftUI/AppKit/Foundation), offline build.
#
# Output: build/dist/mp3-to-m4b.app
#
# Usage: build/build-app.sh [version]   (default version below)

set -euo pipefail

VERSION="${1:-0.1.0}"
BUNDLE_ID="com.arrivarus.mp3tom4b"
APP_NAME="mp3-to-m4b"
MIN_MACOS="11.0"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$REPO_DIR/build"
DIST_DIR="$BUILD_DIR/dist"

# Build + sign the bundle in a STAGING dir OUTSIDE the (iCloud/fileprovider-synced)
# repo, then move the finished, strict-verified bundle into build/dist. When the
# repo lives under iCloud, the fileprovider daemon re-stamps com.apple.FinderInfo /
# com.apple.fileprovider.fpfs#P onto the bundle ROOT directory asynchronously and
# OWNS those xattrs — `xattr -cr` can't keep them off, so an in-repo codesign loses
# the FinderInfo race on every attempt (neighbor .patches/003, and the local
# environment's known iCloud+codesign trap). A scratch dir under TMPDIR has no
# fileprovider daemon stamping it, so sign+verify is deterministic there.
STAGE_ROOT="$(mktemp -d "${TMPDIR:-/tmp}/mp3tom4b-build.XXXXXX")"
trap 'rm -rf "$STAGE_ROOT"' EXIT
APP="$STAGE_ROOT/$APP_NAME.app"

# Swift sources (compiled together, whole-module). main.swift drives the
# AppKit/SwiftUI window; Tokens.swift is the design-token source of truth;
# StateModel.swift is the read-only view of the agent's state.json + manifests
# (M0.3); EngineClient.swift is the app's WRITE side — it drops confirm-build
# commands into queue/commands/ (M0.4). Further reader screens are added later.
SWIFT_SRCS=(
  "$REPO_DIR/app/main.swift"
  "$REPO_DIR/app/Tokens.swift"
  "$REPO_DIR/app/StateModel.swift"
  "$REPO_DIR/app/EngineClient.swift"
)
ICON_SVG="$REPO_DIR/branding/icon-app.svg"

# --- tool checks -----------------------------------------------------------
for t in xcrun lipo sips iconutil plutil codesign; do
  command -v "$t" >/dev/null 2>&1 || { echo "build-app: required tool '$t' not found" >&2; exit 1; }
done
xcrun --find swiftc >/dev/null 2>&1 || { echo "build-app: swiftc not found (install Xcode)" >&2; exit 1; }
for s in "${SWIFT_SRCS[@]}"; do
  [[ -f "$s" ]] || { echo "build-app: missing $s" >&2; exit 1; }
done

SDK_PATH="$(xcrun --show-sdk-path --sdk macosx)"
[[ -d "$SDK_PATH" ]] || { echo "build-app: macOS SDK not found via xcrun" >&2; exit 1; }

# --- clean + build native universal binary ---------------------------------
rm -rf "$APP"
MACOS="$APP/Contents/MacOS"
RES="$APP/Contents/Resources"
mkdir -p "$MACOS" "$RES"

echo "==> compiling native SwiftUI binary (arm64 + x86_64)"
BIN_TMP="$(mktemp -d)"
for arch in arm64 x86_64; do
  echo "    swiftc -> $arch"
  xcrun swiftc \
    -sdk "$SDK_PATH" \
    -target "${arch}-apple-macos${MIN_MACOS}" \
    -O \
    "${SWIFT_SRCS[@]}" \
    -o "$BIN_TMP/$APP_NAME-$arch" 2>&1 | sed 's/^/    /'
  # swiftc exit code is hidden by the pipe to sed — verify the artifact exists.
  [[ -f "$BIN_TMP/$APP_NAME-$arch" ]] || {
    echo "build-app: swiftc failed to produce $arch binary" >&2; rm -rf "$BIN_TMP"; exit 1; }
done

echo "==> lipo -> universal $MACOS/$APP_NAME"
lipo -create "$BIN_TMP/$APP_NAME-arm64" "$BIN_TMP/$APP_NAME-x86_64" \
  -output "$MACOS/$APP_NAME"
chmod 0755 "$MACOS/$APP_NAME"
rm -rf "$BIN_TMP"
lipo -info "$MACOS/$APP_NAME" | sed 's/^/    /'

# --- bundle the engine: python agent package + FDA runner ------------------
# The app is a reader; the agent (this python package) is the engine and single
# writer. We ship both inside the bundle so the installer can stage them to App
# Support. The runner is the stable FDA target → `exec python3 -m agent`.
echo "==> copying engine (agent/ + runner.sh) into Resources"
install -m 0755 "$REPO_DIR/bin/runner.sh" "$RES/runner.sh"
# Copy the python package verbatim (skip __pycache__ / pyc).
AGENT_DST="$RES/agent"
rm -rf "$AGENT_DST"
mkdir -p "$AGENT_DST"
for f in "$REPO_DIR"/agent/*.py; do
  install -m 0644 "$f" "$AGENT_DST/$(basename "$f")"
done

# --- icon: SVG -> PNG set -> .icns -----------------------------------------
# Render the SVG once at 1024 (transparent bg), then sips-downscale to each
# iconset size. Rasterizer preference:
#   1. cairosvg (matches the neighbor's proven path; best fidelity) if present;
#   2. else `sips` reading the SVG directly (zero extra deps; works for this
#      flat icon — verified for branding/icon-app.svg);
#   3. else skip the icon with a loud warning (M0.1 stays unblocked; the bundle
#      gets the generic icon). TODO: pin a rasterizer in build/.venv before the
#      release-icon milestone so the shipped .icns is always cairosvg-quality.
echo "==> building AppIcon.icns from $(basename "$ICON_SVG")"
ICON_TMP="$(mktemp -d)"
ICONSET="$ICON_TMP/AppIcon.iconset"
mkdir -p "$ICONSET"
BASE_PNG="$ICON_TMP/base-1024.png"

CAIROSVG="$BUILD_DIR/.venv/bin/cairosvg"
[[ -x "$CAIROSVG" ]] || CAIROSVG="$(command -v cairosvg 2>/dev/null || true)"

ICON_OK=0
if [[ -n "$CAIROSVG" && -x "$CAIROSVG" ]]; then
  echo "    rasterizer: cairosvg"
  "$CAIROSVG" "$ICON_SVG" -o "$BASE_PNG" --output-width 1024 --output-height 1024 2>&1 | sed 's/^/    /' || true
fi
if [[ ! -s "$BASE_PNG" ]]; then
  echo "    rasterizer: sips (direct SVG read)"
  sips -s format png "$ICON_SVG" --out "$BASE_PNG" >/dev/null 2>&1 || true
fi

if [[ -s "$BASE_PNG" ]]; then
  make_size() { sips -z "$2" "$2" "$BASE_PNG" --out "$ICONSET/$1" >/dev/null; }
  make_size icon_16x16.png        16
  make_size icon_16x16@2x.png     32
  make_size icon_32x32.png        32
  make_size icon_32x32@2x.png     64
  make_size icon_128x128.png     128
  make_size icon_128x128@2x.png  256
  make_size icon_256x256.png     256
  make_size icon_256x256@2x.png  512
  make_size icon_512x512.png     512
  make_size icon_512x512@2x.png 1024
  if iconutil -c icns "$ICONSET" -o "$RES/AppIcon.icns" 2>/dev/null; then
    ICON_OK=1
  fi
fi
rm -rf "$ICON_TMP"

if [[ "$ICON_OK" -ne 1 ]]; then
  echo "    WARNING: could not build AppIcon.icns (no cairosvg, sips fallback failed)." >&2
  echo "             Bundle will use the generic icon. Install cairosvg into build/.venv" >&2
  echo "             (python3 -m venv build/.venv && build/.venv/bin/pip install cairosvg)." >&2
fi

# --- Info.plist: clean, written from scratch (native bundle) ---------------
echo "==> writing Info.plist (id=$BUNDLE_ID, exec=$APP_NAME, version=$VERSION)"
PLIST="$APP/Contents/Info.plist"
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>CFBundleIdentifier</key>
	<string>$BUNDLE_ID</string>
	<key>CFBundleExecutable</key>
	<string>$APP_NAME</string>
	<key>CFBundleName</key>
	<string>$APP_NAME</string>
	<key>CFBundleDisplayName</key>
	<string>$APP_NAME</string>
	<key>CFBundleShortVersionString</key>
	<string>$VERSION</string>
	<key>CFBundleVersion</key>
	<string>$VERSION</string>
	<key>CFBundlePackageType</key>
	<string>APPL</string>
	<key>CFBundleInfoDictionaryVersion</key>
	<string>6.0</string>
	<key>CFBundleDevelopmentRegion</key>
	<string>en</string>
	<key>CFBundleIconFile</key>
	<string>AppIcon</string>
	<key>LSMinimumSystemVersion</key>
	<string>$MIN_MACOS</string>
	<key>NSHighResolutionCapable</key>
	<true/>
	<key>NSPrincipalClass</key>
	<string>NSApplication</string>
	<key>NSSupportsAutomaticTermination</key>
	<true/>
	<key>NSSupportsSuddenTermination</key>
	<true/>
</dict>
</plist>
PLIST_EOF
plutil -lint "$PLIST" >/dev/null

# --- strip xattrs + ad-hoc sign + verify (strict), with retry --------------
# cairosvg/sips/iconutil/touch leave com.apple.FinderInfo / quarantine xattrs that
# make `codesign --deep --strict` reject the bundle. When the repo lives in a
# synced folder (iCloud/fileprovider), the daemon re-stamps com.apple.FinderInfo
# onto the bundle ROOT directory ASYNCHRONOUSLY — sometimes between strip and
# codesign, or codesign and verify — so the failure is a RACE that reproduces only
# intermittently (neighbor .patches/003). That xattr sits on the wrapper DIRECTORY,
# not on any signed payload, so clearing it just before sign/verify is safe.
# Strategy: run strip→sign→clean→verify inside a retry loop (up to 5, ~1s pause).
echo "==> ad-hoc codesign + strict verify (with retry)"
CODESIGN_OK=0
for attempt in 1 2 3 4 5; do
  echo "==> codesign attempt $attempt/5"
  find "$APP" -name '._*' -delete 2>/dev/null || true
  find "$APP" -name '.DS_Store' -delete 2>/dev/null || true
  xattr -cr "$APP" 2>/dev/null || true
  xattr -d com.apple.FinderInfo "$APP" 2>/dev/null || true

  if ! codesign --force --deep -s - "$APP"; then
    echo "    codesign --force failed (attempt $attempt/5), retrying after 1s" >&2
    sleep 1
    continue
  fi

  xattr -d com.apple.FinderInfo "$APP" 2>/dev/null || true

  if codesign --verify --deep --strict "$APP"; then
    CODESIGN_OK=1
    break
  fi
  echo "    strict verify failed (attempt $attempt/5), retrying after 1s" >&2
  sleep 1
done

if [[ "$CODESIGN_OK" -ne 1 ]]; then
  echo "build-app: codesign failed strict verify after 5 attempts (iCloud/fileprovider FinderInfo race)" >&2
  exit 1
fi
{ codesign --verify --deep --strict --verbose=2 "$APP" 2>&1 || true; } | sed 's/^/    /'

# --- move the staged, signed bundle into build/dist --------------------------
# The signature lives inside the bundle (Contents/_CodeSignature, embedded sigs),
# so it travels with the move. The destination wrapper dir may get re-stamped with
# the fileprovider FinderInfo xattr again — that sits on the directory, NOT on any
# signed payload, so a normal (non-strict) verify at the destination still passes
# and is what the OS uses to launch the app.
echo "==> moving signed bundle -> $DIST_DIR"
DEST_APP="$DIST_DIR/$APP_NAME.app"
mkdir -p "$DIST_DIR"
rm -rf "$DEST_APP"
# Use ditto to preserve the signed bundle structure/attributes faithfully.
ditto "$APP" "$DEST_APP"
xattr -d com.apple.FinderInfo "$DEST_APP" 2>/dev/null || true
if codesign --verify "$DEST_APP" >/dev/null 2>&1; then
  echo "    destination signature verifies"
else
  echo "    WARNING: destination signature verify reported an issue (likely the" >&2
  echo "             fileprovider FinderInfo xattr on the wrapper dir; the embedded" >&2
  echo "             signature is intact and the app will launch)." >&2
fi

echo ""
echo "Built: $DEST_APP"
echo "  CFBundleIdentifier: $(plutil -extract CFBundleIdentifier raw -o - "$DEST_APP/Contents/Info.plist")"
echo "  CFBundleExecutable: $(plutil -extract CFBundleExecutable raw -o - "$DEST_APP/Contents/Info.plist")"
echo "  Version:            $(plutil -extract CFBundleShortVersionString raw -o - "$DEST_APP/Contents/Info.plist")"
echo "  Architectures:      $(lipo -archs "$DEST_APP/Contents/MacOS/$APP_NAME")"
echo "  Icon:               $([[ "$ICON_OK" -eq 1 ]] && echo 'AppIcon.icns' || echo '(skipped — generic)')"
