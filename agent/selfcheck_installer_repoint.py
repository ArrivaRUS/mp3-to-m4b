"""§installer self-check — the install CONTRACT the app and the grant depend on.

Run it standalone:

    python3 -m agent.selfcheck_installer_repoint

It drives the REAL ``packaging/installer.sh`` on throwaway trees. Historically this
suite proved one thing (the SETTINGS «Сменить папку» re-point). Release 1.0 turns
the installer into the load-bearing piece of the folder-access design, so the suite
now proves the whole M1 contract (arch/plan-binrunner-mp3-v2.md §7.1):

  gen/commands/repoint/tilde  the original re-point contract (regression)
  bundle_layout   the .app Resources layout (installer + runner.sh + agent/ + helper
                  side by side) installs exactly like the checkout layout
  pa0             ProgramArguments is EXACTLY ONE element and it is the frozen helper
  interval        StartInterval == 300 (Р4), RunAtLoad, ThrottleInterval, 2 WatchPaths
  generation      a fresh UUID per install, present in BOTH plist and receipt
  receipt_last    the receipt is written LAST (absent on every failed install)
  preserve        a re-install never churns the helper (inode+mtime) — the grant
                  is pinned to those bytes at that path
  golden_src      a corrupted SOURCE helper is refused before anything is written
  golden_dst      a destination corrupted after the copy is refused, no plist
  golden_both     src and dst corrupted IDENTICALLY is still refused (blocker B5:
                  a src↔dst compare cannot see this — two broken files are equal)
  nosymlink       a symlinked bin dir is refused (the grant is keyed to a real path)
  heal            a v0.9 plist (PA0 = runner.sh) is healed to the helper
  lock            a second concurrent installer refuses; a stale lock is taken over
  busy_refuse     an install during a live build refuses and changes nothing
  rollback        a failure mid-install restores the plist + the agent package and
                  leaves the OLD receipt (so the app sees "did not complete")
  rollback_pa0    Р5: a rollback never re-points PA0 back at runner.sh
  verify_pa0      B3: the installer asks launchctl what is REALLY loaded and fails
                  on a wrong / two-element PA0 (a correct plist on disk is not proof)
  access_wait     the installer waits for the agent's first access probe (addendum §5.2)
  repair_only     ``--repair-launchd-only`` re-bakes the plist strictly OFFLINE:
                  no ffmpeg, no ffprobe, no pip, no venv (blocker B2)
  downgrade       an older bundle refuses to "update" a newer install (M11f)
  engine_env      the documented FFMPEG/FFPROBE overrides still reach the plist,
                  and FFMPEG_VERSION is parsed the way agent/scan.py parses it
  latch           the test overrides (SUPPORT_DIR / LABEL / LAUNCHAGENTS_DIR) are
                  refused unless the test latch is armed — one forgotten env var
                  must never rewrite the production agent (neighbor .patches/015)
  blast_radius    the suite itself left the LIVE system untouched: no throwaway job
                  loaded, no real App Support tree, no real log, no real plist

NEGATIVE CONTROLS. Every load-bearing guard is also proven to be LOAD-BEARING: the
suite builds a MUTANT installer with that guard's line(s) deleted (they carry a
``# [guard:<tag>]`` marker) and asserts the mutant now fails the very check the
guard exists for. A guard that can be removed without turning a check red is not a
guard. Each mutation also asserts the tag was actually present, so renaming a tag
can never silently turn a negative control into a no-op.

ISOLATION. Nothing outside the temp tree is touched:
  · ``MP3TOM4B_TEST_MODE`` + ``MP3TOM4B_TEST_ROOT`` arm the installer's own latch;
  · ``MP3TOM4B_SUPPORT_DIR`` puts the whole App Support tree in the temp dir;
  · ``MP3TOM4B_LAUNCHAGENTS_DIR`` puts the plist in the temp dir (so the real
    ``~/Library/LaunchAgents`` is never written, not even with a throwaway label);
  · ``MP3TOM4B_LABEL`` is a unique throwaway label;
  · ``MP3TOM4B_NO_LAUNCHCTL=1`` for most scenarios; where the launchd handshake
    itself is under test, a STUB ``launchctl`` on PATH answers instead — the real
    launchd is never contacted and no system dialog can ever appear.

Requires ``plutil`` (macOS). If it is absent the suite says so and exits non-zero
(never a silent green).
"""

from __future__ import annotations

import os
import plistlib
import json
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


REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLER = REPO_ROOT / "packaging" / "installer.sh"
HELPER_SRC = REPO_ROOT / "packaging" / "mp3-to-m4b-agent"
RUNNER_SRC = REPO_ROOT / "bin" / "runner.sh"
AGENT_SRC = REPO_ROOT / "agent"

# The frozen artifact's identity (packaging/agent-src/PROVENANCE.md). The installer
# carries the same constant; this suite pins it independently so a silent edit of
# EITHER side shows up as a failure.
GOLDEN_SHA = "791d020d42477755fe3c46070699421280c2dd7e5f248da59f3f826a5bdbc079"

# Env keys that must never leak into a child from the caller's shell.
_INHERITED = (
    "MP3TOM4B_SUPPORT_DIR", "MP3TOM4B_WATCH_DIR", "MP3TOM4B_COVER_WEB",
    "MP3TOM4B_LABEL", "MP3TOM4B_LAUNCHAGENTS_DIR", "MP3TOM4B_TEST_MODE",
    "MP3TOM4B_TEST_ROOT", "MP3TOM4B_TEST_HOOK", "MP3TOM4B_NO_LAUNCHCTL",
    "MP3TOM4B_NO_VENV", "MP3TOM4B_SRC_DIR", "MP3TOM4B_VERSION",
    "MP3TOM4B_ACCESS_WAIT_S", "MP3TOM4B_ALLOW_DOWNGRADE", "FFMPEG", "FFPROBE",
)


# --- running the installer ---------------------------------------------------


def _env(*, test_root: Path, support: Path, label: str, la_dir: Path,
         latched: bool = True, extra: dict | None = None) -> dict:
    env = dict(os.environ)
    for k in _INHERITED:
        env.pop(k, None)
    if latched:
        env["MP3TOM4B_TEST_MODE"] = "1"
        env["MP3TOM4B_TEST_ROOT"] = str(test_root)
    env["MP3TOM4B_SUPPORT_DIR"] = str(support)
    env["MP3TOM4B_LABEL"] = label
    env["MP3TOM4B_LAUNCHAGENTS_DIR"] = str(la_dir)
    env["MP3TOM4B_NO_LAUNCHCTL"] = "1"
    env["MP3TOM4B_NO_VENV"] = "1"
    env["MP3TOM4B_ACCESS_WAIT_S"] = "0"
    if extra:
        for k, v in extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return env


def _run(installer: Path, args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/bash", str(installer), *args],
                          capture_output=True, text=True, env=env)


def _tail(proc: subprocess.CompletedProcess, n: int = 2) -> str:
    return " | ".join((proc.stderr or "").strip().splitlines()[-n:])


def _read_plist(path: Path) -> dict:
    with open(path, "rb") as fh:
        return plistlib.load(fh)


def _read_receipt(path: Path) -> dict:
    return json.loads(path.read_text())


def _expected_watch(path: Path | str) -> str:
    """What the installer's ``cd "$WATCH_DIR" && pwd`` yields.

    bash's ``pwd`` is LOGICAL (it does not resolve symlinks), so a temp dir like
    ``/var/folders/…`` stays ``/var/…``. We therefore compare against
    ``os.path.abspath`` and not ``realpath``.
    """
    return os.path.abspath(str(path))


