"""§nudge self-check — the agent auto-raises the app on NEW confirm/grouping edges.

Run it standalone:

    python3 -m agent.selfcheck_nudge

Proves the rising-edge nudge layer (scan._publish_showcase_and_maybe_open) on a
REAL temp tree with REAL (tiny, ffmpeg-generated) mp3s, driving the PRODUCTION
path (run_scan → state.json + notified.json ledger → ONE launch command per new
edge). The launch command is captured through the ``MP3TOM4B_NUDGE_CMD`` test
seam (a recorder script) — the REAL app is never opened by this suite.

  new book        first scan of a fresh book → exactly ONE nudge; the ledger
                  records ``book:<id>:<rev[:16]>:<token[:16]>``.
  re-scan         the same pending book on later scans → ZERO nudges.
  rapid-fire      TWO new books in one scan → still ONE nudge (per publication,
                  not per book).
  grouping        a new loose-mp3 grouping prompt → ONE nudge with a
                  ``group:<gid>:<rev[:16]>:<token[:16]>`` ledger key.
  group E10       an UNSTABLE (mid-copy) changed loose set arms nothing and
                  nudges nothing (prior prompt carried forward); once stable it
                  re-arms with ONE nudge. An unchanged set skips the debounce.
  confirm-cycle   confirm-build → drain → done: the post-drain run_scan nudges
                  ZERO times and PRUNES the built book's key (loop broken).
  reconvert       «Собрать заново» re-arms with a FRESH token → a NEW key →
                  exactly ONE re-notification.
  suppression     scratch tree (MP3TOM4B_SUPPORT_DIR) without a nudge cmd →
                  silent (this is what keeps every other suite from popping the
                  real app).
  failure path    a failing launch command journals ``app_nudge_failed`` and
                  never crashes the scan.

Re-drop signals (source_rev v2 = +st_ino/st_dev, presence ledger, v1→v2 migration):

  rev v2          the stored rev IS the v2 digest (≠ legacy v1) and manifests
                  carry ``source_rev_v: 2``.
  copy-redrop     a Finder-style COPY of a done book back into the watch root
                  (same relpath/size/mtime, NEW inodes) re-arms pending-confirm
                  (fresh token, empty processed_keys) with exactly ONE nudge.
  move-in         a same-volume MOVE out and back in (SAME inode → same rev) is
                  caught by the presence ledger (present→absent→present) and
                  re-arms a done book with exactly ONE nudge.
  untouched/login lying done books across repeated scans (RunAtLoad) → ZERO
                  nudges, ZERO re-arms (present→present, rev stable).
  migration       first scan over LEGACY (v1) revs — done book, pending book,
                  pending group, resolved grouping ledger — silently upgrades
                  every rev/key in place: statuses/tokens preserved, ZERO
                  nudges, ZERO re-arms (no window storm after an agent update).

It runs ONLY its own checks (cross-suite regression is orchestrated once by
``agent.selfcheck_all`` — no nested re-runs). ffmpeg/ffprobe are required
(real mp3s); the run SKIPS (rc 1) if missing.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

# --- tiny assertion harness -------------------------------------------------

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


# --- tool plumbing ----------------------------------------------------------


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _has_tools() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _make_mp3(path: Path, *, seconds: float = 1.0, tags: dict | None = None) -> None:
    """Write a real (cover-less) mp3 via an ffmpeg sine tone, with optional ID3."""
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-c:a", "libmp3lame", "-id3v2_version", "3",
    ]
    for k, v in (tags or {}).items():
        argv += ["-metadata", f"{k}={v}"]
    argv.append(str(path))
    subprocess.run(argv, check=True, capture_output=True)


# --- command helpers (exactly the app's shapes) ------------------------------


def _drop_command(commands_dir: Path, payload: dict) -> Path:
    commands_dir.mkdir(parents=True, exist_ok=True)
    cmd_id = payload.get("cmd_id") or str(uuid.uuid4())
    payload.setdefault("cmd_id", cmd_id)
    final = commands_dir / f"{cmd_id}.json"
    tmp = commands_dir / f".{cmd_id}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


def _confirm_build_cmd(manifest: dict) -> dict:
    bid = manifest["book_id"]
    rev = manifest["source_rev"]
    return {
        "cmd_id": str(uuid.uuid4()),
        "action": "confirm-build",
        "book_id": bid,
        "source_rev": rev,
        "confirm_token": manifest["confirm_token"],
        # D17: the app echoes the build_token it saw, proving the command was
        # minted from a COMPLETE manifest and not from the early-nudge skeleton.
        "build_token": manifest.get("build_token"),
        "idempotency_key": f"{bid}:{rev[:16]}",
        "params": dict(manifest.get("params", {})),
        "ts": time.time(),
    }


def _reconvert_cmd(book_id: str) -> dict:
    return {
        "cmd_id": str(uuid.uuid4()),
        "action": "reconvert",
        "book_id": book_id,
        "idempotency_key": f"reconvert:{book_id}",
        "ts": time.time(),
    }


# --- fixture / observation helpers -------------------------------------------


def _nudge_count(log: Path) -> int:
    """How many times the recorder seam was invoked so far."""
    try:
        return len(log.read_text(encoding="utf-8").splitlines())
    except FileNotFoundError:
        return 0


def _ledger_keys(config, state) -> set:
    data = state.read_json(config.notified_file(), default=None)
    keys = data.get("keys") if isinstance(data, dict) else None
    return set(k for k in keys if isinstance(k, str)) if isinstance(keys, list) else set()


def _events_of(state, kind: str) -> list[dict]:
    return [e for e in state.read_events() if e.get("event") == kind]


def _manifest_for(config, state, suffix: str) -> dict | None:
    for p in config.books_dir().glob("*.json"):
        m = state.read_json(p)
        if isinstance(m, dict) and str(m.get("src_dir", "")).endswith(suffix):
            return m
    return None


def _book_key(manifest: dict) -> str:
    rev = str(manifest.get("source_rev") or "")[:16]
    token = str(manifest.get("confirm_token") or "")[:16]
    return f"book:{manifest['book_id']}:{rev}:{token}"


def _pending_group(state) -> dict | None:
    s = state.read_state(default=None)
    if not isinstance(s, dict):
        return None
    groups = s.get("pending_groups")
    if isinstance(groups, list) and groups and isinstance(groups[0], dict):
        return groups[0]
    return None


def _group_key(group: dict) -> str:
    rev = str(group.get("rev") or "")[:16]
    token = str(group.get("token") or "")[:16]
    return f"group:{group['group_id']}:{rev}:{token}"


def run() -> int:
    if not _has_tools():
        print("§nudge self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-nudge-"))
    support = root / "support"
    watch = root / "watch"
    support.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)

    # Recorder seam: every "app raise" appends one line here — no real app opens.
    nudge_log = root / "nudges.log"
    recorder = root / "recorder.sh"
    recorder.write_text(
        f"#!/bin/sh\nprintf 'nudge\\n' >> {shlex.quote(str(nudge_log))}\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)

    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)
    os.environ["MP3TOM4B_COVER_WEB"] = "0"       # offline determinism
    os.environ["MP3TOM4B_STABILITY_DEBOUNCE_S"] = "0"  # fixtures already stable
    os.environ["MP3TOM4B_NUDGE_CMD"] = shlex.quote(str(recorder))

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import config, dispatcher, scan, state  # noqa: E402

    print(f"self-check tree: {root}\n  support: {support}\n  watch:   {watch}\n")

    # === (a) new book → exactly ONE nudge ====================================
    book_a = watch / "Книга А"
    for i in (1, 2):
        _make_mp3(book_a / f"{i:02d}.mp3", tags={"title": f"Глава {i}",
                                                 "album": "Книга А"})
    scan.run_scan()
    man_a = _manifest_for(config, state, "Книга А")
    check("new book: first scan fires exactly ONE nudge",
          _nudge_count(nudge_log) == 1, f"count={_nudge_count(nudge_log)}")
    check("new book: an app_nudged event was journalled",
          len(_events_of(state, "app_nudged")) == 1)
    key_a0 = _book_key(man_a) if man_a else "?"
    check("new book: ledger holds book:<id>:<rev16>:<token16>",
          man_a is not None and key_a0 in _ledger_keys(config, state),
          f"key={key_a0}")

    # === (b) re-scan of the SAME pending book → ZERO new nudges ==============
    scan.run_scan()
    scan.run_scan()
    check("re-scan: repeated scans of the same pending book add ZERO nudges",
          _nudge_count(nudge_log) == 1, f"count={_nudge_count(nudge_log)}")

    # === (e) rapid-fire: TWO new books in ONE scan → ONE nudge ===============
    for name in ("Книга Б", "Книга В"):
        _make_mp3(watch / name / "01.mp3", tags={"album": name})
    scan.run_scan()
    check("rapid-fire: two new books in one scan fire ONE nudge (per publication)",
          _nudge_count(nudge_log) == 2, f"count={_nudge_count(nudge_log)}")

    # === (f) new grouping prompt → ONE nudge =================================
    _make_mp3(watch / "loose-1.mp3", tags={"title": "Loose 1"})
    _make_mp3(watch / "loose-2.mp3", tags={"title": "Loose 2"})
    scan.run_scan()
    grp = _pending_group(state)
    check("grouping: a new loose-mp3 prompt fires ONE nudge",
          grp is not None and _nudge_count(nudge_log) == 3,
          f"count={_nudge_count(nudge_log)} group={bool(grp)}")
    check("grouping: ledger holds group:<gid>:<rev16>:<token16>",
          grp is not None and _group_key(grp) in _ledger_keys(config, state))
    scan.run_scan()
    check("grouping: an unchanged pending group adds ZERO nudges",
          _nudge_count(nudge_log) == 3, f"count={_nudge_count(nudge_log)}")

    # === group E10: unstable changed loose set → no arm, no nudge ============
    real_stable = scan._files_are_stable
    try:
        # (mp3s, folder=None) — the debounce re-lists the directory now (M-E).
        scan._files_are_stable = lambda mp3s, folder=None: False  # simulate mid-copy
        _make_mp3(watch / "loose-3.mp3", tags={"title": "Loose 3"})
        scan.run_scan()
        grp_mid = _pending_group(state)
        check("group E10: an unstable CHANGED set keeps the PRIOR prompt, no nudge",
              grp_mid is not None and grp_mid.get("rev") == grp.get("rev")
              and _nudge_count(nudge_log) == 3
              and len(_events_of(state, "group_still_copying")) >= 1,
              f"count={_nudge_count(nudge_log)} "
              f"still_copying={len(_events_of(state, 'group_still_copying'))}")
    finally:
        scan._files_are_stable = real_stable
    scan.run_scan()
    grp2 = _pending_group(state)
    check("group E10: once stable the set re-arms (new rev) with ONE nudge",
          grp2 is not None and grp2.get("rev") != grp.get("rev")
          and _nudge_count(nudge_log) == 4,
          f"count={_nudge_count(nudge_log)}")

    # === (d) confirm-cycle: build to done → ZERO nudges, key pruned ==========
    _drop_command(config.commands_dir(), _confirm_build_cmd(man_a))
    dispatcher.drain_commands()  # builds + closes with run_scan()
    man_a_done = state.read_json(config.books_dir() / f"{man_a['book_id']}.json")
    check("confirm-cycle: the book reached done (real build ran)",
          isinstance(man_a_done, dict) and man_a_done.get("status") == "done",
          f"status={man_a_done.get('status') if isinstance(man_a_done, dict) else '?'}")
    check("confirm-cycle: the post-drain publication fires ZERO nudges (loop broken)",
          _nudge_count(nudge_log) == 4, f"count={_nudge_count(nudge_log)}")
    check("confirm-cycle: the built book's key is PRUNED from the ledger",
          key_a0 not in _ledger_keys(config, state))

    # === (c) reconvert → fresh token → NEW key → ONE re-notification =========
    _drop_command(config.commands_dir(), _reconvert_cmd(man_a["book_id"]))
    dispatcher.drain_commands()
    man_a2 = state.read_json(config.books_dir() / f"{man_a['book_id']}.json")
    key_a2 = _book_key(man_a2) if isinstance(man_a2, dict) else "?"
    check("reconvert: the book is re-armed pending-confirm with ONE new nudge",
          isinstance(man_a2, dict)
          and man_a2.get("status") == "pending-confirm"
          and _nudge_count(nudge_log) == 5,
          f"count={_nudge_count(nudge_log)}")
    check("reconvert: the ledger key is NEW (fresh confirm_token → new edge)",
          key_a2 != key_a0 and key_a2 in _ledger_keys(config, state),
          f"old={key_a0} new={key_a2}")

    # === suppression: scratch tree without a nudge cmd → silent ==============
    nudged_before = len(_events_of(state, "app_nudged"))
    del os.environ["MP3TOM4B_NUDGE_CMD"]
    try:
        _make_mp3(watch / "Книга Г" / "01.mp3", tags={"album": "Книга Г"})
        scan.run_scan()
        check("suppression: SUPPORT_DIR set + no NUDGE_CMD → no launch, no event",
              _nudge_count(nudge_log) == 5
              and len(_events_of(state, "app_nudged")) == nudged_before,
              f"count={_nudge_count(nudge_log)}")
    finally:
        os.environ["MP3TOM4B_NUDGE_CMD"] = shlex.quote(str(recorder))

    # === failure path: a failing launch cmd journals and never raises ========
    os.environ["MP3TOM4B_NUDGE_CMD"] = "/usr/bin/false"
    try:
        _make_mp3(watch / "Книга Д" / "01.mp3", tags={"album": "Книга Д"})
        scan.run_scan()  # must not raise
        check("failure path: rc≠0 journals app_nudge_failed, scan survives",
              len(_events_of(state, "app_nudge_failed")) >= 1
              and _nudge_count(nudge_log) == 5,
              f"failed_events={len(_events_of(state, 'app_nudge_failed'))}")
    finally:
        os.environ["MP3TOM4B_NUDGE_CMD"] = shlex.quote(str(recorder))

    # ======================================================================
    # Re-drop signals: source_rev v2 (inode+dev), presence ledger, migration.
    # ======================================================================

    def _flip_done(manifest: dict) -> dict:
        """Mark a manifest done in place (test seam simulating a finished build)."""
        path = config.books_dir() / f"{manifest['book_id']}.json"
        man = state.read_json(path)
        man["status"] = "done"
        man["result"] = {
            "output": "test.m4b",
            "output_path": str(watch / "test.m4b"),
            "built_at": time.time(),
        }
        state.write_json_atomic(path, man)
        return man

    def _presence_entry(bid: str) -> dict | None:
        data = state.read_json(config.presence_file(), default=None)
        books = data.get("books") if isinstance(data, dict) else None
        entry = books.get(bid) if isinstance(books, dict) else None
        return entry if isinstance(entry, dict) else None

    def _book_mp3s(folder: Path) -> list[Path]:
        return sorted(folder.glob("*.mp3"))

    # === rev v2: stored rev is the inode-aware digest, ≠ legacy v1 ===========
    man_v = _manifest_for(config, state, "Книга В")
    dir_v = Path(man_v["src_dir"])
    files_v = _book_mp3s(dir_v)
    check("rev v2: manifest rev == source_rev_for (v2) and ≠ legacy v1",
          man_v.get("source_rev") == scan.source_rev_for(files_v, dir_v)
          and man_v.get("source_rev") != scan.source_rev_legacy_for(files_v, dir_v))
    check("rev v2: manifests carry source_rev_v == 2",
          man_v.get("source_rev_v") == 2)

    # === (a) copy-redrop: new inodes on identical content → re-arm + 1 =======
    book_e = watch / "Книга Е"
    for i in (1, 2):
        _make_mp3(book_e / f"{i:02d}.mp3", tags={"title": f"Глава {i}",
                                                 "album": "Книга Е"})
    scan.run_scan()
    base = _nudge_count(nudge_log)  # the new book itself nudged once
    man_e = _manifest_for(config, state, "Книга Е")
    _flip_done(man_e)
    scan.run_scan()  # publish done, prune the pending key, seed presence
    check("copy-redrop setup: a done book settles silently",
          _nudge_count(nudge_log) == base, f"count={_nudge_count(nudge_log)}")

    files_e = _book_mp3s(book_e)
    legacy_before = scan.source_rev_legacy_for(files_e, book_e)
    rev_before = man_e["source_rev"]
    # Finder-style copy re-drop: byte-identical files, preserved mtimes, NEW inodes.
    backup = root / "backup-е"
    shutil.copytree(book_e, backup)
    shutil.rmtree(book_e)
    shutil.copytree(backup, book_e)
    files_e = _book_mp3s(book_e)
    check("copy-redrop: the copy keeps the LEGACY digest but flips the v2 rev",
          scan.source_rev_legacy_for(files_e, book_e) == legacy_before
          and scan.source_rev_for(files_e, book_e) != rev_before)
    scan.run_scan()
    man_e2 = _manifest_for(config, state, "Книга Е")
    check("copy-redrop: the done book re-arms pending-confirm with ONE nudge",
          man_e2.get("status") == "pending-confirm"
          and _nudge_count(nudge_log) == base + 1,
          f"status={man_e2.get('status')} count={_nudge_count(nudge_log)}")
    check("copy-redrop: fresh confirm_token + new rev + EMPTY processed_keys",
          man_e2.get("confirm_token") != man_e.get("confirm_token")
          and man_e2.get("source_rev") != rev_before
          and man_e2.get("processed_keys") == [])
    scan.run_scan()
    check("copy-redrop: the re-armed book is stable on re-scan (no flapping)",
          _nudge_count(nudge_log) == base + 1, f"count={_nudge_count(nudge_log)}")

    # === (b) move-in: same inode, presence present→absent→present → 1 ========
    _flip_done(man_e2)
    scan.run_scan()
    base = _nudge_count(nudge_log)
    ino_before = _book_mp3s(book_e)[0].stat().st_ino
    parked = root / "parked-е"
    os.rename(book_e, parked)  # move OUT (same volume → inode preserved)
    scan.run_scan()
    ent = _presence_entry(man_e2["book_id"])
    check("move-in: after move-out the presence ledger marks the book ABSENT",
          ent is not None and ent.get("present") is False
          and _nudge_count(nudge_log) == base,
          f"entry={ent} count={_nudge_count(nudge_log)}")
    os.rename(parked, book_e)  # move back IN
    check("move-in: the move kept the SAME inode (rev signal stays silent)",
          _book_mp3s(book_e)[0].stat().st_ino == ino_before)
    scan.run_scan()
    man_e3 = _manifest_for(config, state, "Книга Е")
    check("move-in: the reappeared done book re-arms pending-confirm with ONE nudge",
          man_e3.get("status") == "pending-confirm"
          and _nudge_count(nudge_log) == base + 1,
          f"status={man_e3.get('status')} count={_nudge_count(nudge_log)}")
    check("move-in: re-arm came from PRESENCE (rev unchanged), journalled",
          man_e3.get("source_rev") == man_e2.get("source_rev")
          and len(_events_of(state, "book_rearmed_reappeared")) == 1)
    scan.run_scan()
    check("move-in: stable after re-arm (present→present adds nothing)",
          _nudge_count(nudge_log) == base + 1, f"count={_nudge_count(nudge_log)}")

    # === (c)+(d) untouched books / login storm: N lying done books → 0 =======
    _flip_done(man_e3)
    man_g = _manifest_for(config, state, "Книга Г")
    _flip_done(man_g)
    scan.run_scan()  # publish both done rows
    base = _nudge_count(nudge_log)
    rearms_before = len(_events_of(state, "book_rearmed_reappeared"))
    for _ in range(3):  # RunAtLoad / login fires run_scan repeatedly
        scan.run_scan()
    man_e4 = _manifest_for(config, state, "Книга Е")
    check("login: repeated scans over lying done books → ZERO nudges, ZERO re-arms",
          _nudge_count(nudge_log) == base
          and man_e4.get("status") == "done"
          and len(_events_of(state, "book_rearmed_reappeared")) == rearms_before,
          f"count={_nudge_count(nudge_log)}")
    ent_e = _presence_entry(man_e4["book_id"])
    check("login: presence holds the lying books as present (steady state)",
          ent_e is not None and ent_e.get("present") is True)

    # === (e) migration: legacy v1 revs → silent in-place upgrade, ZERO raises =
    # Simulate the tree a PRE-v2 agent left behind: v1 revs in a done manifest,
    # a pending manifest, the pending group, and v1-keyed notified entries; no
    # presence ledger at all. The first v2 scan must upgrade everything in place
    # without a single nudge or re-arm (the "window storm" guard).
    base = _nudge_count(nudge_log)
    rearms_before = len(_events_of(state, "book_rearmed_reappeared"))

    # done book → legacy rev
    path_e = config.books_dir() / f"{man_e4['book_id']}.json"
    man = state.read_json(path_e)
    man["source_rev"] = scan.source_rev_legacy_for(_book_mp3s(book_e), book_e)
    man.pop("source_rev_v", None)
    state.write_json_atomic(path_e, man)
    token_e = man.get("confirm_token")

    # pending book → legacy rev + legacy-format notified key
    man_v = _manifest_for(config, state, "Книга В")
    dir_v = Path(man_v["src_dir"])
    files_v = _book_mp3s(dir_v)
    legacy_v = scan.source_rev_legacy_for(files_v, dir_v)
    key_v_v2 = _book_key(man_v)
    path_v = config.books_dir() / f"{man_v['book_id']}.json"
    man = state.read_json(path_v)
    man["source_rev"] = legacy_v
    man.pop("source_rev_v", None)
    state.write_json_atomic(path_v, man)
    key_v_legacy = _book_key(man)
    ledger_keys = _ledger_keys(config, state)
    ledger_keys.discard(key_v_v2)
    ledger_keys.add(key_v_legacy)

    # pending group → legacy rev + legacy-format notified key
    grp_now = _pending_group(state)
    loose_files = scan._list_loose_mp3s(watch)  # real paths in scan order
    legacy_g = scan.group_rev_legacy_for(loose_files, watch)
    key_g_v2 = _group_key(grp_now)
    st = state.read_state()
    st["pending_groups"][0]["rev"] = legacy_g
    key_g_legacy = _group_key(st["pending_groups"][0])
    state.write_state(st)
    ledger_keys.discard(key_g_v2)
    ledger_keys.add(key_g_legacy)
    state.write_json_atomic(config.notified_file(), {"keys": sorted(ledger_keys)})

    # pre-v2 agents had no presence ledger
    config.presence_file().unlink(missing_ok=True)

    scan.run_scan()
    man_e5 = state.read_json(path_e)
    man_v2 = state.read_json(path_v)
    grp_after = _pending_group(state)
    keys_after = _ledger_keys(config, state)
    check("migration: the first v2 scan fires ZERO nudges and ZERO re-arms",
          _nudge_count(nudge_log) == base
          and len(_events_of(state, "book_rearmed_reappeared")) == rearms_before,
          f"count={_nudge_count(nudge_log)}")
    check("migration: done book upgraded in place (v2 rev, status/token kept)",
          man_e5.get("source_rev") == scan.source_rev_for(_book_mp3s(book_e), book_e)
          and man_e5.get("source_rev_v") == 2
          and man_e5.get("status") == "done"
          and man_e5.get("confirm_token") == token_e)
    check("migration: pending book upgraded in place (status+token preserved)",
          man_v2.get("source_rev") == scan.source_rev_for(files_v, dir_v)
          and man_v2.get("status") == "pending-confirm"
          and man_v2.get("confirm_token") == man_v.get("confirm_token"))
    check("migration: notified key swapped v1→v2 for the pending book (no re-raise)",
          key_v_v2 in keys_after and key_v_legacy not in keys_after)
    check("migration: pending group re-armed to the v2 rev with its TOKEN kept",
          grp_after is not None
          and grp_after.get("rev") == scan.group_rev_for(loose_files, watch)
          and grp_after.get("token") == grp_now.get("token"))
    check("migration: notified key swapped v1→v2 for the pending group",
          key_g_v2 in keys_after and key_g_legacy not in keys_after)
    check("migration: presence ledger reseeded with the lying books as present",
          (_presence_entry(man_e5["book_id"]) or {}).get("present") is True
          and len(_events_of(state, "source_rev_migrated")) >= 2)
    scan.run_scan()
    check("migration: steady after the upgrade scan (second scan adds nothing)",
          _nudge_count(nudge_log) == base, f"count={_nudge_count(nudge_log)}")

    # === (e2) migration of a RESOLVED grouping ledger (both keys honored) =====
    gid = grp_now["group_id"]
    st = state.read_state()
    st["pending_groups"] = []
    st["grouping_processed"] = [scan.grouping_idempotency_key(gid, legacy_g)]
    state.write_state(st)
    keys = _ledger_keys(config, state)
    keys.discard(key_g_v2)
    state.write_json_atomic(config.notified_file(), {"keys": sorted(keys)})
    scan.run_scan()
    st = state.read_state()
    v2_key = scan.grouping_idempotency_key(
        gid, scan.group_rev_for(loose_files, watch))
    check("grouping-ledger migration: a v1-resolved set stays silent (no prompt)",
          st.get("pending_groups") == [] and _nudge_count(nudge_log) == base,
          f"count={_nudge_count(nudge_log)}")
    check("grouping-ledger migration: the v2 key is back-filled into the ledger",
          v2_key in (st.get("grouping_processed") or []))

    # === (d17) TWO PUBLICATIONS, ONE NUDGE — и оба канала считают одинаково ===
    # D17 публикует книгу ДВАЖДЫ: скелет на ~0.8 с, потом дозаполненный манифест.
    # Инвариант I2 держится не дисциплиной, а тем, что confirm_token чеканится ОДИН
    # раз (на скелете) и переносится всеми фазами ⇒ ключ ребра у скелета и у ready
    # один и тот же. Здесь это проверяется на ПРОДАКШН-пути через тестовый шов
    # MP3TOM4B_HALT_AFTER_PHASE, который останавливает дозаполнение так, как это
    # сделал бы краш.
    base_d17 = _nudge_count(nudge_log)
    os.environ["MP3TOM4B_HALT_AFTER_PHASE"] = "skeleton"
    _make_mp3(watch / "Книга Фаза" / "01.mp3", tags={"album": "Книга Фаза"})
    scan.run_scan()
    os.environ.pop("MP3TOM4B_HALT_AFTER_PHASE")
    man_skel = _manifest_for(config, state, "Книга Фаза")
    key_skel = _book_key(man_skel) if isinstance(man_skel, dict) else "?"
    check("D17: публикация СКЕЛЕТА поднимает окно ровно один раз",
          isinstance(man_skel, dict)
          and scan.manifest_phase(man_skel) == "skeleton"
          and _nudge_count(nudge_log) == base_d17 + 1,
          f"count={_nudge_count(nudge_log)} phase="
          f"{scan.manifest_phase(man_skel) if isinstance(man_skel, dict) else '?'}")
    scan.run_scan()                       # вторая публикация: та же книга, ready
    man_ready = _manifest_for(config, state, "Книга Фаза")
    key_ready = _book_key(man_ready) if isinstance(man_ready, dict) else "?!"
    check("D17: ключ ребра у ready СОВПАДАЕТ с ключом скелета (I2 — по построению)",
          key_ready == key_skel, f"{key_skel} vs {key_ready}")
    check("D17: вторая публикация той же книги НЕ поднимает окно",
          _nudge_count(nudge_log) == base_d17 + 1
          and scan.manifest_phase(man_ready) == "ready",
          f"count={_nudge_count(nudge_log)}")
    check("D17: в леджере одно ребро на книгу, а не два",
          sum(1 for k in _ledger_keys(config, state)
              if k.startswith(f"book:{man_ready['book_id']}:")) == 1)

    # ВТОРОЙ КАНАЛ. У приложения свой rising-edge по state.json, мимо агентского
    # леджера. Он не поднимет окно во второй раз ровно потому, что считает новизну
    # ТОЙ ЖЕ функцией — `NudgeEdge.bookKey` в app/StateModel.swift. До сих пор это
    # держалось комментарием «byte-for-byte mirror»; здесь форма приложения
    # ВЫВОДИТСЯ из его исходника и сравнивается с ключом, который агент только что
    # записал. Разъедутся молча — вернётся второй подъём окна.
    swift_src = (repo_root / "app" / "StateModel.swift").read_text(encoding="utf-8")
    m_key = re.search(r'"book:\\\(([A-Za-z]+)\):\\\(([A-Za-z]+)\.prefix\((\d+)\)\):'
                      r'\\\(([A-Za-z]+)\.prefix\((\d+)\)\)"', swift_src)
    check("второй канал: формула ключа найдена в app/StateModel.swift "
          "(промах якоря = красное, не «нечего проверять»)", m_key is not None)
    if m_key is not None:
        app_key = (f"book:{man_ready['book_id']}:"
                   f"{man_ready['source_rev'][:int(m_key.group(3))]}:"
                   f"{man_ready['confirm_token'][:int(m_key.group(5))]}")
        check("второй канал: ключ приложения = ключ агента (окно не поднимут дважды)",
              app_key == scan._book_edge_key(man_ready),
              f"app={app_key} agent={scan._book_edge_key(man_ready)}")
        check("второй канал: ключ приложения уже лежит в агентском леджере ⇒ "
              "для приложения это ребро НЕ новое",
              app_key in _ledger_keys(config, state))

    return _finish(root)


def _finish(root: Path) -> int:
    # Flat verification: this suite runs ONLY its own checks. Cross-suite
    # regression is orchestrated once by ``agent.selfcheck_all`` (no nested re-runs).
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    failed = [name for name, ok, _ in _RESULTS if not ok]
    print(f"\n§nudge self-check: {passed}/{total} checks passed")
    if failed:
        print("  FAILED checks: " + "; ".join(failed))
    print(f"(temp tree left at {root} for inspection; safe to delete)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
