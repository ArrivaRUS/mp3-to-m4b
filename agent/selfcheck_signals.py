"""§signals self-check — empirical proof that a KILLED agent leaves no orphan (M3).

Run it standalone:

    python3 -m agent.selfcheck_signals

What is being proven (arch/plan-binrunner-mp3-v2.md §M3, risk M4f · Codex MAJOR-9 ·
addendum §4.4). The live chain is::

    launchd → mp3-to-m4b-agent (helper) → /bin/bash runner.sh → python3 -m agent
            → ffmpeg (a pool of them in the default fast mode)

``launchctl bootout`` — which the installer runs on EVERY update — sends SIGTERM to
that chain. ``runner.sh`` forwards it (M0), but bash does not know ffmpeg's pid
(it is its *grandchild*) and python's default disposition is to die on the spot,
reaping nobody. The failure mode is an ffmpeg that survives the agent and keeps
writing into a temp directory nobody owns any more, plus a half-written ``.m4b``.

No compile check or code reading can prove the fix. Only killing a REAL build can.
So this suite runs real ffmpeg encodes on a throwaway tree and asserts the
OBSERVABLE outcome — by PID, not by log line:

  A. product shape      ``bin/runner.sh`` → ``python3 -m agent`` → ffmpeg (fast mode,
                        several concurrent children). SIGTERM to the BASH pid ⇒ every
                        captured ffmpeg pid is dead, no ffmpeg anywhere still holds
                        our tree, the temps are swept, NO ``.m4b`` was published,
                        the manifest says ``error: interrupted``, the journal has
                        ``build_interrupted`` + ``agent_interrupted``, and the exit
                        code is 143 (= 128+SIGTERM) mirrored up through bash.
  B. python alone       the same, seamless (single-child) build, signalling
                        ``python3 -m agent`` directly — the twin of the negative
                        control below, so the two differ ONLY by the handler.
  C. NEGATIVE CONTROL   the identical build driven WITHOUT the handler (a bare
                        ``dispatcher.drain_commands()`` driver, i.e. exactly what
                        the code did before M3). The parent dies from the signal
                        and ffmpeg SURVIVES it, the temp stays on disk, the manifest
                        stays ``converting``. This is what makes A/B meaningful:
                        it shows the assertions can actually go red. The orphan is
                        killed by this suite afterwards.
  D. progress deadline  a real encode is frozen with SIGSTOP (the lab analogue of an
                        ffmpeg blocked writing into a folder macOS has not granted —
                        alive, but producing nothing). With
                        ``MP3TOM4B_BUILD_STALL_S`` shrunk, the agent must notice the
                        wedge, SIGKILL the frozen child, sweep, mark the book
                        ``interrupted`` and exit NORMALLY (0 — it was not signalled).

Plus two cheap unit-level checks of the primitives (flag semantics; the stall
guard's two liveness signals).

Safety: everything happens under one ``mktemp`` tree with its own
``MP3TOM4B_SUPPORT_DIR`` / ``MP3TOM4B_WATCH_DIR`` / ``MP3TOM4B_LABEL`` and
``MP3TOM4B_NO_LAUNCHCTL=1``. The user's real Application Support, the real watched
folder and launchd are never touched; no installer is invoked; no system dialog can
appear. Requires ffmpeg + ffprobe on PATH (there are no stubs here on purpose —
a stub cannot be orphaned, which is the whole point).

It runs ONLY its own checks (cross-suite regression is orchestrated once by
``agent.selfcheck_all``) and returns 0 ⇔ every check here passed.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
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
    print(line, flush=True)


# --- ffmpeg fixtures ---------------------------------------------------------


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _has_tools() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


# Fixture size. Silence re-encodes ~400× faster than real time on this class of
# machine, so the wall-clock window we get to interrupt is
# (chapter_seconds × chapters / workers) / 400. Six 15-minute chapters give ≈2 s per
# parallel group and ≈12 s for a single-pass encode — 40×…240× the ~50 ms it takes
# us to notice ffmpeg and signal it. Long enough to be un-racy, short enough that the
# whole suite stays under a minute.
CHAPTERS = 6
CHAPTER_SECONDS = 900.0


def _make_silence_mp3(path: Path, *, seconds: float, tags: dict | None = None) -> None:
    """Write a real (silent) mp3 of ``seconds`` virtual length via anullsrc."""
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(seconds), "-c:a", "libmp3lame", "-b:a", "192k",
    ]
    for k, v in (tags or {}).items():
        argv += ["-metadata", f"{k}={v}"]
    argv.append(str(path))
    subprocess.run(argv, check=True, capture_output=True)


BOOK_DIRNAME = "Сигналы - Длинная книга"


def _make_fixture(fixtures: Path) -> Path:
    """Generate the shared source book ONCE (re-linked into every case tree)."""
    book = fixtures / BOOK_DIRNAME
    for i in range(1, CHAPTERS + 1):
        _make_silence_mp3(
            book / f"{i:02d} - Глава {i}.mp3", seconds=CHAPTER_SECONDS,
            tags={"title": f"Глава {i}", "album": "Длинная книга",
                  "album_artist": "Тест Сигналов"},
        )
    return book


def _link_fixture(src_book: Path, watch: Path) -> Path:
    """Hard-link the fixture mp3s into a case's watch dir (instant, no copy).

    Hard links keep size/mtime identical (so ``source_rev`` is computed exactly as
    for a real drop) and cost nothing. The agent only ever READS sources (I1), so
    sharing inodes across cases is safe. Falls back to a copy across filesystems.
    """
    dst = watch / src_book.name
    dst.mkdir(parents=True, exist_ok=True)
    for mp3 in sorted(src_book.glob("*.mp3")):
        target = dst / mp3.name
        try:
            os.link(mp3, target)
        except OSError:
            shutil.copy2(mp3, target)
    return dst


# --- process-tree helpers (the "no orphan" proof) ----------------------------


def _ps_table() -> list[tuple[int, int, str]]:
    """(pid, ppid, command) for every process — the portable macOS live tree.

    ``-ww`` is load-bearing: without it macOS truncates the command column, and the
    ffmpeg argv we match on (the case's temp paths) sits far to the right of it.
    """
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,ppid=,command="],
                             capture_output=True, text=True).stdout
    except OSError:
        return []
    rows: list[tuple[int, int, str]] = []
    for line in out.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
        except ValueError:
            continue
    return rows


def _is_ffmpeg(command: str) -> bool:
    """True iff this ps row's argv[0] is an ffmpeg binary.

    Must look at argv[0] ONLY: the full command line of an encode is stuffed with
    our paths, so matching anywhere would also catch e.g. a shell that merely
    mentions the file — and taking the basename of the whole line lands in the
    LAST path of the argv, not in the executable.
    """
    head = command.split(None, 1)[0] if command.strip() else ""
    return "ffmpeg" in head.rsplit("/", 1)[-1]


def _ffmpeg_descendants(root_pid: int) -> list[int]:
    """ffmpeg pids anywhere BELOW ``root_pid`` (bash → python → ffmpeg is depth 2)."""
    rows = _ps_table()
    kids: dict[int, list[int]] = {}
    cmd: dict[int, str] = {}
    for pid, ppid, command in rows:
        kids.setdefault(ppid, []).append(pid)
        cmd[pid] = command
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        cur = stack.pop()
        for kid in kids.get(cur, []):
            if kid in seen:
                continue
            seen.add(kid)
            stack.append(kid)
    return sorted(p for p in seen if _is_ffmpeg(cmd.get(p, "")))


def _ffmpeg_holding(needle: str) -> list[int]:
    """ffmpeg pids whose ARGV mentions ``needle`` — orphan detection without ppid.

    Once the parent dies an orphan is re-parented to launchd, so a tree walk can no
    longer find it. Matching on the case's temp path finds it regardless of who its
    parent is now, and can never match anything outside this suite's own tree.
    """
    return sorted(pid for pid, _ppid, command in _ps_table()
                  if needle in command and _is_ffmpeg(command))


def _pid_dead(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return True
    return False


def _wait_all_dead(pids: list[int], timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if all(_pid_dead(p) for p in pids):
            return True
        time.sleep(0.02)
    return all(_pid_dead(p) for p in pids)


def _kill_hard(pids: list[int]) -> None:
    """Clean up processes the NEGATIVE control deliberately orphaned."""
    for p in pids:
        try:
            os.kill(p, signal.SIGKILL)
        except OSError:
            pass


# --- command helpers (mirror how the app drops commands) ---------------------


def _drop_command(commands_dir: Path, payload: dict) -> Path:
    commands_dir.mkdir(parents=True, exist_ok=True)
    cmd_id = payload.get("cmd_id") or str(uuid.uuid4())
    payload.setdefault("cmd_id", cmd_id)
    final = commands_dir / f"{cmd_id}.json"
    tmp = commands_dir / f".{cmd_id}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


def _confirm_build_cmd(manifest: dict, *, build_mode: str) -> dict:
    bid = manifest["book_id"]
    rev = manifest["source_rev"]
    params = dict(manifest.get("params", {}))
    params["build_mode"] = build_mode      # "fast" (pool) | "seamless" (one child)
    return {
        "cmd_id": str(uuid.uuid4()),
        "action": "confirm-build",
        "book_id": bid,
        "source_rev": rev,
        "confirm_token": manifest["confirm_token"],
        "idempotency_key": f"{bid}:{rev[:16]}:{uuid.uuid4().hex[:8]}",
        "params": params,
        "ts": time.time(),
    }


# --- one case tree ------------------------------------------------------------


class Case:
    """A fully isolated agent installation: own support tree, watch dir, book."""

    def __init__(self, root: Path, name: str, fixture_book: Path, repo_root: Path):
        self.name = name
        self.root = root / f"case-{name}"
        self.support = self.root / "support"
        self.watch = self.root / "watch"
        self.support.mkdir(parents=True, exist_ok=True)
        self.watch.mkdir(parents=True, exist_ok=True)
        self.repo_root = repo_root
        self.book_dir = _link_fixture(fixture_book, self.watch)
        self.manifest_path: Path | None = None
        self.out_path: Path | None = None
        self.book_id: str | None = None

    # -- environment -----------------------------------------------------
    def activate(self) -> None:
        """Point THIS process's agent modules at this case's tree."""
        os.environ["MP3TOM4B_SUPPORT_DIR"] = str(self.support)
        os.environ["MP3TOM4B_WATCH_DIR"] = str(self.watch)
        os.environ["MP3TOM4B_COVER_WEB"] = "0"
        os.environ["MP3TOM4B_STABILITY_DEBOUNCE_S"] = "0"

    def child_env(self, extra: dict | None = None) -> dict:
        env = dict(os.environ)
        env.update({
            "MP3TOM4B_SUPPORT_DIR": str(self.support),
            "MP3TOM4B_WATCH_DIR": str(self.watch),
            "MP3TOM4B_COVER_WEB": "0",
            "MP3TOM4B_STABILITY_DEBOUNCE_S": "0",
            # Belt & braces: nothing here goes near launchd, and if it ever did it
            # would use this throwaway label, never the product one.
            "MP3TOM4B_NO_LAUNCHCTL": "1",
            "MP3TOM4B_LABEL": f"com.arrivarus.mp3tom4b.selfcheck-signals-{self.name}",
            "PYTHON3": sys.executable,
            "PYTHONPATH": str(self.repo_root),
        })
        env.update(extra or {})
        return env

    # -- arming ----------------------------------------------------------
    def arm(self, *, build_mode: str) -> None:
        """Scan the book in-process and queue a confirm-build for the child."""
        self.activate()
        from agent import build_m4b, config, scan, state
        scan.run_scan()
        manifest = None
        for p in config.books_dir().glob("*.json"):
            m = state.read_json(p)
            if str(m.get("src_dir", "")).endswith(BOOK_DIRNAME):
                manifest = m
                self.manifest_path = p
                break
        assert manifest is not None, f"case {self.name}: scan did not arm the book"
        self.book_id = manifest["book_id"]
        self.out_path = build_m4b.default_output_path(manifest)
        _drop_command(config.commands_dir(), _confirm_build_cmd(
            manifest, build_mode=build_mode))

    # -- observation -----------------------------------------------------
    def manifest(self) -> dict:
        from agent import state
        assert self.manifest_path is not None
        return state.read_json(self.manifest_path, default={}) or {}

    def events(self, kind: str) -> list[dict]:
        """Read this case's journal directly (the parent's env may have moved on)."""
        path = self.support / "state" / "events.jsonl"
        out: list[dict] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("event") == kind:
                    out.append(rec)
        except OSError:
            return []
        return out

    def temps(self) -> list[str]:
        assert self.out_path is not None
        return sorted(p.name for p in self.out_path.parent.glob(f".{self.out_path.name}.*"))

    def wait_for_build(self, proc: subprocess.Popen, timeout_s: float = 180.0
                       ) -> list[int]:
        """Block until the child is REALLY encoding; return the live ENCODER pids.

        Getting this gate wrong is how a signal test fools itself. "Some ffmpeg
        exists" is not enough — the agent spawns two other, near-instant ffmpegs:
        the embedded-cover extraction during the scan, and the one-shot
        ``ffmpeg -encoders`` capability probe just before the encode. Signalling
        during either would kill the run BEFORE any encoder existed and every
        "no orphan" assertion would pass vacuously.

        So the gate is: the manifest is at ``converting`` **and** an ffmpeg whose
        ARGV mentions this case's tree is alive — that argv only belongs to a
        process actually reading our chapters / writing our output.
        """
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return []
            if self.manifest().get("status") == "converting":
                encoders = _ffmpeg_holding(str(self.root))
                if encoders:
                    return encoders
            time.sleep(0.05)
        return []


# ============================ the checks =====================================


def _unit_shutdown_flag() -> None:
    """Flag semantics: first signal wins, repeats are safe, exit code is 128+sig."""
    from agent import shutdown

    shutdown.reset()
    check("flag: clean process reports no shutdown",
          not shutdown.requested() and shutdown.exit_code() == 0,
          f"requested={shutdown.requested()} code={shutdown.exit_code()}")

    installed = shutdown.install()
    again = shutdown.install()
    check("flag: install() covers TERM/INT/HUP and is idempotent",
          {signal.SIGTERM, signal.SIGINT, signal.SIGHUP} <= set(installed)
          and installed == again,
          f"installed={installed} second_call={again}")

    shutdown.request(signal.SIGTERM)
    shutdown.request(signal.SIGINT)   # a repeat must not overwrite or explode
    shutdown.request(signal.SIGTERM)
    check("flag: the FIRST signal stays authoritative across repeats",
          shutdown.signum() == int(signal.SIGTERM) and shutdown.name() == "SIGTERM"
          and shutdown.count() == 3,
          f"signum={shutdown.signum()} name={shutdown.name()} count={shutdown.count()}")
    check("flag: exit code is the shell convention 128+signum (143 for TERM)",
          shutdown.exit_code() == 143, f"exit_code={shutdown.exit_code()}")
    shutdown.reset()

    # The handler must still be OUR handler after all of that (nothing reset it).
    check("flag: the installed handler survives repeated signals",
          signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL
          and signal.getsignal(signal.SIGTERM) is not signal.SIG_IGN,
          f"handler={signal.getsignal(signal.SIGTERM)}")


def _unit_stall_guard(tmp: Path) -> None:
    """The wedge detector: position OR file motion resets it; nothing = stalled."""
    from agent import build_m4b

    g = build_m4b._StallGuard(limit_s=0.4)
    check("stall-guard: a fresh guard is not stalled", not g.stalled(0), "")

    # Position advancing keeps it quiet even past the limit.
    time.sleep(0.5)
    check("stall-guard: an advancing position resets the clock",
          not g.stalled(1000), "pos 0 → 1000 after 0.5s")

    # Nothing moves at all → stalled once the limit elapses.
    time.sleep(0.5)
    check("stall-guard: no motion for the limit ⇒ stalled",
          g.stalled(1000), "same position, no file")

    # The FILE signal alone is enough to prove liveness — this is what keeps a long
    # `+faststart` finalization (silent, but the file keeps moving) from being killed.
    probe = tmp / "stall-probe.bin"
    probe.write_bytes(b"0")
    g2 = build_m4b._StallGuard(limit_s=0.4)
    g2.stalled(500, (probe,))
    time.sleep(0.5)
    probe.write_bytes(b"0" * 4096)          # position frozen, file grew
    check("stall-guard: file growth alone counts as progress (faststart tail)",
          not g2.stalled(500, (probe,)), "frozen position + growing file")
    time.sleep(0.5)
    check("stall-guard: frozen position AND frozen file ⇒ stalled",
          g2.stalled(500, (probe,)), "nothing moved for the limit")

    # The deadline is tunable but never disable-able.
    os.environ["MP3TOM4B_BUILD_STALL_S"] = "0"
    zero = build_m4b._build_stall_s()
    os.environ["MP3TOM4B_BUILD_STALL_S"] = "nonsense"
    junk = build_m4b._build_stall_s()
    os.environ.pop("MP3TOM4B_BUILD_STALL_S", None)
    check("stall-guard: a bogus/zero override falls back to the 300s default",
          zero == build_m4b.BUILD_STALL_S and junk == build_m4b.BUILD_STALL_S,
          f"zero={zero} junk={junk}")


def _case_signal(case: Case, *, via_runner: bool, build_mode: str) -> None:
    """A REAL build, a REAL SIGTERM, asserted by pid (A and B)."""
    label = f"{case.name}"
    case.arm(build_mode=build_mode)

    if via_runner:
        argv = ["/bin/bash", str(case.repo_root / "bin" / "runner.sh")]
    else:
        argv = [sys.executable, "-m", "agent", "--drain"]

    proc = subprocess.Popen(
        argv, cwd=str(case.repo_root), env=case.child_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,   # own session: our kill hits exactly this pid
    )

    ffmpeg_pids = case.wait_for_build(proc)
    check(f"{label}: a real build is running with live ffmpeg encoder(s)",
          bool(ffmpeg_pids), f"ffmpeg_pids={ffmpeg_pids} rc={proc.poll()}")
    if not ffmpeg_pids:
        proc.kill()
        return

    # The topology the whole design rests on: ffmpeg is a DESCENDANT of the process
    # we are about to signal (via bash → python when we came through runner.sh), i.e.
    # exactly the grandchild bash cannot address by pid.
    descendants = _ffmpeg_descendants(proc.pid)
    check(f"{label}: the encoder(s) really run under the signalled process",
          bool(set(ffmpeg_pids) & set(descendants)),
          f"encoders={ffmpeg_pids} descendants_of_{proc.pid}={descendants}")

    # === the interruption: exactly what `launchctl bootout` does =============
    t_kill = time.monotonic()
    os.kill(proc.pid, signal.SIGTERM)

    # 1. THE point of the milestone, measured against the plan's own acceptance
    #    criterion (risk M4f: "bootout посреди encode ⇒ ни одного потомка через 5 с").
    #    Checked BEFORE waiting for the parent, so this timing is the children's, not
    #    the interpreter's shutdown.
    all_dead = _wait_all_dead(ffmpeg_pids, timeout_s=5.0)
    teardown_s = time.monotonic() - t_kill
    check(f"{label}: NO orphaned ffmpeg — every encoder pid is dead within 5s (M4f)",
          all_dead,
          f"{teardown_s:.2f}s captured={ffmpeg_pids} "
          f"alive={[p for p in ffmpeg_pids if not _pid_dead(p)]}")

    try:
        out, err = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        check(f"{label}: the agent exits promptly after SIGTERM", False,
              "still alive after 60s — had to SIGKILL it")
        return
    check(f"{label}: the agent itself exits promptly after SIGTERM", True,
          f"rc={proc.returncode} after {time.monotonic() - t_kill:.2f}s")

    # 2. …and nothing ffmpeg-shaped still holds this case's tree (an orphan is
    #    re-parented to launchd, so this check does not depend on the ppid chain).
    leftover = _ffmpeg_holding(str(case.root))
    check(f"{label}: no ffmpeg process anywhere still holds the case tree",
          leftover == [], f"leftover_pids={leftover}")
    _kill_hard(leftover)

    # 3. exit code: 128+SIGTERM, mirrored up through bash when we went via runner.sh.
    check(f"{label}: exit code is 143 (128+SIGTERM){' through runner.sh' if via_runner else ''}",
          proc.returncode == 143,
          f"rc={proc.returncode} stderr_tail={(err or '').strip().splitlines()[-2:]}")

    # 4. atomicity held: no half-written .m4b was published, no temp survived.
    assert case.out_path is not None
    check(f"{label}: NO partial .m4b published",
          not case.out_path.exists(), f"path={case.out_path}")
    check(f"{label}: every build temp was swept (.<name>.m4b.* gone)",
          case.temps() == [], f"leftover_temps={case.temps()}")

    # 5. the book surfaces honestly (same reason a killed build gets on recovery).
    man = case.manifest()
    err_obj = man.get("error") if isinstance(man.get("error"), dict) else {}
    check(f"{label}: manifest is error/interrupted with the build marker cleared",
          man.get("status") == "error" and err_obj.get("reason") == "interrupted"
          and man.get("build") is None,
          f"status={man.get('status')!r} error={err_obj} build={man.get('build')}")

    # 6. it is diagnosable: both journal records, with the cause and the signal.
    interrupted = case.events("build_interrupted")
    agent_int = case.events("agent_interrupted")
    check(f"{label}: journalled build_interrupted(cause=signal)",
          any(e.get("cause") == "signal" for e in interrupted), f"events={interrupted}")
    # …and it caught LIVE children — proof the teardown ran in the encoder poll loop
    # rather than the (also correct, but vacuous here) pre-spawn entry gate.
    check(f"{label}: the interrupt tore down live encoder children (not a no-op gate)",
          any(int(e.get("children") or 0) >= 1 for e in interrupted
              if e.get("cause") == "signal"),
          f"children_per_event={[e.get('children') for e in interrupted]}")
    check(f"{label}: journalled agent_interrupted(SIGTERM) with exit_code 143",
          any(e.get("signal") == "SIGTERM" and e.get("exit_code") == 143
              for e in agent_int),
          f"events={agent_int}")


def _case_negative_control(case: Case) -> None:
    """The same build WITHOUT the handler — the orphan must appear (test teeth)."""
    case.arm(build_mode="seamless")

    # The pre-M3 behaviour, reproduced exactly: drive the production dispatcher but
    # never install the handlers (that is all `main()` adds). No test seam is added
    # to the product for this — the difference is only in what this driver calls.
    driver = (
        "import sys; sys.path.insert(0, %r); "
        "from agent import dispatcher; dispatcher.drain_commands()"
        % str(case.repo_root)
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", driver], cwd=str(case.repo_root), env=case.child_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )

    ffmpeg_pids = case.wait_for_build(proc)
    check("negative: the unprotected build is running with a live ffmpeg child",
          bool(ffmpeg_pids), f"ffmpeg_pids={ffmpeg_pids} rc={proc.poll()}")
    if not ffmpeg_pids:
        proc.kill()
        return

    os.kill(proc.pid, signal.SIGTERM)
    try:
        proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()

    check("negative: without the handler the agent dies FROM the signal (rc=-15)",
          proc.returncode == -int(signal.SIGTERM), f"rc={proc.returncode}")

    # Give the dead parent a moment; an orphan does not disappear when it is
    # re-parented, so a short settle is enough and cannot mask a real teardown.
    time.sleep(0.5)
    orphans = [p for p in ffmpeg_pids if not _pid_dead(p)]
    holding = _ffmpeg_holding(str(case.root))
    check("negative: ORPHAN DETECTED — ffmpeg outlived the agent (checks have teeth)",
          bool(orphans) or bool(holding),
          f"orphan_pids={orphans} holding_case_tree={holding}")

    assert case.out_path is not None
    left_temps = case.temps()
    man = case.manifest()
    check("negative: the half-written temp is left behind (sweep assertion has teeth)",
          left_temps != [], f"temps={left_temps}")
    check("negative: the manifest is left stuck at converting (recovery's job)",
          man.get("status") == "converting", f"status={man.get('status')!r}")

    # Clean up what the negative control deliberately leaked.
    _kill_hard(sorted(set(orphans) | set(holding)))
    killed_ok = _wait_all_dead(sorted(set(orphans) | set(holding)), timeout_s=10.0)
    check("negative: the deliberately-orphaned ffmpeg was cleaned up by the suite",
          killed_ok, f"still_alive={[p for p in orphans if not _pid_dead(p)]}")


def _case_cancel_coexistence(case: Case) -> None:
    """Cancel and shutdown share one poll loop — prove they do not fight (M3 · D13).

    Both live on the SAME ~3×/s tick inside the encoder, so they can never run
    concurrently: whichever the tick sees first raises, and the single unwind path
    (kill children → sweep temps → propagate) runs once. The only question worth
    settling empirically is PRECEDENCE when both are pending at the same instant,
    and it is settled deterministically here (flag raised in-process before the
    encode starts, cancel command already on disk) rather than by racing a real kill:

      · cancel alone      ⇒ ``BuildCancelled`` (unchanged D13 behaviour — the book
                            goes back to the queue, the dispatcher consumes the cmd);
      · cancel + shutdown ⇒ ``BuildInterrupted`` wins. The process is leaving either
                            way, and "interrupted" is the honest state; the cancel
                            command stays on disk and is resolved as ``cancel_moot``
                            on the next run (the book is no longer ``converting``),
                            so nothing is double-processed and nothing is stuck.

    Both branches must also leave NO ffmpeg behind — that is the shared invariant.
    """
    case.activate()
    from agent import build_m4b, config, shutdown

    scratch = case.root / "coexist"
    scratch.mkdir(parents=True, exist_ok=True)
    book_id = "coexist-book"
    _drop_command(config.commands_dir(), {
        "cmd_id": str(uuid.uuid4()), "action": "cancel", "book_id": book_id,
        "idempotency_key": f"cancel:{book_id}", "ts": time.time(),
    })

    def _long_encode(out: Path) -> list[str]:
        # 10 virtual hours of silence: the encoder is guaranteed to still be running
        # when the first poll tick (≤0.3 s) fires, without generating any fixture.
        return [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-progress", "pipe:1", "-nostats",
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "36000",
                "-c:a", "aac", "-b:a", "192k",
                "-f", "ipod", "-movflags", "+faststart", str(out)]

    # 1) cancel alone — the D13 path must be untouched by M3.
    shutdown.reset()
    out1 = scratch / "cancel-only.m4a"
    raised = None
    try:
        build_m4b._run_ffmpeg(_long_encode(out1), reason_on_fail="x",
                              book_id=book_id, output_path=out1)
    except BaseException as exc:      # noqa: BLE001 - we classify it below
        raised = exc
    check("coexist: a pending cancel still raises BuildCancelled (D13 intact)",
          isinstance(raised, build_m4b.BuildCancelled), f"raised={raised!r}")
    check("coexist: the cancelled encoder left no ffmpeg behind",
          _ffmpeg_holding(str(case.root)) == [],
          f"leftover={_ffmpeg_holding(str(case.root))}")

    # 2) cancel AND shutdown — the signal outranks the cancel.
    shutdown.request(signal.SIGTERM)
    out2 = scratch / "cancel-plus-signal.m4a"
    raised2 = None
    try:
        build_m4b._run_ffmpeg(_long_encode(out2), reason_on_fail="x",
                              book_id=book_id, output_path=out2)
    except BaseException as exc:      # noqa: BLE001
        raised2 = exc
    check("coexist: with BOTH pending, shutdown wins (BuildInterrupted, cause=signal)",
          isinstance(raised2, build_m4b.BuildInterrupted)
          and getattr(raised2, "cause", None) == "signal"
          and getattr(raised2, "reason", None) == "interrupted",
          f"raised={raised2!r} cause={getattr(raised2, 'cause', None)}")
    check("coexist: BuildInterrupted is a BuildError (dispatcher needs no new branch)",
          isinstance(raised2, build_m4b.BuildError)
          and not isinstance(raised2, build_m4b.BuildCancelled),
          f"mro={type(raised2).__mro__ if raised2 else None}")
    check("coexist: the interrupted encoder left no ffmpeg behind",
          _ffmpeg_holding(str(case.root)) == [],
          f"leftover={_ffmpeg_holding(str(case.root))}")

    # 3) the cancel command is still on disk — the interrupt did not consume it, so
    #    the next run resolves it as ``cancel_moot`` (single owner, no double work).
    leftover_cmds = [p.name for p in config.commands_dir().glob("*.json")]
    check("coexist: the un-acted cancel command survives for the next run to moot",
          len(leftover_cmds) == 1, f"commands={leftover_cmds}")

    shutdown.reset()


def _case_stall(case: Case) -> None:
    """A frozen (SIGSTOP'd) ffmpeg must trip the progress deadline (addendum §4.4)."""
    case.arm(build_mode="seamless")

    stall_s = 4.0
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent", "--drain"],
        cwd=str(case.repo_root),
        env=case.child_env({"MP3TOM4B_BUILD_STALL_S": str(stall_s)}),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True,
    )

    ffmpeg_pids = case.wait_for_build(proc)
    check("stall: a real build is running with a live ffmpeg child",
          bool(ffmpeg_pids), f"ffmpeg_pids={ffmpeg_pids} rc={proc.poll()}")
    if not ffmpeg_pids:
        proc.kill()
        return

    # Freeze it: alive, scheduled nowhere, writing nothing — the lab analogue of an
    # ffmpeg blocked on a TCC-protected write. Both liveness signals go flat.
    for p in ffmpeg_pids:
        os.kill(p, signal.SIGSTOP)
    frozen_at = time.monotonic()

    try:
        proc.communicate(timeout=stall_s + 60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        for p in ffmpeg_pids:
            try:
                os.kill(p, signal.SIGKILL)
            except OSError:
                pass
        check("stall: the agent notices the wedge and exits", False,
              f"still running {stall_s + 60}s after the freeze")
        return

    elapsed = time.monotonic() - frozen_at
    check("stall: the agent unwedged itself within the deadline (+grace)",
          elapsed < stall_s + 30, f"{elapsed:.1f}s after freeze (limit {stall_s}s)")

    # A SIGSTOP'd process ignores SIGTERM until resumed — the escalation to SIGKILL
    # is what actually kills it, so this also proves the escalation really happens.
    check("stall: the FROZEN ffmpeg was killed (SIGTERM→SIGKILL escalation works)",
          _wait_all_dead(ffmpeg_pids, timeout_s=10.0),
          f"pids={ffmpeg_pids} alive={[p for p in ffmpeg_pids if not _pid_dead(p)]}")

    leftover = _ffmpeg_holding(str(case.root))
    _kill_hard(leftover)
    check("stall: no ffmpeg anywhere still holds the case tree", leftover == [],
          f"leftover={leftover}")

    # A stall is not a signal: the agent finished its work normally and exits 0.
    check("stall: the agent exits 0 (a wedge is a build error, not a termination)",
          proc.returncode == 0, f"rc={proc.returncode}")

    assert case.out_path is not None
    check("stall: NO partial .m4b published", not case.out_path.exists(),
          f"path={case.out_path}")
    check("stall: every build temp was swept", case.temps() == [],
          f"leftover_temps={case.temps()}")

    man = case.manifest()
    err_obj = man.get("error") if isinstance(man.get("error"), dict) else {}
    check("stall: manifest is error/interrupted", man.get("status") == "error"
          and err_obj.get("reason") == "interrupted",
          f"status={man.get('status')!r} error={err_obj}")
    stalled = [e for e in case.events("build_interrupted") if e.get("cause") == "stall"]
    check("stall: journalled build_interrupted(cause=stall) naming the deadline",
          bool(stalled) and "no ffmpeg progress" in str(stalled[0].get("detail", "")),
          f"events={stalled}")


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not _has_tools():
        print("§signals self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-signals-"))
    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    print(f"self-check tree: {root}")
    print(f"  repo: {repo_root}\n")

    t0 = time.monotonic()
    fixture = _make_fixture(root / "fixtures")
    print(f"  fixture: {CHAPTERS}×{int(CHAPTER_SECONDS)}s silent chapters "
          f"({time.monotonic() - t0:.1f}s)\n")

    print("— primitives —")
    _unit_shutdown_flag()
    _unit_stall_guard(root)

    print("\n— A: product shape (runner.sh → python → ffmpeg pool, fast mode) —")
    _case_signal(Case(root, "A", fixture, repo_root), via_runner=True,
                 build_mode="fast")

    print("\n— B: python3 -m agent directly (seamless, single child) —")
    _case_signal(Case(root, "B", fixture, repo_root), via_runner=False,
                 build_mode="seamless")

    print("\n— E: cancel × shutdown coexistence (deterministic, in-process) —")
    _case_cancel_coexistence(Case(root, "E", fixture, repo_root))

    print("\n— C: NEGATIVE CONTROL — same build, handler removed —")
    _case_negative_control(Case(root, "C", fixture, repo_root))

    print("\n— D: progress deadline on a frozen ffmpeg —")
    _case_stall(Case(root, "D", fixture, repo_root))

    return _finish(root)


def _finish(root: Path) -> int:
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    failed = [name for name, ok, _ in _RESULTS if not ok]
    print(f"\n§signals self-check: {passed}/{total} checks passed")
    if failed:
        print("  FAILED checks: " + "; ".join(failed))
        # Keep the evidence when something went red — the trees hold the manifests,
        # journals and any temp the run left behind.
        print(f"(temp tree KEPT at {root} for inspection)")
        return 1
    # Green: drop the tree. Unlike the sibling suites this one stages ~130 MB of
    # fixture mp3s (long chapters are what buy the interruption window), so leaving
    # it behind on every run would quietly pile up hundreds of MB in /var/folders.
    shutil.rmtree(root, ignore_errors=True)
    print(f"(temp tree removed: {root})")
    return 0


if __name__ == "__main__":
    sys.exit(run())
