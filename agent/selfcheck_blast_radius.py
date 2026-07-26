"""§blast-radius — the guard that keeps self-checks out of the user's install.

Run its own checks:

    python3 -m agent.selfcheck_blast_radius

WHY IT EXISTS (.patches/005)
    Twice in one day a self-check reached the live system: a negative control
    bootstrapped a real launchd job, and an ad-hoc probe run journalled into the
    real Application Support. So :mod:`agent.selfcheck_all` snapshots the
    production artifacts before and after EVERY suite and fails the run on any
    difference.

WHY IT NEEDED TEETH *AND* EYES
    That guard was designed while there was no live install (addendum §3.1
    measured exactly that: «живой установки v0.9 нет»). Since 1.0 shipped there is
    one, and it is supposed to work: launchd fires the agent every 300 s, and the
    agent writes its journal, its state and its log. A full self-check run takes
    ~5.5 min, so it ALWAYS overlaps a tick — and a verbatim before/after
    comparison painted a random innocent suite red every run.

    "Stop looking at App Support" would have been the end of the guard. So the
    guard learned to ATTRIBUTE instead:

      · STRUCTURAL facts stay strict, always. A plist appearing, changing or
        vanishing; ``ProgramArguments[0]`` being rewritten; the helper or
        ``runner.sh`` bytes changing; the receipt changing; a job or label being
        loaded/unloaded; the tree or the watch folder being created or deleted.
        The live agent NEVER does any of these — only an installer or a test does.
      · RUNTIME facts (journal, state, ledgers, manifests, covers, the launchd log,
        the contents of the watch folder) are what a live tick legitimately
        touches. They are allowed to change ONLY when the change is attributable
        to the live agent — never merely because they are in the "runtime" list.

HOW ATTRIBUTION WORKS — the launchd log is the provenance oracle
    The plist routes ``StandardOutPath``/``StandardErrorPath`` to
    ``~/Library/Logs/mp3-to-m4b.log``, so EVERY run launchd starts leaves a
    ``mp3-to-m4b agent alive (vX)`` banner there. A self-check child cannot: its
    stdout goes into the pipe the runner captures. That single asymmetry answers
    "did the agent actually run in this window, or did something else write?" —
    which is precisely what the 26.07 leak needed and did not have.

    On top of it, the CONTENT has to point at the user's world:
      · every path in every new journal record lives inside the install or the
        watch folder (a ``/var/folders/…`` path is a test's fingerprint);
      · ``state.json`` still carries the install's own ``install_generation`` —
        which only launchd's plist env supplies, so a hand-run agent cannot forge
        it — and still names the production watch folder;
      · the ledgers and manifests only ever reference sources inside the watch
        folder;
      · the journal and the log are append-only, and the number of new
        ``agent_started`` records never exceeds the number of new banners (a
        forged run is a run without a banner);
      · a change inside the watch folder is corroborated by a build event in the
        same journal delta naming that exact output.

NO LIVE INSTALL ⇒ NO EXCEPTIONS
    Every relaxation above is gated on a PROVEN live install: the tree, the plist
    and a loaded job, all three. On a clean machine the guard is byte-for-byte
    strict exactly as it was — which is the machine where a leak would otherwise
    create the tree from nothing and go unnoticed.

RESIDUAL, STATED HONESTLY
    A record that (a) carries no path, (b) is not ``agent_started``, (c) uses an
    event kind the agent really emits, and (d) is written while a legitimate tick
    happens to run in the same window would pass. Closing that needs a per-run
    provenance stamp inside each record, which means changing the agent that is
    already installed on the user's machine — a follow-up, not something to bolt
    on under a live install.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# --- what a live agent run leaves in the launchd log -------------------------
BANNER = "mp3-to-m4b agent alive"

# Event kinds the shipped agent can emit. An unknown kind in the production
# journal is not something the live agent wrote.
KNOWN_EVENTS = frozenset({
    "agent_started", "agent_interrupted", "drain_stopped", "phase_deadline_exceeded",
    "folder_access_probe", "folder_access_consent_window", "folder_access_lost",
    "recheck_access", "app_nudged", "app_nudge_failed", "presence_reconcile_failed",
    "book_rearmed_reappeared", "book_skipped", "skip_rejected", "group_still_copying",
    "confirm_accepted", "confirm_rejected_stale", "command_rejected", "command_noop",
    "command_error", "cancel_moot", "build_skipped_idempotent", "build_started",
    "build_done", "build_failed", "build_progress", "build_interrupted",
    "build_cancelled", "interrupted", "grouping_materialized", "grouping_rejected",
    "reconvert_rejected", "book_rearmed_reconvert", "cover_chosen", "cover_failed",
    "split_done", "engine_probe_failed",
})

# Absolute-path prefixes a production record may legitimately mention besides the
# install and the watch folder (tools, frameworks). Anything else — above all
# ``/var/folders`` and ``/tmp`` — is a self-check's fingerprint.
_SYSTEM_PREFIXES = ("/usr/", "/bin/", "/sbin/", "/opt/", "/Applications/",
                    "/System/", "/Library/")


class Paths:
    """Where the production install lives. Injectable so the tests never touch it."""

    def __init__(self, support: Path, log: Path, launch_agents: Path,
                 watch: Path, label: str) -> None:
        self.support = Path(support)
        self.log = Path(log)
        self.launch_agents = Path(launch_agents)
        self.watch = Path(watch)
        self.label = label

    def as_dict(self) -> dict:
        return {"support": str(self.support), "log": str(self.log),
                "launch_agents": str(self.launch_agents), "watch": str(self.watch),
                "label": self.label}

    @staticmethod
    def from_dict(d: dict) -> "Paths":
        return Paths(Path(d["support"]), Path(d["log"]), Path(d["launch_agents"]),
                     Path(d["watch"]), d["label"])


def default_paths() -> Paths:
    """The real install's paths, taking the watch folder from the receipt.

    The watch folder is NOT assumed to be ``~/Desktop/mp3-to-m4b``: the user can
    re-point it, and a guard that watched the wrong folder would be watching
    nothing. The receipt is the installer's own record of where it put things.
    """
    support = Path.home() / "Library" / "Application Support" / "mp3-to-m4b"
    watch = Path.home() / "Desktop" / "mp3-to-m4b"
    label = "com.arrivarus.mp3tom4b.agent"
    try:
        receipt = json.loads((support / "install-receipt.json").read_text())
        if isinstance(receipt, dict):
            watch = Path(receipt.get("watch_dir") or watch)
            label = receipt.get("label") or label
            support = Path(receipt.get("support_dir") or support)
    except (OSError, ValueError):
        pass
    return Paths(support, Path.home() / "Library" / "Logs" / "mp3-to-m4b.log",
                 Path.home() / "Library" / "LaunchAgents", watch, label)


# --- primitives --------------------------------------------------------------


def _stamp(path: Path) -> tuple:
    try:
        st = path.stat()
        return (True, st.st_size, st.st_mtime_ns)
    except OSError:
        return (False, 0, 0)


def _sha256(path: Path) -> str:
    try:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def _listing(path: Path) -> tuple:
    out = []
    try:
        for child in sorted(path.iterdir(), key=lambda p: p.name):
            out.append((child.name,) + _stamp(child))
    except OSError:
        pass
    return tuple(out)


def _file_pos(path: Path) -> tuple:
    """(inode, size) — enough to read exactly the bytes appended since."""
    try:
        st = path.stat()
        return (st.st_ino, st.st_size)
    except OSError:
        return (0, 0)


def _read_range(path: Path, start: int, end: int) -> str:
    if end <= start:
        return ""
    try:
        with path.open("rb") as fh:
            fh.seek(start)
            return fh.read(end - start).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _loaded_jobs(label: str) -> tuple:
    """Labels loaded in the user's launchd domain that look like ours."""
    try:
        proc = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    except OSError:
        return ()
    needle = label.rsplit(".", 1)[0] if "." in label else label
    return tuple(sorted(
        ln.split("\t")[-1].strip() for ln in (proc.stdout or "").splitlines()
        if "mp3tom4b" in ln or needle in ln
    ))


