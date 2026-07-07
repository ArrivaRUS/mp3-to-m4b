"""§cancel self-check — empirical proof of COOPERATIVE build cancellation (D13).

Run it standalone:

    python3 -m agent.selfcheck_cancel

D13's contract is safety-critical and easy to get subtly wrong (orphaned ffmpeg,
a half-written ``.m4b`` left on disk, a double-consumed command, a corrupted
status). Compile-checks cannot prove any of that — only a REAL build that is
actually interrupted can. So this suite drives the real engine on a throwaway
tree (``MP3TOM4B_SUPPORT_DIR`` / ``MP3TOM4B_WATCH_DIR`` redirect everything; the
user's real Application Support is never touched) and asserts the OBSERVABLE
outcomes of a cancel:

  cancel works fast   a book whose build is cancelled lands back at
                      ``pending-confirm`` (NOT ``error`` — cancel is not failure),
                      with source_rev / confirm_token intact so it can rebuild.
  no orphaned ffmpeg  the ffmpeg child the build spawned (captured live by PPID
                      while it ran) is DEAD after the cancel — no zombie, no
                      detached encoder still chewing CPU.
  no half .m4b        the hidden ``.<name>.m4b.*.tmp`` is swept AND the final
                      ``.m4b`` does NOT exist (a cancel must never publish a
                      partial output).
  journalled          a ``build_cancelled`` event is recorded; the cancel command
                      is CONSUMED (the building agent owns that unwind, so drain
                      never re-handles it → no double processing).
  idempotent          a SECOND cancel for the now-pending book is a no-op
                      (``cancel_moot``, command deleted, status untouched); a
                      cancel for a ``done`` book is likewise moot and leaves it done.
  rebuildable         after a cancel the book confirm-builds again all the way to
                      ``done`` with a valid ``.m4b`` (the token stayed valid).

To make the interruption DETERMINISTIC (silence re-encodes far faster than real
time — ~6s of wall-clock for an hour of silence), we:
  · give the book a LONG virtual duration (several multi-minute chapters) so
    ffmpeg is guaranteed to still be encoding for several seconds, and
  · drop the cancel command BEFORE launching the build in a worker thread, so the
    very first poll tick (≤ CANCEL_POLL_INTERVAL_S) sees it and tears ffmpeg down
    while it is unmistakably mid-encode.
The main thread meanwhile captures the live ffmpeg child's pid (via ``ps`` by
PPID) so "no orphan" is asserted against a process we KNOW existed, not a guess.

It runs ONLY its own checks (cross-suite regression is orchestrated once by
``agent.selfcheck_all`` — there is no nested re-run here) and returns 0 ⇔ every
check here passed. Requires ffmpeg + ffprobe on PATH; if either is missing it says
so and exits non-zero. It writes only inside its temp tree (plus each book's
``.m4b`` next to its source folder, which is inside the temp watch dir).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

# --- tiny assertion harness (same shape as the sibling self-checks) ----------

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


# --- ffmpeg helpers ----------------------------------------------------------


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _has_tools() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _make_silence_mp3(path: Path, *, seconds: float, tags: dict | None = None) -> None:
    """Write a real (silent) mp3 of ``seconds`` virtual length via anullsrc.

    Silence keeps the suite offline + deterministic; the LENGTH is what matters
    — long enough that the AAC re-encode is still running when our cancel's first
    poll tick fires, so the interruption is reliable rather than racy.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(seconds), "-c:a", "libmp3lame", "-b:a", "192k",
    ]
    for k, v in (tags or {}).items():
        argv += ["-metadata", f"{k}={v}"]
    argv.append(str(path))
    subprocess.run(argv, check=True, capture_output=True)


# --- process-tree helpers (the "no orphan" proof) ----------------------------


def _ffmpeg_children(parent_pid: int) -> list[int]:
    """PIDs of ffmpeg processes whose PPID is ``parent_pid`` (best-effort).

    We spawn ffmpeg as a direct child (subprocess.Popen), so the build's encoder
    is exactly an ffmpeg whose parent is this interpreter. ``ps`` is the portable
    way to read the live tree on macOS without extra deps.
    """
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid,ppid,comm"], capture_output=True, text=True
        ).stdout
    except OSError:
        return []
    pids: list[int] = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        comm = parts[2]
        if ppid == parent_pid and "ffmpeg" in os.path.basename(comm).lower():
            pids.append(pid)
    return pids


