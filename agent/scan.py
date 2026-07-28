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

★ EARLY NUDGE (D17). A manifest is no longer born complete, because the window
has to be on screen in ~0.8 s and one ffprobe pass over a 56-file book takes
longer than that on its own. So a scan publishes TWICE: a ``phase=skeleton``
manifest built from the ``stat`` walk we already paid for (chapter names from
filenames, author/title from the folder, count, bytes) goes out first and raises
the app, and the probe + cover chain fill it in to ``phase=ready`` while the
human is already looking at the book. Three rules hold it together:

  · ``confirm_token`` is minted ONCE, on the skeleton, and re-used by every later
    phase. The notified-ledger key embeds it, so both publications are the same
    edge and the book nudges exactly once (I2);
  · ``build_token`` is created ONLY in the final atomic write. It does not exist
    while the manifest is incomplete, which is what makes a confirm command born
    on a skeleton un-buildable even after the manifest is finished (I1 — see
    :func:`agent.dispatcher._not_ready_reason` for the TOCTOU this closes);
  · the unchanged-``source_rev`` short-circuit is phase-aware. A skeleton carries
    its FINAL rev, so a rev-only short-circuit would hand it back forever and the
    window would stay half-empty; ``skeleton``/``chapters`` therefore mean RESUME
    (I4), which doubles as recovery from a crash between phases.

A manifest with NO ``phase`` field was written before D17, is complete by
construction, and reads as ``done`` (:func:`manifest_phase`) — no migration.

Publication order is fixed and one-directional: **manifest → state.json →
ledger → nudge**. State must never announce a book whose manifest is not yet on
disk, and the nudge is last so nothing the app is woken up to look at is still
in flight when it looks.

The scanner NEVER triggers a build — that lives only in the ``confirm-build``
handler (structural guarantee I2).

M4 adds the ACCESS GATE in front of all of it (plan v2 §M4 / Р3, addendum §4):
:func:`probe_watch_dir_access` answers — behind a watchdog, so it can never hang —
whether the folder is readable at all, and :func:`run_scan` refuses to touch
anything when it is not. See the block above :func:`probe_watch_dir_access` for
why the answer has FOUR values and why `blocked` is not a synonym for `denied`.
"""

from __future__ import annotations

import datetime
import errno
import hashlib
import os
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from . import config, cover, metadata, probe, shutdown, state

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

# ── Manifest PHASE (D17 «ранний нудж») ───────────────────────────────────────
# A manifest is no longer born complete. The window has to open in ~0.8 s, which
# is less than one ffprobe pass over a 56-file book, so the agent publishes a
# SKELETON first and fills it in while the human is already looking at it. The
# phase field says which of those states the file on disk is in:
#
#   skeleton → everything that a plain ``stat`` pass already paid for: chapter
#              names from FILE NAMES, author/title from the folder name, file
#              count, byte size. No ffprobe has run. NOT buildable.
#   chapters → ffprobe done: real chapter names/durations from ID3, total length,
#              source sample rate. Cover chain not resolved yet. NOT buildable.
#   ready    → the final atomic write: cover options resolved and ``build_token``
#              minted. Buildable.
#   done     → terminal. Also the COMPAT DEFAULT: a manifest with NO phase field
#              was written by a pre-D17 agent, is complete by construction, and
#              must keep working untouched — hence :func:`manifest_phase`.
#
# A boolean «готово» was considered and rejected: it cannot tell "still reading"
# from "there is nothing left to wait for", which is exactly the distinction the
# app needs to decide between a spinner and an enabled button.
MANIFEST_PHASE_SKELETON = "skeleton"
MANIFEST_PHASE_CHAPTERS = "chapters"
MANIFEST_PHASE_READY = "ready"
MANIFEST_PHASE_DONE = "done"
#: Phases whose manifest the agent still owes work on. These — and ONLY these —
#: make the unchanged-``source_rev`` short-circuit in :func:`_begin_manifest` do
#: work instead of returning early (I4: a skeleton must never freeze).
INCOMPLETE_PHASES = (MANIFEST_PHASE_SKELETON, MANIFEST_PHASE_CHAPTERS)

# ── Cover WEB leg — a state of its own, deliberately NOT a phase (D17/M-B) ────
# ``phase`` answers "may this book be built"; the web lookup must never be part of
# that answer, or «Собрать» is back to waiting for the network. So the web leg
# carries its own field, read by nothing that gates the build:
#
#   pending → ready manifest, local covers in place, web lookup still owed;
#   done    → the lookup ran (found something, or gave up after
#             ``COVER_WEB_MAX_TRIES``). Nothing further is owed.
#
# A MISSING field means "nothing owed" — pre-M-B manifests already have their web
# candidates inline, exactly the same compat rule ``phase`` uses.
COVER_WEB_PENDING = "pending"
COVER_WEB_DONE = "done"
#: How many passes may try a book whose search came back empty. Zero results is
#: ambiguous — no network vs. an obscure book nobody has a cover for — so we retry
#: a couple of ticks (the laptop that woke up before the wifi did is the case worth
#: covering) and then stop, rather than re-asking two search engines forever.
COVER_WEB_MAX_TRIES = 3

# Test seam (M-D): halt the fill-in right after the named phase, leaving the
# manifest on disk exactly as a crashed process would have. SAFE BY DEFAULT — the
# variable is unset in production and only the two literal phase names are
# honoured; anything else means "no halt".
_HALT_AFTER_PHASE_ENV = "MP3TOM4B_HALT_AFTER_PHASE"

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


# ── Cover-web ENRICHMENT budget for one pass (D17/M-B) ───────────────────────
# What this budget is NOT, any more. It was born (M-A) as a stopgap on the
# CRITICAL path: the web lookup sat inside the scan, the phase watchdog
# (``agent.__main__.PHASE_DEADLINE_S`` = 150 s) covers «probe + scan + publish»,
# and when it fires it publishes ``folder_access = blocked`` — the card that tells
# the human macOS is holding the folder hostage. A handful of offline books, at
# ``cover.WEB_TIMEOUT_S`` per request, could sum past 150 s and put a FALSE
# folder-permission card in front of a human whose folder is fine. We spent half a
# year on real TCC problems; a false one is expensive in a way a missing web cover
# never is.
#
# M-B removed that danger STRUCTURALLY rather than by clock: the web leg no longer
# runs inside the scan at all, so it is not under the watchdog and cannot influence
# ``folder_access`` — see :func:`enrich_covers_web` (invariant I6). What remains is
# a different, smaller cost that still deserves a bound: launchd will not start a
# second instance of our label, so every second the enrichment pass keeps this
# process alive is a second the NEXT tick — a newly dropped book, a queued
# «Собрать» — has to wait. Hence a whole-pass ceiling: when it is spent, the
# remaining books simply stay ``cover_web = pending`` and a later tick picks them
# up (generation already guaranteed them a cover — PRD G4).
#
# 45 s: comfortably more than the ~12 s a fully dead network costs for one book
# (two sources × one timed-out request each), so a single offline book never
# starves the rest; and small enough that a queued command is never held back by
# a noticeable amount. The pass ALSO yields early when a command is waiting.
COVER_WEB_BUDGET_S = 45.0
_COVER_WEB_BUDGET_ENV = "MP3TOM4B_COVER_WEB_BUDGET_S"
#: Seconds charged to the budget so far in THIS process's enrichment pass.
_cover_web_spent_s = 0.0


def _cover_web_budget_s() -> float:
    """Whole-pass web budget in seconds (``MP3TOM4B_COVER_WEB_BUDGET_S`` overrides)."""
    raw = os.environ.get(_COVER_WEB_BUDGET_ENV)
    if raw is None:
        return COVER_WEB_BUDGET_S
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return COVER_WEB_BUDGET_S
    return val if val >= 0 else COVER_WEB_BUDGET_S


def _reset_cover_web_budget() -> None:
    """Start a fresh budget (called once per enrichment pass)."""
    global _cover_web_spent_s
    _cover_web_spent_s = 0.0


def _cover_web_allowed() -> bool:
    """Whether the pass may still start ANOTHER book's web lookup."""
    return _cover_web_enabled() and _cover_web_spent_s < _cover_web_budget_s()


def _charge_cover_web(seconds: float) -> None:
    """Charge one book's web lookup to the pass budget.

    Honest now: the enrichment pass does nothing but the network, so this is the
    time actually spent waiting on it (it used to also include the concurrent
    Pillow render). Checked BETWEEN books, never mid-book — one book's lookup is
    already bounded by :data:`cover.WEB_TIMEOUT_S` per request.
    """
    global _cover_web_spent_s
    _cover_web_spent_s += max(0.0, seconds)


