#!/bin/bash
# Frozen-helper byte guard — shared by build-app.sh, make-dmg.sh, build-dmg.sh.
#
# SOURCE this file, do not execute it:
#     . "$(dirname "${BASH_SOURCE[0]}")/helper-guard.sh"
#
# WHY THIS EXISTS
# ---------------
# packaging/mp3-to-m4b-agent is a FROZEN Mach-O. The user's folder-access (TCC)
# grant is pinned to two things at once: the path it is installed at AND the
# cdhash of these exact bytes. Change one byte anywhere in the pipeline and every
# existing user's grant dies — silently. The app still launches, the agent still
# starts, and the watch folder is simply never readable again. There is no error
# to see. See packaging/agent-src/PROVENANCE.md.
#
# So the golden SHA-256 is checked on FOUR borders (plan v2, M3f). Each of them
# is a place where the bytes have just been handed from one tool to another, and
# each of those tools has been observed to rewrite Mach-O files in some
# configuration (codesign re-signs nested code; ditto/hdiutil copy; lipo thins):
#
#   1. repo            — packaging/mp3-to-m4b-agent vs PROVENANCE.md   (build-app.sh)
#   2. signed staging  — <stage>.app/Contents/Resources/  AFTER codesign (build-app.sh)
#   3. dist            — build/dist/*.app/Contents/Resources/ AFTER ditto (build-app.sh)
#   4. mounted DMG     — the .app extracted from the FINAL image   (make-dmg.sh,
#                        build-dmg.sh)
#
# Border 4 is the one that actually matters: everything before it can be green
# while the artifact the user downloads already carries different bytes. It is
# also the only border that inspects the shipped product rather than an
# intermediate.
#
# Every failure is RELEASE-BLOCKING. There is deliberately no WARNING path and no
# override env var: a "we noticed but shipped anyway" outcome here is
# indistinguishable, for the user, from not having checked at all.

# Resolve our own location so the caller does not have to pass paths in.
HELPER_GUARD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HELPER_GUARD_REPO="$(cd "$HELPER_GUARD_DIR/.." && pwd)"

# The frozen helper: file name, where it lives in the repo, and where it must sit
# inside the .app. The bundle path is NOT free to change: packaging/installer.sh
# resolves the helper as a SIBLING of itself ("$SELF_DIR/$HELPER_NAME"), and
# build-app.sh installs installer.sh into Contents/Resources.
HELPER_NAME="mp3-to-m4b-agent"
HELPER_REPO_PATH="$HELPER_GUARD_REPO/packaging/$HELPER_NAME"
HELPER_BUNDLE_RELPATH="Contents/Resources/$HELPER_NAME"
HELPER_PROVENANCE="$HELPER_GUARD_REPO/packaging/agent-src/PROVENANCE.md"

# --- golden SHA: ONE source of truth ----------------------------------------
# The expected hash is read out of PROVENANCE.md rather than duplicated here.
# A second hardcoded copy is a second thing to forget on the day the helper is
# deliberately rebuilt — and the failure mode of forgetting is a build that
# happily blesses the wrong bytes.
#
# Fail-closed: if the document cannot be parsed, or yields anything other than
# exactly one 64-hex value, we have no golden and therefore cannot approve
# anything.
helper_golden_sha() {
  local hits n
  if [[ ! -f "$HELPER_PROVENANCE" ]]; then
    echo "helper-guard: cannot read the golden SHA — missing $HELPER_PROVENANCE" >&2
    return 1
  fi
  hits="$(grep -F 'EXPECTED_HELPER_SHA256' "$HELPER_PROVENANCE" \
          | grep -Eo '[0-9a-f]{64}' | sort -u)"
  n="$(printf '%s\n' "$hits" | grep -c . || true)"
  if [[ "$n" -ne 1 ]]; then
    {
      echo "helper-guard: cannot determine the golden helper SHA-256."
      echo "  source: $HELPER_PROVENANCE"
      echo "  found $n candidate value(s) on lines mentioning EXPECTED_HELPER_SHA256;"
      echo "  exactly 1 is required. Fix PROVENANCE.md — the build cannot verify the"
      echo "  frozen helper without it, and will not guess."
    } >&2
    return 1
  fi
  printf '%s' "$hits"
}

