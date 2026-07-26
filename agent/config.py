"""Canonical data-directory layout for the mp3-to-m4b agent.

Single source of truth for every path under
``~/Library/Application Support/mp3-to-m4b/`` (arch/synthesis.md §D, plans.md M0).
The app (reader) and the agent (writer) must agree on these paths; keep this the
ONLY place that spells them out.

    state/                  state.json showcase (agent writes, app reads)
    queue/books/            per-book manifests <book_id>.json (agent writes)
    queue/commands/         app-owned commands <cmd_id>.json (app writes)
    queue/commands/bad/     malformed/invalid commands quarantined here
    covers/                 extracted / fetched / generated cover art
    bin/                    stable FDA-target runner.sh lives here once installed
    venv/                   project virtualenv (Pillow; urllib is stdlib)

Everything is created idempotently by :func:`ensure_data_dirs`.
"""

from __future__ import annotations

import os
from pathlib import Path

# Stable identifiers (must match the app's bundle ids / installer).
APP_NAME = "mp3-to-m4b"
BUNDLE_ID = "com.arrivarus.mp3tom4b"
AGENT_BUNDLE_ID = "com.arrivarus.mp3tom4b.agent"


def support_root() -> Path:
    """Root of the app's Application Support tree.

    Honors ``MP3TOM4B_SUPPORT_DIR`` so tests / dev runs can redirect the whole
    tree to a scratch location without touching the user's real data.
    """
    override = os.environ.get("MP3TOM4B_SUPPORT_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "Application Support" / APP_NAME


def state_dir() -> Path:
    return support_root() / "state"


def state_file() -> Path:
    """state.json — the showcase the app reads (atomic tmp→rename writes)."""
    return state_dir() / "state.json"


def events_file() -> Path:
    """events.jsonl — append-only diagnostics journal (gate-test source)."""
    return state_dir() / "events.jsonl"


def events_prev_file() -> Path:
    """events.jsonl.1 — the ONE rotated generation of the journal (plan v2 Р6).

    The journal is not just diagnostics: the gate invariants are asserted over it
    («no ``build_started`` without a preceding ``confirm_accepted``»), so rotating
    it naively would manufacture violations — the ``build_started`` still in the
    live file, its ``confirm_accepted`` already moved out of sight. Hence exactly
    one generation, and :func:`agent.state.read_events` reads ``.1`` + current as a
    single ordered sequence. Rotation happens only at process start, before the
    first append (single writer + launchd's one-instance-per-label rule = no
    concurrent appender at that moment).
    """
    return state_dir() / "events.jsonl.1"


def notified_file() -> Path:
    """notified.json — agent-owned ledger of already-raised confirm/grouping edges.

    Holds the set of ``book:…``/``group:…`` keys the agent has ALREADY nudged the
    app about (scan._publish_showcase_and_maybe_open). Rising-edge detection against
    this ledger is what makes the auto-raise fire exactly once per new pending
    book / grouping prompt and never loop. The app never reads or writes it.
    """
    return state_dir() / "notified.json"


def presence_file() -> Path:
    """presence.json — agent-owned ledger of each book's present/absent history.

    Written only by scan runs (single writer). Records, per ``book_id``, whether
    the book's source was alive on the LAST scan. A book that is present NOW but
    was marked absent on a prior scan has been moved out and back in — a conscious
    re-drop the inode signal cannot see (a same-volume Finder MOVE keeps st_ino),
    so the scanner re-arms it ``pending-confirm``. The app never reads or writes
    this file.
    """
    return state_dir() / "presence.json"


def install_receipt_file() -> Path:
    """install-receipt.json — the installer's proof that an install COMPLETED.

    Written LAST by ``packaging/installer.sh`` (after the plist was published,
    the job bootstrapped and ``launchctl print`` confirmed the loaded
    ``ProgramArguments[0]``), so its presence means every earlier step succeeded.
    It carries the install ``generation`` UUID the agent also receives through
    ``MP3TOM4B_INSTALL_GENERATION`` — the app compares the two to tell "the plist
    on disk is right" from "launchd is actually RUNNING that plist" (plan v2, B3).

    It lives in the App Support ROOT, deliberately NOT under ``state/``: the app
    watches the state directory, and an install must not masquerade as a state
    update.
    """
    return support_root() / "install-receipt.json"


def queue_dir() -> Path:
    return support_root() / "queue"


def books_dir() -> Path:
    """queue/books/ — per-book manifests written by the agent."""
    return queue_dir() / "books"


def commands_dir() -> Path:
    """queue/commands/ — commands dropped by the app (also a WatchPaths entry)."""
    return queue_dir() / "commands"


def commands_bad_dir() -> Path:
    """queue/commands/bad/ — quarantine for malformed/invalid commands."""
    return commands_dir() / "bad"


def covers_dir() -> Path:
    return support_root() / "covers"


def bin_dir() -> Path:
    return support_root() / "bin"


def venv_dir() -> Path:
    return support_root() / "venv"


def all_dirs() -> list[Path]:
    """Every directory the agent expects to exist, in creation order."""
    return [
        state_dir(),
        queue_dir(),
        books_dir(),
        commands_dir(),
        commands_bad_dir(),
        covers_dir(),
        bin_dir(),
        venv_dir(),
    ]


def ensure_data_dirs() -> list[Path]:
    """Create the full data-directory tree idempotently.

    Returns the list of directories that were ensured (handy for logging /
    M0.1's "agent alive" sanity print). Safe to call on every launch.
    """
    dirs = all_dirs()
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
    return dirs
