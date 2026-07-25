"""Scan the watched folder, group mp3 files into books, and write manifests.

M0.5 scope (plans.md): the agent discovers books, writes a per-book manifest
``queue/books/<book_id>.json`` (``status=pending-confirm``, ``source_rev``,
``confirm_token``) and refreshes the ``state/state.json`` showcase. Chapters are
now filled with REAL data via ffprobe (durations, ID3 tags, embedded-cover
detection + an extracted preview); book ``title``/``author`` are resolved from
tags with a folder-name fallback (research §5, D1/D5).

★ source_rev stays a PURE file fingerprint (v2: relpath + size + mtime_ns +
st_ino + st_dev) — probe data is payload for display/build, NOT part of the
revision. Folding duration into source_rev would bump it on noise and re-arm
``confirm_token`` on every scan, breaking the M0 idempotency the protocol
depends on. The inode/device components make a CONSCIOUS re-drop (Finder copy =
new inodes) re-arm a done book; a same-volume MOVE (inode kept) is caught by the
presence ledger (:func:`_reconcile_presence`); legacy v1 manifests are upgraded
silently on the first scan (:func:`source_rev_legacy_for`).

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

import datetime
import hashlib
import os
import secrets
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from . import config, cover, metadata, probe, state

# Default parameters for a freshly discovered book (decisions D2 / D6).
DEFAULT_PARAMS: dict = {
    "bitrate": 192,
    "channels": "stereo",
    # ``None`` is the "keep the source sample rate" sentinel: the build resolver
    # (build_m4b._samplerate) falls back to the manifest's ``source_samplerate``
    # (max of the source mp3s) instead of resampling. The user can still pin an
    # explicit 44100 / 48000 in the confirm window, which overrides this.
    "samplerate": None,
    "split": False,
    # P1 split: when ``split`` is True the built .m4b is cut into parts each ≤ this
    # many MB on chapter boundaries (research §3). Default ≈300 MB. Ignored while
    # ``split`` is False (the default → one file, the path is unchanged).
    "split_threshold_mb": 300,
    # Speed-up mode (D15 / arch/speedup-synthesis.md Ступень 2): "fast" (default) =
    # parallel groups of consecutive chapters → concat stream-copy (×6–10; possible
    # ~25 ms silence at group/chapter seams, imperceptible for speech); "seamless" =
    # single-pass encode (bit-exact at seams, slower). build_m4b.build() branches on
    # this; the confirm window's toggle overrides it per-book (rides in the command
    # ``params`` via the P-PARAMS whitelist).
    "build_mode": "fast",
}

# E10 copy-stability debounce. Before a freshly-changed book is armed
# ``pending-confirm`` we snapshot every mp3's (size, mtime_ns), wait a short beat,
# and snapshot again; if anything grew/changed the files are STILL being copied,
# so we skip the book this pass and let the next launchd-fired scan re-arm it once
# stable. Without this guard a confirm against a half-copied mp3 builds a short /
# corrupt chapter (the truncated file still probes a header duration that lies).
# The wait is only paid for books whose inputs ACTUALLY changed (a new/edited
# book), never for the steady-state of an already-recognized book — so a normal
# scan of settled books is not slowed. Overridable for deterministic tests.
STABILITY_DEBOUNCE_S = 0.5
# An env override lets the self-check shrink/grow the window deterministically.
_STABILITY_ENV = "MP3TOM4B_STABILITY_DEBOUNCE_S"

MANIFEST_STATUS_PENDING = "pending-confirm"
# Status of a book whose build is in flight (dispatcher writes this). Used to gate
# carrying the live ``progress`` field onto a showcase row (Task 2): progress is
# preserved across a refresh ONLY while the book is still converting.
STATUS_CONVERTING = "converting"
# Statuses that a conscious RE-DROP of the same book must reverse (lesson
# .patches/004 — «намерение пользователя ≠ новизна контента»): both mean "the user
# is finished with this book", and putting the folder back means he is not.
#   · ``done``    — already built, dropped again → build it again;
#   · ``skipped`` — taken off the pipeline by «Пропустить», dropped again → the
#     mark is lifted and the confirm window comes back. Without this a skip would
#     be a permanent black mark: the user re-drops the book and the app stays
#     silent — exactly the bug .patches/004 was written about.
# The COPY shape of a re-drop needs nothing here (new inodes → new ``source_rev``
# → _write_manifest re-arms on its own); this set is the MOVE shape, where the
# inode survives and only the presence ledger can see the gesture.
REDROP_REARM_STATUSES = ("done", "skipped")
# Status of a loose-mp3 set awaiting the user's grouping decision (D1, flows S4).
# The agent does NOT write a book manifest for these yet — the choice command
# materializes the manifest(s). Until then the group lives only in state.json.
GROUP_STATUS_AWAITING = "grouping-ask"
STATE_SCHEMA = 1

# Process-lifetime cache for the ffmpeg version string (the Status «ffmpeg» stat
# card, spec §5). Probing ffmpeg costs a subprocess spawn; the version cannot change
# while the agent process is alive, so we resolve it at most once per process. ``""``
# means "no ffmpeg on PATH" — surfaced honestly as an empty engine string (the app
# shows a placeholder rather than a fabricated number). ``None`` = not yet resolved.
_ENGINE_VERSION_CACHE: str | None = None


def _probe_engine_version() -> str:
    """Resolve ffmpeg's version string from ``ffmpeg -version`` (first line).

    Returns the short release token (e.g. ``"7.1"`` / ``"6.1.1"``) parsed out of the
    banner ``ffmpeg version <token> ...``; on any failure (ffmpeg absent, non-zero
    exit, odd banner) returns ``""`` so the showcase carries an honest empty string
    rather than guessing. Never raises. NOT cached here — :func:`engine_version`
    owns the process-lifetime cache.
    """
    exe = shutil.which("ffmpeg")
    if not exe:
        return ""
    try:
        proc = subprocess.run(
            [exe, "-version"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    first = (proc.stdout or proc.stderr or "").splitlines()
    if not first:
        return ""
    line = first[0].strip()
    # Banner shape: "ffmpeg version 7.1 Copyright (c) ...". Take the 3rd token,
    # trimming a leading "n" or "v" some builds prefix (e.g. "n7.1", "v6.1").
    parts = line.split()
    if len(parts) >= 3 and parts[0] == "ffmpeg" and parts[1] == "version":
        token = parts[2].lstrip("nv")
        return token or line
    return line  # unexpected banner → surface the whole first line (still non-empty)


def engine_version() -> str:
    """ffmpeg version for the Status «ffmpeg» stat card, cached per process.

    The agent is the single writer of state, so it (not the app) projects the engine
    string into ``state.json``. Cached because the value is immutable for the life of
    the process and probing spawns a subprocess on every scan/refresh otherwise.
    """
    global _ENGINE_VERSION_CACHE
    if _ENGINE_VERSION_CACHE is None:
        _ENGINE_VERSION_CACHE = _probe_engine_version()
    return _ENGINE_VERSION_CACHE


def _result_built_at(manifest: dict) -> float | None:
    """The ``result.built_at`` epoch a done manifest carries, or ``None``.

    Mirrors the manifest shape the dispatcher stamps on success
    (``result = {output, output_path, built_at}``). Defensive: a missing/odd result
    block yields ``None`` (the book just doesn't count toward «За сегодня»).
    """
    res = manifest.get("result")
    if not isinstance(res, dict):
        return None
    ts = res.get("built_at")
    return ts if isinstance(ts, (int, float)) else None


def _is_built_today(built_at: float | None, now: float | None = None) -> bool:
    """True if ``built_at`` (epoch seconds) falls on the CURRENT local calendar day.

    The «За сегодня» stat resets at local midnight (the user's day, not UTC). ``now``
    is injectable for deterministic tests; default is the live wall clock — the date
    is taken at RUNTIME (per the brief), never hard-coded.
    """
    if built_at is None:
        return False
    tz = datetime.datetime.now().astimezone().tzinfo
    today = datetime.datetime.now(tz).date()
    built_day = datetime.datetime.fromtimestamp(built_at, tz).date()
    return built_day == today


def _project_totals(manifests: list[dict], book_count: int) -> dict:
    """Build the showcase ``totals`` block from the projected manifests.

    Spec §5 stat cards «Собрано / За сегодня»:
      - ``built`` — number of books whose status is ``done`` (lifetime in the queue);
      - ``today`` — of those, the ones whose ``result.built_at`` is the current local
        day (runtime date);
      - ``books`` — preserved (the prior projection's count, = number of showcase
        rows) so nothing downstream that read ``totals.books`` breaks.
    """
    built = 0
    today = 0
    for m in manifests:
        if m.get("status") != "done":
            continue
        built += 1
        if _is_built_today(_result_built_at(m)):
            today += 1
    return {"books": book_count, "built": built, "today": today}


def _cover_web_enabled() -> bool:
    """Whether the cover chain may hit the network during a scan.

    Default ON (D7: the real chain prefers a found web cover before falling back
    to generation). Set ``MP3TOM4B_COVER_WEB=0`` to force the offline path
    (deterministic tests / no-network runs) — generation still guarantees a cover.
    """
    return os.environ.get("MP3TOM4B_COVER_WEB", "1") not in ("0", "false", "no", "")


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


def _list_loose_mp3s(watch: Path) -> list[Path]:
    """Return the watch-dir ROOT's direct ``*.mp3`` files (D1 grouping inputs).

    These are the "loose" files that live straight in the watched folder, NOT in a
    subfolder-book. Same rules as :func:`_list_mp3s` (case-insensitive ``.mp3``,
    skip dotfiles, natural-sorted) — a subfolder is never a loose file because the
    iteration only keeps ``is_file()`` children. Empty when there are none.
    """
    return _list_mp3s(watch)


def group_id_for(loose: list[Path]) -> str:
    """Stable ``group_id`` for a set of loose files = sha256 of their sorted paths.

    The id hashes the SORTED absolute paths joined by newlines, first 16 hex chars
    (decisions D1, brief). Sorting first makes the id independent of iteration
    order; using absolute paths makes it stable across scans of the same watch
    folder and distinct from any per-file ``book_id``. An empty set yields the
    hash of the empty string (callers never project an empty group).
    """
    canonical = sorted(os.path.abspath(os.path.expanduser(str(p))) for p in loose)
    h = hashlib.sha256()
    h.update("\n".join(canonical).encode("utf-8"))
    return h.hexdigest()[:16]


def grouping_idempotency_key(group_id: str, rev: str) -> str:
    """Stable idempotency key for "resolve THIS group at THIS revision".

    Mirrors the book build key format (``<id>:<rev[:16]>``) the app already uses in
    :class:`EngineClient` — the app derives the SAME key for the grouping-choice
    command from ``group_id`` + ``rev``. This single identity does double duty:
      - the dispatcher dedups a repeat choice (double-click) whose key is already
        in the resolved ledger;
      - the scanner SUPPRESSES re-projecting a group whose key is resolved, because
        the loose files physically remain in the watch root after materialization
        (they are now claimed by the materialized manifests, not a new prompt).
    A changed loose set flips ``rev`` → a new key → the group legitimately re-arms.
    """
    return f"{group_id}:{rev[:16]}"


def group_rev_for(loose: list[Path], base_dir: Path) -> str:
    """Fingerprint a pending GROUP's loose-file inputs (mirror of source_rev).

    Reuses :func:`source_rev_for` semantics (relpath + size + mtime_ns over the
    natural-sorted list) so any add/remove/rename/resize of a loose file flips the
    digest — exactly like a book's ``source_rev``. The group's ``rev`` is the
    anti-stale guard the choice command is validated against: a choice captured
    against an old loose-file set is rejected once the set changes (the same
    "inputs moved after recognition" protection M0 gives book builds). Duration is
    excluded on purpose (probe data is payload, not revision).

    Because this delegates to the v2 (inode+device) fingerprint, a conscious
    RE-DROP of the same loose files also flips the group rev → a new
    :func:`grouping_idempotency_key` → the resolved ledger no longer silences
    the prompt — the loose-mp3 twin of the book re-drop fix.
    """
    return source_rev_for(loose, base_dir)


def group_rev_legacy_for(loose: list[Path], base_dir: Path) -> str:
    """Legacy (v1) group rev — mirror of :func:`source_rev_legacy_for`.

    Used ONLY by the migration guard in :func:`_build_pending_group`: a resolved
    ledger written by a pre-v2 agent holds keys minted from v1 revs; checking
    BOTH keys keeps an already-decided loose set from re-prompting after the
    agent update (the v2 key is then back-filled into the ledger).
    """
    return source_rev_legacy_for(loose, base_dir)


# Manifest field marking the source_rev fingerprint format. v2 (current) folds
# st_ino + st_dev into every file's line; v1 (legacy, pre-2026-07-07) hashed only
# relpath + size + mtime_ns. Kept in the manifest for future per-volume logic.
SOURCE_REV_VERSION = 2


def source_rev_for(mp3s: list[Path], base_dir: Path) -> str:
    """Fingerprint a book's inputs deterministically (rev v2: identity-aware).

    Hashes ``relpath\\0size\\0mtime_ns\\0st_ino\\0st_dev`` for each file, in the
    given (already natural-sorted) order, joined by newlines. Any add/remove/
    rename/resize or content change (mtime bumps) flips the digest → a stale
    ``confirm-build`` is rejected.

    ★ Why inode+device (v2): a Finder COPY of a folder preserves size AND mtime,
    so the v1 fingerprint (relpath+size+mtime_ns) made a conscious re-drop of an
    already-built book dedup silently as ``done``. Copying always mints new
    inodes (``st_ino``) — and a drop from another volume changes ``st_dev`` —
    so folding both in makes any physically-new file flip the rev and re-arm the
    existing changed-``source_rev`` path in :func:`_write_manifest`. A book that
    merely SITS untouched keeps its ino/dev/mtime → rev stable → no re-arm at
    login (RunAtLoad) or on repeated scans. iCloud eviction/rematerialization
    keeps the same inode (dataless flag on the SAME file) → lying books stay
    silent. Duration stays excluded on purpose (probe data is payload, not
    revision). Legacy v1 manifests are migrated silently — see
    :func:`source_rev_legacy_for` and the upgrade branch in
    :func:`_write_manifest`.
    """
    h = hashlib.sha256()
    for p in mp3s:
        try:
            st = p.stat()
            size = st.st_size
            mtime_ns = st.st_mtime_ns
            ino = st.st_ino
            dev = st.st_dev
        except FileNotFoundError:
            # Vanished mid-scan: still contribute a stable, distinct marker.
            size = -1
            mtime_ns = -1
            ino = -1
            dev = -1
        rel = os.path.relpath(str(p), str(base_dir))
        h.update(f"{rel}\0{size}\0{mtime_ns}\0{ino}\0{dev}\n".encode("utf-8"))
    return h.hexdigest()


def source_rev_legacy_for(mp3s: list[Path], base_dir: Path) -> str:
    """The PRE-v2 (legacy v1) fingerprint: relpath + size + mtime_ns only.

    Kept verbatim so the first scan after an agent update can tell a FORMAT
    change apart from a user re-drop: a manifest whose stored ``source_rev``
    equals this legacy digest of the CURRENT files has untouched sources — the
    only thing that changed is our fingerprint formula, so the manifest is
    silently upgraded in place (status/token/ledger preserved, NO re-arm, no
    window storm after the update). See the migration branch in
    :func:`_write_manifest`.
    """
    h = hashlib.sha256()
    for p in mp3s:
        try:
            st = p.stat()
            size = st.st_size
            mtime_ns = st.st_mtime_ns
        except FileNotFoundError:
            size = -1
            mtime_ns = -1
        rel = os.path.relpath(str(p), str(base_dir))
        h.update(f"{rel}\0{size}\0{mtime_ns}\n".encode("utf-8"))
    return h.hexdigest()


def _stability_debounce_s() -> float:
    """Resolve the copy-stability wait (seconds), env-overridable for tests.

    Defaults to :data:`STABILITY_DEBOUNCE_S`. ``MP3TOM4B_STABILITY_DEBOUNCE_S``
    lets a self-check shrink it (fast deterministic runs) or grow it; a malformed
    / negative value falls back to the default.
    """
    raw = os.environ.get(_STABILITY_ENV)
    if raw is None:
        return STABILITY_DEBOUNCE_S
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return STABILITY_DEBOUNCE_S
    return val if val >= 0 else STABILITY_DEBOUNCE_S


def _size_mtime_snapshot(mp3s: list[Path]) -> dict[str, tuple[int, int]]:
    """Map each file → its ``(size, mtime_ns)`` right now (missing → sentinel).

    A vanished file is recorded as ``(-1, -1)`` so it still participates in the
    comparison (a file disappearing mid-copy is just as "unstable" as one growing).
    """
    snap: dict[str, tuple[int, int]] = {}
    for p in mp3s:
        try:
            st = p.stat()
            snap[str(p)] = (st.st_size, st.st_mtime_ns)
        except OSError:
            snap[str(p)] = (-1, -1)
    return snap


def _files_are_stable(mp3s: list[Path]) -> bool:
    """True iff every mp3's size+mtime is unchanged across a short debounce (E10).

    Snapshots all files, waits :func:`_stability_debounce_s`, snapshots again and
    compares. Any difference (a growing copy, a touched mtime, a vanished file)
    means the set is still settling → ``False`` (the caller skips arming the book
    this pass). An empty set is trivially stable. A zero/near-zero debounce makes
    this a cheap single comparison (used by tests / opt-out). The source mp3s are
    only ``stat``-ed — never read or written (I1).
    """
    if not mp3s:
        return True
    before = _size_mtime_snapshot(mp3s)
    wait = _stability_debounce_s()
    if wait > 0:
        time.sleep(wait)
    after = _size_mtime_snapshot(mp3s)
    return before == after


def _probe_book(mp3s: list[Path]) -> list[dict]:
    """Probe every mp3 once (durations, tags, cover flag). Order = input order."""
    return [probe.probe_file(p) for p in mp3s]


def _build_chapters(ordered_probes: list[dict]) -> list[dict]:
    """Build the real chapter list from ordered probe dicts.

    Each chapter carries its 1-based ``index``, source ``file``, resolved
    ``name`` (ID3 title → cleaned filename, D5) and ``duration_ms`` from probe
    (``None`` if the file was unreadable — surfaced, not hidden). Order is exactly
    the order handed in (already track-/natural-sorted by the caller).
    """
    chapters: list[dict] = []
    for i, pr in enumerate(ordered_probes, start=1):
        chapters.append(
            {
                "index": i,
                "file": pr.get("file", ""),
                "name": metadata.chapter_name(pr),
                "duration_ms": pr.get("duration_ms"),
            }
        )
    return chapters


def _total_duration_ms(ordered_probes: list[dict]) -> int:
    """Sum of all readable chapter durations in milliseconds (skips unreadable)."""
    return sum(
        pr["duration_ms"]
        for pr in ordered_probes
        if isinstance(pr.get("duration_ms"), int)
    )


def _source_samplerate(ordered_probes: list[dict]) -> int | None:
    """The source mp3s' sample rate to keep by default — the MAX across readable files.

    Probe reports each file's ``sample_rate`` (Hz, or None if unreadable). We take
    the MAX of all positive values: if a collection mixes rates we upsample the
    minority to the highest (lossless-ish) rather than EVER downsampling the
    majority (which would throw away audio bandwidth). Returns ``None`` when no
    readable file carries a sample rate — the build then falls back to its own
    DEFAULT_SAMPLERATE. This is the "as in source" anchor the manifest carries so
    the build need not re-probe.
    """
    rates = [
        pr["sample_rate"]
        for pr in ordered_probes
        if isinstance(pr.get("sample_rate"), int) and pr["sample_rate"] > 0
    ]
    return max(rates) if rates else None


def _build_pending_group(
    watch: Path,
    loose: list[Path],
    previous: dict | None = None,
    resolved: list | None = None,
) -> dict | None:
    """Project the watch-root's loose mp3s into a pending-group dict for state.json.

    The group is what the app's grouping sheet (S4 / ref 06) renders and what the
    ``grouping-choice`` command materializes into book manifest(s). We probe the
    loose files ONCE for durations (the sheet shows the count + total length); the
    choice handler re-derives chapters from the live files (single source of truth).
    The group carries a stable ``group_id`` + a revision/token pair so a stale or
    forged choice is rejected — the mirror of a book's ``source_rev`` /
    ``confirm_token``.

    Returns ``None`` (no prompt) when this exact loose set was ALREADY resolved —
    its :func:`grouping_idempotency_key` is in ``resolved``. The loose files stay in
    the watch root after a combine/separate, so without this guard the very next
    scan would re-prompt for files the user already decided on. A changed loose set
    flips ``rev`` → a new key → it legitimately re-arms.

    Idempotency: ``token`` must be STABLE across re-scans of an unchanged loose set,
    exactly like a book manifest preserves its ``confirm_token`` on an unchanged
    ``source_rev``. A launchd-fired scan runs on every WatchPaths wake-up; rotating
    the token each time would reject a choice the user already issued. So when the
    ``previous`` group (from the prior state.json) has the SAME ``group_id`` and
    ``rev``, we keep its ``token`` (and ``ts``); any change re-arms a fresh token.

    The agent does NOT write a manifest for these files yet (the user must choose
    combine vs. separate first) — this dict lives only in ``state.json``.
    """
    ordered = metadata.order_chapters(_probe_book(loose))
    gid = group_id_for(loose)
    rev = group_rev_for(loose, watch)
    legacy_rev = group_rev_legacy_for(loose, watch)

    # Already decided on this exact set → no prompt (files linger in the root).
    if resolved and grouping_idempotency_key(gid, rev) in resolved:
        return None
    # v1→v2 MIGRATION: a ledger written by a pre-v2 agent holds the LEGACY key.
    # Untouched files whose legacy key is resolved were already decided — the
    # only change is the fingerprint formula, so back-fill the v2 key into the
    # ledger IN PLACE (run_scan hands us its live list; build_state persists it)
    # and stay silent. A real re-drop mints new inodes → the legacy digest of
    # the NEW files matches no stored key → the prompt legitimately re-arms.
    if resolved and grouping_idempotency_key(gid, legacy_rev) in resolved:
        if isinstance(resolved, list):
            resolved.append(grouping_idempotency_key(gid, rev))
        return None
    # Names in natural/track order so the sheet's chips match the would-be chapter
    # order; the choice handler re-orders the live files itself (single source).
    files = [str(pr.get("file", "")) for pr in ordered]

    # Preserve token/ts when the loose set is byte-identical to the last scan.
    if (
        isinstance(previous, dict)
        and previous.get("group_id") == gid
        and previous.get("rev") == rev
        and previous.get("token")
    ):
        token = str(previous["token"])
        ts = previous.get("ts", time.time())
    elif (
        isinstance(previous, dict)
        and previous.get("group_id") == gid
        and previous.get("rev") == legacy_rev
        and previous.get("token")
    ):
        # v1→v2 migration of a STILL-PENDING prompt: same untouched files, only
        # the rev formula changed. Keep the token/ts (the user's open sheet stays
        # valid after it re-reads state) and swap the notified key so the app is
        # not re-raised for a prompt it was already raised for.
        token = str(previous["token"])
        ts = previous.get("ts", time.time())
        _notified_replace(
            _group_edge_key(previous),
            _group_edge_key({"group_id": gid, "rev": rev, "token": token}),
        )
    else:
        token = secrets.token_hex(16)
        ts = time.time()

    return {
        "group_id": gid,
        "status": GROUP_STATUS_AWAITING,
        "rev": rev,
        "token": token,
        "watch_dir": os.path.abspath(str(watch)),
        "files": files,
        "count": len(files),
        "total_duration_ms": _total_duration_ms(ordered),
        "source_samplerate": _source_samplerate(ordered),
        "ts": ts,
    }


def _resolve_cover(bid: str, ordered_probes: list[dict], mp3s_by_name: dict) -> dict:
    """Detect an embedded cover and extract a preview into ``covers/``.

    Returns ``{"cover_state": ..., "cover_preview": <path|None>}``. The cover is
    taken from the FIRST file that carries one (research §4). On a successful
    extract ``cover_state="embedded"`` and ``cover_preview`` points at the file;
    if no file has a cover (or extraction fails) ``cover_state="none"`` and the
    web/generate chain (M1) will take over. Never raises.
    """
    for pr in ordered_probes:
        if not pr.get("has_embedded_cover"):
            continue
        src = mp3s_by_name.get(pr.get("file"))
        if src is None:
            continue
        dest = config.covers_dir() / f"{bid}-embedded.jpg"
        if probe.extract_cover(src, dest):
            return {"cover_state": "embedded", "cover_preview": str(dest)}
        # Cover advertised but extraction failed → fall through to next candidate.
    return {"cover_state": "none", "cover_preview": None}


def _title_for(folder: Path) -> str:
    """Light display title for the showcase: the folder name (no parsing)."""
    return folder.name


def title_for_manifest(manifest: dict) -> str:
    """Display title for a manifest, used by the engine to name the output.

    Prefers the resolved ``title`` (ID3 album / parsed folder, M0.5), falling
    back to the folder name of ``src_dir`` and finally the ``book_id`` so the
    title is never empty.
    """
    title = manifest.get("title")
    if title:
        return str(title)
    src_dir = manifest.get("src_dir")
    if src_dir:
        return _title_for(Path(src_dir))
    return str(manifest.get("book_id", "book"))


def _write_manifest(
    *,
    bid: str,
    src_dir: str,
    mp3s: list[Path],
    base_dir: Path,
    author: str,
    title: str,
    debounce: bool = False,
    force: bool = False,
) -> dict | None:
    """Build + atomically write ONE pending-confirm manifest from explicit inputs.

    The shared core behind both the subfolder scanner and the grouping
    materializer. Idempotent: if a manifest already exists for ``bid`` with the SAME
    ``source_rev`` it is returned untouched (``confirm_token`` preserved, no
    re-probe / no re-fetch of covers). Otherwise it probes the files, resolves the
    cover chain (embedded → web → generated, always ≥1 — PRD G4) and writes a fresh
    ``pending-confirm`` manifest with a new ``confirm_token``.

    ``source_rev`` is the pure file-list fingerprint (relpath to ``base_dir`` +
    size + mtime_ns); probe/cover data is display payload and never folded in (so
    an unchanged set keeps its rev/token across scans — M0 idempotency).

    ``force`` bypasses the unchanged-``source_rev`` short-circuit: the files are
    ALWAYS re-probed and a fresh ``pending-confirm`` manifest rewritten even when the
    inputs did not move. This is the «Собрать заново» (reconvert) path — a book built
    by an OLD agent has a stale manifest missing today's fields (e.g.
    ``source_samplerate``); re-arming it in place would preserve the gaps, so we
    re-run the SAME build here to refill every field from a fresh probe. ``source_rev``
    is still recomputed from the (unchanged) files, so it lands on the same value —
    the M0 idempotency contract is intact; only the manifest payload is refreshed.

    E10: when ``debounce`` is set (the subfolder scan path) and we are about to
    write a NEW/changed manifest, we first verify the mp3s are not still being
    copied (size/mtime stable across a short beat — :func:`_files_are_stable`). If
    they are still settling we return ``None`` WITHOUT writing anything, so the
    book is simply not armed this pass and the next scan re-arms it once stable.
    The wait is paid ONLY here (a genuinely new/changed book), never on the
    unchanged-rev fast path above — settled books are not slowed.
    """
    rev = source_rev_for(mp3s, base_dir)
    manifest_path = config.books_dir() / f"{bid}.json"
    existing = state.read_json(manifest_path, default=None)
    if not force and isinstance(existing, dict) and existing.get("book_id") == bid:
        if existing.get("source_rev") == rev:
            # Unchanged inputs → keep the manifest (and its confirm_token) as-is, and
            # skip re-probing (byte-identical inputs → cached chapters/cover still valid).
            return existing
        # v1→v2 MIGRATION guard (must run BEFORE the re-arm branch below): if the
        # stored rev equals the LEGACY digest of the CURRENT files, the sources are
        # untouched — only the fingerprint FORMULA changed (agent update). Without
        # this, the first scan after the update would re-arm EVERY existing book
        # (a window storm). Upgrade the rev in place, preserving status /
        # confirm_token / processed_keys / everything else, and swap the notified
        # ledger key so a still-pending book does not re-nudge on migration.
        if existing.get("source_rev") == source_rev_legacy_for(mp3s, base_dir):
            old_key = _book_edge_key(existing)
            existing["source_rev"] = rev
            existing["source_rev_v"] = SOURCE_REV_VERSION
            state.write_json_atomic(manifest_path, existing)
            _notified_replace(old_key, _book_edge_key(existing))
            state.append_event(
                "source_rev_migrated", book_id=bid, rev_v=SOURCE_REV_VERSION
            )
            return existing

    # New / changed inputs. E10: make sure they are not mid-copy before arming.
    if debounce and not _files_are_stable(mp3s):
        state.append_event("book_still_copying", book_id=bid, src_dir=src_dir)
        return None

    ordered = metadata.order_chapters(_probe_book(mp3s))
    mp3s_by_name = {p.name: p for p in mp3s}
    cover_info = _resolve_cover(bid, ordered, mp3s_by_name)

    manifest = {
        "book_id": bid,
        "src_dir": src_dir,
        "status": MANIFEST_STATUS_PENDING,
        "source_rev": rev,
        "source_rev_v": SOURCE_REV_VERSION,
        "confirm_token": secrets.token_hex(16),
        "title": title,
        "author": author,
        "chapters": _build_chapters(ordered),
        "total_duration_ms": _total_duration_ms(ordered),
        # "As in source" anchor: the max sample rate across the readable mp3s, so
        # the build keeps the source SR by default (params.samplerate == None →
        # build_m4b._samplerate falls back to this) instead of resampling.
        "source_samplerate": _source_samplerate(ordered),
        "cover_state": cover_info["cover_state"],
        "cover_preview": cover_info["cover_preview"],
        "params": dict(DEFAULT_PARAMS),
        # Idempotency ledger (M0.6): idempotency_keys already built for THIS book.
        # Keys are revision-scoped (the app derives them from book_id+source_rev),
        # so a changed source_rev re-arms the book with a fresh, empty ledger —
        # a legitimately new build is allowed, a stale double-click is not.
        "processed_keys": [],
        "ts": time.time(),
    }

    # Cover CHAIN (M1): build the ordered candidate list embedded→web→generated and
    # pick a default, merged in BEFORE the single atomic write (agent stays the only
    # writer). Guarantees every book has a cover (PRD G4) even fully offline.
    manifest.update(
        cover.resolve_cover_options(manifest, do_web=_cover_web_enabled())
    )

    state.write_json_atomic(manifest_path, manifest)
    return manifest


def write_manifest_for_book(folder: Path, *, force: bool = False) -> dict | None:
    """Write / refresh the manifest for one book folder; return the manifest dict.

    Idempotent: if a manifest already exists for this ``book_id`` and its
    ``source_rev`` is unchanged, the file is left untouched (``confirm_token``
    preserved) and the existing manifest is returned. A changed ``source_rev``
    rewrites the manifest with a fresh ``confirm_token`` and re-arms
    ``status=pending-confirm``.

    ``force`` re-probes and rewrites even at an unchanged ``source_rev`` — the
    reconvert («Собрать заново») path (see :func:`_write_manifest`). It also skips
    the debounce (a reconvert targets an already-built, settled book).

    E10: if the folder's mp3s are still being copied (size/mtime not yet stable),
    a NEW/changed book is NOT armed this pass — returns ``None`` so the caller
    skips it; the next scan re-arms it once the files settle. An already-recognized
    (unchanged-rev) book is returned as-is without paying the debounce.
    """
    folder = Path(folder)
    mp3s = _list_mp3s(folder)
    ordered = metadata.order_chapters(_probe_book(mp3s)) if mp3s else []
    author, title = metadata.derive_author_title(folder, ordered)
    return _write_manifest(
        bid=book_id_for(folder),
        src_dir=os.path.abspath(str(folder)),
        mp3s=mp3s,
        base_dir=folder,
        author=author,
        title=title,
        debounce=not force,
        force=force,
    )


def combined_book_id_for(loose: list[Path]) -> str:
    """Stable ``book_id`` for the COMBINED book of a loose set (one .m4b for all).

    Derived from the same sorted-absolute-paths identity as :func:`group_id_for`
    but in a distinct namespace (``combine\\0`` prefix) so it can never collide with
    a real folder's :func:`book_id_for` (which hashes a bare path) — the combined
    book has no folder of its own. 16 hex chars, like every other ``book_id``.
    """
    canonical = sorted(os.path.abspath(os.path.expanduser(str(p))) for p in loose)
    h = hashlib.sha256()
    h.update(b"combine\0")
    h.update("\n".join(canonical).encode("utf-8"))
    return h.hexdigest()[:16]


def materialize_combined(watch: Path, loose: list[Path], *, force: bool = False) -> dict:
    """Materialize the loose set into ONE book (combine choice, D1).

    All loose mp3s become chapters of a single book (natural/track order, exactly
    like a subfolder book). Author/title come from :func:`derive_author_title`
    over the files, with the WATCH-FOLDER NAME as the fallback when the files carry
    no album/artist tags (a loose pile has no subfolder name to parse) — see the
    handler's docstring / report. ``src_dir`` is the watch dir (where the files
    live); the build reads the files by their chapter paths. Returns the manifest.

    ``force`` re-probes/rewrites even at an unchanged ``source_rev`` — the reconvert
    path for a combined book (:func:`_write_manifest`).
    """
    watch = Path(watch)
    ordered = metadata.order_chapters(_probe_book(loose))
    author, title = metadata.derive_author_title(watch, ordered)
    return _write_manifest(
        bid=combined_book_id_for(loose),
        src_dir=os.path.abspath(str(watch)),
        mp3s=loose,
        base_dir=watch,
        author=author,
        title=title,
        force=force,
    )


def materialize_separate(watch: Path, loose: list[Path], *, force: bool = False) -> list[dict]:
    """Materialize the loose set into N books — one per file (separate choice, D1).

    Each loose mp3 becomes its own single-chapter book; ``book_id`` is
    :func:`book_id_for` of the file path (consistent with the brief: "book_id =
    hash of the single path, согласовано с book_id_for"). Author/title resolve from
    that file's own tags, falling back to its filename stem. Returns the list of
    manifests in the loose-set's natural order.

    ``force`` re-probes/rewrites even at an unchanged ``source_rev`` — the reconvert
    path for a separate (single-file) book (:func:`_write_manifest`).
    """
    watch = Path(watch)
    manifests: list[dict] = []
    for f in loose:
        f = Path(f)
        probes = _probe_book([f])
        author, title = metadata.derive_author_title(watch, probes)
        # A single loose file has no album to title the book — prefer its own
        # ID3 title, else the cleaned filename, over the watch-folder fallback.
        if not (probes and probes[0].get("tags", {}).get("album")):
            title = metadata.chapter_name(probes[0]) if probes else f.stem
        manifests.append(
            _write_manifest(
                bid=book_id_for(f),
                src_dir=os.path.abspath(str(f)),
                mp3s=[f],
                base_dir=f.parent,
                author=author,
                title=title,
                force=force,
            )
        )
    return manifests


def rescan_book_manifest(manifest: dict, *, force: bool = True) -> dict | None:
    """Re-build ONE book's manifest from its CURRENT source files (reconvert path).

    «Собрать заново» must re-DISCOVER a book, not just flip its status: a book built
    by an OLD agent carries a manifest missing today's fields (e.g.
    ``source_samplerate`` — without it the confirm window's «Как в источнике · N кГц»
    hint never shows). Re-arming that manifest in place would keep the gaps; instead
    we re-run the SAME scan-build that originally created it (probe of every chapter,
    ID3 tags, ``source_samplerate``, embedded-cover detect, fresh ``source_rev`` +
    ``confirm_token``, cleared ``processed_keys``) so every field is refilled from a
    live probe. DRY: this dispatches to the very functions the scanner/materializer
    use — it does NOT re-implement probe logic.

    The book's KIND is recovered from its ``src_dir`` + ``book_id`` (the same
    identities that minted it), so the rebuild reuses the correct entry point:
      · single mp3 file  → :func:`materialize_separate`  (separate-choice book);
      · directory whose :func:`book_id_for` == the manifest's id → a subfolder book
        → :func:`write_manifest_for_book`;
      · directory whose :func:`combined_book_id_for` of its loose mp3s == the id →
        a combined-choice book → :func:`materialize_combined`.

    Returns the freshly-written manifest, or ``None`` if the source vanished / no
    mp3s remain (the caller — :func:`dispatcher._handle_reconvert` — has already
    gated on :func:`_manifest_source_alive`, so ``None`` here is only a late race;
    it is handled defensively, never crashes). ``force`` defaults True so the rebuild
    always re-probes even though the (unchanged) inputs yield the same ``source_rev``.
    """
    src = manifest.get("src_dir")
    bid = manifest.get("book_id")
    if not isinstance(src, str) or not src or not isinstance(bid, str) or not bid:
        return None
    p = Path(src)

    # Separate-choice book: src_dir is the single source mp3 itself.
    if p.is_file():
        mans = materialize_separate(p.parent, [p], force=force)
        return mans[0] if mans else None

    if not p.is_dir():
        return None  # source gone (late race after the caller's liveness gate)

    mp3s = _list_mp3s(p)
    if not mp3s:
        return None  # directory emptied out — nothing to rebuild

    # Subfolder book: the folder path hashes to the manifest's book_id.
    if book_id_for(p) == bid:
        return write_manifest_for_book(p, force=force)

    # Combined-choice book: the loose set under this watch dir hashes (combine\0
    # namespace) to the manifest's book_id.
    if combined_book_id_for(mp3s) == bid:
        return materialize_combined(p, mp3s, force=force)

    # Fallback: a directory-sourced book whose id matches neither identity (should
    # not happen for a well-formed manifest). Rebuild it as a subfolder book — the
    # most common directory kind — rather than refusing; the id stays stable because
    # book_id_for is derived from the same src_dir the app targets.
    return write_manifest_for_book(p, force=force)


def scan_watch_folder(
    watch: Path,
    previous_group: dict | None = None,
    resolved: list | None = None,
) -> tuple[list[dict], dict | None]:
    """Discover books AND any loose-mp3 group under ``watch``.

    Returns ``(manifests, pending_group)``:
      - ``manifests`` — current manifest dicts for each subfolder-book (a book = a
        direct subfolder containing ≥1 mp3, M0.2), ordered by folder name. Unchanged
        from before — subfolder books keep working exactly as they did.
      - ``pending_group`` — a :func:`_build_pending_group` dict when ≥1 mp3 lives
        loose in the watch ROOT (D1 / flows S4) AND that set has not already been
        resolved (``resolved`` ledger), else ``None``. ``previous_group`` (from the
        prior state.json) is passed through so an unchanged set keeps its ``token``.

    Returns ``([], None)`` if the watch dir does not exist yet.
    """
    watch = Path(watch)
    if not watch.is_dir():
        return [], None

    subfolders = sorted(
        (p for p in watch.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: metadata.natural_sort_key(p.name),
    )

    manifests: list[dict] = []
    for folder in subfolders:
        if not _list_mp3s(folder):
            continue  # no mp3s → not a book (yet)
        m = write_manifest_for_book(folder)
        if m is None:
            # E10: files still being copied → not armed this pass; next scan retries.
            continue
        manifests.append(m)

    loose = _list_loose_mp3s(watch)
    pending_group: dict | None = None
    if loose:
        # E10 for the grouping path (mirror of the subfolder debounce): a loose
        # set STILL BEING COPIED (iCloud partial sync, drag in flight) must not
        # arm a prompt — a half-copied set would flip ``rev`` again a moment
        # later, producing a false grouping edge (and a false app auto-raise).
        # The debounce is paid ONLY for a NEW/changed set: when group_id+rev
        # match the previous scan's group, the files were already stable at the
        # prior arm (rev covers size+mtime), so the fast path skips the wait —
        # settled prompts are not slowed, exactly like unchanged books.
        unchanged = (
            isinstance(previous_group, dict)
            and previous_group.get("group_id") == group_id_for(loose)
            and previous_group.get("rev") == group_rev_for(loose, watch)
        )
        if unchanged or _files_are_stable(loose):
            pending_group = _build_pending_group(
                watch, loose, previous=previous_group, resolved=resolved
            )
        else:
            # Still settling → keep the PRIOR prompt (if any) untouched this
            # pass; the next launchd-fired scan re-arms once the copy finishes.
            state.append_event(
                "group_still_copying",
                watch_dir=os.path.abspath(str(watch)),
                count=len(loose),
            )
            pending_group = previous_group if isinstance(previous_group, dict) else None
    return manifests, pending_group


def build_state(
    manifests: list[dict],
    watch: Path,
    pending_group: dict | None = None,
    grouping_processed: list | None = None,
    prior_progress: dict | None = None,
) -> dict:
    """Compose the lightweight ``state.json`` showcase from the manifests.

    The agent is the SINGLE writer of state. ``books`` is intentionally light
    (id/title/author/status/duration only); rich per-book data (chapters, cover)
    stays in the manifests so the showcase is not rewritten on every
    chapter-level change. ``totals`` (built/today/books) + ``engine`` (ffmpeg
    version) feed the Status stat cards (spec §5), projected from the same manifests;
    ``batch`` stays a placeholder until the real batch pipeline (M1). Title/author/
    duration are projected from the manifest so the
    app's list can show real names and length without opening each manifest.

    ``pending_groups`` is the (0..1) loose-mp3 sets awaiting a grouping decision
    (D1). It is a LIST for forward-compat (one watch root today), and is omitted
    as an empty list when there is none so the existing showcase shape is intact.

    ``grouping_processed`` is the agent-owned ledger of grouping-choice
    ``idempotency_key``s already materialized. Because :func:`run_scan` rebuilds
    state wholesale after each command drain, this ledger MUST be carried forward
    here or a duplicate choice (double-click) would re-materialize after the group
    is gone. It is capped to the most recent keys (bounded growth).

    ``prior_progress`` (Task 2) is the {book_id → progress dict} from the previous
    state.json. A book's live build ``progress`` is carried onto its row ONLY while
    that book is still ``converting`` — so a refresh during a build keeps the
    determinate bar alive, but a book that has moved on (done/error/pending) drops
    it automatically (the contract: progress only exists at status==converting).
    """
    prior_progress = prior_progress if isinstance(prior_progress, dict) else {}
    books = []
    for m in manifests:
        bid = m["book_id"]
        status = m.get("status", MANIFEST_STATUS_PENDING)
        row = {
            "book_id": bid,
            "title": m.get("title") or _title_for(Path(m["src_dir"])),
            "author": m.get("author", ""),
            "status": status,
            "total_duration_ms": m.get("total_duration_ms", 0),
            "chapters": len(m.get("chapters", [])),
        }
        # Carry the live progress only for a still-converting book (Task 2 contract:
        # progress exists ONLY at status==converting — a done/error/pending row drops
        # it). This is what lets refresh_showcase preserve a build's bar mid-encode.
        if status == STATUS_CONVERTING:
            prog = prior_progress.get(bid)
            if isinstance(prog, dict):
                row["progress"] = prog
        books.append(row)
    ledger = [k for k in (grouping_processed or []) if isinstance(k, str)]
    return {
        "schema": STATE_SCHEMA,
        # ``agent.watch_dir`` + ``agent.active``: the Status hero/footer "Активен"
        # pill (spec §5). The agent is alive whenever it writes state (this scan is
        # running), so it projects itself as active — the live-state semantics the
        # neighbor uses; a dead agent simply stops updating state.json.
        "agent": {"watch_dir": os.path.abspath(str(watch)), "active": True},
        "books": books,
        "pending_groups": [pending_group] if pending_group else [],
        # Keep only the last 256 keys — generous for any realistic session, bounded.
        "grouping_processed": ledger[-256:],
        "batch": {"active": False, "total": 0, "done": 0},
        # Status stat cards (spec §5): «Собрано» (built) / «За сегодня» (today) /
        # «ffmpeg» (engine). ``books`` is preserved for backward-compat readers.
        "totals": _project_totals(manifests, len(books)),
        "engine": engine_version(),
        "ts": time.time(),
    }


def _previous_pending_group(prev: dict | None) -> dict | None:
    """Extract the single pending group from a prior state dict (or ``None``).

    Used only to preserve a group's ``token`` across re-scans of an unchanged loose
    set (see :func:`_build_pending_group`). Defensive: any missing/odd shape → None.
    """
    if not isinstance(prev, dict):
        return None
    groups = prev.get("pending_groups")
    if isinstance(groups, list) and groups and isinstance(groups[0], dict):
        return groups[0]
    return None


def _previous_grouping_ledger(prev: dict | None) -> list:
    """Extract the carried-forward grouping idempotency ledger from a prior state."""
    if not isinstance(prev, dict):
        return []
    led = prev.get("grouping_processed")
    return [k for k in led if isinstance(k, str)] if isinstance(led, list) else []


def _previous_book_progress(prev: dict | None) -> dict:
    """Map {book_id → progress dict} from a prior state's book rows (Task 2).

    The dispatcher writes a live build's ``progress`` onto the converting book's
    showcase row; a wholesale re-projection (run_scan / refresh_showcase) would
    drop it unless we carry it forward. We harvest every row that still has a
    ``progress`` dict so :func:`build_state` can re-attach it — but only for rows
    that are STILL converting (build_state gates on status), so a finished build's
    stale progress never lingers. Defensive against any odd shape.
    """
    if not isinstance(prev, dict):
        return {}
    books = prev.get("books")
    if not isinstance(books, list):
        return {}
    out: dict = {}
    for b in books:
        if not isinstance(b, dict):
            continue
        bid = b.get("book_id")
        prog = b.get("progress")
        if isinstance(bid, str) and bid and isinstance(prog, dict):
            out[bid] = prog
    return out


def _manifest_source_alive(manifest: dict) -> bool:
    """True if a manifest's source still exists on disk (else it's stale → drop).

    Matches the cleanup semantics the subfolder scanner already has implicitly (a
    removed folder stops being scanned → drops from the showcase), applied uniformly
    so grouping-materialized books are projected on the SAME rule:
      - subfolder / combined book → ``src_dir`` is a directory that still exists;
      - separate book → ``src_dir`` is the single mp3 file that still exists.
    A vanished source drops the book regardless of status (exactly as a deleted
    folder did before this helper existed). A directory that exists but no longer
    holds any mp3 counts as gone too — that is precisely the scanner's own gate
    (``if not _list_mp3s(folder): continue``), so behavior is unchanged for
    subfolder books.
    """
    src = manifest.get("src_dir")
    if not isinstance(src, str) or not src:
        return False
    p = Path(src)
    if p.is_dir():
        return bool(_list_mp3s(p))
    return p.is_file()


def _collect_showcase_manifests(scanned: list[dict]) -> list[dict]:
    """All manifests to project into the showcase: scanned subfolder books PLUS any
    other live on-disk manifest (grouping-materialized combined/separate books).

    Starts from the freshly-scanned subfolder manifests (so a just-changed
    ``source_rev`` is reflected), then folds in every other manifest file in
    ``queue/books/`` whose source is still alive (:func:`_manifest_source_alive`) and
    whose ``book_id`` is not already present. Ordered: scanned first (folder-name
    order), then the rest by book_id for determinism.
    """
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for m in scanned:
        bid = m.get("book_id")
        if isinstance(bid, str) and bid and bid not in by_id:
            by_id[bid] = m
            order.append(bid)

    books_dir = config.books_dir()
    if books_dir.is_dir():
        for path in sorted(books_dir.glob("*.json")):
            m = state.read_json(path, default=None)
            if not isinstance(m, dict):
                continue
            bid = m.get("book_id")
            if not (isinstance(bid, str) and bid) or bid in by_id:
                continue
            if _manifest_source_alive(m):
                by_id[bid] = m
                order.append(bid)
    return [by_id[b] for b in order]


# --- presence ledger (move-out → move-in re-drop signal) -----------------------
#
# The inode signal (source_rev v2) catches every drop that creates NEW files —
# a Finder copy, a drop from another volume/Mac. It cannot see a same-volume
# Finder MOVE: rename keeps st_ino/st_dev/mtime, so a book moved OUT of the
# watched folder and later moved BACK IN has an identical rev and would dedup
# silently as done. The presence ledger closes that gap: the agent records, per
# book_id, whether the book's source was alive on the last scan; a book that is
# present NOW but was marked ABSENT before (known → vanished → reappeared) is a
# conscious re-drop → re-arm pending-confirm even though the rev matched.
# A book that simply sits present→present is never touched (0 raises at login).

# Bounded ledger size — generous for any realistic queue, prevents unbounded
# growth from books whose folders were deleted forever.
_PRESENCE_MAX_ENTRIES = 512


def _reconcile_presence(manifests: list[dict]) -> list[dict]:
    """Update presence.json from the live manifests; re-arm reappeared done books.

    Called ONLY from :func:`run_scan` (never :func:`refresh_showcase` — a refresh
    runs mid-build and must not re-arm anything). Rules, per showcase manifest:

      - book known + marked ABSENT on a prior scan + present now + status in
        ``REDROP_REARM_STATUSES`` → a move-out/move-in re-drop: rebuild the
        manifest via :func:`rescan_book_manifest` (``force=True`` — fresh
        ``confirm_token``, cleared ``processed_keys``, same machinery as
        reconvert) so the book re-arms ``pending-confirm`` and the nudge layer
        raises the app once;
      - book present and previously present (or brand new to the ledger) → just
        record it present. Seeding is organic: the first scan after the agent
        update writes every lying book as present WITHOUT re-arming (no window
        storm);
      - status pending/converting/error → never re-armed here (pending already
        has its window; converting is the anti-loop guarantee; error keeps its
        honest state) — only ``done``/``skipped`` encode "the user is FINISHED
        with this book", which is exactly what a re-drop reverses.

    Every ledger entry not present in this scan is flipped to absent (that is the
    edge the next reappearance fires on). Returns the manifest list with any
    re-armed manifests substituted in. Defensive throughout; the ledger is
    agent-owned and written atomically.
    """
    ledger = state.read_json(config.presence_file(), default=None)
    raw = ledger.get("books") if isinstance(ledger, dict) else None
    entries: dict[str, dict] = (
        {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)}
        if isinstance(raw, dict)
        else {}
    )
    now = time.time()
    present_ids: set[str] = set()
    out: list[dict] = []
    for m in manifests:
        bid = m.get("book_id")
        if not (isinstance(bid, str) and bid):
            out.append(m)
            continue
        present_ids.add(bid)
        prev_entry = entries.get(bid)
        was_absent = isinstance(prev_entry, dict) and prev_entry.get("present") is False
        if was_absent and m.get("status") in REDROP_REARM_STATUSES:
            fresh = rescan_book_manifest(m, force=True)
            if isinstance(fresh, dict):
                state.append_event(
                    "book_rearmed_reappeared", book_id=bid,
                    src_dir=str(m.get("src_dir") or ""),
                )
                m = fresh
        out.append(m)
        entries[bid] = {
            "present": True,
            "src_dir": str(m.get("src_dir") or ""),
            "ts": now,
        }
    for bid, e in list(entries.items()):
        if bid not in present_ids and e.get("present"):
            entries[bid] = {**e, "present": False, "ts": now}
    if len(entries) > _PRESENCE_MAX_ENTRIES:
        # Prune the oldest ABSENT entries first (present books are always kept).
        absent = sorted(
            (b for b, e in entries.items() if not e.get("present")),
            key=lambda b: entries[b].get("ts", 0),
        )
        for bid in absent[: len(entries) - _PRESENCE_MAX_ENTRIES]:
            entries.pop(bid, None)
    state.write_json_atomic(
        config.presence_file(), {"schema": 1, "books": entries, "ts": now}
    )
    return out


def record_grouping_resolved(key: str) -> None:
    """Append a resolved grouping ``idempotency_key`` to the state ledger (atomic).

    Read-modify-write on ``state.json`` (the agent is the single writer, and the
    dispatcher calls this serially), keeping every other field intact. Idempotent:
    a key already present is not duplicated. The ledger is what makes a double-click
    choice a no-op AND stops the scanner from re-prompting for loose files that
    linger in the root after materialization (see :func:`grouping_idempotency_key`).
    Called BEFORE the drain's closing :func:`run_scan`, which carries it forward.
    """
    if not key:
        return
    cur = state.read_state(default=None)
    cur = cur if isinstance(cur, dict) else {}
    led = cur.get("grouping_processed")
    led = [k for k in led if isinstance(k, str)] if isinstance(led, list) else []
    if key not in led:
        led.append(key)
    cur["grouping_processed"] = led[-256:]
    state.write_state(cur)


# --- app auto-raise (nudge) ---------------------------------------------------
#
# When a NEW pending-confirm book (or a new loose-mp3 grouping prompt) appears,
# the agent brings the app forward so the confirm window can surface even though
# the app was closed (the launchd agent is headless; the GUI has no other wake-up).
# Rising-edge detection is an EXPLICIT ledger (config.notified_file()): on every
# run_scan publication we collect the keys of the CURRENTLY pending books/groups,
# nudge once iff any key is not in the ledger, and rewrite the ledger to exactly
# the current set (pruning keys whose book/group moved on). That construction
# breaks the loop by design: a confirmed book leaves pending → its key is pruned →
# the post-drain run_scan never re-nudges; an unchanged pending book keeps its key
# → repeated scans are silent; a reconvert re-arms with a fresh confirm_token →
# a NEW key → one legitimate re-notification.

# Test seam: overrides the launch command (shlex-split). The self-checks point it
# at a recorder script so no real app is ever opened by a test run.
_NUDGE_CMD_ENV = "MP3TOM4B_NUDGE_CMD"
# How long we give /usr/bin/open before giving up (never blocks the scan for long).
_NUDGE_TIMEOUT_S = 2.0


def _notified_read() -> set[str]:
    """The ledger's current key set; empty on missing/malformed file (defensive)."""
    data = state.read_json(config.notified_file(), default=None)
    keys = data.get("keys") if isinstance(data, dict) else data
    if not isinstance(keys, list):
        return set()
    return {k for k in keys if isinstance(k, str)}


