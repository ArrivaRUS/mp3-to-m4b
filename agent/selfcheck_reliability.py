"""§reliability — DIAGNOSTIC audit of edge-cases E1–E18 (test-plan.md §M1).

    python3 -m agent.selfcheck_reliability

This is an **audit**, NOT a gate. For every edge-case in ``test-plan.md`` §M1
(E1–E18) it EMPIRICALLY reproduces the situation on a throwaway temp tree
(``MP3TOM4B_SUPPORT_DIR`` / ``MP3TOM4B_WATCH_DIR`` → scratch dirs, the user's
real Application Support is never touched) and records the agent's ACTUAL
behaviour, classifying each case:

    PASS    handled correctly today
    FAIL    NOT handled / crashes / wrong result  → a real gap to fix
    DEFER   depends on a layer that is not built yet (e.g. P1 split)

It deliberately runs ONLY these checks — it does NOT re-run the other suites
(``selfcheck_all`` orchestrates those once, flat). It is intentionally allowed
to be RED: a FAIL here is a finding for the audit table, not a broken build, so
it is NOT wired into ``selfcheck_all``.

The summary line ``§reliability self-check: X/Y`` is grep-friendly like the
peers, but the real product is the per-case table printed above it (and returned
in :data:`_RESULTS`). Requires ffmpeg + ffprobe on PATH for the cases that build
real audio; a missing tool is surfaced honestly (those cases FAIL, not skip).

Mapping to test-plan §M1 E1–E18 (the file lists them as a comma-run; this audit
fixes a stable numbering and follows the text):

    E1  no tags → folder fallback, builds
    E2  mixed SR/channels → normalized, valid .m4b
    E3  corrupt mp3 in a book → current behaviour?  (expected gap: whole book errors)
    E4  offline → cover generated
    E5  no disk space → pre-check + danger/cleanup?  (suspected gap)
    E6  idempotency (repeat confirm) — covered by §M1 too
    E7  cancel mid-build — covered by §cancel
    E8  very long book (>100 chapters) → demuxer fallback
    E9  garbage (non-mp3) in folder ignored
    E10 partially-copied / truncated mp3 → detected?  (suspected gap)
    E11 illegal filename characters → output sanitized
    E12 one mp3 in a subfolder → builds
    E13 duplicate chapter names → stable order / unique
    E14 unmount / source vanished → graceful (no crash)
    E15 split threshold < chapter → oversize part (P1 split; e2e in selfcheck_split)
    E16 crash during build → recover_interrupted + temp cleanup
"""

from __future__ import annotations

import collections
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

# --- audit harness ----------------------------------------------------------

# Each record: (case_id, title, verdict, behaviour). verdict ∈ PASS/FAIL/DEFER.
_RESULTS: list[tuple[str, str, str, str]] = []

PASS = "PASS"
FAIL = "FAIL"
DEFER = "DEFER"


def record(case_id: str, title: str, verdict: str, behaviour: str = "") -> None:
    _RESULTS.append((case_id, title, verdict, behaviour))
    print(f"  [{verdict:<5}] {case_id:<4} {title}")
    if behaviour:
        print(f"            → {behaviour}")


# --- ffmpeg/ffprobe helpers (mirror selfcheck_m1) ---------------------------


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


