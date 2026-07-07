"""§status self-check — the data contract the STATUS home screen (spec §5) reads.

Run it standalone:

    python3 -m agent.selfcheck_status

The STATUS screen is a pure READER of the agent's authoritative ``state/state.json``
showcase. Beyond the fields the queue already consumes (``books[]`` / ``batch``),
Status projects three NEW agent-owned fields the stat cards (spec §5) depend on:

  · ``totals.built`` — number of books at status ``done`` (the «Собрано» card);
  · ``totals.today`` — of those, the ones whose ``result.built_at`` is the current
    local day (the «За сегодня» card; the date is taken at RUNTIME, never hard-coded);
  · ``engine``        — ffmpeg's version string (the «ffmpeg» card).

The agent is the SINGLE writer of state, so this check proves the AGENT emits those
fields correctly AND that adding them did not drop any field the showcase already
carried (``books`` / ``batch`` / ``pending_groups`` / ``grouping_processed`` /
``totals.books``). The pixel drawing of the Status screen is verified separately (in
a real browser, by Yurka — intentionally NOT asserted here).

It drives the REAL path on a throwaway tree (``MP3TOM4B_SUPPORT_DIR`` /
``MP3TOM4B_WATCH_DIR`` redirect everything; the user's real Application Support is
never touched):

  built/today  a real scan → real build (dispatcher → ffmpeg) of a book ⇒ the
               showcase ``totals.built`` AND ``totals.today`` both increase by one
               (the just-built book is done AND built right now), and the built
               book's manifest carries a ``result.built_at``.
  engine       the showcase ``engine`` is a non-empty version string (ffmpeg is on
               PATH — required, like the sibling checks) AND matches the value the
               agent's :func:`scan.engine_version` resolves.
  preserve     a second unconfirmed book + a REAL loose-mp3 pending group (projected
               from files in the watch root, the way the agent actually builds it)
               survive the build's showcase refresh: ``books`` still lists both,
               ``pending_groups`` still carries the group through the drain's closing
               ``run_scan`` (which honestly re-derives it from disk), ``batch`` stays a
               well-formed block, and ``totals.books`` equals the row count.
  today-unit   a UNIT assertion on :func:`scan._is_built_today` (now=True, 2-days-ago
               =False, None=False) so the local-midnight reset is proven directly, not
               only inferred from the one freshly-built book.

It runs ONLY its own checks (cross-suite regression is orchestrated once by
``agent.selfcheck_all`` — there is no nested re-run here) and returns 0 ⇔ every
check here passed (passed == total — NOT a false-green exit). Requires ffmpeg +
ffprobe on PATH; if either is missing it says so and exits non-zero.

This file lives in the package so it imports the real modules under test; it writes
only inside its temp tree (plus each book's ``.m4b`` next to its source folder,
which is inside the temp watch dir).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
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


# --- ffmpeg helpers (lifted from the queue/M1 self-checks; same proven recipes) --


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _has_tools() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _make_mp3(path: Path, *, seconds: float, samplerate: int = 44100,
              channels: int = 2, freq: int = 440, tags: dict | None = None) -> None:
    """Write a real sine-tone mp3 with optional ID3 tags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
        "-ar", str(samplerate), "-ac", str(channels),
    ]
    for k, v in (tags or {}).items():
        argv += ["-metadata", f"{k}={v}"]
    argv.append(str(path))
    subprocess.run(argv, check=True, capture_output=True)


# --- command + manifest helpers (mirror how the app drops a confirm-build) ----


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
        "idempotency_key": f"{bid}:{rev[:16]}",
        "params": dict(manifest.get("params", {})),
        "ts": time.time(),
    }


def _manifest_for(config, state, suffix: str) -> dict | None:
    """The manifest whose src_dir ends with `suffix` (re-read fresh each call)."""
    for p in config.books_dir().glob("*.json"):
        m = state.read_json(p)
        if str(m.get("src_dir", "")).endswith(suffix):
            return m
    return None


