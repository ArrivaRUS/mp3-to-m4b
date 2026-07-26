"""Agent entry point — ``python3 -m agent`` (spawned by ``bin/runner.sh``).

On launch the agent (M0.6 scope, plans.md):
  0. installs the cooperative shutdown handlers (M3 — see below) and arms the
     phase-deadline watchdog (M4 — see further below);
  1. ensures the data-directory tree;
  2. **recovers** any manifest left mid-build (``converting`` with no live pid)
     → ``error: interrupted`` + temp sweep (must run before anything reads state);
  3. **scans** the watched folder → a ``pending-confirm`` manifest per book +
     the ``state.json`` showcase (behind the M4 access gate — the scan refuses to
     touch the folder until the probe says it may);
  4. **drains** queued commands in ``queue/commands/`` → validate + real engine
     (``confirm-build`` builds the book), then refreshes the showcase.

Recover-before-scan keeps an orphaned ``converting`` from being read as in-flight;
scan-before-drain is deliberate: a freshly seen book must have its manifest (and
``confirm_token``/``source_rev``) on disk before a command that references it can
validate. Then we exit — launchd re-launches on the next ``WatchPaths`` event
(the watched folder OR ``queue/commands/``), so a run-once-and-exit shape fits.

Signals (M3, arch/plan-binrunner-mp3-v2.md §M3 · risk M4f · Codex MAJOR-9).
``launchctl bootout`` — which the installer runs on EVERY update — sends SIGTERM
down ``helper → bash runner.sh → python → ffmpeg``. Python's default disposition
is to die on the spot, which would leave ffmpeg an orphan still writing into a
temp dir we already deleted (and a manifest stuck at ``converting``). So we install
:mod:`agent.shutdown` FIRST — before a single directory is touched — and then honour
its flag at three levels:

  · inside the encoder (``build_m4b``): every poll tick tears the ffmpeg children
    down (SIGTERM→SIGKILL, reaped), sweeps the temps and raises ``BuildInterrupted``
    → the manifest lands at ``error: interrupted``, no half-written ``.m4b``;
  · inside the drain (``dispatcher.drain_commands``): the loop stops at the next
    command boundary, so books queued BEHIND the interrupted one keep their honest
    ``pending-confirm`` and their commands stay on disk for the next tick;
  · here, between phases: we stop starting NEW work and exit ``128+signum``
    (143 for SIGTERM), which ``bin/runner.sh`` mirrors upward so launchd sees the
    truth instead of a fake success.

PHASE DEADLINE (M4, addendum §4.4 · finding 1). launchd will not start a second
instance of the same label, so a single wedged run is the whole product dead:
``folder_access`` never gets published, the access card never appears, «Проверить
снова» does nothing, and only a reboot changes that. The access probe has its own
watchdog, but it is not the only thing here that reaches into a protected folder —
so the phase «probe + scan + publish» gets a hard deadline of its own. If it has
not closed in :data:`PHASE_DEADLINE_S`, we journal the diagnosis, publish
``blocked`` so the UI can explain itself, and leave with ``os._exit(75)``
(``EX_TEMPFAIL``); launchd's ``StartInterval`` brings us back.

The deadline covers the phase, NOT the build. A book can legitimately take hours,
and killing one on a clock would be a far worse bug than the one we are fixing —
the drain is instead guarded by the encoder's own PROGRESS deadline
(``build_m4b.BUILD_STALL_S``: alive-but-frozen ffmpeg, e.g. blocked writing the
``.m4b`` into the watched folder). Hence: watchdog armed for phase 1, disarmed
before the drain, two guards that never overlap.

Every launch journals an ``agent_started`` event first (durable, fsync'd), so a
run is always observable in ``events.jsonl`` even if it has nothing else to do —
this is what makes the "events.jsonl looked empty" class of confusion debuggable.
A shutdown adds a matching ``agent_interrupted`` record with the signal name and
the phase it landed in, so an interrupted run is just as observable.

Flags (combine freely; no flag = scan + drain, recovery always runs):
  ``--scan``   run only the scan pass (manifests + showcase).
  ``--drain``  run only the command-drain pass.
"""

from __future__ import annotations

import os
import sys
import threading
import time

from . import __version__, config, dispatcher, scan, shutdown, state

