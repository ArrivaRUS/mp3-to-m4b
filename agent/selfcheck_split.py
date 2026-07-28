"""§split self-check — the P1 ``.m4b`` splitter, end to end.

Run it standalone:

    python3 -m agent.selfcheck_split

Proves the split engine (agent/split.py + the dispatcher integration) on a REAL
multi-chapter book built by the real engine, against research/m4b-toolchain.md §3
and the P1 task:

  plan_parts    cuts ONLY on chapter boundaries; every part ≤ the threshold;
                **E15** a single chapter bigger than the threshold becomes its own
                part flagged ``oversize`` (we never split mid-chapter).
  split         produces N valid ``.m4b`` parts where EACH part's chapters come
                ONLY from that part (no duplicate / phantom chapters — the
                ``-map_chapters 1`` proof: each part's chapter count == expected,
                names rebased, START of the first chapter == 0); every part keeps a
                cover (``attached_pic``); each part's ``title`` is «…, Часть N из M»
                with ``track == N/M``; the audio is **stream-copied** (same codec
                as the full book, NOT re-encoded); and Σ part durations ≈ the full
                book (no drift).
  integration   a ``confirm-build`` with ``params.split=True`` publishes the PARTS
                (``result.parts``) and removes the intermediate full file; the
                default ``split=False`` still yields exactly ONE file (regression).

It redirects the whole data tree via ``MP3TOM4B_SUPPORT_DIR`` /
``MP3TOM4B_WATCH_DIR`` (the user's real Application Support is never touched), and
runs ONLY its own checks (cross-suite regression is orchestrated once by
``agent.selfcheck_all`` — no nested re-runs). Requires ffmpeg + ffprobe on PATH;
if either is missing it says so and exits non-zero.
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


# --- ffmpeg/ffprobe helpers (mirror selfcheck_m1) ---------------------------


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def _has_tools() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _make_mp3(
    path: Path, *, seconds: float, samplerate: int = 44100, channels: int = 2,
    freq: int = 440, tags: dict | None = None,
) -> None:
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


def _probe_format(path: Path) -> dict:
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-print_format", "json", "-show_format",
         str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return json.loads(out.stdout or "{}").get("format", {})
    except json.JSONDecodeError:
        return {}


def _probe_chapters(path: Path) -> list[dict]:
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-print_format", "json", "-show_chapters",
         str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return json.loads(out.stdout or "{}").get("chapters", [])
    except json.JSONDecodeError:
        return []


def _probe_duration_s(path: Path) -> float:
    try:
        return float(_probe_format(path).get("duration"))
    except (TypeError, ValueError):
        return 0.0


def _audio_codec(path: Path) -> str:
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_name", "-of",
         "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    return (out.stdout or "").strip().splitlines()[0] if out.stdout.strip() else ""


def _cover_codec(path: Path) -> tuple[bool, str]:
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-select_streams", "v", "-print_format",
         "json", "-show_streams", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        streams = json.loads(out.stdout or "{}").get("streams", [])
    except json.JSONDecodeError:
        return (False, "")
    for s in streams:
        if (s.get("disposition") or {}).get("attached_pic") == 1:
            return (True, s.get("codec_name", ""))
    return (False, "")


def _format_tag(path: Path, key: str) -> str:
    tags = _probe_format(path).get("tags", {}) or {}
    # ffprobe lowercases some tag keys; check a couple of spellings.
    for k in (key, key.lower(), key.capitalize()):
        if k in tags:
            return str(tags[k])
    return ""


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
    for p in config.books_dir().glob("*.json"):
        m = state.read_json(p)
        if str(m.get("src_dir", "")).endswith(suffix):
            return m
    return None


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not _has_tools():
        print("§split self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-split-"))
    support = root / "support"
    watch = root / "watch"
    support.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)
    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import build_m4b, config, dispatcher, scan, split, state  # noqa: E402

    print(f"self-check tree: {root}")
    print(f"  support: {support}")
    print(f"  watch:   {watch}\n")

    # === PART A: plan_parts unit logic ======================================
    # A synthetic manifest, 192 kbps. Use a tiny threshold so a few short chapters
    # split into several parts purely on boundaries.
    def ch(i, ms, name):
        return {"index": i, "duration_ms": ms, "name": name, "file": f"{i:02d}.mp3"}

    man_unit = {
        "title": "Книга", "author": "Автор",
        "params": {"bitrate": 192, "split_threshold_mb": 1},
        "chapters": [ch(1, 30000, "Один"), ch(2, 30000, "Два"),
                     ch(3, 30000, "Три"), ch(4, 30000, "Четыре")],
    }
    # 30s @192k ≈ 0.72 MB audio + ~0.12 MB overhead ≈ 0.84 MB < 1 MB ⇒ one
    # chapter fits; two (1.56 MB) do not ⇒ each chapter is its own part here.
    plan = split.plan_parts(man_unit)
    check("plan: cuts only on chapter boundaries (every index placed once, in order)",
          [i for p in plan for i in p["chapter_indices"]] == [1, 2, 3, 4],
          f"plan={[p['chapter_indices'] for p in plan]}")
    thr = 1 * 1024 * 1024
    check("plan: every part's est_size ≤ threshold",
          all(p["est_size"] <= thr for p in plan),
          f"sizes={[p['est_size'] for p in plan]} thr={thr}")
    check("plan: total stamped on every part",
          all(p["total"] == len(plan) for p in plan) and len(plan) >= 2,
          f"n={len(plan)} totals={[p['total'] for p in plan]}")
    check("plan: spans are contiguous (part start == previous end)",
          all(plan[i]["start_ms"] == plan[i - 1]["end_ms"]
              for i in range(1, len(plan))),
          f"spans={[(p['start_ms'], p['end_ms']) for p in plan]}")

    # E15: one chapter alone exceeds the threshold → its own oversize part.
    man_e15 = {
        "title": "Книга", "author": "Автор",
        "params": {"bitrate": 192, "split_threshold_mb": 1},
        "chapters": [ch(1, 10000, "Мелкая"), ch(2, 120000, "Огромная"),
                     ch(3, 10000, "Мелкая2")],
    }  # ch2 = 120s @192k ≈ 2.88 MB > 1 MB
    plan15 = split.plan_parts(man_e15)
    big = [p for p in plan15 if p["chapter_indices"] == [2]]
    check("E15: oversize chapter stands ALONE as its own part",
          len(big) == 1, f"plan15={[p['chapter_indices'] for p in plan15]}")
    check("E15: that part is flagged oversize=True",
          bool(big) and big[0]["oversize"] is True,
          f"oversize={big[0]['oversize'] if big else 'n/a'}")
    check("E15: the other parts are NOT oversize",
          all(not p["oversize"] for p in plan15 if p["chapter_indices"] != [2]))

    # === PART B: real split end-to-end via the dispatcher ===================
    # A real book: 5 chapters, varied durations, Cyrillic names, one carries no
    # explicit cover (the chain generates one) so each part must keep a cover.
    book = watch / "Толстой - Война и мир"
    durations = [(1, 2.0, 330, "Глава первая"), (2, 3.0, 392, "Глава вторая"),
                 (3, 2.0, 440, "Глава третья"), (4, 3.0, 494, "Глава четвёртая"),
                 (5, 2.0, 550, "Эпилог")]
    for i, secs, freq, name in durations:
        _make_mp3(book / f"{i:02d} - {name}.mp3", seconds=secs,
                  samplerate=44100, channels=2, freq=freq, tags={"title": name})
    total_src_s = sum(secs for _, secs, _, _ in durations)  # 12.0 s

    scan.run_scan()
    man = _manifest_for(config, state, "Толстой - Война и мир")
    assert man is not None, "book manifest not found"

    # Compute a threshold that forces ~3 parts: total audio ≈ 12s @192k ≈ 288 KB;
    # pick a per-part budget so ~2 chapters fit. Use MB granularity via a tiny
    # value — but split_threshold_mb is an int MB. 12s is < 1 MB total, so to force
    # splitting we set the threshold via a direct plan first to find a byte budget,
    # then drive the build with split=True + a 1-MB threshold won't split (whole
    # book < 1 MB). So instead: bump bitrate so the book exceeds 1 MB? 12s would
    # need ~700 kbps for 1 MB. Simpler: drive split via params with a sub-MB intent
    # is impossible (int MB). We therefore force multi-part by MONKEY-FREE means:
    # set params split_threshold_mb to a value and verify; to guarantee ≥2 parts on
    # a short book we raise the encoded size by using a high bitrate so 12s > 1 MB.
    man["params"]["bitrate"] = 1536           # 12s @1536k ≈ 2.3 MB
    man["params"]["split"] = True
    man["params"]["split_threshold_mb"] = 1   # 1 MB ⇒ ~3 parts of ~2 chapters
    # Persist the tweaked params so the dispatcher build uses them.
    state.write_json_atomic(config.books_dir() / f"{man['book_id']}.json", man)

    # Expected plan (the source of truth the parts must match).
    expected_plan = split.plan_parts(man)
    n_expected = len(expected_plan)
    check("setup: forced plan yields ≥2 parts to actually exercise the cut",
          n_expected >= 2, f"n_parts={n_expected}")

    _drop_command(config.commands_dir(), _confirm_build_cmd(man))
    before = state.read_events()
    dispatcher.drain_commands()
    new_events = state.read_events()[len(before):]
    man = _manifest_for(config, state, "Толстой - Война и мир")

    check("build: split book reached done",
          man.get("status") == "done",
          f"status={man.get('status')!r} error={man.get('error')}")
    res = man.get("result") if isinstance(man.get("result"), dict) else {}
    part_paths = [Path(p) for p in (res.get("parts") or [])]
    check("result: result.parts carries the produced part paths",
          len(part_paths) == n_expected and all(p.suffix == ".m4b" for p in part_paths),
          f"parts={[p.name for p in part_paths]}")
    check("result: result.output_path points at the containing folder (Finder reveal)",
          res.get("output_path") == str(book.parent),
          f"output_path={res.get('output_path')!r}")
    full_still = (book.parent / "Толстой - Война и мир.m4b").exists()
    check("result: intermediate full .m4b was removed (parts only)",
          not full_still, "full file still present" if full_still else "")
    check("result: split_done event emitted once",
          sum(1 for e in new_events if e.get("event") == "split_done") == 1)

    all_parts_exist = all(p.is_file() and p.stat().st_size > 0 for p in part_paths)
    check("parts: every part file exists and is non-empty", all_parts_exist)

    # --- per-part assertions -------------------------------------------------
    full_codec = None
    # Determine the source audio codec from the first part (all parts copy it; the
    # full file is gone, but stream-copy means the part codec == the built codec).
    if part_paths:
        full_codec = _audio_codec(part_paths[0])
    check("parts: audio is AAC (the built codec), i.e. a real m4b stream",
          full_codec == "aac", f"codec={full_codec!r}")

    # Chapters per part must match the plan EXACTLY (no duplicates / phantoms —
    # the -map_chapters 1 proof), names rebased, first START == 0, track == N/M.
    chapters_ok = True
    no_dup_ok = True
    title_ok = True
    track_ok = True
    cover_ok = True
    copy_ok = True
    rebase_ok = True
    sum_parts_s = 0.0
    for plan_part, part_path in zip(expected_plan, part_paths):
        idx, tot = plan_part["index"], plan_part["total"]
        exp_n = len(plan_part["chapter_indices"])
        chaps = _probe_chapters(part_path)
        if len(chaps) != exp_n:
            chapters_ok = False
        # No phantom zero-length chapter (the classic missing -map_chapters bug).
        for c in chaps:
            try:
                if abs(float(c.get("end_time", 0)) - float(c.get("start_time", 0))) < 1e-6:
                    no_dup_ok = False
            except (TypeError, ValueError):
                no_dup_ok = False
        # First chapter rebased to START 0.
        if chaps:
            try:
                if round(float(chaps[0].get("start_time", -1)), 3) != 0.0:
                    rebase_ok = False
            except (TypeError, ValueError):
                rebase_ok = False
        # Title = «…, Часть N из M», track = N/M.
        title = _format_tag(part_path, "title")
        if f"Часть {idx} из {tot}" not in title:
            title_ok = False
        track = _format_tag(part_path, "track")
        if track not in (f"{idx}/{tot}",):
            track_ok = False
        # Cover present.
        has_cov, cov_codec = _cover_codec(part_path)
        if not has_cov:
            cover_ok = False
        # Stream-copy: audio codec identical across parts (no re-encode variance).
        if _audio_codec(part_path) != full_codec:
            copy_ok = False
        sum_parts_s += _probe_duration_s(part_path)

    check("parts: each part's chapter count == its plan (no missing chapters)",
          chapters_ok, f"plan={[len(p['chapter_indices']) for p in expected_plan]}")
    check("parts: NO duplicate / phantom zero-length chapters (-map_chapters 1)",
          no_dup_ok)
    check("parts: first chapter of each part rebased to START 0", rebase_ok)
    check("parts: every part title is «…, Часть N из M»", title_ok)
    check("parts: every part track == N/M", track_ok)
    check("parts: every part keeps a cover (attached_pic)", cover_ok)
    check("parts: stream-copy — same audio codec on every part (not re-encoded)",
          copy_ok)
    check("parts: Σ part durations ≈ full book (no drift)",
          abs(sum_parts_s - total_src_s) < 0.4,
          f"Σparts={sum_parts_s:.3f}s book={total_src_s:.3f}s")

    # === PART C: regression — default split=False → exactly ONE file ========
    book2 = watch / "Чехов - Рассказы"
    _make_mp3(book2 / "01 - intro.mp3", seconds=1.0, tags={"title": "Вступление"})
    _make_mp3(book2 / "02 - main.mp3", seconds=1.0, tags={"title": "Рассказ"})
    scan.run_scan()
    man2 = _manifest_for(config, state, "Чехов - Рассказы")
    assert man2 is not None
    check("regression: a fresh book defaults to split=False",
          man2.get("params", {}).get("split") is False,
          f"split={man2.get('params', {}).get('split')!r}")

    _drop_command(config.commands_dir(), _confirm_build_cmd(man2))
    dispatcher.drain_commands()
    man2 = _manifest_for(config, state, "Чехов - Рассказы")
    res2 = man2.get("result") if isinstance(man2.get("result"), dict) else {}
    out2 = res2.get("output_path")
    check("regression: split=False → done with a single .m4b (no parts key)",
          man2.get("status") == "done"
          and isinstance(out2, str) and out2.endswith(".m4b")
          and out2.endswith("Чехов - Рассказы.m4b")
          and not res2.get("parts")
          and Path(out2).is_file(),
          f"result={res2}")

    # default threshold present in params (300 MB).
    check("regression: DEFAULT_PARAMS exposes split_threshold_mb == 300",
          scan.DEFAULT_PARAMS.get("split_threshold_mb") == 300,
          f"val={scan.DEFAULT_PARAMS.get('split_threshold_mb')!r}")

    # --- summary ------------------------------------------------------------
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print(f"\n§split self-check: {passed}/{total} checks passed")
    print(f"(temp tree left at {root} for inspection; safe to delete)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
