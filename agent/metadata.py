"""Derive book metadata: author/title, chapter order, chapter names.

M0.5 implements the full ID3-aware resolution on top of the M0.2 filename
helpers (``natural_sort_key`` / ``chapter_name_from_filename``, unchanged). It
operates on the probe dicts from :mod:`agent.probe` (``{"file", "tags", ...}``),
never touching the filesystem itself.

Resolution rules (arch/synthesis.md §C, research §5, decisions D1/D5):
  - author/title: ID3 ``album`` → title, ``album_artist``/``artist`` → author;
    if both missing, parse the folder name "Автор - Название".
  - chapter order: ID3 ``track`` (int) if present on EVERY file → by it,
    else natural sort of filenames (``01,02,…,10`` not ``1,10,2``).
  - chapter names: ID3 ``title`` → else the filename minus its numeric prefix.
Cyrillic is preserved end-to-end (no transliteration / ASCII coercion).
"""

from __future__ import annotations

import re
from pathlib import Path

# Folder-name fallback for author/title, e.g. "Толстой - Война и мир". We split
# on the FIRST " - " so a title that itself contains " - " stays intact on the
# title side. Tolerant of an em-dash too. Anything without a separator is treated
# as a bare title (author unknown).
_FOLDER_AUTHOR_TITLE_RE = re.compile(r"^\s*(.+?)\s+[-–—]\s+(.+?)\s*$")

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


def parse_author_title_from_folder(folder_name: str) -> tuple[str, str]:
    """Split a folder name "Автор - Название" into ``(author, title)``.

    Splits on the FIRST " - " / " – " / " — " separator. With no separator the
    whole name is the title and the author is empty. Cyrillic is preserved.
    """
    m = _FOLDER_AUTHOR_TITLE_RE.match(folder_name)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", folder_name.strip()


def _tag(probed_file: dict, name: str) -> str:
    """Return a stripped tag value from a probe dict, or ``""`` if absent/blank."""
    tags = probed_file.get("tags") if isinstance(probed_file, dict) else None
    if not isinstance(tags, dict):
        return ""
    val = tags.get(name)
    return str(val).strip() if val else ""


def derive_author_title(folder: Path, probed: list[dict]) -> tuple[str, str]:
    """Return ``(author, title)`` from ID3 tags, falling back to the folder name.

    Priority (research §5, D1/D5):
      - title  ← ``album`` of any file that has it, else parsed folder title.
      - author ← ``album_artist`` (preferred) else ``artist`` of any file that
        has it, else parsed folder author.
    Each field falls back independently, so a book with ``album`` but no artist
    tag still gets its author from the folder name. The folder name is the robust
    fallback for tag-less collections.
    """
    folder = Path(folder)
    fallback_author, fallback_title = parse_author_title_from_folder(folder.name)

    title = ""
    author = ""
    for p in probed:
        if not title:
            title = _tag(p, "album")
        if not author:
            # album_artist is the more reliable "author" than artist, which in
            # the wild is often the narrator/studio (research §5).
            author = _tag(p, "album_artist") or _tag(p, "artist")
        if title and author:
            break

    return (author or fallback_author, title or fallback_title)


def _track_number(probed_file: dict) -> int | None:
    """Parse an ID3 ``track`` tag → leading int (handles "3", "3/12", " 03 ").

    Returns ``None`` if the tag is missing or has no leading number.
    """
    raw = _tag(probed_file, "track")
    if not raw:
        return None
    m = re.match(r"\s*(\d+)", raw)
    return int(m.group(1)) if m else None


def order_chapters(probed: list[dict]) -> list[dict]:
    """Order probed files by ID3 ``track`` if complete, else natural filename sort.

    The track tag is only trusted when EVERY file has a parseable number (a
    partial set is unreliable — a single missing track would collapse the order),
    in which case we sort by ``(track, natural-filename)`` so duplicate/again
    tracks still fall back to a stable filename order. Otherwise we sort purely by
    natural filename order so ``01,02,…,10`` beats lexicographic ``1,10,2``.
    Returns a new list; the input is not mutated.
    """
    items = list(probed)
    tracks = [_track_number(p) for p in items]
    if items and all(t is not None for t in tracks):
        return sorted(
            items,
            key=lambda p: (
                _track_number(p),
                natural_sort_key(str(p.get("file", ""))),
            ),
        )
    return sorted(items, key=lambda p: natural_sort_key(str(p.get("file", ""))))


def chapter_name(probed_file: dict) -> str:
    """Chapter title: ID3 ``title`` if present, else the cleaned filename (D5).

    The filename fallback drops the extension and a leading numeric prefix via
    :func:`chapter_name_from_filename`, preserving Cyrillic.
    """
    title = _tag(probed_file, "title")
    if title:
        return title
    return chapter_name_from_filename(str(probed_file.get("file", "")))