# ── Phase deadline (addendum §4.4) ───────────────────────────────────────────
#: How long «probe + scan + publish» may take before we call it wedged. Well past
#: the consent window (:data:`agent.scan.CONSENT_WINDOW_S` = 90 s) so a human who
#: is answering the system dialog is never mistaken for a hang, and well past any
#: honest scan (thousands of files are seconds, not minutes).
PHASE_DEADLINE_S = 150.0
_PHASE_DEADLINE_ENV = "MP3TOM4B_PHASE_DEADLINE_S"

#: Exit code on a blown phase deadline: ``EX_TEMPFAIL``. Not 0 (that would tell
#: launchd and the installer a wedged run succeeded) and not ``128+sig`` (nothing
#: signalled us). ``bin/runner.sh`` mirrors the child's code upward unchanged.
EXIT_PHASE_DEADLINE = 75

#: How long the watchdog waits for its own best-effort publish before leaving
#: anyway. The publish only touches Application Support, but a watchdog that can
#: itself be blocked is not a watchdog.
_WATCHDOG_PUBLISH_GRACE_S = 5.0


def _phase_deadline_s() -> float:
    """Deadline in seconds; ``MP3TOM4B_PHASE_DEADLINE_S`` overrides (self-checks)."""
    raw = os.environ.get(_PHASE_DEADLINE_ENV)
    if raw is None:
        return PHASE_DEADLINE_S
    try:
        val = float(raw)
    except ValueError:
        return PHASE_DEADLINE_S
    return val if val > 0 else PHASE_DEADLINE_S


class _PhaseWatchdog:
    """Kills the process if the armed phase does not close in time.

    A daemon thread that sleeps on an :class:`threading.Event`: :meth:`disarm` sets
    it and the thread simply ends, so the normal path costs one thread and one
    context switch. Only when the wait TIMES OUT does it act — and then it acts
    unilaterally, because by definition the main thread is parked somewhere in the
    kernel and cannot be asked to co-operate.

    The teardown order matters: journal FIRST (``events.jsonl`` is append+fsync, and
    it is what makes a wedged run diagnosable at all), publish SECOND (best effort,
    on yet another thread with a grace period — the UI can only learn about this
    from ``state.json``), leave THIRD via ``os._exit``. Never ``sys.exit``: that
    unwinds the *watchdog* thread and leaves the wedged process exactly as it was.
    """

    def __init__(self, deadline_s: float) -> None:
        self._deadline = deadline_s
        self._done = threading.Event()
        self._phase = ""
        self._thread: threading.Thread | None = None

    def arm(self, phase: str) -> _PhaseWatchdog:
        self._phase = phase
        self._thread = threading.Thread(
            target=self._run, name="mp3tom4b-phase-watchdog", daemon=True
        )
        self._thread.start()
        return self

    def disarm(self) -> None:
        self._done.set()

    def _run(self) -> None:
        t0 = time.monotonic()
        if self._done.wait(self._deadline):
            return  # phase closed in time — nothing to do
        elapsed = round(time.monotonic() - t0, 3)
        try:
            state.append_event(
                "phase_deadline_exceeded",
                phase=self._phase,
                deadline_s=self._deadline,
                elapsed_s=elapsed,
                probe_wedged=scan.probe_thread_wedged(),
                exit_code=EXIT_PHASE_DEADLINE,
            )
        except Exception:  # noqa: BLE001 - never let diagnostics stop the exit
            pass

        publisher = threading.Thread(
            target=self._publish_blocked, name="mp3tom4b-watchdog-publish", daemon=True
        )
        publisher.start()
        publisher.join(_WATCHDOG_PUBLISH_GRACE_S)

        print(f"  phase '{self._phase}' exceeded {self._deadline}s "
              f"→ published blocked, exit {EXIT_PHASE_DEADLINE}", flush=True)
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        os._exit(EXIT_PHASE_DEADLINE)

    @staticmethod
    def _publish_blocked() -> None:
        """Tell the UI what happened: `blocked`, the state that means "ask the human".

        A blown phase deadline is exactly the `blocked` shape from the user's side —
        the agent could not get an answer out of the folder — and `blocked` is the
        one value whose card says «нажмите «Разрешить»» instead of sending them to
        System Settings. Best-effort by construction: if even this cannot be written
        the process still leaves, and the next tick tries again.
        """
        try:
            scan.publish_folder_access(scan.ACCESS_BLOCKED)
        except Exception:  # noqa: BLE001
            pass


