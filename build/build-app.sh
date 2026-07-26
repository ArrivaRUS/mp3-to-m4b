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
#   2. copy bin/runner.sh + the python `agent/` package + packaging/installer.sh +
#      the FROZEN packaging/mp3-to-m4b-agent helper into Contents/Resources
#   3. build AppIcon.icns from branding/icon-app.svg (cairosvg → else sips → else
#      skip with a warning — see ICON section)
#   4. write a clean Info.plist: CFBundleIdentifier=com.arrivarus.mp3tom4b
#      (stable! a drifting id breaks TCC grants on every rebuild),
#      CFBundleExecutable=mp3-to-m4b, LSMinimumSystemVersion=11.0
#   5. ad-hoc codesign (-s -) + strict verify, inside a retry loop (iCloud/
#      fileprovider FinderInfo race — neighbor's .patches/003 lesson)
#   6. verify the frozen helper's SHA-256 on three of its four borders — repo,
#      signed staging bundle, build/dist after ditto (border 4, the mounted final
#      DMG, is in make-dmg.sh / build-dmg.sh). Any mismatch is release-blocking:
#      those bytes are the identity the user's folder-access grant is pinned to.
#      See build/helper-guard.sh.
#
# Unsandboxed, no external Swift deps (SwiftUI/AppKit/Foundation), offline build.
#
# Output: build/dist/mp3-to-m4b.app
#
# Usage: build/build-app.sh [version] [--allow-sips]
#   --allow-sips  degrade to the low-fidelity `sips` rasterizer instead of
#                 failing when cairosvg is unusable. DEV ONLY — never for a
#                 release build; see the ICON section for why.

set -euo pipefail

VERSION=""
ALLOW_SIPS=0
for arg in "$@"; do
  case "$arg" in
    --allow-sips) ALLOW_SIPS=1 ;;
    -*) echo "build-app: unknown option '$arg' (usage: build-app.sh [version] [--allow-sips])" >&2; exit 2 ;;
    *)  VERSION="$arg" ;;
  esac
done
VERSION="${VERSION:-0.9}"
BUNDLE_ID="com.arrivarus.mp3tom4b"
APP_NAME="mp3-to-m4b"
MIN_MACOS="11.0"

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$REPO_DIR/build"
DIST_DIR="$BUILD_DIR/dist"

