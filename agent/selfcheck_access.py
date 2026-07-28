"""§access — the watch-folder ACCESS GATE (M4 · plan v2 Р3/Р6 · addendum §4).

Run it:

    python3 -m agent.selfcheck_access

WHAT THIS SUITE IS ABOUT
    macOS does not always answer. Measured on this machine 2026-07-25 (macOS
    26.5.2): with the frozen Mach-O helper as ``ProgramArguments[0]`` and no TCC
    grant yet, ``os.listdir`` on the watched folder did not return EPERM — it did
    not return AT ALL (>60 s, stack parked in ``__open_nocancel``, tccd silent),
    because the system wants to ask the human and a background LaunchAgent cannot
    show a dialog. The very same call under ``/bin/bash`` (a PLATFORM binary) is
    refused in ~200 ms.

    launchd never starts a second instance of the same label, so ONE wedged run is
    the whole product dead: ``folder_access`` is never published, the access card
    never appears, «Проверить снова» does nothing, and only a reboot changes it.
    That is precisely the "dropped a folder in, silence, no way to tell why"
    experience that got the app deleted once already.

HOW THE WEDGE IS REPRODUCED WITHOUT TCC
    ``chmod`` cannot simulate it — chmod ANSWERS (EACCES), and an answer is exactly
    what we do not get. So the syscall itself is replaced through
    ``sitecustomize.py`` (auto-imported at interpreter start, so the SHIPPING code
    runs verbatim — only what the syscall does changes): for one target directory,
    ``os.listdir`` / ``os.scandir`` first opens a FIFO that has no writer, which
    parks the caller in a REAL kernel ``open()`` — the same shape as the TCC wedge
    and just as unkillable short of process death.

NEGATIVE CONTROLS (three, because every check here could otherwise be vacuous)
    Each one takes a COPY of the shipping package, removes exactly one line, and
    proves the suite turns red without it:
      · no deadline in the probe        → the probe hangs forever (the mine itself);
      · no early exit on `denied`       → the library re-arms as "all new books";
      · no shutdown stop in the drain   → books nobody touched land at
                                          ``error: interrupted``.
    Every hang check runs in a BACKGROUND process under a ceiling: a regression must
    make this suite RED, never "still running" (a frozen verify reads as a hung
    machine, lesson `selfcheck-no-nested-regression`).

BLAST RADIUS
    Everything runs in a throwaway tree (``MP3TOM4B_SUPPORT_DIR`` /
    ``MP3TOM4B_WATCH_DIR``), no launchctl is ever invoked, and the final scenario
    asserts the real App Support tree / the real plist / the real log were not
    touched — a self-check once bootstrapped a live job by accident.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import selfcheck_blast_radius as blast_radius

REPO_ROOT = Path(__file__).resolve().parent.parent

# --- tiny assertion harness -------------------------------------------------

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _has_tools() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _make_mp3(path: Path, seconds: float = 1.0, title: str = "Глава") -> None:
    """A real (tiny) mp3 via an ffmpeg sine tone — the scanner really probes these."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-c:a", "libmp3lame", "-id3v2_version", "3",
         "-metadata", f"title={title}", str(path)],
        check=True, capture_output=True,
    )


# --- hang injection ---------------------------------------------------------

_SITECUSTOMIZE = '''\
"""Test-only syscall wedge (never shipped): parks one directory in the kernel.

Imported automatically by the interpreter, so the agent code under test runs
completely unmodified — only what os.listdir DOES changes, and only for the one
directory named in the environment.

The replacement is a CALLABLE INSTANCE, not a function, on purpose: pathlib (3.9)
keeps ``listdir = os.listdir`` as a plain class attribute, and a python *function*
put there would bind as a method and receive ``self`` as its first argument. An
instance is not a descriptor, so it behaves exactly like the builtin it replaces.
"""
import os
import threading

_FIFO = os.environ.get("MP3TOM4B_TEST_HANG_FIFO")
_PIDFILE = os.environ.get("MP3TOM4B_TEST_HANG_PIDFILE")
_RELEASE = os.environ.get("MP3TOM4B_TEST_HANG_RELEASE_S")


def _park():
    if _PIDFILE:
        with open(_PIDFILE, "a") as fh:
            fh.write("%d\\n" % os.getpid())
            fh.flush()
    if _RELEASE:
        # A writer shows up after N seconds -> the parked open() returns, exactly
        # like a human finally pressing «Разрешить» in the consent dialog.
        def _release():
            import time
            time.sleep(float(_RELEASE))
            os.close(os.open(_FIFO, os.O_WRONLY))
        threading.Thread(target=_release, daemon=True).start()
    os.close(os.open(_FIFO, os.O_RDONLY))   # blocks in the kernel until a writer


def _matches(path, key):
    tgt = os.environ.get(key)
    if not tgt:
        return False
    try:
        return os.path.realpath(str(path)) == os.path.realpath(tgt)
    except Exception:
        return False


class _WedgedListdir(object):
    def __init__(self, real):
        self._real = real

    def __call__(self, path=".", *a, **k):
        if _matches(path, "MP3TOM4B_TEST_HANG_DIR"):
            _park()
        out = self._real(path, *a, **k)
        # "The grant vanished right after the probe answered": the FIRST successful
        # listing of this directory is also its last. Reproduces the race between
        # the probe and the folder walk with no TCC involved.
        if _matches(path, "MP3TOM4B_TEST_REVOKE_AFTER_LISTDIR"):
            os.environ.pop("MP3TOM4B_TEST_REVOKE_AFTER_LISTDIR")
            os.chmod(str(path), 0o000)
        return out


os.listdir = _WedgedListdir(os.listdir)
'''