def _notified_write(keys: set[str]) -> None:
    """Rewrite the ledger to exactly ``keys`` (atomic tmp→rename, agent-owned)."""
    state.write_json_atomic(
        config.notified_file(), {"keys": sorted(keys), "ts": time.time()}
    )


def _notified_replace(old_key: str, new_key: str) -> None:
    """Swap one ledger key in place (v1→v2 rev migration, no re-nudge).

    A silent rev upgrade changes a PENDING book's/group's edge key (the key
    embeds ``rev[:16]``) while nothing user-visible happened. If the OLD key is
    in the ledger (the app was already raised for this edge), carry the "seen"
    mark over to the NEW key so the next publication does not re-nudge. If the
    old key is absent (never nudged — e.g. suppressed test tree), do nothing:
    the edge is legitimately still new. Never raises beyond the atomic write.
    """
    keys = _notified_read()
    if old_key in keys:
        keys.discard(old_key)
        keys.add(new_key)
        _notified_write(keys)


def _book_edge_key(manifest: dict) -> str:
    """Ledger key of a book manifest: ``book:<id>:<rev[:16]>:<token[:16]>``."""
    bid = str(manifest.get("book_id") or "")
    rev = str(manifest.get("source_rev") or "")[:16]
    token = str(manifest.get("confirm_token") or "")[:16]
    return f"book:{bid}:{rev}:{token}"