def _pid_dead(pid: int) -> bool:
    """True if ``pid`` is gone (no such process). A live (incl. zombie-but-not-reaped
    by someone else) pid is False; we wait/poll around this so a reaped child reads
    as dead promptly."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # exists, owned by another user (won't happen for our child)
    except OSError:
        return True
    return False


# --- command + manifest helpers (mirror how the app drops commands) ----------


def _drop_command(commands_dir: Path, payload: dict) -> Path:
    commands_dir.mkdir(parents=True, exist_ok=True)
    cmd_id = payload.get("cmd_id") or str(uuid.uuid4())
    payload.setdefault("cmd_id", cmd_id)
    final = commands_dir / f"{cmd_id}.json"
    tmp = commands_dir / f".{cmd_id}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


def _cancel_cmd(book_id: str) -> dict:
    """A cancel command exactly as the app drops it (D13 §1): no source_rev /
    confirm_token — it targets the book by id."""
    return {
        "cmd_id": str(uuid.uuid4()),
        "action": "cancel",
        "book_id": book_id,
        "idempotency_key": f"cancel:{book_id}:{uuid.uuid4().hex[:8]}",
        "ts": time.time(),
    }


def _confirm_build_cmd(manifest: dict) -> dict:
    bid = manifest["book_id"]
    rev = manifest["source_rev"]
    return {
        "cmd_id": str(uuid.uuid4()),
        "action": "confirm-build",
        "book_id": bid,
        "source_rev": rev,
        "confirm_token": manifest["confirm_token"],
        "idempotency_key": f"{bid}:{rev[:16]}:{uuid.uuid4().hex[:8]}",
        "params": dict(manifest.get("params", {})),
        "ts": time.time(),
    }


def _manifest_for(config, state, suffix: str) -> dict | None:
    for p in config.books_dir().glob("*.json"):
        m = state.read_json(p)
        if str(m.get("src_dir", "")).endswith(suffix):
            return m
    return None


def _events_of(state, kind: str) -> list[dict]:
    return [e for e in state.read_events() if e.get("event") == kind]


def _cancel_cmds_on_disk(config) -> list[Path]:
    cmd_dir = config.commands_dir()
    out = []
    for p in cmd_dir.glob("*.json"):
        from agent import state as _state
        c = _state.read_json(p, default=None)
        if isinstance(c, dict) and c.get("action") == "cancel":
            out.append(p)
    return out


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not _has_tools():
        print("§cancel self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-cancel-"))
    support = root / "support"
    watch = root / "watch"
    support.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)
    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)
    os.environ["MP3TOM4B_COVER_WEB"] = "0"  # offline determinism (no web cover)

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import build_m4b, config, dispatcher, scan, state  # noqa: E402

    print(f"self-check tree: {root}\n  support: {support}\n  watch:   {watch}\n")

    # === Arm a LONG book so ffmpeg is unmistakably mid-encode on cancel ========
    # Several multi-minute silent chapters → ~hour of virtual audio. Silence
    # re-encodes ~10× faster than real time, but an hour still takes several real
    # seconds — far more than one poll tick — so the cancel reliably interrupts a
    # genuinely running encode (not a build that already finished).
    book_dir = watch / "Отмена - Длинная книга"
    for i in range(1, 5):
        _make_silence_mp3(
            book_dir / f"{i:02d} - Глава {i}.mp3", seconds=900.0,  # 15 min each
            tags={"title": f"Глава {i}", "album": "Длинная книга",
                  "album_artist": "Тест Отмены"},
        )
    scan.run_scan()
    man = _manifest_for(config, state, "Отмена - Длинная книга")
    assert man is not None, "scan did not arm the long book"
    book_id = man["book_id"]
    source_rev0 = man["source_rev"]
    confirm_token0 = man["confirm_token"]
    out_path = build_m4b.default_output_path(man)
    print(f"  armed book_id={book_id}  expected output={out_path.name}\n")

    # === Cancel a REAL in-flight build ========================================
    # Drop the cancel FIRST (so the first poll tick inside build() sees it), then
    # run the real engine in a worker thread. The main thread captures the live
    # ffmpeg child's pid while it briefly runs, so "no orphan" is asserted against
    # a process we KNOW was spawned.
    _drop_command(config.commands_dir(), _cancel_cmd(book_id))

    build_done = threading.Event()

    def _worker() -> None:
        # Re-read the freshly-armed manifest and drive the real transition path.
        m = state.read_json(config.books_dir() / f"{book_id}.json")
        cmd = _confirm_build_cmd(m)
        try:
            dispatcher._real_build(m, config.books_dir() / f"{book_id}.json", cmd)
        finally:
            build_done.set()

    captured_ffmpeg_pids: list[int] = []
    t = threading.Thread(target=_worker, daemon=True)
    t.start()

    # Capture the ffmpeg child while it runs (poll for up to ~5s; the cancel kills
    # it within a poll tick of the build entering ffmpeg, so the window is short).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not build_done.is_set():
        kids = _ffmpeg_children(os.getpid())
        if kids:
            captured_ffmpeg_pids = kids
            break
        time.sleep(0.02)

    # Let the worker finish unwinding the cancel.
    t.join(timeout=30.0)
    finished = not t.is_alive()
    check("cancel: the cancelled build returned promptly (worker thread joined)",
          finished, f"thread_alive={t.is_alive()}")

    # 1) cancel works fast → book is back at pending-confirm (NOT error).
    man_after = state.read_json(config.books_dir() / f"{book_id}.json")
    check("cancel: book is back at pending-confirm (cancel ≠ failure)",
          man_after.get("status") == "pending-confirm",
          f"status={man_after.get('status')!r} error={man_after.get('error')}")

    # 2) no orphaned ffmpeg — the captured child must be DEAD (wait briefly for reap).
    if captured_ffmpeg_pids:
        for _ in range(100):  # up to ~2s for the SIGTERM/SIGKILL + reap to land
            if all(_pid_dead(p) for p in captured_ffmpeg_pids):
                break
            time.sleep(0.02)
        all_dead = all(_pid_dead(p) for p in captured_ffmpeg_pids)
        check("cancel: NO orphaned ffmpeg — the spawned child is dead",
              all_dead, f"captured_pids={captured_ffmpeg_pids} "
                        f"alive={[p for p in captured_ffmpeg_pids if not _pid_dead(p)]}")
    else:
        # We could not observe the child (it died inside a single poll tick before
        # our 20ms sampler caught it). Fall back to a tree sweep: there must be NO
        # ffmpeg child of ours left at all.
        leftover = _ffmpeg_children(os.getpid())
        check("cancel: NO orphaned ffmpeg — no ffmpeg child of ours remains",
              leftover == [],
              f"(child not sampled live) leftover_ffmpeg_children={leftover}")

    # 3a) no half-written temp — the hidden .<name>.m4b.*.tmp must be swept.
    tmp_siblings = list(out_path.parent.glob(f".{out_path.name}.*"))
    check("cancel: no half-written temp left (.<name>.m4b.* swept)",
          tmp_siblings == [], f"leftover_temps={[p.name for p in tmp_siblings]}")

    # 3b) no PARTIAL final output — the .m4b must NOT exist after a cancel.
    check("cancel: NO partial .m4b published (final output absent)",
          not out_path.exists(),
          f"unexpected output exists={out_path.exists()} path={out_path}")

    # 4) journalled — a build_cancelled event for this book.
    cancelled_events = [e for e in _events_of(state, "build_cancelled")
                        if e.get("book_id") == book_id]
    check("cancel: build_cancelled event recorded",
          len(cancelled_events) >= 1, f"events={cancelled_events}")

    # 5) the cancel command was CONSUMED by the owner (no double processing).
    leftover_cancels = _cancel_cmds_on_disk(config)
    check("cancel: the cancel command was consumed (none left on disk)",
          leftover_cancels == [],
          f"leftover={[p.name for p in leftover_cancels]}")

    # token / rev preserved across the cancel (so a rebuild can use them).
    check("cancel: source_rev + confirm_token preserved (rebuildable)",
          man_after.get("source_rev") == source_rev0
          and man_after.get("confirm_token") == confirm_token0,
          f"rev={man_after.get('source_rev')==source_rev0} "
          f"token={man_after.get('confirm_token')==confirm_token0}")
    # the live build marker / planned result must be cleared (no stale half-state).
    check("cancel: build marker + planned result cleared on the way back to pending",
          man_after.get("build") is None and man_after.get("result") is None,
          f"build={man_after.get('build')} result={man_after.get('result')}")

    # === IDEMPOTENCY: a SECOND cancel for the now-pending book is moot =========
    # The book is pending-confirm again (not converting), so a fresh cancel must
    # be deleted as moot and must NOT corrupt the status.
    _drop_command(config.commands_dir(), _cancel_cmd(book_id))
    dispatcher.drain_commands()
    man_after2 = state.read_json(config.books_dir() / f"{book_id}.json")
    moot_events = [e for e in _events_of(state, "cancel_moot")
                  if e.get("book_id") == book_id]
    check("idempotent: a 2nd cancel for the pending book is moot (cancel_moot)",
          len(moot_events) >= 1, f"cancel_moot events={moot_events}")
    check("idempotent: the 2nd cancel command was deleted",
          _cancel_cmds_on_disk(config) == [],
          f"leftover={[p.name for p in _cancel_cmds_on_disk(config)]}")
    check("idempotent: status stays pending-confirm (2nd cancel did not corrupt it)",
          man_after2.get("status") == "pending-confirm",
          f"status={man_after2.get('status')!r}")

    # === REBUILD after cancel: confirm-build now must reach done ===============
    # The token stayed valid → the same confirm flow drives a fresh build to done.
    man_fresh = state.read_json(config.books_dir() / f"{book_id}.json")
    _drop_command(config.commands_dir(), _confirm_build_cmd(man_fresh))
    dispatcher.drain_commands()
    man_rebuilt = state.read_json(config.books_dir() / f"{book_id}.json")
    check("rebuild: after cancel the book confirm-builds again to done",
          man_rebuilt.get("status") == "done",
          f"status={man_rebuilt.get('status')!r} error={man_rebuilt.get('error')}")
    res = man_rebuilt.get("result") if isinstance(man_rebuilt.get("result"), dict) else {}
    rebuilt_out = res.get("output") or res.get("output_path")
    check("rebuild: a valid .m4b exists on disk after the rebuild",
          isinstance(rebuilt_out, str) and rebuilt_out.endswith(".m4b")
          and Path(rebuilt_out).is_file() and Path(rebuilt_out).stat().st_size > 0,
          f"result={res}")

    # === MOOT for a DONE book: a late cancel must leave it done ================
    # Edge (D13 §5): the cancel arrived after the build finished → the book is
    # already done → cancel is moot and must NOT damage the done status/output.
    _drop_command(config.commands_dir(), _cancel_cmd(book_id))
    dispatcher.drain_commands()
    man_done_after = state.read_json(config.books_dir() / f"{book_id}.json")
    check("moot-done: a cancel for a DONE book leaves it done (too-late is honest)",
          man_done_after.get("status") == "done",
          f"status={man_done_after.get('status')!r}")
    check("moot-done: the cancel-for-done command was deleted",
          _cancel_cmds_on_disk(config) == [],
          f"leftover={[p.name for p in _cancel_cmds_on_disk(config)]}")

    return _finish(root)


def _finish(root: Path) -> int:
    # Flat verification: this suite runs ONLY its own checks. Cross-suite
    # regression is orchestrated once by ``agent.selfcheck_all`` (no nested
    # re-runs here — that is what made a single pass take ~30 min).
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    failed = [name for name, ok, _ in _RESULTS if not ok]
    print(f"\n§cancel self-check: {passed}/{total} checks passed")
    if failed:
        print("  FAILED checks: " + "; ".join(failed))
    print(f"(temp tree left at {root} for inspection; safe to delete)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