helper_sha256() {
  shasum -a 256 "$1" | cut -d' ' -f1
}

# _helper_guard_die <border> <file> <got> <golden> <extra-line>
_helper_guard_die() {
  local border="$1" file="$2" got="$3" golden="$4" extra="${5:-}"
  {
    echo ""
    echo "==============================================================================="
    echo "BUILD FAILED — frozen helper byte guard, border: $border"
    echo "==============================================================================="
    echo "  file:     $file"
    echo "  expected: $golden   (packaging/agent-src/PROVENANCE.md)"
    echo "  got:      $got"
    [[ -n "$extra" ]] && echo "  note:     $extra"
    echo ""
    echo "  What this means: the bytes of $HELPER_NAME changed somewhere in"
    echo "  the build pipeline. Those bytes ARE the identity of the LaunchAgent as far"
    echo "  as macOS is concerned — every user's folder-access grant is pinned to their"
    echo "  cdhash. Shipping this image would kill the grant of EVERY existing user,"
    echo "  silently: no error, no prompt, the app just stops seeing their folder."
    echo ""
    echo "  Do NOT bypass this check. Find what rewrote the file (codesign --deep,"
    echo "  ditto, lipo, a quarantine/xattr pass, a git filter, a toolchain update),"
    echo "  or restore the artifact:  git checkout -- packaging/$HELPER_NAME"
    echo "  A deliberate rebuild is a separate, documented event — see PROVENANCE.md"
    echo "  (it costs every user a trip to System Settings, so it needs"
    echo "  requires_fda_regrant=true in the release notes)."
    echo "==============================================================================="
    echo ""
  } >&2
  exit 1
}

# guard_helper_bytes <file> <border-label>
# Release-blocking check that <file> is the frozen helper, byte for byte.
guard_helper_bytes() {
  local file="$1" border="$2" golden got
  golden="$(helper_golden_sha)" || exit 1

  if [[ ! -f "$file" ]]; then
    _helper_guard_die "$border" "$file" "(file missing)" "$golden" \
      "the frozen helper is not present at this border at all"
  fi
  got="$(helper_sha256 "$file")"
  if [[ "$got" != "$golden" ]]; then
    _helper_guard_die "$border" "$file" "$got" "$golden"
  fi
  echo "    [helper-guard] border '$border': ok — sha256 $got"
}

# guard_installer_constant <installer.sh> <where>
# The four borders all measure the same golden value read from PROVENANCE.md, so
# they can be green from end to end while packaging/installer.sh carries a
# DIFFERENT EXPECTED_HELPER_SHA256 — and then the app that just built perfectly
# refuses to install the agent on the user's machine ("REFUSING — source helper
# does not match the frozen artifact"). Nothing else in the pipeline can see that,
# because both sides are internally consistent.
#
# Tolerant to ABSENCE, strict to DISAGREEMENT: the installer is developed in
# parallel, so a missing constant is reported and skipped rather than failing a
# build over someone else's work-in-progress. A constant that exists and does not
# match the golden is a hard stop.
guard_installer_constant() {
  local file="$1" where="$2" golden raw hits n
  golden="$(helper_golden_sha)" || exit 1

  if [[ ! -f "$file" ]]; then
    echo "    [helper-guard] installer constant: SKIPPED — no installer.sh at $file"
    return 0
  fi
  raw="$(grep -E '^[[:space:]]*EXPECTED_HELPER_SHA256=' "$file" || true)"
  if [[ -z "$raw" ]]; then
    echo "    [helper-guard] installer constant: SKIPPED — $where installer.sh does not define"
    echo "                   EXPECTED_HELPER_SHA256 (yet). Nothing cross-checked here."
    return 0
  fi

  hits="$(printf '%s\n' "$raw" | grep -Eo '[0-9a-f]{64}' | sort -u)"
  n="$(printf '%s\n' "$hits" | grep -c . || true)"
  if [[ "$n" -ne 1 ]]; then
    {
      echo ""
      echo "==============================================================================="
      echo "BUILD FAILED — installer.sh declares EXPECTED_HELPER_SHA256, but it cannot be read"
      echo "==============================================================================="
      echo "  file:  $file  ($where)"
      echo "  found $n literal 64-hex value(s) on its assignment line(s); exactly 1 is required."
      echo ""
      echo "  The build will not ship an installer whose idea of the frozen helper it could"
      echo "  not verify. If the constant became computed rather than literal, teach"
      echo "  build/helper-guard.sh how to read it — do not drop the cross-check."
      echo "==============================================================================="
      echo ""
    } >&2
    exit 1
  fi

  if [[ "$hits" != "$golden" ]]; then
    {
      echo ""
      echo "==============================================================================="
      echo "BUILD FAILED — installer.sh and PROVENANCE.md disagree about the frozen helper"
      echo "==============================================================================="
      echo "  installer.sh ($where): $hits"
      echo "  PROVENANCE.md (golden): $golden"
      echo ""
      echo "  What this means: every byte guard in this build would pass, the DMG would"
      echo "  look perfect, and then the shipped installer would REFUSE to install the"
      echo "  agent on every user's machine — because it is checking the helper against a"
      echo "  hash the helper does not have. A green build that cannot install is worse"
      echo "  than a red one."
      echo ""
      echo "  Fix whichever side is stale. PROVENANCE.md is the source of truth for the"
      echo "  identity of the frozen artifact; packaging/installer.sh must quote it."
      echo "==============================================================================="
      echo ""
    } >&2
    exit 1
  fi
  echo "    [helper-guard] installer constant ($where): agrees with PROVENANCE — $hits"
}

