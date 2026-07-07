"""ffprobe wrapper: per-file duration, tags, and embedded-cover detection.

M0.5: real ffprobe calls per ``research/m4b-toolchain.md`` (verified locally on
the build machine, ffprobe 8.1.2). Every external-tool invocation uses an argv
array (never a shell string) so odd filenames stay safe, runs with an explicit
timeout, and degrades gracefully: a broken / unreadable file NEVER raises out of
this module — it returns an error marker instead (full broken-file UX is M1-edge,
but probing must not crash the scan).

What we read from one mp3 (research §4, §5):
  - ``duration_ms`` — precise, from ``format.duration`` (seconds → ms, rounded).
  - ``tags`` — normalized case-insensitively: title / artist / album /
    album_artist / track / date / genre (ffprobe lower-cases ID3 keys, but we
    fold them ourselves so an odd-cased / id3v2-vs-id3v1 key still maps).
  - ``has_embedded_cover`` — True iff a video stream has
    ``disposition.attached_pic == 1`` with an image codec (mjpeg / png).

Cover extraction (``extract_cover``) copies the attached picture out verbatim
(``-an -c:v copy``) for a preview thumbnail — no re-encode (research §4).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

# Tags we care about for book/chapter resolution (research §5). Keys are the
# *normalized* (lower-case) names; ffprobe already lower-cases ID3 frames, but we
# fold the keys ourselves so a stray upper-case / aliased key still lands.
WANTED_TAGS = ("title", "artist", "album", "album_artist", "track", "date", "genre")

# Common aliases ffmpeg/ID3 may emit for the same logical tag.
_TAG_ALIASES = {
    "album_artist": ("album_artist", "albumartist", "band", "ensemble"),
    "track": ("track", "tracknumber", "trkn"),
    "date": ("date", "year", "originalyear", "originaldate"),
}

# Image codecs that count as an embedded cover (research §1c / §4).
_COVER_CODECS = {"mjpeg", "png", "jpeg", "bmp", "gif"}

# Hard ceilings so a wedged ffprobe/ffmpeg can never hang the (launchd-fired,
# short-lived) agent. Probing is metadata-only and fast; extraction copies a
# small image. Generous enough for a slow disk, tight enough to stay responsive.
PROBE_TIMEOUT_S = 30
EXTRACT_TIMEOUT_S = 30


def _ffprobe_bin() -> str:
    """Resolve the ffprobe executable (PATH lookup; falls back to the name)."""
    return shutil.which("ffprobe") or "ffprobe"


def _ffmpeg_bin() -> str:
    """Resolve the ffmpeg executable (PATH lookup; falls back to the name)."""
    return shutil.which("ffmpeg") or "ffmpeg"


def _normalize_tags(raw: dict) -> dict:
    """Fold ffprobe's ``format.tags`` to our canonical lower-case tag set.

    Case-insensitive: every key is lower-cased before matching. Known aliases
    (e.g. ``albumartist`` → ``album_artist``) are mapped. Values are stripped;
    blank values are dropped so callers can treat "missing" and "empty" alike.
    """
    if not isinstance(raw, dict):
        return {}
    lowered: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or v is None:
            continue
        val = str(v).strip()
        if val:
            lowered[k.lower()] = val

    out: dict[str, str] = {}
    for canonical in WANTED_TAGS:
        aliases = _TAG_ALIASES.get(canonical, (canonical,))
        for alias in aliases:
            if alias in lowered:
                out[canonical] = lowered[alias]
                break
    return out


def _duration_ms_from_format(fmt: dict) -> int | None:
    """Parse ``format.duration`` (seconds, string) → integer milliseconds.

    Returns ``None`` if the field is absent or unparseable (e.g. ``"N/A"``).
    """
    raw = fmt.get("duration") if isinstance(fmt, dict) else None
    try:
        return int(round(float(raw) * 1000))
    except (TypeError, ValueError):
        return None


def _detect_cover(streams: list) -> bool:
    """True iff any stream is an attached-picture image (mjpeg/png/...)."""
    if not isinstance(streams, list):
        return False
    for s in streams:
        if not isinstance(s, dict):
            continue
        if s.get("codec_type") != "video":
            continue
        disp = s.get("disposition")
        attached = isinstance(disp, dict) and disp.get("attached_pic") == 1
        codec_ok = s.get("codec_name") in _COVER_CODECS
        if attached and codec_ok:
            return True
    return False


def _audio_sample_rate(streams: list) -> int | None:
    """Sample rate (Hz) of the FIRST audio stream, or ``None`` if unreadable.

    Walks ``streams`` for the first ``codec_type=="audio"`` entry and parses its
    ``sample_rate`` (ffprobe reports it as a string, e.g. ``"44100"``) to a
    positive int. Returns ``None`` when there is no audio stream or the field is
    absent / non-numeric / non-positive — so a caller can treat "unknown" and
    "missing" alike. Never raises: a malformed streams list yields ``None``.
    """
    if not isinstance(streams, list):
        return None
    for s in streams:
        if not isinstance(s, dict):
            continue
        if s.get("codec_type") != "audio":
            continue
        raw = s.get("sample_rate")
        try:
            sr = int(raw)
        except (TypeError, ValueError):
            return None
        return sr if sr > 0 else None
    return None


def probe_file(mp3_path: Path) -> dict:
    """Probe one mp3 → structured metadata, never raising on a bad file.

    Returns a dict with the stable shape::

        {
          "file": "<name.mp3>",
          "path": "<absolute path>",
          "ok": True,
          "duration_ms": 5000,            # int, or None if unreadable
          "tags": {"title": ..., ...},    # normalized; only present keys
          "has_embedded_cover": False,
          "sample_rate": 44100,           # int Hz, or None if unreadable
        }

    On any failure (ffprobe missing, timeout, non-zero exit, malformed JSON, a
    truncated/corrupt mp3) it returns the same shape with ``ok=False``, an
    ``error`` marker, ``duration_ms=None``, empty ``tags`` and
    ``has_embedded_cover=False`` — so the scan can carry on and surface the bad
    file later (M1-edge) instead of crashing here.
    """
    mp3_path = Path(mp3_path)
    base = {
        "file": mp3_path.name,
        "path": str(mp3_path),
        "ok": False,
        "duration_ms": None,
        "tags": {},
        "has_embedded_cover": False,
        # Source sample rate (Hz) of the first audio stream; None until a
        # successful probe fills it. Feeds the "keep the source SR" build default
        # (scan._source_samplerate → manifest.source_samplerate → build_m4b).
        "sample_rate": None,
    }

    argv = [
        _ffprobe_bin(),
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        str(mp3_path),
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        base["error"] = f"{type(exc).__name__}"
        return base

    if proc.returncode != 0:
        base["error"] = f"ffprobe_exit_{proc.returncode}"
        return base

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        base["error"] = "ffprobe_bad_json"
        return base

    fmt = data.get("format") if isinstance(data, dict) else None
    streams = data.get("streams") if isinstance(data, dict) else None
    fmt = fmt if isinstance(fmt, dict) else {}

    duration_ms = _duration_ms_from_format(fmt)
    tags = _normalize_tags(fmt.get("tags", {}))
    streams_list = streams if isinstance(streams, list) else []
    has_cover = _detect_cover(streams_list)
    sample_rate = _audio_sample_rate(streams_list)

    # A readable mp3 must at least yield a duration; without it we cannot place
    # the chapter on the timeline, so treat it as a (soft) failure for the build
    # math while still reporting what we found.
    if duration_ms is None:
        base["error"] = "no_duration"
        base["tags"] = tags
        base["has_embedded_cover"] = has_cover
        base["sample_rate"] = sample_rate
        return base

    base.update(
        ok=True,
        duration_ms=duration_ms,
        tags=tags,
        has_embedded_cover=has_cover,
        sample_rate=sample_rate,
    )
    return base


def duration_seconds(mp3_path: Path) -> float:
    """Precise duration of ``mp3_path`` in seconds (0.0 if unreadable).

    Thin convenience over :func:`probe_file`; the manifest math uses
    ``duration_ms`` directly, so this exists mainly for ad-hoc callers/tests.
    """
    p = probe_file(mp3_path)
    ms = p.get("duration_ms")
    return (ms / 1000.0) if isinstance(ms, int) else 0.0


def has_embedded_cover(mp3_path: Path) -> bool:
    """True if the mp3 carries an attached-picture (cover) stream."""
    return bool(probe_file(mp3_path).get("has_embedded_cover"))


def extract_cover(mp3_path: Path, dest: Path) -> bool:
    """Extract the embedded cover from ``mp3_path`` to ``dest`` (verbatim copy).

    Copies the attached picture out with ``-an -c:v copy`` (no re-encode, exactly
    the bytes that were embedded — research §4) for use as a preview thumbnail.
    Creates ``dest``'s parent dir. Returns ``True`` on success, ``False`` on any
    failure (no cover, ffmpeg missing, timeout, non-zero exit) — never raises, and
    cleans up a half-written/empty output so a failed extract leaves no turd.
    """
    mp3_path = Path(mp3_path)
    dest = Path(dest)
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    argv = [
        _ffmpeg_bin(),
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(mp3_path),
        "-an",            # drop audio
        "-c:v", "copy",   # copy the picture verbatim
        "-frames:v", "1",  # exactly one image
        str(dest),
    ]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=EXTRACT_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        _unlink_quiet(dest)
        return False

    if proc.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        _unlink_quiet(dest)
        return False
    return True


def _unlink_quiet(path: Path) -> None:
    """Remove a file if present, swallowing errors (cleanup helper)."""
    try:
        path.unlink()
    except OSError:
        pass
