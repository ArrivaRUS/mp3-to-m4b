"""Process-wide cooperative shutdown flag (M3 — «агент не оставляет сироту»).

Why this module exists (arch/plan-binrunner-mp3-v2.md §M3, risk M4f / Codex
MAJOR-9). The live process chain is::

    launchd → mp3-to-m4b-agent (helper) → /bin/bash runner.sh → python3 -m agent
            → ffmpeg (one, or a whole pool in fast mode)

``launchctl bootout`` (which the installer performs on EVERY update) sends
SIGTERM down that chain. ``bin/runner.sh`` forwards it to python (M0), but that
alone does **not** close the hole: bash does not know ffmpeg's pid (ffmpeg is its
*grandchild*), and python's DEFAULT SIGTERM disposition is "die immediately" —
which reaps nobody. The result is an orphaned ffmpeg still writing into a temp
directory that is already gone, plus a manifest stuck at ``converting``.

So the *python* layer owns the teardown, and it must be **cooperative**: the
handler only raises a flag, and the code that actually owns the children (the
encoder poll loop in :mod:`agent.build_m4b`, which already wakes ~3×/s for the
cancel poll) reads that flag and performs the real unwind — SIGTERM→SIGKILL every
ffmpeg child, sweep every temp, leave no half-written ``.m4b``, then exit
``128+signum``.

Contract of the handler itself:
  · it does the **minimum** possible work — integer bookkeeping only. No I/O, no
    locks, no journalling, no ``os._exit``: a signal handler runs between two
    bytecodes of whatever the main thread was doing (possibly mid-``os.replace``),
    so anything more is a correctness hazard;
  · it is **idempotent and safe on repeat**. launchd may send TERM and then, after
    its exit timeout, SIGKILL; a user may hit Ctrl-C twice. Every extra signal is
    counted, the FIRST one stays authoritative, and nothing is torn down twice.
    Deliberately there is **no "second signal = hard exit" escalation**: exiting
    from the handler would abandon a live ffmpeg — i.e. it would *create* the very
    orphan this module exists to prevent;
  · it **never raises**, so it can never interrupt an atomic publish or turn into
    a stray exception in an unrelated call.

Signals are only ever delivered to the MAIN thread by CPython, and
:func:`signal.signal` may only be called from it — :func:`install` therefore
tolerates being called from a worker thread (it reports "installed nothing"
instead of raising), which keeps self-checks that drive the engine in a thread
working unchanged.

This module intentionally imports nothing from the package, so anyone (encoder,
scanner, dispatcher) can read the flag without an import cycle.
"""

from __future__ import annotations

import signal

# The three signals a launchd job / a terminal can legitimately use to ask us to
# stop. SIGKILL cannot be caught (by design — that is why the teardown below must
# be fast), and SIGHUP is guarded because it is POSIX-only.
_WANTED: tuple[str, ...] = ("SIGTERM", "SIGINT", "SIGHUP")

# Mutable module state. A dict is used deliberately: item assignment on a dict is
# a single bytecode under the GIL, so the handler can update it without a lock
# (taking a lock inside a signal handler is a deadlock waiting to happen).
_STATE: dict[str, int | None] = {"signum": None, "count": 0}

_INSTALLED: list[int] = []


def _handler(signum: int, frame) -> None:  # noqa: ANN001 - stdlib handler shape
    """The one and only signal handler: raise a flag, do nothing else.

    Re-entrancy: CPython runs a python-level handler at a bytecode boundary and
    will not run a second one *inside* it, so the read-modify-write on ``count``
    is safe here. Keeps the FIRST signum (that is the one we exit with).
    """
    count = _STATE["count"] or 0
    _STATE["count"] = count + 1
    if _STATE["signum"] is None:
        _STATE["signum"] = signum


def install() -> list[int]:
    """Install the cooperative handler for TERM/INT/HUP. Idempotent.

    Returns the list of signal numbers actually installed (``[]`` when called off
    the main thread — CPython forbids ``signal.signal`` there — or on a platform
    missing one of them). Call it as the FIRST thing in the process: a signal that
    arrives before this point still gets the default disposition, but at that
    moment no ffmpeg exists yet, so there is nothing to orphan.
    """
    if _INSTALLED:
        return list(_INSTALLED)
    for name in _WANTED:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            signal.signal(signum, _handler)
        except (ValueError, OSError, RuntimeError):
            # ValueError: not the main thread (self-check driving the engine in a
            # worker) → the process keeps the default disposition; nothing to fix.
            continue
        _INSTALLED.append(int(signum))
    return list(_INSTALLED)


def requested() -> bool:
    """True once a TERM/INT/HUP has been received (the poll loops' predicate)."""
    return _STATE["signum"] is not None


def signum() -> int | None:
    """The FIRST signal number received, or ``None``."""
    return _STATE["signum"]


def count() -> int:
    """How many signals arrived in total (≥2 means launchd/the user repeated)."""
    return _STATE["count"] or 0


def name() -> str:
    """Human/journal name of the signal that asked us to stop (``"SIGTERM"``)."""
    num = _STATE["signum"]
    if num is None:
        return ""
    try:
        return signal.Signals(num).name
    except ValueError:
        return f"signal {num}"


def exit_code() -> int:
    """The process exit code for this shutdown: ``128 + signum`` (0 if none).

    The shell convention (``128+sig``) is what ``bin/runner.sh`` mirrors upward,
    so ``launchctl``/the installer sees a truthful "was terminated by TERM" (143)
    rather than a fake success.
    """
    num = _STATE["signum"]
    return 128 + int(num) if num is not None else 0


def request(signum_value: int = signal.SIGTERM) -> None:
    """Raise the flag programmatically (tests / an internal watchdog).

    Same effect as receiving the signal, minus the kernel. Used by self-checks to
    exercise the teardown without racing a real ``kill``.
    """
    _handler(int(signum_value), None)


def reset() -> None:
    """Clear the flag (tests only — a real process never un-shuts-down)."""
    _STATE["signum"] = None
    _STATE["count"] = 0
