"""Scan the watched folder, group mp3 files into books, and write manifests.

M0.2 scope (plans.md): the agent discovers books, writes a per-book manifest
``queue/books/<book_id>.json`` (``status=pending-confirm``, ``source_rev``,
``confirm_token``) and refreshes the ``state/state.json`` showcase. No ffprobe /
ffmpeg yet — chapter metadata is the minimum derivable from filenames; real
probing (durations, ID3 tags, embedded covers) lands in M0.5.

A "book" at M0.2 is a *subfolder* of the watched folder that contains ≥1 mp3.
Loose mp3s in the watch-dir root (and the D1 grouping prompt) are deferred to M1.

Key contracts (arch/synthesis.md §B):
  - ``book_id``   = sha256(absolute subfolder path)[:16] — stable across re-runs.
  - ``source_rev``= sha256 of a deterministic fingerprint of the file list
    (relpath + size + mtime_ns). Duration is intentionally excluded until M0.5.
  - ``confirm_token`` = random hex the agent generates; the app must echo it back
    in its ``confirm-build`` command (replay/forgery guard).
  - Idempotency: an existing manifest with an unchanged ``source_rev`` is NOT
    rewritten (its ``confirm_token`` is preserved); a changed ``source_rev``
    rewrites the manifest and re-arms ``status=pending-confirm``.

The scanner NEVER triggers a build — that lives only in the ``confirm-build``
handler (structural guarantee I2).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path

from . import config, metadata, state

# Default parameters for a freshly discovered book (decisions D2 / D6).
DEFAULT_PARAMS: dict = {
    "bitrate": 192,
    "channels": "stereo",
    "samplerate": 44100,
    "split": False,
}

MANIFEST_STATUS_PENDING = "pending-confirm"
STATE_SCHEMA = 1


def watch_dir() -> Path:
    """Folder the agent watches for incoming books.

    Honors ``MP3TOM4B_WATCH_DIR`` (tests / dev) and defaults to
    ``~/Desktop/mp3-to-m4b`` (plans.md M0.2).
    """
    override = os.environ.get("MP3TOM4B_WATCH_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Desktop" / config.APP_NAME


def book_id_for(source_path: Path) -> str:
    """Stable ``book_id`` = sha256 of the absolute source path, first 16 hex chars.

    The path is resolved to an absolute form (without requiring the dir to still
    exist) so the id is identical on every scan of the same folder.
    """
    canonical = os.path.abspath(os.path.expanduser(str(source_path)))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _list_mp3s(folder: Path) -> list[Path]:
    """Return the folder's direct ``*.mp3`` children, natural-sorted by name.

    Case-insensitive on the ``.mp3`` extension; non-recursive (M0.2 scans one
    subfolder level only). Hidden files (dotfiles) are skipped.
    """
    mp3s = [
        p
        for p in folder.iterdir()
        if p.is_file()
        and not p.name.startswith(".")
        and p.suffix.lower() == ".mp3"
    ]
    mp3s.sort(key=lambda p: metadata.natural_sort_key(p.name))
    return mp3s


def source_rev_for(mp3s: list[Path], base_dir: Path) -> str:
    """Fingerprint a book's inputs deterministically (no duration until M0.5).

    Hashes ``relpath\\0size\\0mtime_ns`` for each file, in the given (already
    natural-sorted) order, joined by newlines. Any add/remove/rename/resize or
    content change (mtime bumps) flips the digest → a stale ``confirm-build`` is
    rejected. Duration is excluded on purpose (it needs ffprobe; arrives in M0.5).
    """
    h = hashlib.sha256()
    for p in mp3s:
        try:
            st = p.stat()
            size = st.st_size
            mtime_ns = st.st_mtime_ns
        except FileNotFoundError:
            # Vanished mid-scan: still contribute a stable, distinct marker.
            size = -1
            mtime_ns = -1
        rel = os.path.relpath(str(p), str(base_dir))
        h.update(f"{rel}\0{size}\0{mtime_ns}\n".encode("utf-8"))
    return h.hexdigest()


def _build_chapters(mp3s: list[Path]) -> list[dict]:
    """Minimal chapter list from filenames (1-based index, cleaned name)."""
    return [
        {
            "index": i,
            "file": p.name,
            "name": metadata.chapter_name_from_filename(p.name),
        }
        for i, p in enumerate(mp3s, start=1)
    ]


def _title_for(folder: Path) -> str:
    """Light display title for the showcase: the folder name (no parsing at M0.2)."""
    return folder.name


def title_for_manifest(manifest: dict) -> str:
    """Display title for a manifest (folder name of its ``src_dir``).

    Used by the dispatcher's fake-engine to name the output. Falls back to the
    ``book_id`` if ``src_dir`` is somehow absent so the title is never empty.
    """
    src_dir = manifest.get("src_dir")
    if src_dir:
        return _title_for(Path(src_dir))
    return str(manifest.get("book_id", "book"))


def write_manifest_for_book(folder: Path) -> dict:
    """Write / refresh the manifest for one book folder; return the manifest dict.

    Idempotent: if a manifest already exists for this ``book_id`` and its
    ``source_rev`` is unchanged, the file is left untouched (``confirm_token``
    preserved) and the existing manifest is returned. A changed ``source_rev``
    rewrites the manifest with a fresh ``confirm_token`` and re-arms
    ``status=pending-confirm``.
    """
    folder = Path(folder)
    bid = book_id_for(folder)
    mp3s = _list_mp3s(folder)
    src_dir = os.path.abspath(str(folder))
    rev = source_rev_for(mp3s, folder)

    manifest_path = config.books_dir() / f"{bid}.json"
    existing = state.read_json(manifest_path, default=None)
    if (
        isinstance(existing, dict)
        and existing.get("source_rev") == rev
        and existing.get("book_id") == bid
    ):
        # Unchanged inputs → keep the manifest (and its confirm_token) as-is.
        return existing

    manifest = {
        "book_id": bid,
        "src_dir": src_dir,
        "status": MANIFEST_STATUS_PENDING,
        "source_rev": rev,
        "confirm_token": secrets.token_hex(16),
        "chapters": _build_chapters(mp3s),
        "cover_state": "unknown",  # placeholder; cover chain → M1
        "params": dict(DEFAULT_PARAMS),
        # Idempotency ledger (M0.6): idempotency_keys already built for THIS book.
        # Keys are revision-scoped (the app derives them from book_id+source_rev),
        # so a changed source_rev re-arms the book with a fresh, empty ledger —
        # a legitimately new build is allowed, a stale double-click is not.
        "processed_keys": [],
        "ts": time.time(),
    }
    state.write_json_atomic(manifest_path, manifest)
    return manifest


def scan_watch_folder(watch: Path) -> list[dict]:
    """Discover books under ``watch`` and ensure a manifest for each.

    Returns the list of (current) manifest dicts, ordered by book folder name.
    A book = a direct subfolder containing ≥1 mp3 (M0.2). Returns an empty list
    if the watch dir does not exist yet.
    """
    watch = Path(watch)
    if not watch.is_dir():
        return []

    subfolders = sorted(
        (p for p in watch.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: metadata.natural_sort_key(p.name),
    )

    manifests: list[dict] = []
    for folder in subfolders:
        if not _list_mp3s(folder):
            continue  # no mp3s → not a book (yet)
        manifests.append(write_manifest_for_book(folder))
    return manifests


def build_state(manifests: list[dict], watch: Path) -> dict:
    """Compose the lightweight ``state.json`` showcase from the manifests.

    The agent is the SINGLE writer of state. ``books`` is intentionally light
    (id/title/status only); rich per-book data stays in the manifests so the
    showcase is not rewritten on every chapter-level change. ``batch``/``totals``
    are placeholders until the fake-engine (M0.5) and real pipeline (M1).
    """
    books = [
        {
            "book_id": m["book_id"],
            "title": _title_for(Path(m["src_dir"])),
            "status": m.get("status", MANIFEST_STATUS_PENDING),
        }
        for m in manifests
    ]
    return {
        "schema": STATE_SCHEMA,
        "agent": {"watch_dir": os.path.abspath(str(watch))},
        "books": books,
        "batch": {"active": False, "total": 0, "done": 0},
        "totals": {"books": len(books)},
        "ts": time.time(),
    }


def run_scan(watch: Path | None = None) -> dict:
    """Full M0.2 scan pass: ensure manifests, then write the state showcase.

    Returns the showcase dict that was written. Safe to call repeatedly
    (idempotent per book; see :func:`write_manifest_for_book`).
    """
    config.ensure_data_dirs()
    target = Path(watch) if watch is not None else watch_dir()
    manifests = scan_watch_folder(target)
    showcase = build_state(manifests, target)
    state.write_state(showcase)
    return showcase
