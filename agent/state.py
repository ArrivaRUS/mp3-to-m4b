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


def read_events() -> list[dict[str, Any]]:
    """Read every record from ``events.jsonl`` (oldest-first); ``[]`` if absent.

    Used by the §M0 self-check to assert journal invariants (e.g. no
    ``build_started`` without a preceding ``confirm_accepted``). Malformed lines
    are skipped rather than raising — the journal is diagnostics, never a
    correctness dependency of the running agent.
    """
    path = config.events_file()
    out: list[dict[str, Any]] = []
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
        return []
    return out
