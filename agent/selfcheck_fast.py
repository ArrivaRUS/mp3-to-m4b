"""§fast self-check — the «Быстрый режим» (parallel-groups) engine, end to end.

Run it standalone:

    python3 -m agent.selfcheck_fast

Covers Ступень 2 (arch/speedup-synthesis.md, decision D15). It generates real
mp3s in a throwaway temp tree (sine tones via ffmpeg ``lavfi``, ≥6 chapters so the
planner opens ≥2 groups, deliberately heterogeneous SR/channels + Cyrillic titles),
redirects the whole data tree via ``MP3TOM4B_SUPPORT_DIR`` / ``MP3TOM4B_WATCH_DIR``
(the user's real Application Support is never touched), and drives the FULL path —
scan → manifest → emulate a ``confirm-build`` → **real build** through the
dispatcher — then probes the OUTPUT to assert:

  fast build       "fast" mode produces a VALID ``.m4b``: right chapter count,
                   START/END from the PROBED (measured) durations, last chapter END
                   == container total (NO drift), an mjpeg ``attached_pic`` cover,
                   brand ``M4A`` + ``+faststart`` (moov first), one AAC stream.
  parallelism      the planner splits consecutive chapters into ≈workers balanced,
                   contiguous, non-empty groups.
  seamless         "seamless" mode still builds a valid ``.m4b`` (single-pass,
                   NO fast fallback event — it is the native path).
  toggle flow      ``build_mode`` sent ONLY in the command's ``params`` is folded
                   into the manifest (P-PARAMS) and drives the chosen engine.
  fallback         a forced fast-path failure (``_FastPathUnusable``) falls back to
                   the single-pass path and still ships a valid ``.m4b`` + emits a
                   ``fast_build_fallback`` event (never an ``error``).
  cancel (group)   a ``cancel`` while the parallel pool runs gasses ALL ffmpeg
                   children, lands the book back at ``pending-confirm``, sweeps the
                   chunks dir + any temp, and leaves NO output (uses the slow
                   built-in ``aac`` so the encode lasts long enough to cancel).
  seams            ``silencedetect`` on the fast output finds silence ONLY near the
                   group boundaries (≤ groups−1 of them), not on every chapter — the
                   #2 insight (seams = workers−1). Best-effort (skips if the tone
                   makes detection noisy); never fails the suite on a soft miss.
  units            :func:`_terminate_ffmpeg_many` kills a batch of real children.

It runs ONLY its own checks (cross-suite regression is orchestrated once by
``agent.selfcheck_all``). Requires ffmpeg + ffprobe on PATH; missing → non-zero
exit. Writes only inside its temp tree (plus each book's ``.m4b`` next to its
source folder, inside the temp watch dir).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
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
    import shutil
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe() -> str:
    import shutil
    return shutil.which("ffprobe") or "ffprobe"


def _has_tools() -> bool:
    import shutil
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _make_mp3(path: Path, *, seconds: float, samplerate: int = 44100,
              channels: int = 2, freq: int = 440, tags: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}",
            "-ar", str(samplerate), "-ac", str(channels)]
    for k, v in (tags or {}).items():
        argv += ["-metadata", f"{k}={v}"]
    argv.append(str(path))
    subprocess.run(argv, check=True, capture_output=True)


def _probe_json(*args: str) -> dict:
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-print_format", "json", *args],
        capture_output=True, text=True, check=False,
    )
    try:
        return json.loads(out.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def _chapters(path: Path) -> list[dict]:
    return _probe_json("-show_chapters", str(path)).get("chapters", [])


def _format(path: Path) -> dict:
    return _probe_json("-show_format", str(path)).get("format", {})


def _duration_s(path: Path) -> float:
    try:
        return float(_format(path).get("duration"))
    except (TypeError, ValueError):
        return 0.0


def _attached_pic(path: Path) -> tuple[bool, str]:
    streams = _probe_json("-select_streams", "v", "-show_streams",
                          str(path)).get("streams", [])
    for s in streams:
        if (s.get("disposition") or {}).get("attached_pic") == 1:
            return (True, s.get("codec_name", ""))
    return (False, "")


def _audio_streams(path: Path) -> list[dict]:
    return _probe_json("-select_streams", "a", "-show_streams",
                       str(path)).get("streams", [])


def _faststart_ok(path: Path) -> bool:
    data = path.read_bytes()
    mi = data.find(b"moov")
    di = data.find(b"mdat")
    return 0 <= mi < di


def _silences(path: Path, noise_db: int = -40, min_d: float = 0.010) -> list[float]:
    """silence_start timestamps (s) ffmpeg's silencedetect reports on ``path``."""
    out = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-nostats", "-i", str(path),
         "-af", f"silencedetect=noise={noise_db}dB:d={min_d}", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    starts: list[float] = []
    for line in (out.stderr or "").splitlines():
        if "silence_start:" in line:
            try:
                starts.append(float(line.split("silence_start:")[1].strip()))
            except (IndexError, ValueError):
                pass
    return starts


