"""mp3-to-m4b agent — the engine and the SINGLE writer of authoritative state.

Process model (arch/synthesis.md §A):
  - The SwiftUI app is a READER: it reads ``state/`` + ``queue/books/`` and only
    ever drops commands into ``queue/commands/``. It never writes state.
  - This agent (launched by launchd via the thin ``bin/runner.sh`` FDA target →
    ``exec python3 -m agent``) is the only owner of the watched folder, the
    per-book manifests, ``state.json`` and the ffmpeg pipeline.

This package is intentionally small at M0.1: only the protocol skeleton exists.
Real ffprobe/ffmpeg logic lands in M0.5+ (see ``plans.md``).
"""

__version__ = "0.1.0"