def watch_dir() -> Path:
    """Folder the agent watches for incoming books.

    Honors ``MP3TOM4B_WATCH_DIR`` (tests / dev) and defaults to
    ``~/Desktop/mp3-to-m4b`` (plans.md M0.2).
    """
    override = os.environ.get("MP3TOM4B_WATCH_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / "Desktop" / config.APP_NAME


# ─────────────────────────────────────────────────────────────────────────────
# WATCH-FOLDER ACCESS PROBE (M4 · plan v2 §M4 / Р3 · addendum §4.1–4.4)
#
# The agent may not touch the watched folder until it knows it CAN. Four values,
# and merging any two of them costs the user real time (addendum §4.1):
#
#   ok       the probe listed the folder — normal tick.
#   denied   TCC has a "no" on record (the user pressed «Не разрешать») or plain
#            chmod/ACL. EPERM/EACCES are merged ON PURPOSE so a chmod test behaves
#            like the real TCC refusal. The refusal is instant (~200 ms).
#   missing  the folder is gone (ENOENT/ENOTDIR). Transient by default — see
#            :data:`MISSING_TRANSIENT_S`.
#   blocked  NO DECISION EXISTS. macOS is holding the call open because it wants
#            to ask the human, and a background LaunchAgent cannot show that
#            dialog. Measured 2026-07-25 on this machine: with the frozen Mach-O
#            helper as PA0 and no grant yet, ``os.listdir`` did not return at all
#            (>60 s, stack parked in ``__open_nocancel``, tccd silent), while the
#            very same call under ``/bin/bash`` (a PLATFORM binary) was denied in
#            ~200 ms. The subject is the difference: a platform binary is refused
#            silently, an attributable one is treated as a promptable client.
#
# Why `blocked` may never be folded into `denied`: in `denied` we send the human
# to System Settings; in `blocked` the right move is to look at the screen and
# press «Разрешить». Each remedy is useless — and misleading — for the other case.
#
# Why the watchdog is not optional: launchd never starts a second instance of the
# same label. One wedged run means `folder_access` is never published, the access
# card never appears, «Проверить снова» does nothing, and the product is dead
# until reboot. So the syscall runs on a DAEMON thread we never join (it is parked
# in the kernel and cannot be woken), the main thread waits on a DEADLINE, and a
# timeout is an answer — the third state — not a hang.
# ─────────────────────────────────────────────────────────────────────────────

ACCESS_OK = "ok"
ACCESS_DENIED = "denied"
ACCESS_MISSING = "missing"
ACCESS_BLOCKED = "blocked"
#: The exact strings published in ``state.agent.folder_access``. The Swift side
#: (``app/StateModel.swift``) decodes these four explicitly and carries anything
#: else as ``.unknown`` — never silently as "fine".
ACCESS_VALUES = (ACCESS_OK, ACCESS_DENIED, ACCESS_MISSING, ACCESS_BLOCKED)

#: Phase 1 deadline. Generous next to the measured extremes (ok ≈ 5 ms, denied
#: ≈ 200 ms) and short enough that a blocked verdict reaches the app while the
#: system dialog is still on screen — the human must be able to connect the two.
PROBE_FAST_S = 2.0
_PROBE_FAST_ENV = "MP3TOM4B_PROBE_FAST_S"

#: Phase 2 «consent window»: how long we keep the wedged syscall alive hoping the
#: human presses «Разрешить», so the answer lands in THIS tick instead of the next
#: one. Entered ONLY when the process has nothing else to do (addendum §4.2 rule 3)
#: — it must never hold the single job instance hostage while work is queued.
CONSENT_WINDOW_S = 90.0
_CONSENT_WINDOW_ENV = "MP3TOM4B_CONSENT_WINDOW_S"

#: `missing` is transient (plan v2 M8f): a folder that "vanished" for one tick is
#: far more often a rename/sync blip than a real deletion, and treating it as real
#: would flip the whole library to absent — which the NEXT good scan reads as
#: "everything was just re-dropped" and re-arms. Destructive reconcile is allowed
#: only after this many consecutive missing scans AND this much elapsed time.
MISSING_TRANSIENT_MIN_SCANS = 2
MISSING_TRANSIENT_S = 600.0

#: Probes whose syscall never came back in this process. They are daemon threads,
#: so the interpreter will not wait for them — but ``__main__`` still leaves via
#: ``os._exit`` when one is registered here, because a thread parked in the kernel
#: is exactly the thing that must never get a vote on whether we exit.
_WEDGED_PROBES: list[_AccessProbe] = []


def _probe_fast_s() -> float:
    """Phase-1 deadline in seconds (``MP3TOM4B_PROBE_FAST_S`` overrides)."""
    raw = os.environ.get(_PROBE_FAST_ENV)
    if raw is None:
        return PROBE_FAST_S
    try:
        val = float(raw)
    except ValueError:
        return PROBE_FAST_S
    return val if val > 0 else PROBE_FAST_S


def _consent_window_s() -> float:
    """Phase-2 window in seconds; ``0`` disables it (``MP3TOM4B_CONSENT_WINDOW_S``)."""
    raw = os.environ.get(_CONSENT_WINDOW_ENV)
    if raw is None:
        return CONSENT_WINDOW_S
    try:
        val = float(raw)
    except ValueError:
        return CONSENT_WINDOW_S
    return max(0.0, val)


class _AccessProbe:
    """One ``os.listdir`` of the watched folder, running behind a deadline.

    The syscall goes on a daemon thread and is NEVER joined: when TCC parks it in
    ``open()`` there is no way to wake it, and a probe that wedges its own process
    would take the whole agent down — the exact failure this class exists to
    prevent. The main thread only ever waits on an :class:`threading.Event` with a
    timeout, so it always gets an answer.

    The worker publishes into ``_box`` and sets ``_done`` LAST, so a late wake-up
    can never race a verdict that was already reported (the reader stops looking
    at the box the moment the deadline passes).
    """

    def __init__(self, watch: Path) -> None:
        self._watch = Path(watch)
        self._box: dict = {"access": ACCESS_MISSING, "errno": None, "error": None}
        self._done = threading.Event()
        self._t0 = time.monotonic()
        self._thread: threading.Thread | None = None

    # -- the syscall ---------------------------------------------------------
    def _run(self) -> None:
        try:
            # os.listdir is EAGER: permission/absence is raised HERE, not lazily on
            # iteration (unlike an un-consumed scandir), so one call classifies the
            # directory completely. Read-only by construction — a probe must never
            # be the thing that creates or writes anything.
            os.listdir(str(self._watch))
            self._box["access"] = ACCESS_OK
        except PermissionError as exc:
            self._box.update(access=ACCESS_DENIED, errno=exc.errno,
                             error=f"{exc.strerror} (errno={exc.errno})")
        except FileNotFoundError as exc:
            self._box.update(access=ACCESS_MISSING, errno=exc.errno,
                             error=f"{exc.strerror} (errno={exc.errno})")
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                access = ACCESS_DENIED
            else:
                # ENOTDIR / ELOOP / EIO / a gone volume: not a refusal, and we are
                # not going to invent a fifth value for the app to render. They all
                # mean "there is no readable folder here", which is `missing` —
                # and `missing` is transient, so a weird errno cannot destroy the
                # library on one tick.
                access = ACCESS_MISSING
            self._box.update(access=access, errno=exc.errno,
                             error=f"{exc.strerror} (errno={exc.errno})")
        except BaseException as exc:  # noqa: BLE001 - a bug must not read as `blocked`
            self._box.update(access=ACCESS_MISSING, errno=None,
                             error=f"{type(exc).__name__}: {exc}"[:200])
        finally:
            self._done.set()

    # -- lifecycle -----------------------------------------------------------
    def start(self) -> _AccessProbe:
        self._t0 = time.monotonic()
        self._thread = threading.Thread(
            target=self._run, name="mp3tom4b-access-probe", daemon=True
        )
        self._thread.start()
        return self

    def wait(self, seconds: float) -> str | None:
        """Wait up to ``seconds`` for the verdict; ``None`` = still no decision."""
        answered = self._done.wait(seconds)
        return self._box["access"] if answered else None

    @property
    def wedged(self) -> bool:
        """True while the syscall has not returned (it may never)."""
        return not self._done.is_set()

    @property
    def elapsed_s(self) -> float:
        return round(time.monotonic() - self._t0, 3)

    @property
    def detail(self) -> str:
        return str(self._box.get("error") or "")


def _start_access_probe(watch: Path) -> _AccessProbe:
    return _AccessProbe(watch).start()


def _verdict(probe_handle: _AccessProbe, deadline_s: float, watch: Path) -> str:
    """Phase 1: an answer within ``deadline_s``, or the third state.

    A timeout registers the probe as wedged (``__main__`` reads that to decide how
    to leave the process) and is journalled — a `blocked` run must be diagnosable
    afterwards, because by definition nobody was watching when it happened.
    """
    access = probe_handle.wait(deadline_s)
    if access is None:
        _WEDGED_PROBES.append(probe_handle)
        access = ACCESS_BLOCKED
    if access != ACCESS_OK:
        try:
            state.append_event(
                "folder_access_probe",
                access=access,
                elapsed_s=probe_handle.elapsed_s,
                deadline_s=deadline_s,
                watch_dir=os.path.abspath(str(watch)),
                detail=probe_handle.detail[:200],
            )
        except Exception:  # noqa: BLE001 - diagnostics never break the gate
            pass
    return access


def probe_watch_dir_access(watch: Path | None = None,
                           fast_s: float | None = None) -> str:
    """Can the agent read the watched folder RIGHT NOW? One of :data:`ACCESS_VALUES`.

    Phase 1 only (bounded, always answers). The phase-2 consent window is driven by
    :func:`run_scan`, which owns the "is this process idle enough to wait?" call.
    """
    target = Path(watch) if watch is not None else watch_dir()
    deadline = _probe_fast_s() if fast_s is None else float(fast_s)
    return _verdict(_start_access_probe(target), deadline, target)


def probe_thread_wedged() -> bool:
    """True if any probe in this process is still parked in the kernel.

    ``agent/__main__`` uses this to choose ``os._exit`` over a normal return: the
    thread cannot be joined, cannot be signalled and will never run a finalizer.
    """
    return any(p.wedged for p in _WEDGED_PROBES)


def _iso_utc(epoch: float) -> str:
    """ISO-8601 UTC with sub-second precision and a trailing ``Z``.

    Sub-second is DELIBERATE: the app treats ``folder_access_ts`` as an opaque
    freshness token and accepts a recheck only when the token DIFFERS from the one
    captured at press time. Truncating to whole seconds would let a fresh probe
    collide with the pressed value inside the same second, and «Проверить снова»
    would false-timeout on a perfectly healthy grant (plan v2 M5f).
    """
    return (datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
            .isoformat().replace("+00:00", "Z"))


def _prev_agent_block(prev: dict | None) -> dict:
    """The ``agent`` block of a previous state dict (``{}`` for anything odd)."""
    if isinstance(prev, dict):
        block = prev.get("agent")
        if isinstance(block, dict):
            return block
    return {}


def _access_fields(prev_agent: dict, access: str, now: float | None = None) -> dict:
    """The agent-block fields describing THIS probe verdict.

    ``folder_access`` + ``folder_access_ts`` always; the two ``folder_missing_*``
    counters only while the verdict is `missing` — they are rebuilt from scratch on
    every publish, so any other verdict resets the transient budget for free.

    The ts is forced to differ from the previous one (µs bump) so two publishes in
    the same microsecond cannot hand the app an unchanged token.
    """
    now = time.time() if now is None else now
    ts = _iso_utc(now)
    if ts == prev_agent.get("folder_access_ts"):
        ts = _iso_utc(now + 1e-6)
    fields: dict = {"folder_access": access, "folder_access_ts": ts}
    if access == ACCESS_MISSING:
        since = prev_agent.get("folder_missing_since")
        streak = prev_agent.get("folder_missing_streak")
        fields["folder_missing_since"] = (
            float(since) if isinstance(since, (int, float)) else now
        )
        fields["folder_missing_streak"] = (
            (int(streak) if isinstance(streak, int) else 0) + 1
        )
    return fields


def _carried_access(prev_agent: dict) -> dict:
    """Access fields carried forward VERBATIM (no probe ran → no new token).

    Used by :func:`refresh_showcase`: a refresh re-projects manifests mid-build and
    never touches the watched folder, so it must not mint a fresh
    ``folder_access_ts`` — that would resolve the app's «Проверить снова» with an
    answer nobody actually checked.
    """
    keys = ("folder_access", "folder_access_ts",
            "folder_missing_since", "folder_missing_streak")
    return {k: prev_agent[k] for k in keys
            if k in prev_agent and prev_agent[k] is not None}


def _access_blocks_scan(fields: dict) -> bool:
    """Р3 gate: must this verdict stop the scan BEFORE it touches anything?

    `denied` and `blocked` always stop it (addendum §4.3: the folder is unreadable
    either way, and everything downstream — ``scan_watch_folder``,
    ``_reconcile_presence``, ``recover_interrupted``'s temp sweep, even the build,
    which writes the ``.m4b`` INSIDE the watched folder — would hit the same wall,
    most of it without a watchdog).

    `missing` stops it too, but only while its transient budget holds: a real,
    settled deletion (≥ :data:`MISSING_TRANSIENT_MIN_SCANS` scans AND
    ≥ :data:`MISSING_TRANSIENT_S` seconds) is allowed through so the library can
    honestly reconcile.
    """
    access = fields.get("folder_access")
    if access in (ACCESS_DENIED, ACCESS_BLOCKED):
        return True
    if access == ACCESS_MISSING:
        streak = fields.get("folder_missing_streak") or 0
        since = fields.get("folder_missing_since")
        since = float(since) if isinstance(since, (int, float)) else time.time()
        settled = (int(streak) >= MISSING_TRANSIENT_MIN_SCANS
                   and (time.time() - since) >= MISSING_TRANSIENT_S)
        return not settled
    return False


def _process_is_idle() -> bool:
    """Is it safe to spend the consent window here? (addendum §4.2 rule 3)

    Phase 2 keeps the ONLY instance of the job alive for up to 90 s, so it is
    allowed only when nothing is waiting: no shutdown asked for, no queued command,
    no build in flight. Everything it looks at lives in Application Support — never
    in the watched folder, which by this point we already know we cannot read.
    """
    if shutdown.requested():
        return False
    try:
        cmd_dir = config.commands_dir()
        if cmd_dir.is_dir() and any(cmd_dir.glob("*.json")):
            return False
    except OSError:
        return False
    try:
        books = config.books_dir()
        if books.is_dir():
            for manifest_path in books.glob("*.json"):
                man = state.read_json(manifest_path, default=None)
                if isinstance(man, dict) and man.get("status") == STATUS_CONVERTING:
                    return False
    except OSError:
        return False
    return True


def _await_consent(probe_handle: _AccessProbe, watch: Path) -> str | None:
    """Phase 2: hold the wedged syscall open for the human's «Разрешить».

    Returns the late verdict, or ``None`` when the window was skipped or expired.
    Callers have ALREADY published `blocked` by this point — the card must be on
    screen while the system dialog is, not 90 s after it.
    """
    window = _consent_window_s()
    if window <= 0 or not _process_is_idle():
        return None
    late = probe_handle.wait(window)
    try:
        state.append_event(
            "folder_access_consent_window",
            window_s=window,
            outcome=(late or ACCESS_BLOCKED),
            elapsed_s=probe_handle.elapsed_s,
            watch_dir=os.path.abspath(str(watch)),
        )
    except Exception:  # noqa: BLE001
        pass
    return late


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


def _stability_snapshot(mp3s: list[Path], folder: Path | None) -> dict | None:
    """One debounce observation: WHICH mp3s exist right now + their size/mtime.

    With ``folder`` the directory is RE-LISTED (:func:`_list_mp3s`) instead of
    trusting the file list handed in — see :func:`_files_are_stable` for why that
    difference is the whole point. Returns ``None`` when the folder cannot be
    listed at all (deleted / unreadable mid-copy); a ``None`` never compares equal
    to anything, so the caller reads it as "not stable" rather than as an empty —
    and therefore trivially "unchanged" — set.
    """
    if folder is not None:
        try:
            mp3s = _list_mp3s(folder)
        except OSError:
            return None
    return _size_mtime_snapshot(mp3s)


def _files_are_stable(mp3s: list[Path], folder: Path | None = None) -> bool:
    """True iff the mp3 SET **and** every file's size+mtime survive a short beat (E10).

    Snapshots, waits :func:`_stability_debounce_s`, snapshots again and compares.
    Any difference — a growing copy, a touched mtime, a vanished file, **or a file
    that was not there when we started** — means the set is still settling →
    ``False`` (the caller skips arming the book this pass). An empty set is
    trivially stable. A zero/near-zero debounce makes this a cheap single
    comparison (used by tests / opt-out). The source mp3s are only ``stat``-ed —
    never read or written (I1).

    ``folder`` closes a hole that predates the early nudge: the original version
    compared the ALREADY-COMPOSED ``mp3s`` list against itself, so it could only
    ever see files that existed when the caller listed the directory. A copy in
    flight adds files, and every file already on disk is by then complete and
    still — so 20 files out of a 56-file book that is still arriving looked
    perfectly "stable" and the book was armed at a third of its length. Re-listing
    the directory on BOTH observations makes an arriving file a difference in the
    snapshot itself, which is the only thing that can catch it. The debounce
    semantics are otherwise untouched (E10: same wait, same place, still before
    anything is armed or published).
    """
    if folder is None and not mp3s:
        return True
    before = _stability_snapshot(mp3s, folder)
    wait = _stability_debounce_s()
    if wait > 0:
        time.sleep(wait)
    after = _stability_snapshot(mp3s, folder)
    if before is None or after is None:
        return False
    return before == after


#: How many ffprobe children the probe pass may keep in flight at once.
#: A probe is ~27 ms of PROCESS SPAWN and ~0 ms of work (measured: identical cost
#: for a 1.4 MB and a 20 MB mp3, and for a bare ``ffprobe -version``), so the pass
#: is latency-bound, not CPU-bound — 56 files cost 1.45 s serially and 0.22 s
#: across 8 workers on this machine. Capped well below the core count on purpose:
#: the agent runs while the human is working, and the curve is already flat past
#: 8 (16 workers bought another 0.08 s for twice the concurrent load).
_PROBE_WORKERS = min(8, max(2, os.cpu_count() or 4))


def _probe_book(mp3s: list[Path]) -> list[dict]:
    """Probe every mp3 once (durations, tags, cover flag). Order = input order.

    Concurrent, but observationally identical to the serial comprehension it
    replaces: :func:`agent.probe.probe_file` is pure — it spawns its OWN ffprobe,
    touches no shared state, writes nothing, and never raises (a broken file comes
    back as an ``ok=False`` marker, by that function's contract) — and
    ``Executor.map`` yields results in ARGUMENT order, never completion order. So
    this returns the same dicts in the same order; only sooner. The pool is sized
    to the work so a one-file book does not build a thread pool at all.
    """
    if len(mp3s) < 2:
        return [probe.probe_file(p) for p in mp3s]
    with ThreadPoolExecutor(
        max_workers=min(_PROBE_WORKERS, len(mp3s)),
        thread_name_prefix="mp3tom4b-probe",
    ) as pool:
        return list(pool.map(probe.probe_file, mp3s))


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


def _resolve_cover(bid: str, ordered_probes: list[dict], mp3s_by_name: dict,
                   *, generation: str | None = None) -> dict:
    """Detect an embedded cover and extract a preview into ``covers/``.

    Returns ``{"cover_state": ..., "cover_preview": <path|None>}``. The cover is
    taken from the FIRST file that carries one (research §4). On a successful
    extract ``cover_state="embedded"`` and ``cover_preview`` points at the file;
    if no file has a cover (or extraction fails) ``cover_state="none"`` and the
    generate chain (M1) will take over. Never raises.

    ``generation`` scopes the file name to THIS preparation of the book (see
    :func:`agent.cover.cover_generation`) — the same rule the generated and web
    files follow, so re-arming a book never rewrites the bytes under a path an
    older manifest still names.
    """
    for pr in ordered_probes:
        if not pr.get("has_embedded_cover"):
            continue
        src = mp3s_by_name.get(pr.get("file"))
        if src is None:
            continue
        stem = cover.cover_file_stem(bid, generation)
        dest = config.covers_dir() / f"{stem}-embedded.jpg"
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


def manifest_phase(manifest: dict) -> str:
    """The manifest's fill-in phase. A MISSING field means ``done`` (D17 compat).

    Pre-D17 manifests carry no ``phase`` at all and are complete by construction —
    reading their absence as ``done`` is what makes the whole protocol change
    migration-free: an old manifest keeps its short-circuit, keeps its token, and
    is never re-armed just because the agent learned a new field.
    """
    if not isinstance(manifest, dict):
        return MANIFEST_PHASE_DONE
    phase = manifest.get("phase")
    return phase if isinstance(phase, str) and phase else MANIFEST_PHASE_DONE


def manifest_build_token(manifest: dict) -> str:
    """The manifest's ``build_token``, or ``""`` when it has none (not buildable).

    ``build_token`` exists ONLY in the final atomic write (:func:`_finish_manifest`)
    and is therefore the physical proof that the manifest is complete — see the
    gate in :func:`agent.dispatcher.validate_command` for why "physical" beats
    "check the phase field" here.
    """
    if not isinstance(manifest, dict):
        return ""
    token = manifest.get("build_token")
    return token if isinstance(token, str) and token else ""


class _ManifestHalt(RuntimeError):
    """Test seam (M-D): stop filling this manifest in, as a crash would."""


def _halt_after_phase() -> str:
    """Phase after which the fill-in must stop, or ``""`` (the production default).

    Only the two literal incomplete phases are honoured, so a typo — or a stray
    variable in someone's shell — can never halt a real agent at ``ready``.
    """
    raw = os.environ.get(_HALT_AFTER_PHASE_ENV, "")
    return raw if raw in INCOMPLETE_PHASES else ""


def _stat_totals(mp3s: list[Path]) -> tuple[int, int]:
    """``(file_count, total_bytes)`` for the skeleton — pure ``stat``, no probe."""
    total = 0
    for p in mp3s:
        try:
            total += p.stat().st_size
        except OSError:
            continue
    return len(mp3s), total


def _skeleton_chapters(mp3s: list[Path]) -> list[dict]:
    """Chapter rows from FILE NAMES alone — the free half of :func:`_build_chapters`.

    Same shape as the real thing (``index`` / ``file`` / ``name`` / ``duration_ms``)
    so the app renders one list, not two: only ``duration_ms`` is ``None`` and the
    names come from the filename rather than the ID3 title. ``mp3s`` arrives
    natural-sorted from :func:`_list_mp3s`, which is also the fallback order
    :func:`agent.metadata.order_chapters` uses when the files carry no track tags —
    so for the common book the skeleton order IS the final order.
    """
    return [
        {
            "index": i,
            "file": p.name,
            "name": metadata.chapter_name_from_filename(p.name),
            "duration_ms": None,
        }
        for i, p in enumerate(mp3s, start=1)
    ]


class _ManifestPlan(NamedTuple):
    """Everything :func:`_finish_manifest` needs to turn a skeleton into ``ready``."""

    manifest: dict          # the skeleton as it stands (on disk when ``staged``)
    path: Path              # queue/books/<book_id>.json
    bid: str
    mp3s: list[Path]
    derive: Callable[[list[dict]], tuple[str, str]]
    staged: bool            # publish intermediate phases (early-nudge path only)


def _manifest_skeleton(
    *,
    bid: str,
    src_dir: str,
    mp3s: list[Path],
    rev: str,
    token: str,
    derive: Callable[[list[dict]], tuple[str, str]],
) -> dict:
    """The phase-``skeleton`` manifest: everything the ``stat`` pass already bought.

    Deliberately NOT empty. We already walked the directory and stat-ed every file
    to compute ``source_rev``; chapter names, author/title, file count and byte
    size fall out of that same walk for free. So the window that opens in ~0.8 s
    opens on a book — its name, its 56 chapter titles — instead of on a spinner,
    and the ffprobe pass that follows only replaces those names with better ones.

    ``derive`` is called with an EMPTY probe list on purpose: every derivation
    (subfolder, combined, separate) already falls back to the folder/file name when
    the tags are missing, which is exactly the probe-free answer we want here. No
    ffprobe is spawned. No ``build_token`` is minted — that is the point.
    """
    author, title = derive([])
    file_count, total_bytes = _stat_totals(mp3s)
    return {
        "book_id": bid,
        "src_dir": src_dir,
        "status": MANIFEST_STATUS_PENDING,
        "phase": MANIFEST_PHASE_SKELETON,
        "source_rev": rev,
        "source_rev_v": SOURCE_REV_VERSION,
        # Minted ONCE, here, and carried by every later phase of this revision.
        # The notified-ledger key embeds it, so re-using it is what makes the
        # skeleton publication and the ready publication the SAME edge — i.e. what
        # holds "one nudge per publication" (I2) structurally rather than by
        # discipline. Mint a second token at ``ready`` and the human gets two
        # windows for one book.
        "confirm_token": token,
        "title": title,
        "author": author,
        "chapters": _skeleton_chapters(mp3s),
        "total_duration_ms": 0,
        "source_samplerate": None,
        "file_count": file_count,
        "total_bytes": total_bytes,
        "cover_state": "none",
        "cover_preview": None,
        "params": dict(DEFAULT_PARAMS),
        # Idempotency ledger (M0.6): idempotency_keys already built for THIS book.
        # Keys are revision-scoped (the app derives them from book_id+source_rev),
        # so a changed source_rev re-arms the book with a fresh, empty ledger —
        # a legitimately new build is allowed, a stale double-click is not.
        "processed_keys": [],
        "ts": time.time(),
    }


def _ensure_build_token(manifest: dict, manifest_path: Path) -> dict:
    """Give a PRE-D17 pending manifest a ``phase``/``build_token``, in place.

    The build gate is fail-closed on ``build_token``, and a manifest written by
    yesterday's agent has none — so without this a book that was already waiting
    for confirmation when the agent updated would become permanently unbuildable
    (its rev is unchanged, so no scan would ever re-arm it either). We upgrade it
    exactly the way the v1→v2 rev migration does: in place, preserving
    ``source_rev`` / ``confirm_token`` / ``processed_keys``, so the ledger key does
    not move and nothing re-nudges.

    Only ``pending-confirm`` manifests are touched (a done/skipped/converting book
    has no live confirm to serve), and NEVER one at an incomplete phase — handing a
    skeleton a build_token would forge the very proof the gate exists to check.
    """
    if manifest.get("status") != MANIFEST_STATUS_PENDING:
        return manifest
    if manifest_build_token(manifest):
        return manifest
    if manifest_phase(manifest) in INCOMPLETE_PHASES:
        return manifest
    upgraded = dict(manifest)
    upgraded["phase"] = MANIFEST_PHASE_READY
    upgraded["build_token"] = secrets.token_hex(16)
    state.write_json_atomic(manifest_path, upgraded)
    state.append_event(
        "manifest_phase_upgraded",
        book_id=upgraded.get("book_id"),
        phase=MANIFEST_PHASE_READY,
    )
    return upgraded


def _upgrade_legacy_manifests(manifests: list[dict]) -> list[dict]:
    """Sweep :func:`_ensure_build_token` over every projected manifest.

    :func:`_begin_manifest` upgrades the books the folder walk actually visits,
    which is subfolder books and nothing else. A grouping-materialized book
    (combined / separate) has no subfolder of its own, so no later scan ever calls
    :func:`_begin_manifest` for it — a pre-D17 one waiting at ``pending-confirm``
    would otherwise sit there with no ``build_token``, be refused by the gate, and
    never get a chance to acquire one. Cheap: the manifests are already in memory,
    and a book is written at most once, ever. Never raises — a failed upgrade
    leaves the original in place rather than breaking the publication.
    """
    out: list[dict] = []
    for m in manifests:
        bid = m.get("book_id")
        if not (isinstance(bid, str) and bid):
            out.append(m)
            continue
        try:
            out.append(_ensure_build_token(m, config.books_dir() / f"{bid}.json"))
        except OSError:
            out.append(m)
    return out


def _begin_manifest(
    *,
    bid: str,
    src_dir: str,
    mp3s: list[Path],
    base_dir: Path,
    derive: Callable[[list[dict]], tuple[str, str]],
    debounce_dir: Path | None = None,
    force: bool = False,
    staged: bool = False,
) -> tuple[dict | None, _ManifestPlan | None]:
    """Gate one book and produce its skeleton. The cheap half of a manifest write.

    Returns ``(current, plan)``:
      · ``(manifest, None)`` — nothing to do; that manifest is already current
        (unchanged ``source_rev``, or a silent v1→v2 rev migration);
      · ``(None, None)`` — the files are still being copied (E10) → the book is not
        armed this pass and nothing at all was written;
      · ``(skeleton, plan)`` — work to do; hand ``plan`` to :func:`_finish_manifest`.

    ``source_rev`` is the pure file-list fingerprint (relpath to ``base_dir`` +
    size + mtime_ns); probe/cover data is display payload and never folded in, so
    an unchanged set keeps its rev/token across scans (M0 idempotency).

    **The short-circuit is phase-aware, and that is load-bearing (I4).** A skeleton
    is written with its FINAL ``source_rev`` — it has to be, or the app could not
    address it — so the plain "same rev → return as-is" rule would hand the
    skeleton back on every subsequent scan and it would never be filled in. The
    window would open in 0.8 s and stay half-empty forever. So an unchanged rev
    short-circuits only for a COMPLETE manifest; ``skeleton``/``chapters`` at
    ``pending-confirm`` mean RESUME instead — pick the fill-in back up, re-using
    the ``confirm_token`` that is already on disk so the resume does not nudge a
    second time. Resume is therefore also the crash recovery: a process that dies
    between phases leaves a resumable manifest, not a stuck one.

    The status guard on that resume matters too: a skeleton the user has since
    SKIPPED sits at ``status=skipped``, and finishing it would rewrite the status
    back to ``pending-confirm`` — resurrecting a book the human took off the
    pipeline. An incomplete phase alone is not enough; it must still be pending.

    ``force`` bypasses the short-circuit entirely: the files are ALWAYS re-probed
    and a fresh manifest written (with a fresh ``confirm_token``) even when the
    inputs did not move. This is the «Собрать заново» (reconvert) path — a book
    built by an OLD agent has a stale manifest missing today's fields, and re-arming
    it in place would preserve the gaps. ``source_rev`` still lands on the same
    value, so the M0 idempotency contract is intact; only the payload is refreshed.

    E10: when ``debounce_dir`` is set (the subfolder scan path) and we are about to
    write a NEW/changed manifest, we first verify the directory is not still being
    copied into (:func:`_files_are_stable`). If it is still settling we return
    ``(None, None)`` WITHOUT writing anything. The wait is paid ONLY here (a
    genuinely new/changed book), never on the unchanged-rev fast path above — and
    it stays BEFORE the skeleton, so the early nudge can never open a window on a
    book that is still arriving (I3).
    """
    rev = source_rev_for(mp3s, base_dir)
    manifest_path = config.books_dir() / f"{bid}.json"
    existing = state.read_json(manifest_path, default=None)
    resumed: dict | None = None

    if not force and isinstance(existing, dict) and existing.get("book_id") == bid:
        if existing.get("source_rev") == rev:
            if (
                manifest_phase(existing) in INCOMPLETE_PHASES
                and existing.get("status") == MANIFEST_STATUS_PENDING
            ):
                resumed = existing          # fall through to the fill-in below
            else:
                # Unchanged inputs, complete manifest → keep it (and its
                # confirm_token) as-is and skip re-probing; byte-identical inputs
                # mean the cached chapters/cover are still valid.
                return _ensure_build_token(existing, manifest_path), None
        # v1→v2 MIGRATION guard (must run BEFORE the re-arm branch below): if the
        # stored rev equals the LEGACY digest of the CURRENT files, the sources are
        # untouched — only the fingerprint FORMULA changed (agent update). Without
        # this, the first scan after the update would re-arm EVERY existing book
        # (a window storm). Upgrade the rev in place, preserving status /
        # confirm_token / processed_keys / everything else, and swap the notified
        # ledger key so a still-pending book does not re-nudge on migration.
        elif existing.get("source_rev") == source_rev_legacy_for(mp3s, base_dir):
            old_key = _book_edge_key(existing)
            existing["source_rev"] = rev
            existing["source_rev_v"] = SOURCE_REV_VERSION
            state.write_json_atomic(manifest_path, existing)
            _notified_replace(old_key, _book_edge_key(existing))
            state.append_event(
                "source_rev_migrated", book_id=bid, rev_v=SOURCE_REV_VERSION
            )
            return _ensure_build_token(existing, manifest_path), None

    if resumed is None:
        # New / changed inputs. E10: make sure they are not mid-copy before arming.
        if debounce_dir is not None and not _files_are_stable(mp3s, debounce_dir):
            state.append_event("book_still_copying", book_id=bid, src_dir=src_dir)
            return None, None
        current = _manifest_skeleton(
            bid=bid, src_dir=src_dir, mp3s=mp3s, rev=rev,
            token=secrets.token_hex(16), derive=derive,
        )
        if staged:
            # PUBLICATION ORDER, step 1 of 4: the manifest hits the disk BEFORE the
            # caller writes state.json, the ledger and the nudge. State must never
            # announce a book the app cannot then open.
            state.write_json_atomic(manifest_path, current)
            state.append_event(
                "manifest_skeleton", book_id=bid, chapters=len(current["chapters"])
            )
    else:
        # Resume: whatever is already on disk (skeleton OR chapters) is richer than
        # a freshly-built skeleton would be, so keep it — and keep its token.
        current = resumed
        state.append_event(
            "manifest_resumed", book_id=bid, phase=manifest_phase(current)
        )

    return current, _ManifestPlan(
        manifest=current, path=manifest_path, bid=bid, mp3s=mp3s,
        derive=derive, staged=staged,
    )


def _finish_manifest(plan: _ManifestPlan) -> dict:
    """Fill a skeleton in to phase ``ready``. The expensive half of a manifest write.

    THE PROBE HAPPENS HERE, ONCE. ``author``/``title`` are derived from ID3 tags,
    so a caller cannot compute them without probing — which is why every caller
    used to probe the whole book itself and then hand the strings in, leaving
    :func:`_probe_book` to run a SECOND time. That cost 2×N ffprobe spawns for a new
    book (112 for a 56-chapter one) and, worse, N spawns for an UNCHANGED book on
    every launchd tick. The ``derive`` callback keeps the probe below the gates:
    unchanged books cost zero ffprobe, new books exactly N.

    Two writes on the staged (early-nudge) path, one on every other path:
      1. ``chapters`` — real names/durations/sample rate, published as soon as the
         probe returns so the window the human is already looking at fills in;
      2. ``ready`` — the FINAL atomic write: cover options resolved and, only here,
         ``build_token`` minted. Nothing between those two writes can make a
         half-filled manifest look buildable, because the proof of completeness is
         created in the same ``os.replace`` as the completeness itself.

    The intermediate write is skipped when not ``staged``: those callers
    (grouping-materialize, reconvert) replace a manifest that may already be
    complete, and they are not reachable by the subfolder scan's resume path — so a
    crash between the two writes would leave them stranded at ``chapters`` with
    nobody to finish them. One write, no window.
    """
    manifest = dict(plan.manifest)
    halt = _halt_after_phase()
    if halt == MANIFEST_PHASE_SKELETON:
        raise _ManifestHalt(MANIFEST_PHASE_SKELETON)

    ordered = metadata.order_chapters(_probe_book(plan.mp3s))
    author, title = plan.derive(ordered)
    manifest.update({
        "phase": MANIFEST_PHASE_CHAPTERS,
        "title": title,
        "author": author,
        "chapters": _build_chapters(ordered),
        "total_duration_ms": _total_duration_ms(ordered),
        # "As in source" anchor: the max sample rate across the readable mp3s, so
        # the build keeps the source SR by default (params.samplerate == None →
        # build_m4b._samplerate falls back to this) instead of resampling.
        "source_samplerate": _source_samplerate(ordered),
        "ts": time.time(),
    })
    if plan.staged:
        state.write_json_atomic(plan.path, manifest)
    if halt == MANIFEST_PHASE_CHAPTERS:
        raise _ManifestHalt(MANIFEST_PHASE_CHAPTERS)

    generation = cover.cover_generation(manifest)
    cover_info = _resolve_cover(plan.bid, ordered, {p.name: p for p in plan.mp3s},
                                generation=generation)
    manifest["cover_state"] = cover_info["cover_state"]
    manifest["cover_preview"] = cover_info["cover_preview"]

    # Cover CHAIN, LOCAL HALF (M1 · re-cut by D17/M-B): embedded → generated plus a
    # default, merged in BEFORE the final atomic write (agent stays the only
    # writer). Guarantees every book has a cover (PRD G4) fully offline, and costs
    # only local work — a file read and a Pillow render. The WEB half is not here
    # any more: it runs after the drain (:func:`enrich_covers_web`) and appends to
    # the end of this list. That is what makes «Собрать» independent of the
    # network — the whole point of M-B.
    manifest.update(cover.resolve_cover_options(manifest))
    # The book still owes the human a web lookup; the enrichment pass claims it.
    manifest["cover_web"] = COVER_WEB_PENDING
    manifest["cover_web_tries"] = 0

    manifest["phase"] = MANIFEST_PHASE_READY
    manifest["build_token"] = secrets.token_hex(16)
    manifest["ts"] = time.time()
    state.write_json_atomic(plan.path, manifest)
    # Publish first, delete second: the manifest naming this generation's files is
    # on disk, so dropping the PREVIOUS generation's images cannot strand a reader.
    cover.sweep_stale_cover_files(plan.bid, generation)
    return manifest


def _write_manifest(
    *,
    bid: str,
    src_dir: str,
    mp3s: list[Path],
    base_dir: Path,
    derive: Callable[[list[dict]], tuple[str, str]],
    debounce_dir: Path | None = None,
    force: bool = False,
) -> dict | None:
    """Build + atomically write ONE complete manifest, start to finish, no publish.

    The single-shot composition of :func:`_begin_manifest` + :func:`_finish_manifest`
    for every caller that is NOT the early-nudge scan path (grouping-materialize,
    reconvert): same gates, same probe, same final ``ready`` manifest — just without
    the intermediate publications, since nobody is waiting at a window for these.
    Returns ``None`` when the book is still copying (E10).
    """
    current, plan = _begin_manifest(
        bid=bid, src_dir=src_dir, mp3s=mp3s, base_dir=base_dir, derive=derive,
        debounce_dir=debounce_dir, force=force, staged=False,
    )
    if plan is None:
        return current
    return _finish_manifest(plan)


def _book_manifest_args(folder: Path, *, force: bool = False) -> dict:
    """The :func:`_begin_manifest` keyword arguments for ONE subfolder book.

    Shared by the single-shot :func:`write_manifest_for_book` and the two-pass
    early-nudge loop in :func:`scan_watch_folder` so the two paths can never drift
    on identity (``book_id``), fingerprint base or author/title derivation.
    ``debounce_dir`` is the book's own folder: the E10 stability check re-lists it
    rather than trusting the file list we just built (see :func:`_files_are_stable`).
    A reconvert skips the debounce — it targets an already-built, settled book.
    """
    folder = Path(folder)
    return {
        "bid": book_id_for(folder),
        "src_dir": os.path.abspath(str(folder)),
        "mp3s": _list_mp3s(folder),
        "base_dir": folder,
        "derive": lambda ordered: metadata.derive_author_title(folder, ordered),
        "debounce_dir": None if force else folder,
        "force": force,
    }


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

    This is the SINGLE-SHOT entry point (reconvert / rescan / direct callers). The
    scan's own loop goes through :func:`_begin_manifest` directly so it can publish
    the skeleton and nudge the app before paying for the probe — see
    :func:`scan_watch_folder`.
    """
    return _write_manifest(**_book_manifest_args(folder, force=force))


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
    return _write_manifest(
        bid=combined_book_id_for(loose),
        src_dir=os.path.abspath(str(watch)),
        mp3s=loose,
        base_dir=watch,
        derive=lambda ordered: metadata.derive_author_title(watch, ordered),
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
        manifests.append(
            _write_manifest(
                bid=book_id_for(f),
                src_dir=os.path.abspath(str(f)),
                mp3s=[f],
                base_dir=f.parent,
                derive=lambda probes, f=f: _separate_author_title(watch, f, probes),
                force=force,
            )
        )
    return manifests


def _separate_author_title(watch: Path, f: Path, probes: list[dict]) -> tuple[str, str]:
    """Author/title for ONE separate-choice book (its single file's own tags).

    Extracted verbatim from :func:`materialize_separate` so the probe can live
    inside :func:`_write_manifest` (see its docstring): the rule is unchanged —
    resolve as usual, then, because a single loose file has no album to title the
    book, prefer its own ID3 title (else the cleaned filename) over the
    watch-folder fallback.
    """
    author, title = metadata.derive_author_title(watch, probes)
    if not (probes and probes[0].get("tags", {}).get("album")):
        title = metadata.chapter_name(probes[0]) if probes else f.stem
    return author, title


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
    publish_early: Callable[[list[dict], dict | None], None] | None = None,
) -> tuple[list[dict], dict | None]:
    """Discover books AND any loose-mp3 group under ``watch``.

    Returns ``(manifests, pending_group)``:
      - ``manifests`` — current manifest dicts for each subfolder-book (a book = a
        direct subfolder containing ≥1 mp3, M0.2), ordered by folder name.
      - ``pending_group`` — a :func:`_build_pending_group` dict when ≥1 mp3 lives
        loose in the watch ROOT (D1 / flows S4) AND that set has not already been
        resolved (``resolved`` ledger), else ``None``. ``previous_group`` (from the
        prior state.json) is passed through so an unchanged set keeps its ``token``.

    Returns ``([], None)`` if the watch dir does not exist yet.

    TWO PASSES, and the order is the whole feature (D17). Pass 1 is everything a
    ``stat`` walk already pays for: fingerprint, gates, copy-stability debounce and
    a SKELETON manifest per new book. Then — with ``publish_early`` — the caller
    publishes and nudges: the window is on screen at ~0.8 s. Pass 2 is the part
    that costs real time (ffprobe over every chapter, then the LOCAL cover chain)
    and it runs with the human already looking at the book.

    Both passes are strictly local. The web lookup for covers is not here at all —
    it is a third pass that runs after the drain (:func:`enrich_covers_web`), which
    is what makes the time to ``ready`` — and therefore to an active «Собрать» —
    the same on a dead network as on a live one.

    Pass 1 writes ALL the skeletons before the single early publication, rather
    than publishing each book as it is armed. Dropping five books at once is a
    rapid-fire case: five publications would mean five ledger reconciliations and
    five nudges racing each other for the foreground. One publication, one nudge.
    """
    watch = Path(watch)
    if not watch.is_dir():
        return [], None

    subfolders = sorted(
        (p for p in watch.iterdir() if p.is_dir() and not p.name.startswith(".")),
        key=lambda p: metadata.natural_sort_key(p.name),
    )

    manifests: list[dict] = []
    plans: list[tuple[int, _ManifestPlan]] = []
    for folder in subfolders:
        args = _book_manifest_args(folder)
        if not args["mp3s"]:
            continue  # no mp3s → not a book (yet)
        current, plan = _begin_manifest(**args, staged=True)
        if current is None:
            # E10: files still being copied → not armed this pass; next scan retries.
            continue
        manifests.append(current)
        if plan is not None:
            plans.append((len(manifests) - 1, plan))

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
        if unchanged or _files_are_stable(loose, watch):
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

    # --- EARLY PUBLICATION (skeletons only) -----------------------------------
    # Fires only when this pass actually armed something new: a scan that found
    # nothing to fill in publishes exactly once, at the end, exactly as before.
    if plans and publish_early is not None:
        try:
            publish_early(list(manifests), pending_group)
        except Exception as exc:  # defensive: the fill-in must survive a bad publish
            state.append_event("early_publish_failed", error=repr(exc)[:200])

    # --- PASS 2: probe → chapters → cover → ready ------------------------------
    for index, plan in plans:
        try:
            manifests[index] = _finish_manifest(plan)
        except _ManifestHalt as halt:
            # Test seam only (MP3TOM4B_HALT_AFTER_PHASE). Stop the whole pass the
            # way a crash would, leaving this manifest at the halted phase for the
            # next scan's resume to pick up.
            state.append_event(
                "manifest_halted", book_id=plan.bid, after_phase=str(halt)
            )
            break

    return manifests, pending_group


def _agent_block(watch: Path, access: dict | None = None) -> dict:
    """The ``agent`` block of ``state.json``.

    ``watch_dir`` + ``active`` drive the Status hero/footer "Активен" pill (spec
    §5). The agent is alive whenever it writes state (this scan is running), so it
    projects itself as active — the live-state semantics the neighbor uses; a dead
    agent simply stops updating state.json.

    ``install_generation`` (plan v2, B3) is the UUID ``packaging/installer.sh``
    stamps into the LaunchAgent's ``EnvironmentVariables`` for THIS install. The
    agent only carries it through: the app compares it with the generation in
    ``install-receipt.json`` to tell "the plist on disk is correct" from "launchd
    is really running the job we just installed". The key is present ONLY when
    launchd handed us one — an agent started by hand (dev run, self-check) says
    nothing rather than lying with a stale value.

    ``access`` (M4) carries the probe verdict fields — ``folder_access``,
    ``folder_access_ts`` and, while the verdict is `missing`, the two transient
    counters. They are passed in rather than probed here because the block is
    rebuilt on every publish, including publishes that ran no probe at all
    (:func:`refresh_showcase` carries the previous verdict forward verbatim).
    """
    block = {"watch_dir": os.path.abspath(str(watch)), "active": True}
    generation = os.environ.get("MP3TOM4B_INSTALL_GENERATION")
    if generation:
        block["install_generation"] = generation
    if isinstance(access, dict):
        for key in ("folder_access", "folder_access_ts",
                    "folder_missing_since", "folder_missing_streak"):
            if access.get(key) is not None:
                block[key] = access[key]
    return block


def build_state(
    manifests: list[dict],
    watch: Path,
    pending_group: dict | None = None,
    grouping_processed: list | None = None,
    prior_progress: dict | None = None,
    access: dict | None = None,
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
        # watch_dir / active / install_generation / folder_access — _agent_block.
        "agent": _agent_block(watch, access=access),
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


def _totals_from_rows(rows: list) -> dict:
    """Totals projected from carried-forward showcase ROWS (no manifest reads).

    Only used on the Р3 early-exit path, where re-reading manifests would be the
    wrong shape anyway (we are freezing the previous picture, not recomputing it).
    «За сегодня» cannot be derived from a row (it needs ``result.built_at``), so it
    is carried from the previous state by the caller instead of being invented here.
    """
    rows = [r for r in rows if isinstance(r, dict)]
    return {
        "books": len(rows),
        "built": sum(1 for r in rows if r.get("status") == "done"),
        "today": 0,
    }


def _carry_forward_showcase(prev: dict | None, watch: Path, fields: dict) -> dict:
    """The PREVIOUS showcase, republished verbatim with a fresh access verdict (Р3).

    This is the whole of the early exit: when the folder cannot be read we change
    exactly one thing — the ``agent`` block — and touch nothing else. No manifest
    is re-read, no source is stat'ed, no ledger is written. The book rows, the
    pending grouping prompt and the grouping ledger move across untouched, so the
    window keeps showing what it showed a second ago plus an honest explanation.

    Why not re-project from the manifests instead: ``_collect_showcase_manifests``
    gates every row on :func:`_manifest_source_alive`, which stat()s ``src_dir`` —
    a path INSIDE the folder we just failed to read. It would wedge or lie exactly
    where we are trying not to.

    With no previous state (first run ever under a denied grant) the result is an
    empty-but-valid showcase: the app needs the ``agent`` block to render the
    access card, and «no books» is the truth — we have never managed to look.
    """
    base = prev if isinstance(prev, dict) else {}
    rows = base.get("books")
    rows = rows if isinstance(rows, list) else []
    groups = base.get("pending_groups")
    ledger = base.get("grouping_processed")
    batch = base.get("batch")
    totals = base.get("totals")
    return {
        "schema": STATE_SCHEMA,
        "agent": _agent_block(watch, access=fields),
        "books": rows,
        "pending_groups": groups if isinstance(groups, list) else [],
        "grouping_processed": [k for k in (ledger or []) if isinstance(k, str)][-256:],
        "batch": batch if isinstance(batch, dict) else
                 {"active": False, "total": 0, "done": 0},
        "totals": totals if isinstance(totals, dict) else _totals_from_rows(rows),
        # Never spawn ffmpeg just to say "no access": reuse the known version.
        "engine": base.get("engine") or engine_version(),
        "ts": time.time(),
    }


def _publish_access(showcase: dict, prev_access: object) -> dict:
    """Write the showcase and, on a rising edge into trouble, raise the app once.

    Edge rule is ``prev != new`` over {`denied`, `blocked`} rather than
    "≠denied → denied": a `denied` ⇄ `blocked` flip is a change of CAUSE and of
    REMEDY (settings trip vs. press «Разрешить» on the dialog that is on screen
    right now), so the card has to be seen again. `ok` never pops — recovery
    explains itself.

    Deliberately NOT :func:`_publish_showcase_and_maybe_open`: that one rewrites
    ``notified.json``, and Р3 forbids touching any ledger on the early-exit path.
    """
    state.write_state(showcase)
    try:
        new_access = _prev_agent_block(showcase).get("folder_access")
        if new_access in (ACCESS_DENIED, ACCESS_BLOCKED) and prev_access != new_access:
            _nudge_app([f"access:{new_access}"])
    except Exception as exc:  # noqa: BLE001 - publication survives any nudge bug
        try:
            state.append_event("app_nudge_failed", error=repr(exc)[:200])
        except Exception:
            pass
    return showcase


def publish_folder_access(access: str, watch: Path | None = None) -> dict:
    """Publish a probe verdict on its own, without scanning anything.

    Two callers, both of which must be able to speak while the scan path cannot:
      · the ``recheck-access`` command (:mod:`agent.dispatcher`) — the app pressed
        «Проверить снова» and is waiting for ``folder_access_ts`` to move, whatever
        the verdict turns out to be;
      · the phase-deadline watchdog in ``agent/__main__`` — something wedged, and
        the state file is the only way the UI will ever learn about it.

    The showcase itself is carried forward verbatim (Р3): re-projecting would mean
    reading the folder we may not be able to read.
    """
    config.ensure_data_dirs()
    target = Path(watch) if watch is not None else watch_dir()
    prev = state.read_state(default=None)
    prev_agent = _prev_agent_block(prev)
    fields = _access_fields(prev_agent, access)
    return _publish_access(
        _carry_forward_showcase(prev, target, fields),
        prev_agent.get("folder_access"),
    )


def run_scan(watch: Path | None = None) -> dict:
    """Full scan pass: ensure manifests, project any loose group, write the showcase.

    Returns the showcase dict that was written. Safe to call repeatedly
    (idempotent per book and per group; tokens are preserved across unchanged
    re-scans — see :func:`write_manifest_for_book` / :func:`_build_pending_group`).
    The grouping idempotency ledger is read from the prior state and carried
    forward so a double-click choice cannot re-materialize after its group is gone.

    ACCESS GATE (M4 · Р3 · addendum §4.3). Before a single byte of the watched
    folder is touched, the probe answers whether we may touch it at all. Anything
    but a usable verdict ends the pass right here — the previous showcase is
    republished with the new verdict and NOTHING else happens: no folder walk, no
    presence reconcile, no ledger write, no `skip` mark disturbed. That is not
    tidiness, it is the difference between "the агент says it lost access" and
    "every book in the library re-armed itself as new", which is what a scan under
    a refused grant produces: the walk returns empty → every source reads as gone →
    ``_reconcile_presence`` marks all absent → the next GOOD scan sees
    absent→present on done books and re-arms the lot.
    """
    config.ensure_data_dirs()
    target = Path(watch) if watch is not None else watch_dir()
    prev = state.read_state(default=None)
    prev_agent = _prev_agent_block(prev)
    prev_access = prev_agent.get("folder_access")

    probe_handle = _start_access_probe(target)
    access = _verdict(probe_handle, _probe_fast_s(), target)

    if access == ACCESS_BLOCKED:
        # Publish FIRST, wait second (addendum §4.2 rule 2). The system dialog is on
        # the human's screen right now; the card explaining who is asking has to be
        # there with it, not 90 s later.
        fields = _access_fields(prev_agent, access)
        blocked_showcase = _publish_access(
            _carry_forward_showcase(prev, target, fields), prev_access
        )
        late = _await_consent(probe_handle, target)
        if late is None:
            return blocked_showcase          # still no decision — try next tick
        access = late                        # the human answered inside our window
        prev_access = ACCESS_BLOCKED         # the edge we just published

    access_fields = _access_fields(prev_agent, access)
    if _access_blocks_scan(access_fields):
        return _publish_access(
            _carry_forward_showcase(prev, target, access_fields), prev_access
        )

    previous_group = _previous_pending_group(prev)
    ledger = _previous_grouping_ledger(prev)
    prior_progress = _previous_book_progress(prev)

    def _publish_early(early: list[dict], group: dict | None) -> None:
        """Publish the skeletons and raise the app — the ~0.8 s window (D17).

        Deliberately NOT a copy of the final publication: the presence reconcile is
        skipped here. It re-arms books (a done book moved out and back in) and
        writes manifests, and running it twice per scan would double that work for
        a case that is not latency-critical anyway — a re-drop is a deliberate
        gesture, not the "I just dropped a book" moment this path exists for. Any
        edge it does produce is published (and nudged, once) by the final pass.

        The nudge is the LAST step of the publication, after the manifests are on
        disk, after state.json, after the ledger — so nothing the app is woken up
        to look at can still be in flight when it looks.
        """
        _publish_showcase_and_maybe_open(
            build_state(
                _collect_showcase_manifests(early), target, pending_group=group,
                grouping_processed=ledger, prior_progress=prior_progress,
                access=access_fields,
            )
        )

    try:
        manifests, pending_group = scan_watch_folder(
            target, previous_group=previous_group, resolved=ledger,
            publish_early=_publish_early,
        )
        # Project subfolder books PLUS grouping-materialized books (combined/
        # separate), which live in queue/books/ but have no subfolder to find.
        showcase_manifests = _upgrade_legacy_manifests(
            _collect_showcase_manifests(manifests)
        )
    except OSError as exc:
        # The grant (or the folder) went away BETWEEN the probe and the walk. Rare
        # but real: a probe answers about the past, and TCC state can change under
        # us at any instant. Without this the exception escapes ``run_scan``, the
        # process dies with a traceback and publishes NOTHING — the same silent
        # death the access gate exists to prevent, just one function later. So we
        # land on the same early exit the probe would have taken, and the library
        # is frozen rather than half-walked.
        late = (ACCESS_DENIED if exc.errno in (errno.EPERM, errno.EACCES)
                else ACCESS_MISSING)
        try:
            state.append_event(
                "folder_access_lost", access=late, errno=exc.errno,
                during="scan", detail=f"{exc.strerror}"[:200],
                watch_dir=os.path.abspath(str(target)),
            )
        except Exception:  # noqa: BLE001
            pass
        return _publish_access(
            _carry_forward_showcase(prev, target,
                                    _access_fields(prev_agent, late)),
            prev_access,
        )
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
        access=access_fields,
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

    The access verdict is carried across the SAME way, and pointedly WITHOUT a new
    ``folder_access_ts``: no probe ran here, so minting a fresh freshness token
    would resolve the app's «Проверить снова» with an answer nobody checked.
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
        access=_carried_access(_prev_agent_block(prev)),
    )
    state.write_state(showcase)
    return showcase


# ─────────────────────────────────────────────────────────────────────────────
# COVER WEB ENRICHMENT — the pass that runs AFTER the scan and the drain (M-B)
#
# The process order is now  scan(local) → drain → enrich, and the order is the
# feature. Nothing the human waits for lives in this pass: the window is up, the
# book is readable, «Собрать» is active and the build, if he asked for one, has
# already run. All this adds is more tiles at the END of a cover strip he is
# already looking at.
#
# Four properties, each of which is the reason for a specific line below:
#
#   · APPEND-ONLY (the human's own call). New candidates are appended; no existing
#     option changes id, path, label or position, and ``cover_selected`` is not
#     touched. Tiles do not move under the cursor while the search runs.
#   · GENERATION-SCOPED (Архитектор #2). Every file this pass writes carries the
#     generation of the preparation that started it, and the manifest is RE-READ
#     and re-checked right before the append — so a book re-armed mid-search is
#     left alone instead of being handed a previous edition's cover.
#   · A NETWORK TIMEOUT IS NOT A FOLDER PROBLEM (I6). This pass runs outside the
#     phase watchdog (``agent.__main__`` disarms it before the drain) and never
#     touches the watch folder or ``folder_access``: it reads manifests in
#     Application Support and writes images to ``covers/``. A dead network can no
#     longer produce the «нет доступа к папке» card — not because the timing works
#     out, but because the two are no longer on the same clock.
#   · IT YIELDS. Bounded by the pass budget, stopped by a shutdown signal, and
#     abandoned the moment a command is waiting — the human clicking «Собрать»
#     outranks a cover search every time.
# ─────────────────────────────────────────────────────────────────────────────


def cover_web_state(manifest: dict) -> str:
    """The manifest's web-leg state; ``""`` when it owes nothing (compat default)."""
    if not isinstance(manifest, dict):
        return ""
    value = manifest.get("cover_web")
    return value if isinstance(value, str) else ""


def _cover_web_tries(manifest: dict) -> int:
    """How many enrichment passes have already tried this book (0 if unknown)."""
    try:
        return max(0, int(manifest.get("cover_web_tries") or 0))
    except (TypeError, ValueError):
        return 0


def _needs_web_enrichment(manifest: dict) -> bool:
    """Whether THIS manifest, as it is on disk right now, still owes a web lookup.

    Four conditions, and the last three are what keep the pass from touching a
    book it has no business in: the leg must be owed (``pending``, tries left), the
    manifest must be COMPLETE (``ready`` — a skeleton has no title to search for
    and no options to append to), and the book must still be waiting for the human
    (``pending-confirm`` — a converting/done/skipped book is nobody's cover strip).
    """
    return (
        cover_web_state(manifest) == COVER_WEB_PENDING
        and _cover_web_tries(manifest) < COVER_WEB_MAX_TRIES
        and manifest_phase(manifest) == MANIFEST_PHASE_READY
        and manifest.get("status") == MANIFEST_STATUS_PENDING
    )


def _commands_waiting() -> bool:
    """True if the app has queued a command we are making wait (best-effort)."""
    try:
        return any(p.is_file() for p in config.commands_dir().glob("*.json"))
    except OSError:
        return False


def _enrich_book_covers(path: Path, manifest: dict) -> int:
    """Run the web lookup for ONE book and APPEND what it found. Returns #appended.

    The search takes seconds, so the manifest that was read before it is not the
    manifest we may write after it. We therefore RE-READ the file and re-check
    both the eligibility and the GENERATION before appending: a book re-armed (or
    confirmed, or skipped) while we were on the network is left exactly as it is,
    and the images we downloaded are simply orphaned under their own generation's
    names — which is the whole reason those names exist.

    Everything an already-visible tile depends on is copied through untouched: the
    existing options keep their order, ids and paths, and ``cover_selected`` is not
    written at all. The only fields this function changes are ``cover_options``
    (extended at the end), the two web-leg bookkeeping fields and ``ts``.
    """
    bid = str(manifest.get("book_id") or "")
    generation = cover.cover_generation(manifest)
    before = manifest.get("cover_options")
    before = before if isinstance(before, list) else []
    # Continue the numbering past any web option a previous attempt already saved,
    # so ids stay unique and nothing already published has to be renamed.
    start = sum(1 for o in before if isinstance(o, dict) and o.get("kind") == "web")
    found = cover.web_options_for(manifest, start_index=start)

    fresh = state.read_json(path, default=None)
    if (not isinstance(fresh, dict)
            or not _needs_web_enrichment(fresh)
            or cover.cover_generation(fresh) != generation):
        state.append_event(
            "cover_web_discarded", book_id=bid, found=len(found),
            reason="manifest moved on while the search was running",
        )
        if isinstance(fresh, dict) and found:
            # The images we just downloaded belong to a preparation nobody points
            # at. The re-arm's own sweep ran BEFORE we wrote them, so collect them
            # here rather than leaving them until the next one.
            cover.sweep_stale_cover_files(bid, cover.cover_generation(fresh))
        return 0

    options = [o for o in (fresh.get("cover_options") or []) if isinstance(o, dict)]
    known = {o.get("id") for o in options}
    appended = [o for o in found if o["id"] not in known]

    tries = _cover_web_tries(fresh) + 1
    fresh["cover_options"] = options + appended          # APPEND-ONLY, by construction
    fresh["cover_web_tries"] = tries
    if appended or tries >= COVER_WEB_MAX_TRIES:
        fresh["cover_web"] = COVER_WEB_DONE
    fresh["ts"] = time.time()
    state.write_json_atomic(path, fresh)
    state.append_event(
        "cover_web_enriched", book_id=bid, appended=len(appended), tries=tries,
        options=len(fresh["cover_options"]), state=cover_web_state(fresh),
    )
    return len(appended)


def enrich_covers_web() -> int:
    """Append web-found covers to every book that still owes a lookup. Returns #books.

    The last pass of a tick, and the only one allowed to be slow. Walks the
    manifests that are ``ready`` + ``pending-confirm`` + ``cover_web=pending``,
    searches for each in turn and appends what it finds, refreshing the showcase
    after every book so tiles appear as they arrive rather than all at the end.

    Never raises and never reports a folder problem: the watch folder is not read
    here at all. Returns the number of books that actually gained a candidate.
    """
    if not _cover_web_enabled():
        return 0
    _reset_cover_web_budget()
    try:
        paths = sorted(config.books_dir().glob("*.json"))
    except OSError:
        return 0

    enriched = 0
    for path in paths:
        if shutdown.requested():
            state.append_event("cover_web_stopped", reason=shutdown.name() or "signal")
            break
        if _commands_waiting():
            # A queued command is the human waiting on a real answer; a cover
            # search is not. Leave the rest ``pending`` for the next tick.
            state.append_event("cover_web_yielded", reason="commands queued")
            break
        if not _cover_web_allowed():
            state.append_event(
                "cover_web_budget_exhausted",
                spent_s=round(_cover_web_spent_s, 3),
                budget_s=_cover_web_budget_s(),
            )
            break
        manifest = state.read_json(path, default=None)
        if not isinstance(manifest, dict) or not _needs_web_enrichment(manifest):
            continue
        started = time.monotonic()
        try:
            appended = _enrich_book_covers(path, manifest)
        except Exception as exc:  # noqa: BLE001 — one bad book never stops the pass
            appended = 0
            state.append_event(
                "cover_web_failed", book_id=str(manifest.get("book_id") or ""),
                error=repr(exc)[:200],
            )
        finally:
            _charge_cover_web(time.monotonic() - started)
        if appended:
            enriched += 1
            try:
                refresh_showcase()
            except Exception as exc:  # noqa: BLE001 — the manifest is already right
                state.append_event("cover_web_publish_failed", error=repr(exc)[:200])
    return enriched