class Sandbox:
    """One throwaway tree + the environment every child process inherits."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.support = root / "support"
        self.watch = root / "watch"
        self.hangmod = root / "hangmod"
        self.fifo = root / "wedge.fifo"
        self.pids = root / "wedged.pids"
        self.nudges = root / "nudges.log"
        self.recorder = root / "recorder.sh"
        for d in (self.support, self.watch, self.hangmod):
            d.mkdir(parents=True, exist_ok=True)
        os.mkfifo(self.fifo)
        self.pids.write_text("", encoding="utf-8")
        (self.hangmod / "sitecustomize.py").write_text(_SITECUSTOMIZE, encoding="utf-8")
        self.recorder.write_text(
            f"#!/bin/sh\nprintf 'nudge %s\\n' \"$*\" >> {shlex.quote(str(self.nudges))}\n",
            encoding="utf-8",
        )
        self.recorder.chmod(0o755)

    # -- environments --------------------------------------------------------
    def env(self, hang_dir: Path | None = None,
            release_s: float | None = None, pkg_root: Path | None = None,
            **extra: str) -> dict:
        env = dict(os.environ)
        env.update({
            "MP3TOM4B_SUPPORT_DIR": str(self.support),
            "MP3TOM4B_WATCH_DIR": str(self.watch),
            "MP3TOM4B_COVER_WEB": "0",
            "MP3TOM4B_STABILITY_DEBOUNCE_S": "0",
            "MP3TOM4B_NUDGE_CMD": str(self.recorder),
            "PYTHONPATH": os.pathsep.join(
                [str(self.hangmod), str(pkg_root or REPO_ROOT)]
            ),
        })
        for key in ("MP3TOM4B_TEST_HANG_DIR", "MP3TOM4B_TEST_HANG_RELEASE_S",
                    "MP3TOM4B_TEST_REVOKE_AFTER_LISTDIR"):
            env.pop(key, None)
        if hang_dir is not None:
            env["MP3TOM4B_TEST_HANG_DIR"] = str(hang_dir)
        if release_s is not None:
            env["MP3TOM4B_TEST_HANG_RELEASE_S"] = str(release_s)
        env["MP3TOM4B_TEST_HANG_FIFO"] = str(self.fifo)
        env["MP3TOM4B_TEST_HANG_PIDFILE"] = str(self.pids)
        env.update(extra)
        # Isolation is checked, not assumed (.patches/005). Every path a child can
        # write through must live inside this sandbox; a child that would run
        # against the real tree must fail LOUDLY here, not look green while
        # journalling into the user's Application Support.
        # Set, never setdefault: an inherited TMPDIR is exactly what we are taking
        # away, so that a child's own mkdtemp lands inside this sandbox and dies
        # with it instead of accumulating in the user's temp dir.
        env["TMPDIR"] = str(self.root / "tmp")
        (self.root / "tmp").mkdir(parents=True, exist_ok=True)
        root = str(self.root.resolve())
        for key in ("MP3TOM4B_SUPPORT_DIR", "MP3TOM4B_WATCH_DIR", "TMPDIR"):
            value = env.get(key)
            if not value:
                raise RuntimeError(f"{key} is unset — refusing to run a child "
                                   f"against the live system")
            if not str(Path(value).resolve()).startswith(root):
                raise RuntimeError(f"{key}={value} points outside the sandbox "
                                   f"{root} — refusing to run")
        return env

    # -- process plumbing ----------------------------------------------------
    def reap_wedged(self) -> None:
        """Kill anything still parked on the FIFO (belt over ``os._exit``)."""
        try:
            pids = [int(x) for x in self.pids.read_text().split() if x.strip()]
        except (OSError, ValueError):
            pids = []
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.pids.write_text("", encoding="utf-8")

    def nudge_count(self) -> int:
        try:
            return len([ln for ln in self.nudges.read_text().splitlines() if ln.strip()])
        except OSError:
            return 0

    def state(self) -> dict:
        try:
            return json.loads((self.support / "state" / "state.json").read_text())
        except (OSError, ValueError):
            return {}

    def agent_block(self) -> dict:
        block = self.state().get("agent")
        return block if isinstance(block, dict) else {}

    def events(self) -> list[dict]:
        out = []
        for name in ("events.jsonl.1", "events.jsonl"):
            path = self.support / "state" / name
            try:
                for line in path.read_text().splitlines():
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                continue
        return out


def _run_bg(argv: list[str], env: dict, cwd: Path, ceiling_s: float,
            out: Path) -> tuple[bool, int | None, float]:
    """Run a child in the BACKGROUND under a ceiling. Returns (exited, rc, elapsed).

    Backgrounded on purpose: a plain blocking call would inherit the wedge and
    freeze this whole suite if a guard ever regressed. A regression has to be RED,
    never "still running".
    """
    t0 = time.monotonic()
    with out.open("w", encoding="utf-8") as fh:
        proc = subprocess.Popen(argv, env=env, cwd=str(cwd), stdout=fh,
                                stderr=subprocess.STDOUT)
        deadline = t0 + ceiling_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return True, proc.returncode, round(time.monotonic() - t0, 2)
            time.sleep(0.1)
        proc.kill()
        proc.wait(timeout=5)
        return False, None, round(time.monotonic() - t0, 2)


def _driver(box: Sandbox, code: str, env: dict, cwd: Path | None = None,
            ceiling_s: float = 20.0, name: str = "driver") -> tuple[bool, int | None, str]:
    """Run a python snippet as a child; returns (exited, rc, captured output)."""
    out = box.root / f"{name}.out"
    exited, rc, _ = _run_bg([sys.executable, "-c", code], env, cwd or REPO_ROOT,
                            ceiling_s, out)
    try:
        text = out.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    return exited, rc, text


# --- package copies for the negative controls -------------------------------


def _neutered_copy(root: Path, name: str, edits: list[tuple[str, str, str]]) -> Path | None:
    """Copy the shipping package and remove exactly one guard from it.

    ``edits`` is a list of (relative file, exact old text, new text). Each old text
    MUST occur exactly once — otherwise the control would silently test nothing,
    which is worse than not having it, so we refuse and report.
    """
    dst = root / name
    if dst.exists():
        shutil.rmtree(dst)
    (dst / "agent").mkdir(parents=True)
    for src in (REPO_ROOT / "agent").glob("*.py"):
        shutil.copy2(src, dst / "agent" / src.name)
    for rel, old, new in edits:
        path = dst / rel
        text = path.read_text(encoding="utf-8")
        if text.count(old) != 1:
            check(f"negative control «{name}»: anchor «{old[:48]}…» occurs "
                  f"{text.count(old)}× (expected 1)", False,
                  "the shipping code changed shape — the control would be vacuous")
            return None
        path.write_text(text.replace(old, new), encoding="utf-8")
    return dst


# ============================================================================
# A. probe verdicts on real directories (no injection)
# ============================================================================


def scenario_verdicts(box: Sandbox, scan) -> None:
    print("\n— A: probe verdicts (ok / denied / missing / not-a-dir) —")
    good = box.root / "verdicts"
    good.mkdir()
    check("probe: readable folder → ok",
          scan.probe_watch_dir_access(good) == scan.ACCESS_OK)
    os.chmod(good, 0o000)
    try:
        verdict = scan.probe_watch_dir_access(good)
    finally:
        os.chmod(good, 0o755)
    check("probe: chmod 000 → denied (EPERM/EACCES merged on purpose)",
          verdict == scan.ACCESS_DENIED, verdict)
    check("probe: absent folder → missing",
          scan.probe_watch_dir_access(good / "nope") == scan.ACCESS_MISSING)
    afile = good / "a.txt"
    afile.write_text("x", encoding="utf-8")
    check("probe: a FILE where a folder is expected → missing (ENOTDIR)",
          scan.probe_watch_dir_access(afile) == scan.ACCESS_MISSING)
    check("probe: every verdict is one of the four published values",
          set(scan.ACCESS_VALUES) == {"ok", "denied", "missing", "blocked"},
          str(scan.ACCESS_VALUES))


# ============================================================================
# B. blocked — the syscall never returns
# ============================================================================


def scenario_blocked(box: Sandbox) -> None:
    print("\n— B: wedged listdir → blocked (the watchdog must answer anyway) —")
    hang = box.root / "wedged"
    hang.mkdir()
    box.pids.write_text("", encoding="utf-8")
    code = (
        "import os, sys, time\n"
        "t0 = time.time()\n"
        "from agent import scan\n"
        "v = scan.probe_watch_dir_access(os.environ['MP3TOM4B_TEST_PROBE_DIR'])\n"
        "print('VERDICT=%s ELAPSED=%.2f WEDGED=%s' % (v, time.time()-t0,\n"
        "      scan.probe_thread_wedged()))\n"
        "sys.stdout.flush()\n"
    )
    env = box.env(hang_dir=hang, MP3TOM4B_PROBE_FAST_S="1",
                  MP3TOM4B_TEST_PROBE_DIR=str(hang))
    exited, rc, text = _driver(box, code, env, ceiling_s=12.0, name="blocked")
    check("wedged listdir: the probe process EXITED (it did not hang)", exited,
          text.strip()[-160:])
    verdict = ""
    elapsed = 99.0
    for token in text.split():
        if token.startswith("VERDICT="):
            verdict = token.split("=", 1)[1]
        if token.startswith("ELAPSED="):
            elapsed = float(token.split("=", 1)[1])
    check("wedged listdir → blocked (not denied, not empty)",
          verdict == "blocked", verdict or text.strip()[-160:])
    check("the deadline drove the answer (≈1 s deadline, answered <5 s)",
          elapsed < 5.0, f"{elapsed}s")
    check("the wedged run reports its probe thread as wedged",
          "WEDGED=True" in text, text.strip()[-160:])
    wedged_pid = (box.pids.read_text().split() or [""])[0]
    if not wedged_pid:
        check("hang injection fired (a pid was recorded)", False,
              "no pid → every blocked check above would be vacuous")
    else:
        alive = True
        for _ in range(20):
            try:
                os.kill(int(wedged_pid), 0)
            except OSError:
                alive = False
                break
            time.sleep(0.1)
        check("the wedged process is GONE (the stuck thread left with it)",
              not alive, f"pid {wedged_pid}")
    box.reap_wedged()

    # The guard must change nothing on the happy path: same folder, no injection.
    exited2, _, text2 = _driver(box, code,
                                box.env(MP3TOM4B_PROBE_FAST_S="1",
                                        MP3TOM4B_TEST_PROBE_DIR=str(hang)),
                                ceiling_s=12.0, name="blocked_happy")
    check("same folder without injection → ok (the guard is invisible when healthy)",
          exited2 and "VERDICT=ok" in text2, text2.strip()[-160:])


def scenario_blocked_negative(box: Sandbox) -> None:
    print("\n— B′: NEGATIVE CONTROL — strip the deadline, the probe hangs —")
    pkg = _neutered_copy(
        box.root, "noguard_probe",
        [("agent/scan.py", "self._done.wait(seconds)", "self._done.wait()")],
    )
    if pkg is None:
        return
    check("negative control: built a deadline-less copy of the probe", True)
    hang = box.root / "wedged"
    box.pids.write_text("", encoding="utf-8")
    code = (
        "import os, sys\n"
        "from agent import scan\n"
        "print('VERDICT=' + scan.probe_watch_dir_access(\n"
        "      os.environ['MP3TOM4B_TEST_HANG_DIR']))\n"
        "sys.stdout.flush()\n"
    )
    env = box.env(hang_dir=hang, pkg_root=pkg, MP3TOM4B_PROBE_FAST_S="1")
    exited, _, text = _driver(box, code, env, cwd=pkg, ceiling_s=8.0,
                              name="noguard_probe")
    check("without the deadline the SAME input hangs past 8 s — the guard is what "
          "makes this suite green", not exited, text.strip()[-160:])
    box.reap_wedged()


# ============================================================================
# C. the consent window: an answer that arrives late still counts
# ============================================================================


def scenario_consent_window(box: Sandbox) -> None:
    print("\n— C: the human answers during the consent window → ok in the SAME tick —")
    watch = box.root / "consent_watch"
    _make_mp3(watch / "Книга К" / "01.mp3", title="Глава 1")
    support = box.root / "consent_support"
    code = (
        "import json, os, sys\n"
        "from agent import scan\n"
        "sc = scan.run_scan(scan.watch_dir())\n"
        "print('RESULT=' + json.dumps({'access': sc['agent'].get('folder_access'),\n"
        "      'books': len(sc.get('books', []))}))\n"
        "sys.stdout.flush()\n"
    )
    env = box.env(hang_dir=watch, release_s=2.5,
                  MP3TOM4B_PROBE_FAST_S="1", MP3TOM4B_CONSENT_WINDOW_S="20")
    env["MP3TOM4B_WATCH_DIR"] = str(watch)
    env["MP3TOM4B_SUPPORT_DIR"] = str(support)
    exited, _, text = _driver(box, code, env, ceiling_s=40.0, name="consent")
    payload = {}
    for line in text.splitlines():
        if line.startswith("RESULT="):
            payload = json.loads(line[len("RESULT="):])
    check("consent window: the run finished (no hang)", exited, text.strip()[-200:])
    check("late «Разрешить» → the tick ends at ok, not blocked",
          payload.get("access") == "ok", json.dumps(payload))
    check("late «Разрешить» → the scan really ran (the book was found)",
          payload.get("books") == 1, json.dumps(payload))
    # The blocked state must have been published BEFORE the window, not after it.
    events = []
    try:
        for line in (support / "state" / "events.jsonl").read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))
    except (OSError, ValueError):
        pass
    kinds = [e.get("event") for e in events]
    check("blocked was published immediately, before the window was entered",
          "folder_access_probe" in kinds and "folder_access_consent_window" in kinds
          and kinds.index("folder_access_probe")
          < kinds.index("folder_access_consent_window"), str(kinds[:6]))
    box.reap_wedged()


# ============================================================================
# D/E. Р3 — a refused probe must not re-arm the library
# ============================================================================


def _seed_library(box: Sandbox, scan, dispatcher, state, config) -> dict:
    """One scanned book flipped to ``done`` + one flipped to ``skipped``."""
    _make_mp3(box.watch / "Книга А" / "01.mp3", title="Глава 1")
    _make_mp3(box.watch / "Книга Б" / "01.mp3", title="Глава 1")
    scan.run_scan(box.watch)
    ids = {}
    for path in sorted(config.books_dir().glob("*.json")):
        man = json.loads(path.read_text())
        name = Path(man["src_dir"]).name
        ids[name] = man["book_id"]
        man["status"] = "done" if name == "Книга А" else "skipped"
        if name == "Книга А":
            man["result"] = {"output": "a.m4b", "output_path": str(box.root / "a.m4b"),
                             "built_at": time.time()}
        path.write_text(json.dumps(man, ensure_ascii=False), encoding="utf-8")
    scan.run_scan(box.watch)      # settle the presence ledger on the new statuses
    return ids


def _statuses(config) -> dict:
    out = {}
    for path in sorted(config.books_dir().glob("*.json")):
        man = json.loads(path.read_text())
        out[Path(man["src_dir"]).name] = man.get("status")
    return out


def scenario_r3(box: Sandbox, scan, dispatcher, state, config) -> dict:
    print("\n— D: Р3 — `denied` freezes the showcase and touches NO ledger —")
    ids = _seed_library(box, scan, dispatcher, state, config)
    presence = config.presence_file()
    notified = config.notified_file()
    before = {
        "presence": presence.read_bytes() if presence.exists() else b"",
        "notified": notified.read_bytes() if notified.exists() else b"",
        "statuses": _statuses(config),
        "state_books": len(box.state().get("books", [])),
    }
    nudges_before = box.nudge_count()

    os.chmod(box.watch, 0o000)
    try:
        showcase = scan.run_scan(box.watch)
    finally:
        os.chmod(box.watch, 0o755)

    agent_block = showcase.get("agent", {})
    check("denied: published folder_access=denied", agent_block.get("folder_access") == "denied",
          json.dumps(agent_block, ensure_ascii=False))
    check("denied: folder_access_ts is fresh and sub-second (opaque token)",
          isinstance(agent_block.get("folder_access_ts"), str)
          and "." in (agent_block.get("folder_access_ts") or ""),
          str(agent_block.get("folder_access_ts")))
    check("denied: presence.json byte-for-byte untouched",
          (presence.read_bytes() if presence.exists() else b"") == before["presence"])
    check("denied: notified.json byte-for-byte untouched",
          (notified.read_bytes() if notified.exists() else b"") == before["notified"])
    check("denied: every manifest status is unchanged (skip mark survives)",
          _statuses(config) == before["statuses"], json.dumps(_statuses(config)))
    check("denied: the showcase is carried forward, not emptied",
          len(showcase.get("books", [])) == before["state_books"],
          f"{len(showcase.get('books', []))} vs {before['state_books']}")
    check("denied: the app was raised exactly once (rising edge)",
          box.nudge_count() == nudges_before + 1,
          f"{nudges_before} → {box.nudge_count()}")

    # A second denied tick is not a new edge.
    os.chmod(box.watch, 0o000)
    try:
        scan.run_scan(box.watch)
    finally:
        os.chmod(box.watch, 0o755)
    check("denied → denied does NOT re-raise the app",
          box.nudge_count() == nudges_before + 1, str(box.nudge_count()))

    showcase = scan.run_scan(box.watch)
    check("denied → ok: the library is NOT re-armed (Р3 holds)",
          _statuses(config) == before["statuses"], json.dumps(_statuses(config)))
    check("denied → ok: no book_rearmed_reappeared event was journalled",
          not any(e.get("event") == "book_rearmed_reappeared" for e in box.events()))
    check("denied → ok: folder_access is back to ok",
          showcase["agent"].get("folder_access") == "ok")
    return ids


def scenario_blocked_no_rearm(box: Sandbox, scan, config) -> None:
    print("\n— E: `blocked` → ok does not re-arm either (mirror of Р3) —")
    before = _statuses(config)
    watch = box.watch
    hang = box.root / "e_wedge"
    hang.mkdir(exist_ok=True)
    # A blocked verdict published through the same path the probe uses, then a
    # normal scan: the library must survive the round-trip exactly like `denied`.
    scan.publish_folder_access(scan.ACCESS_BLOCKED, watch)
    check("blocked: published folder_access=blocked",
          box.agent_block().get("folder_access") == "blocked",
          json.dumps(box.agent_block(), ensure_ascii=False))
    check("blocked: the showcase kept its books (frozen, not emptied)",
          len(box.state().get("books", [])) == len(before), str(before))
    scan.run_scan(watch)
    check("blocked → ok: statuses unchanged (no re-arm)",
          _statuses(config) == before, json.dumps(_statuses(config)))


def scenario_grant_lost_midscan(box: Sandbox) -> None:
    print("\n— E′: the grant vanishes BETWEEN the probe and the walk —")
    root = box.root / "midscan"
    watch = root / "watch"
    support = root / "support"
    watch.mkdir(parents=True)
    support.mkdir(parents=True)
    _make_mp3(watch / "Книга И" / "01.mp3", title="Глава 1")
    # The injection lets the probe's listing through and revokes the folder right
    # after it — a probe answers about the PAST, and TCC can change under us at any
    # instant. The run must publish `denied` instead of dying with a traceback and
    # publishing nothing (which is the same silent death, one function later).
    code = (
        "import json, os, sys\n"
        "from agent import scan\n"
        "from pathlib import Path\n"
        "w = Path(os.environ['MP3TOM4B_WATCH_DIR'])\n"
        "try:\n"
        "    sc = scan.run_scan(w)\n"
        "    print('RESULT=' + json.dumps({'access': sc['agent'].get('folder_access'),\n"
        "          'books': len(sc.get('books', []))}))\n"
        "finally:\n"
        "    os.chmod(str(w), 0o755)\n"
        "sys.stdout.flush()\n"
    )
    env = box.env(MP3TOM4B_TEST_REVOKE_AFTER_LISTDIR=str(watch))
    env["MP3TOM4B_WATCH_DIR"] = str(watch)
    env["MP3TOM4B_SUPPORT_DIR"] = str(support)
    exited, rc, text = _driver(box, code, env, ceiling_s=30.0, name="midscan")
    payload = {}
    for line in text.splitlines():
        if line.startswith("RESULT="):
            payload = json.loads(line[len("RESULT="):])
    check("grant lost mid-scan: the run survived (no unhandled PermissionError)",
          exited and rc == 0, f"rc={rc} · {text.strip()[-200:]}")
    check("grant lost mid-scan: `denied` is published, not a traceback",
          payload.get("access") == "denied", json.dumps(payload) or text[-200:])
    try:
        events = [json.loads(ln) for ln in
                  (support / "state" / "events.jsonl").read_text().splitlines()
                  if ln.strip()]
    except (OSError, ValueError):
        events = []
    check("grant lost mid-scan: journalled as folder_access_lost",
          any(e.get("event") == "folder_access_lost" for e in events),
          str([e.get("event") for e in events]))


def scenario_r3_negative(box: Sandbox) -> None:
    print("\n— D′: NEGATIVE CONTROL — remove the early exit, the library re-arms —")
    pkg = _neutered_copy(
        box.root, "noguard_r3",
        [("agent/scan.py", "    if _access_blocks_scan(access_fields):",
          "    if False and _access_blocks_scan(access_fields):")],
    )
    if pkg is None:
        return
    check("negative control: built an early-exit-less copy of run_scan", True)
    root = box.root / "r3neg"
    # The vehicle is a ONE-TICK disappearance, not a chmod, and that is on purpose.
    # MEASURED here (python 3.9 pathlib): `Path.is_dir()` does NOT swallow
    # EACCES/EPERM — `_ignore_error` covers only ENOENT/ENOTDIR/EBADF/ELOOP — so a
    # refused folder makes the walk RAISE (that variant is covered by E′). The
    # SILENT variant, where the walk simply returns nothing and the library empties
    # itself, is the vanished folder: ENOENT is swallowed, the presence ledger
    # flips every book to absent, and the next good scan reads absent→present on a
    # `done` book as a conscious re-drop and re-arms it. That is the "all my books
    # came back as new" report, and the early exit + the transient `missing` budget
    # are what stand between the user and it — remove them and it happens.
    watch = root / "watch"
    support = root / "support"
    stash = root / "watch-stashed"
    watch.mkdir(parents=True)
    support.mkdir(parents=True)
    _make_mp3(watch / "Книга В" / "01.mp3", title="Глава 1")
    code = (
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "from agent import config, scan\n"
        "w = Path(os.environ['MP3TOM4B_WATCH_DIR'])\n"
        "stash = w.parent / 'watch-stashed'\n"
        "scan.run_scan(w)\n"
        "for p in config.books_dir().glob('*.json'):\n"
        "    m = json.loads(p.read_text()); m['status'] = 'done'\n"
        "    m['result'] = {'output': 'x.m4b', 'output_path': '/tmp/x.m4b',\n"
        "                   'built_at': time.time()}\n"
        "    p.write_text(json.dumps(m, ensure_ascii=False))\n"
        "scan.run_scan(w)\n"
        "w.rename(stash)\n"
        "try:\n"
        "    print('BLIP_PROBE=' + scan.probe_watch_dir_access(w))\n"
        "    scan.run_scan(w)\n"
        "finally:\n"
        "    stash.rename(w)\n"
        "scan.run_scan(w)\n"
        "print('STATUSES=' + json.dumps([json.loads(p.read_text())['status']\n"
        "      for p in sorted(config.books_dir().glob('*.json'))]))\n"
        "sys.stdout.flush()\n"
    )
    env = box.env(pkg_root=pkg)
    env["MP3TOM4B_WATCH_DIR"] = str(watch)
    env["MP3TOM4B_SUPPORT_DIR"] = str(support)
    exited, _, text = _driver(box, code, env, cwd=pkg, ceiling_s=60.0, name="r3neg")
    statuses = []
    for line in text.splitlines():
        if line.startswith("STATUSES="):
            statuses = json.loads(line[len("STATUSES="):])
    check("negative control ran to completion", exited, text.strip()[-200:])
    check("negative control really staged the blip (not a vacuous run)",
          "BLIP_PROBE=missing" in text, text.strip()[-200:])
    check("without the early exit ONE bad tick re-arms the done book as new — so "
          "the Р3 / transient-missing checks above have teeth",
          statuses == ["pending-confirm"], json.dumps(statuses))


# ============================================================================
# F. missing is transient
# ============================================================================


def scenario_missing_transient(box: Sandbox, scan, state, config) -> None:
    print("\n— F: `missing` is transient (one blip must not empty the library) —")
    presence = config.presence_file()
    before_presence = presence.read_bytes() if presence.exists() else b""
    before_status = _statuses(config)

    # The folder really goes away — sources and all — so `missing` is the honest
    # verdict AND a destructive reconcile would really have something to destroy.
    gone = box.watch
    stash = box.root / "watch-stashed"
    gone.rename(stash)
    showcase = scan.run_scan(gone)
    agent_block = showcase.get("agent", {})
    check("missing #1: published folder_access=missing",
          agent_block.get("folder_access") == "missing", json.dumps(agent_block))
    check("missing #1: streak counter armed at 1",
          agent_block.get("folder_missing_streak") == 1, json.dumps(agent_block))
    check("missing #1: presence.json untouched (no destructive reconcile)",
          (presence.read_bytes() if presence.exists() else b"") == before_presence)
    check("missing #1: manifests untouched", _statuses(config) == before_status)

    showcase = scan.run_scan(gone)
    check("missing #2: streak grows, still transient (needs BOTH scans and time)",
          showcase["agent"].get("folder_missing_streak") == 2
          and (presence.read_bytes() if presence.exists() else b"") == before_presence,
          json.dumps(showcase["agent"]))

    # THE user-visible assertion: a blip that heals must leave the library alone.
    stash.rename(gone)
    scan.run_scan(gone)
    check("a blip that heals leaves every book exactly as it was — no «all my "
          "books came back as new»",
          _statuses(config) == before_status, json.dumps(_statuses(config)))
    check("a healed blip journalled no re-arm",
          not any(e.get("event") == "book_rearmed_reappeared" for e in box.events()))
    gone.rename(stash)
    scan.run_scan(gone)

    # Age the counter past the window: the reconcile is now allowed to be honest.
    cur = json.loads((box.support / "state" / "state.json").read_text())
    cur["agent"]["folder_missing_since"] = time.time() - scan.MISSING_TRANSIENT_S - 60
    (box.support / "state" / "state.json").write_text(
        json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    scan.run_scan(gone)
    after = json.loads(presence.read_text()) if presence.exists() else {"books": {}}
    absent = [b for b, e in (after.get("books") or {}).items()
              if isinstance(e, dict) and e.get("present") is False]
    check("missing, settled (≥2 scans AND ≥10 min): the reconcile finally runs",
          bool(absent), json.dumps(after)[:200])
    check("a settled `missing` still leaves the manifests alone (no status rewrite)",
          _statuses(config) == before_status, json.dumps(_statuses(config)))

    # Back to a readable folder → the counters must reset, not linger.
    stash.rename(gone)
    showcase = scan.run_scan(box.watch)
    check("ok resets the transient counters",
          "folder_missing_streak" not in showcase["agent"]
          and "folder_missing_since" not in showcase["agent"],
          json.dumps(showcase["agent"], ensure_ascii=False))


# ============================================================================
# G. recheck-access
# ============================================================================


def scenario_recheck(box: Sandbox, scan, dispatcher, state, config) -> None:
    print("\n— G: «Проверить снова» = a command, not a kickstart —")
    before_ts = box.agent_block().get("folder_access_ts")
    before_presence = (config.presence_file().read_bytes()
                       if config.presence_file().exists() else b"")
    cmd = config.commands_dir() / "recheck-1.json"
    cmd.write_text(json.dumps({"action": "recheck-access", "ts": time.time()}),
                   encoding="utf-8")
    built = dispatcher.handle_command(cmd)
    check("recheck-access: never builds", built is False)
    check("recheck-access: the command file is consumed", not cmd.exists())
    after = box.agent_block()
    check("recheck-access: folder_access_ts MOVED even though the verdict is the same",
          after.get("folder_access_ts") != before_ts,
          f"{before_ts} → {after.get('folder_access_ts')}")
    check("recheck-access: the verdict is honest (ok on a readable folder)",
          after.get("folder_access") == "ok", json.dumps(after, ensure_ascii=False))
    check("recheck-access: no ledger was written",
          (config.presence_file().read_bytes()
           if config.presence_file().exists() else b"") == before_presence)
    check("recheck-access: journalled for diagnosis",
          any(e.get("event") == "recheck_access" for e in box.events()))

    # And it works in the state it exists for: from a denied folder.
    os.chmod(box.watch, 0o000)
    try:
        cmd2 = config.commands_dir() / "recheck-2.json"
        cmd2.write_text(json.dumps({"action": "recheck-access", "ts": time.time()}),
                        encoding="utf-8")
        dispatcher.handle_command(cmd2)
    finally:
        os.chmod(box.watch, 0o755)
    check("recheck-access from a denied folder republishes denied (not a timeout)",
          box.agent_block().get("folder_access") == "denied",
          json.dumps(box.agent_block(), ensure_ascii=False))
    scan.run_scan(box.watch)   # leave the tree at ok for the suites that follow


# ============================================================================
# H. the drain stops on shutdown instead of lying
# ============================================================================


def scenario_drain_shutdown(box: Sandbox, scan, dispatcher, state, config, shutdown) -> None:
    print("\n— H: a signal mid-drain must not brand untouched books as interrupted —")
    root = box.root / "drain"
    watch = root / "watch"
    watch.mkdir(parents=True)
    for name in ("Книга Г", "Книга Д"):
        _make_mp3(watch / name / "01.mp3", title="Глава 1")
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)
    try:
        scan.run_scan(watch)
        queued = []
        targets = []
        for path in sorted(config.books_dir().glob("*.json")):
            man = json.loads(path.read_text())
            if man.get("status") != "pending-confirm":
                continue
            if Path(man["src_dir"]).parent != watch:
                continue      # only THIS scenario's two books
            targets.append(path)
            cmd = config.commands_dir() / f"build-{man['book_id']}.json"
            cmd.write_text(json.dumps({
                "action": "confirm-build", "book_id": man["book_id"],
                "source_rev": man["source_rev"], "confirm_token": man["confirm_token"],
                # D17: echo the build_token, like the app does.
                "build_token": man.get("build_token"),
                "idempotency_key": f"k-{man['book_id']}", "ts": time.time(),
            }, ensure_ascii=False), encoding="utf-8")
            queued.append(cmd)
        check("drain fixture: two confirm-build commands are queued",
              len(queued) == 2, str([c.name for c in queued]))

        shutdown.request()
        try:
            handled = dispatcher.drain_commands()
        finally:
            shutdown.reset()

        statuses = [json.loads(p.read_text()).get("status") for p in targets]
        check("shutdown mid-drain: nothing was handled", handled == 0, str(handled))
        check("shutdown mid-drain: the queued books keep pending-confirm — none is "
              "branded «error: interrupted»",
              all(s == "pending-confirm" for s in statuses), json.dumps(statuses))
        check("shutdown mid-drain: the command files stay on disk for the next tick",
              all(c.exists() for c in queued))
        check("shutdown mid-drain: journalled as drain_stopped",
              any(e.get("event") == "drain_stopped" for e in box.events()))
    finally:
        os.environ["MP3TOM4B_WATCH_DIR"] = str(box.watch)


def scenario_drain_shutdown_negative(box: Sandbox) -> None:
    print("\n— H′: NEGATIVE CONTROL — no stop in the drain → the false «interrupted» —")
    pkg = _neutered_copy(
        box.root, "noguard_drain",
        [("agent/dispatcher.py",
          "    for index, command_path in enumerate(files):\n"
          "        if shutdown.requested():",
          "    for index, command_path in enumerate(files):\n"
          "        if False:")],
    )
    if pkg is None:
        return
    check("negative control: built a drain without the shutdown stop", True)
    root = box.root / "drainneg"
    watch = root / "watch"
    support = root / "support"
    watch.mkdir(parents=True)
    support.mkdir(parents=True)
    for name in ("Книга Е", "Книга Ж"):
        _make_mp3(watch / name / "01.mp3", title="Глава 1")
    code = (
        "import json, os, sys, time\n"
        "from pathlib import Path\n"
        "from agent import config, dispatcher, scan, shutdown\n"
        "w = Path(os.environ['MP3TOM4B_WATCH_DIR'])\n"
        "scan.run_scan(w)\n"
        "for p in sorted(config.books_dir().glob('*.json')):\n"
        "    m = json.loads(p.read_text())\n"
        "    (config.commands_dir() / ('b-' + m['book_id'] + '.json')).write_text(\n"
        "        json.dumps({'action': 'confirm-build', 'book_id': m['book_id'],\n"
        "                    'source_rev': m['source_rev'],\n"
        "                    'confirm_token': m['confirm_token'],\n"
        "                    'build_token': m.get('build_token'),\n"
        "                    'idempotency_key': 'k-' + m['book_id'],\n"
        "                    'ts': time.time()}))\n"
        "shutdown.request()\n"
        "dispatcher.drain_commands()\n"
        "print('STATUSES=' + json.dumps([json.loads(p.read_text()).get('status')\n"
        "      for p in sorted(config.books_dir().glob('*.json'))]))\n"
        "sys.stdout.flush()\n"
    )
    env = box.env(pkg_root=pkg)
    env["MP3TOM4B_WATCH_DIR"] = str(watch)
    env["MP3TOM4B_SUPPORT_DIR"] = str(support)
    exited, _, text = _driver(box, code, env, cwd=pkg, ceiling_s=90.0, name="drainneg")
    statuses = []
    for line in text.splitlines():
        if line.startswith("STATUSES="):
            statuses = json.loads(line[len("STATUSES="):])
    check("negative control ran to completion", exited, text.strip()[-200:])
    check("without the stop, books nobody touched land at «error» — so the check "
          "above has teeth", "error" in statuses, json.dumps(statuses))


# ============================================================================
# I. the phase deadline (something OTHER than the probe wedges)
# ============================================================================


def scenario_phase_deadline(box: Sandbox) -> None:
    print("\n— I: the phase deadline — a wedge after the probe still ends the run —")
    root = box.root / "phase"
    watch = root / "watch"
    support = root / "support"
    book = watch / "Книга З"
    watch.mkdir(parents=True)
    support.mkdir(parents=True)
    _make_mp3(book / "01.mp3", title="Глава 1")
    box.pids.write_text("", encoding="utf-8")
    # The wedge is on the BOOK subfolder, not the watch root: the probe lists the
    # root and cheerfully answers `ok`, and the folder walk then parks in the kernel
    # one level down. A protected tree behaves exactly like this — which is the
    # whole point: the probe's own watchdog cannot save a run that wedges after it.
    # Only the phase deadline can.
    env = box.env(hang_dir=book, MP3TOM4B_PHASE_DEADLINE_S="3")
    env["MP3TOM4B_WATCH_DIR"] = str(watch)
    env["MP3TOM4B_SUPPORT_DIR"] = str(support)
    out = box.root / "phase.out"
    exited, rc, elapsed = _run_bg(
        [sys.executable, "-m", "agent", "--scan"], env, REPO_ROOT, 25.0, out)
    text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
    check("phase deadline: the agent EXITED on a wedge the probe cannot see",
          exited, f"{elapsed}s · {text.strip()[-200:]}")
    check("phase deadline: exit code 75 (EX_TEMPFAIL, not a fake success)",
          rc == 75, str(rc))
    check("phase deadline: it fired on the clock, not by luck",
          elapsed < 15.0, f"{elapsed}s with a 3 s deadline")
    agent_block = {}
    events = []
    try:
        agent_block = json.loads(
            (support / "state" / "state.json").read_text()).get("agent", {})
    except (OSError, ValueError):
        pass
    try:
        for line in (support / "state" / "events.jsonl").read_text().splitlines():
            if line.strip():
                events.append(json.loads(line))
    except (OSError, ValueError):
        pass
    check("phase deadline: `blocked` was published so the UI can explain itself",
          agent_block.get("folder_access") == "blocked",
          json.dumps(agent_block, ensure_ascii=False))
    check("phase deadline: journalled with the phase name and the diagnosis",
          any(e.get("event") == "phase_deadline_exceeded"
              and e.get("phase") == "probe+scan+publish" for e in events),
          str([e.get("event") for e in events]))
    box.reap_wedged()


# ============================================================================
# J. journal rotation (Р6)
# ============================================================================


def scenario_journal_rotation(box: Sandbox, state, config) -> None:
    print("\n— J: journal rotation keeps the gate invariants readable (Р6) —")
    root = box.root / "journal"
    support = root / "support"
    (support / "state").mkdir(parents=True)
    events = support / "state" / "events.jsonl"
    events.write_text(
        json.dumps({"event": "confirm_accepted", "ts": 1.0}) + "\n"
        + json.dumps({"event": "filler", "ts": 2.0, "pad": "x" * 400}) + "\n",
        encoding="utf-8",
    )
    code = (
        "import json, sys\n"
        "from agent import config, state\n"
        "rotated = state.rotate_events_if_needed()\n"
        "state.append_event('build_started', book_id='b1')\n"
        "recs = state.read_events()\n"
        "print('RESULT=' + json.dumps({\n"
        "    'rotated': rotated,\n"
        "    'prev_exists': config.events_prev_file().exists(),\n"
        "    'live_only': len(config.events_file().read_text().splitlines()),\n"
        "    'kinds': [r.get('event') for r in recs]}))\n"
        "sys.stdout.flush()\n"
    )
    env = box.env(MP3TOM4B_EVENTS_ROTATE_BYTES="100")
    env["MP3TOM4B_SUPPORT_DIR"] = str(support)
    exited, _, text = _driver(box, code, env, ceiling_s=20.0, name="journal")
    payload = {}
    for line in text.splitlines():
        if line.startswith("RESULT="):
            payload = json.loads(line[len("RESULT="):])
    check("rotation: ran at process start", exited and payload.get("rotated") is True,
          text.strip()[-200:])
    check("rotation: exactly one generation kept (events.jsonl.1)",
          payload.get("prev_exists") is True, json.dumps(payload))
    check("rotation: the live file really was truncated to the new run",
          payload.get("live_only") == 1, json.dumps(payload))
    kinds = payload.get("kinds") or []
    check("read_events() reads .1 + live as ONE oldest-first sequence",
          kinds == ["confirm_accepted", "filler", "build_started"], json.dumps(kinds))
    check("the gate invariant survives rotation: build_started still has its "
          "preceding confirm_accepted",
          "confirm_accepted" in kinds and "build_started" in kinds
          and kinds.index("confirm_accepted") < kinds.index("build_started"),
          json.dumps(kinds))
    # A small journal must NOT rotate (rotation is a safety valve, not a habit).
    code_small = (
        "import json, sys\n"
        "from agent import config, state\n"
        "print('RESULT=' + json.dumps({'rotated': state.rotate_events_if_needed()}))\n"
        "sys.stdout.flush()\n"
    )
    env2 = box.env()
    env2["MP3TOM4B_SUPPORT_DIR"] = str(support)
    _, _, text2 = _driver(box, code_small, env2, ceiling_s=20.0, name="journal_small")
    check("rotation: a journal under the threshold is left alone",
          '"rotated": false' in text2, text2.strip()[-120:])


# ============================================================================
# K. blast radius — the live system must be exactly as we found it
# ============================================================================

def _blast_snapshot() -> dict:
    """One shared implementation (:mod:`agent.selfcheck_blast_radius`).

    Not a local copy: the guard has to tell a self-check writing into the live
    install apart from the USER'S OWN AGENT, which since 1.0 ticks every 300 s and
    therefore inside this suite's runtime. Two divergent copies of that judgement
    is how a guard rots.
    """
    return blast_radius.snapshot()


def scenario_blast_radius(before: dict) -> None:
    print("\n— K: blast radius — the live system is untouched —")
    damage = blast_radius.diff(before, blast_radius.snapshot())
    check("blast_radius: this suite did not touch the user's install",
          damage == [], "; ".join(damage)[:400])


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not _has_tools():
        print("§access self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-access-"))
    box = Sandbox(root)

    # An interrupted run must not leave its fixtures behind (.patches/005 item 3:
    # abandoned self-check trees had grown to gigabytes). SIGKILL cannot be caught;
    # everything else cleans up.
    def _bail(signum, frame):  # noqa: ANN001 - stdlib handler shape
        box.reap_wedged()
        shutil.rmtree(root, ignore_errors=True)
        os._exit(130)

    for signame in ("SIGTERM", "SIGHUP", "SIGINT"):
        signum = getattr(signal, signame, None)
        if signum is not None:
            try:
                signal.signal(signum, _bail)
            except (OSError, ValueError, RuntimeError):
                pass

    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(box.support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(box.watch)
    os.environ["MP3TOM4B_COVER_WEB"] = "0"
    os.environ["MP3TOM4B_STABILITY_DEBOUNCE_S"] = "0"
    os.environ["MP3TOM4B_NUDGE_CMD"] = str(box.recorder)

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from agent import config, dispatcher, scan, shutdown, state  # noqa: E402

    print(f"self-check tree: {root}\n  support: {box.support}\n  watch:   {box.watch}\n")

    before = _blast_snapshot()
    try:
        scenario_verdicts(box, scan)
        scenario_blocked(box)
        scenario_blocked_negative(box)
        scenario_consent_window(box)
        scenario_r3(box, scan, dispatcher, state, config)
        scenario_blocked_no_rearm(box, scan, config)
        scenario_grant_lost_midscan(box)
        scenario_r3_negative(box)
        scenario_missing_transient(box, scan, state, config)
        scenario_recheck(box, scan, dispatcher, state, config)
        scenario_drain_shutdown(box, scan, dispatcher, state, config, shutdown)
        scenario_drain_shutdown_negative(box)
        scenario_phase_deadline(box)
        scenario_journal_rotation(box, state, config)
    except KeyboardInterrupt:
        box.reap_wedged()
        shutil.rmtree(root, ignore_errors=True)
        print("\n  interrupted — fixtures removed")
        return 130
    finally:
        box.reap_wedged()
        scenario_blast_radius(before)

    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    failed = [name for name, ok, _ in _RESULTS if not ok]
    print(f"\n§access self-check: {passed}/{total} checks passed")
    if failed:
        # A red run has to stay diagnosable — keep the fixtures, say where.
        print("  FAILED checks: " + "; ".join(failed))
        print(f"(fixtures kept at {root} for inspection; safe to delete)")
        return 1
    # Green: leave nothing behind (.patches/005 — abandoned trees had grown to GBs).
    shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(run())
