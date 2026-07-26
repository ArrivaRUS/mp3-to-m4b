#!/bin/bash
# t0-runner.sh — the shell layer INSIDE the T0 gate. Deployed by t0.sh as
# "$T0_ROOT/bin/runner.sh" (that literal name is baked into the frozen helper's
# bytes: it spawns `/bin/bash <dirname(self)>/runner.sh`).
#
# It is a deliberate twin of the shipping bin/runner.sh: same topology, same
# trap set, same RE-WAIT loop, same exit-code mirroring — the only difference is
# the payload (t0_probe.py instead of `-m agent`), because the gate must measure
# the FORM, not the product. The shipping runner is NOT reused and must NOT grow
# a test hook: a test override on the production path is how the sibling project
# once rewrote a live launchd plist.
#
# t0.sh checks the two files for structural parity and prints the result, so the
# twin cannot silently drift away from the original.
#
# It also records its own identity before doing anything else:
#   bash_pid   $$      — must DIFFER from the python pid (donor form, not exec)
#   bash_ppid  $PPID   — the helper when PA0 = the helper,
#                        launchd (pid 1) when PA0 = this script (negative control)

set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_ROOT="${T0_STATE_ROOT:?t0-runner: T0_STATE_ROOT not set}"
mkdir -p "$STATE_ROOT"

cat > "$STATE_ROOT/bash.json" <<JSON
{
  "bash_pid": $$,
  "bash_ppid": $PPID,
  "pa0_hint": "${T0_PA0_HINT:-unknown}",
  "here": "$HERE"
}
JSON

PYTHON3="${PYTHON3:-}"
if [[ -z "$PYTHON3" || ! -x "$PYTHON3" ]]; then
  for cand in /usr/bin/python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    [[ -x "$cand" ]] && { PYTHON3="$cand"; break; }
  done
fi
[[ -z "$PYTHON3" || ! -x "$PYTHON3" ]] && PYTHON3="$(command -v python3 2>/dev/null || true)"
if [[ -z "$PYTHON3" || ! -x "$PYTHON3" ]]; then
  echo "t0-runner: no usable python3 interpreter found" >&2
  exit 1
fi

# --- run the probe in the background and stay alive as its parent ------------
"$PYTHON3" "$HERE/t0_probe.py" &
CHILD=$!

forward_signal() {
  local sig="$1"
  if kill -0 "$CHILD" 2>/dev/null; then
    kill -s "$sig" "$CHILD" 2>/dev/null || true
  fi
}
trap 'forward_signal TERM' TERM
trap 'forward_signal INT'  INT
trap 'forward_signal HUP'  HUP

# THE RE-WAIT LOOP (same as bin/runner.sh): a trapped signal makes bash return
# from `wait` with >128 while the child is still alive; leaving now would drop
# the shell out of the chain ahead of the child.
status=0
while :; do
  wait "$CHILD"
  status=$?
  if (( status > 128 )) && kill -0 "$CHILD" 2>/dev/null; then
    continue
  fi
  break
done

exit "$status"
