#!/bin/bash
# mp3-to-m4b installer (all install logic lives here).
#
# Cloned from the fb2-to-epub neighbor's proven installer and adapted for this
# project. Differences from fb2:
#   - detects ffmpeg + ffprobe (NOT Calibre); clear `brew install ffmpeg` message
#   - creates a project venv and `pip install Pillow` (cover.py's generation
#     guarantee, PRD G4) — fb2 had no venv step
#   - copies the python *package* agent/ (not standalone scripts) next to the
#     runner so `python3 -m agent` resolves (runner adds its dir to PYTHONPATH)
#   - WatchPaths = watch folder + queue/commands/ (a dropped command wakes the
#     agent; the folder wakes it on new books)
#   - launchd runs the venv python via the stable FDA runner (bin/runner.sh)
#
# Responsibilities:
#   - detect ffmpeg + ffprobe and python3 (clear message if missing)
#   - accept a WATCH_DIR (arg or env); create it if absent
#   - create a venv under App Support and install Pillow into it
#   - copy the agent/ package + the FDA runner into App Support/bin
#   - generate the LaunchAgent plist via `plutil` (NOT sed) so arbitrary paths
#     with spaces / unicode are encoded safely
#   - (re)load the agent idempotently: bootout -> bootstrap -> enable -> kickstart
#   - print Full Disk Access guidance when WATCH_DIR is in a TCC-protected zone
#
# Usage:
#   installer.sh ["/path/to/watch folder"]
#   WATCH_DIR="/path/to/folder" installer.sh
# Default WATCH_DIR: ~/Desktop/mp3-to-m4b
#
# Test/dev escape hatches (keep the real system untouched):
#   MP3TOM4B_SUPPORT_DIR  redirect the whole App Support tree to a scratch dir
#   MP3TOM4B_LABEL        override the LaunchAgent label (use a temp one in tests)
#   MP3TOM4B_NO_LAUNCHCTL=1  skip the launchd (re)load entirely (lint-only checks)
#   MP3TOM4B_NO_VENV=1    skip venv creation / Pillow install (faster dry checks)
#
# Invoked both by the .app applet (do shell script) and directly from a checkout.
# Idempotent: re-running re-points the agent without leaving dupes.

set -euo pipefail

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
LABEL="${MP3TOM4B_LABEL:-com.arrivarus.mp3tom4b.agent}"
# App Support root honors MP3TOM4B_SUPPORT_DIR (same override config.py reads),
# so tests can stage the whole tree into a scratch dir without touching the
# user's real data.
APP_SUPPORT="${MP3TOM4B_SUPPORT_DIR:-$HOME/Library/Application Support/mp3-to-m4b}"
BIN_DIR="$APP_SUPPORT/bin"
VENV_DIR="$APP_SUPPORT/venv"
COMMANDS_DIR="$APP_SUPPORT/queue/commands"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_FILE="$HOME/Library/Logs/mp3-to-m4b.log"
# launchd starts the agent with a minimal PATH; ffmpeg from Homebrew lives in
# /opt/homebrew/bin (Apple Silicon) or /usr/local/bin (Intel). Include both plus
# the system dirs so ffmpeg resolves even though the absolute paths are also
# exported below.
AGENT_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

RUNNER_DST="$BIN_DIR/runner.sh"
# The agent python package is copied verbatim to BIN_DIR/agent so it sits as a
# SIBLING of runner.sh (runner adds its own dir to PYTHONPATH -> `-m agent`).
AGENT_DST="$BIN_DIR/agent"

# Resolve where our source files live. Search order:
#   1) MP3TOM4B_SRC_DIR override (used by build/tests)
#   2) a sibling checkout layout (packaging/.. -> bin/runner.sh, agent/)
#   3) the .app Resources layout (runner.sh + agent/ next to this installer)
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Find the runner.sh source.
find_runner() {
  local c
  for c in \
    "${MP3TOM4B_SRC_DIR:-}/runner.sh" \
    "$SELF_DIR/runner.sh" \
    "$SELF_DIR/../bin/runner.sh" \
    "$SELF_DIR/bin/runner.sh"; do
    [[ -n "$c" && -f "$c" ]] && { printf '%s' "$c"; return 0; }
  done
  return 1
}

