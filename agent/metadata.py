"""Derive book metadata: author/title, chapter order, chapter names.

M0.2 implements only what the scanner needs without ffprobe: natural-sort
ordering of filenames and a chapter name derived from the filename (ID3 tags are
not read yet). The ID3-aware resolution lands in M0.5.

Resolution rules (arch/synthesis.md §C, plans.md M0.5):
  - author/title: ID3 album/artist first, else the folder name "Автор - Название".
  - chapter order: ID3 ``track`` first, else natural sort of filenames.
  - chapter names: ID3 ``title`` first, else the filename minus its numeric prefix.
"""

from __future__ import annotations

import re
from pathlib import Path

# Leading "track number" prefix on a chapter filename: digits then a separator
# (space / dot / underscore / hyphen), e.g. "01 - ", "003.", "12_". Anchored so
# it only ever strips a *leading* number, never digits inside the real title.
_LEADING_NUMBER_RE = re.compile(r"^\d+[\s._-]+")

# Split a string into digit / non-digit runs for natural ("human") ordering.
_NATURAL_CHUNK_RE = re.compile(r"(\d+)")


def natural_sort_key(name: str) -> list:
    """Key for natural (human) ordering so "2" sorts before "10".

    Splits the (case-folded) name into alternating text/number chunks; numeric
    chunks compare as ints, text chunks as strings. Mixed (str, int) tuples never
    compare against each other because chunk positions alternate deterministically.
    """
    chunks = _NATURAL_CHUNK_RE.split(name.casefold())
    key: list = []
    for i, chunk in enumerate(chunks):
        # split() yields text at even indices, captured digits at odd indices.
        if i % 2 == 1:
            key.append((1, int(chunk)))
        else:
            key.append((0, chunk))
    return key


def chapter_name_from_filename(filename: str) -> str:
    """Chapter title from a filename: drop extension + leading numeric prefix.

    "01 - Глава первая.mp3" → "Глава первая"; "003.Пролог.mp3" → "Пролог".
    If stripping leaves nothing (e.g. "01.mp3"), fall back to the bare stem so a
    chapter never ends up nameless.
    """
    stem = Path(filename).stem
    cleaned = _LEADING_NUMBER_RE.sub("", stem).strip()
    return cleaned or stem


def derive_author_title(folder: Path, probed: list[dict]) -> tuple[str, str]:
    """Return ``(author, title)`` from ID3 tags, falling back to folder name. STUB."""
    raise NotImplementedError("metadata.derive_author_title — M0.5")


def order_chapters(probed: list[dict]) -> list[dict]:
    """Return probed files ordered by track number / natural filename sort. STUB."""
    raise NotImplementedError("metadata.order_chapters — M0.5")


def chapter_name(probed_file: dict) -> str:
    """Return a chapter title from the ID3 title or cleaned filename. STUB."""
    raise NotImplementedError("metadata.chapter_name — M0.5")