def _group_edge_key(group: dict) -> str:
    """Ledger key of a pending group: ``group:<gid>:<rev[:16]>:<token[:16]>``."""
    gid = str(group.get("group_id") or "")
    rev = str(group.get("rev") or "")[:16]
    token = str(group.get("token") or "")[:16]
    return f"group:{gid}:{rev}:{token}"


def _edge_keys(showcase: dict) -> set[str]:
    """Ledger keys of every CURRENTLY pending confirm/grouping edge in ``showcase``.

    Shapes (rev/token are NOT in state.json, so books read their manifest):
      - ``book:<book_id>:<source_rev[:16]>:<confirm_token[:16]>`` — for each
        showcase book row at ``pending-confirm``, rev/token from
        ``queue/books/<book_id>.json``;
      - ``group:<group_id>:<rev[:16]>:<token[:16]>`` — for each pending group
        (the showcase group dict IS :func:`_build_pending_group`'s output and
        carries rev/token itself).
    Missing/malformed fields degrade to empty segments — the key still functions
    as an appearance edge for that book/group. Never raises.
    """
    keys: set[str] = set()
    books = showcase.get("books")
    if isinstance(books, list):
        for row in books:
            if not isinstance(row, dict):
                continue
            if row.get("status") != MANIFEST_STATUS_PENDING:
                continue
            bid = row.get("book_id")
            if not (isinstance(bid, str) and bid):
                continue
            man = state.read_json(config.books_dir() / f"{bid}.json", default=None)
            man = man if isinstance(man, dict) else {}
            man.setdefault("book_id", bid)
            keys.add(_book_edge_key(man))
    groups = showcase.get("pending_groups")
    if isinstance(groups, list):
        for g in groups:
            if not isinstance(g, dict):
                continue
            gid = g.get("group_id")
            if not (isinstance(gid, str) and gid):
                continue
            keys.add(_group_edge_key(g))
    return keys


