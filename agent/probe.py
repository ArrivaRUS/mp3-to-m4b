"""ffprobe wrapper: per-file duration, tags, and embedded-cover detection.

STUB (M0.1): signatures + contracts only. Real ffprobe calls land in M0.5
(see ``research/`` recipes). All external-tool invocations use argv arrays
(never shell strings) to stay safe with odd filenames.
"""

from __future__ import annotations

from pathlib import Path


def probe_file(mp3_path: Path) -> dict:
    """Probe one mp3 → ``{duration, tags{...}, has_embedded_cover}``.

    STUB: not implemented at M0.1 (M0.5 wires real ffprobe).
    """
    raise NotImplementedError("probe.probe_file — M0.5")


def duration_seconds(mp3_path: Path) -> float:
    """Return the precise duration of ``mp3_path`` in seconds. STUB."""
    raise NotImplementedError("probe.duration_seconds — M0.5")


def has_embedded_cover(mp3_path: Path) -> bool:
    """True if the mp3 carries an attached picture stream. STUB."""
    raise NotImplementedError("probe.has_embedded_cover — M0.5")
