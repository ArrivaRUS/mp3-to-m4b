"""Assemble the final ``.m4b`` from ordered mp3 chapters via ffmpeg.

STUB (M0.1): signatures + contracts only. Real pipeline lands in M1 — the heart
of the product (arch/synthesis.md §C, plans.md M1):
  - concat filter + ``aformat`` → AAC;
  - FFMETADATA chapters (timebase 1/1000), accumulated chapter math;
  - attached_pic cover, ``-f ipod`` + ``+faststart``;
  - argv arrays (never shell strings); atomic temp → rename of the output;
  - cancel = kill + cleanup.
File-descriptor mitigation (research §1a): ``filter_complex_script`` + a chapter
threshold → fallback to a normalized concat demuxer.

★ This function is invoked ONLY from the ``confirm-build`` handler after the
command is validated (status / source_rev / confirm_token). The scanner never
calls it — that is the structural I2 guarantee (arch/synthesis.md §B).
"""

from __future__ import annotations

from pathlib import Path


def build(manifest: dict, *, out_path: Path) -> Path:
    """Build a ``.m4b`` from a validated manifest → atomic output path. STUB."""
    raise NotImplementedError("build_m4b.build — M1")


def estimate_output_size(manifest: dict) -> int:
    """Rough output-size estimate (bytes) for the confirm window. STUB."""
    raise NotImplementedError("build_m4b.estimate_output_size — M1")
