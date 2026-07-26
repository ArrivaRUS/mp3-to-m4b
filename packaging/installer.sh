#!/bin/bash
# mp3-to-m4b installer (all install logic lives here).
#
# Release 1.0 — transactional, golden-SHA-gated, generation-stamped.
# Design: arch/plan-binrunner-mp3-v2.md (M1) + …-v2-addendum.md.
#
# WHAT CHANGED IN 1.0 (and why)
#   · ProgramArguments[0] is the FROZEN Mach-O helper `mp3-to-m4b-agent`, ONE
#     element. macOS 26 (Tahoe) attributes a launchd job's TCC request to the
#     Mach-O IMAGE of PA0; for a shebang script that image is /bin/bash, so a
#     grant given to runner.sh is dead as a class (T0, PROVENANCE.md).
#   · The helper's identity is checked against an INDEPENDENT golden SHA-256
#     baked in below (B5): source before anything is written, destination after
#     it is installed. A src↔dst compare cannot catch a corrupted source — two
#     identically broken files are equal.
#   · The whole install runs under a cross-process lock and in a fixed order
#     (B4): refuse-if-building → long preflight → stage → validate → bootout →
#     replace → publish plist → bootstrap → verify → receipt. Any failure after
#     the first destructive step rolls the mutable parts back and does NOT write
#     the receipt.
#   · Each install stamps a fresh generation UUID into the plist env; the agent
#     copies it into state.json. A correct plist on disk is NOT proof launchd is
#     running it (B3) — after bootstrap we ask `launchctl print` what PA0 the
#     job actually has, and only then write the receipt (LAST).
#   · `--repair-launchd-only` is a strictly OFFLINE mode (B2): verify the
#     installed files + golden SHA, re-bake the plist, reload, verify, receipt.
#     No engine detection, no venv, no pip — nothing that can touch the network.
#   · StartInterval = 300 s (Р4) as safety-reconciliation; WatchPaths stays the
#     fast path.
#   · Rollback NEVER re-points PA0 back at runner.sh (Р5) — that is precisely
#     the construction 1.0 exists to remove.
#
# Responsibilities:
#   - detect ffmpeg + ffprobe and python3 (clear message if missing)
#   - accept a WATCH_DIR (arg or env); create it if absent
#   - create a venv under App Support and install Pillow into it
#   - install the frozen helper + runner.sh + the agent/ package into
#     App Support/bin
#   - generate the LaunchAgent plist via `plutil` (NOT sed) so arbitrary paths
#     with spaces / unicode are encoded safely
#   - (re)load the agent idempotently and PROVE the reload took
#   - write install-receipt.json last (the app's proof-of-generation)
#
# Usage:
#   installer.sh ["/path/to/watch folder"]
#   installer.sh --repair-launchd-only ["/path/to/watch folder"]
#   WATCH_DIR="/path/to/folder" installer.sh
# Default WATCH_DIR: ~/Desktop/mp3-to-m4b
#
# Test/dev escape hatches — ALL of them are behind the test latch below.
#   MP3TOM4B_TEST_MODE=1 + MP3TOM4B_TEST_ROOT=<dir>   arm the latch
#   MP3TOM4B_SUPPORT_DIR      redirect the App Support tree (MUTATING)
#   MP3TOM4B_LABEL            override the LaunchAgent label (MUTATING)
#   MP3TOM4B_LAUNCHAGENTS_DIR redirect where the plist is written (MUTATING)
#   MP3TOM4B_NO_LAUNCHCTL=1   skip the launchd (re)load
#   MP3TOM4B_NO_VENV=1        skip venv creation / Pillow install
#   MP3TOM4B_TEST_HOOK=<name> fault injection for the rollback/golden tests
#
# Invoked both by the .app applet (do shell script) and directly from a checkout.
# Idempotent: re-running re-points the agent without leaving dupes.

set -euo pipefail
shopt -s nullglob

# ---------------------------------------------------------------------------
# 0. Arguments
# ---------------------------------------------------------------------------
MODE="full"          # full | repair
ARG_WATCH=""
# Capture the env-provided inputs BEFORE anything shadows them: section 5 declares
# the plist variables (WATCH_DIR / FFMPEG / FFPROBE) and would otherwise eat them.
ENV_WATCH_DIR="${WATCH_DIR:-}"
ENV_FFMPEG="${FFMPEG:-}"
ENV_FFPROBE="${FFPROBE:-}"
for _a in ${1+"$@"}; do
  case "$_a" in
    --repair-launchd-only) MODE="repair" ;;
    -h|--help)
      sed -n '1,60p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    "") ;;   # the app may pass an empty watch-dir argument — ignore it
    -*) echo "mp3-to-m4b: unknown option: $_a" >&2; exit 2 ;;
    *)  [[ -z "$ARG_WATCH" ]] && ARG_WATCH="$_a" ;;
  esac
done

# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
die()  { echo "mp3-to-m4b: $*" >&2; exit 1; }
note() { echo "mp3-to-m4b: $*"; }

