"""§all — the FLAT self-check runner: every suite, exactly once, no nesting.

Run it:

    python3 -m agent.selfcheck_all

Each ``agent/selfcheck_<name>`` now runs ONLY its own checks (the historical
"--- regression ---" tail that re-launched the other suites as subprocesses is
gone). That tail multiplied: ``status`` re-ran ``queue`` which re-ran ``grouping``
… each spawning real ffmpeg builds many times over, so a single pass ballooned to
~30 min. This runner replaces that nested fan-out with one FLAT pass: it launches
each suite ONCE, in a fixed dependency order, as an isolated subprocess, collects
each suite's ``X/Y + exit`` line, prints a tidy summary, and exits

    0  ⇔  EVERY suite is green (its own ``passed == total`` AND exit 0)
    1  otherwise

No suite launches another (the nesting is gone), so total time ≈ Σ of each suite
once — in the same ballpark as one heavy suite, not its product.

Each child re-derives its own throwaway data tree (it sets ``MP3TOM4B_SUPPORT_DIR``
/ ``MP3TOM4B_WATCH_DIR`` itself); we clear any inherited overrides so a child is
never pinned to a parent's tree. ffmpeg/ffprobe + Pillow are required by the
suites themselves — a suite that is missing a tool SKIPS with a non-zero exit,
which this runner surfaces as a failure (never a silent green).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

# The fixed, dependency-ordered roster. Each entry is (module, summary-marker).
# The marker is the exact prefix each suite prints in its «X/Y checks passed»
# line — Yurka greps these, so the format is preserved verbatim by the suites.
SUITES: list[tuple[str, str]] = [
    ("agent.selfcheck_m0", "§M0 self-check:"),
    # The re-point contract behind the SETTINGS «Сменить папку» button: re-running
    # the bundled installer.sh with a new WATCH_DIR regenerates the plist's
    # WatchPaths[0] + MP3TOM4B_WATCH_DIR (idempotent, no launchctl/venv in test
    # mode). Lightweight (no ffmpeg build) — grouped with the install/state basics.
    ("agent.selfcheck_installer_repoint", "§installer-repoint self-check:"),
    # The freshness contract behind the app's SELF-UPDATE of the agent: the app
    # ships the current agent in its bundle (Contents/Resources/agent) and compares
    # it (sha256 over every *.py, agent.agent_version) with the STAGED one under App
    # Support; a mismatch → re-run the installer to update it. Pure content-compare
    # (no ffmpeg/launchd/venv) — grouped with the install/state basics. Green ⇔ the
    # Swift detector's rule (identical→up-to-date, any drift/missing→outdated, no
    # bundled tree→undecidable/don't-touch) holds at the source of truth.
    ("agent.selfcheck_agent_update", "§agent-update self-check:"),
    ("agent.selfcheck_m05", "§M0.5 self-check:"),
    ("agent.selfcheck_m1", "§M1-vertical self-check:"),
    ("agent.selfcheck_cover", "§cover self-check:"),
    ("agent.selfcheck_cover_pick", "§cover-pick self-check:"),
    ("agent.selfcheck_grouping", "§grouping self-check:"),
    ("agent.selfcheck_queue", "§queue self-check:"),
    ("agent.selfcheck_status", "§status self-check:"),
    ("agent.selfcheck_cancel", "§cancel self-check:"),
    # «Собрать заново» (reconvert). Book-targeted command like cancel — grouped here.
    # Runs its own real build → reconvert → REAL rebuild end-to-end; green ⇔ a done
    # book is re-armed to pending-confirm (fresh token + CLEARED idempotency ledger)
    # AND the follow-up confirm-build with the same key really rebuilds (not deduped),
    # plus source-gone / non-done / bogus-id rejects. No nesting.
    ("agent.selfcheck_reconvert", "§reconvert self-check:"),
    # The app auto-raise (nudge) layer: a NEW pending-confirm book / grouping
    # prompt makes the agent bring the app forward exactly once (rising edge via
    # the notified.json ledger). Runs the production run_scan/drain path with the
    # MP3TOM4B_NUDGE_CMD recorder seam — the REAL app is never opened. Grouped
    # after reconvert because it exercises the confirm-cycle + reconvert flows.
    # Also owns the conscious re-drop signals: source_rev v2 (st_ino+st_dev —
    # a Finder COPY re-arms a done book), the presence ledger (same-volume MOVE
    # out→in re-arms), and the v1→v2 migration (legacy revs/keys upgraded
    # silently — 0 raises at login / after an agent update).
    ("agent.selfcheck_nudge", "§nudge self-check:"),
    # The P1 split engine (plan_parts + split + the dispatcher integration). Runs
    # its own real multi-chapter build→split end-to-end (no nesting); green ⇔ every
    # part is valid (no duplicate chapters, stream-copy, cover, «Часть N из M») and
    # the default split=False still yields one file.
    ("agent.selfcheck_split", "§split self-check:"),
    # The «Быстрый режим» (parallel-groups) engine (Ступень 2 / D15). Runs its own
    # real multi-group build end-to-end; green ⇔ fast mode ships a valid .m4b (marks
    # from probed durations, no drift, cover, faststart), seamless still works, the
    # build_mode toggle reaches the engine, a forced fast-failure falls back, and a
    # cancel gasses ALL parallel children. No nesting.
    ("agent.selfcheck_fast", "§fast self-check:"),
    # The reliability edge-case gate (E1–E16). Last because it exercises the whole
    # build engine end-to-end; green ⇔ zero FAIL (DEFER for not-yet-built layers
    # like E15 is allowed). It prints the same «X/Y checks passed» line and now
    # exits non-zero on any FAIL, so it is a real gate here.
    ("agent.selfcheck_reliability", "§reliability self-check:"),
]

# Parses "<marker> 12/12 checks passed" → (passed, total).
_COUNTS = re.compile(r":\s*(\d+)\s*/\s*(\d+)\s+checks passed")


def _run_one(mod: str, marker: str, repo_root: Path) -> dict:
    """Run a single suite once (flat) and return a result record.

    The child gets a CLEAN environment w.r.t. our data-tree overrides so it
    derives its own scratch dirs; everything else (PATH for ffmpeg, etc.) is
    inherited.
    """
    env = dict(os.environ)
    for k in ("MP3TOM4B_SUPPORT_DIR", "MP3TOM4B_WATCH_DIR", "MP3TOM4B_COVER_WEB",
              "MP3TOM4B_NUDGE_CMD"):
        env.pop(k, None)
    # The build-focused suites create their fixture mp3s instantly (already stable),
    # so the E10 copy-stability debounce (scan.STABILITY_DEBOUNCE_S) would only add
    # dead wait per freshly-armed book. Disable it for children → the flat run stays
    # fast. The reliability suite OWNS the debounce test (E10) and sets its own
    # non-zero window for just that case, overriding this, so coverage is intact.
    env["MP3TOM4B_STABILITY_DEBOUNCE_S"] = "0"

    t0 = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", mod],
        cwd=str(repo_root), capture_output=True, text=True, env=env,
    )
    elapsed = time.monotonic() - t0

    summary = next((ln.strip() for ln in proc.stdout.splitlines() if marker in ln),
                   "(no summary line)")
    m = _COUNTS.search(summary)
    passed = int(m.group(1)) if m else None
    total = int(m.group(2)) if m else None
    # Green ⇔ the child exited 0 AND its own passed == total (when parseable).
    ok = proc.returncode == 0 and (m is None or passed == total)
    return {
        "mod": mod,
        "rc": proc.returncode,
        "passed": passed,
        "total": total,
        "summary": summary,
        "elapsed": elapsed,
        "ok": ok,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def run() -> int:
    repo_root = Path(__file__).resolve().parent.parent

    print("§all self-check — FLAT runner (each suite once, no nesting)")
    print(f"  python:   {sys.executable}")
    print(f"  repo:     {repo_root}")
    print(f"  suites:   {len(SUITES)} → "
          + " → ".join(m.split('.')[-1].replace('selfcheck_', '') for m, _ in SUITES))
    print()

    results: list[dict] = []
    t_all = time.monotonic()
    for mod, marker in SUITES:
        short = mod.split(".")[-1].replace("selfcheck_", "")
        print(f"▶ {short} …", flush=True)
        res = _run_one(mod, marker, repo_root)
        results.append(res)
        mark = "PASS" if res["ok"] else "FAIL"
        xy = (f"{res['passed']}/{res['total']}"
              if res["passed"] is not None else "?/?")
        print(f"  [{mark}] {short:<11} {xy:>7}  exit={res['rc']}  "
              f"{res['elapsed']:6.1f}s")
        if not res["ok"]:
            # Surface the failing suite's own summary line + a stderr tail so a
            # red run is diagnosable straight from this runner's output.
            print(f"        summary: {res['summary']}")
            tail = (res["stderr"] or "").strip().splitlines()[-5:]
            for ln in tail:
                print(f"        stderr| {ln}")
        print()

    total_elapsed = time.monotonic() - t_all

    # === summary table ======================================================
    green = sum(1 for r in results if r["ok"])
    print("─" * 56)
    print(f"§all self-check: {green}/{len(results)} suites green  "
          f"(total {total_elapsed:.1f}s)")
    for r in results:
        short = r["mod"].split(".")[-1].replace("selfcheck_", "")
        mark = "PASS" if r["ok"] else "FAIL"
        xy = f"{r['passed']}/{r['total']}" if r["passed"] is not None else "?/?"
        print(f"  [{mark}] {short:<11} {xy:>7}  exit={r['rc']}  {r['elapsed']:6.1f}s")
    failed = [r["mod"].split(".")[-1].replace("selfcheck_", "")
              for r in results if not r["ok"]]
    if failed:
        print("  FAILED suites: " + "; ".join(failed))

    # Exit 0 ⇔ EVERY suite is green. Flat, honest, no nested re-runs.
    return 0 if all(r["ok"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(run())
