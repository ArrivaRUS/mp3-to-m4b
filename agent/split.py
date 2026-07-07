"""Optional post-build splitting of an ``.m4b`` along chapter boundaries (P1).

This is the LAST feature (P1 / v1.1, plans.md M1 last bullet, arch/synthesis.md §C,
research/m4b-toolchain.md §3): when a book is too large for one file, cut the
ALREADY-BUILT ``.m4b`` into parts on chapter boundaries, **stream-copy** (no
re-encode — fast, lossless) and give every part its own rebased chapters, cover
and "Часть N из M" title/track.

Two pure-ish steps the dispatcher orchestrates after a normal build:

  :func:`plan_parts`   group CONSECUTIVE chapters so each part's estimated size is
                       ≤ the threshold, cutting ONLY on chapter boundaries. The
                       per-chapter size estimate reuses the build engine's
                       bitrate×duration coefficient (``build_m4b`` is the single
                       source of that math). **Edge E15:** a single chapter that
                       alone exceeds the threshold becomes its own part and is
                       flagged ``oversize`` (the UI later warns; we never split
                       mid-chapter). Returns a list of part descriptors with
                       ``index``/``total``/``chapter_indices``/``start_ms``/
                       ``end_ms``/``est_size``/``oversize``.

  :func:`split`        for each part, run ffmpeg ``-ss/-to`` stream-copy on the
                       source ``.m4b`` with the recipe §3 invariants:
                         · ``-c:a copy -c:v copy`` (no re-encode; cover copied as
                           ``attached_pic``);
                         · a per-part FFMETADATA block whose chapters are REBASED
                           to 0 (START/END shifted by the part's own start) with
                           the original chapter names preserved;
                         · **mandatory ``-map_chapters 1``** — take chapters ONLY
                           from the FFMETADATA file, ignore the source's own
                           chapters. WITHOUT this ffmpeg drags BOTH the source's
                           chapters AND the file's into the part → duplicates +
                           a phantom zero-length chapter (research §3 "грабли,
                           решены"). This flag is the fix;
                         · ``-f ipod`` container, written atomically (temp→rename
                           in ``out_dir``).
                       File name: «<Автор> - <Книга>, Часть N из M.m4b» via
                       :func:`build_m4b.sanitize_filename`. Returns the part paths.

Robustness mirrors :mod:`build_m4b`: ffmpeg is always an argv array (Cyrillic /
odd names safe), every call is bounded by a timeout, each part is atomic, and a
failure sweeps its own temp + FFMETADATA scratch so no half-written part survives.
``split`` reads the source ``.m4b`` only — it never writes/moves/deletes it (I1).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import build_m4b

# Default split threshold (decisions D6 / research §3): ≈300 MB ⇒ ~5.46 h at 128
# kbps, ~3.6 h at 192. Exposed as megabytes in params (``split_threshold_mb``);
# converted to bytes here.
DEFAULT_SPLIT_THRESHOLD_MB = 300
_MB = 1024 * 1024

# Hard ceiling for a single stream-copy part. A copy is near-instant (no encode),
# so this is generous headroom against a wedged ffmpeg on a huge book / slow disk
# while still bounded — the agent (launchd-fired, short-lived) can never hang.
SPLIT_TIMEOUT_S = 60 * 60  # 1 hour per part


def _ffmpeg_bin() -> str:
    """Resolve the ffmpeg executable (PATH lookup; falls back to the name)."""
    return shutil.which("ffmpeg") or "ffmpeg"


def _chapter_size_bytes(manifest: dict, duration_ms: int) -> int:
    """Estimated bytes for ``duration_ms`` of audio at the manifest's bitrate.

    Reuses the build engine's audio-size coefficient (``bitrate_bps / 8 ×
    seconds``) so the split estimate matches the size the build itself targets —
    we do NOT re-invent the math (the cover/mux overhead is accounted once per
    part in :func:`plan_parts`, not per chapter). ``build_m4b._bitrate_kbps``
    resolves/clamps the params' bitrate exactly as the encoder will.
    """
    params = manifest.get("params") or {}
    bitrate_bps = build_m4b._bitrate_kbps(params) * 1000
    seconds = max(0, int(duration_ms)) / 1000.0
    return int(bitrate_bps / 8 * seconds)


# A per-part fixed allowance for the embedded cover + moov atom / chapter track /
# mux slack — the same shape build_m4b.estimate_output_size adds once, applied to
# EACH part here because every part carries its own cover + moov.
_PER_PART_OVERHEAD_BYTES = 60 * 1024 + 64 * 1024


def plan_parts(manifest: dict, threshold_bytes: int | None = None) -> list[dict]:
    """Group consecutive chapters into parts each ≤ ``threshold_bytes`` (research §3).

    Walk the manifest chapters in order, accumulating estimated size; close the
    current part on the PREVIOUS chapter boundary as soon as adding the next
    chapter would exceed the threshold, then start a fresh part. Cuts happen ONLY
    on chapter boundaries — never mid-chapter.

    **Edge E15** — a single chapter whose own estimate already exceeds the
    threshold cannot be made to fit: it becomes its OWN part and is flagged
    ``oversize=True`` (the UI warns; we never split inside a chapter). An oversize
    chapter always stands alone so it never inflates a neighbour.

    Each returned part is a dict:
        index            1-based part number
        total            total number of parts (filled once all are known)
        chapter_indices  the manifest indices of the chapters in this part
        start_ms/end_ms  the part's span on the SOURCE timeline (for ``-ss/-to``)
        est_size         estimated bytes (audio + one per-part overhead)
        oversize         True iff this part is a single chapter over the threshold

    Only chapters with a positive ``duration_ms`` are placed (an unreadable one
    has no timeline position — the build path already refuses such a book via E3,
    so in practice every chapter here is usable). Returns ``[]`` for a manifest
    with no usable chapters.

    ``threshold_bytes`` defaults to the manifest's ``params.split_threshold_mb``
    (or :data:`DEFAULT_SPLIT_THRESHOLD_MB`) converted to bytes.
    """
    if threshold_bytes is None:
        threshold_bytes = _threshold_bytes_from_manifest(manifest)
    # A non-positive threshold is meaningless; fall back to the default so a
    # malformed param can never wedge the planner into zero-size parts.
    if threshold_bytes <= 0:
        threshold_bytes = DEFAULT_SPLIT_THRESHOLD_MB * _MB

    chapters = manifest.get("chapters") or []

    # Build the ordered list of placeable chapters with their cumulative timeline
    # offsets (START/END on the source), preserving each chapter's manifest index.
    placed: list[dict] = []
    cursor_ms = 0
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        dur = ch.get("duration_ms")
        if not (isinstance(dur, int) and dur > 0):
            continue
        idx = ch.get("index")
        start_ms = cursor_ms
        end_ms = cursor_ms + dur
        placed.append({
            "index": idx,
            "duration_ms": dur,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "size": _chapter_size_bytes(manifest, dur),
        })
        cursor_ms = end_ms

    if not placed:
        return []

    # Greedy grouping on chapter boundaries.
    parts: list[dict] = []
    cur: list[dict] = []
    cur_audio = 0  # accumulated audio bytes of the current part (no overhead yet)

    def _flush(group: list[dict], oversize: bool) -> None:
        if not group:
            return
        audio = sum(c["size"] for c in group)
        parts.append({
            "index": len(parts) + 1,
            "total": 0,  # filled below once the count is known
            "chapter_indices": [c["index"] for c in group],
            "start_ms": group[0]["start_ms"],
            "end_ms": group[-1]["end_ms"],
            "est_size": audio + _PER_PART_OVERHEAD_BYTES,
            "oversize": oversize,
        })

    for c in placed:
        # E15: a single chapter bigger than the threshold stands alone, flagged.
        if c["size"] + _PER_PART_OVERHEAD_BYTES > threshold_bytes:
            _flush(cur, oversize=False)
            cur, cur_audio = [], 0
            _flush([c], oversize=True)
            continue

        # Would adding this chapter push the current part over the threshold?
        # (Compare against the part's full estimate = audio + one overhead.)
        prospective = cur_audio + c["size"] + _PER_PART_OVERHEAD_BYTES
        if cur and prospective > threshold_bytes:
            _flush(cur, oversize=False)
            cur, cur_audio = [], 0

        cur.append(c)
        cur_audio += c["size"]

    _flush(cur, oversize=False)

    total = len(parts)
    for p in parts:
        p["total"] = total
    return parts


def _threshold_bytes_from_manifest(manifest: dict) -> int:
    """Resolve the split threshold (bytes) from ``params.split_threshold_mb``.

    Clamps a missing / non-numeric / non-positive value to
    :data:`DEFAULT_SPLIT_THRESHOLD_MB` so the planner always has a sane positive
    budget. The param is in megabytes (UI-facing); we convert to bytes.
    """
    params = manifest.get("params") or {}
    try:
        mb = int(params.get("split_threshold_mb", DEFAULT_SPLIT_THRESHOLD_MB))
    except (TypeError, ValueError):
        mb = DEFAULT_SPLIT_THRESHOLD_MB
    if mb <= 0:
        mb = DEFAULT_SPLIT_THRESHOLD_MB
    return mb * _MB


def part_filename(manifest: dict, index: int, total: int) -> str:
    """Compose «<Автор> - <Книга>, Часть N из M.m4b» (sanitized as one unit).

    Mirrors :func:`build_m4b.output_filename` (author optional) but appends the
    part suffix. The whole stem is sanitized together so a stray separator inside
    author/title cannot escape the filename component.
    """
    author = str(manifest.get("author") or "").strip()
    title = str(manifest.get("title") or "").strip() or "book"
    base = f"{author} - {title}" if author else title
    stem = f"{base}, Часть {index} из {total}"
    return f"{build_m4b.sanitize_filename(stem)}.m4b"


def _part_chapter_names(manifest: dict) -> dict:
    """Map manifest chapter ``index`` → chapter ``name`` (for FFMETADATA titles)."""
    names: dict = {}
    for ch in manifest.get("chapters") or []:
        if isinstance(ch, dict):
            names[ch.get("index")] = str(ch.get("name") or "").strip()
    return names


def _part_ffmetadata_text(manifest: dict, part: dict) -> str:
    """Render the FFMETADATA file for ONE part — chapters REBASED to 0 (research §3).

    The part's chapters keep their original NAMES but their START/END are shifted
    so the first chapter of the part begins at 0 (the part is a fresh stream-copy
    that starts at its own ``start_ms``). Global tags carry the book title/author
    plus the part-specific ``title``/``track`` («Часть N из M», ``N/M``) and a
    shared ``album`` so a player groups the parts of one book together. Values are
    escaped via :func:`build_m4b._meta_escape`; Cyrillic is written as-is (UTF-8).

    ★ This file is the ONLY chapter source for the part — the caller passes
    ``-map_chapters 1`` so the source ``.m4b``'s own chapters are ignored (no
    duplicate / phantom chapters, research §3).
    """
    book_title = str(manifest.get("title") or "").strip()
    author = str(manifest.get("author") or "").strip()
    index = int(part["index"])
    total = int(part["total"])

    esc = build_m4b._meta_escape
    part_title = f"{book_title}, Часть {index} из {total}" if book_title \
        else f"Часть {index} из {total}"

    lines = [";FFMETADATA1"]
    lines.append(f"title={esc(part_title)}")
    if book_title:
        # Shared album = the book, so players group the parts together.
        lines.append(f"album={esc(book_title)}")
    if author:
        lines.append(f"artist={esc(author)}")
    lines.append(f"track={index}/{total}")
    lines.append("genre=Audiobook")
    lines.append("")

    # Rebase each chapter in this part to a 0-based timeline.
    names = _part_chapter_names(manifest)
    base_ms = int(part["start_ms"])
    # Reconstruct each chapter's span from the manifest durations in part order.
    by_index = {}
    cursor = 0
    for ch in manifest.get("chapters") or []:
        if not isinstance(ch, dict):
            continue
        dur = ch.get("duration_ms")
        if not (isinstance(dur, int) and dur > 0):
            continue
        by_index[ch.get("index")] = (cursor, cursor + dur)
        cursor += dur

    for ci in part["chapter_indices"]:
        span = by_index.get(ci)
        if span is None:
            continue
        abs_start, abs_end = span
        start_ms = abs_start - base_ms
        end_ms = abs_end - base_ms
        name = names.get(ci, "")
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={start_ms}")
        lines.append(f"END={end_ms}")
        if name:
            lines.append(f"title={esc(name)}")
        lines.append("")

    return "\n".join(lines) + "\n"


def _ms_to_ffmpeg_ts(ms: int) -> str:
    """Render milliseconds as a seconds.fraction timestamp for ``-ss``/``-to``."""
    return f"{ms / 1000.0:.3f}"


def _extract_cover(m4b_path: Path, dest_jpg: Path) -> Path | None:
    """Pull the source ``.m4b``'s attached-picture cover out to ``dest_jpg``.

    Stream-copying ``-ss/-to`` from the source already carries the cover into each
    part, so this is a belt-and-suspenders fallback only used if a part somehow
    needs the cover re-attached. Best-effort: returns the path on success, ``None``
    if the source has no cover / extraction fails (the part is still valid without
    a re-attached cover because the copy already includes it).
    """
    argv = [
        _ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(m4b_path), "-an", "-c:v", "copy", str(dest_jpg),
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=SPLIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0 and dest_jpg.is_file() and dest_jpg.stat().st_size > 0:
        return dest_jpg
    return None


def _build_one_part(
    m4b_path: Path, part: dict, ffmeta_text: str, final_path: Path
) -> None:
    """Stream-copy ONE part out of ``m4b_path`` → ``final_path`` (atomic, recipe §3).

    ffmpeg invocation (research §3):
      -ss <start> -to <end>     cut the part's span off the SOURCE timeline;
      -i book.m4b -i part.ffmeta
      -map 0:a                  the audio (copy);
      -map 0:v? (attached_pic)  the cover, copied straight from the source m4b so
                                every part keeps the artwork — copied, never
                                re-encoded;
      -map_metadata 1           global tags from the part FFMETADATA;
      -map_chapters 1           **chapters ONLY from the FFMETADATA** (the fix for
                                the duplicate/phantom-chapter trap, research §3);
      -c:a copy -c:v copy       no re-encode at all — fast + lossless;
      -f ipod                   the audiobook container.

    Written to a hidden temp sibling and ``os.replace``-d onto ``final_path`` on
    success; ANY failure sweeps the temp + the FFMETADATA scratch so no
    half-written part survives. Raises :class:`build_m4b.BuildError` on failure.
    """
    out_dir = final_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Per-part FFMETADATA scratch next to the temp output (cleaned with it).
    meta_path = out_dir / f".{final_path.name}.ffmeta"
    meta_path.write_text(ffmeta_text, encoding="utf-8")

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{final_path.name}.", suffix=".tmp", dir=str(out_dir)
    )
    os.close(fd)
    tmp_out = Path(tmp_name)

    ss = _ms_to_ffmpeg_ts(int(part["start_ms"]))
    to = _ms_to_ffmpeg_ts(int(part["end_ms"]))

    argv = [
        _ffmpeg_bin(), "-hide_banner", "-loglevel", "error", "-y",
        # -ss/-to BEFORE -i would be input-seek (faster) but can land mid-packet
        # for stream-copy; placing them as OUTPUT options (after the inputs) keeps
        # the cut frame-accurate on a copy. The cover/metadata inputs follow.
        "-ss", ss, "-to", to,
        "-i", str(m4b_path),
        "-i", str(meta_path),
        "-map", "0:a",
        # The cover is an attached_pic video stream on the source m4b; copy it so
        # the part keeps it. ``0:v?`` (optional) tolerates a source with no cover.
        "-map", "0:v?",
        "-map_metadata", "1",
        "-map_chapters", "1",       # ← mandatory: chapters only from FFMETADATA
        "-c:a", "copy",
        "-c:v", "copy",
        "-disposition:v", "attached_pic",
        "-f", "ipod",
        "-movflags", "+faststart",
        str(tmp_out),
    ]

    try:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=SPLIT_TIMEOUT_S
            )
        except FileNotFoundError:
            raise build_m4b.BuildError("ffmpeg_missing", "ffmpeg not found on PATH")
        except subprocess.TimeoutExpired:
            raise build_m4b.BuildError(
                "split_timeout", f"part exceeded {SPLIT_TIMEOUT_S}s"
            )
        except OSError as exc:
            raise build_m4b.BuildError("ffmpeg_oserror", repr(exc))

        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            raise build_m4b.BuildError(
                "split_part_failed", " | ".join(tail) or f"exit {proc.returncode}"
            )
        if not tmp_out.exists() or tmp_out.stat().st_size == 0:
            raise build_m4b.BuildError("split_empty_part", "ffmpeg produced no part")

        build_m4b._apply_default_mode(tmp_out)
        os.replace(tmp_out, final_path)
    except BaseException:
        build_m4b._unlink_quiet(tmp_out)
        build_m4b._unlink_quiet(meta_path)
        raise
    else:
        build_m4b._unlink_quiet(meta_path)


def split(
    m4b_path: Path, parts: list[dict], *, out_dir: Path, manifest: dict
) -> list[Path]:
    """Stream-copy ``m4b_path`` into ``parts`` at chapter boundaries (recipe §3).

    For each part descriptor from :func:`plan_parts`, cut the source ``.m4b`` with
    ``-ss/-to`` stream-copy, attach a per-part rebased FFMETADATA chapter block
    (``-map_chapters 1`` so only those chapters land — no duplicates), copy the
    cover, set «Часть N из M» title/track, and write atomically to ``out_dir``.

    Returns the list of produced part paths, in order. On ANY part failure a
    :class:`build_m4b.BuildError` propagates AFTER sweeping every part already
    written this call (and the failing part's temp) — splitting is all-or-nothing
    so a half-finished set never lingers. The source ``.m4b`` is read only (I1).
    """
    m4b_path = Path(m4b_path)
    out_dir = Path(out_dir)
    if not parts:
        return []

    produced: list[Path] = []
    try:
        for part in parts:
            fname = part_filename(manifest, int(part["index"]), int(part["total"]))
            final_path = out_dir / fname
            ffmeta = _part_ffmetadata_text(manifest, part)
            _build_one_part(m4b_path, part, ffmeta, final_path)
            produced.append(final_path)
    except BaseException:
        # All-or-nothing: sweep every part we already wrote this call so a failure
        # never leaves a partial set (each was atomically published, so they exist
        # as final files — remove them).
        for p in produced:
            build_m4b._unlink_quiet(p)
        raise

    return produced