def _pa0(plist_path: Path) -> str:
    """``ProgramArguments[0]`` of a plist — the subject TCC and launchd act on."""
    try:
        with plist_path.open("rb") as fh:
            data = plistlib.load(fh)
        args = data.get("ProgramArguments")
        return str(args[0]) if isinstance(args, list) and args else ""
    except Exception:  # noqa: BLE001 - a malformed plist is itself a difference
        return ""


# --- the snapshot ------------------------------------------------------------


def snapshot(paths: Paths | None = None) -> dict:
    """Everything a self-check is forbidden to touch, split by attribution class."""
    p = paths or default_paths()
    plists = []
    try:
        for f in sorted(p.launch_agents.glob("*mp3*")):
            plists.append((f.name,) + _stamp(f) + (_sha256(f), _pa0(f)))
    except OSError:
        pass
    bins = []
    for name in ("mp3-to-m4b-agent", "runner.sh"):
        f = p.support / "bin" / name
        bins.append((name,) + _stamp(f) + (_sha256(f),))
    jobs = _loaded_jobs(p.label)

    structural = {
        "support_exists": p.support.is_dir(),
        "watch_exists": p.watch.is_dir(),
        "venv_exists": (p.support / "venv").is_dir(),
        "receipt": _sha256(p.support / "install-receipt.json"),
        "plists": tuple(plists),
        "bin": tuple(bins),
        "jobs": jobs,
    }
    runtime = {
        "events": _file_pos(p.support / "state" / "events.jsonl"),
        "events_prev": _file_pos(p.support / "state" / "events.jsonl.1"),
        "log": _file_pos(p.log),
        "state": _stamp(p.support / "state" / "state.json"),
        "presence": _stamp(p.support / "state" / "presence.json"),
        "notified": _stamp(p.support / "state" / "notified.json"),
        "books": _listing(p.support / "queue" / "books"),
        "commands": _listing(p.support / "queue" / "commands"),
        "covers": _listing(p.support / "covers"),
        "watch_entries": _listing(p.watch),
    }
    return {
        "paths": p.as_dict(),
        # A relaxation is only ever allowed against a PROVEN live install: the
        # tree, the plist and a loaded job. Two out of three is not an install.
        "live": bool(structural["support_exists"] and plists and jobs),
        "structural": structural,
        "runtime": runtime,
        "ts": time.time(),
    }


