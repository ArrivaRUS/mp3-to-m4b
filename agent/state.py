"""Atomic JSON persistence for the agent's authoritative files.

The agent is the SINGLE writer (arch/synthesis.md §B). A reader (the app) must
never observe a half-written file, so every write goes to a temp file in the
SAME directory and is then ``os.replace``-d over the target — an atomic rename
on the same filesystem. This is shared by ``state.json`` and the per-book
manifests in ``queue/books/``.

This module is implemented for real at M0.1 (it is small, low-risk, and every
later milestone depends on it); the showcase/manifest *schemas* are filled in at
M0.2.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from . import config


def write_json_atomic(path: Path, data: Any) -> None:
    """Serialize ``data`` to ``path`` atomically (tmp file → ``os.replace``).

    The temp file is created in the destination directory so the final rename is
    a same-filesystem atomic operation. ``fsync`` before rename guards against a
    truncated file surviving a crash. The parent directory is created if missing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)  # atomic on the same filesystem
    except BaseException:
        # Never leave a stray temp file behind on failure.
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON from ``path``; return ``default`` if missing or unreadable.

    A malformed/half-written file (should not happen for our own atomic writes,
    but commands come from the app) yields ``default`` rather than raising.
    """
    path = Path(path)
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_state(state: dict[str, Any]) -> None:
    """Write the ``state.json`` showcase atomically (full schema → M0.2)."""
    write_json_atomic(config.state_file(), state)


def read_state(default: Any = None) -> Any:
    """Read the ``state.json`` showcase (None/default if absent)."""
    return read_json(config.state_file(), default=default)


def append_event(kind: str, **fields: Any) -> None:
    """Append one diagnostics record to ``events.jsonl`` (one JSON object/line).

    The journal is the gate-test source (arch/synthesis.md §B): e.g. proving
    there is never a ``build_started`` without a preceding ``confirm_accepted``.
    ``ts`` is stamped automatically if the caller does not pass one. A failure to
    journal must never abort the surrounding operation.

    Durability (M0.6): the agent runs as a *separate* launchd process, fired once
    per ``WatchPaths`` event and then exiting. A buffered write that is never
    flushed before the process is reaped can leave ``events.jsonl`` looking empty
    to a reader inspecting it right after the click (the symptom seen in the live
    run). We therefore open the handle, write, ``flush`` and ``fsync`` it on every
    record so each event is on disk the instant the call returns — and so the
    no-``build_started``-without-``confirm_accepted`` gate is observable in real
    time, not just after a clean interpreter shutdown.
    """
    record: dict[str, Any] = {"event": kind, "ts": fields.pop("ts", time.time())}
    record.update(fields)
    path = config.events_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())  # durable before this call returns
    except OSError:
        # Diagnostics are best-effort; do not let a journal hiccup fail a build.
        pass


# ── Journal rotation (plan v2 Р6 / risk M7f) ─────────────────────────────────
#: Rotate once the live journal passes this size. Generous: at ~200 B per record
#: this is tens of thousands of events, i.e. months of normal operation, so the
#: rotation is a safety valve against unbounded growth rather than a routine event.
EVENTS_ROTATE_BYTES = 2 * 1024 * 1024
_ROTATE_ENV = "MP3TOM4B_EVENTS_ROTATE_BYTES"


def _rotate_threshold() -> int:
    """Rotation threshold in bytes (``MP3TOM4B_EVENTS_ROTATE_BYTES`` overrides)."""
    raw = os.environ.get(_ROTATE_ENV)
    if raw is None:
        return EVENTS_ROTATE_BYTES
    try:
        val = int(raw)
    except ValueError:
        return EVENTS_ROTATE_BYTES
    return val if val > 0 else EVENTS_ROTATE_BYTES


def rotate_events_if_needed() -> bool:
    """Rotate ``events.jsonl`` → ``events.jsonl.1`` once, at PROCESS START.

    Returns True if a rotation happened. Call this before the first
    :func:`append_event` of the run and nowhere else — that timing is the whole
    safety argument (plan v2 Р6):

      · the agent is the single writer AND launchd never starts a second instance
        of the same label, so at process start there is provably no concurrent
        appender. Rotating mid-run would be a race with our own open handles;
      · the journal carries GATE invariants, so the reader must keep seeing the
        records that moved. :func:`read_events` therefore reads ``.1`` and then the
        live file as one ordered sequence — a ``build_started`` can never be
        separated from its ``confirm_accepted`` just because a rename happened
        between them;
      · exactly ONE generation is kept: ``.1`` is replaced, not chained. Bounded
        disk use, and a reader that never has to guess how many files exist.

    NOTE — the launchd ``StandardOutPath`` log is a different problem and is NOT
    handled here: launchd holds that fd open for the life of the job, so renaming
    the file from inside the agent would leave us writing into the rotated inode.
    That one belongs to the plist/installer (it re-opens the path on every job
    start), and is tracked as such.
    """
    path = config.events_file()
    try:
        if path.stat().st_size <= _rotate_threshold():
            return False
        os.replace(path, config.events_prev_file())
        return True
    except FileNotFoundError:
        return False
    except OSError:
        # A journal that cannot be rotated is not a reason to fail a run.
        return False


def read_events() -> list[dict[str, Any]]:
    """Read every record from ``events.jsonl`` (oldest-first); ``[]`` if absent.

    Reads the rotated generation ``events.jsonl.1`` FIRST and the live file after
    it, so the result is one continuous oldest-first sequence across a rotation.
    That is not a convenience: the §M0 gate asserts journal invariants (e.g. no
    ``build_started`` without a preceding ``confirm_accepted``), and a reader that
    only saw the live file would report a violation the moment rotation split a
    pair. Malformed lines are skipped rather than raising — the journal is
    diagnostics, never a correctness dependency of the running agent.
    """
    out: list[dict[str, Any]] = []
    for path in (config.events_prev_file(), config.events_file()):
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(rec, dict):
                        out.append(rec)
        except FileNotFoundError:
            continue
    return out
