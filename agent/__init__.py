"""mp3-to-m4b agent — the engine and the SINGLE writer of authoritative state.

Process model (arch/synthesis.md §A · plan v2 Р1):
  - The SwiftUI app is a READER: it reads ``state/`` + ``queue/books/`` and only
    ever drops commands into ``queue/commands/``. It never writes state.
  - This agent is the only owner of the watched folder, the per-book manifests,
    ``state.json`` and the ffmpeg pipeline.

The live process chain is the donor's shape, 1-for-1 (Р1 — it is the one that has
been proven in production, and TCC attributes the whole chain to its first link)::

    launchd → mp3-to-m4b-agent (frozen Mach-O helper, PA0)
            → /bin/bash bin/runner.sh          (stays ALIVE, does not exec)
            → python3 -m agent                 (spawned in the background + wait)
            → ffmpeg

``runner.sh`` deliberately does NOT ``exec`` python: it keeps a live shell with
TERM/INT/HUP traps between launchd and us, so ``launchctl bootout`` has somewhere
to land and the signal reaches python instead of vaporising the process group.
The actual teardown of ffmpeg is python's job (:mod:`agent.shutdown` +
``build_m4b``) — bash cannot do it, ffmpeg is its grandchild.

Beyond M0 this package holds the whole engine: scan/grouping, the real ffmpeg
build (chapters, cover, split, fast mode), the command protocol, and the M4 access
gate that keeps every one of those away from a folder macOS has not granted.
"""

__version__ = "0.9"