def _row_for(books: list[dict], book_id: str) -> dict | None:
    for b in books:
        if b.get("book_id") == book_id:
            return b
    return None


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not _has_tools():
        print("§status self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-status-"))
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

    from agent import config, dispatcher, scan, state  # noqa: E402

    print(f"self-check tree: {root}\n  support: {support}\n  watch:   {watch}\n")

    # === UNIT: the local-midnight «За сегодня» predicate ====================
    now = time.time()
    check("today-unit: _is_built_today(now)==True",
          scan._is_built_today(now) is True)
    check("today-unit: _is_built_today(2 days ago)==False",
          scan._is_built_today(now - 2 * 86400) is False)
    check("today-unit: _is_built_today(None)==False",
          scan._is_built_today(None) is False)

    # === engine: the agent resolves a non-empty ffmpeg version ==============
    eng = scan.engine_version()
    check("engine: scan.engine_version() is a non-empty string",
          isinstance(eng, str) and len(eng) > 0, f"engine={eng!r}")

    # === Build the books on REAL data =======================================
    # BUILT book — two Cyrillic chapters → real scan → real build → done today.
    built_dir = watch / "Толстой - Война и мир"
    _make_mp3(built_dir / "01 - Том первый.mp3", seconds=1.0,
              tags={"title": "Том первый", "album": "Война и мир",
                    "album_artist": "Лев Толстой"})
    _make_mp3(built_dir / "02 - Том второй.mp3", seconds=1.5,
              tags={"title": "Том второй", "album": "Война и мир",
                    "album_artist": "Лев Толстой"})

    # PENDING book — left unconfirmed so it stays pending-confirm (preserve check).
    pending_dir = watch / "Гоголь - Мёртвые души"
    _make_mp3(pending_dir / "01 - Глава I.mp3", seconds=1.0,
              tags={"title": "Глава I", "album": "Мёртвые души",
                    "album_artist": "Николай Гоголь"})

    # REAL loose mp3s in the watch ROOT (not a subfolder) → the agent projects a
    # genuine pending GROUP (D1) the scan rebuilds from disk on every pass. This is
    # the honest source for the preserve/refresh checks below: a forged group written
    # straight into state.json would be wiped by the drain's closing run_scan (which
    # re-derives pending_groups from the watch root); these on-disk files make the
    # group survive the WHOLE build cycle truthfully. Loose root files contribute a
    # pending_group, NOT a book row (see §grouping), so totals.books stays 2.
    _make_mp3(watch / "loose-01.mp3", seconds=1.0,
              tags={"title": "Отрывок первый", "album": "Разрозненное"})
    _make_mp3(watch / "loose-02.mp3", seconds=1.0,
              tags={"title": "Отрывок второй", "album": "Разрозненное"})

    # First scan: arms both books + projects the BASELINE totals (built=0, today=0).
    base = scan.run_scan()
    check("baseline: fresh scan projects totals.built==0 / today==0",
          isinstance(base.get("totals"), dict)
          and base["totals"].get("built") == 0
          and base["totals"].get("today") == 0,
          f"totals={base.get('totals')}")
    check("baseline: showcase already carries the engine string",
          isinstance(base.get("engine"), str) and base["engine"] == eng,
          f"engine={base.get('engine')!r}")
    base_books = base.get("totals", {}).get("books")
    check("baseline: totals.books equals the row count (2 armed books)",
          base_books == len(base.get("books", [])) == 2,
          f"totals.books={base_books} rows={len(base.get('books', []))}")

    man_built = _manifest_for(config, state, "Толстой - Война и мир")
    man_pending = _manifest_for(config, state, "Гоголь - Мёртвые души")
    assert man_built is not None and man_pending is not None, "scan did not arm both books"

    # === The REAL pending GROUP the preserve check protects ==================
    # The baseline scan already projected a genuine group from the loose root mp3s
    # (the agent owns pending_groups and rebuilds them from disk every pass). Capture
    # its real group_id so the preserve/refresh checks assert THAT group survives the
    # whole build — not a hand-forged dict the closing run_scan would honestly drop.
    base_groups = base.get("pending_groups")
    assert (
        isinstance(base_groups, list)
        and len(base_groups) == 1
        and isinstance(base_groups[0], dict)
        and base_groups[0].get("group_id")
    ), f"baseline scan did not project the loose-mp3 group: {base_groups}"
    grp_id = base_groups[0]["group_id"]

    # Seed the grouping ledger with an UNRELATED resolved key (a real key is
    # ``<group_id>:<rev[:16]>``, so this synthetic value can never match — and thus
    # never suppresses our live loose group). It proves the ledger is carried forward
    # across the build refresh alongside the live group.
    cur = state.read_state()
    cur["grouping_processed"] = ["already:resolved01"]
    state.write_state(cur)

    # === Build ONLY the built book (the dispatcher's real I2 gate → ffmpeg) ===
    _drop_command(config.commands_dir(), _confirm_build_cmd(man_built))
    dispatcher.drain_commands()
    man_built = _manifest_for(config, state, "Толстой - Война и мир")

    check("built: the book reached status==done",
          man_built.get("status") == "done",
          f"status={man_built.get('status')!r} error={man_built.get('error')}")
    res = man_built.get("result") if isinstance(man_built.get("result"), dict) else {}
    built_at = res.get("built_at")
    check("built: manifest result.built_at is a real epoch (the «За сегодня» source)",
          isinstance(built_at, (int, float)) and built_at > 0
          and scan._is_built_today(built_at),
          f"result={res}")

    # === Final showcase projection (the dispatcher already refreshed it) =====
    showcase = state.read_json(config.state_file())
    totals = showcase.get("totals")
    books = showcase.get("books", [])
    built_id = man_built["book_id"]
    pending_id = man_pending["book_id"]

    by_status: dict[str, list[str]] = {}
    for b in books:
        by_status.setdefault(b.get("status", "?"), []).append(b.get("book_id"))
    print(f"\n  state.json projects {len(books)} books: "
          + ", ".join(f"{k}×{len(v)}" for k, v in sorted(by_status.items()))
          + f"; totals={totals}; engine={showcase.get('engine')!r}\n")

    # --- the NEW Status fields ---------------------------------------------
    check("totals: built grew to 1 after the real build (the «Собрано» card)",
          isinstance(totals, dict) and totals.get("built") == 1,
          f"totals.built={totals.get('built') if isinstance(totals, dict) else totals}")
    check("totals: today grew to 1 (built right now → the «За сегодня» card)",
          isinstance(totals, dict) and totals.get("today") == 1,
          f"totals.today={totals.get('today') if isinstance(totals, dict) else totals}")
    check("engine: showcase engine string is non-empty + matches engine_version()",
          isinstance(showcase.get("engine"), str)
          and showcase["engine"] == eng and len(showcase["engine"]) > 0,
          f"engine={showcase.get('engine')!r}")

    # --- agent.active (the hero/footer "Активен" pill) ----------------------
    agent = showcase.get("agent")
    check("agent: showcase carries agent.active==True (the «Активен» pill source)",
          isinstance(agent, dict) and agent.get("active") is True
          and isinstance(agent.get("watch_dir"), str) and agent["watch_dir"],
          f"agent={agent}")

    # --- nothing the showcase already carried got dropped -------------------
    check("preserve: built book is in the done partition",
          built_id in by_status.get("done", []),
          f"done={by_status.get('done')}")
    check("preserve: the unconfirmed book is still listed (pending-confirm)",
          pending_id in by_status.get("pending-confirm", []),
          f"pending={by_status.get('pending-confirm')}")
    check("preserve: totals.books equals the row count (2 books)",
          isinstance(totals, dict) and totals.get("books") == len(books) == 2,
          f"totals.books={totals.get('books') if isinstance(totals, dict) else totals} rows={len(books)}")

    groups = showcase.get("pending_groups")
    check("preserve: the real loose-mp3 pending group survived the build refresh",
          isinstance(groups, list) and len(groups) == 1
          and groups[0].get("group_id") == grp_id,
          f"pending_groups={groups}")
    led = showcase.get("grouping_processed")
    check("preserve: grouping_processed ledger survived the build refresh",
          isinstance(led, list) and "already:resolved01" in led,
          f"grouping_processed={led}")

    batch = showcase.get("batch")
    check("preserve: batch stays a well-formed {active,total,done} block",
          isinstance(batch, dict)
          and isinstance(batch.get("active"), bool)
          and isinstance(batch.get("total"), int)
          and isinstance(batch.get("done"), int),
          f"batch={batch}")

    # --- the cheap showcase row still carries the qrow/recent fields --------
    built_row = _row_for(books, built_id)
    check("row: built book row carries author + chapters + total_duration_ms",
          built_row is not None and built_row.get("author") == "Лев Толстой"
          and built_row.get("chapters") == 2
          and isinstance(built_row.get("total_duration_ms"), int)
          and built_row["total_duration_ms"] > 0,
          f"row={built_row}")

    # --- refresh_showcase (the transition path) must keep the new fields too -
    # The dispatcher calls refresh_showcase() on every build transition; prove it
    # re-projects built/today/engine identically (not only the closing run_scan).
    refreshed = scan.refresh_showcase()
    rt = refreshed.get("totals")
    rgroups = refreshed.get("pending_groups")
    check("refresh: refresh_showcase re-projects built/today/engine identically",
          isinstance(rt, dict) and rt.get("built") == 1 and rt.get("today") == 1
          and refreshed.get("engine") == eng
          and isinstance(rgroups, list) and len(rgroups) == 1
          and rgroups[0].get("group_id") == grp_id,
          f"totals={rt} engine={refreshed.get('engine')!r} "
          f"groups={len(rgroups or [])}")

    # --- summary ------------------------------------------------------------
    return _finish(root)


def _finish(root: Path) -> int:
    # Flat verification: this suite runs ONLY its own checks. Cross-suite
    # regression is orchestrated once by ``agent.selfcheck_all`` (no nested
    # re-runs here — that is what made a single pass take ~30 min).
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    failed = [name for name, ok, _ in _RESULTS if not ok]
    print(f"\n§status self-check: {passed}/{total} checks passed")
    if failed:
        # List every failed check by name so a non-green run can never read as a
        # silent pass (the false-green the sibling checks were hardened against).
        print("  FAILED checks: " + "; ".join(failed))
    print(f"(temp tree left at {root} for inspection; safe to delete)")

    # Exit honestly: green ONLY when EVERY local check passed (passed == total).
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
