"""Assemble the final ``.m4b`` from ordered mp3 chapters via ffmpeg.

M1 (arch/synthesis.md §C, plans.md M1, research/m4b-toolchain.md): this is the
heart of the product — the real engine that replaces the M0 fake build. From a
validated manifest + build params it produces ONE valid ``.m4b``:

  - **concat filter + ``aformat``** normalizes every chapter to the target
    samplerate/channels BEFORE concatenation, then ``concat`` joins them → AAC
    (research §1a: a demuxer drifts on heterogeneous inputs; the filter does not,
    and raw-mp3-in-m4b is impossible so a re-encode to AAC is mandatory anyway);
  - **FFMETADATA chapters** with ``TIMEBASE=1/1000`` and START/END from the
    accumulated per-chapter durations; chapter names come straight from the
    manifest (Cyrillic survives — research §1b);
  - **cover**: the SELECTED cover is burned in as ``attached_pic``
    (``-c:v mjpeg -disposition:v attached_pic``, research §1c — mjpeg, not copy, so a
    PNG from the generator / a user file becomes a JPEG cover-art stream).
    :func:`resolve_cover_path` picks it in priority order selected → embedded →
    first generated (``cover_selected`` / ``cover_options`` from the cover chain,
    set by the user in the confirm window or defaulted by the agent), so any kind
    (embedded / web / generated / a user-replaced ``custom``) is embedded. A book
    is built WITHOUT a cover only on a pre-chain manifest that has no usable cover
    on disk — we must not crash here;
  - **container**: ``-f ipod -movflags +faststart`` → a ``.m4b`` with brand
    ``M4A`` and the moov atom up front (research §1d).

Robustness contracts (arch/synthesis.md §C, §B):
  - ffmpeg is always invoked as an **argv array**, never a shell string, so odd
    filenames / Cyrillic stay safe; every call carries a hard timeout.
  - **Atomicity:** the output is written to a hidden temp sibling in the output
    dir and ``os.replace``-d onto the final path only on success. A failure /
    timeout / cancel leaves NO half-written ``.m4b`` — the temp is swept. (The
    dispatcher's recover_interrupted sweeps the same ``.<name>.*.tmp`` pattern.)
  - **I1:** the source mp3s are only ever read (``-i``); they are never written,
    moved or deleted.
  - File-descriptor mitigation (research §1a): the filter graph is passed via a
    ``filter_complex_script`` file (not an argv that can blow the arg limit on
    books with many chapters); above a chapter threshold we fall back to a
    normalized concat *demuxer* so we do not hold hundreds of inputs open at once.

★ ``build`` is invoked ONLY from the ``confirm-build`` handler after the command
is validated (status / source_rev / confirm_token). The scanner never calls it —
that is the structural I2 guarantee (arch/synthesis.md §B).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import config, cover, shutdown, state

# Build params (decisions D2): AAC 192 kbps, stereo, 44100 Hz by default. A
# manifest's ``params`` overrides any of these per-book (set in the confirm
# window); we clamp to sane values so a malformed param can never produce a
# broken ffmpeg argv.
DEFAULT_BITRATE_KBPS = 192
DEFAULT_CHANNELS = "stereo"
DEFAULT_SAMPLERATE = 44100

_CHANNELS_TO_COUNT = {"stereo": 2, "mono": 1}

# Above this many chapters we stop using the concat *filter* (which holds every
# input open simultaneously) and fall back to a normalized concat *demuxer*, to
# stay under the open-file-descriptor limit (research §1a). Audiobooks are
# usually a handful to a few dozen files, so the filter path is the norm; the
# threshold only protects the rare hundreds-of-files collection.
CONCAT_FILTER_MAX_CHAPTERS = 100

# Hard ceiling so a wedged ffmpeg can never hang the (launchd-fired, short-lived)
# agent forever. A real audiobook re-encode is minutes; this is generous headroom
# for a long book on a slow disk while still bounded.
BUILD_TIMEOUT_S = 6 * 60 * 60  # 6 hours

# Cooperative-cancel poll cadence (D13): while ffmpeg encodes, the building agent
# wakes ~3×/s to check two things — is the child still alive, and has a ``cancel``
# command for THIS book landed in queue/commands/. Fast enough to feel instant,
# rare enough to be negligible disk traffic on a minutes-long encode.
CANCEL_POLL_INTERVAL_S = 0.3

# After SIGTERM we give ffmpeg this long to flush + exit cleanly before escalating
# to SIGKILL. ffmpeg traps SIGTERM and finalizes quickly; this is just headroom so
# we almost never need the hard kill (but we always guarantee the child dies).
CANCEL_TERM_GRACE_S = 3.0

# ── Progress deadline (addendum §4.4: «дедлайн фазы сборки — по ПРОГРЕССУ») ───
# A fixed build timeout cannot protect us here: audiobooks are legitimately
# multi-hour, so BUILD_TIMEOUT_S has to stay huge (6 h) — and a job wedged for 6 h
# is a dead product, because launchd will not start a second instance of the same
# label while this one hangs. The failure mode we must catch is a TCC/network wedge:
# the output ``.m4b`` is written INSIDE the watched folder, so an ffmpeg that is
# blocked on a protected/unavailable path sits there alive but frozen forever.
# Hence the deadline is on PROGRESS, not on wall time: no forward motion for this
# long ⇒ tear the encoder down exactly like a signal would (arch addendum proposes
# 300 s; a real encode emits a progress block ~2×/s, so this is ~600× the normal gap).
BUILD_STALL_S = 300.0
_STALL_ENV = "MP3TOM4B_BUILD_STALL_S"

# How often the stall guard is allowed to ``stat`` the output file. The poll loop
# itself runs ~3×/s (cancel cadence); one stat per second per live output is plenty
# to notice motion and keeps the syscall traffic invisible next to the encode.
_STALL_STAT_INTERVAL_S = 1.0

# E5 free-space pre-flight margins. The output-size estimate is approximate
# (bitrate × duration + cover + a small mux allowance), so we require headroom
# beyond it before starting ffmpeg: a multiplicative slop for estimate error PLUS
# an absolute floor so even a tiny book leaves room for the moov atom / temp.
# Generous enough not to false-trip a normal disk, strict enough to catch a
# genuinely full volume before a doomed multi-minute encode.
SPACE_SAFETY_FACTOR = 1.15
SPACE_SAFETY_FLOOR_BYTES = 50 * 1024 * 1024  # 50 MB

# ── Fast mode (Ступень 2, arch/speedup-synthesis.md, D15) ────────────────────
# Build-mode param values (manifest ``params.build_mode``). "fast" = parallel
# groups → concat stream-copy (×6–10, ~25 ms seams at group/chapter boundaries);
# "seamless" = the single-pass encode (bit-exact, slower). ``build`` branches on
# this; scan.DEFAULT_PARAMS defaults to "fast".
BUILD_MODE_FAST = "fast"
BUILD_MODE_SEAMLESS = "seamless"
DEFAULT_BUILD_MODE = BUILD_MODE_FAST

# The parallel path runs K ≈ this many concurrent ffmpeg encoders (one per group
# of consecutive chapters). #1∥#2 agree the win is parallel PROCESSES, not
# -threads (a measured 12.77 vs 12.83 s on audio). We cap the worker count at the
# machine's CPU count (os.cpu_count) and never exceed the chapter count — a book
# with 3 chapters never spawns 8 encoders. Kept modest so a laptop stays usable
# during a build; the seams are workers−1 (all on chapter boundaries), so fewer
# workers also means fewer seams.
FAST_MAX_WORKERS = 8
# Below this many chapters the parallel machinery (K groups + probe + concat) buys
# nothing over a single continuous encode (which is also seamless), so ``build``
# routes a tiny book to the single-encode path even in fast mode. ≥2 groups need
# ≥2 chapters; we want a real win, so require a handful.
FAST_MIN_CHAPTERS = 3

# Chapter-mark drift guard (synthesis §"метки глав из ИЗМЕРЕННОЙ длительности").
# After concatenation we assert the FFMETADATA END of the last chapter equals the
# probed total container duration within this tolerance; a larger gap means the
# marks were built from source (not measured) durations and would drift audibly
# over many chapters (#1: up to ~1.3 s on 56 chapters). One AAC frame at 44.1 kHz
# is 1024/44100 ≈ 23 ms; we allow ~1.5 frames of rounding slack. A breach trips
# the fallback to the single-encode (seamless) path.
FAST_DRIFT_TOLERANCE_MS = 35

# Free-space multiplier for the parallel path: the per-group ``.m4a`` fragments
# AND the final concatenated ``.m4b`` coexist on disk at the concat step, so the
# peak footprint is ≈2× the final size (#2: ×2.1–2.4). ``_ensure_free_space``
# applies this instead of :data:`SPACE_SAFETY_FACTOR` when strategy == "parallel".
SPACE_SAFETY_FACTOR_PARALLEL = 2.3

# Characters that are illegal / hostile in a macOS (and cross-platform) filename.
# ``/`` and NUL are the hard ones on macOS; the rest keep the name portable and
# shell-safe. Collapsed to a single space after stripping.
_ILLEGAL_FILENAME_CHARS = set('/\\:*?"<>|\0')


class BuildError(Exception):
    """A build failed for a reportable reason (ffmpeg error, bad inputs, …).

    Carries a short, human-meaningful ``reason`` the dispatcher stamps into the
    manifest ``error`` so the app can show *why* (not a stack trace).
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(reason if not detail else f"{reason}: {detail}")


class BuildCancelled(Exception):
    """A build was COOPERATIVELY cancelled at the user's request (D13).

    Distinct from :class:`BuildError` — cancellation is not a failure: the
    dispatcher catches this to put the book back to ``pending-confirm`` (so it can
    be re-built), not to ``error``. Carries the ``book_id`` whose cancel command
    tripped the poll, so the dispatcher knows which book to re-arm.

    The cancellation contract (arch D13): the BUILDING agent itself polls for a
    pending ``cancel`` command targeting this book and terminates ITS OWN ffmpeg
    child (SIGTERM→SIGKILL) — there is NO cross-process kill by pid. Every output
    temp is swept on the way out exactly like the failure path, so a cancel leaves
    NO half-written ``.m4b``.
    """

    def __init__(self, book_id: object) -> None:
        self.book_id = book_id
        super().__init__(f"build cancelled for book {book_id!r}")


class BuildInterrupted(BuildError):
    """The build was aborted by the PROCESS, not by the user (M3).

    Two causes, one outcome:
      · ``"signal"`` — a TERM/INT/HUP reached the agent (``launchctl bootout``
        during an installer update is the everyday case). :mod:`agent.shutdown`
        raised the flag; the encoder poll loop saw it.
      · ``"stall"`` — no forward progress for :data:`BUILD_STALL_S` (addendum §4.4):
        ffmpeg is alive but frozen, e.g. blocked writing the ``.m4b`` into a folder
        macOS has not (yet) granted us.

    It is a :class:`BuildError` with ``reason == "interrupted"`` **on purpose**:
    that is the exact reason ``recover_interrupted`` stamps on a build whose process
    was killed outright, the app already renders it («Сборка была прервана»), and
    the dispatcher's existing ``except BuildError`` therefore does the right thing
    with zero changes — sweep the temps, flip the manifest to ``error: interrupted``,
    refresh the showcase. The discriminator lives in ``cause`` + ``detail`` (and in
    the ``build_interrupted`` journal event), not in a new user-facing reason.

    NOT a :class:`BuildCancelled`: a cancel means "the user changed their mind, put
    the book back in the queue"; an interrupt means "we were stopped mid-flight" —
    the book must surface as interrupted, and the user's cancel command (if any)
    stays on disk to be resolved as ``cancel_moot`` on the next run.
    """

    def __init__(self, cause: str, detail: str = "") -> None:
        self.cause = cause  # "signal" | "stall"
        super().__init__("interrupted", detail)


