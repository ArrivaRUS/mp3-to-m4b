#!/usr/bin/env python3
"""t0_log.py — correlate ONE tccd authorisation request with our process chain.

`launchctl print` proves which PA0 launchd loaded; `codesign` proves which image
is on disk. Neither proves what tccd used as the *subject* of the decision —
only tccd's own log does, and only if all the lines are stitched together by the
SAME ``msgID`` (arch/plan-binrunner-mp3-v2.md §4).

Usage:
    t0_log.py --start "2026-07-25 12:00:00" --py-pid 1234 \
              --helper-path /path/to/mp3-to-m4b-agent-t0 \
              --expect helper|bash [--raw /path/to/dump.log]

Prints a human report and, as its last line, a machine-readable verdict:

    LOG_VERDICT=green      the subject/responsible is what --expect demanded
    LOG_VERDICT=red        the subject/responsible is something else
    LOG_VERDICT=redacted   tccd logged the request but the fields are <private>
    LOG_VERDICT=noevidence tccd logged nothing we can tie to this run

`redacted` / `noevidence` are NOT failures and NOT passes: on those the gate
falls back to the behavioural matrix, which is the stronger evidence anyway
(donor lesson 020: believe the actual action, never the panel — and never a
missing log line either). stdlib only, runs on the system python 3.9.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

PREDICATE = 'process == "tccd" AND subsystem == "com.apple.TCC"'
MSGID_RE = re.compile(r"msgID=([0-9a-fA-F.\-]+)")
SUBJECT_RE = re.compile(r"(?:AUTHREQ_SUBJECT:[^\n]*?subject=|\bSub:\s*)([^\s,]+)")
RESULT_RE = re.compile(r"\bresult=([A-Za-z]+)")
PID_RE = re.compile(r"\bpid=(\d+)")
BINPATH_RE = re.compile(r"\bbinary_path=([^,}]+)")


def run_log_show(start: str) -> str:
    """Fetch the tccd slice of the unified log. ndjson first, compact as fallback."""
    for style in ("ndjson", "compact"):
        cmd = ["log", "show", "--start", start, "--info", "--debug",
               "--style", style, "--predicate", PREDICATE]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except Exception as exc:                                  # pragma: no cover
            sys.stderr.write("t0_log: `log show` failed (%s): %s\n" % (style, exc))
            continue
        if out.returncode != 0:
            sys.stderr.write("t0_log: `log show --style %s` rc=%s: %s\n"
                             % (style, out.returncode, out.stderr.strip()[:400]))
            continue
        if style == "ndjson":
            msgs = []
            for line in out.stdout.splitlines():
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                msg = rec.get("eventMessage")
                if msg:
                    msgs.append("%s %s" % (rec.get("timestamp", ""), msg))
            if msgs:
                return "\n".join(msgs)
        else:
            if out.stdout.strip():
                return out.stdout
    return ""


def brace_block(text: str, key: str) -> str:
    """Return the balanced ``{...}`` that follows ``key=`` (nested braces ok)."""
    i = text.find(key + "={")
    if i < 0:
        return ""
    i += len(key) + 1
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return ""


def field(block: str, rx: re.Pattern) -> str:
    m = rx.search(block)
    return m.group(1).strip() if m else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--py-pid", type=int, required=True)
    ap.add_argument("--helper-path", required=True)
    ap.add_argument("--expect", choices=("helper", "bash"), required=True)
    ap.add_argument("--raw")
    args = ap.parse_args()

    text = run_log_show(args.start)
    if args.raw and text:
        with open(args.raw, "w", encoding="utf-8") as fh:
            fh.write(text)

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        print("  tccd log: no com.apple.TCC events since %s" % args.start)
        print("LOG_VERDICT=noevidence")
        return 0

    # Group every line by its msgID — that is the only honest way to say
    # "these lines describe THE SAME authorisation request".
    groups = {}
    for ln in lines:
        m = MSGID_RE.search(ln)
        if not m:
            continue
        groups.setdefault(m.group(1), []).append(ln)

    helper_name = args.helper_path.rsplit("/", 1)[-1]
    want = args.helper_path if args.expect == "helper" else "/bin/bash"

    # Candidates: a group that names our python pid, or (weaker) our binaries.
    candidates = []
    for msgid, glines in groups.items():
        blob = "\n".join(glines)
        accessing = brace_block(blob, "accessing")
        responsible = brace_block(blob, "responsible")
        requesting = brace_block(blob, "requesting")
        acc_pid = field(accessing, PID_RE)
        score = 0
        if acc_pid and int(acc_pid) == args.py_pid:
            score = 3
        elif re.search(r"\bpid=%d\b" % args.py_pid, blob):
            score = 2
        elif helper_name in blob or "/bin/bash" in blob:
            score = 1
        if score:
            candidates.append((score, msgid, glines, accessing, responsible, requesting))

    if not candidates:
        print("  tccd log: %d event(s), none tied to py_pid=%d (%d msgID groups)"
              % (len(lines), args.py_pid, len(groups)))
        print("LOG_VERDICT=noevidence")
        return 0

    candidates.sort(key=lambda c: -c[0])
    score, msgid, glines, accessing, responsible, requesting = candidates[0]
    blob = "\n".join(glines)

    subject = field(blob, SUBJECT_RE)
    result = field(blob, RESULT_RE)
    acc_pid = field(accessing, PID_RE)
    acc_bin = field(accessing, BINPATH_RE)
    resp_bin = field(responsible, BINPATH_RE)
    req_bin = field(requesting, BINPATH_RE)

    print("  msgID=%s  (match confidence %d/3, %d line(s))" % (msgid, score, len(glines)))
    print("    accessing   : pid=%s binary_path=%s" % (acc_pid or "?", acc_bin or "?"))
    print("    requesting  : %s" % (req_bin or "?"))
    print("    responsible : %s" % (resp_bin or "?"))
    print("    subject     : %s" % (subject or "?"))
    print("    result      : %s" % (result or "?"))
    print("    --- raw lines for this msgID ---")
    for ln in glines:
        print("    | %s" % ln.strip()[:400])

    subj_fields = [f for f in (subject, resp_bin) if f]
    if not subj_fields or all("<private>" in f for f in subj_fields):
        print("  tccd redacted the subject/responsible fields (<private>).")
        print("LOG_VERDICT=redacted")
        return 0

    hit = any(want in f for f in subj_fields)
    other = "/bin/bash" if args.expect == "helper" else args.helper_path
    contaminated = any(other in f for f in subj_fields)

    if hit and not contaminated:
        print("  subject/responsible == %s  (expected: %s)" % (want, args.expect))
        print("LOG_VERDICT=green")
    else:
        print("  subject/responsible is NOT %s (expected: %s)" % (want, args.expect))
        print("LOG_VERDICT=red")
    return 0


if __name__ == "__main__":
    sys.exit(main())