# --- attribution -------------------------------------------------------------


def _is_local(raw: str, p: Paths) -> bool:
    """Does this absolute path belong to the user's install rather than a test?"""
    try:
        path = os.path.normpath(raw)
    except (TypeError, ValueError):
        return False
    for root in (str(p.support), str(p.watch), str(p.log)):
        if path == root or path.startswith(root.rstrip("/") + "/"):
            return True
    return path.startswith(_SYSTEM_PREFIXES)


def _record_paths(value, out: list) -> None:
    """Every absolute-looking string anywhere inside a journal record."""
    if isinstance(value, str):
        if value.startswith("/"):
            out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _record_paths(v, out)
    elif isinstance(value, list):
        for v in value:
            _record_paths(v, out)


def _journal_delta(before: dict, after: dict, p: Paths) -> tuple[list[dict], str]:
    """The records appended to the journal between two snapshots.

    Returns (records, error). A rotation (inode change) is legitimate only when
    the previous generation moved to ``.1``; anything else — a shrink, a rewrite,
    a replaced inode — is reported as an error rather than silently re-read.
    """
    path = p.support / "state" / "events.jsonl"
    b_ino, b_size = before["runtime"]["events"]
    a_ino, a_size = after["runtime"]["events"]
    if b_ino and a_ino and b_ino != a_ino:
        if after["runtime"]["events_prev"][0] != b_ino:
            return [], ("the journal's inode changed without the old generation "
                        "landing in events.jsonl.1 — it was replaced, not rotated")
        text = _read_range(path, 0, a_size)
    elif a_size < b_size:
        return [], "the journal SHRANK — an append-only file was rewritten"
    else:
        text = _read_range(path, b_size, a_size)
    records = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records, ""


def _log_delta(before: dict, after: dict, p: Paths) -> tuple[str, str]:
    b_ino, b_size = before["runtime"]["log"]
    a_ino, a_size = after["runtime"]["log"]
    if b_ino and a_ino and b_ino != a_ino:
        return _read_range(p.log, 0, a_size), ""
    if a_size < b_size:
        return "", "the launchd log SHRANK — an append-only file was rewritten"
    return _read_range(p.log, b_size, a_size), ""


