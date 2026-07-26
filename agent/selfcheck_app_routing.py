"""§app-routing self-check — WHICH BOOK does the confirm window present?

Run it standalone:

    python3 -m agent.selfcheck_app_routing

The bug (shipped in v0.9, third of its class — lesson 005 "видимый кликабельный
контрол ОБЯЗАН работать"): the queue's «Подтвердить» on a ROW navigated to the
confirm window but THREW THE ROW'S BOOK AWAY (``onConfirm: { _ in … }``), so the
window always rendered ``activeBooks.first``. Confirming the second book opened
the first one's window — the user edited metadata/cover/quality and pressed
«Собрать» believing it was the book they picked, and a different book got built.

The routing rule now lives in ONE pure place, ``ShowcaseState.presentedBook``
(app/StateModel.swift), which both the queue pick and the agent's auto-surface go
through. This suite is a thin runner: it compiles the Foundation-only app sources
together with ``app/selfcheck_routing.swift`` (the assertions themselves — Swift,
because the contract is Swift) and surfaces that binary's verdict. The Swift file
is NOT part of the shipped app: build/build-app.sh lists its sources explicitly.

It touches no state tree, no launchd, no ffmpeg — it is a pure unit check of a
value-level rule, so it is grouped with the other lightweight suites.

``swiftc`` (Xcode command line tools) is REQUIRED: build/build-app.sh needs it
anyway, and a missing toolchain is reported as a FAILURE, never a silent green
(the ``selfcheck_all`` contract).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

MARKER = "§app-routing self-check:"

REPO = Path(__file__).resolve().parent.parent

# The app sources the check needs (StateModel pulls the status markers in via
# loadState). Mostly Foundation-only value code; `Tokens.swift` +
# `FolderAccessCard.swift` import SwiftUI, which compiles and links fine in a
# command-line binary as long as nothing instantiates an NSApplication — and it is
# worth the two extra seconds: it means the M6 assertions run against the EXACT
# strings and rules the shipped card renders, not a parallel copy of them that can
# drift away from what the user sees.
SOURCES = [
    REPO / "app" / "StateModel.swift",
    REPO / "app" / "WindowGeometry.swift",
    REPO / "app" / "EngineClient.swift",
    REPO / "app" / "EngineClient+Status.swift",
    REPO / "app" / "Tokens.swift",
    REPO / "app" / "FolderAccessCard.swift",
    REPO / "app" / "selfcheck_routing.swift",
]


def _fail(reason: str) -> int:
    """Report a runner-level failure in the suites' own summary format."""
    print(f"  [FAIL] {reason}")
    print(f"\n{MARKER} 0/1 checks passed")
    print(f"  FAILED checks: {reason}")
    return 1


def run() -> int:
    missing = [str(p) for p in SOURCES if not p.is_file()]
    if missing:
        return _fail("missing swift sources: " + ", ".join(missing))
    if shutil.which("xcrun") is None:
        return _fail("xcrun/swiftc not found (install Xcode command line tools)")

    with tempfile.TemporaryDirectory(prefix="mp3tom4b-selfcheck-routing-") as tmp:
        binary = Path(tmp) / "routing"
        compile_proc = subprocess.run(
            ["xcrun", "swiftc", *[str(p) for p in SOURCES], "-o", str(binary)],
            capture_output=True, text=True,
        )
        if compile_proc.returncode != 0 or not binary.is_file():
            sys.stdout.write(compile_proc.stdout)
            sys.stderr.write(compile_proc.stderr)
            return _fail("swiftc failed to build the routing check")

        # The Swift binary prints the per-check lines AND the «X/Y checks passed»
        # summary in the shared format, so pass its output through verbatim and
        # inherit its verdict.
        run_proc = subprocess.run([str(binary)], capture_output=True, text=True)
        sys.stdout.write(run_proc.stdout)
        sys.stderr.write(run_proc.stderr)
        if MARKER not in run_proc.stdout:
            return _fail("routing check produced no summary line")
        return run_proc.returncode


if __name__ == "__main__":
    sys.exit(run())
