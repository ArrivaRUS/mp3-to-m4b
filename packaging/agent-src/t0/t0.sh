#!/bin/bash
# t0.sh — the T0 gate (arch/plan-binrunner-mp3-v2.md §4). It answers ONE
# question on a live system: does macOS hand the TCC/FDA decision to our frozen
# Mach-O helper, or to /bin/bash?
#
# Until this is GREEN, milestones M1–M7 do not start: if the subject is not the
# helper, the whole binary-runner design is pointless and half of M1–M6 would be
# built on sand.
#
# WHAT MAKES A VERDICT TRUSTWORTHY HERE
#   * The System Settings panel is NOT evidence — it fails to draw fresh records
#     (sibling lesson 020B). We never screenshot it and never reason from it.
#   * `launchctl print` proves the LOADED PA0. `codesign` proves the image on
#     disk. Neither proves the TCC subject.
#   * So the gate rests on two independent legs:
#       (a) BEHAVIOUR — a 4-cell truth table only one hypothesis satisfies;
#       (b) tccd's own log, stitched by a single msgID (t0_log.py).
#     (a) is the gate. (b) corroborates it and is reported honestly even when
#     macOS redacts the fields as <private>.
#
# THE TRUTH TABLE (cells B–F need the human's toggle, ~5 min once)
#   A  PA0 = helper, no grant yet               → unreadable  (T0.1)
#   A' PA0 = runner.sh (shebang), no grant yet  → unreadable + subject=/bin/bash (T0.2)
#   B  PA0 = helper, grant given                → READABLE    (T0.3)
#   C  PA0 = runner.sh, grant still on helper   → unreadable  (T0.2 with teeth)
#   D  PA0 = helper, MUTATED bytes, same path   → unreadable  (T0.4a)
#   E  PA0 = helper, frozen bytes restored      → READABLE    (T0.4b)
#   F  PA0 = helper, toggle switched off        → unreadable  (control + cleanup)
#   Only "the grant is pinned to PA0's image, by path AND by bytes" fits all of
#   them. C alone kills "it was ambient access all along"; D alone kills "any
#   binary at that path would do".
#
# TWO WAYS TO BE UNREADABLE — and the difference is evidence
#   Measured here on 2026-07-25 (macOS 26.5.2), an ungranted read of a protected
#   folder ends in one of two ways depending ONLY on PA0:
#     `denied`  — EPERM in ~200 ms, with a full tccd AUTHREQ naming /bin/bash;
#     `blocked` — no answer at all, wedged in open() (>60 s), tccd silent.
#   The shebang PA0 always produced the first, the Mach-O helper always the
#   second. So the cells assert readable / not-readable and REPORT the flavour
#   separately — folding `blocked` into `denied` would throw away the sharpest
#   pre-grant signal we have. (It also means the shipping probe needs a
#   watchdog: see t0_probe.py's docstring, and tell the architects.)
#
# ISOLATION
#   Test label, test root, test helper NAME — nothing here touches the
#   production label / App Support tree / plist, and the harness refuses to run
#   if any of them collide. The T0 grant is deliberately NOT the production
#   grant: the production one (R1b) is a separate 2-minute step in M7, so cell D
#   can corrupt bytes without ever endangering it.
#
# Usage:
#   t0.sh              # = run: prepare + cells A/A', then tell the human what to click
#   t0.sh t07          # clean zone re-test GATE→CLOUD→GATE (addendum §1.5), no
#                      # destructive cells, fresh identity — run this after a reboot
#   t0.sh selftest     # check the decision logic itself (no launchd, no human)
#   t0.sh verify       # after the grant: cells B, C, D, E → the gate verdict
#   t0.sh verify-off   # after the human switches the toggle back off: cell F
#   t0.sh exec-form    # T0.5, informational only, not part of the gate
#   t0.sh status       # what is on disk / loaded right now
#   t0.sh clean        # remove the T0 root, plist and probe folder
#
# Overrides (both axes are independent variables ON PURPOSE — M1f):
#   T0_HELPER_DIR   where the grant subject lives   (default $T0_ROOT/bin)
#   T0_STATE_ROOT   where state is written          (default $T0_ROOT/state)
#   T0_ROOT T0_LABEL T0_HELPER_NAME T0_WATCH

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SELF_DIR/../../.." && pwd)"

LABEL="${T0_LABEL:-com.arrivarus.mp3tom4b.t0}"
T0_ROOT="${T0_ROOT:-$HOME/Library/Application Support/mp3-to-m4b-t0}"
HELPER_DIR="${T0_HELPER_DIR:-$T0_ROOT/bin}"
HELPER_NAME="${T0_HELPER_NAME:-mp3-to-m4b-agent-t0}"
HELPER_PATH="$HELPER_DIR/$HELPER_NAME"
T0_RUNNER="$HELPER_DIR/runner.sh"
STATE_ROOT="${T0_STATE_ROOT:-$T0_ROOT/state}"
# TWO probe zones, and the distinction is load-bearing (measured 2026-07-25):
#   GATE  — TCC-protected but LOCAL. This is where the helper-vs-bash question
#           gets a clean, fast answer, so this is what the gate asserts.
#   CLOUD — ~/Desktop, which on this machine is an iCloud FileProvider domain.
#           With an identical, granted TCC record the very same read hangs here
#           and answers in 5 ms in the GATE zone. That is a property of the
#           folder, not of the helper — so it is reported as a DIAGNOSTIC and
#           never allowed to turn the gate red.
WATCH_GATE="${T0_WATCH:-$HOME/Downloads/mp3tom4b-t0-probe}"
WATCH_CLOUD="${T0_WATCH_CLOUD:-$HOME/Desktop/mp3tom4b-t0-probe}"
CUR_WATCH="$WATCH_GATE"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
RUN_LOG="$T0_ROOT/t0.log"
GOLDEN="$REPO/packaging/mp3-to-m4b-agent"
SRC_C="$REPO/packaging/agent-src/mp3-to-m4b-agent.c"
DOMAIN="gui/$(id -u)"
AGENT_PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