def _src_dirs_local(path: Path, p: Paths, limit: int = 200) -> list[str]:
    """Manifests / ledgers must only ever reference sources inside the watch folder."""
    bad = []
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return bad
    candidates = []
    if isinstance(data, dict):
        books = data.get("books")
        if isinstance(books, dict):
            candidates = [e.get("src_dir") for e in books.values()
                          if isinstance(e, dict)]
        elif isinstance(data.get("src_dir"), str):
            candidates = [data["src_dir"]]
    for src in candidates[:limit]:
        if isinstance(src, str) and src and not _is_local(src, p):
            bad.append(src)
    return bad


def diff(before: dict, after: dict, settle_s: float = 2.0) -> list[str]:
    """What changed in the live system that a self-check must answer for.

    Empty list = clean. Every entry is a human-readable violation; the caller
    turns them into a red run naming the suite.
    """
    p = Paths.from_dict(after.get("paths") or before["paths"])
    out: list[str] = []

    # --- STRUCTURAL: always strict, live install or not ---------------------
    labels = {
        "support_exists": "the install tree was created or deleted",
        "watch_exists": "the watch folder was created or deleted",
        "venv_exists": "the venv was created or deleted",
        "receipt": "install-receipt.json changed",
        "plists": f"a LaunchAgent plist in {p.launch_agents} changed "
                  f"(name/bytes/ProgramArguments[0])",
        "bin": "the installed helper or runner.sh bytes changed",
        "jobs": "the set of loaded launchd jobs changed",
    }
    for key, text in labels.items():
        if before["structural"].get(key) != after["structural"].get(key):
            out.append(f"{text}: {before['structural'].get(key)} → "
                       f"{after['structural'].get(key)}")

    # --- RUNTIME ------------------------------------------------------------
    runtime_changed = [k for k in before["runtime"]
                       if before["runtime"].get(k) != after["runtime"].get(k)]
    if not runtime_changed:
        return out

    if not (before.get("live") and after.get("live")):
        # No proven live install ⇒ nothing may write here at all. This is the
        # clean-machine case, and it is the one where a leak would otherwise
        # create the tree out of nothing and never be noticed.
        out.append("no live install is present, so NOTHING may write to the "
                   f"production tree — changed: {', '.join(sorted(runtime_changed))}")
        return out

    # A live install is present. Every change now has to be attributable.
    records, err = _journal_delta(before, after, p)
    if err:
        out.append(err)
    log_text, log_err = _log_delta(before, after, p)
    if log_err:
        out.append(log_err)
    banners = log_text.count(BANNER)

    # (1) Provenance: the agent must actually have RUN. A self-check child's
    #     stdout goes into the runner's pipe, never into StandardOutPath, so a
    #     window with production writes but no banner is the 26.07 leak exactly.
    if banners == 0 and settle_s > 0:
        # Absorb the in-flight tick: a run can be mid-flight at snapshot time.
        time.sleep(settle_s)
        log_text, _ = _log_delta(before, snapshot(p), p)
        banners = log_text.count(BANNER)
    if banners == 0:
        out.append("the production tree changed but the launchd log gained no "
                   f"«{BANNER}» banner — no agent run can account for it "
                   f"(changed: {', '.join(sorted(runtime_changed))})")

    # (2) Every new journal record must look like the live install wrote it.
    started = 0
    for rec in records:
        kind = rec.get("event")
        if kind == "agent_started":
            started += 1
        if kind not in KNOWN_EVENTS:
            out.append(f"a journal record of unknown kind {kind!r} appeared in the "
                       "production journal")
            continue
        found: list = []
        _record_paths(rec, found)
        foreign = [x for x in found if not _is_local(x, p)]
        if foreign:
            out.append(f"a production journal record ({kind}) references a path "
                       f"outside the install: {foreign[:3]}")

    # (3) A forged run is a run without a banner.
    if started > banners:
        out.append(f"{started} new agent_started records but only {banners} launchd "
                   "banners — a run was journalled that launchd never started")

    # (4) The state file must still describe THIS install. install_generation
    #     comes only from the plist env, so a hand-run agent cannot forge it.
    try:
        state = json.loads((p.support / "state" / "state.json").read_text())
        block = state.get("agent") if isinstance(state, dict) else None
        block = block if isinstance(block, dict) else {}
        receipt = json.loads((p.support / "install-receipt.json").read_text())
        generation = receipt.get("generation") if isinstance(receipt, dict) else None
        if block.get("watch_dir") and not _is_local(block["watch_dir"], p):
            out.append("state.json now names a watch folder outside the install: "
                       f"{block['watch_dir']}")
        if generation and block.get("install_generation") != generation:
            out.append("state.json lost the install's generation "
                       f"({block.get('install_generation')} ≠ {generation}) — it was "
                       "written by something launchd did not start")
    except (OSError, ValueError):
        pass

    # (5) Ledgers and manifests may only reference sources inside the watch folder.
    for rel in ("state/presence.json", "state/notified.json"):
        for bad in _src_dirs_local(p.support / rel, p):
            out.append(f"{rel} references a source outside the watch folder: {bad}")
    books_dir = p.support / "queue" / "books"
    try:
        for man in sorted(books_dir.glob("*.json"))[:200]:
            for bad in _src_dirs_local(man, p):
                out.append(f"manifest {man.name} references a source outside the "
                           f"watch folder: {bad}")
    except OSError:
        pass

    # (6) The user's watch folder. Two different questions, deliberately:
    #
    #     · something DISAPPEARED — never legitimate. The agent does not delete
    #       source folders, and a self-check deleting the user's books is the worst
    #       outcome this guard exists to prevent. Always red.
    #     · something APPEARED — legitimate in exactly two shapes: the agent wrote
    #       its own output there (``.m4b`` named by a build event in this window),
    #       or the USER dropped a book in and the agent acknowledged it by writing
    #       a manifest for that source. Anything else is unattributed and red.
    if before["runtime"]["watch_entries"] != after["runtime"]["watch_entries"]:
        built = set()
        for rec in records:
            for key in ("output_path", "output"):
                val = rec.get(key)
                if isinstance(val, str) and val:
                    built.add(Path(val).name)
        known_sources = set()
        try:
            for man in sorted((p.support / "queue" / "books").glob("*.json"))[:500]:
                try:
                    data = json.loads(man.read_text())
                except (OSError, ValueError):
                    continue
                src = data.get("src_dir") if isinstance(data, dict) else None
                if isinstance(src, str) and src:
                    known_sources.add(Path(src).name)
        except OSError:
            pass
        before_names = {e[0] for e in before["runtime"]["watch_entries"]}
        after_names = {e[0] for e in after["runtime"]["watch_entries"]}
        vanished = before_names - after_names
        unexplained = {
            n for n in (after_names - before_names)
            if n not in built and n not in known_sources
            and not any(n.startswith("." + b) for b in built)
        }
        if vanished:
            out.append(f"entries disappeared from the user's watch folder: "
                       f"{sorted(vanished)[:5]}")
        if unexplained:
            out.append(
                "entries appeared in the user's watch folder that neither a build "
                "event nor a book manifest accounts for: "
                f"{sorted(unexplained)[:5]} — either a self-check wrote into the "
                "user's folder, or a book was dropped in by hand in the last "
                "seconds and the agent has not scanned it yet"
            )

    return out