# --- mutants (negative controls) --------------------------------------------


def _make_bundle(dest: Path, installer_text: str | None = None) -> Path:
    """A `.app/Contents/Resources`-shaped source bundle: installer.sh, runner.sh,
    agent/ and the frozen helper side by side. This is both the layout build-app.sh
    produces and a convenient place to put a mutated / corrupted copy without
    touching the repo."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "agent").mkdir(exist_ok=True)
    for py in sorted(AGENT_SRC.glob("*.py")):
        shutil.copy2(py, dest / "agent" / py.name)
    shutil.copy2(RUNNER_SRC, dest / "runner.sh")
    shutil.copy2(HELPER_SRC, dest / "mp3-to-m4b-agent")
    text = installer_text if installer_text is not None else INSTALLER.read_text()
    inst = dest / "installer.sh"
    inst.write_text(text)
    inst.chmod(0o755)
    (dest / "runner.sh").chmod(0o755)
    (dest / "mp3-to-m4b-agent").chmod(0o755)
    return inst


def _mutate(drop_tags: list[str]) -> str | None:
    """installer.sh with every line tagged ``# [guard:<tag>]`` removed.

    Returns None (and records a failure) if a tag is not present — otherwise a
    renamed tag would silently turn the negative control into a no-op.
    """
    text = INSTALLER.read_text()
    missing = [t for t in drop_tags if f"[guard:{t}]" not in text]
    if missing:
        check(f"mutation: guard tag(s) present in installer.sh {drop_tags}", False,
              f"missing: {missing}")
        return None
    kept = [ln for ln in text.splitlines(True)
            if not any(f"[guard:{t}]" in ln for t in drop_tags)]
    return "".join(kept)


def _mutant_installer(root: Path, name: str, drop_tags: list[str]) -> Path | None:
    text = _mutate(drop_tags)
    if text is None:
        return None
    inst = _make_bundle(root / f"mutant-{name}", installer_text=text)
    syn = subprocess.run(["/bin/bash", "-n", str(inst)], capture_output=True, text=True)
    if syn.returncode != 0:
        check(f"mutation: mutant '{name}' is still valid bash", False,
              (syn.stderr or "").strip()[:160])
        return None
    return inst


# --- a stub launchctl (the launchd handshake without touching launchd) -------


_STUB_LAUNCHCTL = """#!/bin/bash
# Stub launchctl for the installer self-check. `print` replays a canned job
# description from $STUB_PRINT_FILE (missing file → "not loaded"); every other
# subcommand is a no-op. The real launchd is never contacted.
case "$1" in
  print)
    if [[ -n "${STUB_PRINT_FILE:-}" && -f "$STUB_PRINT_FILE" ]]; then
      cat "$STUB_PRINT_FILE"; exit 0
    fi
    echo "Could not find service" >&2; exit 113 ;;
  *) exit 0 ;;