# guard_bundle_signature <app> <what>
# RELEASE-BLOCKING signature check for the DMG scripts.
#
# `--deep` WITHOUT `--strict` is deliberate and is the right gauge for this
# bundle: it is ad-hoc signed, and when the repo lives in an iCloud/fileprovider
# tree the daemon re-stamps com.apple.FinderInfo onto the bundle's wrapper
# DIRECTORY asynchronously. That xattr sits on the wrapper, not on signed
# payload, so it makes `--strict` fail on a bundle that is perfectly fine
# (neighbor .patches/003). The retry loop plus clearing that one xattr right
# before each attempt is the same race guard build-app.sh uses when it signs.
#
# What is NOT tolerated is a real failure. This used to print a WARNING and
# package the image anyway — and a bundle with a mutated helper fails exactly
# this check, so the one signal that something had rewritten the payload was
# being talked past on its way to the user.
guard_bundle_signature() {
  local app="$1" what="${2:-bundle}" attempt out
  for attempt in 1 2 3 4 5; do
    # Wrapper-directory xattr only — never touches signed payload.
    xattr -d com.apple.FinderInfo "$app" 2>/dev/null || true
    if codesign --verify --deep "$app" >/dev/null 2>&1; then
      echo "    [helper-guard] $what signature verifies (codesign --verify --deep, attempt $attempt)"
      return 0
    fi
    sleep 1
  done
  out="$(codesign --verify --deep --verbose=2 "$app" 2>&1 || true)"
  {
    echo ""
    echo "==============================================================================="
    echo "BUILD FAILED — $what does not pass 'codesign --verify --deep'"
    echo "==============================================================================="
    echo "  bundle: $app"
    echo ""
    echo "$out" | sed 's/^/    /'
    echo ""
    echo "  Tried 5 times, clearing com.apple.FinderInfo before each attempt, so this is"
    echo "  not the iCloud/fileprovider xattr race — the signature is genuinely broken."
    echo ""
    echo "  This is refused, not warned about: a bundle whose payload was modified after"
    echo "  signing fails exactly here, and the frozen helper is payload. Shipping it"
    echo "  risks both a Gatekeeper rejection and a silently dead folder-access grant."
    echo "  Rebuild with build/build-app.sh; do not repackage a bundle that fails this."
    echo "==============================================================================="
    echo ""
  } >&2
  exit 1
}