# ============================================================================
# The suite: prove the guard is green on a live tick and red on a leak.
# Everything below runs against a SYNTHETIC install in a temp tree — the user's
# real install is never read, written or stopped.
# ============================================================================

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""),
          flush=True)


class FakeInstall:
    """A synthetic production install we may safely abuse."""

    def __init__(self, root: Path, live: bool = True) -> None:
        self.root = root
        self.support = root / "AppSupport"
        self.log = root / "Logs" / "mp3-to-m4b.log"
        self.launch_agents = root / "LaunchAgents"
        self.watch = root / "Desktop" / "mp3-to-m4b"
        self.label = "com.arrivarus.mp3tom4b.agent"
        self.generation = "GEN-0001"
        for d in (self.support / "state", self.support / "queue" / "books",
                  self.support / "queue" / "commands", self.support / "covers",
                  self.support / "bin", self.support / "venv",
                  self.launch_agents, self.watch, self.log.parent):
            d.mkdir(parents=True, exist_ok=True)
        (self.support / "bin" / "mp3-to-m4b-agent").write_bytes(b"MACHO")
        (self.support / "bin" / "runner.sh").write_text("#!/bin/bash\n")
        (self.support / "install-receipt.json").write_text(json.dumps({
            "generation": self.generation, "label": self.label,
            "support_dir": str(self.support), "watch_dir": str(self.watch),
        }))
        (self.support / "state" / "state.json").write_text(json.dumps({
            "schema": 1, "agent": {"watch_dir": str(self.watch), "active": True,
                                   "install_generation": self.generation,
                                   "folder_access": "ok"}, "books": [],
        }))
        (self.support / "state" / "events.jsonl").write_text(
            json.dumps({"event": "agent_started", "ts": 1.0, "version": "1.0"}) + "\n")
        self.log.write_text(f"{BANNER} (v1.0)\n  folder access: ok\n")
        if live:
            (self.launch_agents / f"{self.label}.plist").write_bytes(
                plistlib.dumps({"Label": self.label,
                                "ProgramArguments": [
                                    str(self.support / "bin" / "mp3-to-m4b-agent")]}))

    def paths(self) -> Paths:
        return Paths(self.support, self.log, self.launch_agents, self.watch, self.label)

    # -- what the LIVE agent does every 300 s --------------------------------
    def tick(self, extra_events: list[dict] | None = None) -> None:
        with (self.support / "state" / "events.jsonl").open("a") as fh:
            fh.write(json.dumps({"event": "agent_started", "ts": time.time(),
                                 "version": "1.0", "argv": []}) + "\n")
            for rec in extra_events or []:
                fh.write(json.dumps(rec) + "\n")
        with self.log.open("a") as fh:
            fh.write(f"{BANNER} (v1.0)\n  watch dir: {self.watch}\n"
                     f"  folder access: ok\n  books found: 0\n")
        state = json.loads((self.support / "state" / "state.json").read_text())
        state["ts"] = time.time()
        (self.support / "state" / "state.json").write_text(json.dumps(state))