esac
"""


def _make_stub_launchctl(dirpath: Path) -> Path:
    dirpath.mkdir(parents=True, exist_ok=True)
    p = dirpath / "launchctl"
    p.write_text(_STUB_LAUNCHCTL)
    p.chmod(0o755)
    return p


def _job_description(label: str, args: list[str]) -> str:
    arglines = "".join(f"\t\t{a}\n" for a in args)
    return (
        f"gui/{os.getuid()}/{label} = {{\n"
        "\tactive count = 1\n"
        "\tstate = running\n"
        "\n"
        f"\tprogram = {args[0] if args else ''}\n"
        "\targuments = {\n"
        f"{arglines}"
        "\t}\n"
        "}\n"
    )


# --- tripwires (prove a code path never ran) --------------------------------


def _make_tripwire(path: Path, marker: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/bin/bash\n"
        f'echo "$(basename "$0") $*" >> "{marker}"\n'
        "exit 1\n"
    )
    path.chmod(0o755)
    return path


# =============================================================================
# scenarios
# =============================================================================


class Tree:
    """One throwaway install tree (support dir + label + plist path)."""

    def __init__(self, root: Path, name: str):
        self.root = root
        self.name = name
        self.support = root / f"support-{name}"
        self.la = root / f"launchagents-{name}"
        self.label = f"com.arrivarus.mp3tom4b.selfcheck-{name}-{uuid.uuid4().hex[:8]}"
        self.support.mkdir(parents=True, exist_ok=True)
        self.la.mkdir(parents=True, exist_ok=True)

    @property
    def plist(self) -> Path:
        return self.la / f"{self.label}.plist"

    @property
    def receipt(self) -> Path:
        return self.support / "install-receipt.json"

    @property
    def helper(self) -> Path:
        return self.support / "bin" / "mp3-to-m4b-agent"

    @property
    def agent_dir(self) -> Path:
        return self.support / "bin" / "agent"

    def env(self, **kw) -> dict:
        return _env(test_root=self.root, support=self.support, label=self.label,
                    la_dir=self.la, **kw)

    def install(self, installer: Path, watch: Path | str, **kw) -> subprocess.CompletedProcess:
        return _run(installer, [str(watch)], self.env(**kw))


def scenario_repoint(root: Path) -> None:
    """The original contract: install → plist, re-point, tilde expansion."""
    t = Tree(root, "repoint")
    watch_a = root / "watch-A"
    watch_b = root / "watch-B"

    proc = t.install(INSTALLER, watch_a)
    if proc.returncode != 0:
        check("gen: installer.sh exits 0 (test mode, no launchctl/venv)", False,
              f"rc={proc.returncode}; stderr tail: {_tail(proc, 3)}")
        return
    check("gen: installer.sh exits 0 (test mode, no launchctl/venv)", True)
    check("gen: WATCH_DIR (folder A) was created on disk by the installer",
          watch_a.is_dir(), f"watch_a={watch_a}")
    check("gen: LaunchAgent plist was generated at the temp-label path",
          t.plist.is_file(), f"plist={t.plist}")
    if not t.plist.is_file():
        return

    pl = _read_plist(t.plist)
    resolved_a = _expected_watch(watch_a)
    wp = pl.get("WatchPaths")
    ev = pl.get("EnvironmentVariables")
    check("gen: WatchPaths is a 2-entry list [WATCH_DIR, commands]",
          isinstance(wp, list) and len(wp) == 2, f"WatchPaths={wp}")
    check("gen: WatchPaths.0 == the chosen watch folder A",
          isinstance(wp, list) and wp and wp[0] == resolved_a,
          f"got={wp[0] if isinstance(wp, list) and wp else None!r} expected={resolved_a!r}")
    check("gen: EnvironmentVariables.MP3TOM4B_WATCH_DIR == folder A",
          isinstance(ev, dict) and ev.get("MP3TOM4B_WATCH_DIR") == resolved_a,
          f"got={ev.get('MP3TOM4B_WATCH_DIR') if isinstance(ev, dict) else None!r}")

    expected_cmds = str(t.support / "queue" / "commands")
    check("commands: WatchPaths.1 == App Support queue/commands",
          isinstance(wp, list) and len(wp) >= 2 and wp[1] == expected_cmds,
          f"got={wp[1] if isinstance(wp, list) and len(wp) >= 2 else None!r}")

    proc2 = t.install(INSTALLER, watch_b)
    check("repoint: re-run with folder B exits 0 (idempotent re-point)",
          proc2.returncode == 0, f"rc={proc2.returncode}; {_tail(proc2)}")
    pl2 = _read_plist(t.plist)
    resolved_b = _expected_watch(watch_b)
    wp2 = pl2.get("WatchPaths")
    ev2 = pl2.get("EnvironmentVariables")
    check("repoint: WatchPaths.0 moved to folder B",
          isinstance(wp2, list) and wp2 and wp2[0] == resolved_b,
          f"got={wp2[0] if isinstance(wp2, list) and wp2 else None!r}")
    check("repoint: MP3TOM4B_WATCH_DIR moved to folder B",
          isinstance(ev2, dict) and ev2.get("MP3TOM4B_WATCH_DIR") == resolved_b,
          f"got={ev2.get('MP3TOM4B_WATCH_DIR') if isinstance(ev2, dict) else None!r}")
    check("repoint: WatchPaths stays a clean 2-entry list (no dupe accretion)",
          isinstance(wp2, list) and len(wp2) == 2, f"WatchPaths={wp2}")

    # tilde: a literal ~/… argument expands to $HOME/…
    tilde_leaf = f".mp3tom4b-selfcheck-{uuid.uuid4().hex[:10]}"
    tilde_abs = Path.home() / tilde_leaf
    try:
        proc3 = t.install(INSTALLER, f"~/{tilde_leaf}")
        pl3 = _read_plist(t.plist)
        wp3 = pl3.get("WatchPaths")
        ev3 = pl3.get("EnvironmentVariables")
        expanded = _expected_watch(tilde_abs)
        check("tilde: ~/<leaf> expands to $HOME/<leaf> in WatchPaths.0",
              proc3.returncode == 0 and isinstance(wp3, list) and wp3
              and wp3[0] == expanded,
              f"got={wp3[0] if isinstance(wp3, list) and wp3 else None!r}")
        check("tilde: ~/<leaf> expands in MP3TOM4B_WATCH_DIR too",
              isinstance(ev3, dict) and ev3.get("MP3TOM4B_WATCH_DIR") == expanded,
              f"got={ev3.get('MP3TOM4B_WATCH_DIR') if isinstance(ev3, dict) else None!r}")
    finally:
        if tilde_abs.is_dir():
            shutil.rmtree(tilde_abs, ignore_errors=True)


def scenario_shape(root: Path) -> None:
    """PA0 / StartInterval / generation / receipt / preserve — the plist contract."""
    t = Tree(root, "shape")
    watch = root / "watch-shape"
    proc = t.install(INSTALLER, watch)
    if proc.returncode != 0 or not t.plist.is_file():
        check("pa0: installer ran for the plist-shape checks", False,
              f"rc={proc.returncode}; {_tail(proc, 3)}")
        return

    pl = _read_plist(t.plist)
    pa = pl.get("ProgramArguments")
    check("pa0: ProgramArguments is EXACTLY ONE element",
          isinstance(pa, list) and len(pa) == 1, f"ProgramArguments={pa}")
    check("pa0: ProgramArguments.0 == <support>/bin/mp3-to-m4b-agent (the frozen helper)",
          isinstance(pa, list) and pa and pa[0] == str(t.helper),
          f"got={pa[0] if isinstance(pa, list) and pa else None!r} expected={str(t.helper)!r}")
    check("pa0: the installed helper matches the golden SHA-256",
          t.helper.is_file() and _sha256(t.helper) == GOLDEN_SHA,
          f"sha={_sha256(t.helper) if t.helper.is_file() else 'missing'}")
    check("pa0: runner.sh is installed as the helper's sibling (frozen name contract)",
          (t.support / "bin" / "runner.sh").is_file())

    check("interval: StartInterval == 300 (integer)",
          pl.get("StartInterval") == 300 and isinstance(pl.get("StartInterval"), int),
          f"StartInterval={pl.get('StartInterval')!r}")
    check("interval: RunAtLoad is true", pl.get("RunAtLoad") is True,
          f"RunAtLoad={pl.get('RunAtLoad')!r}")
    check("interval: ThrottleInterval is set (must not suppress a restart)",
          isinstance(pl.get("ThrottleInterval"), int),
          f"ThrottleInterval={pl.get('ThrottleInterval')!r}")

    ev = pl.get("EnvironmentVariables") or {}
    gen_plist_val = ev.get("MP3TOM4B_INSTALL_GENERATION")
    check("generation: the plist carries a MP3TOM4B_INSTALL_GENERATION UUID",
          isinstance(gen_plist_val, str) and len(gen_plist_val) >= 32,
          f"generation={gen_plist_val!r}")
    check("generation: FFMPEG_VERSION is pre-probed into the plist (no ffmpeg spawn per tick)",
          bool(ev.get("FFMPEG_VERSION")), f"FFMPEG_VERSION={ev.get('FFMPEG_VERSION')!r}")

    check("receipt_last: install-receipt.json exists after a successful install",
          t.receipt.is_file(), f"receipt={t.receipt}")
    if t.receipt.is_file():
        rc = _read_receipt(t.receipt)
        check("receipt_last: receipt.generation == the plist's generation",
              rc.get("generation") == gen_plist_val,
              f"receipt={rc.get('generation')!r} plist={gen_plist_val!r}")
        check("receipt_last: receipt records the golden helper sha + watch dir",
              rc.get("helper_sha256") == GOLDEN_SHA
              and rc.get("watch_dir") == _expected_watch(watch),
              f"sha={rc.get('helper_sha256')!r} watch={rc.get('watch_dir')!r}")

    # preserve: a re-install must NOT churn the helper (the grant is pinned to
    # those bytes at that path) and must mint a NEW generation.
    st_before = t.helper.stat()
    proc2 = t.install(INSTALLER, watch)
    st_after = t.helper.stat()
    check("preserve: a re-install leaves the helper untouched (same inode + mtime)",
          proc2.returncode == 0
          and (st_before.st_ino, st_before.st_mtime_ns) == (st_after.st_ino, st_after.st_mtime_ns),
          f"before=({st_before.st_ino},{st_before.st_mtime_ns}) "
          f"after=({st_after.st_ino},{st_after.st_mtime_ns})")
    gen2 = (_read_plist(t.plist).get("EnvironmentVariables") or {}).get("MP3TOM4B_INSTALL_GENERATION")
    check("generation: every install mints a NEW generation",
          bool(gen2) and gen2 != gen_plist_val, f"first={gen_plist_val!r} second={gen2!r}")
    if t.receipt.is_file():
        check("generation: the receipt follows the new generation",
              _read_receipt(t.receipt).get("generation") == gen2,
              f"receipt={_read_receipt(t.receipt).get('generation')!r} plist={gen2!r}")


def scenario_engine_override(root: Path) -> None:
    """The documented FFMPEG / FFPROBE env overrides still reach the plist.

    Regression guard: the 1.0 installer declares FFMPEG/FFPROBE as its own plist
    variables, which silently shadowed the env overrides once. It also proves the
    version parse that saves an `ffmpeg -version` spawn on every idle tick (M6f).
    """
    t = Tree(root, "engineenv")
    fake_dir = root / "fake-engine"
    fake_dir.mkdir(parents=True, exist_ok=True)
    fake_ffmpeg = fake_dir / "ffmpeg"
    fake_ffmpeg.write_text(
        "#!/bin/bash\n"
        'echo "ffmpeg version n9.9-fake Copyright (c) 2000-2026 the FFmpeg developers"\n')
    fake_ffmpeg.chmod(0o755)
    fake_ffprobe = fake_dir / "ffprobe"
    fake_ffprobe.write_text("#!/bin/bash\nexit 0\n")
    fake_ffprobe.chmod(0o755)

    proc = t.install(INSTALLER, root / "watch-engineenv",
                     extra={"FFMPEG": str(fake_ffmpeg), "FFPROBE": str(fake_ffprobe)})
    ev = (_read_plist(t.plist).get("EnvironmentVariables") or {}) if t.plist.is_file() else {}
    check("engine_env: FFMPEG / FFPROBE env overrides reach the plist",
          proc.returncode == 0 and ev.get("FFMPEG") == str(fake_ffmpeg)
          and ev.get("FFPROBE") == str(fake_ffprobe),
          f"rc={proc.returncode}; FFMPEG={ev.get('FFMPEG')!r}")
    check("engine_env: FFMPEG_VERSION is parsed the same way agent/scan.py parses it",
          ev.get("FFMPEG_VERSION") == "9.9-fake",
          f"FFMPEG_VERSION={ev.get('FFMPEG_VERSION')!r}")


def scenario_bundle_layout(root: Path) -> None:
    """The .app Resources layout installs exactly like the checkout layout.

    Also the baseline for every mutant below (they run from this same shape), so a
    mutant failing is about the removed guard and not about the layout.
    """
    t = Tree(root, "bundle")
    inst = _make_bundle(root / "bundle-pristine")
    proc = t.install(inst, root / "watch-bundle")
    ok = proc.returncode == 0 and t.plist.is_file()
    pa = (_read_plist(t.plist).get("ProgramArguments") if t.plist.is_file() else None)
    check("bundle_layout: a Resources-shaped bundle installs and points PA0 at its helper",
          ok and isinstance(pa, list) and len(pa) == 1 and pa[0] == str(t.helper),
          f"rc={proc.returncode}; PA={pa}; {_tail(proc)}")


def scenario_golden(root: Path) -> None:
    """B5 — the independent golden SHA, in all three shapes, + negative controls."""
    # --- golden_src: a corrupted SOURCE helper -----------------------------
    t = Tree(root, "goldensrc")
    bad_bundle = _make_bundle(root / "bundle-badsrc")
    with open(bad_bundle.parent / "mp3-to-m4b-agent", "ab") as fh:
        fh.write(b"\x00corrupted")
    proc = t.install(bad_bundle, root / "watch-goldensrc")
    check("golden_src: a corrupted SOURCE helper is REFUSED",
          proc.returncode != 0 and "REFUSING" in (proc.stderr or ""),
          f"rc={proc.returncode}; {_tail(proc)}")
    check("golden_src: nothing was written (no plist, no bin dir) before the refusal",
          not t.plist.exists() and not (t.support / "bin").exists(),
          f"plist={t.plist.exists()} bin={(t.support / 'bin').exists()}")

    # --- golden_both: src AND dst corrupted IDENTICALLY --------------------
    # This is the hole a src↔dst compare cannot see (blocker B5).
    t2 = Tree(root, "goldenboth")
    (t2.support / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy2(bad_bundle.parent / "mp3-to-m4b-agent", t2.helper)
    proc = t2.install(bad_bundle, root / "watch-goldenboth")
    check("golden_both: identically corrupted src+dst is STILL refused",
          proc.returncode != 0 and "REFUSING" in (proc.stderr or ""),
          f"rc={proc.returncode}; {_tail(proc)}")
    check("golden_both: no plist was published for the corrupted pair",
          not t2.plist.exists())

    # --- golden_dst: destination corrupted AFTER the copy ------------------
    t3 = Tree(root, "goldendst")
    proc = t3.install(INSTALLER, root / "watch-goldendst",
                      extra={"MP3TOM4B_TEST_HOOK": "corrupt-dst-after-install"})
    check("golden_dst: a destination corrupted after the copy is REFUSED",
          proc.returncode != 0 and "REFUSING" in (proc.stderr or ""),
          f"rc={proc.returncode}; {_tail(proc)}")
    check("golden_dst: the plist is not published and no receipt is written",
          not t3.plist.exists() and not t3.receipt.exists(),
          f"plist={t3.plist.exists()} receipt={t3.receipt.exists()}")

    # --- negative control: no golden checks at all -------------------------
    # Deleting the golden gates leaves ONLY the historical src↔dst compare — the
    # pre-B5 installer. It must now accept the corrupted pair.
    mut = _mutant_installer(root, "nogolden", ["golden-src", "golden-dst"])
    if mut is not None:
        tm = Tree(root, "nogolden")
        (tm.support / "bin").mkdir(parents=True, exist_ok=True)
        shutil.copy2(bad_bundle.parent / "mp3-to-m4b-agent", tm.helper)
        # give the mutant the corrupted SOURCE too (identical bytes → cmp passes)
        shutil.copy2(bad_bundle.parent / "mp3-to-m4b-agent",
                     mut.parent / "mp3-to-m4b-agent")
        proc = tm.install(mut, root / "watch-nogolden")
        installed_sha = _sha256(tm.helper) if tm.helper.is_file() else ""
        check("NEG golden: without the golden gates the corrupted helper INSTALLS "
              "(so the gates are what refuse it)",
              proc.returncode == 0 and installed_sha != GOLDEN_SHA,
              f"rc={proc.returncode}; installed_sha={installed_sha[:16]}…")

    # --- negative control: no destination-side check at all ----------------
    mut2 = _mutant_installer(root, "nodst", ["golden-dst", "copy-quality"])
    if mut2 is not None:
        tm2 = Tree(root, "nodst")
        proc = tm2.install(mut2, root / "watch-nodst",
                           extra={"MP3TOM4B_TEST_HOOK": "corrupt-dst-after-install"})
        installed_sha = _sha256(tm2.helper) if tm2.helper.is_file() else ""
        check("NEG golden_dst: without the destination check a corrupted install "
              "reports SUCCESS",
              proc.returncode == 0 and installed_sha != GOLDEN_SHA,
              f"rc={proc.returncode}; installed_sha={installed_sha[:16]}…")


def scenario_nosymlink(root: Path) -> None:
    """m2 — the grant is keyed to a real path: no symlink may stand on it."""
    t = Tree(root, "nosymlink")
    elsewhere = root / "elsewhere-bin"
    elsewhere.mkdir(parents=True, exist_ok=True)
    (t.support / "bin").parent.mkdir(parents=True, exist_ok=True)
    os.symlink(str(elsewhere), str(t.support / "bin"))
    proc = t.install(INSTALLER, root / "watch-nosymlink")
    check("nosymlink: a symlinked bin dir is refused",
          proc.returncode != 0 and "symlink" in (proc.stderr or ""),
          f"rc={proc.returncode}; {_tail(proc)}")
    check("nosymlink: no plist was published through the symlink",
          not t.plist.exists())

    mut = _mutant_installer(root, "nosymlink", ["nosymlink"])
    if mut is not None:
        tm = Tree(root, "nosymlinkmut")
        elsewhere2 = root / "elsewhere-bin-2"
        elsewhere2.mkdir(parents=True, exist_ok=True)
        os.symlink(str(elsewhere2), str(tm.support / "bin"))
        proc = tm.install(mut, root / "watch-nosymlink-mut")
        check("NEG nosymlink: without the guard the installer happily writes "
              "through the symlink",
              proc.returncode == 0 and (elsewhere2 / "mp3-to-m4b-agent").is_file(),
              f"rc={proc.returncode}; {_tail(proc)}")


def scenario_heal(root: Path) -> None:
    """A v0.9 install (PA0 = runner.sh) is healed to the helper, grant preserved."""
    t = Tree(root, "heal")
    watch = root / "watch-heal"
    proc = t.install(INSTALLER, watch)
    if proc.returncode != 0:
        check("heal: baseline install", False, _tail(proc, 3))
        return
    # Seed the v0.9 shape: PA0 points at runner.sh (the dead-grant construction).
    subprocess.run(["plutil", "-replace", "ProgramArguments", "-json",
                    json.dumps([str(t.support / "bin" / "runner.sh")]), str(t.plist)],
                   check=True, capture_output=True)
    st_before = t.helper.stat()
    proc2 = t.install(INSTALLER, watch)
    pa = _read_plist(t.plist).get("ProgramArguments")
    st_after = t.helper.stat()
    check("heal: a v0.9 plist (PA0 = runner.sh) is re-pointed at the helper",
          proc2.returncode == 0 and isinstance(pa, list) and len(pa) == 1
          and pa[0] == str(t.helper),
          f"rc={proc2.returncode}; PA={pa}")
    check("heal: healing does NOT churn the helper (the grant survives)",
          (st_before.st_ino, st_before.st_mtime_ns) == (st_after.st_ino, st_after.st_mtime_ns))


def scenario_lock(root: Path) -> None:
    """B4 — one installer at a time; a stale lock is taken over."""
    t = Tree(root, "lock")
    watch = root / "watch-lock"
    proc = t.install(INSTALLER, watch)
    if proc.returncode != 0:
        check("lock: baseline install", False, _tail(proc, 3))
        return
    before = t.plist.read_bytes()

    # A live owner: this very test process.
    lock_dir = t.support / ".install.lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "pid").write_text(str(os.getpid()))
    proc2 = t.install(INSTALLER, root / "watch-lock-2")
    check("lock: a second installer refuses while another one holds the lock",
          proc2.returncode != 0 and "another install" in (proc2.stderr or ""),
          f"rc={proc2.returncode}; {_tail(proc2)}")
    check("lock: the refused run changed nothing (plist byte-identical)",
          t.plist.read_bytes() == before)

    mut = _mutant_installer(root, "nolock", ["lock"])
    if mut is not None:
        proc3 = t.install(mut, root / "watch-lock-3")
        changed = t.plist.read_bytes() != before
        check("NEG lock: without the lock the second installer runs anyway and "
              "rewrites the live install",
              proc3.returncode == 0 and changed,
              f"rc={proc3.returncode}; plist_changed={changed}")
        # Restore a sane state for the stale-lock check below. The held lock must
        # go first — otherwise this very install is refused and "restore" is a lie.
        shutil.rmtree(lock_dir, ignore_errors=True)
        restore = t.install(INSTALLER, watch)
        check("lock: the tree is re-installable once the lock is released",
              restore.returncode == 0, f"rc={restore.returncode}; {_tail(restore)}")

    # A stale owner: a pid that has already exited.
    dead = subprocess.Popen(["/usr/bin/true"])
    dead.wait()
    lock_dir.mkdir(parents=True, exist_ok=True)
    (lock_dir / "pid").write_text(str(dead.pid))
    proc4 = t.install(INSTALLER, watch)
    check("lock: a STALE lock (owner gone) is taken over, not a deadlock",
          proc4.returncode == 0 and "stale install lock" in (proc4.stdout or ""),
          f"rc={proc4.returncode}; {_tail(proc4)}")
    check("lock: the lock directory is released after a successful install",
          not lock_dir.exists())


def scenario_busy(root: Path) -> None:
    """B4 — never tear the engine out from under a live build."""
    t = Tree(root, "busy")
    watch = root / "watch-busy"
    proc = t.install(INSTALLER, watch)
    if proc.returncode != 0:
        check("busy_refuse: baseline install", False, _tail(proc, 3))
        return
    before = t.plist.read_bytes()
    books = t.support / "queue" / "books"
    books.mkdir(parents=True, exist_ok=True)
    (books / "abc123.json").write_text(json.dumps({
        "book_id": "abc123", "status": "converting",
        "build": {"pid": os.getpid(), "started_at": 0},
    }))
    proc2 = t.install(INSTALLER, root / "watch-busy-2")
    check("busy_refuse: an install during a LIVE build is refused",
          proc2.returncode != 0 and "being built right now" in (proc2.stderr or ""),
          f"rc={proc2.returncode}; {_tail(proc2)}")
    check("busy_refuse: the refused run changed nothing (plist byte-identical)",
          t.plist.read_bytes() == before)

    # A dead pid must NOT block (an orphaned `converting` manifest is normal).
    dead = subprocess.Popen(["/usr/bin/true"])
    dead.wait()
    (books / "abc123.json").write_text(json.dumps({
        "book_id": "abc123", "status": "converting",
        "build": {"pid": dead.pid, "started_at": 0},
    }))
    proc3 = t.install(INSTALLER, watch)
    check("busy_refuse: an ORPHANED converting manifest (dead pid) does not block",
          proc3.returncode == 0, f"rc={proc3.returncode}; {_tail(proc3)}")

    mut = _mutant_installer(root, "nobusy", ["busy"])
    if mut is not None:
        (books / "abc123.json").write_text(json.dumps({
            "book_id": "abc123", "status": "converting",
            "build": {"pid": os.getpid(), "started_at": 0},
        }))
        before2 = t.plist.read_bytes()
        proc4 = t.install(mut, root / "watch-busy-mut")
        check("NEG busy_refuse: without the guard the installer reloads the agent "
              "mid-build",
              proc4.returncode == 0 and t.plist.read_bytes() != before2,
              f"rc={proc4.returncode}")


def scenario_rollback(root: Path) -> None:
    """B4/Р5 — a failure mid-install restores the mutable parts and writes no receipt."""
    t = Tree(root, "rollback")
    watch_a = root / "watch-rb-A"
    watch_b = root / "watch-rb-B"
    proc = t.install(INSTALLER, watch_a)
    if proc.returncode != 0 or not t.receipt.is_file():
        check("rollback: baseline install", False, _tail(proc, 3))
        return
    gen1 = _read_receipt(t.receipt).get("generation")
    plist_before = t.plist.read_bytes()
    canary = t.agent_dir / "zz_canary.py"
    canary.write_text("# canary written after install #1\n")

    proc2 = t.install(INSTALLER, watch_b,
                      extra={"MP3TOM4B_TEST_HOOK": "fail-after-publish-plist"})
    check("rollback: a failure after the plist was published exits non-zero",
          proc2.returncode != 0, f"rc={proc2.returncode}; {_tail(proc2)}")
    check("rollback: the previous plist is restored byte-for-byte",
          t.plist.read_bytes() == plist_before,
          f"watch now={(_read_plist(t.plist).get('WatchPaths') or [None])[0]!r}")
    check("rollback: the previous agent package is restored (canary is back)",
          canary.is_file())
    check("rollback: the receipt still describes the LAST GOOD install",
          t.receipt.is_file() and _read_receipt(t.receipt).get("generation") == gen1,
          f"receipt gen={_read_receipt(t.receipt).get('generation') if t.receipt.is_file() else None!r}")

    mut = _mutant_installer(root, "norollback", ["rollback"])
    if mut is not None:
        tm = Tree(root, "norollback")
        proc3 = tm.install(INSTALLER, root / "watch-nrb-A")
        if proc3.returncode == 0:
            before = tm.plist.read_bytes()
            proc4 = tm.install(mut, root / "watch-nrb-B",
                               extra={"MP3TOM4B_TEST_HOOK": "fail-after-publish-plist"})
            check("NEG rollback: without the rollback the failed install leaves the "
                  "NEW plist behind",
                  proc4.returncode != 0 and tm.plist.read_bytes() != before,
                  f"rc={proc4.returncode}")


def scenario_rollback_pa0(root: Path) -> None:
    """Р5 — a rollback must never re-point ProgramArguments[0] back at runner.sh."""
    t = Tree(root, "rbpa0")
    watch = root / "watch-rbpa0"
    proc = t.install(INSTALLER, watch)
    if proc.returncode != 0:
        check("rollback_pa0: baseline install", False, _tail(proc, 3))
        return
    # Seed the v0.9 shape as the PREVIOUS plist, then fail the upgrade.
    runner = str(t.support / "bin" / "runner.sh")
    subprocess.run(["plutil", "-replace", "ProgramArguments", "-json",
                    json.dumps([runner]), str(t.plist)], check=True, capture_output=True)
    proc2 = t.install(INSTALLER, watch,
                      extra={"MP3TOM4B_TEST_HOOK": "fail-after-publish-plist"})
    pa = _read_plist(t.plist).get("ProgramArguments") if t.plist.is_file() else None
    restored_to_runner = isinstance(pa, list) and pa and pa[0] == runner
    check("rollback_pa0: the failed upgrade did NOT restore the v0.9 runner.sh PA0",
          proc2.returncode != 0 and not restored_to_runner,
          f"rc={proc2.returncode}; PA0={pa[0] if isinstance(pa, list) and pa else None!r}")
    check("rollback_pa0: the human is told the old job was not restored",
          "was NOT restored" in (proc2.stderr or ""), _tail(proc2, 4))


def scenario_verify_pa0(root: Path) -> None:
    """B3 — a correct plist on disk is NOT proof launchd runs it.

    The launchd handshake is exercised against a STUB ``launchctl`` on PATH: the
    real launchd is never contacted, so this can never raise a system dialog.
    """
    stub_dir = root / "stub-bin"
    _make_stub_launchctl(stub_dir)
    printfile = root / "launchctl-print.txt"

    def stub_env(t: Tree) -> dict:
        return t.env(extra={
            "MP3TOM4B_NO_LAUNCHCTL": None,          # let the (stubbed) launchd path run
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "STUB_PRINT_FILE": str(printfile),
        })

    # (a) launchd reports the helper as the only argument → success
    t = Tree(root, "verifyok")
    printfile.write_text(_job_description(t.label, [str(t.helper)]))
    proc = _run(INSTALLER, [str(root / "watch-verify")], stub_env(t))
    check("verify_pa0: a job loaded with PA0 = helper (1 arg) passes verification",
          proc.returncode == 0 and t.receipt.is_file(),
          f"rc={proc.returncode}; {_tail(proc)}")

    # (b) launchd reports TWO arguments (helper + runner.sh) → refuse
    t2 = Tree(root, "verify2args")
    printfile.write_text(_job_description(
        t2.label, [str(t2.helper), str(t2.support / "bin" / "runner.sh")]))
    proc2 = _run(INSTALLER, [str(root / "watch-verify2")], stub_env(t2))
    check("verify_pa0: a TWO-element loaded ProgramArguments is refused",
          proc2.returncode != 0 and "ProgramArguments (expected exactly 1" in (proc2.stderr or ""),
          f"rc={proc2.returncode}; {_tail(proc2)}")
    check("verify_pa0: no receipt is written when the loaded job is wrong",
          not t2.receipt.exists())

    # (c) launchd reports runner.sh as PA0 (the plist was right, the job is not)
    t3 = Tree(root, "verifywrong")
    printfile.write_text(_job_description(t3.label, [str(t3.support / "bin" / "runner.sh")]))
    proc3 = _run(INSTALLER, [str(root / "watch-verify3")], stub_env(t3))
    check("verify_pa0: a WRONG loaded ProgramArguments[0] is refused",
          proc3.returncode != 0 and "WRONG ProgramArguments[0]" in (proc3.stderr or ""),
          f"rc={proc3.returncode}; {_tail(proc3)}")

    # (d) the job did not load at all
    t4 = Tree(root, "verifynone")
    if printfile.exists():
        printfile.unlink()
    proc4 = _run(INSTALLER, [str(root / "watch-verify4")], stub_env(t4))
    check("verify_pa0: a job that did not load at all is refused",
          proc4.returncode != 0 and "did not load" in (proc4.stderr or ""),
          f"rc={proc4.returncode}; {_tail(proc4)}")

    # negative control: without the verification the two-element job passes and a
    # receipt is written — exactly blocker B3.
    mut = _mutant_installer(root, "noverify", ["verify-pa0"])
    if mut is not None:
        t5 = Tree(root, "noverify")
        printfile.write_text(_job_description(
            t5.label, [str(t5.helper), str(t5.support / "bin" / "runner.sh")]))
        proc5 = _run(mut, [str(root / "watch-verify5")], stub_env(t5))
        check("NEG verify_pa0: without the launchctl check a WRONG running job is "
              "reported as a successful install",
              proc5.returncode == 0 and t5.receipt.is_file(),
              f"rc={proc5.returncode}; receipt={t5.receipt.exists()}")


def scenario_access_wait(root: Path) -> None:
    """Addendum §5.2 — the installer waits for the agent's first access probe."""
    stub_dir = root / "stub-bin"
    _make_stub_launchctl(stub_dir)
    printfile = root / "launchctl-print-access.txt"

    t = Tree(root, "accesswait")
    printfile.write_text(_job_description(t.label, [str(t.helper)]))
    # Pre-publish a state.json as if the agent had already probed.
    (t.support / "state").mkdir(parents=True, exist_ok=True)
    (t.support / "state" / "state.json").write_text(json.dumps({
        "schema": 1,
        "agent": {"watch_dir": "/x", "active": True,
                  "folder_access": "ok", "folder_access_ts": "2026-07-26T00:00:00.5Z"},
        "books": [],
    }))
    proc = _run(INSTALLER, [str(root / "watch-accesswait")], t.env(extra={
        "MP3TOM4B_NO_LAUNCHCTL": None,
        "MP3TOM4B_ACCESS_WAIT_S": "3",
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "STUB_PRINT_FILE": str(printfile),
    }))
    receipt = _read_receipt(t.receipt) if t.receipt.is_file() else {}
    check("access_wait: the installer picks up the agent's first folder_access_ts",
          proc.returncode == 0 and receipt.get("first_access_ts") == "2026-07-26T00:00:00.5Z",
          f"rc={proc.returncode}; first_access_ts={receipt.get('first_access_ts')!r}")

    # No probe at all: the install must still succeed (soft wait, never fatal).
    t2 = Tree(root, "accesswait2")
    printfile.write_text(_job_description(t2.label, [str(t2.helper)]))
    proc2 = _run(INSTALLER, [str(root / "watch-accesswait2")], t2.env(extra={
        "MP3TOM4B_NO_LAUNCHCTL": None,
        "MP3TOM4B_ACCESS_WAIT_S": "1",
        "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
        "STUB_PRINT_FILE": str(printfile),
    }))
    check("access_wait: a missing probe is reported but never fails the install",
          proc2.returncode == 0 and "has not published an access probe" in (proc2.stdout or ""),
          f"rc={proc2.returncode}; {_tail(proc2)}")


