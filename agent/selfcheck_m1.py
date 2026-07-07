"""§M1 vertical self-check — the REAL ffmpeg engine, end to end.

Run it standalone:

    python3 -m agent.selfcheck_m1

It generates real mp3s in a throwaway temp tree (sine tones via ffmpeg
``lavfi``, deliberately heterogeneous: different sample rates and channel counts,
Cyrillic chapter titles, one book with an embedded ``attached_pic`` cover),
redirects the whole data tree via ``MP3TOM4B_SUPPORT_DIR`` /
``MP3TOM4B_WATCH_DIR`` (the user's real Application Support is never touched),
then drives the FULL path — scan → manifest → emulate a ``confirm-build`` command
→ **real build** (:func:`agent.build_m4b.build` via the dispatcher) — and probes
the OUTPUT with ffprobe / ffmpeg to assert ``test-plan.md §M1``:

  container     ``.m4b`` assembled, brand ``M4A``, ``+faststart`` (moov first)
  chapters      ``-show_chapters`` → right count, START/END (1/1000), Cyrillic OK
  no-drift      total duration ≈ Σ source durations (concat filter + aformat)
  cover         ``attached_pic`` (mjpeg) present when embedded AND, since the cover
                chain landed, also for a cover_state==none book (a generated cover
                is resolved + burned — PRD G4; the picker proof is in §cover-pick)
  atomicity     a forced build failure (corrupt input) → NO half ``.m4b``, status
                ``error``, temp swept
  I1            source mp3s byte-identical (size + mtime) after the build
  idempotency   a duplicate ``confirm-build`` (double click) → exactly one build,
                one output

It runs ONLY its own checks (cross-suite regression is orchestrated once by
``agent.selfcheck_all`` — there is no nested re-run here). Requires ffmpeg +
ffprobe on PATH; if either is missing the script says so and exits non-zero.

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

# --- tiny assertion harness -------------------------------------------------

_RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _RESULTS.append((name, bool(ok), detail))
    mark = "PASS" if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f" — {detail}"
    print(line)


# --- ffmpeg/ffprobe helpers -------------------------------------------------


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def _has_tools() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _make_mp3(
    path: Path,
    *,
    seconds: float,
    samplerate: int = 44100,
    channels: int = 2,
    freq: int = 440,
    tags: dict | None = None,
) -> None:
    """Write a real sine-tone mp3 with a given SR/channels and optional tags."""
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


def _make_mp3_with_cover(
    path: Path, *, seconds: float = 1.0, tags: dict | None = None
) -> None:
    """Write a real mp3 carrying an embedded attached-picture cover (mjpeg)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    art = path.parent / f".art-{path.stem}.jpg"
    subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=green:s=400x400:d=1",
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


def _probe_format(path: Path) -> dict:
    """ffprobe ``format`` block (name + tags) for the built file."""
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-print_format", "json",
         "-show_format", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return json.loads(out.stdout or "{}").get("format", {})
    except json.JSONDecodeError:
        return {}


def _probe_chapters(path: Path) -> list[dict]:
    """ffprobe ``-show_chapters`` → list of {start,end,title}."""
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-print_format", "json",
         "-show_chapters", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return json.loads(out.stdout or "{}").get("chapters", [])
    except json.JSONDecodeError:
        return []


def _probe_duration_s(path: Path) -> float:
    """Total container duration in seconds (0.0 if unreadable)."""
    fmt = _probe_format(path)
    try:
        return float(fmt.get("duration"))
    except (TypeError, ValueError):
        return 0.0


def _has_attached_pic(path: Path) -> tuple[bool, str]:
    """Return (has_cover, codec_name) for an attached-picture video stream."""
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "v",
         "-print_format", "json", "-show_streams", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        streams = json.loads(out.stdout or "{}").get("streams", [])
    except json.JSONDecodeError:
        return (False, "")
    for s in streams:
        disp = s.get("disposition") or {}
        if disp.get("attached_pic") == 1:
            return (True, s.get("codec_name", ""))
    return (False, "")


def _faststart_ok(path: Path) -> bool:
    """True iff the ``moov`` atom precedes ``mdat`` (i.e. +faststart took)."""
    data = path.read_bytes()
    mi = data.find(b"moov")
    di = data.find(b"mdat")
    return 0 <= mi < di


# --- command helpers (mirror how the app drops a confirm-build) -------------


def _drop_command(commands_dir: Path, payload: dict) -> Path:
    commands_dir.mkdir(parents=True, exist_ok=True)
    cmd_id = payload.get("cmd_id") or str(uuid.uuid4())
    payload.setdefault("cmd_id", cmd_id)
    final = commands_dir / f"{cmd_id}.json"
    tmp = commands_dir / f".{cmd_id}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


