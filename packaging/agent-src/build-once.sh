#!/bin/bash
# build-once.sh — ONE-SHOT build of the frozen FDA helper packaging/mp3-to-m4b-agent.
#
# ⚠️  THIS SCRIPT IS RUN ONCE, EVER. The produced binary is the TCC identity:
#     the user's Full Disk Access grant pins the ad-hoc designated requirement
#     (= cdhash of these exact bytes). REBUILDING — even from identical source,
#     even "just to refresh" — produces a NEW cdhash and SILENTLY KILLS the FDA
#     grant of every existing user (they all get a mystery re-grant trip to
#     System Settings). The committed packaging/mp3-to-m4b-agent is the single
#     source of the release file; build/build-app.sh only COPIES it, never
#     compiles.
#
#     Rebuild is a rare, deliberate event (e.g. a security fix in the helper):
#     set MP3TOM4B_AGENT_REBUILD_I_UNDERSTAND=1, update PROVENANCE.md, and add a
#     release-notes warning that access must be re-granted
#     (`requires_fda_regrant`).
#
# What it does:
#   clang (arm64 + x86_64 in one invocation → universal Mach-O), -Os,
#   min macOS 11.0 (the app's own contract) → strip → ad-hoc codesign
#   (the arm64 slice gets a linker ad-hoc signature anyway; an explicit
#   `codesign -s -` makes BOTH slices consistently signed) → print SHA-256 and
#   cdhash for PROVENANCE.md.
#
# Usage:  packaging/agent-src/build-once.sh

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$SRC_DIR/mp3-to-m4b-agent.c"
OUT="$(cd "$SRC_DIR/.." && pwd)/mp3-to-m4b-agent"
MIN_MACOS="11.0"

[[ -f "$SRC" ]] || { echo "build-once: missing $SRC" >&2; exit 1; }
for t in clang lipo strip codesign shasum; do
  command -v "$t" >/dev/null 2>&1 || { echo "build-once: required tool '$t' not found" >&2; exit 1; }
done

# --- the freeze guard: refuse to overwrite the frozen artifact ---------------
if [[ -f "$OUT" && "${MP3TOM4B_AGENT_REBUILD_I_UNDERSTAND:-}" != "1" ]]; then
  cat >&2 <<EOF
build-once: REFUSING to rebuild.

  $OUT already exists — it is the FROZEN artifact whose cdhash every user's
  Full Disk Access grant is pinned to. Rebuilding changes the cdhash and
  silently revokes those grants.

  If this is a deliberate, rare event (helper bug/security fix):
    MP3TOM4B_AGENT_REBUILD_I_UNDERSTAND=1 packaging/agent-src/build-once.sh
  then update packaging/agent-src/PROVENANCE.md (SHA-256 + cdhash + date) and
  warn in the release notes that access must be re-granted.
EOF
  exit 1
fi

echo "==> compiling universal (arm64 + x86_64), min macOS $MIN_MACOS"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BIN="$TMP/mp3-to-m4b-agent"
clang -Os -Wall -Wextra \
  -arch arm64 -arch x86_64 \
  -mmacosx-version-min="$MIN_MACOS" \
  -o "$BIN" "$SRC"

echo "==> strip + ad-hoc codesign"
strip "$BIN"
codesign --force -s - "$BIN"
codesign --verify --strict "$BIN"

install -m 0755 "$BIN" "$OUT"

echo ""
echo "Built (FROZEN from now on): $OUT"
echo "  archs:  $(lipo -archs "$OUT")"
echo "  sha256: $(shasum -a 256 "$OUT" | cut -d' ' -f1)"
echo "  size:   $(stat -f%z "$OUT") bytes"
echo "  cdhash / DR:"
codesign -dvvv "$OUT" 2>&1 | grep -iE 'CDHash|CandidateCDHash|Signature|Format' | sed 's/^/    /'
codesign -d -r- "$OUT" 2>&1 | sed 's/^/    /'
echo ""
echo "Record these values in packaging/agent-src/PROVENANCE.md and NEVER rebuild casually."