# Canonical absolute path for a path that may not exist yet: resolve the deepest
# EXISTING ancestor physically (pwd -P) and re-append the missing tail.
canon_path() {
  local p="${1:-}" suffix="" base root
  [[ -n "$p" ]] || return 1
  [[ "$p" != /* ]] && p="$PWD/$p"
  while [[ ! -d "$p" ]]; do
    base="$(basename "$p")"
    p="$(dirname "$p")"
    suffix="/$base$suffix"
    [[ "$p" == "/" ]] && break
  done
  root="$(cd "$p" && pwd -P)" || return 1
  if [[ -z "$suffix" ]]; then
    printf '%s' "$root"          # the path itself exists → its physical form
  else
    [[ "$root" == "/" ]] && root=""   # avoid "//tail"
    printf '%s' "${root}${suffix}"
  fi
}

# is_inside <child> <root>: equal to root, or below it on a segment boundary.
is_inside() {
  case "$1" in "$2"|"$2"/*) return 0 ;; *) return 1 ;; esac
}

sha256_of() { shasum -a 256 "$1" | cut -d' ' -f1; }

# a >= b for dotted numeric versions ("1.0.1" >= "1.0"). Non-numeric junk in a
# field is stripped; missing fields count as 0.
ver_ge() {
  awk -v a="$1" -v b="$2" 'BEGIN{
    na=split(a,A,"."); nb=split(b,B,".");
    n=(na>nb?na:nb);
    for(i=1;i<=n;i++){
      x=A[i]; y=B[i]; gsub(/[^0-9].*$/,"",x); gsub(/[^0-9].*$/,"",y);
      x=(x==""?0:x+0); y=(y==""?0:y+0);
      if(x>y) exit 0;
      if(x<y) exit 1;
    }
    exit 0
  }'
}

# ---------------------------------------------------------------------------
# 1. Test latch (lesson: neighbor .patches/015)
#
# A verify-override once leaked into the real installer and REWROTE the human's
# production LaunchAgent. Here every test hatch is two-stage:
#
#   • the latch is ARMED  ⇔ MP3TOM4B_TEST_MODE=1 AND MP3TOM4B_TEST_ROOT is an
#     existing directory that is neither "/" nor an ancestor of the real passwd
#     home (a too-wide TEST_ROOT would make production paths "inside" it);
#   • mutation is ALLOWED ⇔ the latch is armed AND the install root we are about
#     to write (i.e. the requested MP3TOM4B_SUPPORT_DIR) — and the LaunchAgents
#     dir, when overridden — live INSIDE that canonical TEST_ROOT.
#
# Two classes of hatch, two different failure modes, both fail-closed:
#   · REDIRECTING (SUPPORT_DIR / LABEL / LAUNCHAGENTS_DIR) — honored only under
#     the latch; set without it the installer REFUSES to run. Silently ignoring
#     them would be worse here than in the neighbor: ignoring SUPPORT_DIR means
#     a test aimed at a scratch tree would hit the production one instead.
#   · WORK-SKIPPING (NO_VENV / NO_LAUNCHCTL) and fault injection (TEST_HOOK) —
#     honored only under the latch, otherwise ignored, which always errs toward
#     doing the FULL real work.
#
# Swift twin: none needed — the app never sets these in production.
# ---------------------------------------------------------------------------
mp3_test_root() {
  [[ "${MP3TOM4B_TEST_MODE:-}" == "1" ]] || return 1
  local raw="${MP3TOM4B_TEST_ROOT:-}" root uname phome
  [[ -n "$raw" && -d "$raw" ]] || return 1
  root="$(cd "$raw" && pwd -P)" || return 1
  [[ "$root" == "/" ]] && return 1
  uname="$(id -un 2>/dev/null || true)"
  if [[ -n "$uname" ]]; then
    phome="$(dscl . -read "/Users/$uname" NFSHomeDirectory 2>/dev/null | sed 's/^NFSHomeDirectory: //')"
    [[ -z "$phome" ]] && phome="$(eval echo "~$uname" 2>/dev/null || true)"
    if [[ -n "$phome" && -d "$phome" ]]; then
      phome="$(cd "$phome" && pwd -P)"
      is_inside "$phome" "$root" && return 1
    fi
  fi
  printf '%s' "$root"
}
LATCH_ROOT="$(mp3_test_root || true)"

# True ⇔ every redirected path we were asked to use is inside the canonical
# TEST_ROOT. Uses the REQUESTED values (not the resolved constants) on purpose:
# the whole point is to judge the override before it takes effect.
latch_allows_mutation() {
  [[ -n "$LATCH_ROOT" ]] || return 1
  local want_support want_la
  want_support="$(canon_path "${MP3TOM4B_SUPPORT_DIR:-$HOME/Library/Application Support/mp3-to-m4b}")" || return 1
  is_inside "$want_support" "$LATCH_ROOT" || return 1
  if [[ -n "${MP3TOM4B_LAUNCHAGENTS_DIR:-}" ]]; then
    want_la="$(canon_path "$MP3TOM4B_LAUNCHAGENTS_DIR")" || return 1
    is_inside "$want_la" "$LATCH_ROOT" || return 1
  fi
  return 0
}

# Refuse (loudly, before any write) when a REDIRECTING override is set without a
# properly armed latch. This is the whole point of the latch: one forgotten env
# var must never be able to rewrite the production job.
guard_test_overrides() {
  # A newline-joined string, not an array: /bin/bash on macOS is 3.2, where an
  # empty array under `set -u` is an "unbound variable".
  local set_vars=""
  [[ -n "${MP3TOM4B_SUPPORT_DIR:-}" ]]      && set_vars="$set_vars  - MP3TOM4B_SUPPORT_DIR"$'\n'
  [[ -n "${MP3TOM4B_LABEL:-}" ]]            && set_vars="$set_vars  - MP3TOM4B_LABEL"$'\n'
  [[ -n "${MP3TOM4B_LAUNCHAGENTS_DIR:-}" ]] && set_vars="$set_vars  - MP3TOM4B_LAUNCHAGENTS_DIR"$'\n'
  [[ -z "$set_vars" ]] && return 0
  latch_allows_mutation && return 0
  {
    echo "mp3-to-m4b: refusing to run — test override(s) set without an armed test latch:"
    printf '%s' "$set_vars"
    echo
    echo "These variables redirect WHERE this installer writes (App Support tree,"
    echo "LaunchAgent label, plist directory). Honoring them unchecked is how a test"
    echo "run overwrites the real background agent, so they only take effect when:"
    echo "  MP3TOM4B_TEST_MODE=1"
    echo "  MP3TOM4B_TEST_ROOT=<existing dir, not '/' and not above your home>"
    echo "  and every redirected path is INSIDE that MP3TOM4B_TEST_ROOT."
    echo
    echo "If you did not mean to run a test, unset them and run the installer again."
  } >&2
  exit 1
}
guard_test_overrides   # [guard:latch]

# Work-skipping hatches: only under the latch, otherwise the full real work.
NO_LAUNCHCTL=0
NO_VENV=0
if latch_allows_mutation; then
  [[ "${MP3TOM4B_NO_LAUNCHCTL:-0}" == "1" ]] && NO_LAUNCHCTL=1
  [[ "${MP3TOM4B_NO_VENV:-0}" == "1" ]] && NO_VENV=1
fi

# Fault injection for the self-checks (rollback / corrupted destination). Never
# armed outside the latch. Called at named steps; a matching name aborts or
# corrupts exactly there so the guard under test has something to catch.
test_hook() {
  latch_allows_mutation || return 0
  [[ "${MP3TOM4B_TEST_HOOK:-}" == "$1" ]] || return 0
  case "$1" in
    corrupt-dst-after-install)
      printf 'x' >> "$AGENT_BIN_DST" ;;
    fail-after-publish-plist|fail-after-bootstrap|fail-after-replace)
      die "TEST_HOOK: forced failure at $1" ;;
    *) die "TEST_HOOK: unknown hook $1" ;;
  esac
}

# ---------------------------------------------------------------------------
# 2. Constants
# ---------------------------------------------------------------------------
# The golden identity of the frozen helper (packaging/agent-src/PROVENANCE.md).
# INDEPENDENT of whatever is on disk: this is what makes B5 a real gate.
EXPECTED_HELPER_SHA256="791d020d42477755fe3c46070699421280c2dd7e5f248da59f3f826a5bdbc079"
HELPER_NAME="mp3-to-m4b-agent"

LABEL="${MP3TOM4B_LABEL:-com.arrivarus.mp3tom4b.agent}"
APP_SUPPORT="${MP3TOM4B_SUPPORT_DIR:-$HOME/Library/Application Support/mp3-to-m4b}"
BIN_DIR="$APP_SUPPORT/bin"
VENV_DIR="$APP_SUPPORT/venv"
COMMANDS_DIR="$APP_SUPPORT/queue/commands"
BOOKS_DIR="$APP_SUPPORT/queue/books"
STATE_JSON="$APP_SUPPORT/state/state.json"
LAUNCH_AGENTS_DIR="${MP3TOM4B_LAUNCHAGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST="$LAUNCH_AGENTS_DIR/$LABEL.plist"
# Under the latch the log goes into the scratch tree too, so a test never writes
# into the human's ~/Library/Logs.
if latch_allows_mutation; then
  LOG_FILE="$APP_SUPPORT/logs/mp3-to-m4b.log"
else
  LOG_FILE="$HOME/Library/Logs/mp3-to-m4b.log"
fi
# launchd starts the agent with a minimal PATH; ffmpeg from Homebrew lives in
# /opt/homebrew/bin (Apple Silicon) or /usr/local/bin (Intel). Include both plus
# the system dirs so ffmpeg resolves even though the absolute paths are also
# exported below.
AGENT_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# ProgramArguments[0] — the frozen Mach-O helper, the file the user grants
# access to. Its PATH is part of the grant identity: never move it.
AGENT_BIN_DST="$BIN_DIR/$HELPER_NAME"
# runner.sh is the helper's sibling by a contract baked into the frozen bytes
# (the helper spawns `/bin/bash <dirname(self)>/runner.sh`). Freely mutable
# content, frozen NAME.
RUNNER_DST="$BIN_DIR/runner.sh"
# The agent python package sits next to runner.sh (runner adds its own dir to
# PYTHONPATH → `-m agent` resolves).
AGENT_DST="$BIN_DIR/agent"

# Proof-of-install, written LAST. Lives in the App Support ROOT (not state/) so
# writing it never wakes the app's state-file watcher.
RECEIPT="$APP_SUPPORT/install-receipt.json"

LOCK_DIR="$APP_SUPPORT/.install.lock"
STAGE_DIR="$APP_SUPPORT/.install.stage"
BACKUP_DIR="$APP_SUPPORT/.install.backup"

START_INTERVAL=300     # Р4 — safety reconciliation; WatchPaths stays the fast path
THROTTLE_INTERVAL=5    # must not suppress a restart after a crash (addendum §4.4)

DOMAIN="gui/$(id -u)"

# Resolve where our source files live. Search order:
#   1) MP3TOM4B_SRC_DIR override (used by build/tests — read-only, no redirect)
#   2) a sibling checkout layout (packaging/.. -> bin/runner.sh, agent/)
#   3) the .app Resources layout (helper + runner.sh + agent/ next to this file)
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

# The frozen helper: in a checkout it sits in packaging/ (= SELF_DIR); inside the
# .app it sits in Contents/Resources/ (= SELF_DIR).
find_agent_bin() {
  local c
  for c in \
    "${MP3TOM4B_SRC_DIR:-}/$HELPER_NAME" \
    "$SELF_DIR/$HELPER_NAME" \
    "$SELF_DIR/../packaging/$HELPER_NAME" \
    "$SELF_DIR/bin/$HELPER_NAME"; do
    [[ -n "$c" && -f "$c" ]] && { printf '%s' "$c"; return 0; }
  done
  return 1
}

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

# The version this installer SHIPS (for the receipt + the downgrade guard).
# Inside the .app: Contents/Resources/installer.sh → ../Info.plist. From a
# checkout there is no bundle → unknown ("" disables the downgrade guard).
bundled_version() {
  local v="${MP3TOM4B_VERSION:-}"
  [[ -n "$v" ]] && { printf '%s' "$v"; return 0; }
  local info="$SELF_DIR/../Info.plist"
  if [[ -f "$info" ]]; then
    v="$(plutil -extract CFBundleShortVersionString raw -o - "$info" 2>/dev/null || true)"
    [[ -n "$v" ]] && { printf '%s' "$v"; return 0; }
  fi
  printf ''
}
BUNDLED_VERSION="$(bundled_version)"

# ---------------------------------------------------------------------------
# 3. Guards
# ---------------------------------------------------------------------------
# B5 — independent golden SHA. Called on the SOURCE before anything is written
# and on the DESTINATION after it is installed. A src↔dst compare is kept too,
# but only as a copy-quality check: it cannot see a corrupted source.
guard_golden_sha() {
  local file="$1" what="$2" got
  [[ -f "$file" ]] || die "$what helper missing at $file"
  got="$(sha256_of "$file")"
  if [[ "$got" != "$EXPECTED_HELPER_SHA256" ]]; then
    {
      echo "mp3-to-m4b: REFUSING — $what helper does not match the frozen artifact."
      echo "  file:     $file"
      echo "  sha256:   $got"
      echo "  expected: $EXPECTED_HELPER_SHA256"
      echo
      echo "The user's folder-access grant is pinned to these exact bytes at this exact"
      echo "path. Installing anything else would silently kill it. Re-download the app."
    } >&2
    exit 1
  fi
}

# m2 — no symlink may stand anywhere on the helper's path: the grant is keyed to
# a real path, and a redirected one is a different file to TCC.
guard_no_symlinks() {
  local p canon
  for p in "$APP_SUPPORT" "$BIN_DIR" "$AGENT_BIN_DST" "$RUNNER_DST" "$AGENT_DST"; do
    [[ -L "$p" ]] && die "refusing: $p is a symlink (the access grant is keyed to a real path)"
  done
  # In production the helper's path must be PHYSICALLY real: TCC keys the grant to
  # the resolved path, so a plist carrying a path that resolves elsewhere would
  # never match the subject macOS attributes. Skipped under the test latch, where
  # the scratch root legitimately lives under /var → /private/var.
  if ! latch_allows_mutation; then
    canon="$(canon_path "$AGENT_BIN_DST")"
    [[ "$canon" == "$AGENT_BIN_DST" ]] || \
      die "refusing: $AGENT_BIN_DST resolves to $canon — the access grant is keyed to the resolved path"
  fi
  return 0
}

# B4 — one installer at a time. mkdir is atomic; a lock whose owner is gone is
# taken over once.
LOCK_HELD=0
acquire_install_lock() {
  mkdir -p "$APP_SUPPORT"
  local owner
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    owner="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
    if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
      die "another install is already running (pid $owner) — try again when it finishes"
    fi
    # Stale lock (owner gone / no pid file): take it over exactly once.
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" 2>/dev/null || die "cannot acquire the install lock at $LOCK_DIR"
    note "took over a stale install lock (owner pid: ${owner:-unknown})"
  fi
  echo "$$" > "$LOCK_DIR/pid"
  LOCK_HELD=1
}
release_install_lock() {
  [[ "$LOCK_HELD" == "1" ]] || return 0
  rm -rf "$LOCK_DIR" 2>/dev/null || true
  LOCK_HELD=0
}

# B4 — never tear the engine out from under a running build.
guard_no_build_in_progress() {
  local f status pid
  [[ -d "$BOOKS_DIR" ]] || return 0
  for f in "$BOOKS_DIR"/*.json; do
    status="$(plutil -extract status raw -o - "$f" 2>/dev/null || true)"
    [[ "$status" == "converting" ]] || continue
    pid="$(plutil -extract build.pid raw -o - "$f" 2>/dev/null || true)"
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    if kill -0 "$pid" 2>/dev/null; then
      die "a book is being built right now (pid $pid) — the update was not applied. Try again when the build finishes."
    fi
  done
  return 0
}

# M11f — an older bundle must not silently "update" a newer install.
guard_not_downgrade() {
  [[ -n "$BUNDLED_VERSION" ]] || return 0     # dev checkout: unknown → no opinion
  [[ -f "$RECEIPT" ]] || return 0
  local installed
  installed="$(plutil -extract engine_version raw -o - "$RECEIPT" 2>/dev/null || true)"
  [[ -n "$installed" ]] || return 0
  ver_ge "$BUNDLED_VERSION" "$installed" && return 0
  if [[ "${MP3TOM4B_ALLOW_DOWNGRADE:-0}" == "1" ]]; then
    note "downgrade $installed → $BUNDLED_VERSION allowed explicitly (MP3TOM4B_ALLOW_DOWNGRADE=1)"
    return 0
  fi
  die "refusing to downgrade: installed $installed is newer than this package ($BUNDLED_VERSION). Set MP3TOM4B_ALLOW_DOWNGRADE=1 to override."
}

# B3 — a correct plist on disk is NOT proof launchd runs it. Ask launchd.
verify_loaded_pa0() {
  [[ "$NO_LAUNCHCTL" == "1" ]] && { note "MP3TOM4B_NO_LAUNCHCTL=1 -> skipping the loaded-PA0 verification"; return 0; }
  local out args count first
  out="$(launchctl print "$DOMAIN/$LABEL" 2>&1)" || \
    die "the agent did not load (launchctl print $DOMAIN/$LABEL failed): ${out##*$'\n'}"
  # The `arguments = { … }` block lists ProgramArguments one per line.
  args="$(printf '%s\n' "$out" | awk '
    /^[[:space:]]*arguments[[:space:]]*=[[:space:]]*\{/ {inblk=1; next}
    inblk && /^[[:space:]]*\}/ {exit}
    inblk {sub(/^[[:space:]]+/,""); sub(/[[:space:]]+$/,""); print}
  ')"
  if [[ -z "$args" ]]; then
    # Some jobs print only `program = …` — accept that as the single argument.
    args="$(printf '%s\n' "$out" | awk -F' = ' '/^[[:space:]]*program[[:space:]]*=/ {print $2; exit}')"
  fi
  [[ -n "$args" ]] || die "could not read the loaded ProgramArguments from launchctl print"
  count="$(printf '%s\n' "$args" | grep -c .)"
  first="$(printf '%s\n' "$args" | head -1)"
  [[ "$first" == "$AGENT_BIN_DST" ]] || \
    die "launchd is running the WRONG ProgramArguments[0]: '$first' (expected '$AGENT_BIN_DST')"
  [[ "$count" == "1" ]] || \
    die "launchd job has $count ProgramArguments (expected exactly 1: the helper)"
}

# ---------------------------------------------------------------------------
# 4. Transaction bookkeeping (B4) — rollback + lock release on any exit
# ---------------------------------------------------------------------------
PHASE="safe"          # safe → danger → committed
BACKED_UP_PLIST=0
BACKED_UP_AGENT=0
BACKED_UP_RUNNER=0
PREV_PLIST_PA0=""

rollback() {
  echo "mp3-to-m4b: rolling back the failed install…" >&2

  # 1. the agent package (mutable)
  # Every step is best-effort on purpose: a rollback that aborts half-way would
  # also skip the lock release below and leave the tree worse than it found it.
  if [[ "$BACKED_UP_AGENT" == "1" && -d "$BACKUP_DIR/agent" ]]; then
    rm -rf "$AGENT_DST" 2>/dev/null || true
    mv "$BACKUP_DIR/agent" "$AGENT_DST" 2>/dev/null || true
  fi
  # 2. runner.sh (mutable)
  if [[ "$BACKED_UP_RUNNER" == "1" && -f "$BACKUP_DIR/runner.sh" ]]; then
    install -m 0755 "$BACKUP_DIR/runner.sh" "$RUNNER_DST" 2>/dev/null || true
  fi
  # 3. the helper is NEVER rolled back: its bytes are frozen and its path is
  #    part of the user's grant identity (Р5).
  # 4. the plist — restored ONLY if the previous one already pointed at the
  #    helper. Р5: a rollback must never re-create the Tahoe bug by pointing
  #    ProgramArguments[0] back at runner.sh.
  if [[ "$BACKED_UP_PLIST" == "1" && -f "$BACKUP_DIR/plist" ]]; then
    if [[ "$PREV_PLIST_PA0" == "$AGENT_BIN_DST" ]]; then
      cp -p "$BACKUP_DIR/plist" "$PLIST" 2>/dev/null || true
      if [[ "$NO_LAUNCHCTL" != "1" ]]; then
        launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
        launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || true
      fi
      echo "mp3-to-m4b: previous LaunchAgent restored." >&2
    else
      echo "mp3-to-m4b: the previous LaunchAgent pointed at '$PREV_PLIST_PA0', not the helper —" >&2
      echo "            it was NOT restored (that configuration cannot get folder access on" >&2
      echo "            macOS 26). The agent is left stopped; run the installer again, or" >&2
      echo "            'installer.sh --repair-launchd-only', to finish." >&2
      [[ "$NO_LAUNCHCTL" != "1" ]] && launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    fi
  elif [[ -f "$PLIST" ]]; then
    # We failed BEFORE replacing the plist, so the one on disk is still the
    # previous one — but we already booted the job out. Put it back exactly as we
    # found it, still subject to Р5 (never revive a runner.sh PA0).
    local cur_pa0
    cur_pa0="$(plutil -extract ProgramArguments.0 raw -o - "$PLIST" 2>/dev/null || true)"
    if [[ "$cur_pa0" == "$AGENT_BIN_DST" && "$NO_LAUNCHCTL" != "1" ]]; then
      launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || true
      echo "mp3-to-m4b: the previous LaunchAgent was restarted." >&2
    fi
  fi
  # 5. no receipt is written on a failed install — the app sees the generation
  #    mismatch and offers the repair surface (plan §6.3).
  echo "mp3-to-m4b: rollback done; the install receipt was NOT updated." >&2
}

on_exit() {
  local rc="$1"
  if (( rc != 0 )) && [[ "$PHASE" == "danger" ]]; then
    :          # rollback point (the line below is deleted by the mutation test)
    rollback   # [guard:rollback]
  fi
  rm -rf "$STAGE_DIR" 2>/dev/null || true
  [[ "$PHASE" == "committed" ]] && rm -rf "$BACKUP_DIR" 2>/dev/null
  release_install_lock
  return 0
}
trap 'on_exit $?' EXIT

# ---------------------------------------------------------------------------
# 5. plist generation (via plutil — safe for spaces/unicode)
# ---------------------------------------------------------------------------
# Values the plist carries. Filled by the full preflight or, in repair mode,
# read back from the existing receipt/plist (offline).
PYTHON3_FOR_AGENT=""
FFMPEG=""
FFPROBE=""
FFMPEG_VERSION=""
WATCH_DIR=""
INSTALL_GENERATION=""

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

  # ProgramArguments -> [ helper ] — EXACTLY ONE element (B3/T0). macOS attributes
  # the TCC request to the Mach-O image of PA0; the helper finds runner.sh by
  # itself (sibling contract baked into its frozen bytes). A second argument is
  # not merely redundant: it makes the loaded job differ from the one shape we
  # verify, which is how the neighbor ended up with a hand-repointed two-element
  # job in production.
  plutil -replace ProgramArguments -json '[]' "$out"
  plutil -insert  ProgramArguments.0 -string "$AGENT_BIN_DST" "$out"

  # WatchPaths -> [ WATCH_DIR, COMMANDS_DIR ]
  #   - WATCH_DIR fires the agent when a new book lands;
  #   - COMMANDS_DIR fires it when the app drops a command.
  # Both dirs MUST exist for launchd to watch them (created before we get here).
  plutil -replace WatchPaths -json '[]' "$out"
  plutil -insert  WatchPaths.0 -string "$WATCH_DIR"    "$out"
  plutil -insert  WatchPaths.1 -string "$COMMANDS_DIR" "$out"

  # EnvironmentVariables. The helper passes them through to runner.sh unchanged.
  #   PYTHON3                       -> the venv python (carries Pillow)
  #   MP3TOM4B_WATCH_DIR            -> the folder scan.py watches
  #   FFMPEG/FFPROBE                -> absolute engine paths
  #   FFMPEG_VERSION                -> pre-probed here so an idle tick does not
  #                                    spawn `ffmpeg -version` every 5 minutes (M6f)
  #   MP3TOM4B_INSTALL_GENERATION   -> this install's UUID; the agent copies it
  #                                    into state.json so the app can prove the
  #                                    RUNNING job is the one we just installed (B3)
  #   PATH                          -> includes Homebrew so a bare `ffmpeg` resolves
  plutil -replace EnvironmentVariables -json '{}' "$out"
  plutil -insert  EnvironmentVariables.PYTHON3            -string "$PYTHON3_FOR_AGENT" "$out"
  plutil -insert  EnvironmentVariables.MP3TOM4B_WATCH_DIR -string "$WATCH_DIR"         "$out"
  [[ -n "$FFMPEG" ]]  && plutil -insert EnvironmentVariables.FFMPEG  -string "$FFMPEG"  "$out"
  [[ -n "$FFPROBE" ]] && plutil -insert EnvironmentVariables.FFPROBE -string "$FFPROBE" "$out"
  [[ -n "$FFMPEG_VERSION" ]] && \
    plutil -insert EnvironmentVariables.FFMPEG_VERSION -string "$FFMPEG_VERSION" "$out"
  plutil -insert  EnvironmentVariables.MP3TOM4B_INSTALL_GENERATION -string "$INSTALL_GENERATION" "$out"
  plutil -insert  EnvironmentVariables.PATH               -string "$AGENT_PATH"        "$out"

  # ONLY under the test latch: pin the scratch tree into the plist. launchd does
  # not inherit the test's environment, so without this the job launchd starts
  # would write into the REAL App Support tree. Outside the latch this block is
  # skipped and the plist is byte-identical to a production one.
  if latch_allows_mutation; then
    plutil -insert EnvironmentVariables.MP3TOM4B_SUPPORT_DIR -string "$APP_SUPPORT" "$out"
  fi

  plutil -replace RunAtLoad        -bool true "$out"
  # Р4: safety reconciliation every 5 minutes. WatchPaths remains the fast path;
  # this only guarantees the agent eventually reconciles if a watch event is lost.
  plutil -replace StartInterval    -integer "$START_INTERVAL" "$out"
  plutil -replace ThrottleInterval -integer "$THROTTLE_INTERVAL" "$out"
  plutil -replace StandardOutPath  -string "$LOG_FILE" "$out"
  plutil -replace StandardErrorPath -string "$LOG_FILE" "$out"

  plutil -lint "$out" >/dev/null
}

publish_plist() {
  mkdir -p "$LAUNCH_AGENTS_DIR" "$(dirname "$LOG_FILE")"
  # Back up the previous plist (and remember its PA0 — Р5 needs it on rollback).
  if [[ -f "$PLIST" ]]; then
    mkdir -p "$BACKUP_DIR"
    cp -p "$PLIST" "$BACKUP_DIR/plist"
    PREV_PLIST_PA0="$(plutil -extract ProgramArguments.0 raw -o - "$PLIST" 2>/dev/null || true)"
    BACKED_UP_PLIST=1
  fi
  local tmp_base tmp
  tmp_base="$(mktemp -t mp3tom4bplist)"
  tmp="$tmp_base.plist"
  gen_plist "$tmp"
  mv -f "$tmp" "$PLIST"
  rm -f "$tmp_base"
}

reload_agent() {
  if [[ "$NO_LAUNCHCTL" == "1" ]]; then
    note "MP3TOM4B_NO_LAUNCHCTL=1 -> skipping launchd (re)load"
    return 0
  fi
  launchctl bootstrap "$DOMAIN" "$PLIST"
  launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true
}

stop_agent() {
  [[ "$NO_LAUNCHCTL" == "1" ]] && return 0
  # bootout is best-effort (the agent may not be loaded yet).
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
}

# Addendum §5.2 — an ungranted helper makes macOS ASK the user, and the answer
# arrives seconds later. Wait (softly) for the agent's first access probe so the
# app can show the right surface immediately instead of guessing. Never fatal.
wait_first_access_probe() {
  [[ "$NO_LAUNCHCTL" == "1" ]] && return 0
  local budget="${MP3TOM4B_ACCESS_WAIT_S:-10}" ts deadline
  [[ "$budget" =~ ^[0-9]+$ ]] || budget=10
  (( budget == 0 )) && return 0
  # Wall-clock budget (SECONDS), not an iteration count: each poll spawns plutil,
  # so counting laps would silently stretch a "10 s" wait well past 10 s.
  deadline=$((SECONDS + budget))
  while (( SECONDS < deadline )); do
    ts="$(plutil -extract agent.folder_access_ts raw -o - "$STATE_JSON" 2>/dev/null || true)"
    if [[ -n "$ts" ]]; then
      FIRST_ACCESS_TS="$ts"
      note "first access probe published at $ts"
      return 0
    fi
    sleep 0.2
  done
  note "the agent has not published an access probe yet (waited ${budget}s) — the app will keep watching"
  return 0
}
FIRST_ACCESS_TS=""

# The receipt is the LAST thing written (B3): its presence means every earlier
# step succeeded, including the launchd verification.
write_receipt() {
  local tmp="$APP_SUPPORT/.install-receipt.tmp.plist"
  cat > "$tmp" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict/>
</plist>
PLIST
  plutil -replace schema         -integer 1 "$tmp"
  plutil -replace generation     -string "$INSTALL_GENERATION" "$tmp"
  plutil -replace engine_version -string "$BUNDLED_VERSION" "$tmp"
  plutil -replace installed_at   -string "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$tmp"
  plutil -replace mode           -string "$MODE" "$tmp"
  plutil -replace label          -string "$LABEL" "$tmp"
  plutil -replace plist          -string "$PLIST" "$tmp"
  plutil -replace support_dir    -string "$APP_SUPPORT" "$tmp"
  plutil -replace watch_dir      -string "$WATCH_DIR" "$tmp"
  plutil -replace helper_path    -string "$AGENT_BIN_DST" "$tmp"
  plutil -replace helper_sha256  -string "$EXPECTED_HELPER_SHA256" "$tmp"
  plutil -replace runner_path    -string "$RUNNER_DST" "$tmp"
  plutil -replace agent_dir      -string "$AGENT_DST" "$tmp"
  plutil -replace python3        -string "$PYTHON3_FOR_AGENT" "$tmp"
  plutil -replace ffmpeg         -string "$FFMPEG" "$tmp"
  plutil -replace ffprobe        -string "$FFPROBE" "$tmp"
  plutil -replace ffmpeg_version -string "$FFMPEG_VERSION" "$tmp"
  plutil -replace start_interval -integer "$START_INTERVAL" "$tmp"
  plutil -replace first_access_ts -string "$FIRST_ACCESS_TS" "$tmp"
  plutil -convert json "$tmp"
  mv -f "$tmp" "$RECEIPT"
}

# ---------------------------------------------------------------------------
# 6. Repair mode (B2) — STRICTLY OFFLINE
# ---------------------------------------------------------------------------
# Everything here reads what is already installed and re-bakes the LaunchAgent.
# No engine detection, no venv, no pip: this mode is what the app calls
# synchronously when the only thing wrong is ProgramArguments[0], and it is not
# allowed to touch the network (the full installer's `pip install --upgrade pip`
# is exactly why this mode exists).
receipt_field() {
  [[ -f "$RECEIPT" ]] || return 1
  plutil -extract "$1" raw -o - "$RECEIPT" 2>/dev/null || return 1
}
plist_env_field() {
  [[ -f "$PLIST" ]] || return 1
  plutil -extract "EnvironmentVariables.$1" raw -o - "$PLIST" 2>/dev/null || return 1
}
# receipt → plist, first non-empty wins.
carry_over() {
  local from_receipt from_plist
  from_receipt="$(receipt_field "$1" 2>/dev/null || true)"
  [[ -n "$from_receipt" ]] && { printf '%s' "$from_receipt"; return 0; }
  from_plist="$(plist_env_field "$2" 2>/dev/null || true)"
  [[ -n "$from_plist" ]] && { printf '%s' "$from_plist"; return 0; }
  printf ''
}

run_repair() {
  acquire_install_lock          # [guard:lock]
  guard_no_build_in_progress    # [guard:busy]
  guard_no_symlinks             # [guard:nosymlink]

  [[ -f "$RECEIPT" || -f "$PLIST" ]] || \
    die "nothing to repair: no install receipt and no LaunchAgent plist under $APP_SUPPORT. Run the full installer."

  # The installed engine must be complete and genuine — offline checks only.
  guard_golden_sha "$AGENT_BIN_DST" "installed"   # [guard:golden-dst]
  [[ -f "$RUNNER_DST" ]] || die "runner.sh is missing at $RUNNER_DST — run the full installer."
  [[ -f "$AGENT_DST/__main__.py" ]] || die "the agent package is missing at $AGENT_DST — run the full installer."

  # Carry the environment over from the previous install (never re-detect).
  # Explicit argument > WATCH_DIR env > receipt > plist.
  WATCH_DIR="$ARG_WATCH"
  [[ -z "$WATCH_DIR" ]] && WATCH_DIR="$ENV_WATCH_DIR"
  [[ -z "$WATCH_DIR" ]] && WATCH_DIR="$(carry_over watch_dir MP3TOM4B_WATCH_DIR)"
  [[ -z "$WATCH_DIR" ]] && die "cannot tell which folder to watch (no receipt, no plist env) — run the full installer."
  case "$WATCH_DIR" in "~"|"~/"*) WATCH_DIR="$HOME/${WATCH_DIR#\~/}" ;; esac
  mkdir -p "$WATCH_DIR"
  WATCH_DIR="$(cd "$WATCH_DIR" && pwd)"

  PYTHON3_FOR_AGENT="$(carry_over python3 PYTHON3)"
  [[ -z "$PYTHON3_FOR_AGENT" && -x "$VENV_DIR/bin/python3" ]] && PYTHON3_FOR_AGENT="$VENV_DIR/bin/python3"
  [[ -z "$PYTHON3_FOR_AGENT" ]] && die "cannot tell which python3 the agent used — run the full installer."
  FFMPEG="$(carry_over ffmpeg FFMPEG)"
  FFPROBE="$(carry_over ffprobe FFPROBE)"
  FFMPEG_VERSION="$(carry_over ffmpeg_version FFMPEG_VERSION)"

  mkdir -p "$BIN_DIR" "$COMMANDS_DIR"
  INSTALL_GENERATION="$(uuidgen)"

  PHASE="danger"
  stop_agent
  publish_plist
  test_hook fail-after-publish-plist
  reload_agent
  test_hook fail-after-bootstrap
  verify_loaded_pa0             # [guard:verify-pa0]
  wait_first_access_probe
  write_receipt
  PHASE="committed"

  cat <<EOF
mp3-to-m4b: LaunchAgent repaired (offline).

  Watch folder: $WATCH_DIR
  Agent label:  $LABEL
  Agent binary: $AGENT_BIN_DST
  LaunchAgent:  $PLIST
  Generation:   $INSTALL_GENERATION
EOF
  exit 0
}

[[ "$MODE" == "repair" ]] && run_repair   # [guard:repair-offline]

# ---------------------------------------------------------------------------
# 7. Full install — preflight (nothing production-critical is touched yet)
# ---------------------------------------------------------------------------

# 7.0 The frozen helper source, checked against the golden SHA BEFORE we write
#     anything at all. This is the first gate in the whole script on purpose.
src_helper="$(find_agent_bin)" || die "missing $HELPER_NAME (the frozen helper) next to this installer"
[[ -L "$src_helper" ]] && die "refusing: the helper source $src_helper is a symlink"
guard_golden_sha "$src_helper" "source"   # [guard:golden-src]

src_runner="$(find_runner)" || die "missing runner.sh source"
src_agent_dir="$(find_agent_dir)" || die "missing agent/ package source (no __main__.py found)"

acquire_install_lock          # [guard:lock]
guard_no_build_in_progress    # [guard:busy]
guard_not_downgrade           # [guard:downgrade]
guard_no_symlinks             # [guard:nosymlink]

# 7.1 Detect ffmpeg + ffprobe (the engine) and python3
detect_tool() {
  # $1 = tool name, $2 = the env override captured at startup ($FFMPEG/$FFPROBE —
  # they must be read from ENV_* because this script uses the bare names itself).
  # Order: env override -> Homebrew dirs -> PATH.
  local name="$1" cand="${2:-}"
  if [[ -n "$cand" && -x "$cand" ]]; then printf '%s' "$cand"; return 0; fi
  for cand in "/opt/homebrew/bin/$name" "/usr/local/bin/$name"; do
    [[ -x "$cand" ]] && { printf '%s' "$cand"; return 0; }
  done
  cand="$(command -v "$name" 2>/dev/null || true)"
  [[ -n "$cand" && -x "$cand" ]] && { printf '%s' "$cand"; return 0; }
  return 1
}

if ! FFMPEG="$(detect_tool ffmpeg "$ENV_FFMPEG")"; then
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

if ! FFPROBE="$(detect_tool ffprobe "$ENV_FFPROBE")"; then
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

# Probe the engine version ONCE, here, and hand it to the agent through the
# plist: an idle tick must not spawn `ffmpeg -version` every StartInterval (M6f).
# Same parse as agent/scan.py::_probe_engine_version ("ffmpeg version n7.1 …" → 7.1).
FFMPEG_VERSION="$("$FFMPEG" -version 2>/dev/null | head -1 | awk '
  {if ($1=="ffmpeg" && $2=="version") {t=$3; sub(/^[nv]/,"",t); print (t==""?$0:t)} else {print $0}; exit}' || true)"

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

# 7.2 Resolve WATCH_DIR  (argument > WATCH_DIR env > default)
WATCH_DIR="${ARG_WATCH:-${ENV_WATCH_DIR:-$HOME/Desktop/mp3-to-m4b}}"
# Normalize a literal leading tilde from user input (it would not expand inside
# the quoted arg/env). We match the literal '~' on purpose, then expand via HOME.
# shellcheck disable=SC2088
case "$WATCH_DIR" in
  "~"|"~/"*) WATCH_DIR="$HOME/${WATCH_DIR#\~/}" ;;
esac
mkdir -p "$WATCH_DIR"
WATCH_DIR="$(cd "$WATCH_DIR" && pwd)"

# 7.3 Data-directory skeleton. COMMANDS_DIR must EXIST before (re)load — launchd
#     only watches paths that are present.
mkdir -p "$BIN_DIR" "$COMMANDS_DIR" "$LAUNCH_AGENTS_DIR" "$(dirname "$LOG_FILE")"

# 7.4 venv + Pillow (cover generation guarantee, PRD G4). This is the LONG,
#     possibly-networked part — it deliberately runs BEFORE the first
#     destructive step, so a slow/failing pip can never leave the agent torn
#     down. Both pip calls are bounded (no hang: that was blocker B2).
VENV_PYTHON="$VENV_DIR/bin/python3"
if [[ "$NO_VENV" != "1" ]]; then
  if [[ ! -x "$VENV_PYTHON" ]]; then
    note "creating venv at $VENV_DIR"
    "$PYTHON3_SRC" -m venv "$VENV_DIR"
  fi
  [[ -x "$VENV_PYTHON" ]] || die "venv python missing after creation ($VENV_PYTHON)"
  # Quietly upgrade pip (best effort — never fail the install, never hang).
  "$VENV_PYTHON" -m pip install --quiet --timeout 15 --retries 1 --upgrade pip >/dev/null 2>&1 || true
  if "$VENV_PYTHON" -c "import PIL" >/dev/null 2>&1; then
    note "Pillow already present in venv"
  else
    note "installing Pillow into venv"
    if ! "$VENV_PYTHON" -m pip install --quiet --timeout 30 --retries 2 Pillow; then
      cat >&2 <<EOF
mp3-to-m4b: failed to install Pillow into the venv.

Pillow is needed to generate audiobook covers. Check your internet connection
and re-run this installer, or install it manually:

  "$VENV_PYTHON" -m pip install Pillow
EOF
      exit 1
    fi
  fi
  "$VENV_PYTHON" -c "import PIL; from PIL import Image, ImageDraw, ImageFont" \
    || die "Pillow installed but import failed"
  PYTHON3_FOR_AGENT="$VENV_PYTHON"
else
  note "MP3TOM4B_NO_VENV=1 -> skipping venv/Pillow (using $PYTHON3_SRC)"
  PYTHON3_FOR_AGENT="$PYTHON3_SRC"
fi

# ---------------------------------------------------------------------------
# 8. Stage → validate (still nothing production-critical touched)
# ---------------------------------------------------------------------------
rm -rf "$STAGE_DIR" "$BACKUP_DIR"
mkdir -p "$STAGE_DIR/agent"
for f in "$src_agent_dir"/*.py; do
  install -m 0644 "$f" "$STAGE_DIR/agent/$(basename "$f")"
done
[[ -f "$STAGE_DIR/agent/__main__.py" ]] || die "agent package staging failed (no __main__.py staged)"
install -m 0755 "$src_runner" "$STAGE_DIR/runner.sh"
install -m 0755 "$src_helper" "$STAGE_DIR/$HELPER_NAME"
# The staged helper must still be the frozen artifact (catches a broken copy
# before anything production sees it). Same source-side gate as above.
guard_golden_sha "$STAGE_DIR/$HELPER_NAME" "staged"   # [guard:golden-src]

INSTALL_GENERATION="$(uuidgen)"

# ---------------------------------------------------------------------------
# 9. DANGER ZONE — from here a failure must roll back
#    bootout → replace → publish plist → bootstrap → verify → receipt
# ---------------------------------------------------------------------------
PHASE="danger"
mkdir -p "$BACKUP_DIR"

stop_agent

# 9.1 the agent package: move the old one aside, move the staged one in.
if [[ -d "$AGENT_DST" ]]; then
  mv "$AGENT_DST" "$BACKUP_DIR/agent"
  BACKED_UP_AGENT=1
fi
mv "$STAGE_DIR/agent" "$AGENT_DST"

# 9.2 runner.sh — freely mutable, but keep the preserve rule so an identical
#     re-install does not churn the file.
if [[ -f "$RUNNER_DST" ]]; then
  cp -p "$RUNNER_DST" "$BACKUP_DIR/runner.sh"
  BACKED_UP_RUNNER=1
fi
if [[ ! -f "$RUNNER_DST" ]] || ! cmp -s "$STAGE_DIR/runner.sh" "$RUNNER_DST"; then
  install -m 0755 "$STAGE_DIR/runner.sh" "$RUNNER_DST"
fi

# 9.3 the frozen helper. PRESERVE: only write it when it is missing or actually
#     different, so a re-install never churns the file the grant is pinned to.
#     It is never rolled back and never removed (Р5).
if [[ ! -f "$AGENT_BIN_DST" ]] || ! cmp -s "$STAGE_DIR/$HELPER_NAME" "$AGENT_BIN_DST"; then
  install -m 0755 "$STAGE_DIR/$HELPER_NAME" "$AGENT_BIN_DST"
fi
# Targeted quarantine strip — ONLY the helper (a quarantined Mach-O exec'd by
# launchd can be blocked with no UI). Never recursive. Best effort.
xattr -d com.apple.quarantine "$AGENT_BIN_DST" 2>/dev/null || true

test_hook corrupt-dst-after-install
# B5, destination side: what is actually on disk now must BE the frozen artifact.
guard_golden_sha "$AGENT_BIN_DST" "installed"   # [guard:golden-dst]
# Copy-quality check (kept, but it is NOT the identity gate — two identically
# corrupted files compare equal, which is precisely why the golden SHA exists).
cmp -s "$src_helper" "$AGENT_BIN_DST" || die "installed helper differs from the shipped one"   # [guard:copy-quality]
test_hook fail-after-replace

# 9.4 publish the plist (with a backup for rollback)
publish_plist
test_hook fail-after-publish-plist

# 9.5 (re)load and PROVE the reload took
reload_agent
test_hook fail-after-bootstrap
verify_loaded_pa0             # [guard:verify-pa0]

# 9.6 give the agent a moment to publish its first access probe (addendum §5.2)
wait_first_access_probe

# 9.7 the receipt — LAST (B3): its presence is the proof that everything above
#     succeeded, including the launchd verification.
write_receipt
PHASE="committed"

# ---------------------------------------------------------------------------
# 10. Report
# ---------------------------------------------------------------------------
needs_access=0
case "$WATCH_DIR/" in
  "$HOME/Desktop/"*|"$HOME/Documents/"*|"$HOME/Downloads/"*) needs_access=1 ;;
esac

cat <<EOF
mp3-to-m4b installed.

  Watch folder: $WATCH_DIR
  Agent label:  $LABEL
  Agent binary: $AGENT_BIN_DST
  Runner:       $RUNNER_DST
  Agent:        $AGENT_DST
  Python:       $PYTHON3_FOR_AGENT
  ffmpeg:       $FFMPEG ($FFMPEG_VERSION)
  ffprobe:      $FFPROBE
  LaunchAgent:  $PLIST
  Generation:   $INSTALL_GENERATION
  Receipt:      $RECEIPT
  Log:          $LOG_FILE

Drop a folder of .mp3 files into the watch folder — the app will offer to build
it into a single .m4b audiobook.
EOF

if [[ "$needs_access" -eq 1 ]]; then
  cat <<EOF

NOTE: Your watch folder is inside a macOS-protected location. macOS will ask you
once for permission — click "Allow" in the system dialog.

If the dialog never appears and books are not picked up, grant Full Disk Access
to the agent binary instead:

  System Settings -> Privacy & Security -> Full Disk Access -> "+"
  Add: $AGENT_BIN_DST
  (press Cmd-Shift-G in the picker and paste the path above)

The grant is keyed to that exact file and survives every update.
EOF
fi