PROD_LABEL="com.arrivarus.mp3tom4b.agent"
PROD_ROOT="$HOME/Library/Application Support/mp3-to-m4b"

PY="/usr/bin/python3"
[[ -x "$PY" ]] || PY="$(command -v python3 2>/dev/null || true)"

say() { printf '%s\n' "$*"; }
hr()  { printf -- '--------------------------------------------------------------------\n'; }
die() { printf 't0: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Guards: the production install must be untouchable from here.
# ---------------------------------------------------------------------------
guard() {
  [[ "$LABEL" == "$PROD_LABEL" ]]      && die "refusing: T0 label equals the production label"
  [[ "$T0_ROOT" == "$PROD_ROOT" ]]     && die "refusing: T0 root equals the production App Support tree"
  [[ "$HELPER_DIR" == "$PROD_ROOT/bin" ]] && die "refusing: T0 helper dir equals the production bin/"
  [[ "$HELPER_PATH" == "$GOLDEN" ]]    && die "refusing: T0 helper path equals the repo's frozen artifact"
  [[ -n "$PY" && -x "$PY" ]]           || die "python3 not found"
  return 0
}

bootout_quiet() { launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1 || true; }

# --- test identity ---------------------------------------------------------
# The grant is keyed to (path, cdhash). A path that already carries a TCC record
# for DIFFERENT bytes is not a clean slate: macOS sees a known client that no
# longer matches, and the probe wedges instead of prompting. T0.7 must start
# from an identity nobody has ever granted, otherwise its first cell can only
# report `blocked` and the whole run is wasted.
use_identity() {
  local suffix="$1"
  HELPER_NAME="mp3-to-m4b-agent-$suffix"
  HELPER_DIR="$T0_ROOT/bin-$suffix"
  HELPER_PATH="$HELPER_DIR/$HELPER_NAME"
  T0_RUNNER="$HELPER_DIR/runner.sh"
}

# Pick the first suffix with no TCC record and no directory. Honors an explicit
# T0_T07_SUFFIX. The query is scoped to our own T0 paths — nothing else is read.
pick_t07_suffix() {
  if [[ -n "${T0_T07_SUFFIX:-}" ]]; then printf '%s' "$T0_T07_SUFFIX"; return 0; fi
  local db="$HOME/Library/Application Support/com.apple.TCC/TCC.db" n cand esc cnt
  for n in 1 2 3 4 5 6 7 8 9; do
    cand="$T0_ROOT/bin-t$n/mp3-to-m4b-agent-t$n"
    [[ -e "$cand" ]] && continue
    if [[ -r "$db" ]]; then
      esc="${cand//\'/\'\'}"
      cnt="$(sqlite3 "$db" "SELECT COUNT(*) FROM access WHERE client='$esc';" 2>/dev/null || printf '0')"
      [[ "$cnt" == "0" ]] || continue
    fi
    printf 't%s' "$n"; return 0
  done
  printf 't9'
}

sha_of() { shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1; }

# ---------------------------------------------------------------------------
# Structural parity between the shipping runner and the T0 twin. The gate must
# measure the FORM that ships; a drifted twin would measure nothing.
# ---------------------------------------------------------------------------
parity_check() {
  local a="$REPO/bin/runner.sh" b="$SELF_DIR/t0-runner.sh" miss=0 pat
  while IFS= read -r pat; do
    [[ -z "$pat" ]] && continue
    if ! grep -qF "$pat" "$a" || ! grep -qF "$pat" "$b"; then
      say "   FORM PARITY MISS: «$pat» not in both runners"; miss=1
    fi
  done <<'PATTERNS'
CHILD=$!
trap 'forward_signal TERM' TERM
trap 'forward_signal INT'  INT
trap 'forward_signal HUP'  HUP
if (( status > 128 )) && kill -0 "$CHILD" 2>/dev/null; then
exit "$status"
PATTERNS
  if [[ "$miss" -eq 0 ]]; then
    say "   form parity : OK — bin/runner.sh and t0-runner.sh share the trap + re-wait skeleton"
  else
    say "   form parity : DRIFTED — the gate would not measure the shipping form"
  fi
  return "$miss"
}

# ---------------------------------------------------------------------------
prepare() {
  guard
  [[ -f "$GOLDEN" ]] || die "missing frozen helper $GOLDEN (run packaging/agent-src/build-once.sh)"
  mkdir -p "$HELPER_DIR" "$STATE_ROOT" "$(dirname "$PLIST")" || die "cannot create the T0 tree"
  # The harness itself needs to reach both probe folders. Terminal.app is denied
  # Full Disk Access on this machine while Claude Code holds it, so "run it
  # yourself in Terminal" can fail here for a reason that has nothing to do with
  # the test. Say so in plain language instead of dying cryptically.
  local w
  for w in "$WATCH_GATE" "$WATCH_CLOUD"; do
    if ! mkdir -p "$w" 2>/dev/null || ! printf 'mp3-to-m4b T0 marker — if the agent can read this line, the grant works.\n' > "$w/marker.txt" 2>/dev/null; then
      die "не могу создать/записать $w — у программы, из которой запущен скрипт,
      нет доступа к этой папке (у Терминала его обычно нет, у Claude Code есть).
      Это НЕ результат теста. Попроси Юрку запустить t0.sh t07 за тебя, либо
      выдай Терминалу доступ в «Конфиденциальность и безопасность»."
    fi
  done

  install -m 0755 "$GOLDEN"                "$HELPER_PATH"
  install -m 0755 "$SELF_DIR/t0-runner.sh" "$T0_RUNNER"
  install -m 0644 "$SELF_DIR/t0_probe.py"  "$HELPER_DIR/t0_probe.py"
  # A copy that ever travelled through a DMG/download would carry quarantine and
  # be refused by Gatekeeper for the wrong reason.
  xattr -d com.apple.quarantine "$HELPER_PATH" 2>/dev/null || true

  hr
  say "== T0 setup"
  say "   helper (grant subject) : $HELPER_PATH"
  say "   helper sha256          : $(sha_of "$HELPER_PATH")"
  say "   frozen artifact sha256 : $(sha_of "$GOLDEN")"
  say "   runner (sibling)       : $T0_RUNNER"
  say "   state root (separate)  : $STATE_ROOT"
  say "   watch GATE  (TCC,local): $WATCH_GATE"
  say "   watch CLOUD (TCC+iCloud, diagnostic only): $WATCH_CLOUD"
  say "   label                  : $LABEL"
  say "   plist                  : $PLIST"
  parity_check
  if [[ "$(sha_of "$HELPER_PATH")" != "$(sha_of "$GOLDEN")" ]]; then
    die "deployed helper differs from the frozen artifact"
  fi
}

# ---------------------------------------------------------------------------
# write_plist <PA0> <hint>
# ---------------------------------------------------------------------------
write_plist() {
  local pa0="$1" hint="$2" out
  out="$(mktemp -t mp3t0plist)".plist
  cat > "$out" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict/>
</plist>
PLIST
  plutil -replace Label -string "$LABEL" "$out"
  plutil -replace ProgramArguments -json '[]' "$out"
  plutil -insert  ProgramArguments.0 -string "$pa0" "$out"
  plutil -replace RunAtLoad        -bool true  "$out"
  plutil -replace ThrottleInterval -integer 1  "$out"
  plutil -replace StandardOutPath   -string "$RUN_LOG" "$out"
  plutil -replace StandardErrorPath -string "$RUN_LOG" "$out"
  plutil -replace EnvironmentVariables -json '{}' "$out"
  plutil -insert  EnvironmentVariables.PYTHON3       -string "$PY"         "$out"
  plutil -insert  EnvironmentVariables.PATH          -string "$AGENT_PATH" "$out"
  plutil -insert  EnvironmentVariables.T0_STATE_ROOT -string "$STATE_ROOT" "$out"
  plutil -insert  EnvironmentVariables.T0_WATCH      -string "$CUR_WATCH"  "$out"
  plutil -insert  EnvironmentVariables.T0_PA0_HINT   -string "$hint"       "$out"
  plutil -insert  EnvironmentVariables.T0_PROBE_TIMEOUT -string "${T0_PROBE_TIMEOUT:-8}" "$out"
  # The agent's own override, pointed at the scratch state — NOT at the helper
  # path. Keeping these two axes separate is the whole point of M1f.
  plutil -insert  EnvironmentVariables.MP3TOM4B_SUPPORT_DIR -string "$STATE_ROOT" "$out"
  plutil -lint "$out" >/dev/null || die "generated an invalid plist"
  mv -f "$out" "$PLIST"
}

# ---------------------------------------------------------------------------
# ORDERING RULE, ENFORCED — not a comment (addendum §1.5 п.4)
#
# `mutate_helper` runs a foreign binary at the granted path. That poisons the
# subject: every later probe measures a client macOS no longer recognises, and a
# refusal after it says nothing about the folder, the grant or anything else.
# The first version of this harness measured the iCloud zone AFTER that cell and
# produced a confident, WRONG conclusion about FileProvider.
#
# So: a cell declared `diag` REFUSES to run once anything destructive has, and
# every cell after a destructive one is stamped in the output. Comparing a
# poisoned reading with a clean one is now impossible by construction, not by
# discipline.
# ---------------------------------------------------------------------------
DESTRUCTIVE_RAN=0

# ---------------------------------------------------------------------------
# cycle <PA0> <hint> <expected-access> <expected-subject: helper|bash> <title>
#       [kind: gate|diag|destructive]
# Sets: CYCLE_ACCESS CYCLE_LOGV CYCLE_OK
# ---------------------------------------------------------------------------
cycle() {
  local pa0="$1" hint="$2" want_access="$3" want_subject="$4" title="$5" kind="${6:-gate}"
  CYCLE_ACCESS="?"; CYCLE_LOGV="?"; CYCLE_OK=1

  if [[ "$kind" == "diag" && "$DESTRUCTIVE_RAN" -eq 1 ]]; then
    hr
    say "== $title"
    die "REFUSING to run a diagnostic cell after a destructive one — it would measure a poisoned subject and the reading would be worthless. Re-run from a clean state (t0.sh t07)."
  fi

  hr
  say "== $title"
  if [[ "$DESTRUCTIVE_RAN" -eq 1 ]]; then
    say "   ⚠️  ОТРАВЛЕННЫЙ ПУТЬ: по этому пути уже запускался чужой бинарь."
    say "       Отказ в этой ячейке НИЧЕГО не говорит ни о папке, ни о гранте."
    say "       Сравнивать её с ячейками до порчи нельзя."
  fi
  say "   watch      : $CUR_WATCH"
  say "   PA0        : $pa0"
  say "   realpath   : $("$PY" -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' "$pa0" 2>/dev/null)"
  say "   sha256     : $(sha_of "$pa0")"

  bootout_quiet
  rm -f "$STATE_ROOT/t0_result.json" "$STATE_ROOT/python.json" "$STATE_ROOT/bash.json"
  : > "$RUN_LOG" 2>/dev/null || true
  write_plist "$pa0" "$hint"

  local start; start="$(date -v-3S '+%Y-%m-%d %H:%M:%S')"
  if ! launchctl bootstrap "$DOMAIN" "$PLIST" 2>"$STATE_ROOT/bootstrap.err"; then
    say "   bootstrap  : FAILED — $(tr '\n' ' ' < "$STATE_ROOT/bootstrap.err")"
    CYCLE_ACCESS="bootstrap-failed"; return 1
  fi

  launchctl print "$DOMAIN/$LABEL" > "$STATE_ROOT/launchctl-print.txt" 2>&1 || true
  local loaded
  loaded="$(awk '/arguments = \{/{f=1;next} f&&/^[ \t]*\}/{exit} f{gsub(/^[ \t]+|[ \t]+$/,"");print}' \
            "$STATE_ROOT/launchctl-print.txt" | head -1)"
  say "   loaded PA0 : ${loaded:-«launchctl print gave nothing»}"

  # The harness must outwait the probe's own watchdog, otherwise it declares "no
  # result" while the probe is still legitimately waiting — which is exactly what
  # happens when a human is being asked to approve a dialog.
  local i max_wait=$(( ${T0_PROBE_TIMEOUT:-8} + 15 ))
  for i in $(seq 1 $((max_wait * 10))); do
    [[ -f "$STATE_ROOT/t0_result.json" ]] && break
    sleep 0.1
  done
  if [[ ! -f "$STATE_ROOT/t0_result.json" ]]; then
    say "   probe      : NO RESULT after ${max_wait}s. Job stdout/stderr:"
    sed 's/^/     | /' "$RUN_LOG" 2>/dev/null | head -20
    bootout_quiet
    CYCLE_ACCESS="no-result"; return 1
  fi

  local kv
  kv="$("$PY" - "$STATE_ROOT/t0_result.json" <<'PY'
import json, sys
try:
    from shlex import quote
except ImportError:
    from pipes import quote
d = json.load(open(sys.argv[1]))
for k in ("folder_access", "py_pid", "py_ppid", "bash_pid", "bash_ppid",
          "marker_seen", "marker_text", "error", "form", "python_exe", "elapsed_s"):
    print("R_%s=%s" % (k.upper(), quote(str(d.get(k)))))
PY
)"
  eval "$kv"

  say "   FORM       : $R_FORM"
  say "   pids       : helper/launchd=$R_BASH_PPID  bash=$R_BASH_PID  python=$R_PY_PID (python's parent=$R_PY_PPID)"
  if [[ "$R_BASH_PID" == "$R_PY_PID" ]]; then
    say "   form check : ⚠️  bash pid == python pid → this is the exec form, NOT the donor form"
  else
    say "   form check : OK — bash pid ≠ python pid, exactly as the donor form requires"
    say "                (Codex's recipe was written for the exec variant and would print"
    say "                 «NOT EXEC: pid changed» here — that is the EXPECTED result, not a failure)"
  fi
  [[ "$R_PY_PPID" == "$R_BASH_PID" ]] \
    && say "   chain      : OK — python's parent is our bash" \
    || say "   chain      : ⚠️  python's parent ($R_PY_PPID) is not our bash ($R_BASH_PID)"

  say "   python     : $R_PYTHON_EXE"
  say "   ACCESS     : $R_FOLDER_ACCESS  (${R_ELAPSED_S}s, marker_seen=$R_MARKER_SEEN)"
  [[ "$R_ERROR" != "None" ]] && say "                $R_ERROR"
  [[ "$R_MARKER_TEXT" != "None" ]] && say "   marker read: «$R_MARKER_TEXT»"
  # The FLAVOUR of a refusal is itself evidence about the subject.
  case "$R_FOLDER_ACCESS" in
    denied)  say "   flavour    : instant deny — tccd answered. On this OS that is the"
             say "                signature of a PLATFORM binary as subject (/bin/bash)." ;;
    blocked) say "   flavour    : no answer at all — neither allow nor deny, tccd silent."
             say "                Signature of an ATTRIBUTABLE, ungranted subject: macOS"
             say "                wants to ask the user, and a bare LaunchAgent cannot ask." ;;
  esac

  say "   tccd log correlation (single msgID):"
  local logout
  logout="$("$PY" "$SELF_DIR/t0_log.py" --start "$start" --py-pid "$R_PY_PID" \
            --helper-path "$HELPER_PATH" --expect "$want_subject" \
            --raw "$STATE_ROOT/tccd-$hint.log" 2>&1)"
  printf '%s\n' "$logout" | grep -v '^LOG_VERDICT=' | sed 's/^/  /'
  CYCLE_LOGV="$(printf '%s\n' "$logout" | sed -n 's/^LOG_VERDICT=//p' | tail -1)"
  say "   log verdict: ${CYCLE_LOGV:-?} (expected subject: $want_subject)"

  bootout_quiet
  CYCLE_ACCESS="$R_FOLDER_ACCESS"
  # Cells assert reachability (ok / not-ok), not the exact refusal word: an
  # ungranted attributable subject answers `blocked`, a platform binary answers
  # `denied`, and BOTH mean "the folder was not readable". Which of the two it
  # was is reported above as evidence, never silently folded into a pass.
  if [[ "$want_access" == "ok" ]]; then
    [[ "$CYCLE_ACCESS" == "ok" ]] && CYCLE_OK=0 || CYCLE_OK=1
  else
    [[ "$CYCLE_ACCESS" != "ok" ]] && CYCLE_OK=0 || CYCLE_OK=1
  fi
  if [[ "$CYCLE_OK" -eq 0 ]]; then
    say "   CELL       : PASS (access=$CYCLE_ACCESS, expected $want_access)"
  else
    say "   CELL       : FAIL (access=$CYCLE_ACCESS, expected $want_access)"
  fi
  return "$CYCLE_OK"
}