def scenario_repair_only(root: Path) -> None:
    """B2 — ``--repair-launchd-only`` is strictly offline: no ffmpeg, no pip, no venv."""
    t = Tree(root, "repair")
    watch = root / "watch-repair"
    proc = t.install(INSTALLER, watch)
    if proc.returncode != 0 or not t.receipt.is_file():
        check("repair_only: baseline install", False, _tail(proc, 3))
        return
    gen1 = _read_receipt(t.receipt).get("generation")
    py_before = (_read_plist(t.plist).get("EnvironmentVariables") or {}).get("PYTHON3")

    # Tripwires: anything that would go near the engine or the network explodes.
    trip_dir = root / "tripwires"
    marker = root / "tripwire-hits.txt"
    if marker.exists():
        marker.unlink()
    for tool in ("ffmpeg", "ffprobe", "pip", "pip3", "curl"):
        _make_tripwire(trip_dir / tool, marker)

    repair_env = t.env(extra={
        "MP3TOM4B_NO_VENV": None,   # deliberately NOT skipped: repair must never venv
        "PATH": f"{trip_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
        "FFMPEG": str(trip_dir / "ffmpeg"),
        "FFPROBE": str(trip_dir / "ffprobe"),
    })
    proc2 = _run(INSTALLER, ["--repair-launchd-only"], repair_env)
    check("repair_only: --repair-launchd-only exits 0 on a healthy install",
          proc2.returncode == 0, f"rc={proc2.returncode}; {_tail(proc2, 3)}")
    check("repair_only: it never invoked ffmpeg / ffprobe / pip (no tripwire hit)",
          not marker.exists(),
          marker.read_text().strip().replace("\n", "; ") if marker.exists() else "")
    check("repair_only: it created no venv",
          not (t.support / "venv").exists())

    pl = _read_plist(t.plist)
    ev = pl.get("EnvironmentVariables") or {}
    gen2 = ev.get("MP3TOM4B_INSTALL_GENERATION")
    check("repair_only: the plist gets a NEW generation and the receipt follows",
          bool(gen2) and gen2 != gen1
          and _read_receipt(t.receipt).get("generation") == gen2,
          f"gen1={gen1!r} gen2={gen2!r} receipt={_read_receipt(t.receipt).get('generation')!r}")
    pa = pl.get("ProgramArguments")
    check("repair_only: PA0 is still the single frozen helper",
          isinstance(pa, list) and len(pa) == 1 and pa[0] == str(t.helper), f"PA={pa}")
    check("repair_only: the watch folder and python carry over from the old install",
          (pl.get("WatchPaths") or [None])[0] == _expected_watch(watch)
          and ev.get("PYTHON3") == py_before,
          f"watch={(pl.get('WatchPaths') or [None])[0]!r} python={ev.get('PYTHON3')!r}")

    # repair heals a v0.9 PA0 offline — the "PA0-only" migration branch (§6.2).
    subprocess.run(["plutil", "-replace", "ProgramArguments", "-json",
                    json.dumps([str(t.support / "bin" / "runner.sh")]), str(t.plist)],
                   check=True, capture_output=True)
    proc3 = _run(INSTALLER, ["--repair-launchd-only"], repair_env)
    pa3 = _read_plist(t.plist).get("ProgramArguments")
    check("repair_only: it heals a v0.9 PA0 (runner.sh → helper) without going online",
          proc3.returncode == 0 and isinstance(pa3, list) and len(pa3) == 1
          and pa3[0] == str(t.helper) and not marker.exists(),
          f"rc={proc3.returncode}; PA={pa3}")

    # A corrupted installed helper must block the repair too (golden dst).
    t4 = Tree(root, "repairbad")
    proc4 = t4.install(INSTALLER, root / "watch-repairbad")
    if proc4.returncode == 0:
        with open(t4.helper, "ab") as fh:
            fh.write(b"\x00corrupted")
        proc5 = _run(INSTALLER, ["--repair-launchd-only"], t4.env())
        check("repair_only: a corrupted INSTALLED helper blocks the repair",
              proc5.returncode != 0 and "REFUSING" in (proc5.stderr or ""),
              f"rc={proc5.returncode}; {_tail(proc5)}")

    # negative control: without the offline dispatch, repair falls through into the
    # full installer and immediately touches the engine.
    mut = _mutant_installer(root, "norepair", ["repair-offline"])
    if mut is not None:
        if marker.exists():
            marker.unlink()
        tm = Tree(root, "norepair")
        procm = tm.install(INSTALLER, root / "watch-norepair")
        if procm.returncode == 0:
            # Pass the watch folder explicitly: without the offline dispatch the
            # mutant falls into the FULL path, which would otherwise default to
            # ~/Desktop/mp3-to-m4b — a test must never reach for the real desktop.
            _run(mut, ["--repair-launchd-only", str(root / "watch-norepair")], tm.env(extra={
                "PATH": f"{trip_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
                "FFMPEG": str(trip_dir / "ffmpeg"),
                "FFPROBE": str(trip_dir / "ffprobe"),
            }))
            check("NEG repair_only: without the offline dispatch the repair path "
                  "reaches the engine (tripwire fires)",
                  marker.exists(),
                  marker.read_text().strip().replace("\n", "; ") if marker.exists() else "no hit")