def _nudge_command() -> list[str] | None:
    """Resolve the app-raise command; ``None`` = stay silent (test suppression).

    Order:
      1. ``MP3TOM4B_NUDGE_CMD`` set → shlex-split it (test seam / recorder).
      2. ``MP3TOM4B_SUPPORT_DIR`` set WITHOUT a nudge cmd → ``None``. Every
         self-check runs in a scratch support tree, so this one rule keeps the
         whole existing suite from popping the real app; a production install
         does not set the override (installer.sh only propagates it for
         test-mode installs), so the real agent still nudges.
      3. Default: ``/usr/bin/open -b <BUNDLE_ID>`` — absolute path (launchd's
         PATH is trimmed), no ``-g``/``-j`` (the whole point is foreground).
    """
    raw = os.environ.get(_NUDGE_CMD_ENV)
    if raw:
        try:
            cmd = shlex.split(raw)
        except ValueError:
            return None
        return cmd or None
    if os.environ.get("MP3TOM4B_SUPPORT_DIR"):
        return None
    return ["/usr/bin/open", "-b", config.BUNDLE_ID]


def _nudge_app(new_keys: list[str]) -> None:
    """Bring the app forward ONCE (one publication = at most one launch).

    Best-effort by contract: a missing app, a hung LaunchServices or a bogus
    override is journalled (``app_nudged`` / ``app_nudge_failed``) and never
    raises — a failed nudge must never take the agent down with it.
    """
    cmd = _nudge_command()
    if cmd is None:
        return
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_NUDGE_TIMEOUT_S,
            check=False,
        )
    except Exception as exc:  # timeout, missing binary, anything — journal only
        state.append_event(
            "app_nudge_failed", error=repr(exc)[:200], keys=new_keys[:8]
        )
        return
    if proc.returncode == 0:
        state.append_event("app_nudged", keys=new_keys[:8])
    else:
        state.append_event(
            "app_nudge_failed", rc=proc.returncode, keys=new_keys[:8]
        )