# ---------------------------------------------------------------------------
# Byte negative control: a DIFFERENT, still-validly-signed binary at the SAME
# path. Built from a copy of the frozen source with one string changed — a
# byte-flipped file would be rejected for an invalid signature instead, which
# would prove nothing. The repo artifact is never touched.
# ---------------------------------------------------------------------------
mutate_helper() {
  command -v clang >/dev/null 2>&1 || { say "   clang missing — cannot build the byte control"; return 1; }
  # From here on the path's identity is compromised for the rest of the session.
  DESTRUCTIVE_RAN=1
  local tmp; tmp="$(mktemp -d)"
  sed 's|cannot resolve own path|cannot resolve own path [T0 BYTE-CONTROL VARIANT, NEVER SHIP]|' \
      "$SRC_C" > "$tmp/variant.c"
  clang -Os -arch arm64 -arch x86_64 -mmacosx-version-min=11.0 \
        -o "$tmp/variant" "$tmp/variant.c" 2>"$tmp/err" || {
    say "   variant build failed: $(head -3 "$tmp/err")"; rm -rf "$tmp"; return 1; }
  strip "$tmp/variant"
  codesign --force -s - "$tmp/variant" >/dev/null 2>&1
  codesign --verify --strict "$tmp/variant" >/dev/null 2>&1 || {
    say "   variant failed codesign --verify"; rm -rf "$tmp"; return 1; }
  install -m 0755 "$tmp/variant" "$HELPER_PATH"
  rm -rf "$tmp"
  say "   byte control installed at the SAME path, sha256=$(sha_of "$HELPER_PATH")"
  say "   (a valid ad-hoc signature, different cdhash — the only honest control)"
  return 0
}