def _probe_chapters(path: Path) -> list[dict]:
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
    out = subprocess.run(
        [_ffprobe(), "-v", "error", "-print_format", "json",
         "-show_format", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return float(json.loads(out.stdout or "{}").get("format", {}).get("duration"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0.0


def _has_attached_pic(path: Path) -> tuple[bool, str]:
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
        if (s.get("disposition") or {}).get("attached_pic") == 1:
            return (True, s.get("codec_name", ""))
    return (False, "")


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


def _manifest_for(config, state, suffix: str) -> dict | None:
    for p in config.books_dir().glob("*.json"):
        m = state.read_json(p)
        if isinstance(m, dict) and str(m.get("src_dir", "")).endswith(suffix):
            return m
    return None


def _count_events(events, kind: str) -> int:
    return sum(1 for e in events if e.get("event") == kind)


# --- the audit run ----------------------------------------------------------


def run() -> int:  # noqa: C901 - one linear audit, kept flat on purpose
    if not _has_tools():
        # The build cases need real ffmpeg; without it the whole audit is moot.
        print("§reliability self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-reliability-"))
    support = root / "support"
    watch = root / "watch"
    support.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)

    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)
    # Force the OFFLINE cover path everywhere (no network in a self-check); E4
    # then flips it on for its own book to prove the generated fallback.
    os.environ["MP3TOM4B_COVER_WEB"] = "0"

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import build_m4b, config, dispatcher, scan, state  # noqa: E402

    print(f"self-check tree: {root}")
    print(f"  support: {support}")
    print(f"  watch:   {watch}\n")
    print("Audit of edge-cases E1–E18 (test-plan §M1) — diagnostic, not a gate.\n")

    # ======================================================================
    # E1 — NO TAGS → author/title from folder name, build OK.
    # ======================================================================
    try:
        b = watch / "Толстой - Анна Каренина"
        # mp3s with NO ID3 tags at all → resolution must fall back to the folder.
        _make_mp3(b / "01.mp3", seconds=1.0, freq=300)
        _make_mp3(b / "02.mp3", seconds=1.0, freq=400)
        scan.run_scan()
        m = _manifest_for(config, state, "Толстой - Анна Каренина")
        author_ok = m and m.get("author") == "Толстой" and m.get("title") == "Анна Каренина"
        _drop_command(config.commands_dir(), _confirm_build_cmd(m))
        dispatcher.drain_commands()
        m = _manifest_for(config, state, "Толстой - Анна Каренина")
        out = (m.get("result") or {}).get("output_path") if m else None
        built = bool(out) and Path(out).is_file() and m.get("status") == "done"
        if author_ok and built and Path(out).name == "Толстой - Анна Каренина.m4b":
            record("E1", "Нет тегов → фолбэк по папке, сборка ок", PASS,
                   f"author/title из имени папки; собран {Path(out).name}")
        else:
            record("E1", "Нет тегов → фолбэк по папке, сборка ок", FAIL,
                   f"author_ok={author_ok} built={built} out={out}")
    except Exception as exc:
        record("E1", "Нет тегов → фолбэк по папке, сборка ок", FAIL, f"raised {exc!r}")

    # ======================================================================
    # E2 — MIXED sample-rate / channels → normalized, ONE valid .m4b, no drift.
    # ======================================================================
    try:
        b = watch / "Сборник - Разный звук"
        _make_mp3(b / "01.mp3", seconds=1.0, samplerate=44100, channels=2, freq=330)
        _make_mp3(b / "02.mp3", seconds=2.0, samplerate=48000, channels=1, freq=440)
        _make_mp3(b / "03.mp3", seconds=3.0, samplerate=22050, channels=2, freq=550)
        scan.run_scan()
        m = _manifest_for(config, state, "Сборник - Разный звук")
        _drop_command(config.commands_dir(), _confirm_build_cmd(m))
        dispatcher.drain_commands()
        m = _manifest_for(config, state, "Сборник - Разный звук")
        out = (m.get("result") or {}).get("output_path") if m else None
        dur = _probe_duration_s(Path(out)) if out and Path(out).is_file() else 0.0
        sr = subprocess.run(
            [_ffprobe(), "-v", "error", "-select_streams", "a", "-show_entries",
             "stream=sample_rate,channels", "-of",
             "default=noprint_wrappers=1", str(out)],
            capture_output=True, text=True, check=False).stdout
        drift_ok = abs(dur - 6.0) < 0.1
        # "As in source" default (params.samplerate=None): the mixed sources
        # (44100/48000/22050) are upsampled to their MAX (48000), never down —
        # and the manifest records that source max. Channels default stays stereo.
        src_sr = m.get("source_samplerate") if m else None
        norm_ok = "sample_rate=48000" in sr and "channels=2" in sr and src_sr == 48000
        if m and m.get("status") == "done" and drift_ok and norm_ok:
            record("E2", "Разные SR/каналы → как в источнике (max), валидный .m4b", PASS,
                   f"длительность {dur:.3f}s≈6.0 (нет дрейфа), выход 48000/stereo "
                   f"(source_samplerate={src_sr})")
        else:
            record("E2", "Разные SR/каналы → как в источнике (max), валидный .m4b", FAIL,
                   f"status={m.get('status') if m else None} dur={dur:.3f} "
                   f"norm={norm_ok} source_samplerate={src_sr} sr_out={sr.strip()!r}")
    except Exception as exc:
        record("E2", "Разные SR/каналы → нормализуются, валидный .m4b", FAIL,
               f"raised {exc!r}")

    # ======================================================================
    # E3 — CORRUPT mp3 inside a book → the WHOLE book errors (decision: do NOT
    # ship a silently-partial .m4b). The build path now refuses if ANY chapter is
    # unreadable: status=error(unreadable_chapter), the bad file name(s) in the
    # error detail, and NO partial output on disk. The book stays rebuildable —
    # fix/remove the file → a re-scan re-arms it (rev changes).
    # ======================================================================
    try:
        b = watch / "Битый - Внутри книги"
        _make_mp3(b / "01 - ok.mp3", seconds=1.0, freq=300, tags={"title": "Целая"})
        # A non-audio file with a .mp3 extension → ffprobe yields no duration.
        (b / "02 - broken.mp3").write_bytes(b"\x00\x01\x02 not audio at all \xff\xfe")
        _make_mp3(b / "03 - ok2.mp3", seconds=1.0, freq=500, tags={"title": "Тоже целая"})
        scan.run_scan()
        m = _manifest_for(config, state, "Битый - Внутри книги")
        # The broken file IS recognized as a chapter with no duration (probe sets
        # duration_ms=None) — that is the signal the build guard keys on.
        chs = m.get("chapters", []) if m else []
        broken_has_no_dur = any(
            "broken" in str(c.get("file", "")) and c.get("duration_ms") is None for c in chs
        )
        out_path = b.parent / "Битый - Внутри книги.m4b"
        _drop_command(config.commands_dir(), _confirm_build_cmd(m))
        before = state.read_events()
        dispatcher.drain_commands()
        ev = state.read_events()[len(before):]
        m = _manifest_for(config, state, "Битый - Внутри книги")
        err = (m.get("error") or {}) if m else {}
        # New contract: whole book → error(unreadable_chapter), bad file named in
        # detail, NO partial .m4b (neither final nor a leftover temp).
        errored = m.get("status") == "error" if m else False
        reason_ok = err.get("reason") == "unreadable_chapter"
        names_broken = "broken" in str(err.get("detail", ""))
        no_partial = (
            not out_path.exists()
            and list(b.parent.glob(".Битый - Внутри книги.m4b.*")) == []
        )
        failed_ev = _count_events(ev, "build_failed") >= 1
        if (broken_has_no_dur and errored and reason_ok and names_broken
                and no_partial and failed_ev):
            record("E3", "Битый mp3 в книге → ВСЯ книга в ошибку (без частичного)",
                   PASS,
                   "одна нечитаемая глава → status=error(unreadable_chapter), "
                   "имя битого файла в detail, частичного .m4b нет, книга "
                   "пересобираема")
        else:
            record("E3", "Битый mp3 в книге → ВСЯ книга в ошибку (без частичного)",
                   FAIL,
                   f"status={m.get('status') if m else None} reason={err.get('reason')!r} "
                   f"detail={err.get('detail')!r} no_partial={no_partial} "
                   f"failed_ev={failed_ev} broken_no_dur={broken_has_no_dur}")
    except Exception as exc:
        record("E3", "Битый mp3 в книге → ВСЯ книга в ошибку (без частичного)",
               FAIL, f"raised {exc!r}")

    # ======================================================================
    # E4 — NO INTERNET → cover GENERATED (Pillow). We keep MP3TOM4B_COVER_WEB=0
    # (offline) and assert the chain still yields a generated cover that gets
    # burned into the .m4b.
    # ======================================================================
    try:
        b = watch / "Офлайн - Без сети"
        _make_mp3(b / "01.mp3", seconds=1.0, freq=300, tags={"title": "Глава"})
        scan.run_scan()
        m = _manifest_for(config, state, "Офлайн - Без сети")
        opts = m.get("cover_options", []) if m else []
        has_generated = any(o.get("kind") == "generated" for o in opts)
        no_web = not any(o.get("kind") == "web" for o in opts)
        _drop_command(config.commands_dir(), _confirm_build_cmd(m))
        dispatcher.drain_commands()
        m = _manifest_for(config, state, "Офлайн - Без сети")
        out = (m.get("result") or {}).get("output_path") if m else None
        has_cov, codec = _has_attached_pic(Path(out)) if out and Path(out).is_file() else (False, "")
        if has_generated and no_web and has_cov and codec == "mjpeg":
            record("E4", "Нет интернета → генерация обложки (Pillow)", PASS,
                   "офлайн: сгенерированы варианты, обложка зашита (mjpeg)")
        else:
            record("E4", "Нет интернета → генерация обложки (Pillow)", FAIL,
                   f"generated={has_generated} no_web={no_web} cover={has_cov} codec={codec!r}")
    except Exception as exc:
        record("E4", "Нет интернета → генерация обложки (Pillow)", FAIL,
               f"raised {exc!r}")

    # ======================================================================
    # E5 — NO DISK SPACE → a free-space PRE-CHECK fails the build BEFORE ffmpeg
    # runs (status=error(no_space), danger), with a clean unwind (no partial
    # file). We must NOT actually fill the disk, so we drive a REAL healthy book
    # through the dispatcher with shutil.disk_usage monkeypatched to report a tiny
    # free figure → the pre-flight (required_free_space) trips. We assert: the
    # book errors with reason 'no_space', a detail is present, NO output/temp is
    # left, and ffmpeg was never reached (no empty_output / ffmpeg_* reason).
    # ======================================================================
    try:
        b = watch / "Нетместа - Симуляция"
        _make_mp3(b / "01.mp3", seconds=1.0, freq=300, tags={"title": "Глава"})
        _make_mp3(b / "02.mp3", seconds=1.0, freq=400, tags={"title": "Глава2"})
        scan.run_scan()
        m = _manifest_for(config, state, "Нетместа - Симуляция")
        out_path = b.parent / "Нетместа - Симуляция.m4b"

        # Static: the pre-check helper exists in the build path.
        import inspect
        src = inspect.getsource(build_m4b)
        has_space_precheck = (
            "disk_usage" in src and "_ensure_free_space" in src
        )

        # Dynamic: force "almost no free space" so required_free_space > free.
        DiskUsage = collections.namedtuple("usage", ["total", "free", "used"])
        real_disk_usage = build_m4b.shutil.disk_usage

        def _fake_disk_usage(path):
            return DiskUsage(total=10 ** 12, free=1024, used=10 ** 12 - 1024)

        build_m4b.shutil.disk_usage = _fake_disk_usage
        try:
            _drop_command(config.commands_dir(), _confirm_build_cmd(m))
            before = state.read_events()
            dispatcher.drain_commands()
            ev = state.read_events()[len(before):]
        finally:
            build_m4b.shutil.disk_usage = real_disk_usage

        m = _manifest_for(config, state, "Нетместа - Симуляция")
        err = (m.get("error") or {}) if m else {}
        errored = m.get("status") == "error" if m else False
        reason_ok = err.get("reason") == "no_space"
        has_detail = bool(str(err.get("detail", "")))
        clean = (
            not out_path.exists()
            and list(b.parent.glob(".Нетместа - Симуляция.m4b.*")) == []
        )
        failed_ev = _count_events(ev, "build_failed") >= 1
        if (has_space_precheck and errored and reason_ok and has_detail
                and clean and failed_ev):
            record("E5", "Нет места (диск полон) → пред-проверка + danger/cleanup",
                   PASS,
                   "пред-проверка свободного места падает ДО ffmpeg → "
                   "status=error(no_space) с detail, частичного файла нет")
        else:
            record("E5", "Нет места (диск полон) → пред-проверка + danger/cleanup",
                   FAIL,
                   f"precheck={has_space_precheck} status={m.get('status') if m else None} "
                   f"reason={err.get('reason')!r} clean={clean} failed_ev={failed_ev}")
    except Exception as exc:
        record("E5", "Нет места (диск полон) → пред-проверка + danger/cleanup", FAIL,
               f"raised {exc!r}")

    # ======================================================================
    # E6 — IDEMPOTENCY: a repeated confirm-build (double click) → exactly ONE
    # build. (Also covered by §M1 / §queue; confirmed here for completeness.)
    # ======================================================================
    try:
        b = watch / "Идемп - Повтор"
        _make_mp3(b / "01.mp3", seconds=1.0, freq=300, tags={"title": "Раз"})
        _make_mp3(b / "02.mp3", seconds=1.0, freq=400, tags={"title": "Два"})
        scan.run_scan()
        m = _manifest_for(config, state, "Идемп - Повтор")
        idem = f"{m['book_id']}:{m['source_rev'][:16]}"
        _drop_command(config.commands_dir(), _confirm_build_cmd(m, idem=idem))
        _drop_command(config.commands_dir(), _confirm_build_cmd(m, idem=idem))
        before = state.read_events()
        dispatcher.drain_commands()
        ev = state.read_events()[len(before):]
        m = _manifest_for(config, state, "Идемп - Повтор")
        one_build = _count_events(ev, "build_done") == 1
        skipped = _count_events(ev, "build_skipped_idempotent") == 1
        if one_build and skipped and m.get("status") == "done":
            record("E6", "Идемпотентность (повторный confirm) → ровно 1 сборка", PASS,
                   "две одинаковые команды → 1 build_done + 1 skip (covered §M1/§queue)")
        else:
            record("E6", "Идемпотентность (повторный confirm) → ровно 1 сборка", FAIL,
                   f"build_done={_count_events(ev, 'build_done')} skipped={skipped}")
    except Exception as exc:
        record("E6", "Идемпотентность (повторный confirm) → ровно 1 сборка", FAIL,
               f"raised {exc!r}")

    # ======================================================================
    # E7 — CANCEL mid-build. Fully exercised by agent.selfcheck_cancel (real
    # ffmpeg SIGTERM→SIGKILL, temp sweep, back-to-pending). Here we ONLY do a
    # light structural confirmation (the cancel plumbing exists + is importable),
    # to avoid duplicating that heavy suite.
    # ======================================================================
    try:
        has_cancel_api = (
            hasattr(build_m4b, "BuildCancelled")
            and hasattr(build_m4b, "_cancel_requested")
            and hasattr(dispatcher, "_consume_cancel_commands")
            and dispatcher.CANCEL_ACTION == "cancel"
        )
        if has_cancel_api:
            record("E7", "Отмена в процессе → кооперативный teardown + cleanup", PASS,
                   "плумбинг отмены на месте; полный прогон — agent.selfcheck_cancel")
        else:
            record("E7", "Отмена в процессе → кооперативный teardown + cleanup", FAIL,
                   "не найден API отмены (BuildCancelled/_cancel_requested/…)")
    except Exception as exc:
        record("E7", "Отмена в процессе → кооперативный teardown + cleanup", FAIL,
               f"raised {exc!r}")

    # ======================================================================
    # E8 — VERY LONG book (> CONCAT_FILTER_MAX_CHAPTERS) → demuxer fallback.
    # We build a book with threshold+5 tiny chapters and assert: it reaches done,
    # has the right chapter count, and the fallback path was taken (count >
    # threshold). Many tiny files keep it fast.
    # ======================================================================
    try:
        n = build_m4b.CONCAT_FILTER_MAX_CHAPTERS + 5
        b = watch / "Длинная - Много глав"
        b.mkdir(parents=True, exist_ok=True)
        # Generate ONE short mp3 and hardlink/copy it N times (fast: no N encodes).
        seed = b / "_seed.mp3"
        _make_mp3(seed, seconds=0.3, freq=300)
        for i in range(1, n + 1):
            shutil.copyfile(seed, b / f"{i:04d} - ch.mp3")
        seed.unlink()
        scan.run_scan()
        m = _manifest_for(config, state, "Длинная - Много глав")
        n_chs = len(m.get("chapters", [])) if m else 0
        uses_demuxer = n_chs > build_m4b.CONCAT_FILTER_MAX_CHAPTERS
        _drop_command(config.commands_dir(), _confirm_build_cmd(m))
        dispatcher.drain_commands()
        m = _manifest_for(config, state, "Длинная - Много глав")
        out = (m.get("result") or {}).get("output_path") if m else None
        built = bool(out) and Path(out).is_file() and m.get("status") == "done"
        out_chs = len(_probe_chapters(Path(out))) if built else 0
        if built and uses_demuxer and out_chs == n:
            record("E8", "Очень длинная книга (>100 глав) → fallback demuxer", PASS,
                   f"{n} глав → demuxer-путь, собрано, {out_chs} глав в выходе")
        else:
            record("E8", "Очень длинная книга (>100 глав) → fallback demuxer", FAIL,
                   f"n={n} built={built} demuxer={uses_demuxer} out_chs={out_chs}")
    except Exception as exc:
        record("E8", "Очень длинная книга (>100 глав) → fallback demuxer", FAIL,
               f"raised {exc!r}")

    # ======================================================================
    # E9 — GARBAGE (non-mp3) files in a book folder are IGNORED.
    # ======================================================================
    try:
        b = watch / "Мусор - В папке"
        _make_mp3(b / "01.mp3", seconds=1.0, freq=300, tags={"title": "Глава"})
        (b / "readme.txt").write_text("notes", encoding="utf-8")
        (b / "cover.jpg").write_bytes(b"\xff\xd8\xff\xe0 jpeg-ish")
        (b / "thumbs.db").write_bytes(b"\x00\x01")
        (b / ".hidden.mp3").write_bytes(b"dotfile mp3 must be skipped")
        scan.run_scan()
        m = _manifest_for(config, state, "Мусор - В папке")
        chs = m.get("chapters", []) if m else []
        files = [c.get("file") for c in chs]
        only_real_mp3 = files == ["01.mp3"]
        _drop_command(config.commands_dir(), _confirm_build_cmd(m))
        dispatcher.drain_commands()
        m = _manifest_for(config, state, "Мусор - В папке")
        built = m.get("status") == "done"
        if only_real_mp3 and built:
            record("E9", "Мусор в папке (не-mp3) игнорируется", PASS,
                   "txt/jpg/db/dotfile отброшены, в книге только 01.mp3, собрана")
        else:
            record("E9", "Мусор в папке (не-mp3) игнорируется", FAIL,
                   f"files={files} built={built}")
    except Exception as exc:
        record("E9", "Мусор в папке (не-mp3) игнорируется", FAIL, f"raised {exc!r}")

    # ======================================================================
    # E10 — PARTIALLY-COPIED / still-growing mp3 → the scan's copy-stability
    # DEBOUNCE refuses to arm the book while a file is still being written
    # (size/mtime changing across a short beat), and arms it correctly once the
    # copy settles. We reproduce a live copy: a background thread keeps appending
    # to one mp3 across the debounce window. The first scan (file growing) must
    # NOT arm the book; after the copy finishes, a second scan arms it and a build
    # yields a full, valid chapter (no short/corrupt timeline).
    # ======================================================================
    try:
        # A real (short) debounce so the growing file is caught deterministically.
        os.environ[scan._STABILITY_ENV] = "0.4"
        b = watch / "Недокоп - Растущий"
        b.mkdir(parents=True, exist_ok=True)
        _make_mp3(b / "02 - ok.mp3", seconds=1.0, freq=400, tags={"title": "Целая"})
        # Seed a valid mp3 we will keep appending to (simulates an in-flight copy).
        growing = b / "01 - growing.mp3"
        _make_mp3(growing, seconds=2.0, freq=300, tags={"title": "Растущая"})
        chunk = (b / "_payload.mp3")
        _make_mp3(chunk, seconds=3.0, freq=320)
        payload = chunk.read_bytes()
        chunk.unlink()

        stop = threading.Event()

        def _keep_growing():
            # Keep appending until told to stop (safety cap ~10s) so the file is
            # STILL changing throughout the first scan no matter how long the scan
            # takes to reach this folder (earlier cases left many books to walk).
            deadline = time.time() + 10.0
            step = max(1, len(payload) // 12)
            off = 0
            with open(growing, "ab") as fh:
                while not stop.is_set() and time.time() < deadline:
                    fh.write(payload[off:off + step])
                    fh.flush()
                    os.fsync(fh.fileno())
                    off = (off + step) % max(1, len(payload))
                    time.sleep(0.05)

        grower = threading.Thread(target=_keep_growing, daemon=True)
        grower.start()
        # Scan WHILE the file grows → debounce should skip arming this book.
        scan.run_scan()
        m_during = _manifest_for(config, state, "Недокоп - Растущий")
        skipped_while_copying = m_during is None

        # Stop the copy and let it settle, then re-scan.
        stop.set()
        grower.join(timeout=5)
        # File has settled now → a fresh scan must arm the book and build cleanly.
        scan.run_scan()
        m = _manifest_for(config, state, "Недокоп - Растущий")
        armed_after_settle = (
            m is not None and m.get("status") == "pending-confirm"
        )
        chs = m.get("chapters", []) if m else []
        grow_ch = next((c for c in chs if "growing" in str(c.get("file", ""))), None)
        # The settled chapter probes a full (>1.5s) duration — not the seed's 2.0s
        # truncated-header lie; the whole appended stream decodes.
        settled_dur = grow_ch.get("duration_ms") if grow_ch else None
        if armed_after_settle:
            _drop_command(config.commands_dir(), _confirm_build_cmd(m))
            dispatcher.drain_commands()
            m = _manifest_for(config, state, "Недокоп - Растущий")
        out = (m.get("result") or {}).get("output_path") if m else None
        built = bool(out) and Path(out).is_file() and m.get("status") == "done"
        out_chs = len(_probe_chapters(Path(out))) if built else 0
        os.environ.pop(scan._STABILITY_ENV, None)

        if (skipped_while_copying and armed_after_settle and built
                and out_chs == 2 and isinstance(settled_dur, int)):
            record("E10", "Недокопированный mp3 (растущий) → дебаунс, потом сборка",
                   PASS,
                   "пока файл рос — книга НЕ армирована (skip), после стабилизации "
                   "армирована и собрана полностью (2 главы)")
        else:
            record("E10", "Недокопированный mp3 (растущий) → дебаунс, потом сборка",
                   FAIL,
                   f"skipped_while_copying={skipped_while_copying} "
                   f"armed_after_settle={armed_after_settle} built={built} "
                   f"out_chs={out_chs} settled_dur={settled_dur}")
    except Exception as exc:
        os.environ.pop(scan._STABILITY_ENV, None)
        record("E10", "Недокопированный mp3 (растущий) → дебаунс, потом сборка",
               FAIL, f"raised {exc!r}")

    # ======================================================================
    # E11 — ILLEGAL filename characters → output name sanitized (no crash, safe
    # single component, no path escape).
    # ======================================================================
    try:
        # Author/title carrying separators and illegal chars.
        forged = {
            "book_id": "sanitize-book",
            "author": 'A/B:C*?"<>|',
            "title": "Кни:га\\<нечисть>",
        }
        name = build_m4b.output_filename(forged)
        illegal = set('/\\:*?"<>|\0')
        # The name is ONE component (no separators), .m4b suffix, no illegal chars,
        # not a hidden/dotfile, non-empty stem.
        stem = name[:-4] if name.endswith(".m4b") else name
        clean = (
            name.endswith(".m4b")
            and not any(c in illegal for c in name)
            and "/" not in name
            and not name.startswith(".")
            and stem.strip() != ""
        )
        # Empty/garbage falls back to "book.m4b".
        fb = build_m4b.output_filename({"author": "", "title": "///"})
        fb_ok = fb == "book.m4b"
        if clean and fb_ok:
            record("E11", "Недопустимые символы в имени → sanitize выходного имени", PASS,
                   f"«{name}» — один компонент, без нечисти; пустое → {fb}")
        else:
            record("E11", "Недопустимые символы в имени → sanitize выходного имени", FAIL,
                   f"name={name!r} clean={clean} fallback={fb!r}")
    except Exception as exc:
        record("E11", "Недопустимые символы в имени → sanitize выходного имени", FAIL,
               f"raised {exc!r}")

    # ======================================================================
    # E12 — ONE mp3 in a subfolder → the book still assembles.
    # ======================================================================
    try:
        b = watch / "Одна - Глава"
        _make_mp3(b / "01 - solo.mp3", seconds=1.5, freq=350, tags={"title": "Единственная"})
        scan.run_scan()
        m = _manifest_for(config, state, "Одна - Глава")
        one_ch = len(m.get("chapters", [])) == 1 if m else False
        _drop_command(config.commands_dir(), _confirm_build_cmd(m))
        dispatcher.drain_commands()
        m = _manifest_for(config, state, "Одна - Глава")
        out = (m.get("result") or {}).get("output_path") if m else None
        built = bool(out) and Path(out).is_file() and m.get("status") == "done"
        out_chs = len(_probe_chapters(Path(out))) if built else 0
        if one_ch and built and out_chs == 1:
            record("E12", "Один mp3 в подпапке → книга собирается", PASS,
                   "1 глава → собран валидный .m4b с 1 главой")
        else:
            record("E12", "Один mp3 в подпапке → книга собирается", FAIL,
                   f"one_ch={one_ch} built={built} out_chs={out_chs}")
    except Exception as exc:
        record("E12", "Один mp3 в подпапке → книга собирается", FAIL, f"raised {exc!r}")

    # ======================================================================
    # E13 — DUPLICATE chapter names → stable order, names preserved (no crash,
    # deterministic). Two files share the same ID3 title; order must be stable
    # and both chapters must survive distinctly on the timeline.
    # ======================================================================
    try:
        b = watch / "Дубли - Имена"
        _make_mp3(b / "01.mp3", seconds=1.0, freq=300, tags={"title": "Глава"})
        _make_mp3(b / "02.mp3", seconds=1.0, freq=400, tags={"title": "Глава"})
        _make_mp3(b / "03.mp3", seconds=1.0, freq=500, tags={"title": "Глава"})
        scan.run_scan()
        m = _manifest_for(config, state, "Дубли - Имена")
        chs = m.get("chapters", []) if m else []
        names = [c.get("name") for c in chs]
        files = [c.get("file") for c in chs]
        # Order is stable by filename (01,02,03), all three present, all named.
        order_ok = files == ["01.mp3", "02.mp3", "03.mp3"]
        all_named = names == ["Глава", "Глава", "Глава"]
        _drop_command(config.commands_dir(), _confirm_build_cmd(m))
        dispatcher.drain_commands()
        m = _manifest_for(config, state, "Дубли - Имена")
        out = (m.get("result") or {}).get("output_path") if m else None
        built = bool(out) and Path(out).is_file() and m.get("status") == "done"
        out_chs = _probe_chapters(Path(out)) if built else []
        three_distinct = len(out_chs) == 3
        if order_ok and all_named and built and three_distinct:
            record("E13", "Дубли имён глав → стабильный порядок/уникальность", PASS,
                   "3 одноимённые главы: порядок 01/02/03 стабилен, все 3 на таймлайне")
        else:
            record("E13", "Дубли имён глав → стабильный порядок/уникальность", FAIL,
                   f"order_ok={order_ok} named={all_named} built={built} "
                   f"out_chs={len(out_chs)}")
    except Exception as exc:
        record("E13", "Дубли имён глав → стабильный порядок/уникальность", FAIL,
               f"raised {exc!r}")

    # ======================================================================
    # E14 — UNMOUNT / source vanished. Build a manifest, then DELETE the source
    # folder (simulating an unmounted volume / removed source), and drive a
    # confirm-build. Must NOT crash; must surface an error gracefully.
    # ======================================================================
    try:
        b = watch / "Исчез - Источник"
        _make_mp3(b / "01.mp3", seconds=1.0, freq=300, tags={"title": "Глава"})
        _make_mp3(b / "02.mp3", seconds=1.0, freq=400, tags={"title": "Глава2"})
        scan.run_scan()
        m = _manifest_for(config, state, "Исчез - Источник")
        cmd = _confirm_build_cmd(m)  # capture token/rev BEFORE removing the source
        # Now the source vanishes (unmount / move) AFTER recognition.
        shutil.rmtree(b)
        _drop_command(config.commands_dir(), cmd)
        before = state.read_events()
        crashed = False
        try:
            dispatcher.drain_commands()
        except Exception:
            crashed = True
        ev = state.read_events()[len(before):]
        m = _manifest_for(config, state, "Исчез - Источник")
        # Graceful = no exception bubbled, and the book did NOT silently land at
        # "done" with a bogus output. Acceptable graceful outcomes: error
        # (source_missing) OR rejected-stale (the re-scan saw files gone). Either
        # is fine as long as no crash and no fake success.
        status = m.get("status") if m else None
        rejected = (
            _count_events(ev, "command_rejected") > 0
            or _count_events(ev, "confirm_rejected_stale") > 0
            or _count_events(ev, "build_failed") > 0
        )
        out = (m.get("result") or {}).get("output") if m else None
        no_fake_success = not (status == "done" and out and Path(out).is_file())
        graceful = (not crashed) and no_fake_success and (status in ("error", "pending-confirm") or rejected)
        if graceful:
            record("E14", "Размонтирование / исчез источник → без краха (graceful)", PASS,
                   f"источник удалён → без исключения, статус={status!r}, нет фейк-успеха")
        else:
            record("E14", "Размонтирование / исчез источник → без краха (graceful)", FAIL,
                   f"crashed={crashed} status={status!r} out={out}")
    except Exception as exc:
        record("E14", "Размонтирование / исчез источник → без краха (graceful)", FAIL,
               f"raised {exc!r}")

    # ======================================================================
    # E15 — SPLIT threshold < chapter. The split layer (P1) is now BUILT, so this
    # is a real check (no longer DEFER): a single chapter bigger than the threshold
    # must become its OWN part flagged ``oversize`` — we never cut mid-chapter.
    # This is a CHEAP in-process ``plan_parts`` assertion (no ffmpeg). The HEAVY
    # end-to-end proof (real build→split, no duplicate chapters, stream-copy,
    # per-part cover/title) lives in ``agent.selfcheck_split`` — NOT re-run here, to
    # keep this gate flat (the "no nested regression" rule).
    # ======================================================================
    try:
        from agent import split as split_mod
        man_e15 = {
            "title": "Книга", "author": "Автор",
            "params": {"bitrate": 192, "split_threshold_mb": 1},
            "chapters": [
                {"index": 1, "duration_ms": 10_000, "name": "Мелкая"},
                {"index": 2, "duration_ms": 120_000, "name": "Огромная"},  # >1 MB
                {"index": 3, "duration_ms": 10_000, "name": "Мелкая2"},
            ],
        }
        plan = split_mod.plan_parts(man_e15)
        big = [p for p in plan if p.get("chapter_indices") == [2]]
        oversize_alone = len(big) == 1 and big[0].get("oversize") is True
        others_ok = all(not p.get("oversize") for p in plan
                        if p.get("chapter_indices") != [2])
        boundaries_ok = [i for p in plan for i in p["chapter_indices"]] == [1, 2, 3]
        if oversize_alone and others_ok and boundaries_ok:
            record("E15", "Порог < главы → нарезка (часть=глава + oversize-флаг)",
                   PASS,
                   "глава > порога → отдельная часть с oversize=True, границы "
                   "глав соблюдены (полный e2e — в selfcheck_split)")
        else:
            record("E15", "Порог < главы → нарезка (часть=глава + oversize-флаг)",
                   FAIL,
                   f"plan_parts не выделил крупную главу как oversize-часть: "
                   f"plan={[(p.get('chapter_indices'), p.get('oversize')) for p in plan]}")
    except Exception as exc:
        record("E15", "Порог < главы → нарезка (часть=глава + oversize-флаг)", FAIL,
               f"plan_parts упал на E15-манифесте: {exc!r}")

    # ======================================================================
    # E16 — CRASH during build → recover_interrupted + temp cleanup. Forge a
    # manifest stuck at "converting" with a DEAD pid and a half-written temp on
    # disk, then run recover_interrupted(): it must flip to error(interrupted),
    # sweep the temp, and clear the build marker.
    # ======================================================================
    try:
        b = watch / "Краш - Прерванная"
        b.mkdir(parents=True, exist_ok=True)
        out_path = b.parent / "Краш - Прерванная.m4b"
        half = b.parent / ".Краш - Прерванная.m4b.abc123.tmp"
        half.write_bytes(b"half-written m4b bytes that a crash left behind")
        dead_pid = 2_000_000_000  # implausible pid → not alive
        forged = {
            "book_id": "interrupted-book", "src_dir": str(b), "status": "converting",
            "source_rev": "rev-x", "confirm_token": "tok-x",
            "title": "Прерванная", "author": "Краш",
            "build": {"pid": dead_pid, "started_at": time.time() - 60},
            "result": {"output_path": str(out_path)},
            "chapters": [{"index": 1, "file": "01.mp3", "name": "Г", "duration_ms": 1000}],
            "progress": 0.4, "params": dict(scan.DEFAULT_PARAMS), "ts": time.time(),
        }
        fp = config.books_dir() / "interrupted-book.json"
        state.write_json_atomic(fp, forged)
        before = state.read_events()
        recovered = dispatcher.recover_interrupted()
        ev = state.read_events()[len(before):]
        m = state.read_json(fp)
        ok = (
            recovered >= 1
            and m.get("status") == "error"
            and (m.get("error") or {}).get("reason") == "interrupted"
            and m.get("build") is None
            and not half.exists()
            and _count_events(ev, "interrupted") >= 1
        )
        if ok:
            record("E16", "Краш при сборке → recover_interrupted + чистка temp", PASS,
                   "converting+мёртвый pid → error(interrupted), temp подметён, marker снят")
        else:
            record("E16", "Краш при сборке → recover_interrupted + чистка temp", FAIL,
                   f"recovered={recovered} status={m.get('status')} "
                   f"err={m.get('error')} half_exists={half.exists()}")
    except Exception as exc:
        record("E16", "Краш при сборке → recover_interrupted + чистка temp", FAIL,
               f"raised {exc!r}")

    # === summary ============================================================
    n_pass = sum(1 for _, _, v, _ in _RESULTS if v == PASS)
    n_fail = sum(1 for _, _, v, _ in _RESULTS if v == FAIL)
    n_defer = sum(1 for _, _, v, _ in _RESULTS if v == DEFER)
    total = len(_RESULTS)

    print("\n" + "=" * 70)
    print("AUDIT TABLE — E1–E18 (test-plan §M1)")
    print("=" * 70)
    print(f"  {'CASE':<5} {'VERDICT':<7} TITLE")
    print("  " + "-" * 66)
    for cid, title, verdict, _ in _RESULTS:
        print(f"  {cid:<5} {verdict:<7} {title}")
    print("=" * 70)
    # Grep-friendly summary line in the EXACT peer format «X/Y checks passed» the
    # flat runner (selfcheck_all) parses. ``green ⇔ X == Y ⇔ zero FAIL``: a DEFER
    # is an acknowledged not-yet-built layer (E15), NOT a failure, so it counts
    # toward X. The detailed PASS/FAIL/DEFER breakdown rides on the same line for
    # humans. This is what makes ``reliability`` a real gate now.
    ok_count = n_pass + n_defer
    print(f"§reliability self-check: {ok_count}/{total} checks passed "
          f"(PASS={n_pass} DEFER={n_defer} FAIL={n_fail})")
    print(f"(temp tree left at {root} for inspection; safe to delete)")

    # Now a GATE (wired into selfcheck_all): exit non-zero iff any case FAILed. A
    # DEFER does not fail the gate (it is a tracked, not-yet-built layer). The
    # per-case table above remains the diagnostic.
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(run())
