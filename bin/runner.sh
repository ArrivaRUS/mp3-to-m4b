#!/bin/bash
# runner.sh — the mp3-to-m4b LaunchAgent's shell layer.
#
# ⚠️  THIS FILE IS NO LONGER THE FDA TARGET. It was demoted in release 1.0.
#   macOS Tahoe (26.x) attributes a launchd agent's TCC request to the Mach-O
#   IMAGE of the responsible process. For a shebang script that image is
#   /bin/bash (a platform binary → silent deny), so a Full Disk Access grant
#   given to THIS file never worked and could never work.
#   => The FDA target is now the frozen Mach-O helper:
#        ~/Library/Application Support/mp3-to-m4b/bin/mp3-to-m4b-agent
#      It is ProgramArguments[0]; it spawns `/bin/bash runner.sh` (its sibling)
#      and stays alive as the responsible parent.
#   Diagnosis: ../2026.06 fb2-to-epub/.patches/020-tahoe-fda-script-grant-dead-real-not-panel.md
#   Design:    arch/plan-binrunner-mp3-v2.md (Р1, M3)
#
# Because this file is no longer part of the grant identity, it is freely
# mutable: edit it in any release without costing anyone a re-grant. What it
# must NOT do is change its NAME (the frozen helper looks for the literal
# `runner.sh` next to itself) or its process TOPOLOGY.
#
# TOPOLOGY (load-bearing — do not "simplify" back to `exec`):
#   launchd → mp3-to-m4b-agent (helper, alive) → /bin/bash runner.sh (alive)
#           → python3 -m agent → ffmpeg
#   python runs in the BACKGROUND and we `wait` for it, forwarding TERM/INT/HUP,
#   so a `launchctl bootout` walks the whole chain down in order instead of
#   leaving an orphaned ffmpeg writing into a deleted temp dir. `exec python3`
#   would collapse bash out of the chain and remove the trap point.
#
# Env (PATH / MP3TOM4B_* / FFMPEG / FFPROBE / PYTHON3) is inherited from the
# LaunchAgent's EnvironmentVariables, through the helper, unchanged.

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

# --- run python in the background and stay alive as its parent ---------------
"$PYTHON3" -m agent &
CHILD=$!

# Forward the termination signals to python so ITS handler can stop ffmpeg and
# sweep its temp dir. `kill -0` guards against a race where the child is already
# gone. The handler must stay cheap and must not exit the shell.
forward_signal() {
  local sig="$1"
  if kill -0 "$CHILD" 2>/dev/null; then
    kill -s "$sig" "$CHILD" 2>/dev/null || true
  fi
}
trap 'forward_signal TERM' TERM
trap 'forward_signal INT'  INT
trap 'forward_signal HUP'  HUP

# THE RE-WAIT LOOP (load-bearing).
# When a trapped signal arrives while bash sits in `wait`, bash returns from
# `wait` IMMEDIATELY with a status > 128 and only then runs the trap — the child
# is still very much alive. Exiting here would drop runner.sh out of the chain
# ahead of python, and launchd would SIGKILL the remainder of the tree (ffmpeg
# included) a few seconds later. So: if the wait was interrupted and the child
# still exists, wait again.
# The one extra lap possible when the child is a not-yet-reaped zombie is
# harmless: the next `wait` reaps it and returns its real status.
status=0
while :; do
  wait "$CHILD"
  status=$?
  if (( status > 128 )) && kill -0 "$CHILD" 2>/dev/null; then
    continue
  fi
  break
done

# Mirror the child's fate: its exit code, or 128+signal when it died of one
# (`wait` already reports the latter in that form).
exit "$status"