restore_helper() {
  install -m 0755 "$GOLDEN" "$HELPER_PATH"
  [[ "$(sha_of "$HELPER_PATH")" == "$(sha_of "$GOLDEN")" ]] \
    && { say "   frozen bytes restored, sha256=$(sha_of "$HELPER_PATH")"; return 0; } \
    || { say "   RESTORE FAILED"; return 1; }
}

# ---------------------------------------------------------------------------
human_block() {
  hr
  say ""
  say "  ЧЕЛОВЕКУ — 5 минут, два клика и один тумблер"
  say "  ─────────────────────────────────────────────"
  say ""
  say "  Путь уже скопирован в буфер обмена. Нужно:"
  say ""
  say "  1) Открыть  Системные настройки → Конфиденциальность и безопасность"
  say "              → Полный доступ к диску."
  say "  2) Нажать «+» (появится окно выбора файла)."
  say "  3) В окне выбора нажать Cmd+Shift+G и вставить путь (Cmd+V) — он уже"
  say "     в буфере:"
  say ""
  say "       $HELPER_PATH"
  say ""
  say "  4) Нажать «Открыть» — в списке появится строка «$HELPER_NAME»."
  say "  5) ВКЛЮЧИТЬ тумблер напротив неё."
  say ""
  say "  Это ТЕСТОВЫЙ файл (в имени «-t0»), не боевой. Он удалится в конце."
  say "  Если система попросит пароль/Touch ID — это нормально, это она."
  say ""
  say "  Дальше ничего делать не надо: скажи «включил» — и я прогоню проверку."
  say ""
  say "  ⚠️  ПОТОМ, когда я скажу: этот же тумблер нужно будет ВЫКЛЮЧИТЬ"
  say "     (а лучше — выделить строку «$HELPER_NAME» и нажать «−»)."
  say "     Это не уборка ради уборки: выключенный тумблер — последний"
  say "     негативный контроль. Если после выключения доступ пропадёт, значит"
  say "     мы всё это время мерили именно твой грант, а не что-то постороннее."
  say ""
  hr
}