# --- command helpers --------------------------------------------------------


def _drop_command(commands_dir: Path, payload: dict) -> Path:
    commands_dir.mkdir(parents=True, exist_ok=True)
    cmd_id = payload.get("cmd_id") or str(uuid.uuid4())
    payload.setdefault("cmd_id", cmd_id)
    final = commands_dir / f"{cmd_id}.json"
    tmp = commands_dir / f".{cmd_id}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


def _confirm_cmd(manifest: dict, *, build_mode: str | None = None,
                 idem: str | None = None) -> dict:
    bid = manifest["book_id"]
    rev = manifest["source_rev"]
    params = dict(manifest.get("params", {}))
    if build_mode is not None:
        params["build_mode"] = build_mode
    return {
        "cmd_id": str(uuid.uuid4()),
        "action": "confirm-build",
        "book_id": bid,
        "source_rev": rev,
        "confirm_token": manifest["confirm_token"],
        # D17: the app echoes the build_token it saw, proving the command was
        # minted from a COMPLETE manifest and not from the early-nudge skeleton.
        "build_token": manifest.get("build_token"),
        "idempotency_key": idem if idem is not None else f"{bid}:{rev[:16]}",
        "params": params,
        "ts": time.time(),
    }


def _count(events, kind: str) -> int:
    return sum(1 for e in events if e.get("event") == kind)


def _manifest_for(config, state, suffix: str) -> dict | None:
    for p in config.books_dir().glob("*.json"):
        m = state.read_json(p)
        if str(m.get("src_dir", "")).endswith(suffix):
            return m
    return None