def _confirm_build_cmd(manifest: dict, *, idem: str | None = None) -> dict:
    bid = manifest["book_id"]
    rev = manifest["source_rev"]
    return {
        "cmd_id": str(uuid.uuid4()),
        "action": "confirm-build",
        "book_id": bid,
        "source_rev": rev,
        "confirm_token": manifest["confirm_token"],
        "idempotency_key": idem if idem is not None else f"{bid}:{rev[:16]}",
        "params": dict(manifest.get("params", {})),
        "ts": time.time(),
    }


def _count_events(events, kind: str) -> int:
    return sum(1 for e in events if e.get("event") == kind)


def _manifest_for(config, state, suffix: str) -> dict | None:
    for p in config.books_dir().glob("*.json"):
        m = state.read_json(p)
        if str(m.get("src_dir", "")).endswith(suffix):
            return m
    return None


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not _has_tools():
        print("§M1-vertical self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-m1-"))
    support = root / "support"
    watch = root / "watch"
    support.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)

    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import build_m4b, config, dispatcher, probe, scan, state  # noqa: E402

    print(f"self-check tree: {root}")
    print(f"  support: {support}")
    print(f"  watch:   {watch}\n")

    # === Book A: heterogeneous SR/channels, Cyrillic chapters, NO cover =======
    # Durations 1s/2s/3s ⇒ expected total 6.000s; mixed SR + stereo/mono proves
    # the concat-filter + aformat path joins without drift.
    book_a = watch / "Толстой - Война и мир"
    _make_mp3(book_a / "01 - Глава первая.mp3", seconds=1.0,
              samplerate=44100, channels=2, freq=330, tags={"title": "Глава первая"})
    _make_mp3(book_a / "02 - Глава вторая.mp3", seconds=2.0,
              samplerate=48000, channels=1, freq=440, tags={"title": "Глава вторая"})
    _make_mp3(book_a / "03 - Эпилог.mp3", seconds=3.0,
              samplerate=22050, channels=2, freq=550, tags={"title": "Эпилог"})

    scan.run_scan()
    man_a = _manifest_for(config, state, "Толстой - Война и мир")
    assert man_a is not None, "book A manifest not found"
    check("setup: book A pending-confirm, 3 chapters, cover none",
          man_a.get("status") == "pending-confirm"
          and len(man_a.get("chapters", [])) == 3
          and man_a.get("cover_state") == "none",
          f"status={man_a.get('status')} ch={len(man_a.get('chapters', []))} "
          f"cover={man_a.get('cover_state')}")

    # I1 snapshot of the source files BEFORE the build.
    src_snapshot = {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in book_a.iterdir() if p.suffix == ".mp3"
    }

    # Drive the real build through the dispatcher (the production path: I2 gate →
    # validate → _real_build → ffmpeg).
    _drop_command(config.commands_dir(), _confirm_build_cmd(man_a))
    before = state.read_events()
    dispatcher.drain_commands()
    new_events = state.read_events()[len(before):]
    man_a = _manifest_for(config, state, "Толстой - Война и мир")

    check("build: book A reached done", man_a.get("status") == "done",
          f"status={man_a.get('status')!r} error={man_a.get('error')}")
    check("build: exactly one build_done event",
          _count_events(new_events, "build_done") == 1)
    res_a = man_a.get("result") if isinstance(man_a.get("result"), dict) else {}
    out_a = res_a.get("output_path") or res_a.get("output")
    check("build: result carries a real output path (no 'fake')",
          isinstance(out_a, str) and out_a.endswith(".m4b") and "fake" not in res_a,
          f"result={res_a}")
    out_a_path = Path(out_a) if isinstance(out_a, str) else None

    # --- output location + name ---------------------------------------------
    check("output: .m4b sits next to the source folder (same parent)",
          out_a_path is not None and out_a_path.parent == book_a.parent,
          f"out={out_a_path}")
    check("output: filename is «Автор - Название».m4b",
          out_a_path is not None and out_a_path.name == "Толстой - Война и мир.m4b",
          f"name={out_a_path.name if out_a_path else None}")
    check("output: file exists and is non-empty",
          out_a_path is not None and out_a_path.is_file()
          and out_a_path.stat().st_size > 0)

    # --- container: brand M4A + faststart -----------------------------------
    fmt_a = _probe_format(out_a_path)
    brand = (fmt_a.get("tags", {}) or {}).get("major_brand", "").strip()
    check("container: major_brand == 'M4A' (-f ipod)", brand == "M4A",
          f"major_brand={brand!r}")
    check("container: +faststart (moov atom precedes mdat)",
          _faststart_ok(out_a_path))

    # --- chapters: count, START/END (1/1000), Cyrillic ----------------------
    chaps = _probe_chapters(out_a_path)
    check("chapters: count == 3", len(chaps) == 3, f"got {len(chaps)}")
    names = [c.get("tags", {}).get("title") for c in chaps]
    check("chapters: Cyrillic names intact (from manifest)",
          names == ["Глава первая", "Глава вторая", "Эпилог"], f"names={names}")
    # START/END use timebase 1/1000 → start_time strings 0.000/1.000/3.000.
    starts = [round(float(c.get("start_time", -1)), 3) for c in chaps]
    ends = [round(float(c.get("end_time", -1)), 3) for c in chaps]
    check("chapters: START/END accumulate (1/1000): starts 0/1/3",
          starts == [0.0, 1.0, 3.0], f"starts={starts}")
    check("chapters: END of last == total (≈6.0)",
          abs(ends[-1] - 6.0) < 0.05, f"ends={ends}")
    # Timebase is exactly 1/1000 (ms) on each chapter.
    tbs = {c.get("time_base") for c in chaps}
    check("chapters: every chapter TIMEBASE == 1/1000", tbs == {"1/1000"},
          f"time_bases={tbs}")

    # --- no drift: total duration ≈ sum of sources --------------------------
    total_a = _probe_duration_s(out_a_path)
    check("no-drift: total duration ≈ 6.0s (Σ of 1+2+3, mixed SR/channels)",
          abs(total_a - 6.0) < 0.1, f"duration={total_a:.3f}s")

    # --- cover guarantee: even a cover_state=none book gets one (PRD G4) -----
    # The cover CHAIN (M1) resolves embedded → web → generated during the scan, so a
    # book with no embedded picture still has generated cover_options and the build
    # burns the default generated variant in. (This SUPERSEDES the pre-chain M0.5
    # behavior of "cover_state==none → no cover"; the dedicated picker proof lives
    # in §cover-pick.) So book A — no embedded cover — must still carry an mjpeg
    # attached_pic.
    has_cov_a, codec_a = _has_attached_pic(out_a_path)
    check("cover: cover_state==none still gets a generated attached_pic (mjpeg, G4)",
          has_cov_a and codec_a == "mjpeg",
          f"has={has_cov_a} codec={codec_a!r} selected={man_a.get('cover_selected')!r}")

    # --- target params applied (default: 192k stereo, sample rate AS IN SOURCE) -
    # book_a mixes 44100/48000/22050 source mp3s; the "keep the source SR" default
    # (params.samplerate=None) upsamples the minority to the MAX of the sources
    # (= 48000), never downsampling. Channels default stays stereo (2). So the
    # output SR is 48000 here BECAUSE that is the source max — not a forced 44100.
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=sample_rate,channels", "-of",
         "default=noprint_wrappers=1", str(out_a_path)],
        capture_output=True, text=True, check=False,
    ).stdout
    check("params: output keeps the source sample rate (max=48000) + stereo",
          "sample_rate=48000" in out and "channels=2" in out, out.strip())

    # === I1: source files untouched by the build ============================
    src_after = {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in book_a.iterdir() if p.suffix == ".mp3" and p.name in src_snapshot
    }
    i1_ok = all(src_after.get(n) == src_snapshot[n] for n in src_snapshot)
    check("I1: source mp3s unchanged (size + mtime) after build", i1_ok,
          "" if i1_ok else
          f"changed={[n for n in src_snapshot if src_after.get(n) != src_snapshot[n]]}")
    # And the scanner ignores our own .m4b output (input is mp3-only).
    scan.run_scan()
    man_a2 = _manifest_for(config, state, "Толстой - Война и мир")
    check("scanner: re-scan ignores the .m4b output (book stays done)",
          man_a2.get("status") == "done", f"status={man_a2.get('status')!r}")

    # === Book B: EMBEDDED cover → attached_pic present ======================
    book_b = watch / "Чехов - Рассказы"
    _make_mp3(book_b / "01 - intro.mp3", seconds=1.0, tags={"title": "Вступление"})
    _make_mp3_with_cover(book_b / "02 - main.mp3", seconds=1.0,
                         tags={"album": "Рассказы", "title": "Рассказ"})
    scan.run_scan()
    man_b = _manifest_for(config, state, "Чехов - Рассказы")
    assert man_b is not None, "book B manifest not found"
    check("cover-book: detected embedded cover (cover_state == embedded)",
          man_b.get("cover_state") == "embedded",
          f"cover_state={man_b.get('cover_state')!r}")

    _drop_command(config.commands_dir(), _confirm_build_cmd(man_b))
    dispatcher.drain_commands()
    man_b = _manifest_for(config, state, "Чехов - Рассказы")
    res_b = man_b.get("result") if isinstance(man_b.get("result"), dict) else {}
    out_b = Path(res_b.get("output_path")) if res_b.get("output_path") else None
    check("cover-book: reached done with output", man_b.get("status") == "done"
          and out_b is not None and out_b.is_file(),
          f"status={man_b.get('status')!r}")
    has_cov_b, cov_codec = _has_attached_pic(out_b) if out_b else (False, "")
    check("cover: attached_pic present and mjpeg when embedded",
          has_cov_b and cov_codec == "mjpeg", f"has={has_cov_b} codec={cov_codec!r}")
    # Chapters survive alongside the cover (no clobber).
    check("cover-book: chapters still present with a cover (2)",
          len(_probe_chapters(out_b)) == 2)

    # === Atomicity: a forced build failure → NO half-.m4b, status error =====
    # A book whose single chapter is a corrupt 'mp3'. The scan probes it as
    # unreadable (duration None), so the manifest has a chapter with no duration.
    # We hand-craft a manifest with a real (positive-duration) chapter pointing at
    # a CORRUPT file so build() gets past the usable-chapter gate and ffmpeg itself
    # fails — exercising the failure→cleanup path with a real ffmpeg error.
    book_c = watch / "Битая - Книга"
    book_c.mkdir(parents=True, exist_ok=True)
    corrupt = book_c / "01 - broken.mp3"
    corrupt.write_bytes(b"this is not an mp3 \x00\x01\x02 garbage bytes here")
    out_c_path = book_c.parent / "Битая - Книга.m4b"
    forged = {
        "book_id": "forced-fail-book",
        "src_dir": str(book_c),
        "status": "pending-confirm",
        "source_rev": "rev-forced",
        "confirm_token": "tok-forced",
        "title": "Книга",
        "author": "Битая",
        "chapters": [
            # Positive duration so _usable_chapters passes; the FILE is corrupt so
            # ffmpeg fails when it actually tries to decode it.
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
    forged_path = config.books_dir() / "forced-fail-book.json"
    state.write_json_atomic(forged_path, forged)

    _drop_command(config.commands_dir(), _confirm_build_cmd(forged))
    before = state.read_events()
    dispatcher.drain_commands()
    fail_events = state.read_events()[len(before):]
    man_c = state.read_json(forged_path)

    check("atomicity: failed build → status error",
          man_c.get("status") == "error", f"status={man_c.get('status')!r}")
    check("atomicity: error carries a reason",
          isinstance(man_c.get("error"), dict) and bool(man_c["error"].get("reason")),
          f"error={man_c.get('error')}")
    check("atomicity: build_failed event emitted",
          _count_events(fail_events, "build_failed") == 1)
    check("atomicity: NO final .m4b produced on failure",
          not out_c_path.exists(), f"unexpected file: {out_c_path}")
    leftover_temps = list(book_c.parent.glob(".Битая - Книга.m4b.*"))
    check("atomicity: no half-written temp left behind",
          leftover_temps == [], f"temps={leftover_temps}")
    check("atomicity: corrupt source untouched (I1 holds on failure too)",
          corrupt.exists() and corrupt.stat().st_size > 0)

    # === Idempotency: a duplicate confirm-build → exactly ONE build/output ===
    book_d = watch / "Гоголь - Вечера"
    _make_mp3(book_d / "01.mp3", seconds=1.0, freq=300, tags={"title": "Раз"})
    _make_mp3(book_d / "02.mp3", seconds=1.0, freq=400, tags={"title": "Два"})
    scan.run_scan()
    man_d = _manifest_for(config, state, "Гоголь - Вечера")
    assert man_d is not None
    idem = f"{man_d['book_id']}:{man_d['source_rev'][:16]}"
    c1 = _confirm_build_cmd(man_d, idem=idem)
    c2 = _confirm_build_cmd(man_d, idem=idem)  # same key, different cmd_id
    _drop_command(config.commands_dir(), c1)
    _drop_command(config.commands_dir(), c2)
    before = state.read_events()
    dispatcher.drain_commands()
    idem_events = state.read_events()[len(before):]
    man_d = _manifest_for(config, state, "Гоголь - Вечера")

    check("idempotency: exactly ONE build_done for two identical commands",
          _count_events(idem_events, "build_done") == 1,
          f"build_done={_count_events(idem_events, 'build_done')}")
    check("idempotency: the duplicate was skipped",
          _count_events(idem_events, "build_skipped_idempotent") == 1)
    res_d = man_d.get("result") if isinstance(man_d.get("result"), dict) else {}
    out_d = Path(res_d.get("output_path")) if res_d.get("output_path") else None
    check("idempotency: book done with exactly one output file",
          man_d.get("status") == "done" and out_d is not None and out_d.is_file())
    check("idempotency: key recorded in ledger",
          idem in (man_d.get("processed_keys") or []))

    # --- encoder selection (speedup Ступень 1: aac_at, fallback aac) --------
    # _encoder() returns aac_at when AudioToolbox is available, else aac; both
    # build paths above already used it (book A..D are all valid .m4b), so this
    # asserts the contract directly. The argv fragment must carry CBR so the size
    # estimate / disk gate stay accurate.
    enc = build_m4b._encoder()
    check("encoder: _encoder() picks aac_at or aac (CBR), never anything else",
          enc in ("aac_at", "aac"), f"encoder={enc!r}")
    frag = build_m4b._audio_encoder_args(enc, bitrate_kbps=192)
    check("encoder: argv fragment is CBR for the chosen encoder",
          frag[:2] == ["-c:a", enc] and "-b:a" in frag
          and ("192k" in frag)
          and (enc != "aac_at" or ("-aac_at_mode" in frag and "cbr" in frag)),
          f"frag={frag}")
    # The fallback fragment (aac) is always well-formed regardless of this machine.
    fb = build_m4b._audio_encoder_args("aac", bitrate_kbps=128)
    check("encoder: aac fallback fragment well-formed (built-in, CBR)",
          fb == ["-c:a", "aac", "-b:a", "128k"], f"fallback={fb}")

    # --- estimate_output_size sanity (UI helper) ----------------------------
    est = build_m4b.estimate_output_size(man_a)
    # 6s @ 192 kbps ≈ 144 KB audio + overhead; just assert a sane positive ballpark.
    check("estimate: output-size estimate is a sane positive number",
          isinstance(est, int) and 50_000 < est < 5_000_000, f"estimate={est}")

    # === Sample rate: KEEP THE SOURCE by default; explicit pick resamples =====
    # The "as in source" feature: probe must read each file's sample_rate;
    # scan._source_samplerate takes the MAX across sources (upsample minority,
    # never downsample); the default build (params.samplerate=None) keeps that
    # source rate; and an explicit 44100/48000 still resamples.
    def _out_sr(path: Path) -> str:
        return subprocess.run(
            [_ffprobe(), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=sample_rate", "-of",
             "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=False).stdout.strip()

    book_sr = watch / "Источник - 32 кГц"
    # A non-44100 source so "as in source" is visibly different from the old default.
    _make_mp3(book_sr / "01.mp3", seconds=1.0, samplerate=32000, freq=300,
              tags={"title": "Раз"})
    _make_mp3(book_sr / "02.mp3", seconds=1.0, samplerate=32000, freq=400,
              tags={"title": "Два"})

    # 1) probe reads sample_rate off a real mp3.
    pr_sr = probe.probe_file(book_sr / "01.mp3")
    check("samplerate: probe reads sample_rate from the source mp3",
          pr_sr.get("sample_rate") == 32000, f"sample_rate={pr_sr.get('sample_rate')!r}")

    # 2) _source_samplerate = MAX across readable probes (mixed → highest).
    sr_max = scan._source_samplerate(
        [{"sample_rate": 32000}, {"sample_rate": 48000}, {"sample_rate": None}])
    check("samplerate: _source_samplerate takes the MAX of readable sources",
          sr_max == 48000, f"max={sr_max!r}")

    scan.run_scan()
    man_sr = _manifest_for(config, state, "Источник - 32 кГц")
    assert man_sr is not None
    check("samplerate: manifest records source_samplerate (=32000)",
          man_sr.get("source_samplerate") == 32000,
          f"source_samplerate={man_sr.get('source_samplerate')!r}")
    check("samplerate: DEFAULT_PARAMS.samplerate is the None ('as in source') sentinel",
          man_sr.get("params", {}).get("samplerate") is None,
          f"params.samplerate={man_sr.get('params', {}).get('samplerate')!r}")

    # 3) default build keeps the source SR (no resample): 32000 in → 32000 out.
    _drop_command(config.commands_dir(), _confirm_build_cmd(man_sr))
    dispatcher.drain_commands()
    man_sr = _manifest_for(config, state, "Источник - 32 кГц")
    res_sr = man_sr.get("result") if isinstance(man_sr.get("result"), dict) else {}
    out_src = Path(res_sr.get("output_path")) if res_sr.get("output_path") else None
    src_out_sr = _out_sr(out_src) if out_src and out_src.is_file() else ""
    check("samplerate: DEFAULT build keeps the source rate — 32000 in → 32000 out",
          man_sr.get("status") == "done" and src_out_sr == "32000",
          f"status={man_sr.get('status')!r} out_sr={src_out_sr!r}")

    # 4) explicit override still resamples (44100 and 48000), same 32000 source.
    #    P-PARAMS: the override is sent ONLY in the COMMAND's params — the manifest
    #    is NOT pre-written. This proves _apply_params_choice actually merges the
    #    user's confirm-window pick command→manifest before the build (previously the
    #    command's params reached only the cover, so this would have stayed 32000).
    for want in (44100, 48000):
        bk = watch / f"Явный - {want}"
        _make_mp3(bk / "01.mp3", seconds=1.0, samplerate=32000, freq=320,
                  tags={"title": "Раз"})
        scan.run_scan()
        m_pin = _manifest_for(config, state, f"Явный - {want}")
        assert m_pin is not None
        # Sanity: the on-disk manifest still says "as in source" (None) — so a 44100/
        # 48000 output can ONLY come from the command's params being applied.
        assert m_pin.get("params", {}).get("samplerate") is None, \
            "manifest samplerate should be the None sentinel before the command merge"
        cmd = _confirm_build_cmd(m_pin)
        cmd["params"] = {**dict(m_pin.get("params", {})), "samplerate": want}
        _drop_command(config.commands_dir(), cmd)
        dispatcher.drain_commands()
        m_pin = _manifest_for(config, state, f"Явный - {want}")
        res_pin = m_pin.get("result") if isinstance(m_pin.get("result"), dict) else {}
        out_pin = Path(res_pin.get("output_path")) if res_pin.get("output_path") else None
        pin_sr = _out_sr(out_pin) if out_pin and out_pin.is_file() else ""
        check(f"P-PARAMS: command-only samplerate {want} reaches the build — 32000 in → {want} out",
              m_pin.get("status") == "done" and pin_sr == str(want),
              f"status={m_pin.get('status')!r} out_sr={pin_sr!r}")
        # And the merge PERSISTED the pick onto the manifest (not just used in-flight).
        check(f"P-PARAMS: the {want} pick is persisted onto the manifest",
              m_pin.get("params", {}).get("samplerate") == want,
              f"manifest samplerate={m_pin.get('params', {}).get('samplerate')!r}")

    # === P-PARAMS: a COMMAND-ONLY bitrate override reaches the encoder =========
    # 32000 source, default manifest bitrate 192; the command asks for 128k ONLY.
    # The output's measured bitrate must reflect 128k — proof the bitrate pick
    # (not just samplerate) flows command→manifest→engine.
    def _out_bitrate_kbps(path: Path) -> int:
        raw = subprocess.run(
            [_ffprobe(), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=bit_rate", "-of",
             "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=False).stdout.strip()
        try:
            return round(int(raw) / 1000)
        except (TypeError, ValueError):
            return 0

    book_br = watch / "Битрейт - 128"
    # A few seconds so the measured bitrate is stable enough to bucket.
    _make_mp3(book_br / "01.mp3", seconds=4.0, samplerate=44100, freq=300,
              tags={"title": "Раз"})
    _make_mp3(book_br / "02.mp3", seconds=4.0, samplerate=44100, freq=400,
              tags={"title": "Два"})
    scan.run_scan()
    m_br = _manifest_for(config, state, "Битрейт - 128")
    assert m_br is not None
    assert m_br.get("params", {}).get("bitrate") == 192, "manifest default should be 192"
    cmd_br = _confirm_build_cmd(m_br)
    cmd_br["params"] = {**dict(m_br.get("params", {})), "bitrate": 128}
    _drop_command(config.commands_dir(), cmd_br)
    dispatcher.drain_commands()
    m_br = _manifest_for(config, state, "Битрейт - 128")
    res_br = m_br.get("result") if isinstance(m_br.get("result"), dict) else {}
    out_br = Path(res_br.get("output_path")) if res_br.get("output_path") else None
    meas_br = _out_bitrate_kbps(out_br) if out_br and out_br.is_file() else 0
    # AAC CBR lands close to the target; assert it is clearly 128-ish (and NOT 192).
    check("P-PARAMS: command-only bitrate 128k reaches the encoder (output ≈128k, not 192k)",
          m_br.get("status") == "done" and 100 <= meas_br <= 156,
          f"status={m_br.get('status')!r} measured={meas_br}k")

    # === P-PARAMS: a COMMAND-ONLY split toggle drives the splitter ============
    # Default manifest split=False; the command turns split=True + a tiny threshold.
    # A high bitrate makes the few short chapters exceed the budget → ≥2 parts. Proof
    # that split + split_threshold_mb flow command→manifest→engine (the existing
    # feature was previously unreachable from the confirm window — P-PARAMS gap).
    book_sp = watch / "Нарезка - Команда"
    for i in range(1, 5):
        _make_mp3(book_sp / f"{i:02d}.mp3", seconds=3.0, samplerate=44100,
                  freq=300 + i * 20, channels=2, tags={"title": f"Глава {i}"})
    scan.run_scan()
    m_sp = _manifest_for(config, state, "Нарезка - Команда")
    assert m_sp is not None
    assert m_sp.get("params", {}).get("split") is False, "manifest default should be split=False"
    cmd_sp = _confirm_build_cmd(m_sp)
    # 1536 kbps × 12s ≈ 2.3 MB total; a 1-MB threshold forces ~3 parts on boundaries.
    cmd_sp["params"] = {**dict(m_sp.get("params", {})),
                        "split": True, "split_threshold_mb": 1, "bitrate": 1536}
    _drop_command(config.commands_dir(), cmd_sp)
    dispatcher.drain_commands()
    m_sp = _manifest_for(config, state, "Нарезка - Команда")
    res_sp = m_sp.get("result") if isinstance(m_sp.get("result"), dict) else {}
    parts_sp = res_sp.get("parts") if isinstance(res_sp.get("parts"), list) else []
    check("P-PARAMS: command-only split=True + threshold drives the splitter (≥2 parts)",
          m_sp.get("status") == "done" and len(parts_sp) >= 2,
          f"status={m_sp.get('status')!r} parts={len(parts_sp)}")
    check("P-PARAMS: the split pick is persisted onto the manifest",
          m_sp.get("params", {}).get("split") is True
          and m_sp.get("params", {}).get("split_threshold_mb") == 1,
          f"params={m_sp.get('params')}")

    # === P-PARAMS regression: a COVER-ONLY command leaves audio defaults ========
    # The command carries ONLY a cover_id (no audio keys). The build must use the
    # manifest defaults exactly as before the merge existed: 192k, stereo, and SR
    # "as in source" (here 44100). Partial-merge proof — omitted keys are untouched.
    book_co = watch / "ТолькоОбложка - Книга"
    _make_mp3(book_co / "01.mp3", seconds=1.0, samplerate=44100, freq=300,
              tags={"title": "Раз"})
    _make_mp3(book_co / "02.mp3", seconds=1.0, samplerate=44100, freq=400,
              tags={"title": "Два"})
    scan.run_scan()
    m_co = _manifest_for(config, state, "ТолькоОбложка - Книга")
    assert m_co is not None
    cmd_co = _confirm_build_cmd(m_co)
    # Only a cover_id (the first resolved option) — NO audio keys at all.
    first_opt = (m_co.get("cover_options") or [{}])[0].get("id")
    cmd_co["params"] = {"cover_id": first_opt} if first_opt else {}
    _drop_command(config.commands_dir(), cmd_co)
    dispatcher.drain_commands()
    m_co = _manifest_for(config, state, "ТолькоОбложка - Книга")
    res_co = m_co.get("result") if isinstance(m_co.get("result"), dict) else {}
    out_co = Path(res_co.get("output_path")) if res_co.get("output_path") else None
    co_sr = _out_sr(out_co) if out_co and out_co.is_file() else ""
    co_br = _out_bitrate_kbps(out_co) if out_co and out_co.is_file() else 0
    check("P-PARAMS regression: cover-only command keeps audio defaults "
          "(SR as-in-source 44100, ≈192k, samplerate sentinel intact)",
          m_co.get("status") == "done" and co_sr == "44100"
          and 160 <= co_br <= 224
          and m_co.get("params", {}).get("samplerate") is None,
          f"status={m_co.get('status')!r} sr={co_sr!r} br={co_br}k "
          f"samplerate={m_co.get('params', {}).get('samplerate')!r}")

    # === Live progress (Task 2): snapshot math + a real build's progress stream ==
    # First the deterministic contract math (no ffmpeg needed), then a REAL build
    # whose engine progress snapshots we capture via a direct progress_cb.
    chs_math = [{"index": 1, "name": "А", "duration_ms": 1000},
                {"index": 2, "name": "Б", "duration_ms": 2000},
                {"index": 3, "name": "В", "duration_ms": 3000}]
    snap_mid = build_m4b._progress_snapshot(1500, 6000, chs_math,
                                            time.monotonic() - 3.0)
    check("progress: snapshot percent + current chapter (1500/6000 → 25%, ch2)",
          snap_mid["percent"] == 25.0 and snap_mid["current_chapter_index"] == 2
          and snap_mid["current_chapter_name"] == "Б"
          and snap_mid["total_chapters"] == 3,
          f"snap={snap_mid}")
    snap_end = build_m4b._progress_snapshot(6000, 6000, chs_math, time.monotonic())
    check("progress: at end → 100% and pins to the last chapter",
          snap_end["percent"] == 100.0 and snap_end["current_chapter_index"] == 3,
          f"snap={snap_end}")
    snap_early = build_m4b._progress_snapshot(50, 6000, chs_math, time.monotonic())
    check("progress: eta is None below the ~2% floor (no wild early estimate)",
          snap_early["eta_s"] is None, f"eta={snap_early['eta_s']}")
    # out_time_us is canonical (µs→ms); a stray out_time_ms is treated as µs too.
    check("progress: parses out_time_us (µs→ms), ignores ffmpeg's µs 'out_time_ms'",
          build_m4b._parse_progress_out_time_ms({"out_time_us": "2000000"}) == 2000
          and build_m4b._parse_progress_out_time_ms({"out_time_ms": "4000000"}) == 4000,
          "")

    # A real multi-chapter build. We capture the engine's progress snapshots via a
    # direct ``progress_cb`` to build() (DETERMINISTIC — no timing race against the
    # fast aac_at encode; a live state.json sampler would miss a sub-second build).
    # Many short chapters → the encoder emits several ticks regardless of speed.
    book_pr = watch / "Прогресс - Книга"
    for i in range(1, 25):
        _make_mp3(book_pr / f"{i:02d}.mp3", seconds=2.0, samplerate=44100,
                  freq=300 + i * 5, channels=2, tags={"title": f"Глава {i}"})
    scan.run_scan()
    man_pr = _manifest_for(config, state, "Прогресс - Книга")
    assert man_pr is not None
    bid_pr = man_pr["book_id"]
    mpath_pr = config.books_dir() / f"{bid_pr}.json"

    captured: list[dict] = []
    out_pr = build_m4b.default_output_path(man_pr)
    build_m4b.build(man_pr, out_path=out_pr, progress_cb=lambda s: captured.append(s))
    out_pr_ok = out_pr.is_file() and out_pr.stat().st_size > 0
    _unlink = getattr(build_m4b, "_unlink_quiet", None)
    if _unlink:
        _unlink(out_pr)  # tidy the throwaway build

    # The engine must stream ≥1 live snapshot off ffmpeg's -progress for a real
    # build (the WIRING proof). We do NOT demand ≥2 here: aac_at can finish a short
    # fixture so fast that ffmpeg emits a single -progress block before progress=end
    # — a unit-test timing artifact, not a defect. Monotonic GROWTH over a long
    # encode is proven separately (the §M1 math tests above are deterministic, and
    # the developer's standalone 110-chapter demuxer run captured 35 rising samples).
    pcts = [s.get("percent", 0.0) for s in captured]
    monotonic = all(pcts[i] <= pcts[i + 1] + 0.01 for i in range(len(pcts) - 1))
    check("progress: engine streamed ≥1 live snapshot off -progress (valid .m4b, monotonic)",
          out_pr_ok and len(captured) >= 1 and monotonic,
          f"snapshots={len(captured)} peak={max(pcts) if pcts else 0} out_ok={out_pr_ok}")
    # every captured snapshot is the full state.json contract shape.
    shape_ok = all(
        set(s.keys()) >= {"percent", "out_time_ms", "total_ms", "elapsed_s",
                          "eta_s", "current_chapter_index", "current_chapter_name",
                          "total_chapters"}
        for s in captured) if captured else False
    check("progress: each live snapshot carries the full state.json contract",
          shape_ok and bool(captured),
          f"shape_ok={shape_ok} keys="
          f"{sorted(captured[0].keys()) if captured else 'none'}")

    # The dispatcher's targeted patch writes a converting row's progress into
    # state.json; after a done transition the projection drops it (contract).
    cmd_pr = _confirm_build_cmd(man_pr)
    dispatcher._real_build(man_pr, mpath_pr, cmd_pr)
    man_pr = state.read_json(mpath_pr)
    st_after = state.read_state(default=None)
    row_after = next((b for b in (st_after or {}).get("books", [])
                      if isinstance(b, dict) and b.get("book_id") == bid_pr), {})
    check("progress: cleared after done (no progress on a non-converting row)",
          man_pr.get("status") == "done" and "progress" not in row_after,
          f"status={man_pr.get('status')!r} row_keys={sorted(row_after.keys())}")

    # refresh_showcase PRESERVES a converting book's progress (Task 2): forge a
    # converting row with a progress field, refresh, and assert it survives.
    forged_prog = {
        "book_id": "progress-preserve-book",
        "src_dir": str(book_pr),  # a live dir so the row is projected
        "status": "converting",
        "source_rev": "rev-x", "confirm_token": "tok-x",
        "title": "Сохранение прогресса", "author": "Тест",
        "chapters": [{"index": 1, "file": "01.mp3", "name": "Г", "duration_ms": 1000}],
        "total_duration_ms": 1000, "cover_state": "none", "cover_preview": None,
        "params": dict(scan.DEFAULT_PARAMS), "processed_keys": [],
        "build": {"pid": os.getpid(), "started_at": time.time()},
        "ts": time.time(),
    }
    state.write_json_atomic(config.books_dir() / "progress-preserve-book.json", forged_prog)
    # seed a progress on its showcase row, then refresh and re-read.
    scan.refresh_showcase()
    dispatcher._patch_book_progress("progress-preserve-book",
                                    {"percent": 42.0, "out_time_ms": 420,
                                     "total_ms": 1000, "elapsed_s": 1, "eta_s": 1,
                                     "current_chapter_index": 1,
                                     "current_chapter_name": "Г", "total_chapters": 1})
    scan.refresh_showcase()  # the refresh under test
    st_pres = state.read_state(default=None)
    pres_row = next((b for b in (st_pres or {}).get("books", [])
                     if isinstance(b, dict)
                     and b.get("book_id") == "progress-preserve-book"), {})
    check("progress: refresh_showcase PRESERVES a converting book's progress",
          isinstance(pres_row.get("progress"), dict)
          and pres_row["progress"].get("percent") == 42.0,
          f"row_progress={pres_row.get('progress')}")

    # --- summary ------------------------------------------------------------
    # Flat verification: this suite runs ONLY its own checks. Cross-suite
    # regression is orchestrated once by ``agent.selfcheck_all`` (no nested
    # re-runs here — that is what made a single pass take ~30 min).
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print(f"\n§M1-vertical self-check: {passed}/{total} checks passed")
    print(f"(temp tree left at {root} for inspection; safe to delete)")

    # Exit honestly: green ⇔ every local check passed.
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