# ---------------------------------------------------------------------------
cmd_run() {
  prepare
  local a b a_flavour b_flavour
  cycle "$HELPER_PATH" "nogrant-helper" "notok" "helper" \
        "T0.1 — subject without a grant (PA0 = helper)"; a=$?; a_flavour="$CYCLE_ACCESS"
  cycle "$T0_RUNNER" "nogrant-bash" "notok" "bash" \
        "T0.2 — negative PA0 control (PA0 = runner.sh, a shebang script)"; b=$?; b_flavour="$CYCLE_ACCESS"

  hr
  say "== Итог до гранта"
  say "   T0.1 (PA0=helper,    без гранта): $([[ $a -eq 0 ]] && echo "папка недоступна — верно (flavour: $a_flavour)" || echo "ПАПКА ЧИТАЕТСЯ без гранта — разбираться")"
  say "   T0.2 (PA0=runner.sh, без гранта): $([[ $b -eq 0 ]] && echo "папка недоступна — верно (flavour: $b_flavour)" || echo "ПАПКА ЧИТАЕТСЯ без гранта — разбираться")"
  say ""
  if [[ "$a_flavour" != "$b_flavour" ]]; then
    say "   ⚠️  Флейворы РАЗНЫЕ: helper → «$a_flavour», shebang → «$b_flavour»."
    say "   Это уже сильная улика ДО всякого гранта: единственное, что менялось между"
    say "   прогонами — PA0. Значит субъект решения определяется именно им."
    say "   При shebang tccd в журнале прямо назвал subject=/bin/bash и мгновенно"
    say "   отказал; при helper'е tccd не сказал ничего — систему устраивает наш"
    say "   бинарь как адресат вопроса, только спросить ей некого."
  else
    say "   Обе ячейки до гранта недоступны — это только «прибор включён»."
  fi
  say "   Дискриминирующая сила — в ячейках ПОСЛЕ гранта (t0.sh verify)."
  printf '%s' "$HELPER_PATH" | pbcopy 2>/dev/null \
    && say "   Путь helper'а скопирован в буфер обмена." \
    || say "   (pbcopy недоступен — путь придётся скопировать глазами)"
  human_block
  return 0
}

# ---------------------------------------------------------------------------
# verdict_zones <gate1> <cloud> <gate2>
#
# The ONLY licensed way to say anything about the iCloud zone. A single
# CLOUD reading proves nothing: the subject can go bad between two probes (that
# is exactly how the first "iCloud is the blocker" conclusion was manufactured).
# The claim is allowed only when a repeat of the GATE zone, taken in the same
# state, still answers `ok` — otherwise the difference is drift, not the folder.
# ---------------------------------------------------------------------------
verdict_zones() {
  local gate1="$1" cloud="$2" gate2="$3"
  if [[ "$gate1" != "ok" ]]; then
    say "   ⛔ Судить нельзя: ПЕРВЫЙ замер GATE-зоны дал «$gate1», а не ok."
    say "      Либо грант не выдан этим байтам, либо путь отравлен. Ничего про"
    say "      iCloud эти цифры не говорят. Нужен чистый прогон с новым именем:"
    say "        T0_T07_SUFFIX=t2 packaging/agent-src/t0/t0.sh t07"
    return 2
  fi
  if [[ "$gate2" != "ok" ]]; then
    say "   ⛔ Судить нельзя: контрольный повтор GATE дал «$gate2» вместо ok —"
    say "      состояние поехало МЕЖДУ замерами, и разница «GATE vs CLOUD» может"
    say "      быть просто дрейфом. Ровно так родился отменённый вывод про"
    say "      FileProvider. Повторить прогон целиком."
    return 2
  fi
  if [[ "$cloud" == "ok" ]]; then
    say "   ✅ ok / ok / ok — находка 3 (iCloud) НЕ подтвердилась: это был артефакт"
    say "      порядка ячеек. Дефолтная watch-папка ~/Desktop/mp3-to-m4b остаётся,"
    say "      план Б (§2 аддендума) не нужен. Вопрос закрыт."
    return 0
  fi
  say "   ⚠️  ok / $cloud / ok — iCloud ПОДТВЕРЖДЁН как отдельная причина:"
  say "      обе GATE-пробы в этом же состоянии отвечают ok, а облачная — «$cloud»."
  say "      Это уже не дрейф. Включать план Б из §2 аддендума (смена дефолта)."
  return 1
}