def _publish_showcase_and_maybe_open(showcase: dict) -> None:
    """Single publication point for :func:`run_scan`: write state, maybe raise app.

    Writes ``state.json`` FIRST (so a cold-started app reads the fresh showcase),
    then reconciles the notified ledger: ``new = current − ledger``, ledger :=
    current (pruning edges that moved on), and fires ONE nudge iff ``new`` is
    non-empty. Everything after the state write is wrapped defensively — the
    nudge machinery can never break the scan contract.
    """
    state.write_state(showcase)
    try:
        current = _edge_keys(showcase)
        seen = _notified_read()
        new = current - seen
        _notified_write(current)
        if new:
            _nudge_app(sorted(new))
    except Exception as exc:  # defensive: publication must survive any nudge bug
        try:
            state.append_event("app_nudge_failed", error=repr(exc)[:200])
        except Exception:
            pass


def run_scan(watch: Path | None = None) -> dict:
    """Full scan pass: ensure manifests, project any loose group, write the showcase.

    Returns the showcase dict that was written. Safe to call repeatedly
    (idempotent per book and per group; tokens are preserved across unchanged
    re-scans — see :func:`write_manifest_for_book` / :func:`_build_pending_group`).
    The grouping idempotency ledger is read from the prior state and carried
    forward so a double-click choice cannot re-materialize after its group is gone.
    """
    config.ensure_data_dirs()
    target = Path(watch) if watch is not None else watch_dir()
    prev = state.read_state(default=None)
    previous_group = _previous_pending_group(prev)
    ledger = _previous_grouping_ledger(prev)
    prior_progress = _previous_book_progress(prev)
    manifests, pending_group = scan_watch_folder(
        target, previous_group=previous_group, resolved=ledger
    )
    # Project subfolder books PLUS grouping-materialized books (combined/separate),
    # which live in queue/books/ but have no subfolder for the scanner to find.
    showcase_manifests = _collect_showcase_manifests(manifests)
    # Presence reconcile (move-out → move-in re-drop signal): re-arms a done book
    # that vanished on a prior scan and is back now, and refreshes presence.json.
    # Defensive: a presence bug must never break the scan/publication contract.
    try:
        showcase_manifests = _reconcile_presence(showcase_manifests)
    except Exception as exc:
        try:
            state.append_event("presence_reconcile_failed", error=repr(exc)[:200])
        except Exception:
            pass
    showcase = build_state(
        showcase_manifests, target, pending_group=pending_group,
        grouping_processed=ledger, prior_progress=prior_progress,
    )
    _publish_showcase_and_maybe_open(showcase)
    return showcase


