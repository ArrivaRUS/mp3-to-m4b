#!/bin/bash
# Stable FDA "responsible target" for the mp3-to-m4b LaunchAgent.
#
# WHY THIS EXISTS (cloned from the fb2-to-epub neighbor's proven pattern):
#   macOS TCC attributes file-access permission (incl. Full Disk Access) to the
#   *executable named in ProgramArguments*, not to the interpreter it spawns. If
#   the LaunchAgent pointed ProgramArguments at /usr/bin/python3 directly, the
#   user would have to grant FDA to python3 itself (broad, and re-keyed on every
#   Python update). By giving the agent its own stable runner at a fixed App
#   Support path, Full Disk Access can be granted to THIS file specifically, and
#   the grant survives reinstalls as long as the path and bytes stay stable.
#
#   => Grant Full Disk Access to:
#      ~/Library/Application Support/mp3-to-m4b/bin/runner.sh
#
# It is intentionally minimal: resolve a python3, then `exec python3 -m agent`.
# The agent package is the real engine (single writer of state + ffmpeg). Env
# (PATH / MP3TOM4B_* / FFMPEG / PYTHON3) is inherited from the LaunchAgent's
# EnvironmentVariables. Keep this file stable (avoid churn) so the TCC grant holds.

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# python3 absolute path: env override (set by installer) -> common locations ->
# bare-PATH lookup. The agent starts with a minimal PATH, so we never rely on a
# login shell having resolved a custom interpreter. Prefer the project venv's
# python when present (it carries Pillow); fall back to a system interpreter.
PYTHON3="${PYTHON3:-}"
if [[ -z "$PYTHON3" || ! -x "$PYTHON3" ]]; then
  for cand in \
    "$HOME/Library/Application Support/mp3-to-m4b/venv/bin/python3" \
    /usr/bin/python3 \
    /opt/homebrew/bin/python3 \
    /usr/local/bin/python3; do
    if [[ -x "$cand" ]]; then PYTHON3="$cand"; break; fi
  done
fi
[[ -z "$PYTHON3" || ! -x "$PYTHON3" ]] && PYTHON3="$(command -v python3 2>/dev/null || true)"

if [[ -z "$PYTHON3" || ! -x "$PYTHON3" ]]; then
  echo "mp3-to-m4b: no usable python3 interpreter found" >&2
  exit 1
fi

# The `agent` package must be importable regardless of layout:
#   - bundled / installed: runner.sh and agent/ are SIBLINGS in the same dir
#     (Contents/Resources/runner.sh + Contents/Resources/agent/, or the staged
#     App Support copy) → agent is under "$HERE".
#   - dev checkout: bin/runner.sh with agent/ one level up at <repo>/agent/
#     → agent is under "$(dirname "$HERE")".
# Add BOTH to PYTHONPATH so `-m agent` resolves regardless of cwd or layout.
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$HERE:$(dirname "$HERE")"

exec "$PYTHON3" -m agent