# Find the agent/ package source directory (must contain __main__.py).
find_agent_dir() {
  local c
  for c in \
    "${MP3TOM4B_SRC_DIR:-}/agent" \
    "$SELF_DIR/agent" \
    "$SELF_DIR/../agent" \
    "$SELF_DIR/bin/agent"; do
    [[ -n "$c" && -f "$c/__main__.py" ]] && { (cd "$c" && pwd); return 0; }
  done
  return 1
}

# ---------------------------------------------------------------------------
# 1. Detect ffmpeg + ffprobe (the engine) and python3
# ---------------------------------------------------------------------------
detect_tool() {
  # $1 = tool name. env override ($FFMPEG / $FFPROBE) -> Homebrew dirs -> PATH.
  local name="$1" envvar cand
  envvar="$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')"
  cand="$(eval "printf '%s' \"\${$envvar:-}\"")"
  if [[ -n "$cand" && -x "$cand" ]]; then printf '%s' "$cand"; return 0; fi
  for cand in "/opt/homebrew/bin/$name" "/usr/local/bin/$name"; do
    [[ -x "$cand" ]] && { printf '%s' "$cand"; return 0; }
  done
  cand="$(command -v "$name" 2>/dev/null || true)"
  [[ -n "$cand" && -x "$cand" ]] && { printf '%s' "$cand"; return 0; }
  return 1
}

if ! FFMPEG="$(detect_tool ffmpeg)"; then
  cat >&2 <<'EOF'
mp3-to-m4b: ffmpeg not found.

This app uses ffmpeg (and ffprobe) to turn folders of .mp3 into a single .m4b
audiobook. Install it first:

  brew install ffmpeg

(Homebrew: https://brew.sh — then run the line above.)
Then run this installer again.
EOF
  exit 1
fi

if ! FFPROBE="$(detect_tool ffprobe)"; then
  cat >&2 <<EOF
mp3-to-m4b: ffprobe not found.

ffprobe ships alongside ffmpeg. It is normally installed by:

  brew install ffmpeg

If you installed ffmpeg some other way, make sure ffprobe is on your PATH,
then run this installer again.

(ffmpeg was found at: $FFMPEG)
EOF
  exit 1
fi

detect_python3() {
  local cand
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    [[ -x "$cand" ]] && { printf '%s' "$cand"; return 0; }
  done
  cand="$(command -v python3 2>/dev/null || true)"
  [[ -n "$cand" ]] && { printf '%s' "$cand"; return 0; }
  return 1
}
if ! PYTHON3_SRC="$(detect_python3)"; then
  cat >&2 <<'EOF'
mp3-to-m4b: python3 not found.

Install the Xcode Command Line Tools (provides /usr/bin/python3):
  xcode-select --install

Then run this installer again.
EOF
  exit 1
fi

# ---------------------------------------------------------------------------
# 2. Resolve WATCH_DIR
# ---------------------------------------------------------------------------
WATCH_DIR="${1:-${WATCH_DIR:-$HOME/Desktop/mp3-to-m4b}}"
# Normalize a literal leading tilde from user input (it would not expand inside
# the quoted arg/env). We match the literal '~' on purpose, then expand via HOME.
# shellcheck disable=SC2088
case "$WATCH_DIR" in
  "~"|"~/"*) WATCH_DIR="$HOME/${WATCH_DIR#\~/}" ;;
esac
mkdir -p "$WATCH_DIR"
WATCH_DIR="$(cd "$WATCH_DIR" && pwd)"

# ---------------------------------------------------------------------------
# 3. Create the data-directory skeleton + copy engine into App Support/bin
# ---------------------------------------------------------------------------
# COMMANDS_DIR must EXIST before (re)load — launchd only watches paths that are
# present. The agent itself also ensures the full tree on launch, but the plist's
# WatchPaths needs it up front.
mkdir -p "$BIN_DIR" "$COMMANDS_DIR" "$(dirname "$PLIST")" "$(dirname "$LOG_FILE")"

src_runner="$(find_runner)" || { echo "mp3-to-m4b: missing runner.sh source" >&2; exit 1; }
src_agent_dir="$(find_agent_dir)" || { echo "mp3-to-m4b: missing agent/ package source (no __main__.py found)" >&2; exit 1; }