def refresh_showcase(watch: Path | None = None) -> dict:
    """Re-project ``state.json`` from the CURRENT on-disk manifests, no folder re-scan.

    The dispatcher calls this on every build status transition (pending-confirm →
    converting → done/error) so the showcase the app/queue reads reflects a book's
    live status the instant it changes — not only on the next ``run_scan`` after the
    drain. Without it a multi-minute ``converting`` build is invisible to the В РАБОТЕ
    section (it would jump pending-confirm → done), which is exactly the projection
    bug this fixes. The agent stays the SINGLE writer of state.

    Differs from :func:`run_scan` deliberately: it does NOT walk the watch folder
    (no re-probe, no re-arm of ``source_rev``/``confirm_token`` mid-build — that
    could rotate a token while a build is in flight). It only re-reads the manifests
    already on disk (the caller persisted the new status atomically just before) and
    re-runs the SAME projection :func:`run_scan` uses (:func:`_collect_showcase_manifests`
    → :func:`build_state`).

    Crucially it PRESERVES the two non-manifest fields the scan owns from the
    current ``state.json`` — ``pending_groups`` (the live loose-mp3 group dict) and
    ``grouping_processed`` (the resolved-choice ledger) — so a refresh during a build
    can never drop a pending grouping prompt or re-open an already-resolved one.
    Returns the showcase dict written.
    """
    config.ensure_data_dirs()
    target = Path(watch) if watch is not None else watch_dir()
    prev = state.read_state(default=None)
    # Preserve the scan-owned fields verbatim (a refresh must not touch grouping).
    pending_group = _previous_pending_group(prev)
    ledger = _previous_grouping_ledger(prev)
    # Carry the live build progress forward so a refresh mid-encode keeps the
    # determinate bar (Task 2); build_state attaches it only to still-converting rows.
    prior_progress = _previous_book_progress(prev)
    # Project from the manifests already on disk (no folder walk → no re-arm).
    showcase_manifests = _collect_showcase_manifests([])
    showcase = build_state(
        showcase_manifests, target, pending_group=pending_group,
        grouping_processed=ledger, prior_progress=prior_progress,
    )
    state.write_state(showcase)
    return showcase
