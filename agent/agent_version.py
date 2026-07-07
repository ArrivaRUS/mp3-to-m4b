"""Agent freshness contract — is the STAGED agent behind the BUNDLED one?

Single source of truth for the "должен ли app сам обновить фоновый агент" check.
The Swift app (`app/main.swift`, `AgentUpdate`) mirrors this EXACT rule at runtime;
this module lets the self-check exercise the same contract in Python the same way
`selfcheck_installer_repoint` proves the installer contract the Swift button trusts.

The bug this guards: after the .app is updated, the background agent staged under
``~/Library/Application Support/mp3-to-m4b/bin/agent/`` can stay on OLD code (new UI
+ old engine → no aac_at/parallel/progress). The .app ships the CURRENT agent in its
bundle at ``<App>.app/Contents/Resources/agent/`` — the SAME source the installer
copies. So "staged behind bundled" is decidable by comparing the two trees.

The rule (both sides ship ``agent/*.py`` verbatim — see build-app.sh / installer.sh):

  · Build a content fingerprint of a directory: for every ``*.py`` file directly in
    it (NOT recursive — the package is flat), map ``basename -> sha256(bytes)``.
  · The agent is UP-TO-DATE  ⇔  fingerprint(staged) == fingerprint(bundled).
  · The agent is OUTDATED    ⇔  the two differ in ANY way (a changed file, a file
    present in the bundle but missing/extra in the staged tree, …).
  · The check is UNDECIDABLE (→ "leave it alone, don't touch") when we cannot read
    the BUNDLED tree — e.g. a dev run where there is no ``Contents/Resources/agent``.
    A missing/empty STAGED tree with a real bundled tree is NOT undecidable: that is
    the classic "app installed, agent never staged / wiped" case → OUTDATED.

Hashing ALL ``*.py`` (not a hand-picked key-file list) is deliberate: it catches
drift in ANY engine module (build_m4b/dispatcher/scan/probe/split/config/…) and can
never go stale as files are added or renamed. It matches exactly what ships: both the
bundle build and the installer copy ``"$src"/agent/*.py`` verbatim.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Result of a freshness comparison. Kept as plain strings so both the self-check
# and any caller read the same vocabulary the Swift enum uses.
UP_TO_DATE = "up-to-date"
OUTDATED = "outdated"
UNDECIDABLE = "undecidable"

# The number of bytes to read per chunk when hashing (files are tiny, but stream
# anyway so this never loads a huge file into memory).
_CHUNK = 1 << 16


def _sha256_file(path: Path) -> str:
    """Hex sha256 of a file's raw bytes (streamed)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def fingerprint(agent_dir: Path | str) -> dict[str, str] | None:
    """Content fingerprint of an agent directory: ``{basename: sha256}`` over its
    direct ``*.py`` files, or ``None`` if the directory does not exist.

    NOT recursive (the ``agent`` package is flat). ``None`` ⇔ the directory is
    absent — the caller uses that to decide "undecidable" vs. "outdated".
    """
    d = Path(agent_dir)
    if not d.is_dir():
        return None
    out: dict[str, str] = {}
    for f in sorted(d.glob("*.py")):
        if f.is_file():
            out[f.name] = _sha256_file(f)
    return out


def compare(bundled_dir: Path | str, staged_dir: Path | str) -> str:
    """Compare the BUNDLED agent tree against the STAGED (installed) one.

    Returns one of ``UP_TO_DATE`` / ``OUTDATED`` / ``UNDECIDABLE``:

      · UNDECIDABLE — the bundled tree is unreadable (no ``Contents/Resources/agent``,
        e.g. a dev run). "Can't check → don't touch" so we never wrongly reinstall.
      · OUTDATED    — the bundled tree is readable AND (the staged tree is absent OR
        its fingerprint differs from the bundled one).
      · UP_TO_DATE  — both readable and fingerprints identical.

    This is the whole decision the app makes at launch (auto-update iff OUTDATED and
    the agent is already installed) and the Settings status line reflects.
    """
    bundled = fingerprint(bundled_dir)
    if bundled is None:
        # No bundled reference to compare against → we cannot judge staleness.
        return UNDECIDABLE
    staged = fingerprint(staged_dir)
    if staged is None:
        # App shipped a real agent but nothing is staged (never installed / wiped).
        return OUTDATED
    return UP_TO_DATE if staged == bundled else OUTDATED


def is_outdated(bundled_dir: Path | str, staged_dir: Path | str) -> bool:
    """Convenience: True ⇔ ``compare(...) == OUTDATED`` (undecidable is NOT outdated,
    so a dev run with no bundled tree never reports "outdated")."""
    return compare(bundled_dir, staged_dir) == OUTDATED
