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

ISOLATION IS THE RUNNER'S JOB, NOT THE SUITE'S (.patches/005)
    Twice in one day a self-check reached the user's real system: once a negative
    control bootstrapped a live launchd job, once a suite redirected
    ``MP3TOM4B_WATCH_DIR`` but not ``MP3TOM4B_SUPPORT_DIR`` and journalled into the
    real Application Support. Both were fixed where they happened — and that is
    exactly the problem: the next suite starts from scratch and re-arms the mine.
    The neighbour hit this class THREE times, once overwriting the user's live
    plist. So isolation stopped being a matter of author discipline:

      · this runner ARMS every redirection for each child — support tree, watch
        folder, LaunchAgent label, LaunchAgents dir, ``TMPDIR`` — inside one
        sandbox it owns. A suite that also sets its own (all of today's do) simply
        wins inside that sandbox; a suite that forgets is covered for free;
      · a redirection that is missing, or that points OUTSIDE the sandbox, is a
        LOUD refusal to launch the suite — never a silent fallback to the real
        path, which is precisely how the first incident looked green while writing
        to the live system;
      · a ``blast_radius`` snapshot of the production artifacts is taken before AND
        after EVERY suite (App Support, ``~/Library/LaunchAgents/*mp3*``, the agent
        log, the default watch folder, loaded launchd jobs). Any difference fails
        the run and names the suite that caused it.

    Because ``TMPDIR`` is redirected too, each suite's own ``mkdtemp`` tree lands
    inside the sandbox: a green suite's tree is deleted with it, a FAILED suite's
    tree is kept and its path printed (a red run has to stay diagnosable), and an
    interrupted run cleans up after itself instead of leaving gigabytes behind.

ffmpeg/ffprobe + Pillow are required by the suites themselves — a suite that is
missing a tool SKIPS with a non-zero exit, which this runner surfaces as a failure
(never a silent green).
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import selfcheck_blast_radius as blast_radius

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
    # WHICH BOOK the confirm window presents. The queue's «Подтвердить» on a row
    # must open THAT book — it opened the first one in v0.9 (the row's book was
    # discarded), so «Собрать» could build a book the user never chose. The rule
    # is now one pure function (ShowcaseState.presentedBook) shared by the queue
    # pick and the agent's auto-surface; this suite compiles the Foundation-only
    # app sources + app/selfcheck_routing.swift and runs those assertions. Needs
    # swiftc, no ffmpeg/launchd/state tree — grouped with the lightweight basics.
    ("agent.selfcheck_app_routing", "§app-routing self-check:"),
    ("agent.selfcheck_m05", "§M0.5 self-check:"),
    ("agent.selfcheck_m1", "§M1-vertical self-check:"),
    ("agent.selfcheck_cover", "§cover self-check:"),
    ("agent.selfcheck_cover_pick", "§cover-pick self-check:"),
    ("agent.selfcheck_grouping", "§grouping self-check:"),
    ("agent.selfcheck_queue", "§queue self-check:"),
    ("agent.selfcheck_status", "§status self-check:"),
    ("agent.selfcheck_cancel", "§cancel self-check:"),
    # M3 — the agent must never leave an orphaned ffmpeg. Sibling of ``cancel``: same
    # teardown machinery, but triggered by the PROCESS dying rather than by the user.
    # Kills real builds with a real SIGTERM (through bin/runner.sh, i.e. the shape
    # ``launchctl bootout`` produces) and asserts by PID that every encoder died, the
    # temps were swept, no partial .m4b was published and the exit code is 143 —
    # WITH a negative control that removes the handler and proves the orphan appears.
    # Also covers the progress deadline on a frozen (SIGSTOP'd) encoder.
    ("agent.selfcheck_signals", "§signals self-check:"),
    # M4 — the watch-folder ACCESS GATE. Sibling of ``signals``: both are about the
    # process surviving something it cannot control. Here it is macOS declining to
    # answer at all (measured: with an attributable Mach-O runner and no grant,
    # ``os.listdir`` never returns — the system wants to ask the human and a
    # background LaunchAgent cannot show the dialog). Green ⇔ the probe always
    # answers (`blocked` is a verdict, not a hang), a refused/absent folder never
    # re-arms the library (Р3), «Проверить снова» moves ``folder_access_ts``
    # unconditionally, a signal mid-drain leaves untouched books alone, and the
    # phase deadline ends a run that wedges where the probe cannot see. Reproduces
    # the wedge without TCC (a FIFO with no writer) and carries three negative
    # controls that prove each guard is what keeps the suite green.
    ("agent.selfcheck_access", "§access self-check:"),
    # «Собрать заново» (reconvert). Book-targeted command like cancel — grouped here.
    # Runs its own real build → reconvert → REAL rebuild end-to-end; green ⇔ a done
    # book is re-armed to pending-confirm (fresh token + CLEARED idempotency ledger)
    # AND the follow-up confirm-build with the same key really rebuilds (not deduped),
    # plus source-gone / non-done / bogus-id rejects. No nesting.
    ("agent.selfcheck_reconvert", "§reconvert self-check:"),
    # «Пропустить» (skip). Book-targeted like cancel/reconvert — grouped with them.
    # Green ⇔ BOTH halves of the contract hold: the mark HOLDS (a skipped book is
    # not re-armed by any later scan and never raises the app) AND the mark is not
    # permanent — a conscious re-drop resurrects the book through BOTH macOS drop
    # shapes: COPY (new inodes → new source_rev) and MOVE out→in (inode survives →
    # presence ledger). That second half is lesson .patches/004 encoded as a gate.
    # Also covers «Вернуть» (a reconvert of a skipped book) and the status guards.
    ("agent.selfcheck_skip", "§skip self-check:"),
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
    # The guard this runner itself relies on (.patches/005). It has to stay green
    # on the user's live agent ticking through the run AND red on a self-check
    # writing into the install — both halves are proven here against a SYNTHETIC
    # install, so the user's real one is only ever read. Last in the roster: if it
    # is red, every blast_radius verdict above it is suspect.
    ("agent.selfcheck_blast_radius", "§blast-radius self-check:"),
]

# Parses "<marker> 12/12 checks passed" → (passed, total).
_COUNTS = re.compile(r":\s*(\d+)\s*/\s*(\d+)\s+checks passed")


# ── Isolation (.patches/005) ─────────────────────────────────────────────────
#: Every redirection the runner arms for a child. Each MUST end up pointing inside
#: the run's sandbox; anything else aborts the run instead of quietly using the
#: real path. ``TMPDIR`` is in the list on purpose — it is what pulls each suite's
#: own ``mkdtemp`` tree into the sandbox so it can be cleaned up.
_REQUIRED_PATH_OVERRIDES = (
    "MP3TOM4B_SUPPORT_DIR",
    "MP3TOM4B_WATCH_DIR",
    "MP3TOM4B_LAUNCHAGENTS_DIR",
    "TMPDIR",
)
#: Not a path — a launchd label. It only has to exist and to differ from the
#: product's, so that a bootstrap escaping ``NO_LAUNCHCTL`` cannot replace the
#: user's job (that is incident #1 in .patches/005).
_REQUIRED_LABEL = "MP3TOM4B_LABEL"
PROD_LABEL = "com.arrivarus.mp3tom4b.agent"

# The guard that decides whether the live system was touched lives in its own
# module, with its own suite: since 1.0 shipped there is a REAL install on this
# machine, its agent fires every 300 s, and a full run always overlaps a tick. So
# the guard has to tell "a self-check wrote here" from "the user's agent did its
# job" — see :mod:`agent.selfcheck_blast_radius` for how that attribution works and
# what stays strict regardless (plists, PA0, helper bytes, jobs, the tree itself).
_prod_snapshot = blast_radius.snapshot


def _prod_diff(before: dict, after: dict) -> list[str]:
    return blast_radius.diff(before, after)


def _child_env(sandbox: Path, short: str) -> dict:
    """The fully-redirected environment one suite runs in.

    Raises instead of returning a half-armed environment: a suite that runs
    without isolation looks green while writing to the user's system, which is the
    exact failure this function exists to make impossible.
    """
    home = sandbox / short
    for sub in ("support", "watch", "LaunchAgents", "tmp"):
        (home / sub).mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update({
        "MP3TOM4B_SUPPORT_DIR": str(home / "support"),
        "MP3TOM4B_WATCH_DIR": str(home / "watch"),
        "MP3TOM4B_LAUNCHAGENTS_DIR": str(home / "LaunchAgents"),
        # A label that cannot collide with the product's, so even a bootstrap that
        # escapes NO_LAUNCHCTL cannot replace the user's job.
        "MP3TOM4B_LABEL": f"com.arrivarus.mp3tom4b.selfcheck-{os.getpid()}-{short}",
        "TMPDIR": str(home / "tmp"),
    })
    # Suite-local conveniences (not isolation): offline cover chain, no copy-
    # stability wait for fixtures that are already stable. The reliability suite
    # owns the debounce test and overrides this for that one case.
    env.pop("MP3TOM4B_NUDGE_CMD", None)
    env["MP3TOM4B_COVER_WEB"] = "0"
    env["MP3TOM4B_STABILITY_DEBOUNCE_S"] = "0"

    missing = [k for k in _REQUIRED_PATH_OVERRIDES + (_REQUIRED_LABEL,)
               if not env.get(k)]
    if missing:
        raise RuntimeError(
            f"isolation not armed for {short}: {', '.join(missing)} unset — "
            "refusing to run a suite against the live system (.patches/005)"
        )
    root = str(sandbox.resolve())
    outside = [k for k in _REQUIRED_PATH_OVERRIDES
               if not str(Path(env[k]).resolve()).startswith(root)]
    if outside:
        raise RuntimeError(
            f"isolation not armed for {short}: {', '.join(outside)} points outside "
            f"the sandbox {root} — refusing to run (.patches/005)"
        )
    if env[_REQUIRED_LABEL] == PROD_LABEL:
        raise RuntimeError(
            f"isolation not armed for {short}: MP3TOM4B_LABEL is the PRODUCTION "
            f"label {PROD_LABEL} — refusing to run (.patches/005)"
        )
    return env


def _run_one(mod: str, marker: str, repo_root: Path, sandbox: Path) -> dict:
    """Run a single suite once (flat), fully isolated, and return a result record.

    Everything the suite could write through is redirected into ``sandbox`` by
    :func:`_child_env`; everything else (PATH for ffmpeg, etc.) is inherited.
    """
    short = mod.split(".")[-1].replace("selfcheck_", "")
    env = _child_env(sandbox, short)

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


def _sweep(sandbox: Path, keep: list[str]) -> None:
    """Delete the sandbox, keeping only the named suites' trees (failed ones).

    A green run leaves nothing behind — that is what stops the "272 abandoned
    temp dirs / 5 GB" drift. A red one keeps exactly the trees needed to diagnose
    it, and prints where they are.
    """
    try:
        if not keep:
            shutil.rmtree(sandbox, ignore_errors=True)
            return
        for child in sandbox.iterdir():
            if child.name not in keep:
                shutil.rmtree(child, ignore_errors=True)
    except OSError:
        pass


def run() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    sandbox = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-all-"))

    # An interrupted run must not leave its tree behind either (the old behaviour
    # is what accumulated gigabytes of abandoned fixtures). SIGTERM gets the same
    # treatment as Ctrl-C; SIGKILL cannot be caught, and nothing can be done there.
    def _bail(signum, frame):  # noqa: ANN001 - stdlib handler shape
        print(f"\n  interrupted ({signal.Signals(signum).name}) — "
              f"cleaning up {sandbox}", flush=True)
        shutil.rmtree(sandbox, ignore_errors=True)
        os._exit(130)

    for signame in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, signame, None)
        if signum is not None:
            try:
                signal.signal(signum, _bail)
            except (OSError, ValueError, RuntimeError):
                pass

    print("§all self-check — FLAT runner (each suite once, no nesting)")
    print(f"  python:   {sys.executable}")
    print(f"  repo:     {repo_root}")
    print(f"  sandbox:  {sandbox}  (every suite runs redirected INTO this)")
    print(f"  suites:   {len(SUITES)} → "
          + " → ".join(m.split('.')[-1].replace('selfcheck_', '') for m, _ in SUITES))
    print()

    results: list[dict] = []
    t_all = time.monotonic()
    baseline = _prod_snapshot()
    try:
        for mod, marker in SUITES:
            short = mod.split(".")[-1].replace("selfcheck_", "")
            print(f"▶ {short} …", flush=True)
            before = _prod_snapshot()
            try:
                res = _run_one(mod, marker, repo_root, sandbox)
            except RuntimeError as exc:
                # Isolation could not be armed. That is a RED suite, loudly — never
                # a quiet fallback to the real paths (.patches/005 rule 3).
                res = {"mod": mod, "rc": None, "passed": None, "total": None,
                       "summary": f"ISOLATION NOT ARMED — {exc}", "elapsed": 0.0,
                       "ok": False, "stdout": "", "stderr": str(exc)}
            # blast_radius, per suite: a suite that touched the live system is RED
            # even if all of its own checks passed (.patches/005).
            damage = _prod_diff(before, _prod_snapshot())
            res["damage"] = damage
            if damage:
                res["ok"] = False
            results.append(res)
            mark = "PASS" if res["ok"] else "FAIL"
            xy = (f"{res['passed']}/{res['total']}"
                  if res["passed"] is not None else "?/?")
            print(f"  [{mark}] {short:<11} {xy:>7}  exit={res['rc']}  "
                  f"{res['elapsed']:6.1f}s")
            for line in damage:
                print(f"        BLAST RADIUS — {short} touched the live system: {line}")
            if not res["ok"]:
                # Surface the failing suite's own summary line + its failing checks,
                # and PERSIST the full output next to its fixtures. A truncated tail
                # is how an intermittent failure becomes unattributable: the run that
                # caught it is the only one that had the evidence.
                print(f"        summary: {res['summary']}")
                for ln in res["stdout"].splitlines():
                    if "[FAIL]" in ln:
                        print(f"        {ln.strip()[:300]}")
                try:
                    out_dir = sandbox / short
                    out_dir.mkdir(parents=True, exist_ok=True)
                    (out_dir / "stdout.txt").write_text(res["stdout"] or "",
                                                        encoding="utf-8")
                    (out_dir / "stderr.txt").write_text(res["stderr"] or "",
                                                        encoding="utf-8")
                    print(f"        full output: {out_dir}/stdout.txt")
                except OSError:
                    pass
                tail = (res["stderr"] or "").strip().splitlines()[-5:]
                for ln in tail:
                    print(f"        stderr| {ln}")
            print()
    except KeyboardInterrupt:
        print(f"\n  interrupted — cleaning up {sandbox}", flush=True)
        shutil.rmtree(sandbox, ignore_errors=True)
        return 130

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

    # blast_radius over the WHOLE run, in case something drifted between suites.
    overall = _prod_diff(baseline, _prod_snapshot())
    if overall:
        for line in overall:
            print(f"  BLAST RADIUS (whole run): {line}")
    else:
        final = _prod_snapshot()
        state = ("live install present — its own agent ticks are attributed and "
                 "allowed" if final.get("live") else "no live install on this machine")
        print(f"  blast_radius: no self-check touched the user's install "
              f"({state})")

    _sweep(sandbox, keep=failed)
    if failed:
        print(f"  fixtures of the failed suites kept at {sandbox}")

    # Exit 0 ⇔ EVERY suite is green AND nothing reached the live system.
    return 0 if (all(r["ok"] for r in results) and not overall) else 1


if __name__ == "__main__":
    sys.exit(run())