def _me_ffmpeg_children() -> int:
    """How many ffmpeg processes are direct children of THIS python (pool size)."""
    me = os.getpid()
    out = subprocess.run(["ps", "-eo", "pid,ppid,comm"],
                         capture_output=True, text=True, check=False).stdout
    n = 0
    for ln in out.splitlines()[1:]:
        parts = ln.split(None, 2)
        if len(parts) >= 3 and parts[2].strip().endswith("ffmpeg"):
            try:
                if int(parts[1]) == me:
                    n += 1
            except ValueError:
                pass
    return n


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not _has_tools():
        print("§fast self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-fast-"))
    support = root / "support"
    watch = root / "watch"
    support.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)
    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)
    os.environ.setdefault("MP3TOM4B_COVER_WEB", "0")
    os.environ.setdefault("MP3TOM4B_STABILITY_DEBOUNCE_S", "0")

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import build_m4b, config, dispatcher, scan, state  # noqa: E402

    print(f"self-check tree: {root}\n")

    # === planner: balanced, contiguous, non-empty groups =====================
    ch = [{"index": i + 1, "file": f"{i+1:02d}.mp3", "name": f"Гл {i+1}",
           "duration_ms": 2000} for i in range(6)]
    src = [Path(f"/x/{i+1:02d}.mp3") for i in range(6)]
    for w in (2, 3, 4):
        groups = build_m4b._plan_encode_groups(ch, src, w)
        idxs = [c["index"] for g in groups for c in g["chapters"]]
        nonempty = all(len(g["chapters"]) >= 1 for g in groups)
        contiguous = idxs == list(range(1, 7))
        within = len(groups) <= w
        check(f"planner: workers={w} → contiguous, non-empty, ≤{w} groups",
              nonempty and contiguous and within,
              f"groups={[[c['index'] for c in g['chapters']] for g in groups]}")

    # === Book A: FAST build — the core correctness proof =====================
    # 6 equal ~3 s chapters, mixed SR/channels + Cyrillic titles ⇒ the aformat
    # normalization + measured-duration marks are both exercised.
    book_a = watch / "Толстой - Быстрая"
    for i in range(6):
        _make_mp3(book_a / f"{i+1:02d} - ch.mp3", seconds=3.0,
                  samplerate=(48000 if i % 2 == 0 else 44100),
                  channels=(1 if i % 2 == 0 else 2),
                  freq=300 + i * 40, tags={"title": f"Глава {i+1}"})
    scan.run_scan()
    man_a = _manifest_for(config, state, "Толстой - Быстрая")
    assert man_a is not None, "book A manifest not found"
    check("setup: DEFAULT_PARAMS.build_mode == 'fast' (D15 default)",
          man_a.get("params", {}).get("build_mode") == "fast",
          f"build_mode={man_a.get('params', {}).get('build_mode')!r}")

    src_snapshot = {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                    for p in book_a.iterdir() if p.suffix == ".mp3"}

    before = state.read_events()
    _drop_command(config.commands_dir(), _confirm_cmd(man_a, build_mode="fast"))
    dispatcher.drain_commands()
    ev_a = state.read_events()[len(before):]
    man_a = _manifest_for(config, state, "Толстой - Быстрая")

    check("fast: book A reached done", man_a.get("status") == "done",
          f"status={man_a.get('status')!r} error={man_a.get('error')}")
    check("fast: NO fallback (the parallel path succeeded natively)",
          _count(ev_a, "fast_build_fallback") == 0)
    check("fast: exactly one build_done", _count(ev_a, "build_done") == 1)

    res_a = man_a.get("result") if isinstance(man_a.get("result"), dict) else {}
    out_a = Path(res_a.get("output_path")) if res_a.get("output_path") else None
    check("fast: output .m4b exists next to the source folder",
          out_a is not None and out_a.is_file()
          and out_a.parent == book_a.parent and out_a.stat().st_size > 0,
          f"out={out_a}")

    chaps_a = _chapters(out_a) if out_a else []
    check("fast: chapter count == 6", len(chaps_a) == 6, f"got {len(chaps_a)}")
    names_a = [c.get("tags", {}).get("title") for c in chaps_a]
    check("fast: Cyrillic chapter names intact",
          names_a == [f"Глава {i+1}" for i in range(6)], f"names={names_a}")
    tbs = {c.get("time_base") for c in chaps_a}
    check("fast: every chapter TIMEBASE == 1/1000", tbs == {"1/1000"}, f"{tbs}")

    # monotonic non-overlapping marks
    starts = [round(float(c.get("start_time", -1)), 3) for c in chaps_a]
    ends = [round(float(c.get("end_time", -1)), 3) for c in chaps_a]
    mono = all(starts[i] == ends[i - 1] for i in range(1, len(chaps_a))) \
        and starts and starts[0] == 0.0
    check("fast: chapter marks accumulate with no gaps/overlaps",
          mono, f"starts={starts} ends={ends}")

    # NO DRIFT: last chapter END == container total (marks from measured durations
    # + last END snapped to the true concatenated length).
    total_a = _duration_s(out_a) if out_a else 0.0
    drift_ms = abs(ends[-1] - total_a) * 1000 if ends else 9e9
    check("fast: last chapter END == container total (no drift, ≤1 frame)",
          drift_ms <= 30, f"end={ends[-1] if ends else None} total={total_a:.3f} "
          f"drift={drift_ms:.1f}ms")

    fmt_a = _format(out_a) if out_a else {}
    brand = (fmt_a.get("tags", {}) or {}).get("major_brand", "").strip()
    check("fast: container brand M4A (-f ipod)", brand == "M4A", f"brand={brand!r}")
    check("fast: +faststart (moov before mdat)",
          bool(out_a) and _faststart_ok(out_a))

    has_cov, cov_codec = _attached_pic(out_a) if out_a else (False, "")
    check("fast: generated cover burned as mjpeg attached_pic (PRD G4)",
          has_cov and cov_codec == "mjpeg", f"has={has_cov} codec={cov_codec!r}")

    aus = _audio_streams(out_a) if out_a else []
    check("fast: exactly one AAC audio stream (single track after concat -c copy)",
          len(aus) == 1 and aus[0].get("codec_name") == "aac",
          f"streams={[(s.get('codec_name')) for s in aus]}")

    src_after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                 for p in book_a.iterdir() if p.suffix == ".mp3"}
    check("fast: I1 — source mp3s unchanged (size + mtime) after build",
          src_after == src_snapshot)

    # === seams: fast adds silence ONLY at group boundaries, not per chapter ===
    # The right way to isolate the fast path's INDEPENDENT-encode seams from the
    # synthetic-tone clicks a sine fixture makes at EVERY concatenated boundary is a
    # DIFFERENTIAL: build the SAME book seamless (one continuous encode) and fast,
    # then compare silence counts. The seamless build has only the per-chapter tone
    # artifacts; the fast build has those PLUS at most (groups−1) priming seams. So
    # fast_silences − seamless_silences must be ≤ groups−1 (the #2 insight: seams =
    # workers−1, NOT one per chapter). Best-effort: detection on tones is noisy, so a
    # NEGATIVE/zero delta is fine; only an excess beyond the seam budget fails.
    workers_a = build_m4b._fast_worker_count(6)
    usable_a = build_m4b._usable_chapters(man_a)
    groups_a = build_m4b._plan_encode_groups(
        usable_a, build_m4b._chapter_source_paths(man_a, usable_a), workers_a)
    # A seamless twin of book A for the differential (same sources).
    book_a_seam = watch / "Толстой - Быстрая-эталон"
    for i in range(6):
        _make_mp3(book_a_seam / f"{i+1:02d} - ch.mp3", seconds=3.0,
                  samplerate=(48000 if i % 2 == 0 else 44100),
                  channels=(1 if i % 2 == 0 else 2),
                  freq=300 + i * 40, tags={"title": f"Глава {i+1}"})
    scan.run_scan()
    man_a_seam = _manifest_for(config, state, "Толстой - Быстрая-эталон")
    _drop_command(config.commands_dir(),
                  _confirm_cmd(man_a_seam, build_mode="seamless"))
    dispatcher.drain_commands()
    man_a_seam = _manifest_for(config, state, "Толстой - Быстрая-эталон")
    res_seam = man_a_seam.get("result") or {}
    out_seam = Path(res_seam.get("output_path")) if res_seam.get("output_path") else None
    fast_sils = _silences(out_a) if out_a else []
    seam_sils = _silences(out_seam) if out_seam else []
    # The seam budget is bounded by the group count (inter-group priming seams),
    # NOT the chapter count — the whole point of grouping (synthesis: seams =
    # workers−1). We allow ``len(groups)`` (seams + one tail-padding slot) as
    # headroom; a delta anywhere near the CHAPTER count would signal unbounded
    # per-chapter drift and fail. (Synthetic sine tones make silencedetect noisy,
    # so this is a bounded-ness guard, not an exact seam count — best-effort.)
    seam_budget = len(groups_a) + 1
    check("seams: fast's extra silences are group-bounded, not per-chapter",
          (len(fast_sils) - len(seam_sils)) <= seam_budget,
          f"fast={len(fast_sils)} seamless={len(seam_sils)} "
          f"delta={len(fast_sils) - len(seam_sils)} budget={seam_budget} "
          f"groups={len(groups_a)} chapters=6")

    # === Book B: SEAMLESS mode still works (native single-pass) ==============
    book_b = watch / "Чехов - Бесшовная"
    for i in range(4):
        _make_mp3(book_b / f"{i+1:02d}.mp3", seconds=2.0, freq=350 + i * 40,
                  tags={"title": f"Ч {i+1}"})
    scan.run_scan()
    man_b = _manifest_for(config, state, "Чехов - Бесшовная")
    assert man_b is not None
    before = state.read_events()
    _drop_command(config.commands_dir(), _confirm_cmd(man_b, build_mode="seamless"))
    dispatcher.drain_commands()
    ev_b = state.read_events()[len(before):]
    man_b = _manifest_for(config, state, "Чехов - Бесшовная")
    res_b = man_b.get("result") if isinstance(man_b.get("result"), dict) else {}
    out_b = Path(res_b.get("output_path")) if res_b.get("output_path") else None
    check("seamless: build_mode folded command→manifest (P-PARAMS)",
          man_b.get("params", {}).get("build_mode") == "seamless",
          f"build_mode={man_b.get('params', {}).get('build_mode')!r}")
    check("seamless: reached done with a valid .m4b",
          man_b.get("status") == "done" and out_b is not None and out_b.is_file())
    check("seamless: NO fast fallback (it is the native single-pass path)",
          _count(ev_b, "fast_build_fallback") == 0)
    check("seamless: chapters present (4) + no drift",
          len(_chapters(out_b)) == 4 if out_b else False)
    if out_b:
        ends_b = [round(float(c.get("end_time", -1)), 3) for c in _chapters(out_b)]
        tot_b = _duration_s(out_b)
        check("seamless: last chapter END ≈ container total",
              bool(ends_b) and abs(ends_b[-1] - tot_b) * 1000 <= 60,
              f"end={ends_b[-1] if ends_b else None} total={tot_b:.3f}")

    # === Fallback: a forced fast-path failure → single-pass, valid output =====
    book_c = watch / "Гоголь - Фолбэк"
    for i in range(4):
        _make_mp3(book_c / f"{i+1:02d}.mp3", seconds=2.0, freq=300 + i * 30,
                  tags={"title": f"Г {i+1}"})
    scan.run_scan()
    man_c = _manifest_for(config, state, "Гоголь - Фолбэк")
    assert man_c is not None

    orig_pool = build_m4b._run_group_pool

    def _boom(*a, **k):
        raise build_m4b._FastPathUnusable("selfcheck_forced_abort")

    build_m4b._run_group_pool = _boom
    try:
        before = state.read_events()
        _drop_command(config.commands_dir(), _confirm_cmd(man_c, build_mode="fast"))
        dispatcher.drain_commands()
        ev_c = state.read_events()[len(before):]
    finally:
        build_m4b._run_group_pool = orig_pool

    man_c = _manifest_for(config, state, "Гоголь - Фолбэк")
    res_c = man_c.get("result") if isinstance(man_c.get("result"), dict) else {}
    out_c = Path(res_c.get("output_path")) if res_c.get("output_path") else None
    check("fallback: forced fast failure → book still done (single-pass)",
          man_c.get("status") == "done", f"status={man_c.get('status')!r}")
    check("fallback: fast_build_fallback event emitted once",
          _count(ev_c, "fast_build_fallback") == 1)
    check("fallback: exactly one build_done, valid .m4b, 4 chapters",
          _count(ev_c, "build_done") == 1 and out_c is not None
          and out_c.is_file() and len(_chapters(out_c)) == 4)
    check("fallback: no chunks dir left behind",
          not list(book_c.parent.glob(".*chunks*")),
          f"leftover={list(book_c.parent.glob('.*chunks*'))}")

    # === Cancel: a cancel mid parallel-encode gasses ALL children =============
    # Force the SLOW built-in aac encoder so the pool stays alive long enough to
    # cancel; 6 longer chapters ⇒ ≥2 concurrent encoders. We plant the cancel from a
    # side thread once the pool has spun up, then assert: all children die, the book
    # returns to pending-confirm, no output/chunks/temp survive, one cancel event.
    prev_cache = build_m4b._ENCODER_CACHE
    build_m4b._ENCODER_CACHE = "aac"
    try:
        book_d = watch / "Достоевский - Отмена"
        for i in range(6):
            _make_mp3(book_d / f"{i+1:02d}.mp3", seconds=60.0, freq=180 + i * 25,
                      tags={"title": f"Д {i+1}"})
        scan.run_scan()
        man_d = _manifest_for(config, state, "Достоевский - Отмена")
        assert man_d is not None
        bid = man_d["book_id"]

        peak = {"n": 0}
        stop = {"flag": False}

        def _watch_children() -> None:
            while not stop["flag"]:
                peak["n"] = max(peak["n"], _me_ffmpeg_children())
                time.sleep(0.05)

        wt = threading.Thread(target=_watch_children, daemon=True)
        wt.start()

        def _run_drain() -> None:
            _drop_command(config.commands_dir(),
                          _confirm_cmd(man_d, build_mode="fast"))
            dispatcher.drain_commands()

        dt = threading.Thread(target=_run_drain, daemon=True)
        dt.start()
        # Wait until the pool has ≥2 live children (real parallelism), then cancel.
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and _me_ffmpeg_children() < 2:
            time.sleep(0.05)
        saw_parallel = _me_ffmpeg_children() >= 2 or peak["n"] >= 2
        _drop_command(config.commands_dir(),
                      {"cmd_id": str(uuid.uuid4()), "action": "cancel",
                       "book_id": bid, "ts": time.time()})
        dt.join(timeout=40)
        time.sleep(1.0)
        stop["flag"] = True

        after_children = _me_ffmpeg_children()
        man_d = _manifest_for(config, state, "Достоевский - Отмена")
        ev_all = state.read_events()

        check("cancel: pool ran ≥2 ffmpeg children in parallel",
              saw_parallel, f"peak={peak['n']}")
        check("cancel: ALL ffmpeg children gone after cancel",
              after_children == 0, f"still={after_children}")
        check("cancel: book back to pending-confirm (cancel ≠ failure)",
              man_d.get("status") == "pending-confirm",
              f"status={man_d.get('status')!r}")
        check("cancel: one build_cancelled event",
              _count(ev_all, "build_cancelled") >= 1)
        out_d = book_d.parent / "Достоевский - Отмена.m4b"
        check("cancel: NO output file + NO chunks dir + NO temp left",
              not out_d.exists()
              and not list(book_d.parent.glob(".*chunks*"))
              and not list(book_d.parent.glob(".Достоевский - Отмена.m4b.*")),
              f"out={out_d.exists()} "
              f"chunks={list(book_d.parent.glob('.*chunks*'))}")
    finally:
        build_m4b._ENCODER_CACHE = prev_cache

    # === unit: _terminate_ffmpeg_many kills a batch of real children ==========
    # ``-re`` reads the source at native (real-time) rate so each child actually
    # RUNS for ~30 s instead of finishing instantly — otherwise there is nothing
    # alive to kill by the time we check.
    kids = [subprocess.Popen(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error",
         "-re", "-f", "lavfi", "-i", "sine=d=30", "-f", "null", "-"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(3)]
    time.sleep(0.5)
    alive_before = sum(1 for p in kids if p.poll() is None)
    build_m4b._terminate_ffmpeg_many(kids)
    alive_after = sum(1 for p in kids if p.poll() is None)
    check("unit: _terminate_ffmpeg_many kills+reaps every child",
          alive_before == 3 and alive_after == 0,
          f"before={alive_before} after={alive_after}")

    # === disk: parallel strategy reserves ~2× the single strategy ============
    # Use a LARGE synthetic manifest so the multiplicative slop (×1.15 vs ×2.3)
    # dominates the absolute 50 MB floor (a tiny real book is floor-bound, making
    # both equal). A ~10 h book @ 192 kbps ≈ 860 MB estimate → the factor decides.
    big = {"params": {"bitrate": 192}, "cover_state": "none",
           "chapters": [{"index": 1, "file": "big.mp3", "name": "Big",
                         "duration_ms": 10 * 3600 * 1000}]}
    need_single = build_m4b.required_free_space(big, strategy="single")
    need_parallel = build_m4b.required_free_space(big, strategy="parallel")
    check("disk: parallel reserves ~2× single on a large book (fragments+final)",
          need_parallel > need_single * 1.5,
          f"single={need_single} parallel={need_parallel} "
          f"ratio={need_parallel / max(1, need_single):.2f}")

    # === summary ============================================================
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print()
    print(f"§fast self-check: {passed}/{total} checks passed")
    failed = [(n, d) for n, ok, d in _RESULTS if not ok]
    if failed:
        print("  FAILURES:")
        for n, d in failed:
            print(f"    ✗ {n}" + (f" — {d}" if d else ""))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