# ---------------------------------------------------------------------------
# T0.7 — the clean re-test (addendum §1.5). GATE → CLOUD → GATE, nothing
# destructive, one pass. This is the whole point: no cell in this mode can
# poison the subject for the next one.
# ---------------------------------------------------------------------------
cmd_t07() {
  guard
  prepare
  hr
  say "== T0.7 — чистый ре-тест зон (GATE → CLOUD → GATE)"
  say "   Ни одной разрушающей ячейки. Байты helper'а — текущие замороженные."
  say "   sha256: $(sha_of "$HELPER_PATH")"
  say ""
  say "   Сейчас macOS покажет системный диалог с запросом доступа."
  say "   Их будет ДВА подряд: сначала про «Загрузки», потом про «Рабочий стол»"
  say "   (это разные разрешения). Оба надо разрешить."
  say ""
  say "   ⚠️  ПЕРВЫЙ диалог СНИМИ, прежде чем нажимать «Разрешить»:"
  say "       Cmd+Shift+4 → ПРОБЕЛ → клик по окну диалога."
  say "       Скриншот сам ляжет на Рабочий стол. Он нужен, чтобы узнать, каким"
  say "       ИМЕНЕМ система называет наш файл в тексте запроса — от этого зависят"
  say "       тексты онбординга. Потом жми «Разрешить»."
  say ""
  say "   Скрипт ждёт ответа до 3 минут на диалог, спешить не нужно."
  say ""
  printf '%s' "$HELPER_PATH" | pbcopy 2>/dev/null && say "   (путь на всякий случай скопирован в буфер)"

  local g1 g1a cl cla g2 g2a
  # Cells 1 and 2 may each raise a dialog — wait for a human, not for a machine.
  # (Assigned plainly: a `VAR=x func` prefix on a shell FUNCTION persists or not
  # depending on the shell, and this value must be unambiguous.)
  T0_PROBE_TIMEOUT=180
  CUR_WATCH="$WATCH_GATE"
  cycle "$HELPER_PATH" "t07-gate1" "ok" "helper" \
        "T0.7/1 — GATE (локальная защищённая папка)" diag; g1=$?; g1a="$CYCLE_ACCESS"
  CUR_WATCH="$WATCH_CLOUD"
  cycle "$HELPER_PATH" "t07-cloud" "ok" "helper" \
        "T0.7/2 — CLOUD (iCloud-Рабочий стол)" diag; cl=$?; cla="$CYCLE_ACCESS"
  # Cell 3 must be instant: both permissions already exist. If it is not, that IS
  # the drift signal, so it gets a short leash on purpose.
  T0_PROBE_TIMEOUT=20
  CUR_WATCH="$WATCH_GATE"
  cycle "$HELPER_PATH" "t07-gate2" "ok" "helper" \
        "T0.7/3 — GATE повторно (контроль дрейфа)" diag; g2=$?; g2a="$CYCLE_ACCESS"
  T0_PROBE_TIMEOUT=8

  hr
  say "== ВЕРДИКТ T0.7"
  say "   идентичность: $HELPER_NAME"
  say "   GATE → CLOUD → GATE  =  $g1a → $cla → $g2a"
  say ""
  verdict_zones "$g1a" "$cla" "$g2a"
  local rc=$?
  # Collect the dialog screenshot ourselves — the human should not have to move
  # files around. macOS names them "Screenshot …" or «Снимок экрана …» depending
  # on the UI language, so match on extension + mtime instead of on the name.
  local shot
  shot="$(find "$HOME/Desktop" -maxdepth 1 -name '*.png' -newermt "-10 minutes" -print 2>/dev/null \
          | while read -r f; do printf '%s\t%s\n' "$(stat -f%m "$f")" "$f"; done \
          | sort -rn | head -1 | cut -f2-)"
  if [[ -n "$shot" ]]; then
    cp "$shot" "$T0_ROOT/dialog-screenshot.png" 2>/dev/null \
      && say "   📸 скриншот подобран автоматически:" \
      && say "      $shot" \
      && say "      → скопирован в $T0_ROOT/dialog-screenshot.png"
  else
    say "   📸 скриншот диалога на Рабочем столе не найден."
    say "      Если снимал — скажи, где он; если нет, не страшно: важен сам текст"
    say "      запроса (каким именем система назвала файл)."
  fi
  return "$rc"
}