def scenario_latch(root: Path) -> None:
    """The test-override latch (neighbor .patches/015): one forgotten env var must
    never be able to rewrite the production agent."""
    real_plist = Path.home() / "Library" / "LaunchAgents" / "com.arrivarus.mp3tom4b.agent.plist"
    real_before = (real_plist.exists(), real_plist.stat().st_mtime_ns if real_plist.exists() else 0)

    t = Tree(root, "latch")
    # (a) overrides set, latch NOT armed → refuse, write nothing
    proc = t.install(INSTALLER, root / "watch-latch", latched=False)
    check("latch: redirecting overrides without MP3TOM4B_TEST_MODE are REFUSED",
          proc.returncode != 0 and "without an armed test latch" in (proc.stderr or ""),
          f"rc={proc.returncode}; {_tail(proc)}")
    check("latch: the refused run wrote nothing at all (no tree, no plist)",
          not t.plist.exists() and not (t.support / "bin").exists())

    # (b) TEST_ROOT = "/" is too wide to be a latch
    proc2 = t.install(INSTALLER, root / "watch-latch2",
                      extra={"MP3TOM4B_TEST_MODE": "1", "MP3TOM4B_TEST_ROOT": "/"})
    check("latch: MP3TOM4B_TEST_ROOT=/ does not arm the latch",
          proc2.returncode != 0, f"rc={proc2.returncode}; {_tail(proc2)}")

    # (c) TEST_ROOT containing the real home is too wide as well
    proc3 = t.install(INSTALLER, root / "watch-latch3",
                      extra={"MP3TOM4B_TEST_MODE": "1",
                             "MP3TOM4B_TEST_ROOT": str(Path.home().parent)})
    check("latch: a MP3TOM4B_TEST_ROOT above the real home does not arm the latch",
          proc3.returncode != 0, f"rc={proc3.returncode}; {_tail(proc3)}")

    # (d) armed latch but the support dir points OUTSIDE it
    outside = root.parent / f"outside-{uuid.uuid4().hex[:8]}"
    outside.mkdir(parents=True, exist_ok=True)
    try:
        proc4 = _run(INSTALLER, [str(root / "watch-latch4")],
                     _env(test_root=root, support=outside, label=t.label, la_dir=t.la))
        check("latch: a support dir OUTSIDE the test root does not arm the latch",
              proc4.returncode != 0 and not (outside / "bin").exists(),
              f"rc={proc4.returncode}; {_tail(proc4)}")
    finally:
        shutil.rmtree(outside, ignore_errors=True)

    # (e) the real production plist was never touched by any of this
    real_after = (real_plist.exists(), real_plist.stat().st_mtime_ns if real_plist.exists() else 0)
    check("latch: the REAL production LaunchAgent plist was never touched",
          real_before == real_after, f"before={real_before} after={real_after}")

    # negative control: without the guard the unlatched overrides take effect.
    #
    # TWO things this mutant needs, and both are lessons paid for once:
    #  · its tree lives under the REALPATH of the temp root — unlatched, the
    #    installer also enforces "the helper path must be physically real", and
    #    /var/folders/… → /private/var/folders/… would stop the mutant for the
    #    wrong reason (a negative control that fails for an unrelated reason
    #    proves nothing);
    #  · it runs against a STUB launchctl. Unlatched, MP3TOM4B_NO_LAUNCHCTL is
    #    ignored too (by design — production must always really load the job), so
    #    without the stub this mutant BOOTSTRAPS A REAL LAUNCHD JOB, which then
    #    runs the real agent against the real App Support tree. It did exactly
    #    that once during development. A mutant must never reach the live system.
    mut = _mutant_installer(root, "nolatch", ["latch"])
    if mut is not None:
        stub_dir = root / "stub-bin"
        _make_stub_launchctl(stub_dir)
        tm = Tree(Path(os.path.realpath(root)), "nolatch")
        printfile = root / "launchctl-print-nolatch.txt"
        printfile.write_text(_job_description(tm.label, [str(tm.helper)]))
        procm = _run(mut, [str(root / "watch-nolatch")], tm.env(latched=False, extra={
            "PATH": f"{stub_dir}:{os.environ.get('PATH', '')}",
            "STUB_PRINT_FILE": str(printfile),
        }))
        check("NEG latch: without the guard an UNLATCHED override installs a whole "
              "agent (this is what the guard prevents)",
              procm.returncode == 0 and tm.plist.is_file(),
              f"rc={procm.returncode}; plist={tm.plist.exists()}")


