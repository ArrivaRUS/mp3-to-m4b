"""Command dispatch: read app-owned commands and run the right handler.

M0.5 (arch/synthesis.md §B): the app drops ``queue/commands/<cmd_id>.json``
atomically; launchd wakes the agent because ``queue/commands/`` is a
``WatchPaths`` entry. Each command carries ``action``
(``confirm-build`` | ``grouping-choice`` | ``cover-choice`` | ``cancel`` |
``skip`` | ``apply-to-all``), ``book_id``, ``source_rev``, ``confirm_token`` and
``idempotency_key``.

This module wires the happy path plus the M0.6 protocol protections:
  - malformed / unreadable command JSON → quarantined in ``queue/commands/bad/``
    (the agent never crashes on a bad file);
  - ``validate_command`` returns a *verdict* (accept / reject / reject-stale):
      * bad/missing ``confirm_token``, missing manifest, wrong status → REJECT
        (``command_rejected``, command dropped, no build);
      * valid token but the inputs changed after recognition (``source_rev``
        mismatch) → REJECT_STALE (``confirm_rejected_stale`` event, book STAYS
        ``pending-confirm`` — the scan re-armed it with a fresh token — only the
        stale command is dropped);
      * a known ``idempotency_key`` (double click / retry) → ACCEPT-as-skip
        (``build_skipped_idempotent``, no second build);
  - ONLY ``confirm-build`` may invoke the build (structural guarantee I2);
  - ``fake-engine``: flip the manifest ``pending-confirm`` → ``converting`` →
    ``done``, stamp a ``build`` marker (pid) on the way in, record the
    ``idempotency_key`` and a fake ``result`` on the way out (no ffmpeg yet — the
    real engine is M1). The state showcase is refreshed from the manifests after.
  - :func:`recover_interrupted` (run at startup): a manifest stuck at
    ``converting`` with no live build pid → ``error`` (``reason=interrupted``) +
    temp sweep, so a crash/kill mid-build surfaces instead of dangling.

A processed command file is removed only AFTER its handler completes, never
before — so a crash mid-handle leaves the command to be retried, not lost.
The ``events.jsonl`` journal records ``confirm_accepted`` → ``build_started`` →
``build_done``; the §M0 gate-test asserts no ``build_started`` without a
preceding ``confirm_accepted``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import config, scan, state

# Actions that may trigger a build. Per I2 this is the ONLY gate to the engine.
BUILD_ACTION = "confirm-build"

# Manifest status transitions driven by the fake-engine.
STATUS_PENDING = "pending-confirm"
STATUS_CONVERTING = "converting"
STATUS_DONE = "done"
STATUS_ERROR = "error"

# validate_command verdicts → how handle_command reacts (M0.6).
#   ACCEPT          build (or, for an already-processed key, idempotent-skip)
#   REJECT_STALE    inputs changed after recognition → confirm_rejected_stale,
#                   book STAYS pending-confirm (it was re-armed by the scan with a
#                   fresh token); only the stale command is dropped.
#   REJECT          any other invalid command (bad/missing token, no manifest,
#                   wrong status, malformed) → command_rejected, command dropped.
VERDICT_ACCEPT = "accept"
VERDICT_REJECT_STALE = "reject_stale"
VERDICT_REJECT = "reject"


def _move_to_bad(command_path: Path, reason: str) -> None:
    """Quarantine an unusable command file into ``queue/commands/bad/``.

    Best-effort: the whole point is to keep the drain loop alive, so any failure
    to move (already gone, permissions) is swallowed after journaling. A name
    clash in ``bad/`` is avoided by suffixing the nanosecond clock.
    """
    bad_dir = config.commands_bad_dir()
    try:
        bad_dir.mkdir(parents=True, exist_ok=True)
        dest = bad_dir / command_path.name
        if dest.exists():
            dest = bad_dir / f"{command_path.stem}.{time.time_ns()}{command_path.suffix}"
        command_path.replace(dest)
    except OSError:
        # Could not move it; try to remove so it is not retried forever.
        try:
            command_path.unlink()
        except OSError:
            pass
    state.append_event("command_bad", file=command_path.name, reason=reason)


def _already_processed(command: dict, manifest: dict) -> bool:
    """True if this command's ``idempotency_key`` was already built for this book.

    The ledger (``manifest['processed_keys']``) is revision-scoped because the app
    derives the key from ``book_id`` + ``source_rev`` — so a repeat key means "the
    SAME build was already done", which is exactly the double-click case.
    """
    key = command.get("idempotency_key")
    if not key:
        return False
    keys = manifest.get("processed_keys")
    return isinstance(keys, list) and key in keys


def validate_command(command: dict, manifest: dict | None) -> tuple[str, str]:
    """Return ``(verdict, reason)`` for a parsed command against its manifest.

    Verdict is one of ``VERDICT_ACCEPT`` / ``VERDICT_REJECT_STALE`` /
    ``VERDICT_REJECT``; ``reason`` is a short machine-ish tag for the journal.

    Ordering matters (M0.6):
      1. structural sanity (object, ``book_id``, manifest exists);
      2. **idempotency** — a known ``idempotency_key`` is an ACCEPT (the caller
         turns it into an idempotent *skip*, not a rebuild) regardless of the now
         ``done`` status, so a double-click collapses to one build;
      3. **stale source_rev** — the inputs changed after recognition →
         ``REJECT_STALE`` (book stays pending, re-armed by scan). This is checked
         BEFORE the token because a re-arming scan rotates BOTH ``source_rev`` and
         ``confirm_token``: a genuinely stale command therefore carries the *old*
         token too, but a ``source_rev`` mismatch already proves "inputs moved",
         which is the precise diagnosis the user is owed (``confirm_rejected_stale``)
         rather than a generic token reject;
      4. **confirm_token** — for a command that DOES match the current rev, the
         token must match (anti-forgery / anti-replay on the live revision);
      5. status must be ``pending-confirm`` to build.
    """
    if not isinstance(command, dict):
        return VERDICT_REJECT, "command_not_object"
    if not command.get("book_id"):
        return VERDICT_REJECT, "missing_book_id"
    if manifest is None:
        return VERDICT_REJECT, "manifest_missing"

    # An already-processed key short-circuits to an idempotent skip even if the
    # book is now ``done`` and (harmlessly) the token still matches.
    if _already_processed(command, manifest):
        return VERDICT_ACCEPT, "idempotent_skip"

    if command.get("source_rev") != manifest.get("source_rev"):
        # Inputs changed after the app captured the manifest → explicit stale.
        # (A re-arm also rotated the token, so we deliberately do NOT fall through
        # to a token reject here — staleness is the more informative cause.)
        return VERDICT_REJECT_STALE, "source_rev_mismatch"

    # Same revision → the command must prove it holds the live token.
    if command.get("confirm_token") != manifest.get("confirm_token"):
        return VERDICT_REJECT, "confirm_token_mismatch"

    if manifest.get("status") != STATUS_PENDING:
        return VERDICT_REJECT, f"status_not_pending:{manifest.get('status')!r}"
    return VERDICT_ACCEPT, "ok"


def _record_processed_key(manifest: dict, command: dict) -> None:
    """Append this command's ``idempotency_key`` to the manifest ledger (no I/O).

    Idempotent within the dict: a key is never duplicated. The caller persists the
    manifest atomically right after.
    """
    key = command.get("idempotency_key")
    if not key:
        return
    keys = manifest.get("processed_keys")
    if not isinstance(keys, list):
        keys = []
    if key not in keys:
        keys.append(key)
    manifest["processed_keys"] = keys


def _fake_build(manifest: dict, manifest_path: Path, command: dict) -> dict:
    """Fake-engine: move the manifest through ``converting`` → ``done``.

    No ffmpeg yet (the real pipeline is M1). We still walk the real status
    transitions and write each one atomically so a reader observes a coherent
    sequence, and we stamp a clearly-fake ``result`` so nobody mistakes the
    output for a real ``.m4b``. Returns the final manifest dict.

    M0.6 additions:
      - On entering ``converting`` we stamp a ``build`` marker (pid + start time)
        into the manifest. The real engine has no separate pid for the fake build,
        so the marker is what makes an *interrupted* ``converting`` detectable on
        the next launch: a manifest left at ``converting`` is, by definition, a
        build that never reached ``done`` (see :func:`recover_interrupted`).
      - On reaching ``done`` we record the command's ``idempotency_key`` in the
        ledger and clear the marker, so a second identical command is an
        idempotent skip rather than a second build.
    """
    book_id = manifest.get("book_id")
    title = scan.title_for_manifest(manifest)

    # pending-confirm → converting (a real engine would stream progress here).
    manifest["status"] = STATUS_CONVERTING
    manifest["progress"] = 0.0
    manifest["build"] = {"pid": os.getpid(), "started_at": time.time()}
    state.write_json_atomic(manifest_path, manifest)
    state.append_event("build_started", book_id=book_id)

    # converting → done. Fake output marker — replaced by build_m4b.build at M1.
    manifest["status"] = STATUS_DONE
    manifest["progress"] = 1.0
    manifest["result"] = {
        "output": f"{title}.m4b (fake)",
        "fake": True,
        "built_at": time.time(),
    }
    _record_processed_key(manifest, command)
    manifest.pop("build", None)  # build finished → no live marker
    state.write_json_atomic(manifest_path, manifest)
    state.append_event("build_done", book_id=book_id, output=manifest["result"]["output"])
    return manifest


def handle_command(command_path: Path) -> bool:
    """Parse, validate and dispatch a single command file.

    Returns ``True`` if a build ran, ``False`` otherwise. The command file is
    always removed after handling (success, validation-fail, or non-build
    action); malformed files are routed to ``bad/`` instead by the caller's
    parse step. A build runs ONLY for ``action == confirm-build`` that passes
    validation (structural I2).
    """
    command = state.read_json(command_path, default=None)
    if command is None or not isinstance(command, dict):
        # Unreadable / not an object → quarantine, do not delete from queue.
        _move_to_bad(command_path, "malformed_json")
        return False

    book_id = command.get("book_id")
    action = command.get("action")

    manifest_path = config.books_dir() / f"{book_id}.json" if book_id else None
    manifest = state.read_json(manifest_path, default=None) if manifest_path else None

    # A non-build action is dispatched without ever touching build validation
    # (grouping/cover/cancel/… land in M1). It still requires a real manifest so a
    # garbage file does not masquerade as a no-op.
    if action != BUILD_ACTION:
        state.append_event(
            "command_noop", file=command_path.name, book_id=book_id, action=action
        )
        _delete_command(command_path)
        return False

    verdict, reason = validate_command(command, manifest)

    if verdict == VERDICT_REJECT_STALE:
        # Inputs changed after recognition. Do NOT build and do NOT silently drop:
        # emit the explicit status event. The book stays pending-confirm — the
        # scan re-armed it with a fresh source_rev/confirm_token, so the app can
        # confirm again against the current inputs. Only this stale command dies.
        state.append_event(
            "confirm_rejected_stale",
            file=command_path.name,
            book_id=book_id,
            command_rev=command.get("source_rev"),
            manifest_rev=(manifest or {}).get("source_rev"),
        )
        _delete_command(command_path)
        return False

    if verdict == VERDICT_REJECT:
        # Any other invalid command (bad/missing token, no manifest, wrong status):
        # no build, reason journaled, command dropped.
        state.append_event(
            "command_rejected", file=command_path.name, book_id=book_id, reason=reason
        )
        _delete_command(command_path)
        return False

    # VERDICT_ACCEPT. Two sub-cases:
    if reason == "idempotent_skip":
        # A second command with an already-processed idempotency_key (double click
        # / retry). The build already happened → skip it, but still consume the
        # duplicate command. No confirm_accepted/build_* events: the gate stays
        # "exactly one build per key".
        state.append_event(
            "build_skipped_idempotent",
            file=command_path.name,
            book_id=book_id,
            idempotency_key=command.get("idempotency_key"),
        )
        _delete_command(command_path)
        return False

    # confirm-build, validated, first time for this key → the ONLY path to the
    # engine (I2). confirm_accepted is emitted BEFORE build_started so the journal
    # gate (no build_started without a preceding confirm_accepted) holds.
    state.append_event("confirm_accepted", book_id=book_id, file=command_path.name)
    _fake_build(manifest, manifest_path, command)  # type: ignore[arg-type]

    # Remove the command only AFTER the build completed.
    _delete_command(command_path)
    return True


def _delete_command(command_path: Path) -> None:
    """Remove a fully-handled command file (best-effort; idempotent)."""
    try:
        command_path.unlink()
    except OSError:
        pass


def _pending_command_files() -> list[Path]:
    """List queued command files (``*.json``), oldest-``ts``-first.

    Ordering is by the command's own ``ts`` field when readable (the app stamps
    it), falling back to file mtime — so commands are processed roughly in the
    order the user issued them. The ``bad/`` subdir is skipped (it is not a
    command source). Unreadable files still sort (last) and get quarantined when
    handled.
    """
    cmd_dir = config.commands_dir()
    if not cmd_dir.is_dir():
        return []
    files = [p for p in cmd_dir.glob("*.json") if p.is_file()]

    def sort_key(p: Path) -> tuple[float, str]:
        data = state.read_json(p, default=None)
        if isinstance(data, dict) and isinstance(data.get("ts"), (int, float)):
            return (float(data["ts"]), p.name)
        try:
            return (p.stat().st_mtime, p.name)
        except OSError:
            return (float("inf"), p.name)

    return sorted(files, key=sort_key)


def drain_commands() -> int:
    """Process every currently-queued command once; return the count handled.

    "Handled" = the command file was consumed (built, rejected, no-op'd, or
    quarantined). One bad file never stops the drain — each is handled in
    isolation. After draining, the state showcase is refreshed so the app sees
    the new ``done`` statuses.
    """
    files = _pending_command_files()
    handled = 0
    for command_path in files:
        try:
            handle_command(command_path)
        except Exception as exc:  # defensive: never let one command kill the loop
            state.append_event(
                "command_error", file=command_path.name, error=repr(exc)
            )
            _move_to_bad(command_path, f"handler_exception:{type(exc).__name__}")
        handled += 1

    if handled:
        # Reflect the new manifest statuses in the showcase the app reads.
        scan.run_scan()
    return handled


def _pid_alive(pid: object) -> bool:
    """Best-effort liveness check for a recorded build pid.

    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` if the pid is gone and
    ``PermissionError`` if it exists but is owned by another user (still alive).
    A missing / non-int pid counts as not-alive. The fake engine never leaves a
    *separate* live process, so in practice any manifest found at ``converting``
    on startup is orphaned; the pid check keeps the logic honest for the real
    engine in M1.
    """
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _cleanup_build_temps(manifest: dict) -> list[str]:
    """Remove any half-written output temp files for an interrupted build.

    The real engine (M1) writes the ``.m4b`` to a hidden temp in the output dir
    and atomically renames on success, so an interrupt can leave a ``.<name>.*.tmp``
    behind. The fake engine writes none, so this is normally a no-op — but wiring
    the sweep now means the recovery path is already correct when the real engine
    lands. Returns the list of removed paths (for the journal).
    """
    removed: list[str] = []
    out = manifest.get("result", {})
    out_path = out.get("output_path") if isinstance(out, dict) else None
    candidates: list[Path] = []
    if isinstance(out_path, str) and out_path:
        p = Path(out_path)
        candidates.append(p)
        # tmp siblings: .<name>.*.tmp in the same dir (matches state.write_json_atomic style)
        try:
            candidates.extend(p.parent.glob(f".{p.name}.*"))
        except OSError:
            pass
    for c in candidates:
        try:
            if c.exists():
                c.unlink()
                removed.append(str(c))
        except OSError:
            pass
    return removed


def recover_interrupted() -> int:
    """Reconcile manifests left mid-build after a crash/kill (run at startup).

    A manifest at ``status == converting`` whose recorded build pid is not alive
    is an *orphan*: the process that owned it died before reaching ``done``. We
    flip it to ``error`` with ``reason="interrupted"``, sweep any output temp
    files, clear the live ``build`` marker, and journal an ``interrupted`` event.
    The book is NOT silently re-armed to pending here — surfacing the failure is
    the point; the user re-triggers (or a later edit re-arms it via the scan).

    Returns the number of manifests recovered. Safe to call on every launch
    (idempotent: a manifest already at ``error`` is skipped).
    """
    books_dir = config.books_dir()
    if not books_dir.is_dir():
        return 0
    recovered = 0
    for manifest_path in sorted(books_dir.glob("*.json")):
        manifest = state.read_json(manifest_path, default=None)
        if not isinstance(manifest, dict):
            continue
        if manifest.get("status") != STATUS_CONVERTING:
            continue
        build = manifest.get("build")
        pid = build.get("pid") if isinstance(build, dict) else None
        if _pid_alive(pid):
            # A live build owns this manifest (real engine, M1) → leave it alone.
            continue

        book_id = manifest.get("book_id")
        removed = _cleanup_build_temps(manifest)
        manifest["status"] = STATUS_ERROR
        manifest["error"] = {"reason": "interrupted", "at": time.time()}
        manifest.pop("build", None)
        manifest["progress"] = manifest.get("progress", 0.0)
        state.write_json_atomic(manifest_path, manifest)
        state.append_event(
            "interrupted", book_id=book_id, cleaned=removed, prior_pid=pid
        )
        recovered += 1
    return recovered


def run_once() -> int:
    """Backwards-compatible alias for :func:`drain_commands` (used by __main__)."""
    return drain_commands()