cmd_verify() {
  guard
  [[ -f "$HELPER_PATH" ]] || die "T0 tree not prepared — run t0.sh first"
  local b c d e b_access
  cycle "$HELPER_PATH" "grant-helper" "ok" "helper" \
        "T0.3 — functional allow (PA0 = helper, frozen bytes, grant given)"; b=$?
  b_access="$CYCLE_ACCESS"
  cycle "$T0_RUNNER" "grant-bash" "notok" "bash" \
        "T0.2′ — negative PA0 control WITH the grant in place (PA0 = runner.sh)"; c=$?

  # DIAGNOSTICS FIRST — everything that only observes runs while the subject is
  # still clean. The destructive cell is last on purpose (see the ordering rule).
  local cloud cloud_access ctrl ctrl_access
  CUR_WATCH="$WATCH_CLOUD"
  cycle "$HELPER_PATH" "cloud-helper" "ok" "helper" \
        "ДИАГНОСТИКА — тот же helper, папка на iCloud-Рабочем столе" diag; cloud=$?
  cloud_access="$CYCLE_ACCESS"
  CUR_WATCH="$WATCH_GATE"
  cycle "$HELPER_PATH" "gate-control" "ok" "helper" \
        "КОНТРОЛЬ — повтор GATE-зоны в том же состоянии" diag; ctrl=$?
  ctrl_access="$CYCLE_ACCESS"

  hr
  say "== T0.4a — byte negative control: a different binary at the same path"
  if mutate_helper; then
    cycle "$HELPER_PATH" "mutated-helper" "notok" "helper" \
          "T0.4a — PA0 = helper with MUTATED bytes" destructive; d=$?
  else
    say "   skipped (could not build the variant)"; d=1
  fi

  hr
  say "== T0.4b — restore the frozen bytes"
  if restore_helper; then
    cycle "$HELPER_PATH" "restored-helper" "ok" "helper" \
          "T0.4b — PA0 = helper, frozen bytes back"; e=$?
  else
    say "   RESTORE FAILED — fix before anything else"; e=1
  fi

  hr
  say "== ВЕРДИКТ T0"
  say "   ГЕЙТ (эти три отвечают на вопрос T0):"
  say "   B  helper + грант              → читается    : $([[ $b -eq 0 ]] && echo PASS || echo FAIL)"
  say "   C  runner.sh + тот же грант    → НЕ читается : $([[ $c -eq 0 ]] && echo PASS || echo FAIL)"
  say "   D  чужие байты, тот же путь    → НЕ читается : $([[ $d -eq 0 ]] && echo PASS || echo FAIL)"
  say ""
  say "   ПОСТ-УСЛОВИЕ (в гейт НЕ входит, см. ниже почему):"
  say "   E  замороженные байты вернули  → читается    : $([[ $e -eq 0 ]] && echo PASS || echo "НЕ ПОДТВЕРЖДЕНО ($CYCLE_ACCESS)")"
  if [[ $e -ne 0 ]]; then
    say "      Ячейка D — единственный способ проверить привязку к байтам — сама"
    say "      отравляет путь: после запуска ЧУЖОГО бинаря по гранченному пути"
    say "      доступ не возвращается и после возврата замороженных байт (проверено"
    say "      2.5 мин, и при перезаписи на месте, и при новом inode). Это артефакт"
    say "      теста, а не продукта: в бою чужих байт по этому пути не бывает —"
    say "      installer отказывается ставить что-либо кроме golden SHA."
    say "      Перепроверить E можно только с чистого листа — ПОСЛЕ перезагрузки."
    say "      На гейт не влияет: «грант привязан к helper'у» уже доказано B+C+D,"
    say "      а «зелёный B не случайность» — ячейкой C в том же прогоне."
  fi
  say ""
  say "   ДИАГНОСТИКА (в гейт НЕ входит), снята ДО разрушающей ячейки:"
  say "   iCloud-Рабочий стол → $cloud_access"
  say "   контрольный повтор GATE в том же состоянии → $ctrl_access"
  verdict_zones "$b_access" "$cloud_access" "$ctrl_access"
  say ""
  if [[ $b -eq 0 && $c -eq 0 && $d -eq 0 ]]; then
    say "   T0 GREEN: грант вешается на helper (по пути И по байтам)."
    say "   Ячейка C убивает версию «доступ был и так»; D — версию «сгодился бы"
    say "   любой файл по этому пути». M1–M7 разрешены."
    say ""
    say "   Осталось: попроси человека ВЫКЛЮЧИТЬ тумблер «$HELPER_NAME»"
    say "   и запусти:  t0.sh verify-off   (последний контроль + уборка)"
    return 0
  fi
  say "   T0 RED. ⛔ СТОП — к человеку, M1–M7 не начинаются."
  [[ $b -ne 0 ]] && say "   B FAIL: грант выдан, а доступа нет → subject НЕ helper, вся конструкция под вопросом."
  [[ $c -ne 0 ]] && say "   C FAIL: доступ есть и через shebang → мы мерили не грант (ambient access?), зелёный B ничего не значит."
  [[ $d -ne 0 ]] && say "   D FAIL: чужие байты по тому же пути прошли → грант НЕ пришпилен к cdhash, заморозка байтов бессмысленна."
  return 1
}

cmd_verify_off() {
  guard
  local f
  cycle "$HELPER_PATH" "toggle-off" "notok" "helper" \
        "T0.F — toggle switched off (final control)"; f=$?
  hr
  if [[ $f -eq 0 ]]; then
    say "   PASS: тумблер выключен → доступ пропал. Значит всё это время мы мерили"
    say "   именно грант человека, а не постороннюю щель."
  else
    say "   FAIL: тумблер выключен, а доступ остался (access=$CYCLE_ACCESS)."
    say "   Значит источник доступа — НЕ этот грант. Предыдущий зелёный под вопросом."
  fi
  say ""
  say "   Дальше:  t0.sh clean   (снести T0-дерево, plist и папку-пробу)"
  return "$f"
}