# guard_image_verify <dmg>
# RELEASE-BLOCKING `hdiutil verify`.
#
# No retry: unlike codesign, this reads the finished image file and is not
# exposed to the fileprovider FinderInfo race — a failure here is a real defect
# in the image (bad checksum, truncated write, corrupt UDIF), not a flake.
#
# This used to warn and carry on, which meant a malformed image still got its
# .sha256 written next to it and looked exactly like a release. Border 4 covers
# only the catastrophic case (an image so broken it will not mount); an image
# that verify rejects but that still mounts used to sail through everything.
guard_image_verify() {
  local dmg="$1" out
  if out="$(hdiutil verify "$dmg" 2>&1)"; then
    echo "    [helper-guard] image verifies (hdiutil verify)"
    return 0
  fi
  {
    echo ""
    echo "==============================================================================="
    echo "BUILD FAILED — 'hdiutil verify' rejected the image"
    echo "==============================================================================="
    echo "  image: $dmg"
    echo ""
    echo "$out" | sed 's/^/    /'
    echo ""
    echo "  A malformed image is not shipped and not left on disk to be picked up later"
    echo "  by mistake. Rebuild; if it fails again the defect is upstream (disk full,"
    echo "  a source file changing under dmgbuild/hdiutil, a failing volume)."
    echo "==============================================================================="
    echo ""
  } >&2
  exit 1
}

# guard_helper_in_dmg <dmg> <app-name> [border-label]
# BORDER 4. Mounts the finished image read-only on a private mount point, copies
# the helper out, unmounts, and only then compares. Extract-then-unmount-then-
# compare is deliberate: the image is never left mounted on a failure path.
#
# -mountrandom under TMPDIR (not /Volumes) also sidesteps this project's known
# DMG trap — a stale volume of the same name still mounted from an earlier test
# makes name-based mounting unreliable.
guard_helper_in_dmg() {
  local dmg="$1" app_name="$2" border="${3:-mounted DMG}"
  local golden mnt_parent mount_point extracted got detached
  golden="$(helper_golden_sha)" || exit 1

  if [[ ! -f "$dmg" ]]; then
    _helper_guard_die "$border" "$dmg" "(image missing)" "$golden" \
      "the DMG to verify does not exist"
  fi

  mnt_parent="$(mktemp -d "${TMPDIR:-/tmp}/mp3tom4b-dmgcheck.XXXXXX")"
  extracted="$mnt_parent-helper.bin"

  echo "    [helper-guard] mounting $(basename "$dmg") read-only to inspect the shipped bundle"
  if ! hdiutil attach "$dmg" -nobrowse -readonly -noverify -noautoopen \
       -mountrandom "$mnt_parent" >/dev/null 2>&1; then
    rm -rf "$mnt_parent"
    _helper_guard_die "$border" "$dmg" "(could not mount)" "$golden" \
      "hdiutil attach failed — the shipped image could not be inspected, so it cannot be approved"
  fi

  # -mountrandom creates exactly one directory under $mnt_parent.
  mount_point="$(find "$mnt_parent" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | head -1)"

  local in_dmg=""
  if [[ -n "$mount_point" ]]; then
    in_dmg="$mount_point/$app_name.app/$HELPER_BUNDLE_RELPATH"
    [[ -f "$in_dmg" ]] && cp "$in_dmg" "$extracted" 2>/dev/null || true
  fi

  # Always unmount before judging, retrying a busy volume (Spotlight/Finder can
  # hold it for a moment) and forcing as a last resort.
  detached=0
  if [[ -n "$mount_point" ]]; then
    for _ in 1 2 3 4 5; do
      if hdiutil detach "$mount_point" >/dev/null 2>&1; then detached=1; break; fi
      sleep 1
    done
    [[ "$detached" -eq 1 ]] || hdiutil detach "$mount_point" -force >/dev/null 2>&1 || true
  fi
  rm -rf "$mnt_parent"

  if [[ -z "$mount_point" ]]; then
    rm -f "$extracted"
    _helper_guard_die "$border" "$dmg" "(no mount point)" "$golden" \
      "the image mounted but no volume appeared under $mnt_parent"
  fi
  if [[ ! -f "$extracted" ]]; then
    _helper_guard_die "$border" "$dmg" "(helper missing in image)" "$golden" \
      "expected it at <volume>/$app_name.app/$HELPER_BUNDLE_RELPATH — the shipped app cannot install its agent at all"
  fi

  got="$(helper_sha256 "$extracted")"
  rm -f "$extracted"
  if [[ "$got" != "$golden" ]]; then
    _helper_guard_die "$border" "$dmg  ->  $app_name.app/$HELPER_BUNDLE_RELPATH" \
      "$got" "$golden" "read back out of the mounted final image — this is what users get"
  fi
  echo "    [helper-guard] border '$border': ok — sha256 $got"
}
