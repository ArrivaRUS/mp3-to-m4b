"""Optional post-build splitting of an ``.m4b`` along chapter boundaries.

STUB (M0.1): signatures + contracts only. This is the LAST feature (P1 / v1.1,
plans.md M1 last bullet, arch/synthesis.md §C):
  - stream-copy ``-ss/-to`` at chapter boundaries with mandatory ``-map_chapters 1``;
  - per-part cover + track number;
  - a parts preview is shown before committing.
"""

from __future__ import annotations

from pathlib import Path


def plan_parts(manifest: dict, boundaries: list[int]) -> list[dict]:
    """Compute the list of parts (ranges, titles) for a preview. STUB."""
    raise NotImplementedError("split.plan_parts — P1")


def split(m4b_path: Path, parts: list[dict], *, out_dir: Path) -> list[Path]:
    """Stream-copy ``m4b_path`` into parts at chapter boundaries. STUB."""
    raise NotImplementedError("split.split — P1")