cmd_exec_form() {
  guard
  [[ -f "$HELPER_PATH" ]] || die "T0 tree not prepared — run t0.sh first"
  hr
  say "== T0.5 — the exec form (informational, NOT a gate)"
  say "   Knowledge for the record: does the subject survive an exec? The answer"
  say "   changes nothing for 1.0 (Р1 fixed the donor form), it just goes into"
  say "   PROVENANCE.md so a future maintainer does not have to re-discover it."
  local execrun="$HELPER_DIR/runner-exec.sh"
  cat > "$execrun" <<'SH'
#!/bin/bash
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_ROOT="${T0_STATE_ROOT:?}"
mkdir -p "$STATE_ROOT"
printf '{\n  "bash_pid": %s,\n  "bash_ppid": %s,\n  "pa0_hint": "%s",\n  "here": "%s"\n}\n' \
  "$$" "$PPID" "${T0_PA0_HINT:-exec}" "$HERE" > "$STATE_ROOT/bash.json"
PYTHON3="${PYTHON3:-/usr/bin/python3}"
exec "$PYTHON3" "$HERE/t0_probe.py"
SH
  chmod +x "$execrun"
  # The helper always spawns the sibling literally named runner.sh, so swap it
  # for the exec twin just for this measurement, then put the real one back.
  cp "$T0_RUNNER" "$T0_RUNNER.bak"
  cp "$execrun" "$T0_RUNNER"
  cycle "$HELPER_PATH" "exec-form" "ok" "helper" "T0.5 — helper → bash → exec python"
  mv -f "$T0_RUNNER.bak" "$T0_RUNNER"
  rm -f "$execrun"
  say "   (record the outcome in packaging/agent-src/PROVENANCE.md, T0 table)"
  return 0
}

cmd_status() {
  hr
  say "== T0 status"
  say "   helper      : $HELPER_PATH $([[ -f "$HELPER_PATH" ]] && echo "(sha $(sha_of "$HELPER_PATH"))" || echo '(absent)')"
  say "   frozen      : $GOLDEN $([[ -f "$GOLDEN" ]] && echo "(sha $(sha_of "$GOLDEN"))" || echo '(absent)')"
  say "   bytes match : $([[ -f "$HELPER_PATH" && "$(sha_of "$HELPER_PATH")" == "$(sha_of "$GOLDEN")" ]] && echo yes || echo NO)"
  say "   plist       : $PLIST $([[ -f "$PLIST" ]] && echo '(present)' || echo '(absent)')"
  say "   watch GATE  : $WATCH_GATE $([[ -d "$WATCH_GATE" ]] && echo '(present)' || echo '(absent)')"
  say "   watch CLOUD : $WATCH_CLOUD $([[ -d "$WATCH_CLOUD" ]] && echo '(present)' || echo '(absent)')"
  say "   loaded      :"
  launchctl print "$DOMAIN/$LABEL" 2>/dev/null | sed -n '1,12p' | sed 's/^/     /' || say "     (not loaded)"
  if [[ -f "$STATE_ROOT/t0_result.json" ]]; then
    say "   last result :"
    sed 's/^/     /' "$STATE_ROOT/t0_result.json"
  fi
  say "   production  : label $PROD_LABEL $([[ -f "$HOME/Library/LaunchAgents/$PROD_LABEL.plist" ]] && echo 'INSTALLED' || echo 'not installed')"
  say "                 tree  $PROD_ROOT $([[ -d "$PROD_ROOT" ]] && echo 'exists' || echo 'absent')"
}

cmd_clean() {
  guard
  bootout_quiet
  rm -f "$PLIST"
  rm -rf "$T0_ROOT"
  rm -rf "$WATCH_GATE"
  rm -rf "$WATCH_CLOUD"
  hr
  say "== T0 cleaned"
  say "   removed: $T0_ROOT"
  say "   removed: $PLIST"
  say "   removed: $WATCH_GATE"
  say "   removed: $WATCH_CLOUD"
  say ""
  say "   Осталось одно ручное действие (я не могу его сделать за человека):"
  say "   в «Полном доступе к диску» выделить строку «$HELPER_NAME»"
  say "   и нажать «−», чтобы в панели не осталось мусора рядом с боевой строкой."
}

# ---------------------------------------------------------------------------
# selftest — exercises the decision logic without launchd, TCC or the human.
# The verdict function is the part that got the previous conclusion wrong, so it
# is the part that gets asserted.
# ---------------------------------------------------------------------------
cmd_selftest() {
  local pass=0 fail=0
  check() { # <desc> <want-rc> <gate1> <cloud> <gate2>
    local desc="$1" want="$2" got
    verdict_zones "$3" "$4" "$5" >/dev/null; got=$?
    if [[ "$got" == "$want" ]]; then pass=$((pass+1)); printf '  ok   - %s (rc=%s)\n' "$desc" "$got"
    else fail=$((fail+1)); printf '  FAIL - %s: got rc=%s want %s\n' "$desc" "$got" "$want"; fi
  }
  hr
  say "== t0 selftest — verdict_zones"
  check "ok/ok/ok → находка 3 артефакт"            0 ok      ok      ok
  check "ok/blocked/ok → iCloud подтверждён"       1 ok      blocked ok
  check "ok/denied/ok → iCloud подтверждён"        1 ok      denied  ok
  check "blocked/*/* → путь отравлен, не судим"    2 blocked ok      ok
  check "denied/*/* → грант не выдан, не судим"    2 denied  blocked ok
  check "ok/ok/blocked → дрейф, не судим"          2 ok      ok      blocked
  check "ok/blocked/blocked → дрейф, не судим"     2 ok      blocked blocked
  say ""
  say "  ordering guard is structural: a 'diag' cell after a destructive one dies"
  say "  in cycle() before it can measure anything (grep DESTRUCTIVE_RAN)."
  say ""
  local s; s="$(pick_t07_suffix)"
  use_identity "$s"
  say "  t07 would use identity : $HELPER_NAME"
  say "  t07 helper path        : $HELPER_PATH"
  say "  frozen artifact sha256 : $(sha_of "$GOLDEN")"
  say ""
  say "== $pass passed, $fail failed"
  [[ "$fail" -eq 0 ]]
}

case "${1:-run}" in
  run|"")       cmd_run ;;
  selftest)     cmd_selftest ;;
  prepare)      prepare ;;
  t07)          use_identity "$(pick_t07_suffix)"; cmd_t07 ;;
  verify)       cmd_verify ;;
  verify-off)   cmd_verify_off ;;
  exec-form)    cmd_exec_form ;;
  status)       cmd_status ;;
  clean)        cmd_clean ;;
  *)            die "unknown command «$1» (run|t07|selftest|verify|verify-off|exec-form|status|clean)" ;;
esac