def _stop_now(phase: str) -> int:
    """Journal the interrupt and return the process exit code (M3).

    Called at a phase boundary once :mod:`agent.shutdown` says a TERM/INT/HUP
    arrived. By this point the encoder (if any) has already killed and reaped its
    ffmpeg children and swept its temps — that unwind happens inside the build's
    own poll loop, not here; this function only records what happened and hands
    launchd an honest ``128+signum``.
    """
    state.append_event(
        "agent_interrupted",
        signal=shutdown.name(),
        signals_received=shutdown.count(),
        phase=phase,
        exit_code=shutdown.exit_code(),
    )
    print(f"  interrupted by {shutdown.name()} during {phase} "
          f"→ exit {shutdown.exit_code()}")
    return shutdown.exit_code()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 0. Cooperative shutdown handlers FIRST (M3). Cheap, allocation-free, and it
    #    must precede anything that can spawn a child or hold a temp file.
    shutdown.install()

    # 0b. Journal rotation, BEFORE the first append of this run (Р6): the single
    #     writer + launchd's one-instance-per-label rule mean no other process can
    #     be appending right now, which is the only moment rotation is safe.
    state.rotate_events_if_needed()

    # 1. Ensure the data-directory layout exists (idempotent).
    dirs = config.ensure_data_dirs()

    # 2. Liveness banner + journal (the journal entry is durable: it proves the
    #    agent actually ran, independent of whether stdout was captured).
    print(f"mp3-to-m4b agent alive (v{__version__})")
    print(f"  support root: {config.support_root()}")
    print(f"  data dirs ready: {len(dirs)}")
    state.append_event("agent_started", version=__version__, argv=argv)

    # 3. Recover interrupted builds BEFORE reading/refreshing state (M0.6).
    recovered = dispatcher.recover_interrupted()
    if recovered:
        print(f"  interrupted builds recovered: {recovered}")
    if shutdown.requested():
        return _stop_now("recover")

    # Choose passes. With no flag, do BOTH (scan, then drain).
    only_scan = "--scan" in argv
    only_drain = "--drain" in argv
    do_scan = only_scan or not (only_scan or only_drain)
    do_drain = only_drain or not (only_scan or only_drain)

    # 4. Scan the watched folder → manifests + state showcase (M0.2), under the
    #    phase deadline: everything from here to the publish reaches into a folder
    #    macOS may decide to hold hostage (addendum §4.4).
    if do_scan:
        watchdog = _PhaseWatchdog(_phase_deadline_s()).arm("probe+scan+publish")
        try:
            target = scan.watch_dir()
            showcase = scan.run_scan(target)
        finally:
            # Disarmed before the drain on purpose: a build may legitimately run for
            # hours and is guarded by its own PROGRESS deadline instead.
            watchdog.disarm()
        access = (showcase.get("agent") or {}).get("folder_access")
        print(f"  watch dir: {target}")
        print(f"  folder access: {access}")
        print(f"  books found: {len(showcase.get('books', []))}")
        if shutdown.requested():
            return _stop_now("scan")

    # 5. Drain queued commands → validate + real engine (M0.5). Refreshes state.
    if do_drain:
        handled = dispatcher.drain_commands()
        print(f"  commands handled: {handled}")
        if shutdown.requested():
            # A build that was in flight has already unwound itself (its ffmpeg is
            # dead, its temps are swept, its manifest says ``interrupted``); any
            # book still queued behind it was NOT started — the drain stops at the
            # command boundary, so those books keep pending-confirm and their
            # commands wait on disk for the next tick.
            return _stop_now("drain")

    return 0


if __name__ == "__main__":
    code = main(sys.argv[1:])
    if scan.probe_thread_wedged():
        # A probe thread is parked in the kernel and will never return. It is a
        # daemon thread, so CPython does not join it — but it also cannot run a
        # finalizer, cannot be signalled and cannot be reasoned about, so the only
        # honest way out is the one that gives it no vote at all. Flush first:
        # os._exit skips every buffer.
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        os._exit(code)
    sys.exit(code)
