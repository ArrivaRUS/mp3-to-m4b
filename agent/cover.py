"""Cover-art resolution chain: embedded → web search → generated fallback.

STUB (M0.1): signatures + contracts only. Real chain lands in M1
(arch/synthesis.md §C, decision R-S3):
  1. extract an embedded picture from the mp3 (via probe/ffmpeg);
  2. else search the web with stdlib ``urllib`` (DuckDuckGo / Yandex) and keep
     square candidates only;
  3. else GENERATE a fallback with Pillow — brand gradient + title/author text in
     the green display font (no cairosvg, avoids the Cyrillic thin-stroke trap).
The chosen/candidate covers live under ``covers/``; the app picks via a
``cover-choice`` command.
"""

from __future__ import annotations

from pathlib import Path


def extract_embedded(mp3_path: Path, out_dir: Path) -> Path | None:
    """Extract an attached picture from ``mp3_path`` → file, or None. STUB."""
    raise NotImplementedError("cover.extract_embedded — M1")


def search_web(author: str, title: str, *, exclude: list[str] | None = None) -> list[dict]:
    """Search the web for square cover candidates (urllib). STUB."""
    raise NotImplementedError("cover.search_web — M1")


def generate_fallback(author: str, title: str, out_path: Path) -> Path:
    """Render a Pillow fallback cover (brand gradient + text). STUB."""
    raise NotImplementedError("cover.generate_fallback — M1")
