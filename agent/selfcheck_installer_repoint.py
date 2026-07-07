"""§installer-repoint self-check — the mechanism the SETTINGS «Сменить папку» uses.

Run it standalone:

    python3 -m agent.selfcheck_installer_repoint

The in-app SETTINGS screen re-points the background agent at a NEW watch folder by
re-running the SAME bundled ``packaging/installer.sh`` the Setup screen uses, passing
the chosen folder as ``WATCH_DIR`` (argv[1]). The installer regenerates the
LaunchAgent plist via ``plutil`` and idempotently reloads the agent
(bootout→bootstrap→enable→kickstart). No new re-point mechanism exists — the app
just re-invokes the installer. This check proves that contract at the source of
truth (the installer), so the Swift button can trust it.

It drives the REAL installer on a throwaway tree, with the installer's own
test/dev escape hatches so nothing on the live system is touched:

  · ``MP3TOM4B_NO_LAUNCHCTL=1`` — skip the launchd (re)load (no bootout/bootstrap);
  · ``MP3TOM4B_NO_VENV=1``      — skip venv creation / Pillow install (fast, offline);
  · ``MP3TOM4B_LABEL=<temp>``   — a throwaway LaunchAgent label so the plist path is
                                  ``~/Library/LaunchAgents/<temp>.plist`` (never the
                                  real ``com.arrivarus.mp3tom4b.agent`` one);
  · ``MP3TOM4B_SUPPORT_DIR``    — redirect the whole App Support tree to the scratch
                                  dir (so ``queue/commands`` etc. land there).

Assertions (the re-point contract):
  gen        after ``installer.sh <WATCH_DIR_A>`` the plist exists and is a valid
             plist with ``WatchPaths.0 == WATCH_DIR_A`` AND
             ``EnvironmentVariables.MP3TOM4B_WATCH_DIR == WATCH_DIR_A``;
  commands   ``WatchPaths.1`` is the App Support ``queue/commands`` dir (the second
             watched path the agent needs — a dropped command wakes it);
  repoint    re-running with a DIFFERENT folder (``WATCH_DIR_B``) re-points BOTH
             ``WatchPaths.0`` and ``MP3TOM4B_WATCH_DIR`` to B (idempotent, no dupe
             WatchPaths entry — still exactly two) — this is the exact «Сменить
             папку» action;
  tilde      a literal ``~/…`` folder argument expands to ``$HOME/…`` in the plist
             (the installer normalizes a leading tilde) — matches how the Swift
             field can show/return a tilde path.

It runs ONLY its own checks (cross-suite orchestration is ``agent.selfcheck_all``'s
job) and returns 0 ⇔ every check passed. Requires ``plutil`` (macOS) — if it is
absent the suite says so and exits non-zero (never a silent green).

The LaunchAgent plist is written under the user's real
``~/Library/LaunchAgents/`` (that path is NOT overridable in the installer), but the
LABEL is a unique throwaway, so the file is ``<temp-label>.plist`` and this suite
deletes it (and its scratch tree) on exit — the real agent's plist is untouched.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

# --- tiny assertion harness (same shape as the sibling self-checks) ----------

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


def _installer_path(repo_root: Path) -> Path:
    return repo_root / "packaging" / "installer.sh"


def _run_installer(installer: Path, watch_dir: str, *, support_dir: Path,
                   label: str) -> subprocess.CompletedProcess:
    """Run the real installer in test mode (no launchctl, no venv, temp label)."""
    env = dict(os.environ)
    # Clear any inherited data-tree overrides so ours are authoritative.
    for k in ("MP3TOM4B_SUPPORT_DIR", "MP3TOM4B_WATCH_DIR", "MP3TOM4B_COVER_WEB"):
        env.pop(k, None)
    env["MP3TOM4B_NO_LAUNCHCTL"] = "1"   # never touch the real launchd domain
    env["MP3TOM4B_NO_VENV"] = "1"        # skip venv/Pillow (fast, offline)
    env["MP3TOM4B_SUPPORT_DIR"] = str(support_dir)
    env["MP3TOM4B_LABEL"] = label        # throwaway plist name
    return subprocess.run(
        ["/bin/bash", str(installer), watch_dir],
        capture_output=True, text=True, env=env,
    )


def _read_plist(path: Path) -> dict:
    with open(path, "rb") as fh:
        return plistlib.load(fh)


def _expected_watch(path: Path | str) -> str:
    """The path the installer's ``cd "$WATCH_DIR" && pwd`` yields for WATCH_DIR.

    bash's ``pwd`` returns the LOGICAL cwd (it does NOT resolve symlinks by
    default), so a temp dir like ``/var/folders/…`` stays ``/var/…`` in the plist
    rather than becoming its physical ``/private/var/…``. We therefore compare
    against the plain absolute path (``os.path.abspath``), NOT ``realpath`` —
    matching what the installer actually writes.
    """
    return os.path.abspath(str(path))


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not shutil.which("plutil"):
        print("§installer-repoint self-check: SKIPPED — plutil not found (needs macOS)")
        return 1

    repo_root = Path(__file__).resolve().parent.parent
    installer = _installer_path(repo_root)
    if not installer.is_file():
        print(f"§installer-repoint self-check: SKIPPED — installer not found at {installer}")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-repoint-"))
    support = root / "support"
    watch_a = root / "watch-A"
    watch_b = root / "watch-B"
    support.mkdir(parents=True, exist_ok=True)
    # NOTE: do NOT pre-create watch_a / watch_b — the installer mkdir -p's the
    # WATCH_DIR itself; letting it create them proves that path too.

    # A unique throwaway LaunchAgent label → its own plist file we own + delete.
    label = f"com.arrivarus.mp3tom4b.selfcheck-{uuid.uuid4().hex[:12]}"
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
    commands_dir = support / "queue" / "commands"

    print(f"self-check tree: {root}\n  support: {support}\n"
          f"  label:   {label}\n  plist:   {plist_path}\n")

    try:
        # === gen: install pointing at folder A ==============================
        proc = _run_installer(installer, str(watch_a), support_dir=support, label=label)
        if proc.returncode != 0:
            check("gen: installer.sh exits 0 (test mode, no launchctl/venv)", False,
                  f"rc={proc.returncode}; stderr tail: "
                  + " | ".join((proc.stderr or "").strip().splitlines()[-3:]))
            return _finish(root, plist_path)
        check("gen: installer.sh exits 0 (test mode, no launchctl/venv)", True)

        check("gen: WATCH_DIR (folder A) was created on disk by the installer",
              watch_a.is_dir(), f"watch_a={watch_a}")
        check("gen: LaunchAgent plist was generated at the temp-label path",
              plist_path.is_file(), f"plist={plist_path}")
        if not plist_path.is_file():
            return _finish(root, plist_path)

        pl = _read_plist(plist_path)
        resolved_a = _expected_watch(watch_a)

        watch_paths = pl.get("WatchPaths")
        check("gen: WatchPaths is a 2-entry list [WATCH_DIR, commands]",
              isinstance(watch_paths, list) and len(watch_paths) == 2,
              f"WatchPaths={watch_paths}")
        check("gen: WatchPaths.0 == the chosen watch folder A",
              isinstance(watch_paths, list) and len(watch_paths) >= 1
              and watch_paths[0] == resolved_a,
              f"WatchPaths.0={watch_paths[0] if isinstance(watch_paths, list) and watch_paths else None!r}"
              f" expected={resolved_a!r}")

        env_vars = pl.get("EnvironmentVariables")
        check("gen: EnvironmentVariables.MP3TOM4B_WATCH_DIR == folder A",
              isinstance(env_vars, dict)
              and env_vars.get("MP3TOM4B_WATCH_DIR") == resolved_a,
              f"MP3TOM4B_WATCH_DIR="
              f"{env_vars.get('MP3TOM4B_WATCH_DIR') if isinstance(env_vars, dict) else None!r}"
              f" expected={resolved_a!r}")

        # === commands: WatchPaths.1 is the App Support queue/commands dir ====
        # The installer mkdir -p's COMMANDS_DIR but does NOT cd/pwd it, so the plist
        # carries the path verbatim from $MP3TOM4B_SUPPORT_DIR (no symlink resolve).
        expected_cmds = str(commands_dir)
        check("commands: WatchPaths.1 == App Support queue/commands",
              isinstance(watch_paths, list) and len(watch_paths) >= 2
              and watch_paths[1] == expected_cmds,
              f"WatchPaths.1={watch_paths[1] if isinstance(watch_paths, list) and len(watch_paths) >= 2 else None!r}"
              f" expected={expected_cmds!r}")

        # === repoint: re-run pointing at a DIFFERENT folder B ================
        # This is EXACTLY the Settings «Сменить папку» action: same installer,
        # new WATCH_DIR. Both WatchPaths.0 and the env must move to B, and the
        # WatchPaths list must stay a clean 2-entry list (no dupe accretion).
        proc2 = _run_installer(installer, str(watch_b), support_dir=support, label=label)
        check("repoint: re-run with folder B exits 0 (idempotent re-point)",
              proc2.returncode == 0,
              f"rc={proc2.returncode}; stderr tail: "
              + " | ".join((proc2.stderr or "").strip().splitlines()[-3:]))

        pl2 = _read_plist(plist_path)
        resolved_b = _expected_watch(watch_b)
        wp2 = pl2.get("WatchPaths")
        ev2 = pl2.get("EnvironmentVariables")

        check("repoint: WatchPaths.0 moved to folder B",
              isinstance(wp2, list) and len(wp2) >= 1 and wp2[0] == resolved_b,
              f"WatchPaths.0={wp2[0] if isinstance(wp2, list) and wp2 else None!r}"
              f" expected={resolved_b!r}")
        check("repoint: MP3TOM4B_WATCH_DIR moved to folder B",
              isinstance(ev2, dict) and ev2.get("MP3TOM4B_WATCH_DIR") == resolved_b,
              f"MP3TOM4B_WATCH_DIR="
              f"{ev2.get('MP3TOM4B_WATCH_DIR') if isinstance(ev2, dict) else None!r}"
              f" expected={resolved_b!r}")
        check("repoint: WatchPaths stays a clean 2-entry list (no dupe accretion)",
              isinstance(wp2, list) and len(wp2) == 2,
              f"WatchPaths={wp2}")

        # === tilde: a literal ~/… argument expands to $HOME/… ===============
        # The Swift field may hand back a tilde path; the installer normalizes a
        # leading '~'. We point at a throwaway ~/<unique> dir, assert the plist
        # carries the expanded absolute path, then remove that dir.
        tilde_leaf = f".mp3tom4b-selfcheck-{uuid.uuid4().hex[:10]}"
        tilde_abs = Path.home() / tilde_leaf
        try:
            proc3 = _run_installer(installer, f"~/{tilde_leaf}",
                                   support_dir=support, label=label)
            pl3 = _read_plist(plist_path)
            wp3 = pl3.get("WatchPaths")
            ev3 = pl3.get("EnvironmentVariables")
            expanded = _expected_watch(tilde_abs)
            check("tilde: ~/<leaf> expands to $HOME/<leaf> in WatchPaths.0",
                  proc3.returncode == 0 and isinstance(wp3, list) and wp3
                  and wp3[0] == expanded,
                  f"WatchPaths.0={wp3[0] if isinstance(wp3, list) and wp3 else None!r}"
                  f" expected={expanded!r}")
            check("tilde: ~/<leaf> expands in MP3TOM4B_WATCH_DIR too",
                  isinstance(ev3, dict) and ev3.get("MP3TOM4B_WATCH_DIR") == expanded,
                  f"MP3TOM4B_WATCH_DIR="
                  f"{ev3.get('MP3TOM4B_WATCH_DIR') if isinstance(ev3, dict) else None!r}"
                  f" expected={expanded!r}")
        finally:
            # Clean the throwaway ~/<leaf> dir the installer created.
            if tilde_abs.is_dir():
                shutil.rmtree(tilde_abs, ignore_errors=True)

        return _finish(root, plist_path)
    finally:
        # Always remove the throwaway plist + scratch tree, even on early return.
        try:
            if plist_path.is_file():
                plist_path.unlink()
        except OSError:
            pass


def _finish(root: Path, plist_path: Path) -> int:
    # Remove the throwaway plist here too (idempotent; the finally block also tries).
    try:
        if plist_path.is_file():
            plist_path.unlink()
    except OSError:
        pass
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    failed = [name for name, ok, _ in _RESULTS if not ok]
    print(f"\n§installer-repoint self-check: {passed}/{total} checks passed")
    if failed:
        print("  FAILED checks: " + "; ".join(failed))
    print(f"(temp tree left at {root} for inspection; safe to delete)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