# Frozen-helper byte guard (borders 1-3 below; border 4 lives in the DMG scripts).
# The user's folder-access grant is pinned to the helper's bytes — see the header
# of helper-guard.sh for why a byte change here is a silent, total failure.
# shellcheck source=helper-guard.sh
. "$BUILD_DIR/helper-guard.sh"

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
# commands into queue/commands/ (M0.4); QueueView.swift is the "Очередь" screen
# (spec §7). Further reader screens are added later.
SWIFT_SRCS=(
  "$REPO_DIR/app/main.swift"
  "$REPO_DIR/app/Tokens.swift"
  "$REPO_DIR/app/StateModel.swift"
  "$REPO_DIR/app/WindowGeometry.swift"
  "$REPO_DIR/app/EngineClient.swift"
  "$REPO_DIR/app/EngineClient+Status.swift"
  "$REPO_DIR/app/QueueView.swift"
  "$REPO_DIR/app/StatusView.swift"
  "$REPO_DIR/app/SetupView.swift"
  "$REPO_DIR/app/FolderAccessCard.swift"
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

# --- BORDER 1/4: the artifact in the repo ------------------------------------
# Checked BEFORE anything is compiled: if the checkout itself already carries the
# wrong bytes (a bad merge, a git filter, a stray rebuild, an editor that
# "fixed" the file), there is nothing worth building. Fail in a second, not after
# a full compile+sign+package cycle.
echo "==> frozen helper: border 1/4 (repo artifact vs PROVENANCE.md)"
guard_helper_bytes "$HELPER_REPO_PATH" "repo"
# ...and the installer we are about to bundle must be checking for the SAME bytes.
# Otherwise all four borders pass and the shipped installer still refuses to run.
guard_installer_constant "$REPO_DIR/packaging/installer.sh" "repo"

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

# --- bundle the engine: python agent package + FDA runner + installer ------
# The app is a reader; the agent (this python package) is the engine and single
# writer. We ship both inside the bundle so the installer can stage them to App
# Support. The runner is the stable FDA target → `exec python3 -m agent`.
# packaging/installer.sh is bundled too: it resolves runner.sh + agent/ + the
# frozen helper as its SIBLINGS (its find_runner/find_agent_dir/find_agent_bin all
# check "$SELF_DIR"), so dropping all four into Resources lets a
# "do shell script <installer.sh>" front-end (the applet, or the app's Setup
# screen) install the background agent from the bundle.
#
# mp3-to-m4b-agent is the FROZEN Mach-O the LaunchAgent's ProgramArguments[0]
# points at — the file the user's folder-access grant is bound to. It is COPIED
# verbatim, never rebuilt, and its bytes are re-verified after signing (border 2)
# and after ditto (border 3). Its location here is not free to change: the
# installer looks for it beside itself.
echo "==> copying engine (agent/ + runner.sh + installer.sh + frozen helper) into Resources"
install -m 0755 "$REPO_DIR/bin/runner.sh" "$RES/runner.sh"
install -m 0755 "$REPO_DIR/packaging/installer.sh" "$RES/installer.sh"
install -m 0755 "$HELPER_REPO_PATH" "$APP/$HELPER_BUNDLE_RELPATH"
# Copy the python package verbatim (skip __pycache__ / pyc).
AGENT_DST="$RES/agent"
rm -rf "$AGENT_DST"
mkdir -p "$AGENT_DST"
for f in "$REPO_DIR"/agent/*.py; do
  install -m 0644 "$f" "$AGENT_DST/$(basename "$f")"
done

# --- icon: SVG -> PNG set -> .icns -----------------------------------------
# Render the SVG once at 1024 (transparent bg), then sips-downscale to each
# iconset size.
#
# cairosvg is the ONLY rasterizer we ship with. `sips` can read an SVG, but it
# does so crudely, and the failure is invisible: the build stays green and the
# release quietly carries a worse icon. The icon is the face of the release, so
# an unusable rasterizer is a BUILD ERROR (--allow-sips opts out, dev only).
#
# Two traps, both live on this machine — do not "simplify" back to calling the
# console script:
#   * $BUILD_DIR/.venv/bin/cairosvg is a console script. When the project path
#     contains a space it is invoked through /bin/sh, and SIP strips DYLD_* from
#     that shell's environment -> cairocffi's dlopen fails with
#     "cannot load library 'libcairo.2.dylib'". Calling the venv's python
#     directly with `-m cairosvg` keeps DYLD_* alive (memory:
#     venv-scripts-space-path-sip-strips-dyld).
#   * libcairo lives wherever Homebrew is; its prefix is DISCOVERED below, never
#     hardcoded.
echo "==> building AppIcon.icns from $(basename "$ICON_SVG")"
ICON_TMP="$(mktemp -d)"
ICONSET="$ICON_TMP/AppIcon.iconset"
mkdir -p "$ICONSET"
BASE_PNG="$ICON_TMP/base-1024.png"

VENV_PY="$BUILD_DIR/.venv/bin/python3"

# Where does libcairo.2.dylib live? brew first (handles a relocated Homebrew),
# then the conventional prefixes.
find_cairo_libdir() {
  local p
  for p in "$(brew --prefix cairo 2>/dev/null || true)" "$(brew --prefix 2>/dev/null || true)"; do
    [[ -n "$p" && -f "$p/lib/libcairo.2.dylib" ]] && { printf '%s' "$p/lib"; return 0; }
  done
  for p in /opt/homebrew/lib /usr/local/lib /opt/local/lib; do
    [[ -f "$p/libcairo.2.dylib" ]] && { printf '%s' "$p"; return 0; }
  done
  return 1
}

ICON_OK=0
RASTERIZER=""
ICON_WHY=""
if [[ ! -x "$VENV_PY" ]]; then
  ICON_WHY="no venv python at $VENV_PY"
else
  CAIRO_LIBDIR="$(find_cairo_libdir || true)"
  if [[ -z "$CAIRO_LIBDIR" ]]; then
    ICON_WHY="libcairo.2.dylib not found (brew --prefix cairo / the usual prefixes)"
  elif DYLD_FALLBACK_LIBRARY_PATH="$CAIRO_LIBDIR${DYLD_FALLBACK_LIBRARY_PATH:+:$DYLD_FALLBACK_LIBRARY_PATH}" \
       "$VENV_PY" -m cairosvg "$ICON_SVG" -o "$BASE_PNG" \
       --output-width 1024 --output-height 1024 2>"$ICON_TMP/cairo.err"; then
    RASTERIZER="cairosvg"
    echo "    rasterizer: cairosvg  ($VENV_PY -m cairosvg)"
    echo "                libcairo: $CAIRO_LIBDIR"
  else
    ICON_WHY="$(tr '\n' ' ' < "$ICON_TMP/cairo.err" | cut -c1-300)"
  fi
fi

# A produced file is not a produced ICON: check the geometry too, or a truncated
# render sails through as green.
if [[ -s "$BASE_PNG" ]]; then
  px_w="$(sips -g pixelWidth "$BASE_PNG" 2>/dev/null | awk '/pixelWidth/{print $2}')"
  px_h="$(sips -g pixelHeight "$BASE_PNG" 2>/dev/null | awk '/pixelHeight/{print $2}')"
  if [[ "$px_w" != "1024" || "$px_h" != "1024" ]]; then
    ICON_WHY="rasterizer produced ${px_w}x${px_h}, expected 1024x1024"
    rm -f "$BASE_PNG"
    RASTERIZER=""
  else
    echo "    base render: ${px_w}x${px_h} ok"
  fi
fi

if [[ ! -s "$BASE_PNG" ]]; then
  if [[ "$ALLOW_SIPS" -eq 1 ]]; then
    echo "    cairosvg unusable ($ICON_WHY)" >&2
    echo "    --allow-sips given -> degrading to the low-fidelity sips rasterizer" >&2
    sips -s format png "$ICON_SVG" --out "$BASE_PNG" >/dev/null 2>&1 || true
    [[ -s "$BASE_PNG" ]] && RASTERIZER="sips (DEGRADED)" && echo "    rasterizer: sips (DEGRADED — not release quality)"
  else
    cat >&2 <<EOF

build-app: FAILED — cannot rasterize the app icon with cairosvg.

  reason: $ICON_WHY

  The icon is release-visible, so the build refuses to substitute the
  low-fidelity 'sips' path silently. Fix the rasterizer:

    python3 -m venv "$BUILD_DIR/.venv"
    "$BUILD_DIR/.venv/bin/pip" install cairosvg
    brew install cairo

  Note: call it as '"\$VENV/bin/python3" -m cairosvg', never the console script
  '.venv/bin/cairosvg' — SIP strips DYLD_* from the /bin/sh that wraps it when
  the project path contains a space, and cairocffi then cannot dlopen libcairo.

  For a throwaway dev build only:  build/build-app.sh $VERSION --allow-sips

EOF
    rm -rf "$ICON_TMP"
    exit 1
  fi
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
  echo "" >&2
  echo "build-app: FAILED — iconutil could not assemble AppIcon.icns" >&2
  echo "           (base render came from: ${RASTERIZER:-none})." >&2
  echo "           Not shipping a bundle with a generic icon." >&2
  exit 1
fi
echo "    AppIcon.icns built via: $RASTERIZER"

# --- Info.plist: clean, written from scratch (native bundle) ---------------
# NSSupportsSuddenTermination / NSSupportsAutomaticTermination are DELIBERATELY NOT
# emitted below (both default to false when the key is absent). Sudden termination
# lets AppKit tear the process down with exit(): -applicationWillTerminate: never
# runs and pending completion handlers never fire. This app depends on BOTH — the
# delegate's teardown (app/main.swift applicationWillTerminate) and the agent
# installer, which runs as a CHILD Process() awaited synchronously with
# waitUntilExit (app/SetupView.swift). Being exit()'d mid-install leaves a
# half-installed agent and skips handleInstalled. Do not re-add these keys.
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

# --- BORDER 2/4: the signed staging bundle -----------------------------------
# `codesign --force --deep` walks the bundle and re-signs nested code. Empirically
# it leaves a bare Mach-O in Contents/Resources alone (it seals it as a resource
# instead) — but that is a property of today's toolchain, not a contract, and the
# xattr sweep above (`xattr -cr`, find -delete) runs over the same tree. So verify
# rather than assume: whatever the signing step did, the helper must come out of
# it byte-identical.
echo "==> frozen helper: border 2/4 (signed staging bundle, after codesign)"
guard_helper_bytes "$APP/$HELPER_BUNDLE_RELPATH" "signed staging .app"

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
# Destination signature: RELEASE-BLOCKING, not a warning.
# This used to warn and continue on the theory that a failure here is only ever
# the fileprovider FinderInfo xattr on the wrapper directory. That theory is
# right about the common case and useless as a policy: the same check is what
# fails when the payload really was modified, and a warning that scrolls past is
# how a broken bundle reaches the DMG step. guard_bundle_signature handles the
# xattr race properly (clear + retry ×5) and fails hard on anything else. It also
# uses --deep, which the old check did not — so this is a stronger test, not just
# a louder one.
guard_bundle_signature "$DEST_APP" "build/dist .app"

# --- BORDER 3/4: build/dist, after ditto -------------------------------------
# The staging bundle was verified, but what leaves this script is the ditto'd
# copy in build/dist — and build/dist is inside the (iCloud/fileprovider-synced)
# repo, where a daemon touches files behind our back. ditto is a copy, so this
# border also covers the copy itself.
echo "==> frozen helper: border 3/4 (build/dist bundle, after ditto)"
guard_helper_bytes "$DEST_APP/$HELPER_BUNDLE_RELPATH" "build/dist .app (post-ditto)"

echo ""
echo "Built: $DEST_APP"
echo "  CFBundleIdentifier: $(plutil -extract CFBundleIdentifier raw -o - "$DEST_APP/Contents/Info.plist")"
echo "  CFBundleExecutable: $(plutil -extract CFBundleExecutable raw -o - "$DEST_APP/Contents/Info.plist")"
echo "  Version:            $(plutil -extract CFBundleShortVersionString raw -o - "$DEST_APP/Contents/Info.plist")"
echo "  Architectures:      $(lipo -archs "$DEST_APP/Contents/MacOS/$APP_NAME")"
echo "  Icon:               $([[ "$ICON_OK" -eq 1 ]] && echo 'AppIcon.icns' || echo '(skipped — generic)')"
echo "  Frozen helper:      $HELPER_BUNDLE_RELPATH"
echo "                      sha256 $(helper_sha256 "$DEST_APP/$HELPER_BUNDLE_RELPATH")  (borders 1-3 verified)"