def _patched(fake: FakeInstall):
    """Run snapshot/diff against the fake install, never the user's."""
    return fake.paths()


def _snap(fake: FakeInstall) -> dict:
    return snapshot(fake.paths())


def _diff(before: dict, after: dict) -> list[str]:
    return diff(before, after, settle_s=0.0)


def scenario_live_tick_is_green(root: Path) -> None:
    print("\n— A: the user's agent doing its job is NOT a violation —")
    fake = FakeInstall(root / "live_tick")
    before = _snap(fake)
    check("a proven live install is detected", before["live"] is True)
    fake.tick()
    check("a plain 300 s tick (journal + log + state) is green",
          _diff(before, _snap(fake)) == [], str(_diff(before, _snap(fake))))

    # Several ticks, as in a 5.5-minute run.
    before = _snap(fake)
    for _ in range(3):
        fake.tick()
    check("three ticks in one window are still green",
          _diff(before, _snap(fake)) == [], str(_diff(before, _snap(fake))))

    # A tick that actually finds something: the USER drops a book, the agent scans
    # it (manifest + cover), builds it, and writes the .m4b into the watch folder.
    before = _snap(fake)
    book = fake.watch / "Книга"
    book.mkdir()                                   # ← the user's drop
    (fake.support / "queue" / "books" / "abc.json").write_text(
        json.dumps({"book_id": "abc", "src_dir": str(book), "status": "done"}))
    (fake.support / "covers" / "abc-gen-grad-deep.png").write_bytes(b"png")
    out_file = fake.watch / "Книга.m4b"
    out_file.write_text("m4b")                     # ← the agent's output
    fake.tick(extra_events=[
        {"event": "folder_access_probe", "ts": time.time(), "access": "denied",
         "watch_dir": str(fake.watch), "detail": "Permission denied (errno=13)"},
        {"event": "app_nudged", "ts": time.time(), "keys": ["book:abc:rev:tok"]},
        {"event": "build_started", "ts": time.time(), "book_id": "abc",
         "output_path": str(out_file)},
        {"event": "build_done", "ts": time.time(), "book_id": "abc",
         "output_path": str(out_file)},
    ])
    damage = _diff(before, _snap(fake))
    check("a tick that scans a user's new book and builds it (manifest, cover, "
          ".m4b in the watch folder) is green", damage == [], str(damage))