class _FastPathUnusable(Exception):
    """INTERNAL: the fast (parallel) path cannot safely produce this book.

    Raised inside :func:`_build_with_parallel_groups` when a correctness guarantee
    fails — a group encode errored, a fragment could not be probed, the fragments
    disagree on codec/SR/channels (so a ``-c copy`` concat would be invalid), or the
    concatenated timeline drifted past :data:`FAST_DRIFT_TOLERANCE_MS`. It is NOT a
    user-facing failure: :func:`build` catches it and FALLS BACK to the single-pass
    (seamless) path (synthesis Ступень 2: "при провале валидации/дрейфа — fallback
    на single-encode"). A genuine ffmpeg failure on the fallback path then surfaces
    as a normal :class:`BuildError`. Carries a short ``reason`` for the journal.

    Deliberately NOT a subclass of :class:`BuildError` / :class:`BuildCancelled` so
    it can never be mistaken for either by the dispatcher — it stays internal to the
    build strategy selector.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _ffmpeg_bin() -> str:
    """Resolve the ffmpeg executable (PATH lookup; falls back to the name)."""
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe_bin() -> str:
    """Resolve the ffprobe executable (PATH lookup; falls back to the name).

    The fast path re-measures each encoded fragment's duration with ffprobe to
    build drift-free chapter marks (:func:`_probe_media_duration_ms`).
    """
    return shutil.which("ffprobe") or "ffprobe"


# Hard ceiling for a fragment-duration ffprobe on the fast path — metadata-only,
# so seconds is plenty; keeps a wedged ffprobe from hanging the short-lived agent.
FRAGMENT_PROBE_TIMEOUT_S = 30


# Process-lifetime cache for the chosen AAC encoder (speedup Ступень 1, synthesis
# §"aac_at"). Detecting it spawns ``ffmpeg -encoders`` once; the answer cannot
# change while this process lives. ``None`` = not yet resolved.
_ENCODER_CACHE: str | None = None


def _encoder() -> str:
    """Pick the AAC encoder: Apple ``aac_at`` if available, else built-in ``aac``.

    ``aac_at`` (AudioToolbox) is macOS-native and noticeably faster/higher-quality
    than ffmpeg's built-in ``aac`` (synthesis §"aac_at" — Ступень 1: a besшовный,
    single-pass swap, no recipe change). We detect it ONCE per process by grepping
    ``ffmpeg -hide_banner -encoders`` for an ``aac_at`` line, caching the verdict.
    On ANY detection failure (ffmpeg missing, odd output, OS error) we fall back to
    ``aac`` — the universally-present built-in — so the build never breaks on a
    machine without AudioToolbox. The fallback path is byte-for-byte the prior
    recipe (``-c:a aac``), so a no-aac_at machine is exactly as before.
    """
    global _ENCODER_CACHE
    if _ENCODER_CACHE is not None:
        return _ENCODER_CACHE
    enc = "aac"  # safe default — always present
    try:
        out = subprocess.run(
            [_ffmpeg_bin(), "-hide_banner", "-encoders"],
            capture_output=True, text=True, timeout=10,
        )
        text = (out.stdout or "") + (out.stderr or "")
        # The encoders table lists one per line as "<flags> <name>  <desc>"; match
        # the name token to avoid a false hit on a description mentioning aac_at.
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "aac_at":
                enc = "aac_at"
                break
    except (OSError, subprocess.SubprocessError):
        enc = "aac"  # cannot probe → stay on the built-in
    _ENCODER_CACHE = enc
    return enc


def _audio_encoder_args(encoder: str, *, bitrate_kbps: int) -> list[str]:
    """ffmpeg argv fragment for the chosen AAC encoder at CBR ``bitrate_kbps``.

    Both encoders run CBR so :func:`estimate_output_size` / the disk pre-flight /
    the UI estimate stay accurate (synthesis §"aac_at": VBR would make the size
    unpredictable for the disk gate). For ``aac_at`` we force CBR mode explicitly
    (``-aac_at_mode cbr``) — its default is otherwise a VBR-ish ABR. The built-in
    ``aac`` is CBR with ``-b:a`` already. ``-ar`` / ``-ac`` are applied by the
    callers (unchanged) — this fragment is only the codec + its rate/mode.
    """
    if encoder == "aac_at":
        return ["-c:a", "aac_at", "-aac_at_mode", "cbr", "-b:a", f"{bitrate_kbps}k"]
    return ["-c:a", "aac", "-b:a", f"{bitrate_kbps}k"]


def _cancel_requested(book_id: object) -> bool:
    """True iff a pending ``cancel`` command targets ``book_id`` (D13 poll).

    The building agent owns its own cancellation: instead of any cross-process
    pid kill, it simply LOOKS for the app's cooperative signal — a
    ``queue/commands/<cmd>.json`` whose ``action == "cancel"`` and
    ``book_id`` matches. The command stays on disk (we never delete it here); the
    dispatcher consumes it once the build has actually unwound (single owner — no
    double processing). A missing/unreadable command dir or file is simply "no
    cancel" — polling must never raise into the encode loop.

    ``book_id is None`` can never match a real command (commands always carry a
    book_id), so a manifest with no id is conservatively never cancellable here.
    """
    if not book_id:
        return False
    cmd_dir = config.commands_dir()
    try:
        entries = list(cmd_dir.glob("*.json"))
    except OSError:
        return False
    for path in entries:
        cmd = state.read_json(path, default=None)
        if not isinstance(cmd, dict):
            continue
        if cmd.get("action") == "cancel" and cmd.get("book_id") == book_id:
            return True
    return False


def sanitize_filename(name: str) -> str:
    """Make ``name`` safe to use as a single filename component.

    Strips illegal / hostile characters, collapses whitespace, trims leading
    dots (so we never produce a hidden file) and trailing dots/spaces (illegal as
    a trailing char on some filesystems). Falls back to ``"book"`` if nothing
    usable remains. Cyrillic and most Unicode are kept verbatim.
    """
    cleaned_chars = [
        (ch if ch not in _ILLEGAL_FILENAME_CHARS else " ") for ch in str(name)
    ]
    cleaned = "".join(cleaned_chars)
    # Collapse runs of whitespace to one space.
    cleaned = " ".join(cleaned.split())
    # No leading dots (hidden file) / trailing dots or spaces.
    cleaned = cleaned.lstrip(".").rstrip(" .")
    return cleaned or "book"


def output_filename(manifest: dict) -> str:
    """Compose ``«Автор - Название».m4b`` (sanitized) for a manifest.

    Uses the resolved ``author`` / ``title``; if the author is blank, the name is
    just ``«Название».m4b``. The whole component is sanitized as one unit so a
    stray separator inside author/title cannot escape the filename.
    """
    author = str(manifest.get("author") or "").strip()
    title = str(manifest.get("title") or "").strip() or "book"
    stem = f"{author} - {title}" if author else title
    return f"{sanitize_filename(stem)}.m4b"


def default_output_path(manifest: dict) -> Path:
    """Where the ``.m4b`` lands: next to the source folder, same parent.

    The output sits beside the book's source directory (``src_dir``) — i.e. in the
    SAME parent folder as the book — named ``«Автор - Название».m4b``. Keeping it
    out of the source folder means the scanner (which only ingests mp3 under a
    subfolder) never re-reads our own output. Falls back to the support tree if
    ``src_dir`` is somehow absent.
    """
    src_dir = manifest.get("src_dir")
    if src_dir:
        parent = Path(src_dir).parent
    else:  # pragma: no cover - defensive; a real manifest always has src_dir
        from . import config

        parent = config.support_root()
    return parent / output_filename(manifest)


def _channels_count(params: dict) -> int:
    """Resolve the output channel count from params (default stereo=2)."""
    ch = str(params.get("channels") or DEFAULT_CHANNELS).lower()
    return _CHANNELS_TO_COUNT.get(ch, 2)


def _channel_layout(params: dict) -> str:
    """ffmpeg ``channel_layouts`` token for ``aformat`` (stereo / mono)."""
    return "mono" if _channels_count(params) == 1 else "stereo"


def _samplerate(params: dict, source_sr: int | None = None) -> int:
    """Resolve the output sample rate (Hz): explicit pick → source SR → default.

    Priority (D-"keep source SR"):
      1. ``params['samplerate']`` as a POSITIVE int → the user's explicit choice
         (44100 / 48000 from the confirm window) wins — we resample to it;
      2. otherwise (``None`` / missing / non-positive sentinel = "as in source")
         → ``source_sr`` if it is a positive int (the manifest's
         ``source_samplerate``, max across the source mp3s) — no resample;
      3. else :data:`DEFAULT_SAMPLERATE` (44100) as the final safety net (e.g. a
         pre-field manifest with no source SR recorded).
    """
    raw = params.get("samplerate", None)
    try:
        sr = int(raw)
    except (TypeError, ValueError):
        sr = 0
    if sr > 0:
        return sr  # explicit user choice → resample to it
    # "As in source": keep the source rate when we know it, else the default.
    if isinstance(source_sr, int) and source_sr > 0:
        return source_sr
    return DEFAULT_SAMPLERATE


def _bitrate_kbps(params: dict) -> int:
    """Resolve the output AAC bitrate (kbps) from params (default 192)."""
    try:
        br = int(params.get("bitrate", DEFAULT_BITRATE_KBPS))
    except (TypeError, ValueError):
        return DEFAULT_BITRATE_KBPS
    return br if br > 0 else DEFAULT_BITRATE_KBPS


def _build_mode(params: dict) -> str:
    """Resolve the build mode from params: ``"fast"`` (default) or ``"seamless"``.

    D15 / synthesis Ступень 2: the confirm window's toggle rides in
    ``params.build_mode``. Any value other than the exact ``"seamless"`` sentinel
    (missing / null / garbage / ``"fast"``) resolves to :data:`BUILD_MODE_FAST`,
    so the default and every malformed value both mean "fast" — the engine can
    never be tricked into a third mode. Only the literal ``"seamless"`` selects the
    single-pass bit-exact path.
    """
    mode = str(params.get("build_mode") or "").strip().lower()
    return BUILD_MODE_SEAMLESS if mode == BUILD_MODE_SEAMLESS else BUILD_MODE_FAST


def _fast_worker_count(chapter_count: int) -> int:
    """How many parallel encoders the fast path uses for ``chapter_count`` chapters.

    K = min(CPU count, :data:`FAST_MAX_WORKERS`, chapter_count) but never < 1.
    Capping at the chapter count means each group holds ≥1 chapter (an empty group
    is impossible); capping at the CPU count keeps the machine responsive and
    matches the fact that beyond core-count the encoders just time-slice. Fewer
    workers ⇒ fewer inter-group seams (workers−1), which is also strictly better
    for seamlessness — so a conservative K is a double win.
    """
    cpu = os.cpu_count() or 1
    return max(1, min(FAST_MAX_WORKERS, cpu, max(1, int(chapter_count))))


def _usable_chapters(manifest: dict) -> list[dict]:
    """Chapters with a real source file and a positive duration, in order.

    A chapter whose ``duration_ms`` is missing/None (an unreadable mp3, surfaced
    by probe) cannot be placed on the timeline. Order is the manifest order
    (already track-/natural-sorted by the scanner).

    ★ This is the *filter* used for size/duration math (``estimate_output_size``)
    where dropping an unreadable chapter is harmless. The BUILD path does NOT use
    it to silently drop a bad chapter — :func:`_unreadable_chapter_files` is the
    guard there (E3 decision: one unreadable chapter fails the WHOLE book rather
    than shipping a silently-partial ``.m4b``).
    """
    out: list[dict] = []
    for ch in manifest.get("chapters", []):
        if not isinstance(ch, dict):
            continue
        dur = ch.get("duration_ms")
        src = ch.get("file")
        if isinstance(dur, int) and dur > 0 and src:
            out.append(ch)
    return out


def _unreadable_chapter_files(manifest: dict) -> list[str]:
    """Names of chapters that cannot be placed on the timeline (E3 guard).

    A chapter is "unreadable" when it has a source ``file`` but no positive
    ``duration_ms`` — exactly the probe verdict for a corrupt / non-audio mp3
    (probe sets ``duration_ms=None``). The build refuses to proceed if ANY chapter
    is unreadable: shipping a book that silently dropped a chapter is worse than
    failing (E3 — the user must fix/remove the file, after which a re-scan re-arms
    the book). Returns the bad chapters' file names in manifest order (for the
    error ``detail`` the banner shows). Chapters with no ``file`` at all are
    skipped here — they are not a "missing chapter" the user can point at.
    """
    bad: list[str] = []
    for ch in manifest.get("chapters", []):
        if not isinstance(ch, dict):
            continue
        src = ch.get("file")
        if not src:
            continue
        dur = ch.get("duration_ms")
        if not (isinstance(dur, int) and dur > 0):
            bad.append(str(src))
    return bad


def resolve_cover_path(manifest: dict) -> Path | None:
    """Resolve which cover file to burn into the ``.m4b`` for ``manifest``.

    M1 (the cover chain landed): the cover is no longer "embedded only". The agent
    resolves the user's pick — or the default — in priority order, returning the
    FIRST existing file:

      1. the SELECTED option — ``cover.selected_cover_path`` maps
         ``cover_selected`` → its entry in ``cover_options`` (embedded / web /
         generated / a user-replaced ``custom`` the dispatcher copied in). This is
         the user's choice from the confirm window (or the agent's default when the
         user didn't re-pick), and it ALREADY falls back to the embedded preview for
         pre-options manifests;
      2. the embedded preview directly (extra belt-and-suspenders for an exotic
         manifest that has ``cover_state==embedded`` but no resolvable selection);
      3. the first ``generated`` option on disk (the PRD-G4 guarantee — a book is
         never coverless; if the chain ran, a generated variant always exists).

    Returns ``None`` only when truly nothing usable exists on disk (e.g. a manifest
    that predates the cover chain AND has no embedded preview) — then the build
    proceeds WITHOUT a cover rather than failing. Any resolved cover (generated /
    web / custom included) is burned as ``attached_pic`` by the caller.
    """
    # 1) the selected option (also handles the legacy embedded-preview fallback).
    sel = cover.selected_cover_path(manifest)
    if sel is not None:
        return sel

    # 2) embedded preview directly (defensive — selected_cover_path already tries
    #    this, but a manifest could carry an embedded preview with no options list).
    if manifest.get("cover_state") == "embedded":
        preview = manifest.get("cover_preview")
        if isinstance(preview, str) and preview and Path(preview).is_file():
            return Path(preview)

    # 3) first generated option that exists (the guarantee — never coverless).
    for opt in manifest.get("cover_options") or []:
        if isinstance(opt, dict) and opt.get("kind") == "generated":
            p = opt.get("path")
            if isinstance(p, str) and p and Path(p).is_file():
                return Path(p)

    return None


def _chapter_source_paths(manifest: dict, chapters: list[dict]) -> list[Path]:
    """Absolute source paths for ``chapters``, resolved under ``src_dir``.

    The manifest stores chapter ``file`` as a bare name relative to the book
    folder; join it onto ``src_dir`` to get the real input path for ffmpeg.
    """
    src_dir = Path(manifest.get("src_dir") or ".")
    return [src_dir / str(ch.get("file")) for ch in chapters]


def _plan_encode_groups(
    chapters: list[dict], sources: list[Path], workers: int
) -> list[dict]:
    """Split CONSECUTIVE chapters into ≈``workers`` balanced groups (synthesis §key).

    The fast path's central idea (#2 improving #1): rather than encode each chapter
    alone (55 seams on 56 chapters), encode GROUPS of consecutive chapters — one
    continuous ffmpeg pass per group. Stitches WITHIN a group are bit-exact (one
    encode); the only independent seams are BETWEEN groups (``workers−1`` of them),
    and because a group boundary is always a chapter boundary those seams fall on
    natural pauses. So we want K ≈ ``workers`` groups that are balanced by DURATION
    (not chapter count) — the encode is wall-clock-bound by the SLOWEST group, so
    equal-duration groups minimize total time.

    Algorithm — a greedy contiguous partition:
      · target = total_duration / K;
      · walk chapters in order, appending to the current group; close the group
        once its accumulated duration reaches the target AND we still have enough
        remaining chapters to fill the groups we have not opened yet (so we never
        starve the tail into empty groups — every group gets ≥1 chapter);
      · the last group takes whatever remains.
    Contiguity is mandatory: concatenating groups back in order must reproduce the
    book, so a group is always a run of adjacent chapters (never reordered).

    Each returned group dict::

        {"index": 0-based, "chapters": [chapter dicts],
         "sources": [Path], "planned_ms": Σ source duration_ms}

    ``planned_ms`` is the SOURCE-duration sum (for balancing + the disk estimate);
    the chapter MARKS are rebuilt later from the ffprobe-MEASURED fragment
    durations, never from this (synthesis §"метки глав из ИЗМЕРЕННОЙ длительности").
    """
    n = len(chapters)
    k = max(1, min(int(workers), n))
    if k <= 1:
        return [{
            "index": 0,
            "chapters": list(chapters),
            "sources": list(sources),
            "planned_ms": sum(int(c.get("duration_ms") or 0) for c in chapters),
        }]

    durations = [int(c.get("duration_ms") or 0) for c in chapters]
    total = sum(durations) or 1
    target = total / k

    groups: list[dict] = []
    cur_ch: list[dict] = []
    cur_src: list[Path] = []
    cur_ms = 0
    for i in range(n):
        cur_ch.append(chapters[i])
        cur_src.append(sources[i])
        cur_ms += durations[i]
        groups_left_to_open = k - len(groups) - 1  # after the current one closes
        remaining_after = n - (i + 1)
        # Close the current group when it has met the per-group target, but only
        # while leaving AT LEAST one chapter for each group not yet opened (so no
        # group ends up empty — ``remaining_after >= groups_left_to_open``). Never
        # open more than k groups.
        if (len(groups) < k - 1
                and cur_ms >= target
                and remaining_after >= groups_left_to_open):
            groups.append({
                "index": len(groups),
                "chapters": cur_ch,
                "sources": cur_src,
                "planned_ms": cur_ms,
            })
            cur_ch, cur_src, cur_ms = [], [], 0

    if cur_ch:  # the tail (always non-empty here) becomes the last group
        groups.append({
            "index": len(groups),
            "chapters": cur_ch,
            "sources": cur_src,
            "planned_ms": cur_ms,
        })
    return groups


def _probe_media_duration_ms(path: Path) -> int | None:
    """ffprobe the ACTUAL container duration (ms) of an encoded fragment, or ``None``.

    The fast path builds chapter START/END marks from the MEASURED duration of each
    encoded ``.m4a`` group — NOT the source ``duration_ms`` — because an AAC encode
    adds priming/padding so a fragment's real length differs from its mp3 source by
    a frame or two; summed over many chapters that source-based drift reaches ~1.3 s
    (#1). Reading ``format.duration`` back from the encoded fragment is the ground
    truth the concatenated timeline will actually have. Argv array (never a shell
    string), bounded timeout, never raises: any failure returns ``None`` and the
    caller treats a group with no measurable duration as a fatal fast-path error
    (→ fallback to the single-encode path).
    """
    try:
        out = subprocess.run(
            [_ffprobe_bin(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=FRAGMENT_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    raw = (out.stdout or "").strip()
    try:
        return int(round(float(raw) * 1000))
    except (TypeError, ValueError):
        return None


def _ffmetadata_header(manifest: dict) -> list[str]:
    """The FFMETADATA global-tags header lines (title/album/artist/genre).

    Shared by the source-duration path (:func:`_ffmetadata_text`) and the
    measured-duration path (:func:`_ffmetadata_from_marks`) so both files carry
    identical book tags. Escaped per the FFMETADATA spec; Cyrillic kept as-is.
    """
    title = str(manifest.get("title") or "").strip()
    author = str(manifest.get("author") or "").strip()
    lines = [";FFMETADATA1"]
    if title:
        lines.append(f"title={_meta_escape(title)}")
        lines.append(f"album={_meta_escape(title)}")
    if author:
        lines.append(f"artist={_meta_escape(author)}")
    lines.append("genre=Audiobook")
    lines.append("")
    return lines


def _chapter_marks(chapters: list[dict], durations_ms: list[int]) -> list[dict]:
    """Accumulate per-chapter START/END (ms) from a list of durations, in order.

    Each mark is ``{"start", "end", "name"}``. START is the running sum; END is
    START + this chapter's duration. The ``durations_ms`` list is what makes this
    reusable: the single-pass path passes the SOURCE ``duration_ms`` (exact, one
    encode), while the fast path passes the ffprobe-MEASURED fragment durations
    (synthesis §"метки глав из ИЗМЕРЕННОЙ длительности") so the marks match the real
    concatenated timeline instead of drifting. Lengths must align 1:1; a missing /
    non-positive duration contributes 0 so the timeline never goes backwards.
    """
    marks: list[dict] = []
    start = 0
    for ch, dur in zip(chapters, durations_ms):
        d = int(dur) if isinstance(dur, int) and dur > 0 else 0
        end = start + d
        marks.append({"start": start, "end": end,
                      "name": str(ch.get("name") or "").strip()})
        start = end
    return marks


def _ffmetadata_from_marks(manifest: dict, marks: list[dict]) -> str:
    """Render the FFMETADATA chapter file from pre-computed START/END marks.

    The fast path builds ``marks`` from the ffprobe-measured group durations (via
    :func:`_chapter_marks`), so the chapter boundaries land exactly where the
    concatenated audio actually splits — no source-vs-encoded drift. Header +
    escaping are identical to :func:`_ffmetadata_text`; only the START/END source
    differs (measured, not summed source ``duration_ms``).
    """
    lines = _ffmetadata_header(manifest)
    for m in marks:
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={int(m['start'])}")
        lines.append(f"END={int(m['end'])}")
        name = str(m.get("name") or "").strip()
        if name:
            lines.append(f"title={_meta_escape(name)}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _ffmetadata_text(manifest: dict, chapters: list[dict]) -> str:
    """Render the FFMETADATA chapter file (TIMEBASE 1/1000), research §1b.

    Global tags (title/artist/album/genre) come from the manifest; START/END are
    the accumulated SOURCE chapter durations in milliseconds (exact on the
    single-pass path, which re-encodes the whole book in one go); each chapter
    ``title`` is the manifest chapter name. Values are escaped per the FFMETADATA
    spec (``=``, ``;``, ``#``, ``\\`` and newlines are backslash-escaped) so a name
    containing those characters cannot corrupt the file. Cyrillic is written as-is
    (the file is UTF-8). (The fast path uses :func:`_ffmetadata_from_marks` with
    MEASURED durations instead — see its docstring.)
    """
    durations = [int(ch["duration_ms"]) for ch in chapters]
    marks = _chapter_marks(chapters, durations)
    return _ffmetadata_from_marks(manifest, marks)


def _meta_escape(value: str) -> str:
    """Escape a value for an FFMETADATA key=value line.

    Per the ffmpeg metadata spec, the special characters ``=``, ``;``, ``#`` and
    ``\\`` must be backslash-escaped; a literal newline in a value would break
    the line, so we escape it too. (Backslash is escaped first to avoid
    double-escaping the escapes we add.)
    """
    out = value.replace("\\", "\\\\")
    for ch in ("=", ";", "#"):
        out = out.replace(ch, "\\" + ch)
    out = out.replace("\n", "\\\n").replace("\r", "")
    return out


def _filter_complex_script(n: int, samplerate: int, layout: str) -> str:
    """Build the concat-filter graph for ``n`` inputs (research §1a recipe).

    Each input audio is normalized to the target SR/layout via ``aformat`` BEFORE
    being fed to ``concat``, which is what keeps the joined duration drift-free on
    heterogeneous chapters. Returns the graph text for a
    ``filter_complex_script`` file (keeps it out of argv → no arg-length / fd
    pressure from a huge inline filter).
    """
    parts = []
    labels = []
    for i in range(n):
        parts.append(
            f"[{i}:a]aformat=sample_rates={samplerate}:channel_layouts={layout}[a{i}]"
        )
        labels.append(f"[a{i}]")
    parts.append(f"{''.join(labels)}concat=n={n}:v=0:a=1[aout]")
    return ";\n".join(parts) + "\n"


def _terminate_ffmpeg(proc: "subprocess.Popen") -> None:
    """Kill our OWN ffmpeg child (SIGTERM, then SIGKILL) and reap it (D13).

    This is the *cooperative* teardown: ``proc`` is the child WE spawned, so we
    signal it directly — no pid lookup of someone else's process. SIGTERM lets
    ffmpeg finalize and exit; if it has not gone after ``CANCEL_TERM_GRACE_S`` we
    escalate to SIGKILL so the child can NEVER be orphaned. We always ``wait`` so
    no zombie lingers. Best-effort: a child that already exited just no-ops.
    """
    if proc.poll() is not None:
        return
    try:
        proc.terminate()  # SIGTERM
    except OSError:
        pass
    try:
        proc.wait(timeout=CANCEL_TERM_GRACE_S)
        return
    except subprocess.TimeoutExpired:
        pass
    except OSError:
        return
    # Still alive after the grace window → hard kill, then reap.
    try:
        proc.kill()  # SIGKILL
    except OSError:
        pass
    try:
        proc.wait(timeout=CANCEL_TERM_GRACE_S)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _terminate_ffmpeg_many(children: "list[subprocess.Popen]") -> None:
    """Kill ALL our ffmpeg children (SIGTERM→grace→SIGKILL) and reap every one (D13).

    The fast path runs a pool of concurrent ffmpeg encoders, so a cancel must gas
    the WHOLE group, not one at a time. We do it in two phases so the grace window
    is paid ONCE for the group, not serially per child (looping
    :func:`_terminate_ffmpeg` would wait up to ``CANCEL_TERM_GRACE_S`` × N):
      1. SIGTERM every still-running child at once;
      2. wait up to the single grace window for them all to finalize;
      3. SIGKILL any that are still alive, then ``wait`` on each so no zombie
         lingers.
    Best-effort and fully tolerant — a child that already exited just no-ops, and
    an OSError on any signal is swallowed (the goal is that no child survives, and
    every one is reaped). ``children`` may include already-dead entries.
    """
    alive = [p for p in children if p is not None and p.poll() is None]
    # Phase 1: SIGTERM all.
    for p in alive:
        try:
            p.terminate()
        except OSError:
            pass
    # Phase 2: one shared grace window for the group to exit cleanly.
    deadline = time.monotonic() + CANCEL_TERM_GRACE_S
    for p in alive:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            p.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            pass
        except OSError:
            pass
    # Phase 3: SIGKILL survivors, then reap everyone.
    for p in alive:
        if p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass
        try:
            p.wait(timeout=CANCEL_TERM_GRACE_S)
        except (subprocess.TimeoutExpired, OSError):
            pass


def _interrupted(cause: str, detail: str, children: "list[subprocess.Popen]",
                 book_id: object = None) -> BuildInterrupted:
    """Kill every ffmpeg child, journal the abort, RETURN the exception to raise.

    Returning (instead of raising) keeps the ``raise`` visible at the call site
    while the teardown stays in one place. Order matters: children die FIRST — the
    journal write is best-effort and must never come between a signal and a live
    encoder. Safe to call twice (``_terminate_ffmpeg_many`` no-ops on dead pids).
    """
    _terminate_ffmpeg_many(children)
    state.append_event("build_interrupted", cause=cause, detail=detail,
                       book_id=book_id, children=len(children))
    return BuildInterrupted(cause, detail)


def _interrupt_if_shutdown(children: "list[subprocess.Popen]",
                           book_id: object = None) -> None:
    """If a signal asked us to stop: kill every ffmpeg child, then raise (M3).

    This is THE hand-off between :mod:`agent.shutdown` (which only raises a flag,
    because a signal handler must stay trivial) and the process tree (which only
    the main loop can safely take down). Called from every encoder poll tick, so
    the worst-case latency between ``launchctl bootout`` and a dead ffmpeg is one
    :data:`CANCEL_POLL_INTERVAL_S` plus the SIGTERM grace — far inside launchd's
    own exit timeout, so the SIGKILL that follows finds nothing left to orphan.

    Idempotent by construction: it raises, so the unwind happens once;
    :func:`_terminate_ffmpeg_many` itself tolerates already-dead children, so a
    repeated signal (or a second call from an enclosing loop) is a no-op.
    """
    if not shutdown.requested():
        return
    raise _interrupted("signal", f"agent received {shutdown.name()}",
                       children, book_id)


def _build_stall_s() -> float:
    """Resolve the no-progress deadline (seconds), env-overridable for tests.

    Defaults to :data:`BUILD_STALL_S`; ``MP3TOM4B_BUILD_STALL_S`` lets a self-check
    shrink it to a few seconds so a genuinely wedged encoder can be proven in a test
    run rather than argued about. A malformed / non-positive value falls back to the
    default — the deadline can be tuned, never disabled.
    """
    raw = os.environ.get(_STALL_ENV)
    if raw is None:
        return BUILD_STALL_S
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return BUILD_STALL_S
    return val if val > 0 else BUILD_STALL_S


class _StallGuard:
    """Watches an encode for *forward motion* and reports a wedge (addendum §4.4).

    Two independent liveness signals, either of which resets the clock:

    1. **the reported position advanced** — ffmpeg's ``-progress`` stream is parsed
       by the reader threads anyway, so this is free;
    2. **the output file changed** (size *or* mtime). This second signal is what
       keeps the guard from murdering a healthy build: ``-movflags +faststart``
       relocates the moov atom AFTER the last progress block, so a big book ends
       with a silent stretch where only the file keeps moving. Under a real wedge
       (TCC prompt on the watch folder, vanished network volume) neither moves.

    ``stalled()`` is therefore honest in both directions: it fires on a frozen
    encoder and stays quiet on a slow one.
    """

    __slots__ = ("limit", "_stat_every", "_best_ms", "_sigs", "_since", "_next_stat")

    def __init__(self, limit_s: float | None = None) -> None:
        self.limit = _build_stall_s() if limit_s is None else float(limit_s)
        # Sample the file at least ~4× per deadline window, so the second liveness
        # signal is never starved by the throttle when the deadline is tuned down
        # (a self-check runs with seconds, production with 300 s → the plain 1 s).
        self._stat_every = max(0.05, min(_STALL_STAT_INTERVAL_S, self.limit / 4.0))
        self._best_ms = -1
        self._sigs: dict[str, tuple[int, int]] = {}
        self._since = time.monotonic()
        self._next_stat = 0.0

    @staticmethod
    def _signature(path: "Path") -> tuple[int, int] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_size, st.st_mtime_ns)

    def stalled(self, pos_ms: int | None = None,
                paths: "tuple[Path, ...] | list[Path]" = ()) -> bool:
        """Feed this tick's liveness signals; True ⇔ nothing moved for ``limit``.

        ``pos_ms`` is the aggregate encoded position (ms) — compared against the
        BEST seen, so the momentary dip when the parallel pool retires one group
        and starts the next is not mistaken for progress *or* for a stall.
        """
        now = time.monotonic()
        moved = False

        if pos_ms is not None and pos_ms > self._best_ms:
            self._best_ms = int(pos_ms)
            moved = True

        # The stat pass is throttled — it is a fallback signal, not the main one.
        if paths and now >= self._next_stat:
            self._next_stat = now + self._stat_every
            for p in paths:
                sig = self._signature(p)
                if sig is None:
                    continue
                key = str(p)
                if self._sigs.get(key) != sig:
                    self._sigs[key] = sig
                    moved = True

        if moved:
            self._since = now
            return False
        return (now - self._since) >= self.limit

    def detail(self) -> str:
        """Journal/manifest detail describing the wedge we just caught."""
        return f"no ffmpeg progress for {int(self.limit)}s"


def _progress_snapshot(
    out_time_ms: int, total_ms: int, chapters: list[dict], start_monotonic: float
) -> dict:
    """Compute the state.json ``progress`` contract from a live ffmpeg position.

    Contract (status.md / synthesis): a dict carried on the converting book's
    showcase row::

        {percent, out_time_ms, total_ms, elapsed_s, eta_s|None,
         current_chapter_index, current_chapter_name, total_chapters}

    · ``percent`` = 100·out_time/total, clamped 0..100;
    · ``current_chapter_index`` (1-based) = the FIRST chapter whose CUMULATIVE end
      (ms) exceeds ``out_time_ms`` — i.e. the one being encoded now — with its
      ``current_chapter_name``; once past the last chapter's end it pins to the last;
    · ``elapsed_s`` = wall seconds since the build entered ffmpeg
      (``time.monotonic`` anchor passed in);
    · ``eta_s`` = elapsed·(1/frac − 1) once frac is past a small floor (≈2%) so the
      first noisy ticks don't emit a wild estimate — ``None`` before that.

    ⚠️ ``out_time_ms`` here is REAL milliseconds: the caller already converted
    ffmpeg's ``out_time_us`` (microseconds) by /1000. ffmpeg's own ``out_time_ms``
    field is historically microseconds and is NOT used.
    """
    total = total_ms if isinstance(total_ms, int) and total_ms > 0 else 0
    out_ms = max(0, int(out_time_ms))
    if total > 0:
        out_ms = min(out_ms, total)
        frac = out_ms / total
    else:
        frac = 0.0
    percent = max(0.0, min(100.0, frac * 100.0))

    # current chapter = first whose cumulative END (ms) > out_time_ms.
    cur_idx: int | None = None
    cur_name: str | None = None
    acc = 0
    for ch in chapters:
        dur = ch.get("duration_ms")
        if not (isinstance(dur, int) and dur > 0):
            continue
        acc += dur
        if acc > out_ms:
            cur_idx = int(ch.get("index")) if isinstance(ch.get("index"), int) else None
            cur_name = str(ch.get("name") or "") or None
            break
    if cur_idx is None and chapters:
        # Past the last boundary (finishing the final chapter) → pin to the last.
        last = chapters[-1]
        cur_idx = int(last.get("index")) if isinstance(last.get("index"), int) else None
        cur_name = str(last.get("name") or "") or None

    elapsed = max(0.0, time.monotonic() - start_monotonic)
    eta: int | None = None
    if frac > 0.02:
        eta = int(round(elapsed * (1.0 / frac - 1.0)))
        if eta < 0:
            eta = 0

    return {
        "percent": round(percent, 1),
        "out_time_ms": out_ms,
        "total_ms": total,
        "elapsed_s": int(round(elapsed)),
        "eta_s": eta,
        "current_chapter_index": cur_idx,
        "current_chapter_name": cur_name,
        "total_chapters": len([c for c in chapters
                               if isinstance(c.get("duration_ms"), int)
                               and c["duration_ms"] > 0]),
    }


def _parse_progress_out_time_ms(block: dict) -> int | None:
    """Pull a position (REAL ms) out of one ffmpeg ``-progress`` key/value block.

    Prefers ``out_time_us`` (microseconds → /1000), the reliable field. Falls back
    to parsing ``out_time`` (``HH:MM:SS.micros``) if ``out_time_us`` is absent/odd.
    Deliberately ignores ffmpeg's ``out_time_ms`` — it is microseconds in practice
    (a long-standing quirk noted in synthesis), so trusting it would 1000× the
    position. Returns ``None`` when no position can be read from this block.
    """
    us = block.get("out_time_us") or block.get("out_time_ms")  # see note: us first
    # NOTE: we only consult out_time_ms as a LAST resort and still treat it as µs,
    # because that is what ffmpeg actually writes there. out_time_us is canonical.
    if "out_time_us" in block:
        try:
            return int(int(block["out_time_us"]) / 1000)
        except (TypeError, ValueError):
            pass
    ot = block.get("out_time")
    if isinstance(ot, str) and ":" in ot:
        try:
            hh, mm, ss = ot.split(":")
            secs = int(hh) * 3600 + int(mm) * 60 + float(ss)
            return int(secs * 1000)
        except (TypeError, ValueError):
            pass
    # Last resort: out_time_ms is microseconds in ffmpeg → /1000.
    if us is not None:
        try:
            return int(int(us) / 1000)
        except (TypeError, ValueError):
            return None
    return None


def _run_ffmpeg(
    argv: list[str], *, reason_on_fail: str, book_id: object = None,
    total_ms: int = 0, chapters: list[dict] | None = None,
    progress_cb=None, output_path: "Path | None" = None,
    timeout_s: float | None = None, reason_on_timeout: str = "timeout",
) -> None:
    """Run ffmpeg (argv array) interruptibly; raise on failure OR cancel.

    We launch ffmpeg via :class:`subprocess.Popen` (NOT ``subprocess.run``) so the
    encode is *cancellable*: a poll loop wakes every ``CANCEL_POLL_INTERVAL_S`` and
    checks (a) whether a ``cancel`` command now targets ``book_id`` — if so we kill
    our own child and raise :class:`BuildCancelled` — and (b) the overall
    ``BUILD_TIMEOUT_S`` ceiling. The ffmpeg argv and atomicity are unchanged, so a
    SUCCESSFUL build is identical to before — only the wait is interruptible and,
    when ``progress_cb`` is given, the live position is reported.

    Live progress (Task 2): the caller adds ``-progress pipe:1 -nostats`` to ``argv``
    so ffmpeg streams ``key=value`` blocks to STDOUT. A daemon reader thread parses
    those blocks (``out_time_us`` → real ms — NOT ffmpeg's microsecond
    ``out_time_ms``), turns each into the :func:`_progress_snapshot` contract dict
    against ``total_ms``/``chapters`` and a monotonic start anchor, and hands it to
    ``progress_cb``. The cb itself THROTTLES/persists (dispatcher side); the reader
    only computes. stderr is still captured separately for the failure tail. When
    ``progress_cb`` is ``None`` the reader still drains the pipes (so ffmpeg never
    blocks on a full stdout buffer) but computes nothing.

    Interruption + wedge protection (M3). The same poll loop now watches three
    things besides the child's exit:
      · :func:`_interrupt_if_shutdown` — a TERM/INT/HUP reached the agent
        (``launchctl bootout``): kill the child, raise :class:`BuildInterrupted`.
        Checked FIRST — the process is going away regardless, and we have only
        launchd's exit timeout to get ffmpeg down before the SIGKILL;
      · the cooperative cancel (unchanged, D13);
      · :class:`_StallGuard` — no forward progress for :data:`BUILD_STALL_S` while
        ffmpeg sits alive (addendum §4.4): same teardown, ``cause="stall"``.
    ``output_path`` (the temp ffmpeg writes) gives the guard its second liveness
    signal, so a long ``+faststart`` finalization is never mistaken for a wedge.

    ``timeout_s`` / ``reason_on_timeout`` let a caller with a different ceiling
    (``split``: minutes, not hours) reuse this runner without inheriting the
    6-hour build timeout or its reason string.

    Outcomes:
      - exit 0 → return (success, unchanged);
      - non-zero exit / missing binary / OS error / timeout → :class:`BuildError`
        with ``reason_on_fail`` and a short stderr tail (the diagnostic);
      - a pending cancel for ``book_id`` → child SIGTERM→SIGKILL'd and reaped, then
        :class:`BuildCancelled` raised (the caller sweeps the temp);
      - a signal / a stall → child SIGTERM→SIGKILL'd and reaped, then
        :class:`BuildInterrupted` (a ``BuildError`` with ``reason="interrupted"``).
    """
    chapters = chapters or []
    # A signal that arrived before we even spawned: do not start a new encoder we
    # would have to kill a moment later (launchd is already counting down to KILL).
    _interrupt_if_shutdown([])
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        raise BuildError("ffmpeg_missing", "ffmpeg not found on PATH")
    except OSError as exc:
        raise BuildError("ffmpeg_oserror", repr(exc))

    start_monotonic = time.monotonic()

    # --- stdout reader thread: parse -progress blocks → progress_cb ----------
    # ffmpeg emits repeated key=value lines; a block ends on a ``progress=`` line
    # (``continue`` while running, ``end`` at the close). We accumulate keys until
    # that terminator, then emit one snapshot. Reading in a thread keeps the main
    # loop's cancel cadence crisp and prevents ffmpeg from blocking on a full pipe.
    stderr_chunks: list[str] = []

    def _drain_stderr() -> None:
        try:
            if proc.stderr is not None:
                for line in proc.stderr:
                    stderr_chunks.append(line)
        except (OSError, ValueError):
            pass

    # Latest position (ms) the reader thread saw — read by the stall guard in the
    # poll loop below. Parsed ALWAYS (not only when progress_cb is set), because the
    # wedge detector needs the liveness signal even for a caller that shows no bar.
    pos_box: dict[str, int] = {"ms": -1}

    def _read_progress() -> None:
        block: dict = {}
        try:
            if proc.stdout is None:
                return
            for raw in proc.stdout:
                line = raw.strip()
                if not line or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip()
                if key == "progress":
                    # Terminator: compute + emit one snapshot for this block.
                    out_ms = _parse_progress_out_time_ms(block)
                    if out_ms is not None:
                        pos_box["ms"] = out_ms          # liveness for _StallGuard
                        if progress_cb is not None:
                            try:
                                progress_cb(_progress_snapshot(
                                    out_ms, total_ms, chapters, start_monotonic))
                            except Exception:
                                pass  # a cb hiccup must never break the encode
                    block = {}
                else:
                    block[key] = val
        except (OSError, ValueError):
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    progress_thread = threading.Thread(target=_read_progress, daemon=True)
    stderr_thread.start()
    progress_thread.start()

    ceiling_s = BUILD_TIMEOUT_S if timeout_s is None else float(timeout_s)
    deadline = start_monotonic + ceiling_s
    watched = (output_path,) if output_path is not None else ()
    stall = _StallGuard()
    while True:
        try:
            proc.wait(timeout=CANCEL_POLL_INTERVAL_S)
            break  # child exited — fall through to returncode handling
        except subprocess.TimeoutExpired:
            pass  # still running — run the periodic checks below

        # (a) process shutdown (M3): a signal outranks everything — we are leaving,
        #     and ffmpeg must not outlive us as an orphan writing into a dead temp.
        _interrupt_if_shutdown([proc], book_id)

        # (b) cooperative cancel: a cancel command landed for this book.
        if _cancel_requested(book_id):
            _terminate_ffmpeg(proc)
            raise BuildCancelled(book_id)

        # (c) progress deadline (addendum §4.4): alive but frozen (e.g. blocked
        #     writing into a folder we have no access to) → same teardown.
        if stall.stalled(pos_box["ms"], watched):
            raise _interrupted("stall", stall.detail(), [proc], book_id)

        # (d) hard ceiling: a wedged ffmpeg must not hang the agent forever.
        if time.monotonic() >= deadline:
            _terminate_ffmpeg(proc)
            raise BuildError(reason_on_timeout, f"ffmpeg exceeded {int(ceiling_s)}s")

    # Child exited on its own — let the reader threads finish draining the pipes.
    progress_thread.join(timeout=CANCEL_TERM_GRACE_S)
    stderr_thread.join(timeout=CANCEL_TERM_GRACE_S)
    stderr = "".join(stderr_chunks)

    if proc.returncode != 0:
        tail = (stderr or "").strip().splitlines()[-3:]
        raise BuildError(reason_on_fail, " | ".join(tail) or f"exit {proc.returncode}")


def _build_with_filter(
    sources: list[Path],
    metadata_path: Path,
    cover_path: Path | None,
    tmp_out: Path,
    *,
    samplerate: int,
    layout: str,
    channels: int,
    bitrate_kbps: int,
    book_id: object = None,
    total_ms: int = 0,
    chapters: list[dict] | None = None,
    progress_cb=None,
) -> None:
    """Assemble via the concat *filter* (the default, drift-free path).

    Inputs, in order: the N chapter mp3s, then the FFMETADATA file, then
    (optionally) the cover image. The filter graph is supplied via a temp
    ``filter_complex_script`` so a book with many chapters does not push a giant
    filter string through argv. ``-progress pipe:1 -nostats`` streams the live
    position so :func:`_run_ffmpeg` can report it via ``progress_cb`` (Task 2).
    """
    n = len(sources)
    meta_idx = n          # FFMETADATA is input #n
    cover_idx = n + 1     # cover (if any) is input #n+1

    script_text = _filter_complex_script(n, samplerate, layout)
    # The script file lives next to the output temp so it is cleaned with it.
    script_path = tmp_out.parent / f".{tmp_out.name}.filter"
    script_path.write_text(script_text, encoding="utf-8")

    # ``-progress pipe:1 -nostats`` → machine-readable progress on STDOUT (Task 2).
    argv = [_ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-progress", "pipe:1", "-nostats"]
    for src in sources:
        argv += ["-i", str(src)]
    argv += ["-i", str(metadata_path)]
    if cover_path is not None:
        argv += ["-i", str(cover_path)]

    argv += ["-filter_complex_script", str(script_path)]
    argv += ["-map", "[aout]"]
    if cover_path is not None:
        argv += ["-map", f"{cover_idx}:v"]
    argv += ["-map_metadata", str(meta_idx)]

    # AAC encoder: Apple aac_at (fast/HQ, CBR) when available, else built-in aac
    # (synthesis Ступень 1). CBR so the size estimate / disk gate stay accurate.
    argv += _audio_encoder_args(_encoder(), bitrate_kbps=bitrate_kbps)
    argv += [
        "-ar", str(samplerate),
        "-ac", str(channels),
    ]
    if cover_path is not None:
        # Transcode the cover to mjpeg (NOT -c:v copy): the chain can hand us a PNG
        # (generated / a user-replaced png) and the m4b cover-art convention is JPEG
        # (research §1c). mjpeg-encoding one still is cheap and yields a consistent
        # attached_pic regardless of the source format (png/jpg).
        argv += ["-c:v", "mjpeg", "-disposition:v", "attached_pic"]

    argv += ["-f", "ipod", "-movflags", "+faststart", str(tmp_out)]

    try:
        _run_ffmpeg(argv, reason_on_fail="ffmpeg_concat_filter_failed",
                    book_id=book_id, total_ms=total_ms, chapters=chapters,
                    progress_cb=progress_cb, output_path=tmp_out)
    finally:
        _unlink_quiet(script_path)


def _build_with_demuxer(
    sources: list[Path],
    metadata_path: Path,
    cover_path: Path | None,
    tmp_out: Path,
    *,
    samplerate: int,
    layout: str,
    channels: int,
    bitrate_kbps: int,
    book_id: object = None,
    total_ms: int = 0,
    chapters: list[dict] | None = None,
    progress_cb=None,
) -> None:
    """Fallback for very many chapters: normalized concat *demuxer* (research §1a).

    The demuxer reads a list file and opens inputs sequentially (not all at once),
    so it stays under the fd limit on hundreds of files. We still re-encode to AAC
    at the target SR/channels with ``-ar``/``-ac`` applied to the OUTPUT so the
    join is normalized; ``concat`` demuxer + an explicit output format avoids the
    per-chapter drift the filter path also avoids, at the cost of a tiny join
    overhead. Used only above ``CONCAT_FILTER_MAX_CHAPTERS``. ``-progress pipe:1
    -nostats`` streams the live position for ``progress_cb`` (Task 2) — the long
    (many-chapter) book is exactly where the determinate bar matters most.
    """
    # concat-demuxer list file (paths must be quoted/escaped per its mini-format).
    list_path = tmp_out.parent / f".{tmp_out.name}.concat"
    lines = []
    for src in sources:
        # The demuxer's own escaping: backslash-escape single quotes.
        safe = str(src.resolve()).replace("'", "'\\''")
        lines.append(f"file '{safe}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    argv = [
        _ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-progress", "pipe:1", "-nostats",   # live progress on STDOUT (Task 2)
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-i", str(metadata_path),
    ]
    cover_idx = 2
    if cover_path is not None:
        argv += ["-i", str(cover_path)]

    argv += ["-map", "0:a", "-map_metadata", "1"]
    if cover_path is not None:
        argv += ["-map", f"{cover_idx}:v"]

    # AAC encoder: Apple aac_at (CBR) when available, else built-in aac — same as
    # the filter path (synthesis Ступень 1). CBR keeps the size estimate honest.
    argv += _audio_encoder_args(_encoder(), bitrate_kbps=bitrate_kbps)
    argv += [
        "-ar", str(samplerate),
        "-ac", str(channels),
    ]
    if cover_path is not None:
        # mjpeg, not copy — see _build_with_filter (the cover may be a PNG; m4b
        # cover art is JPEG by convention, research §1c).
        argv += ["-c:v", "mjpeg", "-disposition:v", "attached_pic"]

    argv += ["-f", "ipod", "-movflags", "+faststart", str(tmp_out)]

    try:
        _run_ffmpeg(argv, reason_on_fail="ffmpeg_concat_demuxer_failed",
                    book_id=book_id, total_ms=total_ms, chapters=chapters,
                    progress_cb=progress_cb, output_path=tmp_out)
    finally:
        _unlink_quiet(list_path)


# ── Fast (parallel-groups) path ──────────────────────────────────────────────


def _encode_group_argv(
    group_sources: list[Path], out_fragment: Path,
    *, samplerate: int, layout: str, channels: int, bitrate_kbps: int,
) -> list[str]:
    """ffmpeg argv to encode ONE group's chapters into a single continuous ``.m4a``.

    The group's chapters are concatenated with the SAME drift-free recipe the
    single-pass path uses — ``aformat`` normalizes each input to the target
    SR/layout, then ``concat`` joins them (research §1a) — and re-encoded to AAC
    (Apple ``aac_at`` CBR when available, else built-in ``aac``; synthesis Ступень
    1). Output invariants that make the later ``-c copy`` concat valid + drift-free:
      · ``-f ipod`` → an ``.m4a`` (MP4) fragment that stores accurate edit-list /
        timing (synthesis §"промежуточный контейнер только .m4a/mp4, НЕ ADTS .aac");
      · ``-vn`` → NO cover/video in the fragment (the cover is burned once, on the
        final concat), so every fragment is pure audio with identical stream layout;
      · ``-movflags +faststart`` on the fragment too — cheap, and keeps ffprobe's
        duration read fast/robust.
    The filter graph is passed inline (a group is a handful of files — well under any
    argv limit) so no scratch script file is needed per group. All ``-i`` are reads
    only (I1).
    """
    n = len(group_sources)
    parts = []
    labels = []
    for i in range(n):
        parts.append(
            f"[{i}:a]aformat=sample_rates={samplerate}:channel_layouts={layout}[a{i}]"
        )
        labels.append(f"[a{i}]")
    parts.append(f"{''.join(labels)}concat=n={n}:v=0:a=1[aout]")
    filter_graph = ";".join(parts)

    argv = [_ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-progress", "pipe:1", "-nostats"]
    for src in group_sources:
        argv += ["-i", str(src)]
    argv += ["-filter_complex", filter_graph, "-map", "[aout]", "-vn"]
    argv += _audio_encoder_args(_encoder(), bitrate_kbps=bitrate_kbps)
    argv += ["-ar", str(samplerate), "-ac", str(channels)]
    argv += ["-f", "ipod", "-movflags", "+faststart", str(out_fragment)]
    return argv


def _fragment_streams(path: Path) -> tuple[str, int, int] | None:
    """(codec_name, sample_rate, channels) of a fragment's first audio stream.

    Read back with ffprobe so we can VERIFY every group fragment shares the same
    codec/SR/channels before a ``-c copy`` concat (mismatched streams cannot be
    stream-copied into one valid track — synthesis §"валидировать единые
    SR/каналы/кодек фрагментов перед -c copy"). Returns ``None`` on any failure /
    no audio stream; the caller treats that as a fast-path abort → fallback.
    """
    try:
        out = subprocess.run(
            [_ffprobe_bin(), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,sample_rate,channels",
             "-of", "default=noprint_wrappers=1:nokey=0", str(path)],
            capture_output=True, text=True, timeout=FRAGMENT_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    codec = ""
    sr = 0
    ch = 0
    for line in (out.stdout or "").splitlines():
        key, _, val = line.strip().partition("=")
        if key == "codec_name":
            codec = val.strip()
        elif key == "sample_rate":
            try:
                sr = int(val)
            except (TypeError, ValueError):
                sr = 0
        elif key == "channels":
            try:
                ch = int(val)
            except (TypeError, ValueError):
                ch = 0
    if not codec or sr <= 0 or ch <= 0:
        return None
    return (codec, sr, ch)


def _run_group_pool(
    groups: list[dict], chunks_dir: Path,
    *, samplerate: int, layout: str, channels: int, bitrate_kbps: int,
    workers: int, book_id: object, total_ms: int, flat_chapters: list[dict],
    progress_cb=None,
) -> list[Path]:
    """Encode every group to a ``.m4a`` fragment via a bounded Popen pool.

    Runs at most ``workers`` ffmpeg encoders at once (one per group of consecutive
    chapters). The pool loop is *cancellable + progress-reporting* exactly like the
    single-pass :func:`_run_ffmpeg`, but across N children:
      · every ``CANCEL_POLL_INTERVAL_S`` it checks for a ``cancel`` command targeting
        ``book_id`` — on a hit it SIGTERM→SIGKILLs ALL live children
        (:func:`_terminate_ffmpeg_many`) and raises :class:`BuildCancelled`;
      · it enforces the overall :data:`BUILD_TIMEOUT_S` ceiling the same way;
      · each child streams ``-progress`` on stdout; a per-child reader thread tracks
        that child's live position, and the loop aggregates
        Σ(finished groups' planned_ms) + Σ(live partials) into ONE snapshot against
        the WHOLE book timeline (``total_ms`` / ``flat_chapters``) via
        :func:`_progress_snapshot`, so the determinate bar advances smoothly across
        the parallel encode using the SAME state.json contract (UI unchanged).

    Returns the fragment paths in GROUP ORDER (index order) — the order the concat
    must use. Raises :class:`_FastPathUnusable` if any group exits non-zero / a
    fragment is missing/empty (→ the caller falls back to single-encode). A launch
    failure (ffmpeg missing) also aborts to fallback. Children are always reaped.
    """
    n = len(groups)
    fragments: list[Path] = [chunks_dir / f"group-{g['index']:04d}.m4a"
                             for g in groups]

    # Per-group live position (ms) the reader threads publish; the loop sums them.
    live_ms: list[int] = [0] * n
    live_lock = threading.Lock()
    start_monotonic = time.monotonic()

    procs: dict[int, subprocess.Popen] = {}       # group index → running Popen
    readers: dict[int, threading.Thread] = {}
    stderr_tails: dict[int, list[str]] = {}
    next_to_launch = 0

    def _reader_for(idx: int, proc: subprocess.Popen) -> None:
        block: dict = {}
        try:
            if proc.stdout is None:
                return
            for raw in proc.stdout:
                line = raw.strip()
                if not line or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip(); val = val.strip()
                if key == "progress":
                    pos = _parse_progress_out_time_ms(block)
                    if pos is not None:
                        with live_lock:
                            live_ms[idx] = pos
                    block = {}
                else:
                    block[key] = val
        except (OSError, ValueError):
            pass

    def _stderr_for(idx: int, proc: subprocess.Popen) -> None:
        try:
            if proc.stderr is not None:
                for line in proc.stderr:
                    stderr_tails[idx].append(line)
        except (OSError, ValueError):
            pass

    done_planned = 0            # Σ planned_ms of fully-finished groups
    deadline = start_monotonic + BUILD_TIMEOUT_S
    stall = _StallGuard()       # wedge detector across the WHOLE pool (addendum §4.4)

    def _emit_progress() -> None:
        if progress_cb is None:
            return
        with live_lock:
            live_total = sum(min(live_ms[i], int(groups[i]["planned_ms"]))
                             for i in procs)
        pos = done_planned + live_total
        try:
            progress_cb(_progress_snapshot(pos, total_ms, flat_chapters,
                                           start_monotonic))
        except Exception:
            pass

    try:
        while next_to_launch < n or procs:
            # A signal before/between launches: stop filling the pool at once (M3).
            _interrupt_if_shutdown(list(procs.values()), book_id)
            # Fill the pool up to ``workers``.
            while len(procs) < workers and next_to_launch < n:
                idx = next_to_launch
                argv = _encode_group_argv(
                    groups[idx]["sources"], fragments[idx],
                    samplerate=samplerate, layout=layout,
                    channels=channels, bitrate_kbps=bitrate_kbps,
                )
                try:
                    proc = subprocess.Popen(
                        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True,
                    )
                except (FileNotFoundError, OSError) as exc:
                    _terminate_ffmpeg_many(list(procs.values()))
                    raise _FastPathUnusable(f"group_launch_failed:{exc!r}")
                procs[idx] = proc
                stderr_tails[idx] = []
                rt = threading.Thread(target=_reader_for, args=(idx, proc),
                                      daemon=True)
                et = threading.Thread(target=_stderr_for, args=(idx, proc),
                                      daemon=True)
                rt.start(); et.start()
                readers[idx] = rt
                next_to_launch += 1

            # Poll the running children for exit / shutdown / cancel / stall / timeout.
            time.sleep(CANCEL_POLL_INTERVAL_S)

            # (a) process shutdown (M3) — gas the WHOLE pool, not one child.
            _interrupt_if_shutdown(list(procs.values()), book_id)
            if _cancel_requested(book_id):
                _terminate_ffmpeg_many(list(procs.values()))
                raise BuildCancelled(book_id)
            # (c) progress deadline: the aggregate position across the pool plus the
            #     live fragments' file signatures — a frozen pool moves neither.
            with live_lock:
                pooled_ms = done_planned + sum(live_ms[i] for i in procs)
            if stall.stalled(pooled_ms, [fragments[i] for i in procs]):
                raise _interrupted("stall", stall.detail(),
                                   list(procs.values()), book_id)
            if time.monotonic() >= deadline:
                _terminate_ffmpeg_many(list(procs.values()))
                raise BuildError("timeout", f"ffmpeg exceeded {BUILD_TIMEOUT_S}s")

            # Reap any that finished this tick.
            for idx in [i for i, p in procs.items() if p.poll() is not None]:
                proc = procs.pop(idx)
                readers.get(idx, threading.Thread()).join(timeout=CANCEL_TERM_GRACE_S)
                if proc.returncode != 0:
                    tail = "".join(stderr_tails.get(idx, [])).strip().splitlines()[-3:]
                    _terminate_ffmpeg_many(list(procs.values()))
                    raise _FastPathUnusable(
                        f"group_{idx}_exit_{proc.returncode}:{' | '.join(tail)}")
                done_planned += int(groups[idx]["planned_ms"])

            _emit_progress()
    finally:
        # Belt-and-suspenders: never leave a child running on ANY exit path.
        if procs:
            _terminate_ffmpeg_many(list(procs.values()))

    # Every group must have produced a non-empty fragment.
    for idx, frag in enumerate(fragments):
        if not frag.exists() or frag.stat().st_size == 0:
            raise _FastPathUnusable(f"group_{idx}_empty_fragment")
    return fragments


def _build_with_parallel_groups(
    manifest: dict,
    chapters: list[dict],
    sources: list[Path],
    cover_path: Path | None,
    tmp_out: Path,
    *,
    samplerate: int,
    layout: str,
    channels: int,
    bitrate_kbps: int,
    workers: int,
    book_id: object = None,
    total_ms: int = 0,
    progress_cb=None,
) -> None:
    """Fast path: parallel group encodes → concat ``-c copy`` (synthesis Ступень 2).

    Pipeline:
      1. plan ≈``workers`` balanced groups of CONSECUTIVE chapters
         (:func:`_plan_encode_groups`);
      2. encode each group to a ``.m4a`` fragment in a hidden ``.<name>.*.chunks/``
         dir via a bounded Popen pool (:func:`_run_group_pool`) — cancellable +
         progress-reporting;
      3. **validate** the fragments: ffprobe each for (codec, SR, channels) and
         require them IDENTICAL (a mismatch cannot be stream-copied into one valid
         track), and read each fragment's MEASURED duration;
      4. concat the fragments (audio only, ``-c copy``, NO re-encode — the whole
         point of the speed-up) into one joined ``.m4a``, then ffprobe its TRUE
         total. **Drift guard:** that total must equal Σ(measured fragments) within
         ~one frame PER SEAM (:data:`FAST_DRIFT_TOLERANCE_MS` × seams) — else the
         timing model drifted → fall back;
      5. build chapter marks from the MEASURED durations (:func:`_chapter_marks`),
         SNAP the last END to the probed container total (chapters span the file
         exactly), then remux the joined audio + those chapters (``-map_chapters 1``)
         + the cover (``attached_pic`` mjpeg) into ``tmp_out`` (``-c copy``,
         ``-f ipod -movflags +faststart``). Both copies are near-instant.

    On ANY correctness breach (a group failed, a fragment unprobeable, streams
    disagree, drift too large) it raises :class:`_FastPathUnusable` so :func:`build`
    falls back to the single-pass path. :class:`BuildCancelled` / :class:`BuildError`
    (timeout) propagate unchanged. The chunks dir is always swept. Sources are read
    only (I1).
    """
    chunks_dir = tmp_out.parent / f".{tmp_out.name}.chunks"
    try:
        chunks_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _FastPathUnusable(f"chunks_dir_unwritable:{exc!r}")

    try:
        groups = _plan_encode_groups(chapters, sources, workers)
        if len(groups) < 2:
            # One group ⇒ a single continuous encode with no seams — nothing the
            # parallel path adds. Let the caller use the seamless single path.
            raise _FastPathUnusable("only_one_group")

        fragments = _run_group_pool(
            groups, chunks_dir,
            samplerate=samplerate, layout=layout, channels=channels,
            bitrate_kbps=bitrate_kbps, workers=workers, book_id=book_id,
            total_ms=total_ms, flat_chapters=chapters, progress_cb=progress_cb,
        )

        # --- validate uniform streams across fragments (needed for -c copy) -----
        ref = _fragment_streams(fragments[0])
        if ref is None:
            raise _FastPathUnusable("fragment_0_unprobeable")
        for idx in range(1, len(fragments)):
            got = _fragment_streams(fragments[idx])
            if got is None:
                raise _FastPathUnusable(f"fragment_{idx}_unprobeable")
            if got != ref:
                raise _FastPathUnusable(
                    f"fragment_{idx}_stream_mismatch:{got}!={ref}")

        # --- measured per-fragment (group) durations ----------------------------
        # A fragment holds a GROUP of consecutive chapters. Measure each group's
        # real (encoded) duration; the per-CHAPTER measured length is that group
        # duration distributed across its chapters in proportion to their SOURCE
        # durations (the last chapter in a group absorbs the rounding remainder so a
        # group's chapter ENDs sum EXACTLY to its measured duration).
        measured_chapter_ms: list[int] = []
        for g, frag in zip(groups, fragments):
            gm = _probe_media_duration_ms(frag)
            if gm is None or gm <= 0:
                raise _FastPathUnusable(f"group_{g['index']}_unmeasurable")
            src_ms = [int(c.get("duration_ms") or 0) for c in g["chapters"]]
            src_sum = sum(src_ms) or 1
            acc = 0
            for j, c_src in enumerate(src_ms):
                if j == len(src_ms) - 1:
                    part = gm - acc          # last chapter takes the remainder
                else:
                    part = int(round(gm * (c_src / src_sum)))
                    acc += part
                measured_chapter_ms.append(max(0, part))
        sum_fragments = sum(measured_chapter_ms)

        # --- 1) concat the fragments (audio only, stream-copy) to a temp .m4a ----
        # Two stream-copies (concat, then a metadata remux) — both near-instant, so
        # the extra one is negligible even on a multi-hour book (measured: identical
        # wall time to a single pass) — let us read the TRUE concatenated duration
        # BEFORE writing the chapter marks. That way the last chapter END lands
        # EXACTLY on the real end of the file (no tail gap), and the drift guard
        # checks the SHIPPED audio.
        list_path = chunks_dir / "concat.txt"
        lines = []
        for frag in fragments:
            safe = str(frag.resolve()).replace("'", "'\\''")
            lines.append(f"file '{safe}'")
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        joined = chunks_dir / "joined.m4a"
        concat_argv = [
            _ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-map", "0:a", "-c:a", "copy",
            "-f", "ipod", "-movflags", "+faststart", str(joined),
        ]
        # No progress_cb on the two finalize stream-copies: the PARALLEL ENCODE is
        # ~all the wall time and already drove the bar to ~100% (its snapshots cover
        # the whole timeline); the concat + remux are near-instant copies, so
        # re-reporting them (0→100% again vs the same total) would visibly RESET the
        # bar. Kept silent + still cancellable/bounded — the bar holds at the encode's
        # final value until build_done clears the row.
        _run_ffmpeg(concat_argv, reason_on_fail="ffmpeg_concat_copy_failed",
                    book_id=book_id, output_path=joined)
        if not joined.exists() or joined.stat().st_size == 0:
            raise _FastPathUnusable("joined_empty")

        # --- 2) probe the TRUE concatenated duration; drift guard ---------------
        container_total = _probe_media_duration_ms(joined)
        if container_total is None or container_total <= 0:
            raise _FastPathUnusable("joined_unmeasurable")
        # The concat demuxer accumulates a tiny AAC priming gap at EACH inter-group
        # seam, so the real container is a hair longer than Σ(fragment durations) —
        # bounded by the seam count (workers−1), the very property groups buy us
        # (synthesis: seams = workers−1, NOT chapter count). If the concatenated total
        # diverges from Σ(measured fragments) by more than ~one frame PER SEAM, our
        # timing model drifted (source-style, unbounded) → do NOT ship these chapters:
        # fall back to the single-encode path (which is bit-exact).
        seams = max(1, len(groups) - 1)
        drift_budget = FAST_DRIFT_TOLERANCE_MS * seams
        drift = abs(container_total - sum_fragments)
        if drift > drift_budget:
            raise _FastPathUnusable(f"drift_{drift}ms>{drift_budget}")

        # Chapter marks from the MEASURED fragment durations (synthesis §"метки глав
        # из ИЗМЕРЕННОЙ длительности"): per-chapter error is bounded by the SEAM
        # count, not the chapter count → no accumulating drift. Snap the LAST chapter
        # END to the true container end so the chapters span the whole file EXACTLY
        # (the seam slack is absorbed into the final chapter, never a tail gap).
        marks = _chapter_marks(chapters, measured_chapter_ms)
        if marks:
            marks[-1]["end"] = max(marks[-1]["start"], container_total)

        # --- 3) remux joined audio + chapters + cover → tmp_out (stream-copy) ----
        metadata_path = chunks_dir / "chapters.ffmeta"
        metadata_path.write_text(_ffmetadata_from_marks(manifest, marks),
                                 encoding="utf-8")

        argv = [
            _ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(joined),
            "-i", str(metadata_path),
        ]
        cover_idx = 2
        if cover_path is not None:
            argv += ["-i", str(cover_path)]
        # Audio copied verbatim (no re-encode — the speed-up); chapters ONLY from the
        # FFMETADATA file (-map_chapters 1) so the joined file's own (absent) chapters
        # cannot leak in as duplicates/phantoms (research §3 grabli, as in split.py).
        argv += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
        if cover_path is not None:
            argv += ["-map", f"{cover_idx}:v"]
        argv += ["-c:a", "copy"]
        if cover_path is not None:
            argv += ["-c:v", "mjpeg", "-disposition:v", "attached_pic"]
        argv += ["-f", "ipod", "-movflags", "+faststart", str(tmp_out)]

        # Silent finalize copy too (see the concat note) — cancellable + bounded.
        # This one writes STRAIGHT INTO the watched folder (tmp_out), so it is the
        # step the addendum's progress deadline is really aimed at.
        _run_ffmpeg(argv, reason_on_fail="ffmpeg_remux_copy_failed", book_id=book_id,
                    output_path=tmp_out)
    finally:
        _cleanup_temp_tree(chunks_dir)


def _cleanup_temp_tree(path: Path) -> None:
    """Recursively remove a temp DIRECTORY tree, swallowing errors (fast-path sweep).

    The fast path stages group fragments + the concat list + FFMETADATA inside a
    hidden ``.<name>.*.chunks/`` dir; on success OR any failure/cancel we ``rmtree``
    it so no scratch survives. Best-effort (a partly-removed tree on a racing FS is
    harmless — the next build uses a fresh unique dir).
    """
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def build(manifest: dict, *, out_path: Path | None = None, progress_cb=None) -> Path:
    """Build a ``.m4b`` from a validated manifest → atomic output path.

    Steps:
      1. resolve usable chapters (real file + positive duration) and the output
         path (next to the source folder, ``«Автор - Название».m4b``);
      2. write the FFMETADATA chapter file to a temp;
      3. run ffmpeg (concat filter, or demuxer fallback above the threshold) to a
         hidden temp ``.<name>.*.tmp`` in the output dir, burning in the SELECTED
         cover (``resolve_cover_path``: selected → embedded → first generated; any
         kind as ``attached_pic``) — a book is coverless only on a pre-chain
         manifest with no embedded preview;
      4. ``os.replace`` the temp onto the final path (atomic) and return it.

    ``progress_cb`` (Task 2): an optional callable invoked with the
    :func:`_progress_snapshot` contract dict as ffmpeg encodes (off the live
    ``-progress`` stream). The callback owns throttling/persistence (the dispatcher
    patches state.json). It runs on a reader thread, so it must be cheap + tolerant;
    a raise inside it is swallowed (it can never break the encode). ``None`` → no
    progress reporting, identical to before.

    On ANY failure a :class:`BuildError` is raised and every temp (output,
    filter/list/metadata scratch) is swept, so no half-written ``.m4b`` survives.
    On a cooperative cancel (a ``cancel`` command for this ``book_id`` lands while
    ffmpeg runs) a :class:`BuildCancelled` is raised instead — the ffmpeg child is
    SIGTERM→SIGKILL'd and reaped first, and the SAME ``except`` sweeps every temp,
    so a cancel also leaves NO half-written ``.m4b`` and NO orphaned ffmpeg.
    A process-level interrupt (M3) — a TERM/INT/HUP from ``launchctl bootout``, or a
    :data:`BUILD_STALL_S` stretch with no forward progress — raises
    :class:`BuildInterrupted` through the very same path: children killed and reaped
    first, then the identical temp sweep, so the guarantee "no orphan, no half
    ``.m4b``" holds for a killed agent exactly as it does for a cancel.
    Source mp3s are only read (I1). Raises :class:`BuildError` with no usable
    chapters.
    """
    # M3 entry gate: if a signal already asked us to stop, do NOT start a new
    # encode. The drain loop may still have queued books when the TERM lands, and
    # spawning ffmpeg while launchd counts down to SIGKILL is exactly how an orphan
    # is born. The book surfaces as ``error: interrupted`` — the same, honest state
    # a build killed mid-flight lands in — and is re-triggerable by the user.
    _interrupt_if_shutdown([], manifest.get("book_id"))

    # E3: a single unreadable chapter fails the WHOLE book — we never ship a
    # silently-partial .m4b. The user fixes / removes the offending file and a
    # re-scan (new source_rev) re-arms the book for a fresh build. The detail
    # carries the bad file name(s) so the banner can name them.
    unreadable = _unreadable_chapter_files(manifest)
    if unreadable:
        shown = ", ".join(unreadable[:5])
        if len(unreadable) > 5:
            shown += f" (+{len(unreadable) - 5})"
        raise BuildError("unreadable_chapter", shown)

    chapters = _usable_chapters(manifest)
    if not chapters:
        raise BuildError("no_usable_chapters", "no chapter has a readable duration")

    params = manifest.get("params") or {}
    # Keep the source sample rate by default (params.samplerate == None sentinel):
    # resolve against the manifest's recorded source SR so we don't resample unless
    # the user explicitly pinned 44.1 / 48 in the confirm window.
    samplerate = _samplerate(params, manifest.get("source_samplerate"))
    layout = _channel_layout(params)
    channels = _channels_count(params)
    bitrate = _bitrate_kbps(params)
    # Build mode (D15): "fast" (default) = parallel groups → concat stream-copy;
    # "seamless" = the single-pass encode. We take the fast path only when it can
    # actually win: ≥ FAST_MIN_CHAPTERS chapters (so ≥2 balanced groups) — a tiny
    # book is a single continuous encode either way (also seamless), so it routes to
    # the single path even in fast mode.
    mode = _build_mode(params)
    workers = _fast_worker_count(len(chapters))
    use_fast = (mode == BUILD_MODE_FAST
                and len(chapters) >= FAST_MIN_CHAPTERS
                and workers >= 2)
    # Pass the book id down to the ffmpeg runner so its poll loop can spot a
    # cancel command for THIS book and tear down its own child (D13).
    book_id = manifest.get("book_id")

    sources = _chapter_source_paths(manifest, chapters)
    missing = [str(s) for s in sources if not s.exists()]
    if missing:
        raise BuildError("source_missing", f"{len(missing)} chapter file(s) missing")

    final_path = Path(out_path) if out_path is not None else default_output_path(manifest)
    out_dir = final_path.parent
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BuildError("output_dir_unwritable", repr(exc))

    # E5: free-space pre-flight. Estimate the output size and require it (plus a
    # safety margin for mux overhead / estimate slop) to fit on the output volume
    # BEFORE we spawn ffmpeg — otherwise an ENOSPC surfaces postfactum as a
    # generic ffmpeg failure. Failing here (no_space) gives the user a precise,
    # actionable danger reason and leaves NO half-written file (we have not
    # created the temp yet). ``shutil.disk_usage`` is best-effort: if it cannot be
    # read (exotic fs / OS error) we skip the gate rather than block a real build.
    # The fast path needs ~2× headroom (fragments + final coexist at the concat
    # step, synthesis §disk); the seamless path needs the normal margin.
    _ensure_free_space(out_dir, manifest,
                       strategy="parallel" if use_fast else "single")

    # Cover (M1 — the chain landed): burn in the SELECTED cover (user pick from the
    # confirm window, or the agent default), resolved in priority order
    # selected → embedded → first generated (resolve_cover_path). Any kind
    # (embedded / web / generated / user custom) is burned as attached_pic. Only a
    # manifest with truly no usable cover on disk (pre-chain + no embedded) builds
    # without one — we must not crash here.
    cover_path: Path | None = resolve_cover_path(manifest)

    # Hidden temp siblings in the output dir → atomic rename, and they match the
    # ``.<name>.*`` sweep pattern recover_interrupted / the failure path use.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{final_path.name}.", suffix=".tmp", dir=str(out_dir)
    )
    os.close(fd)  # ffmpeg writes the file itself; we only needed a unique name
    tmp_out = Path(tmp_name)

    metadata_path = out_dir / f".{final_path.name}.ffmeta"

    # Total timeline (ms) for the progress percentage — Σ of usable chapter
    # durations (the same chapters we hand the encoder + the FFMETADATA writer).
    total_ms = sum(int(ch["duration_ms"]) for ch in chapters)

    def _run_seamless() -> None:
        """The single-pass (bit-exact) encode → ``tmp_out`` — the seamless mode AND
        the fast-path fallback. Writes its own source-duration FFMETADATA and picks
        the concat filter (default) or demuxer (very many chapters) exactly as
        before. Kept as a closure so both entry points share one recipe."""
        metadata_path.write_text(_ffmetadata_text(manifest, chapters),
                                 encoding="utf-8")
        if len(sources) > CONCAT_FILTER_MAX_CHAPTERS:
            _build_with_demuxer(
                sources, metadata_path, cover_path, tmp_out,
                samplerate=samplerate, layout=layout,
                channels=channels, bitrate_kbps=bitrate, book_id=book_id,
                total_ms=total_ms, chapters=chapters, progress_cb=progress_cb,
            )
        else:
            _build_with_filter(
                sources, metadata_path, cover_path, tmp_out,
                samplerate=samplerate, layout=layout,
                channels=channels, bitrate_kbps=bitrate, book_id=book_id,
                total_ms=total_ms, chapters=chapters, progress_cb=progress_cb,
            )

    try:
        if use_fast:
            # Fast path: parallel group encodes → concat stream-copy. On ANY
            # correctness breach (_FastPathUnusable — a group failed, a fragment
            # unprobeable, streams disagree, drift too large) fall back to the
            # single-pass path (synthesis Ступень 2). A cancel/timeout is NOT a
            # fallback — it propagates. Any half-written tmp_out from the aborted
            # fast attempt is removed before the fallback re-creates it.
            try:
                _build_with_parallel_groups(
                    manifest, chapters, sources, cover_path, tmp_out,
                    samplerate=samplerate, layout=layout, channels=channels,
                    bitrate_kbps=bitrate, workers=workers, book_id=book_id,
                    total_ms=total_ms, progress_cb=progress_cb,
                )
            except _FastPathUnusable as exc:
                state.append_event("fast_build_fallback", book_id=book_id,
                                   reason=exc.reason)
                _unlink_quiet(tmp_out)
                _run_seamless()
        else:
            _run_seamless()

        if not tmp_out.exists() or tmp_out.stat().st_size == 0:
            raise BuildError("empty_output", "ffmpeg produced no output file")

        # mkstemp created the temp 0600 and ffmpeg truncated it in place, so the
        # output would otherwise inherit owner-only perms. Relax to the normal
        # umask default (0644 & ~umask) so the .m4b behaves like a regular
        # user-created file in Finder / players. Best-effort.
        _apply_default_mode(tmp_out)

        # Atomic publish: temp → final. After this the .m4b appears all-at-once.
        os.replace(tmp_out, final_path)
    except BaseException:
        # Sweep every scratch file so a failure/cancel leaves no half-.m4b.
        _unlink_quiet(tmp_out)
        _unlink_quiet(metadata_path)
        raise
    else:
        _unlink_quiet(metadata_path)

    return final_path


def estimate_output_size(manifest: dict) -> int:
    """Rough output-size estimate (bytes) for the confirm window (research §3).

    Audio bytes ≈ ``bitrate_bps / 8 × total_seconds``; plus a small allowance for
    an embedded cover and mux overhead. Uses the manifest's target bitrate and
    summed chapter durations. Best-effort — for the UI estimate, not a guarantee.
    """
    params = manifest.get("params") or {}
    bitrate_bps = _bitrate_kbps(params) * 1000

    chapters = _usable_chapters(manifest)
    total_ms = sum(int(ch["duration_ms"]) for ch in chapters)
    total_seconds = total_ms / 1000.0

    audio_bytes = int(bitrate_bps / 8 * total_seconds)
    # A cover is almost always present now (the chain guarantees ≥1). Allow for it
    # when one resolves on disk; fall back to the embedded heuristic otherwise.
    has_cover = (
        resolve_cover_path(manifest) is not None
        or manifest.get("cover_state") == "embedded"
    )
    cover_bytes = 60 * 1024 if has_cover else 0
    overhead = 64 * 1024  # moov atom + chapter track + slack
    return audio_bytes + cover_bytes + overhead


def required_free_space(manifest: dict, strategy: str = "single") -> int:
    """Bytes the output volume must have free before a build may start (E5).

    The raw :func:`estimate_output_size` plus headroom. The multiplicative slop
    depends on the build strategy:
      · ``"single"`` (default, seamless / demuxer / filter) — only the final file
        plus mux slop is ever on disk, so :data:`SPACE_SAFETY_FACTOR` (×1.15);
      · ``"parallel"`` (fast path) — the per-group ``.m4a`` fragments AND the final
        concatenated ``.m4b`` coexist at the concat step, so the peak is ≈2× the
        final size → :data:`SPACE_SAFETY_FACTOR_PARALLEL` (×2.3, synthesis §disk).
    An absolute floor (so even a tiny book reserves room for the moov atom / temp
    churn) is always applied on top. This is the threshold the pre-flight compares
    the volume's free bytes against.
    """
    est = estimate_output_size(manifest)
    factor = (SPACE_SAFETY_FACTOR_PARALLEL if strategy == "parallel"
              else SPACE_SAFETY_FACTOR)
    return max(int(est * factor), est + SPACE_SAFETY_FLOOR_BYTES)


def _ensure_free_space(out_dir: Path, manifest: dict,
                       strategy: str = "single") -> None:
    """Raise :class:`BuildError` ``no_space`` if ``out_dir``'s volume can't fit it.

    Compares ``shutil.disk_usage(out_dir).free`` against
    :func:`required_free_space` (with the strategy-specific multiplier — the fast
    path reserves ~2× for the coexisting fragments + final). Best-effort: if free
    space cannot be read (an exotic filesystem, an OS error) we DO NOT block — a
    real build should never be stopped by a flaky stat, and a genuine ENOSPC still
    surfaces from ffmpeg as a clean failure with temp sweep. The ``detail`` reports
    the shortfall in MB so the danger banner is actionable without the agent
    inventing specifics.
    """
    try:
        free = shutil.disk_usage(str(out_dir)).free
    except OSError:
        return  # cannot tell → don't block; ffmpeg ENOSPC path is the backstop
    need = required_free_space(manifest, strategy=strategy)
    if free < need:
        need_mb = need / (1024 * 1024)
        free_mb = free / (1024 * 1024)
        raise BuildError(
            "no_space",
            f"need ~{need_mb:.0f} MB, free {free_mb:.0f} MB",
        )


def _apply_default_mode(path: Path) -> None:
    """Set ``path`` to the umask-respecting default mode (0666 & ~umask).

    The temp came from ``mkstemp`` (0600); a finished ``.m4b`` should be a normal
    file. We read the current umask (and restore it immediately) to compute the
    same mode a plain ``open(..., "w")`` would have produced. Best-effort.
    """
    try:
        cur = os.umask(0)
        os.umask(cur)
        os.chmod(path, 0o666 & ~cur)
    except OSError:
        pass


def _unlink_quiet(path: Path) -> None:
    """Remove a file if present, swallowing errors (cleanup helper)."""
    try:
        Path(path).unlink()
    except OSError:
        pass