def scenario_downgrade(root: Path) -> None:
    """M11f — an older package must not silently 'update' a newer install."""
    t = Tree(root, "downgrade")
    watch = root / "watch-downgrade"
    proc = t.install(INSTALLER, watch, extra={"MP3TOM4B_VERSION": "1.1"})
    if proc.returncode != 0:
        check("downgrade: baseline install", False, _tail(proc, 3))
        return
    proc2 = t.install(INSTALLER, watch, extra={"MP3TOM4B_VERSION": "1.0"})
    check("downgrade: an older bundle refuses to overwrite a newer install",
          proc2.returncode != 0 and "refusing to downgrade" in (proc2.stderr or ""),
          f"rc={proc2.returncode}; {_tail(proc2)}")
    proc3 = t.install(INSTALLER, watch,
                      extra={"MP3TOM4B_VERSION": "1.0", "MP3TOM4B_ALLOW_DOWNGRADE": "1"})
    check("downgrade: an explicit MP3TOM4B_ALLOW_DOWNGRADE=1 is honored",
          proc3.returncode == 0, f"rc={proc3.returncode}; {_tail(proc3)}")
    proc4 = t.install(INSTALLER, watch, extra={"MP3TOM4B_VERSION": "1.2"})
    check("downgrade: a newer bundle installs over an older one",
          proc4.returncode == 0, f"rc={proc4.returncode}; {_tail(proc4)}")


