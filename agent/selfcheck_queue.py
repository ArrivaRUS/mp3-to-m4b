"""§queue self-check — the data contract the QUEUE screen (spec §7) reads.

Run it standalone:

    python3 -m agent.selfcheck_queue

The QUEUE is a pure READER of the agent's authoritative files: it projects
``state/state.json`` (the showcase ``books[]`` partitioned BY STATUS + the
``batch`` block) into the four sections spec §7 names — ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ
(pending-confirm) · В РАБОТЕ (converting) · ГОТОВО (done) · ОШИБКА (error) — and
reads each book's ``queue/books/<id>.json`` manifest for the qrow fields (cover
preview, finished ``.m4b`` path, error reason). This check proves the AGENT emits
that data in the shape the Swift queue depends on; the pixel drawing is verified
separately (in a real browser, by Yurka — it is intentionally NOT asserted here).

It drives the REAL path on a throwaway tree (``MP3TOM4B_SUPPORT_DIR`` /
``MP3TOM4B_WATCH_DIR`` redirect everything; the user's real Application Support is
never touched):

  done       a real scan → real build (dispatcher → ffmpeg) of a book WITH an
             embedded cover ⇒ manifest ``status==done`` carrying
             ``result.output_path`` (the "Открыть" target) + a non-empty
             ``cover_options`` / ``cover_preview`` (the qrow cover).
  pending    a second scanned book left unconfirmed ⇒ ``status==pending-confirm``.
  error      a corrupt-mp3 book built through the dispatcher ⇒ ``status==error``
             with an ``error.reason`` (the qrow sub-line maps it).
  converting a forged ``status==converting`` manifest ⇒ projects into В РАБОТЕ.

then asserts the FINAL ``state.json``:
  · ``books[]`` partitions correctly by status (each id in exactly its section);
  · every showcase row carries ``author`` / ``chapters`` / ``total_duration_ms``
    (the cheap qrow sub-line fields — the queue avoids loading a manifest for them);
  · ``batch`` is a well-formed ``{active,total,done}`` block (the batch-chip source).

It runs ONLY its own checks (cross-suite regression is orchestrated once by
``agent.selfcheck_all`` — there is no nested re-run here) and returns 0 ⇔ every
check here passed. Requires ffmpeg + ffprobe on PATH; if either is missing it says
so and exits non-zero.

This file lives in the package so it imports the real modules under test; it
writes only inside its temp tree (plus each book's ``.m4b`` next to its source
folder, which is inside the temp watch dir).
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


# --- ffmpeg helpers (lifted from the M1 self-check; same proven recipes) -----


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe() -> str:
    return shutil.which("ffprobe") or "ffprobe"


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


def _make_mp3_with_cover(path: Path, *, seconds: float = 1.0,
                         tags: dict | None = None) -> None:
    """Write a real mp3 carrying an embedded attached-picture cover (mjpeg)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    art = path.parent / f".art-{path.stem}.jpg"
    subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=teal:s=400x400:d=1",
         "-frames:v", "1", str(art)],
        check=True, capture_output=True,
    )
    argv = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-i", str(art),
        "-map", "0:a", "-map", "1:v",
        "-c:a", "libmp3lame", "-c:v", "copy",
        "-id3v2_version", "3",
        "-metadata:s:v", "title=Album cover",
        "-disposition:v", "attached_pic",
    ]
    for k, v in (tags or {}).items():
        argv += ["-metadata", f"{k}={v}"]
    argv.append(str(path))
    subprocess.run(argv, check=True, capture_output=True)
    try:
        art.unlink()
    except OSError:
        pass


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
        # D17: the app echoes the build_token it saw, proving the command was
        # minted from a COMPLETE manifest and not from the early-nudge skeleton.
        "build_token": manifest.get("build_token"),
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
        print("§queue self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-queue-"))
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

    # === Build the four queue states on REAL data ===========================
    # DONE book — embedded cover, two Cyrillic chapters → real build → done.
    done_dir = watch / "Толстой - Анна Каренина"
    _make_mp3_with_cover(done_dir / "01 - Часть первая.mp3", seconds=1.0,
                         tags={"title": "Часть первая", "album": "Анна Каренина",
                               "album_artist": "Лев Толстой"})
    _make_mp3(done_dir / "02 - Часть вторая.mp3", seconds=2.0,
              tags={"title": "Часть вторая", "album": "Анна Каренина",
                    "album_artist": "Лев Толстой"})

    # PENDING book — left unconfirmed so it stays pending-confirm.
    pending_dir = watch / "Гоголь - Мёртвые души"
    _make_mp3(pending_dir / "01 - Глава I.mp3", seconds=1.0,
              tags={"title": "Глава I", "album": "Мёртвые души",
                    "album_artist": "Николай Гоголь"})
    _make_mp3(pending_dir / "02 - Глава II.mp3", seconds=1.0,
              tags={"title": "Глава II", "album": "Мёртвые души",
                    "album_artist": "Николай Гоголь"})

    scan.run_scan()

    man_done = _manifest_for(config, state, "Толстой - Анна Каренина")
    man_pending = _manifest_for(config, state, "Гоголь - Мёртвые души")
    assert man_done is not None and man_pending is not None, "scan did not arm both books"

    # Build ONLY the done book (the dispatcher's real I2 gate → ffmpeg).
    _drop_command(config.commands_dir(), _confirm_build_cmd(man_done))
    dispatcher.drain_commands()
    man_done = _manifest_for(config, state, "Толстой - Анна Каренина")

    check("done: built book reached status==done",
          man_done.get("status") == "done",
          f"status={man_done.get('status')!r} error={man_done.get('error')}")

    # result.output_path — the "Открыть" target the done qrow reveals in Finder.
    res = man_done.get("result") if isinstance(man_done.get("result"), dict) else {}
    out_path = res.get("output_path") or res.get("output")
    check("done: result.output_path is a real .m4b on disk (qrow «Открыть» target)",
          isinstance(out_path, str) and out_path.endswith(".m4b")
          and Path(out_path).is_file(),
          f"result={res}")

    # cover_options / cover_preview — the qrow's 38px cover source (PRD G4: ≥1).
    cov_opts = man_done.get("cover_options")
    check("done: manifest carries cover_options (≥1) for the qrow cover (G4)",
          isinstance(cov_opts, list) and len(cov_opts) >= 1,
          f"cover_options={len(cov_opts) if isinstance(cov_opts, list) else cov_opts}")
    check("done: cover_state==embedded + cover_preview path present",
          man_done.get("cover_state") == "embedded"
          and isinstance(man_done.get("cover_preview"), str)
          and Path(man_done["cover_preview"]).is_file(),
          f"cover_state={man_done.get('cover_state')} preview={man_done.get('cover_preview')}")

    check("pending: the unconfirmed book stayed pending-confirm",
          man_pending.get("status") == "pending-confirm",
          f"status={man_pending.get('status')!r}")

    # === ERROR book — corrupt mp3 → real ffmpeg failure → status error ======
    err_dir = watch / "Битая - Книга"
    err_dir.mkdir(parents=True, exist_ok=True)
    (err_dir / "01 - broken.mp3").write_bytes(b"not an mp3 \x00\x01\x02 garbage")
    forged_err = {
        "book_id": "queue-error-book",
        "src_dir": str(err_dir),
        "status": "pending-confirm",
        "source_rev": "rev-err",
        "confirm_token": "tok-err",
        # D17: complete-looking manifest (the fixture is about a corrupt SOURCE).
        "phase": scan.MANIFEST_PHASE_READY,
        "build_token": "btok-err",
        "title": "Книга",
        "author": "Битая",
        "chapters": [
            {"index": 1, "file": "01 - broken.mp3", "name": "Битая глава",
             "duration_ms": 1000},
        ],
        "total_duration_ms": 1000,
        "cover_state": "none",
        "cover_preview": None,
        "params": dict(scan.DEFAULT_PARAMS),
        "processed_keys": [],
        "ts": time.time(),
    }
    err_path = config.books_dir() / "queue-error-book.json"
    state.write_json_atomic(err_path, forged_err)
    _drop_command(config.commands_dir(), _confirm_build_cmd(forged_err))
    dispatcher.drain_commands()
    man_err = state.read_json(err_path)

    check("error: failed build → status==error",
          man_err.get("status") == "error", f"status={man_err.get('status')!r}")
    check("error: error.reason present (the qrow sub-line maps it)",
          isinstance(man_err.get("error"), dict)
          and bool(man_err["error"].get("reason")),
          f"error={man_err.get('error')}")

    # === CONVERTING book — the REAL in-flight shape projects into В РАБОТЕ =====
    # The real engine (dispatcher._real_build) flips a book to ``converting`` with a
    # LIVE build pid and then calls ``scan.refresh_showcase()`` so the В РАБОТЕ
    # section sees it DURING the multi-minute build (not only on the next scan after
    # the drain). We reproduce that exact persisted shape:
    #   · a real source dir WITH mp3s (so the source is "alive" — an empty dir would
    #     be dropped as stale, the trap the earlier version of this check fell into);
    #   · a live build pid = os.getpid() (this very process), so the manifest mirrors
    #     a build that is genuinely in flight — recover_interrupted MUST leave it.
    # Then we drive the real projection via refresh_showcase() (the function the
    # dispatcher calls on the converting transition), so this asserts the production
    # path Bug A fixed, not a recover-defeated forgery.
    conv_dir = watch / "Достоевский - Идиот"
    _make_mp3(conv_dir / "01 - Часть первая.mp3", seconds=1.0,
              tags={"title": "Часть первая", "album": "Идиот",
                    "album_artist": "Фёдор Достоевский"})
    _make_mp3(conv_dir / "02 - Часть вторая.mp3", seconds=1.0,
              tags={"title": "Часть вторая", "album": "Идиот",
                    "album_artist": "Фёдор Достоевский"})
    forged_conv = {
        "book_id": "queue-converting-book",
        "src_dir": str(conv_dir),
        "status": "converting",
        "source_rev": "rev-conv",
        "confirm_token": "tok-conv",
        "title": "Идиот",
        "author": "Фёдор Достоевский",
        "chapters": [
            {"index": 1, "file": "01 - Часть первая.mp3", "name": "Часть первая",
             "duration_ms": 5000},
            {"index": 2, "file": "02 - Часть вторая.mp3", "name": "Часть вторая",
             "duration_ms": 5000},
        ],
        "total_duration_ms": 10000,
        "cover_state": "none",
        "cover_preview": None,
        "progress": 0.0,
        # Live pid → a genuine in-flight build; recover_interrupted leaves it alone.
        "build": {"pid": os.getpid(), "started_at": time.time()},
        "params": dict(scan.DEFAULT_PARAMS),
        "processed_keys": [],
        "ts": time.time(),
    }
    conv_path = config.books_dir() / "queue-converting-book.json"
    state.write_json_atomic(conv_path, forged_conv)

    # recover_interrupted must NOT touch a converting book whose build pid is alive
    # (this is what lets the real engine hold ``converting`` across a long encode).
    recovered = dispatcher.recover_interrupted()
    conv_after_recover = state.read_json(conv_path)
    check("converting: a LIVE-pid converting manifest survives recover_interrupted",
          recovered == 0 and conv_after_recover.get("status") == "converting",
          f"recovered={recovered} status={conv_after_recover.get('status')!r}")

    # === Final projection: refresh the showcase (the real transition path) ====
    # Use refresh_showcase() — the exact function the dispatcher invokes on every
    # build transition — so we assert the projection that makes В РАБОТЕ live.
    scan.refresh_showcase()
    showcase = state.read_json(config.state_file())
    books = showcase.get("books", [])
    by_status: dict[str, list[str]] = {}
    for b in books:
        by_status.setdefault(b.get("status", "?"), []).append(b.get("book_id"))

    print(f"\n  state.json projects {len(books)} books: "
          + ", ".join(f"{k}×{len(v)}" for k, v in sorted(by_status.items())) + "\n")

    done_id = man_done["book_id"]
    pending_id = man_pending["book_id"]

    check("project: all four statuses present in the showcase",
          {"done", "pending-confirm", "error", "converting"} <= set(by_status),
          f"statuses={sorted(by_status)}")
    check("project: the built book is in the GOTOVO (done) partition",
          done_id in by_status.get("done", []),
          f"done={by_status.get('done')}")
    check("project: the unconfirmed book is in the OZHIDAET (pending-confirm) partition",
          pending_id in by_status.get("pending-confirm", []),
          f"pending={by_status.get('pending-confirm')}")
    check("project: the failed book is in the OSHIBKA (error) partition",
          "queue-error-book" in by_status.get("error", []),
          f"error={by_status.get('error')}")
    check("project: the in-flight book is in the V RABOTE (converting) partition",
          "queue-converting-book" in by_status.get("converting", []),
          f"converting={by_status.get('converting')}")
    check("project: each book sits in EXACTLY one partition (no dup ids)",
          len(books) == len({b.get("book_id") for b in books}),
          f"rows={len(books)} unique={len({b.get('book_id') for b in books})}")

    # qrow sub-line fields carried by the cheap showcase row (no manifest load).
    done_row = _row_for(books, done_id)
    conv_row = _row_for(books, "queue-converting-book")
    check("qrow: showcase row carries author (sub-line «Автор · …»)",
          done_row is not None and done_row.get("author") == "Лев Толстой",
          f"author={done_row.get('author') if done_row else None}")
    check("qrow: showcase row carries chapters count + total_duration_ms",
          done_row is not None
          and isinstance(done_row.get("chapters"), int) and done_row["chapters"] == 2
          and isinstance(done_row.get("total_duration_ms"), int)
          and done_row["total_duration_ms"] > 0,
          f"chapters={done_row.get('chapters')} dur_ms={done_row.get('total_duration_ms')}")
    check("qrow: forged converting row also carries author + chapters",
          conv_row is not None and conv_row.get("author") == "Фёдор Достоевский"
          and conv_row.get("chapters") == 2,
          f"row={conv_row}")

    # batch block — the batch-chip source (active/total/done). The queue only SHOWS
    # the chip when active==True; the projection must still emit a well-formed block.
    batch = showcase.get("batch")
    check("batch: state.json carries a well-formed {active,total,done} block",
          isinstance(batch, dict)
          and isinstance(batch.get("active"), bool)
          and isinstance(batch.get("total"), int)
          and isinstance(batch.get("done"), int),
          f"batch={batch}")

    # --- summary ------------------------------------------------------------
    return _finish(root)


def _finish(root: Path) -> int:
    # Flat verification: this suite runs ONLY its own checks. Cross-suite
    # regression is orchestrated once by ``agent.selfcheck_all`` (no nested
    # re-runs here — that is what made a single pass take ~30 min).
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    failed = [name for name, ok, _ in _RESULTS if not ok]
    print(f"\n§queue self-check: {passed}/{total} checks passed")
    if failed:
        # List every failed check by name so a non-green run can never read as a
        # silent pass (the false-green this check was hardened against).
        print("  FAILED checks: " + "; ".join(failed))
    print(f"(temp tree left at {root} for inspection; safe to delete)")

    # Exit honestly: green ONLY when EVERY local check passed (passed == total).
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