# runner.sh is the FDA-granted "responsible" target — the TCC grant is keyed to
# this file. On update, only (re)install it if missing or actually changed, so an
# idempotent re-run never churns the file and risks dropping the user's FDA grant.
if [[ ! -f "$RUNNER_DST" ]] || ! cmp -s "$src_runner" "$RUNNER_DST"; then
  install -m 0755 "$src_runner" "$RUNNER_DST"
fi

# Copy the python package verbatim (skip __pycache__ / *.pyc). Refresh fully so a
# reinstall never leaves a stale module behind.
rm -rf "$AGENT_DST"
mkdir -p "$AGENT_DST"
for f in "$src_agent_dir"/*.py; do
  [[ -e "$f" ]] || continue
  install -m 0644 "$f" "$AGENT_DST/$(basename "$f")"
done
[[ -f "$AGENT_DST/__main__.py" ]] || { echo "mp3-to-m4b: agent package copy failed (no __main__.py at destination)" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 4. Create the venv and install Pillow (cover generation guarantee, PRD G4)
# ---------------------------------------------------------------------------
# The production agent runs under the VENV python, which carries Pillow. urllib
# (web-cover fetch) is stdlib, so Pillow is the only third-party dependency.
# Pip is run offline-tolerant: a fresh venv has pip; we upgrade quietly and best
# effort, but only the Pillow install gates success.
VENV_PYTHON="$VENV_DIR/bin/python3"
if [[ "${MP3TOM4B_NO_VENV:-0}" != "1" ]]; then
  if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "mp3-to-m4b: creating venv at $VENV_DIR"
    "$PYTHON3_SRC" -m venv "$VENV_DIR"
  fi
  if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "mp3-to-m4b: venv python missing after creation ($VENV_PYTHON)" >&2
    exit 1
  fi
  # Quietly upgrade pip (best effort — don't fail the install if the index is
  # unreachable for pip-self-upgrade).
  "$VENV_PYTHON" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  # Pillow is required. If it's already importable (re-run), skip the install.
  if "$VENV_PYTHON" -c "import PIL" >/dev/null 2>&1; then
    echo "mp3-to-m4b: Pillow already present in venv"
  else
    echo "mp3-to-m4b: installing Pillow into venv"
    if ! "$VENV_PYTHON" -m pip install --quiet Pillow; then
      cat >&2 <<EOF
mp3-to-m4b: failed to install Pillow into the venv.

Pillow is needed to generate audiobook covers. Check your internet connection
and re-run this installer, or install it manually:

  "$VENV_PYTHON" -m pip install Pillow
EOF
      exit 1
    fi
  fi
  # Verify the import actually works (catches a half-built wheel).
  "$VENV_PYTHON" -c "import PIL; from PIL import Image, ImageDraw, ImageFont" \
    || { echo "mp3-to-m4b: Pillow installed but import failed" >&2; exit 1; }
  PYTHON3_FOR_AGENT="$VENV_PYTHON"
else
  echo "mp3-to-m4b: MP3TOM4B_NO_VENV=1 -> skipping venv/Pillow (using $PYTHON3_SRC)"
  PYTHON3_FOR_AGENT="$PYTHON3_SRC"
fi

# ---------------------------------------------------------------------------
# 5. Generate the LaunchAgent plist via plutil (safe for spaces/unicode)
#    Build a minimal valid skeleton, then insert/replace typed values whose
#    contents are passed as separate argv -> never spliced into XML.
# ---------------------------------------------------------------------------
gen_plist() {
  local out="$1"
  cat > "$out" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict/>
</plist>
PLIST

  plutil -replace Label -string "$LABEL" "$out"

  # ProgramArguments -> [ runner ]  (runner exec's `python3 -m agent`)
  plutil -replace ProgramArguments -json '[]' "$out"
  plutil -insert  ProgramArguments.0 -string "$RUNNER_DST" "$out"

  # WatchPaths -> [ WATCH_DIR, COMMANDS_DIR ]
  #   - WATCH_DIR fires the agent when a new book lands;
  #   - COMMANDS_DIR fires it when the app drops a confirm-build/cover command.
  # Both dirs MUST exist for launchd to watch them (created above).
  plutil -replace WatchPaths -json '[]' "$out"
  plutil -insert  WatchPaths.0 -string "$WATCH_DIR"    "$out"
  plutil -insert  WatchPaths.1 -string "$COMMANDS_DIR" "$out"

  # EnvironmentVariables. The runner inherits these; the agent reads MP3TOM4B_*.
  #   PYTHON3        -> the venv python (carries Pillow); runner prefers it
  #   MP3TOM4B_WATCH_DIR -> the folder scan.py watches
  #   FFMPEG/FFPROBE -> absolute engine paths (build_m4b/probe can use them)
  #   PATH           -> includes Homebrew so a bare `ffmpeg` also resolves
  plutil -replace EnvironmentVariables -json '{}' "$out"
  plutil -insert  EnvironmentVariables.PYTHON3            -string "$PYTHON3_FOR_AGENT" "$out"
  plutil -insert  EnvironmentVariables.MP3TOM4B_WATCH_DIR -string "$WATCH_DIR"         "$out"
  plutil -insert  EnvironmentVariables.FFMPEG             -string "$FFMPEG"            "$out"
  plutil -insert  EnvironmentVariables.FFPROBE            -string "$FFPROBE"           "$out"
  plutil -insert  EnvironmentVariables.PATH               -string "$AGENT_PATH"        "$out"

  # If the App Support tree is redirected (tests), pass the override through so
  # the agent writes into the same scratch tree the installer staged.
  if [[ -n "${MP3TOM4B_SUPPORT_DIR:-}" ]]; then
    plutil -insert EnvironmentVariables.MP3TOM4B_SUPPORT_DIR -string "$MP3TOM4B_SUPPORT_DIR" "$out"
  fi

  plutil -replace RunAtLoad        -bool true "$out"
  plutil -replace ThrottleInterval -integer 5 "$out"
  plutil -replace StandardOutPath  -string "$LOG_FILE" "$out"
  plutil -replace StandardErrorPath -string "$LOG_FILE" "$out"

  # Final sanity: must be a valid plist.
  plutil -lint "$out" >/dev/null
}

# Generate into a temp file, then atomically move it into place. mktemp gives a
# bare name; appending .plist keeps `plutil` happy. The trap cleans up on any
# early exit (set -e).
tmp_plist_base="$(mktemp -t mp3tom4bplist)"
tmp_plist="$tmp_plist_base.plist"
trap 'rm -f "$tmp_plist_base" "$tmp_plist"' EXIT
gen_plist "$tmp_plist"
mv -f "$tmp_plist" "$PLIST"
rm -f "$tmp_plist_base"
trap - EXIT

# ---------------------------------------------------------------------------
# 6. (Re)load the agent idempotently
# ---------------------------------------------------------------------------
if [[ "${MP3TOM4B_NO_LAUNCHCTL:-0}" == "1" ]]; then
  echo "mp3-to-m4b: MP3TOM4B_NO_LAUNCHCTL=1 -> skipping launchd (re)load"
else
  domain="gui/$(id -u)"
  # bootout is best-effort (agent may not be loaded yet); ignore its failure.
  launchctl bootout "$domain/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$domain" "$PLIST"
  launchctl enable "$domain/$LABEL" 2>/dev/null || true
  launchctl kickstart -k "$domain/$LABEL" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# 7. Full Disk Access guidance for TCC-protected zones
# ---------------------------------------------------------------------------
needs_fda=0
case "$WATCH_DIR/" in
  "$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Downloads/"*) needs_fda=1 ;;
esac

cat <<EOF
mp3-to-m4b installed.

  Watch folder: $WATCH_DIR
  Agent label:  $LABEL
  Runner:       $RUNNER_DST
  Agent:        $AGENT_DST
  Python:       $PYTHON3_FOR_AGENT
  ffmpeg:       $FFMPEG
  ffprobe:      $FFPROBE
  LaunchAgent:  $PLIST
  Log:          $LOG_FILE

Drop a folder of .mp3 files into the watch folder — the app will offer to build
it into a single .m4b audiobook.
EOF

if [[ "$needs_fda" -eq 1 ]]; then
  cat <<EOF

NOTE: Your watch folder is inside a macOS-protected location. If books are not
picked up, grant Full Disk Access to the runner:

  System Settings -> Privacy & Security -> Full Disk Access -> "+"
  Add: $RUNNER_DST
  (press Cmd-Shift-G in the picker and paste the path above)

Then toggle it on. The grant is keyed to that file and persists across updates.
EOF
fi