SELFCHECK_LABEL_PREFIX = "com.arrivarus.mp3tom4b.selfcheck"
REAL_SUPPORT = Path.home() / "Library" / "Application Support" / "mp3-to-m4b"
REAL_LOG = Path.home() / "Library" / "Logs" / "mp3-to-m4b.log"
REAL_PLIST = Path.home() / "Library" / "LaunchAgents" / "com.arrivarus.mp3tom4b.agent.plist"


def _loaded_selfcheck_jobs() -> list[str]:
    """Labels of OUR throwaway jobs that are actually loaded in the user's gui domain."""
    proc = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    return [ln.split("\t")[-1].strip() for ln in (proc.stdout or "").splitlines()
            if SELFCHECK_LABEL_PREFIX in ln]


def _blast_radius_snapshot() -> dict:
    def stamp(p: Path):
        return (p.exists(), p.stat().st_mtime_ns if p.exists() else 0)
    return {
        "support": stamp(REAL_SUPPORT),
        "log": stamp(REAL_LOG),
        "plist": stamp(REAL_PLIST),
        "jobs": sorted(_loaded_selfcheck_jobs()),
    }


def scenario_blast_radius(before: dict) -> None:
    """This suite must leave the LIVE system exactly as it found it.

    Not a nicety: a negative control once bootstrapped a real launchd job under a
    throwaway label, which started the real agent against the real App Support
    tree. That class of accident is now a red check instead of a surprise.
    """
    after = _blast_radius_snapshot()
    stray = [j for j in after["jobs"] if j not in before["jobs"]]
    check("blast_radius: no throwaway job was loaded into the real launchd domain",
          not stray, f"stray jobs: {stray}")
    for job in stray:   # never leave one behind, even while failing
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{job}"],
                       capture_output=True)
    check("blast_radius: the real App Support tree was not created/modified",
          after["support"] == before["support"],
          f"before={before['support']} after={after['support']}")
    check("blast_radius: the real agent log was not created/modified",
          after["log"] == before["log"], f"before={before['log']} after={after['log']}")
    check("blast_radius: the real production plist was not created/modified",
          after["plist"] == before["plist"],
          f"before={before['plist']} after={after['plist']}")