def scenario_leak_is_red(root: Path) -> None:
    print("\n— B: a self-check writing into the live tree is STILL red —")

    # (1) The 26.07 leak verbatim: one record, temp path, no agent run.
    fake = FakeInstall(root / "leak_bare")
    before = _snap(fake)
    with (fake.support / "state" / "events.jsonl").open("a") as fh:
        fh.write(json.dumps({
            "event": "folder_access_probe", "ts": time.time(), "access": "blocked",
            "elapsed_s": 1.005, "deadline_s": 1.0,
            "watch_dir": "/private/tmp/claude-501/scratchpad/wedge/watch",
        }) + "\n")
    damage = _diff(before, _snap(fake))
    check("the 26.07 leak (a probe record with a scratchpad path) is RED",
          any("outside the install" in d for d in damage), str(damage))
    check("…and is also caught as «no banner accounts for it»",
          any("banner" in d for d in damage), str(damage))

    # (2) The hard case the coordinator asked for: the test writes WHILE the live
    #     agent writes. The banner exists, so only content attribution can save us.
    fake = FakeInstall(root / "leak_concurrent")
    before = _snap(fake)
    fake.tick()                                   # the real agent ticks…
    with (fake.support / "state" / "events.jsonl").open("a") as fh:  # …and a test writes
        fh.write(json.dumps({
            "event": "folder_access_probe", "ts": time.time(), "access": "ok",
            "watch_dir": "/var/folders/ww/T/mp3tom4b-selfcheck-access-x/watch",
        }) + "\n")
    damage = _diff(before, _snap(fake))
    check("a test's record alongside a genuine tick is RED (content attribution)",
          any("outside the install" in d for d in damage), str(damage))

    # (3) A forged run: agent_started without a launchd banner.
    fake = FakeInstall(root / "leak_forged")
    before = _snap(fake)
    fake.tick()
    with (fake.support / "state" / "events.jsonl").open("a") as fh:
        fh.write(json.dumps({"event": "agent_started", "ts": time.time(),
                             "version": "1.0", "argv": []}) + "\n")
    damage = _diff(before, _snap(fake))
    check("a forged agent_started (no matching banner) is RED",
          any("launchd never started" in d for d in damage), str(damage))

    # (4) A test-written state.json loses the generation only launchd can supply.
    fake = FakeInstall(root / "leak_state")
    before = _snap(fake)
    fake.tick()
    (fake.support / "state" / "state.json").write_text(json.dumps({
        "schema": 1, "agent": {"watch_dir": str(fake.watch), "active": True,
                               "folder_access": "ok"}, "books": []}))
    damage = _diff(before, _snap(fake))
    check("a state.json written without the install generation is RED",
          any("generation" in d for d in damage), str(damage))

    # (5) Structural changes stay red even under a live install.
    fake = FakeInstall(root / "leak_structural")
    before = _snap(fake)
    fake.tick()
    (fake.launch_agents / "com.arrivarus.mp3tom4b.selfcheck.plist").write_bytes(
        plistlib.dumps({"Label": "x", "ProgramArguments": ["/bin/echo"]}))
    check("a plist dropped by a test is RED even while the agent ticks",
          any("plist" in d for d in _diff(before, _snap(fake))),
          str(_diff(before, _snap(fake))))

    fake = FakeInstall(root / "leak_pa0")
    before = _snap(fake)
    fake.tick()
    (fake.launch_agents / f"{fake.label}.plist").write_bytes(plistlib.dumps(
        {"Label": fake.label, "ProgramArguments": ["/bin/bash", "runner.sh"]}))
    check("a rewritten ProgramArguments[0] is RED (the v0.9 regression)",
          any("plist" in d for d in _diff(before, _snap(fake))),
          str(_diff(before, _snap(fake))))

    fake = FakeInstall(root / "leak_helper")
    before = _snap(fake)
    fake.tick()
    (fake.support / "bin" / "mp3-to-m4b-agent").write_bytes(b"MUTATED")
    check("mutated helper bytes are RED (the user's TCC grant is bound to them)",
          any("helper" in d for d in _diff(before, _snap(fake))),
          str(_diff(before, _snap(fake))))

    # (6) The user's books are not ours to touch.
    fake = FakeInstall(root / "leak_watch")
    (fake.watch / "Книга человека").mkdir()
    before = _snap(fake)
    fake.tick()
    (fake.watch / "test-book").mkdir()
    check("a folder appearing in the user's watch dir that no build event and no "
          "manifest account for is RED",
          any("appeared in the user's watch folder" in d
              for d in _diff(before, _snap(fake))),
          str(_diff(before, _snap(fake))))

    fake = FakeInstall(root / "leak_delete")
    (fake.watch / "Книга человека").mkdir()
    before = _snap(fake)
    fake.tick()
    (fake.watch / "Книга человека").rmdir()
    check("a folder DISAPPEARING from the user's watch dir is RED",
          any("disappeared" in d for d in _diff(before, _snap(fake))),
          str(_diff(before, _snap(fake))))

    # (7) Append-only: rewriting the journal is not a tick.
    fake = FakeInstall(root / "leak_truncate")
    before = _snap(fake)
    fake.tick()
    (fake.support / "state" / "events.jsonl").write_text("")
    check("truncating/rewriting the production journal is RED",
          any("SHRANK" in d or "replaced" in d for d in _diff(before, _snap(fake))),
          str(_diff(before, _snap(fake))))

    # (8) An unknown event kind is nobody's tick.
    fake = FakeInstall(root / "leak_kind")
    before = _snap(fake)
    fake.tick()
    with (fake.support / "state" / "events.jsonl").open("a") as fh:
        fh.write(json.dumps({"event": "selfcheck_marker", "ts": time.time()}) + "\n")
    check("an unknown event kind in the production journal is RED",
          any("unknown kind" in d for d in _diff(before, _snap(fake))),
          str(_diff(before, _snap(fake))))


