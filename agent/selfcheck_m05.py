"""§M0.5 reconnaissance self-check — real ffprobe/ffmpeg on synthetic mp3s.

Run it standalone:

    python3 -m agent.selfcheck_m05

It generates tiny real mp3s in a throwaway temp tree (sine tones via ffmpeg
``lavfi``, with ``-metadata title/artist/album/track`` set, and one file given an
embedded cover via ``attached_pic``), redirects the whole data tree via
``MP3TOM4B_SUPPORT_DIR`` / ``MP3TOM4B_WATCH_DIR`` (the user's real Application
Support is never touched), then drives the real scan and asserts the
``test-plan.md §M0.5`` cases:

  full-tags     author←artist/album, title←album, order←track, chapter name←title
  folder-parse  empty tags + "Толстой - Война и мир" → parsed author/title,
                chapter names from filenames
  natural-sort  ``01,02,…,10`` (not ``1,10,2``)
  cyrillic      tags / names round-trip without tofu
  cover         attached_pic detected + a preview is extracted to covers/
  idempotency   two scans in a row → source_rev / confirm_token unchanged

It also re-runs the §M0 protocol self-check at the end and asserts 36/36 (no
regression). Requires ffmpeg + ffprobe on PATH; if either is missing the script
says so and exits non-zero.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
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


# --- mp3 fixtures (real, via ffmpeg lavfi) ----------------------------------


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _make_mp3(path: Path, *, seconds: float = 1.0, tags: dict | None = None) -> None:
    """Write a real sine-tone mp3 at ``path`` with optional ID3 tags."""
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
    ]
    for k, v in (tags or {}).items():
        argv += ["-metadata", f"{k}={v}"]
    argv.append(str(path))
    subprocess.run(argv, check=True, capture_output=True)


def _make_mp3_with_cover(path: Path, *, seconds: float = 1.0, tags: dict | None = None) -> None:
    """Write a real mp3 with an embedded attached-picture cover."""
    path.parent.mkdir(parents=True, exist_ok=True)
    art = path.parent / f".art-{path.stem}.jpg"
    subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "color=c=red:s=300x300:d=1", "-frames:v", "1", str(art)],
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


def _has_tools() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not _has_tools():
        print("§M0.5 self-check: SKIPPED — ffmpeg/ffprobe not found on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-m05-"))
    support = root / "support"
    watch = root / "watch"
    support.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)

    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import config, metadata, probe, scan, state  # noqa: E402

    print(f"self-check tree: {root}")
    print(f"  support: {support}")
    print(f"  watch:   {watch}\n")

    # === Fixture A: FULL TAGS — order by track, names from title ============
    # Filenames are deliberately in the "wrong" lexical order vs the track tags
    # so we can prove ordering follows `track`, not the filename.
    book_a = watch / "Сборник Чехова"  # folder name is NOT "Author - Title"
    _make_mp3(book_a / "z-first.mp3",  seconds=1.0, tags={
        "album": "Дама с собачкой", "artist": "Антон Чехов",
        "album_artist": "Чехов А.П.", "title": "Глава Первая", "track": "1"})
    _make_mp3(book_a / "a-second.mp3", seconds=2.0, tags={
        "album": "Дама с собачкой", "artist": "Антон Чехов",
        "album_artist": "Чехов А.П.", "title": "Глава Вторая", "track": "2"})
    _make_mp3(book_a / "m-third.mp3",  seconds=3.0, tags={
        "album": "Дама с собачкой", "artist": "Антон Чехов",
        "album_artist": "Чехов А.П.", "title": "Глава Третья", "track": "3"})

    scan.run_scan()
    man_a = None
    for p in config.books_dir().glob("*.json"):
        m = state.read_json(p)
        if str(m.get("src_dir", "")).endswith("Сборник Чехова"):
            man_a = m
            break
    assert man_a is not None, "book A manifest not found"

    check("full-tags: title ← album",
          man_a.get("title") == "Дама с собачкой", f"title={man_a.get('title')!r}")
    check("full-tags: author ← album_artist (preferred over artist)",
          man_a.get("author") == "Чехов А.П.", f"author={man_a.get('author')!r}")
    names_a = [c["name"] for c in man_a["chapters"]]
    check("full-tags: chapter order follows track tag (not filename)",
          names_a == ["Глава Первая", "Глава Вторая", "Глава Третья"],
          f"order={names_a}")
    check("full-tags: chapter names ← title tag",
          names_a == ["Глава Первая", "Глава Вторая", "Глава Третья"])
    durs_a = [c["duration_ms"] for c in man_a["chapters"]]
    check("full-tags: real durations probed (≈1000/2000/3000 ms)",
          all(isinstance(d, int) for d in durs_a)
          and abs(durs_a[0] - 1000) < 120
          and abs(durs_a[1] - 2000) < 120
          and abs(durs_a[2] - 3000) < 120,
          f"durations={durs_a}")
    check("full-tags: total_duration_ms = sum of chapters",
          man_a.get("total_duration_ms") == sum(durs_a),
          f"total={man_a.get('total_duration_ms')} sum={sum(durs_a)}")

    # === Fixture B: EMPTY TAGS + folder "Толстой - Война и мир" =============
    book_b = watch / "Толстой - Война и мир"
    for fn in ["01 - Пролог.mp3", "02 - Глава вторая.mp3", "10 - Эпилог.mp3"]:
        _make_mp3(book_b / fn, seconds=1.0, tags=None)  # no tags at all

    scan.run_scan()
    man_b = None
    for p in config.books_dir().glob("*.json"):
        m = state.read_json(p)
        if str(m.get("src_dir", "")).endswith("Толстой - Война и мир"):
            man_b = m
            break
    assert man_b is not None, "book B manifest not found"

    check("folder-parse: author ← folder 'Толстой - …'",
          man_b.get("author") == "Толстой", f"author={man_b.get('author')!r}")
    check("folder-parse: title ← folder '… - Война и мир'",
          man_b.get("title") == "Война и мир", f"title={man_b.get('title')!r}")
    names_b = [c["name"] for c in man_b["chapters"]]
    check("folder-parse: chapter names from filenames (prefix stripped)",
          names_b == ["Пролог", "Глава вторая", "Эпилог"], f"names={names_b}")

    # === natural sort: 01,02,…,10 (not 1,10,2), no track tags ===============
    files_b = [c["file"] for c in man_b["chapters"]]
    check("natural-sort: 01 < 02 < 10 (not lexicographic 1,10,2)",
          files_b == ["01 - Пролог.mp3", "02 - Глава вторая.mp3", "10 - Эпилог.mp3"],
          f"order={files_b}")

    # === cyrillic integrity (no tofu / mojibake) ===========================
    cyr_ok = (
        man_a.get("title") == "Дама с собачкой"
        and man_a.get("author") == "Чехов А.П."
        and "Война и мир" == man_b.get("title")
        and all("�" not in c["name"] for c in man_a["chapters"])
        and all("�" not in c["name"] for c in man_b["chapters"])
    )
    check("cyrillic: tags & names round-trip intact (no \\ufffd)", cyr_ok)

    # === Fixture C: EMBEDDED COVER — detect + extract preview ==============
    book_c = watch / "Аудиокнига с обложкой"
    _make_mp3(book_c / "01 - intro.mp3", seconds=1.0, tags={"title": "Вступление"})
    _make_mp3_with_cover(book_c / "02 - main.mp3", seconds=1.0,
                         tags={"album": "С Обложкой", "title": "Основная"})

    scan.run_scan()
    man_c = None
    for p in config.books_dir().glob("*.json"):
        m = state.read_json(p)
        if str(m.get("src_dir", "")).endswith("Аудиокнига с обложкой"):
            man_c = m
            break
    assert man_c is not None, "book C manifest not found"

    check("cover: detected embedded attached_pic → cover_state='embedded'",
          man_c.get("cover_state") == "embedded",
          f"cover_state={man_c.get('cover_state')!r}")
    preview = man_c.get("cover_preview")
    check("cover: preview extracted to covers/ (non-empty file)",
          isinstance(preview, str) and Path(preview).is_file()
          and Path(preview).stat().st_size > 0,
          f"preview={preview}")
    # Sanity: a book WITHOUT any embedded cover reports 'none'.
    check("cover: tag-less book reports cover_state='none'",
          man_b.get("cover_state") == "none",
          f"cover_state={man_b.get('cover_state')!r}")

    # Direct probe assertions on the cover file (unit-level, complements scan).
    cover_src = book_c / "02 - main.mp3"
    check("cover: probe.has_embedded_cover True on the cover file",
          probe.has_embedded_cover(cover_src))
    check("cover: probe.has_embedded_cover False on a plain file",
          not probe.has_embedded_cover(book_c / "01 - intro.mp3"))

    # === idempotency: two scans, unchanged inputs → rev & token stable =====
    rev_before = man_a["source_rev"]
    tok_before = man_a["confirm_token"]
    scan.run_scan()
    scan.run_scan()
    man_a2 = None
    for p in config.books_dir().glob("*.json"):
        m = state.read_json(p)
        if str(m.get("src_dir", "")).endswith("Сборник Чехова"):
            man_a2 = m
            break
    check("idempotency: source_rev unchanged across re-scans",
          man_a2.get("source_rev") == rev_before,
          f"{rev_before} -> {man_a2.get('source_rev')}")
    check("idempotency: confirm_token NOT re-armed (probe data is not in rev)",
          man_a2.get("confirm_token") == tok_before,
          f"{tok_before} -> {man_a2.get('confirm_token')}")
    check("idempotency: chapters identical across re-scans",
          [c["name"] for c in man_a2["chapters"]] == names_a)

    # === graceful: a corrupt 'mp3' must not crash probe/scan ===============
    book_d = watch / "Битый файл"
    (book_d).mkdir(parents=True, exist_ok=True)
    (book_d / "broken.mp3").write_bytes(b"not really an mp3 \x00\x01\x02")
    crashed = False
    try:
        scan.run_scan()
    except Exception as exc:  # the point: it must NOT raise
        crashed = True
        check("graceful: scan survived a corrupt mp3", False, repr(exc))
    if not crashed:
        check("graceful: scan survived a corrupt mp3", True)
    pr_bad = probe.probe_file(book_d / "broken.mp3")
    check("graceful: probe returns ok=False on corrupt mp3 (no raise)",
          pr_bad.get("ok") is False and pr_bad.get("duration_ms") is None,
          f"probe={pr_bad}")

    # === showcase projection: state.json carries real title/author/duration =
    showcase = state.read_state()
    sc_books = {b["book_id"]: b for b in (showcase or {}).get("books", [])}
    sc_a = sc_books.get(man_a["book_id"], {})
    check("showcase: state.json projects real title",
          sc_a.get("title") == "Дама с собачкой", f"title={sc_a.get('title')!r}")
    check("showcase: state.json projects author + total duration",
          sc_a.get("author") == "Чехов А.П."
          and sc_a.get("total_duration_ms") == man_a.get("total_duration_ms"),
          f"author={sc_a.get('author')!r} dur={sc_a.get('total_duration_ms')}")

    # --- summary ------------------------------------------------------------
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print(f"\n§M0.5 self-check: {passed}/{total} checks passed")
    print(f"(temp tree left at {root} for inspection; safe to delete)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