def _sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not shutil.which("plutil"):
        print("§installer-repoint self-check: SKIPPED — plutil not found (needs macOS)")
        return 1
    for src, what in ((INSTALLER, "installer.sh"), (HELPER_SRC, "the frozen helper"),
                      (RUNNER_SRC, "runner.sh")):
        if not src.is_file():
            print(f"§installer-repoint self-check: SKIPPED — {what} not found at {src}")
            return 1
    if _sha256(HELPER_SRC) != GOLDEN_SHA:
        print("§installer-repoint self-check: SKIPPED — packaging/mp3-to-m4b-agent does "
              "not match the golden SHA in PROVENANCE.md; the whole suite would be lying")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-installer-"))
    print(f"self-check tree: {root}\n"
          f"  installer: {INSTALLER}\n"
          f"  helper:    {HELPER_SRC} ({GOLDEN_SHA[:16]}…)\n")

    before = _blast_radius_snapshot()
    try:
        scenario_repoint(root)
        scenario_shape(root)
        scenario_engine_override(root)
        scenario_bundle_layout(root)
        scenario_golden(root)
        scenario_nosymlink(root)
        scenario_heal(root)
        scenario_lock(root)
        scenario_busy(root)
        scenario_rollback(root)
        scenario_rollback_pa0(root)
        scenario_verify_pa0(root)
        scenario_access_wait(root)
        scenario_repair_only(root)
        scenario_downgrade(root)
        scenario_latch(root)
    finally:
        scenario_blast_radius(before)

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