def scenario_clean_machine_is_strict(root: Path) -> None:
    print("\n— C: on a machine with NO live install the guard stays strict —")
    fake = FakeInstall(root / "clean", live=False)   # tree + log, but no plist/job
    before = _snap(fake)
    check("no plist ⇒ the install is not proven live", before["live"] is False)
    fake.tick()   # exactly the writes that are FINE under a live install
    damage = _diff(before, _snap(fake))
    check("the very same writes are RED without a proven live install",
          any("no live install" in d for d in damage), str(damage))

    # And the strictest case of all: the tree appearing out of nothing.
    empty = root / "empty"
    (empty / "Logs").mkdir(parents=True)
    (empty / "LaunchAgents").mkdir(parents=True)
    (empty / "Desktop" / "mp3-to-m4b").mkdir(parents=True)
    paths = Paths(empty / "AppSupport", empty / "Logs" / "mp3-to-m4b.log",
                  empty / "LaunchAgents", empty / "Desktop" / "mp3-to-m4b",
                  "com.arrivarus.mp3tom4b.agent")
    before = snapshot(paths)
    (paths.support / "state").mkdir(parents=True)
    (paths.support / "state" / "events.jsonl").write_text(
        json.dumps({"event": "folder_access_probe", "ts": 1.0, "access": "blocked",
                    "watch_dir": "/var/folders/x/y"}) + "\n")
    damage = diff(before, snapshot(paths), settle_s=0.0)
    check("a tree created from nothing on a clean machine is RED",
          any("created or deleted" in d for d in damage), str(damage))


def scenario_real_install_readonly() -> None:
    print("\n— D: the guard reads the REAL install without touching it —")
    p = default_paths()
    before = snapshot(p)
    after = snapshot(p)
    check("two back-to-back snapshots of the real system agree",
          diff(before, after, settle_s=0.0) == [],
          str(diff(before, after, settle_s=0.0)))
    check("the real watch folder is resolved from the receipt, not assumed",
          str(p.watch).endswith("mp3-to-m4b"), str(p.watch))
    live = "live install detected" if before["live"] else "no live install"
    check(f"the real machine is classified: {live}", True,
          f"support={before['structural']['support_exists']} "
          f"plists={len(before['structural']['plists'])} "
          f"jobs={list(before['structural']['jobs'])}")


def run() -> int:
    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-blast-"))
    print(f"self-check tree: {root}\n")
    try:
        scenario_live_tick_is_green(root)
        scenario_leak_is_red(root)
        scenario_clean_machine_is_strict(root)
        scenario_real_install_readonly()
    except KeyboardInterrupt:
        import shutil
        shutil.rmtree(root, ignore_errors=True)
        print("\n  interrupted — fixtures removed")
        return 130

    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    failed = [n for n, ok, _ in _RESULTS if not ok]
    print(f"\n§blast-radius self-check: {passed}/{total} checks passed")
    if failed:
        print("  FAILED checks: " + "; ".join(failed))
        print(f"(fixtures kept at {root} for inspection; safe to delete)")
        return 1
    import shutil
    shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(run())
