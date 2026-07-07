"""§agent-update self-check — the contract behind the app's SELF-UPDATE of the agent.

Run it standalone:

    python3 -m agent.selfcheck_agent_update

The bug: after the .app is updated, the background agent staged under
``~/Library/Application Support/mp3-to-m4b/bin/agent/`` can stay on OLD code (new UI
+ old engine → no aac_at/parallel/progress bar). The fix: the app compares the agent
it SHIPS in its bundle (``<App>.app/Contents/Resources/agent/`` — the same source the
installer copies) with the STAGED one and, if they differ, re-runs the bundled
installer to update it. The decision rule lives in ``agent.agent_version`` (single
source of truth); the Swift ``AgentUpdate`` mirrors it. This suite proves that rule at
the source of truth so the Swift detector can trust it — the same relationship
``selfcheck_installer_repoint`` has with ``installer.sh``.

It runs entirely on THROWAWAY trees under a scratch ``MP3TOM4B_SUPPORT_DIR`` and never
touches the user's real agent, plist, or App Support tree. There is no launchd / venv
here at all (this is a pure content-comparison check), but we still set
``MP3TOM4B_SUPPORT_DIR`` / ``MP3TOM4B_LABEL`` / ``MP3TOM4B_NO_LAUNCHCTL`` /
``MP3TOM4B_NO_VENV`` for parity with the isolated-install suites and to make the
"staged lives under $SUPPORT/bin/agent" mapping explicit.

Assertions (the freshness contract):
  identical    fingerprint(staged) == fingerprint(bundled) → UP_TO_DATE;
  changed      one staged ``*.py`` byte-differs               → OUTDATED;
  missing      a bundled ``*.py`` is absent from the staged tree → OUTDATED;
  extra        an extra ``*.py`` only in the staged tree       → OUTDATED;
  no-staged    the staged ``bin/agent`` dir does not exist (app installed, agent
               never staged / wiped) with a real bundled tree  → OUTDATED;
  no-bundled   the bundled tree is unreadable (dev run, no Resources/agent) →
               UNDECIDABLE (→ "don't touch", never a wrong reinstall);
  real-same    a byte-copy of the REAL repo ``agent/`` matches it              → UP_TO_DATE;
  real-drift   the classic bug shape — a copy of the REAL ``agent/`` whose
               ``build_m4b.py`` is reverted to old bytes                        → OUTDATED;
  staged-path  the staged tree the app checks is ``$MP3TOM4B_SUPPORT_DIR/bin/agent``
               (the exact path the installer writes) — a fingerprint taken there
               is non-empty and matches a fingerprint of the same files.

It runs ONLY its own checks (cross-suite orchestration is ``agent.selfcheck_all``'s
job) and returns 0 ⇔ every check passed.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

from agent import agent_version as av

# --- tiny assertion harness (same shape as the sibling self-checks) ----------

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


def _copy_py_tree(src: Path, dst: Path) -> None:
    """Copy the direct ``*.py`` files of ``src`` into a fresh ``dst`` (flat, verbatim)
    — exactly what installer.sh / build-app.sh do when they stage the agent."""
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    for f in sorted(src.glob("*.py")):
        if f.is_file():
            shutil.copy2(f, dst / f.name)


# --- the run ----------------------------------------------------------------


def run() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    real_agent = repo_root / "agent"
    if not (real_agent / "__main__.py").is_file():
        print(f"§agent-update self-check: SKIPPED — real agent dir not found at {real_agent}")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-agentupd-"))
    # Isolation env for parity with the install suites (no real system touched).
    os.environ.setdefault("MP3TOM4B_NO_LAUNCHCTL", "1")
    os.environ.setdefault("MP3TOM4B_NO_VENV", "1")
    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(root / "support")
    os.environ["MP3TOM4B_LABEL"] = "com.arrivarus.mp3tom4b.selfcheck-agentupd"

    # Synthetic bundled tree (stands in for <App>.app/Contents/Resources/agent).
    bundled = root / "bundled"
    bundled.mkdir(parents=True, exist_ok=True)
    (bundled / "__main__.py").write_text("print('new __main__')\n")
    (bundled / "build_m4b.py").write_text("AAC_AT = True  # new engine\n")
    (bundled / "scan.py").write_text("SCHEMA = 2\n")

    print(f"self-check tree: {root}\n  bundled: {bundled}\n"
          f"  support: {os.environ['MP3TOM4B_SUPPORT_DIR']}\n")

    try:
        # === identical: a verbatim copy of the bundled tree is up-to-date =====
        staged = root / "staged"
        _copy_py_tree(bundled, staged)
        check("identical: byte-copy of bundled staged → UP_TO_DATE",
              av.compare(bundled, staged) == av.UP_TO_DATE,
              f"got {av.compare(bundled, staged)!r}")

        # === changed: one staged file byte-differs → OUTDATED =================
        (staged / "build_m4b.py").write_text("AAC_AT = False  # OLD engine\n")
        check("changed: one staged *.py differs → OUTDATED",
              av.compare(bundled, staged) == av.OUTDATED,
              f"got {av.compare(bundled, staged)!r}")

        # === missing: a bundled file absent from the staged tree → OUTDATED ===
        _copy_py_tree(bundled, staged)                 # reset to identical
        (staged / "scan.py").unlink()                  # staged missing a shipped file
        check("missing: bundled file absent in staged → OUTDATED",
              av.compare(bundled, staged) == av.OUTDATED,
              f"got {av.compare(bundled, staged)!r}")

        # === extra: an extra *.py only in the staged tree → OUTDATED ==========
        _copy_py_tree(bundled, staged)                 # reset to identical
        (staged / "ghost.py").write_text("# stray module not in the bundle\n")
        check("extra: staged-only *.py → OUTDATED",
              av.compare(bundled, staged) == av.OUTDATED,
              f"got {av.compare(bundled, staged)!r}")

        # === no-staged: staged dir missing (app installed, agent wiped) =======
        missing_staged = root / "does-not-exist"
        check("no-staged: absent staged dir + real bundled → OUTDATED",
              av.compare(bundled, missing_staged) == av.OUTDATED,
              f"got {av.compare(bundled, missing_staged)!r}")
        check("no-staged: fingerprint(absent dir) is None",
              av.fingerprint(missing_staged) is None)

        # === no-bundled: bundled tree unreadable (dev run) → UNDECIDABLE ======
        _copy_py_tree(bundled, staged)                 # a perfectly good staged tree…
        missing_bundled = root / "no-resources-agent"  # …but no bundled reference
        check("no-bundled: absent bundled dir → UNDECIDABLE (don't touch)",
              av.compare(missing_bundled, staged) == av.UNDECIDABLE,
              f"got {av.compare(missing_bundled, staged)!r}")
        check("no-bundled: is_outdated() is False when undecidable",
              av.is_outdated(missing_bundled, staged) is False)

        # === real-same: a byte-copy of the REAL agent matches it =============
        real_copy = root / "real-copy"
        _copy_py_tree(real_agent, real_copy)
        check("real-same: byte-copy of the REAL agent/ → UP_TO_DATE",
              av.compare(real_agent, real_copy) == av.UP_TO_DATE,
              f"got {av.compare(real_agent, real_copy)!r}")

        # === real-drift: the classic bug shape — old build_m4b.py in a copy ===
        # Copy the real agent, then overwrite build_m4b.py with OLD bytes (as the
        # staged agent stuck on 30.06 code would be) → must read OUTDATED.
        drifted = root / "real-drifted"
        _copy_py_tree(real_agent, drifted)
        (drifted / "build_m4b.py").write_text(
            "# OLD engine: -c:a aac -ar 44100, single-pass, no aac_at/parallel\n")
        check("real-drift: copy of real agent w/ OLD build_m4b.py → OUTDATED",
              av.compare(real_agent, drifted) == av.OUTDATED,
              f"got {av.compare(real_agent, drifted)!r}")

        # === staged-path: the app checks $SUPPORT/bin/agent (installer's path) =
        support = Path(os.environ["MP3TOM4B_SUPPORT_DIR"])
        staged_real = support / "bin" / "agent"
        _copy_py_tree(bundled, staged_real)
        fp = av.fingerprint(staged_real)
        check("staged-path: fingerprint($SUPPORT/bin/agent) is non-empty",
              isinstance(fp, dict) and len(fp) == 3,
              f"files={sorted(fp) if isinstance(fp, dict) else None}")
        check("staged-path: bundled vs $SUPPORT/bin/agent copy → UP_TO_DATE",
              av.compare(bundled, staged_real) == av.UP_TO_DATE,
              f"got {av.compare(bundled, staged_real)!r}")

        return _finish(root)
    finally:
        # Best-effort cleanup of the scratch tree.
        shutil.rmtree(root, ignore_errors=True)


def _finish(root: Path) -> int:
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    failed = [name for name, ok, _ in _RESULTS if not ok]
    print(f"\n§agent-update self-check: {passed}/{total} checks passed")
    if failed:
        print("  FAILED checks: " + "; ".join(failed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
