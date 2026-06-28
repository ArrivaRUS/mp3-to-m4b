"""Agent entry point — ``python3 -m agent`` (exec'd by ``bin/runner.sh``).

On launch the agent (M0.6 scope, plans.md):
  1. ensures the data-directory tree;
  2. **recovers** any manifest left mid-build (``converting`` with no live pid)
     → ``error: interrupted`` + temp sweep (must run before anything reads state);
  3. **scans** the watched folder → a ``pending-confirm`` manifest per book +
     the ``state.json`` showcase;
  4. **drains** queued commands in ``queue/commands/`` → validate + fake-engine
     (``confirm-build`` flips the book to ``done``), then refreshes the showcase.

Recover-before-scan keeps an orphaned ``converting`` from being read as in-flight;
scan-before-drain is deliberate: a freshly seen book must have its manifest (and
``confirm_token``/``source_rev``) on disk before a command that references it can
validate. Then we exit — launchd re-launches on the next ``WatchPaths`` event
(the watched folder OR ``queue/commands/``), so a run-once-and-exit shape fits.

Every launch journals an ``agent_started`` event first (durable, fsync'd), so a
run is always observable in ``events.jsonl`` even if it has nothing else to do —
this is what makes the "events.jsonl looked empty" class of confusion debuggable.

Flags (combine freely; no flag = scan + drain, recovery always runs):
  ``--scan``   run only the scan pass (manifests + showcase).
  ``--drain``  run only the command-drain pass.
"""

from __future__ import annotations

import sys

from . import __version__, config, dispatcher, scan, state


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    # 1. Ensure the data-directory layout exists (idempotent).
    dirs = config.ensure_data_dirs()

    # 2. Liveness banner + journal (the journal entry is durable: it proves the
    #    agent actually ran, independent of whether stdout was captured).
    print(f"mp3-to-m4b agent alive (v{__version__})")
    print(f"  support root: {config.support_root()}")
    print(f"  data dirs ready: {len(dirs)}")
    state.append_event("agent_started", version=__version__, argv=argv)

    # 3. Recover interrupted builds BEFORE reading/refreshing state (M0.6).
    recovered = dispatcher.recover_interrupted()
    if recovered:
        print(f"  interrupted builds recovered: {recovered}")

    # Choose passes. With no flag, do BOTH (scan, then drain).
    only_scan = "--scan" in argv
    only_drain = "--drain" in argv
    do_scan = only_scan or not (only_scan or only_drain)
    do_drain = only_drain or not (only_scan or only_drain)

    # 4. Scan the watched folder → manifests + state showcase (M0.2).
    if do_scan:
        target = scan.watch_dir()
        showcase = scan.run_scan(target)
        print(f"  watch dir: {target}")
        print(f"  books found: {len(showcase.get('books', []))}")

    # 5. Drain queued commands → validate + fake-engine (M0.5). Refreshes state.
    if do_drain:
        handled = dispatcher.drain_commands()
        print(f"  commands handled: {handled}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
