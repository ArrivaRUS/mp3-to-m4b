"""§cover-pick self-check — the SELECTED cover travels app→command→agent→build.

Run it standalone:

    python3 -m agent.selfcheck_cover_pick

The engine layer (:mod:`agent.selfcheck_cover`) already proves the cover *chain*
(generate / web / resolve). THIS check closes the loop the picker added: that the
cover the user chose in the confirm window — carried in ``confirm-build``'s
``params`` (``cover_id`` / ``cover_custom_path``, NOT a separate command) — is the
one actually burned into the ``.m4b``, not the default/embedded one.

It drives the PRODUCTION path (scan → manifest → emulate a confirm-build command →
:func:`dispatcher.drain_commands` → real ffmpeg) and then PROVES the embedded cover
by content:

  pick-generated   a book with NO embedded cover + offline → options are
                   generated-only (default = generated-0). We confirm-build with
                   ``cover_id = generated-2`` (NOT the default) and prove the
                   attached_pic in the output matches the generated-2 SOURCE image
                   (closest by a downscaled-pixel signature), and is FARTHER from
                   generated-0 — i.e. the chosen, not the default, cover is embedded.
  pick-custom      confirm-build with ``cover_custom_path`` = a freshly drawn solid
                   image → the agent copies it under covers/ and the attached_pic
                   matches THAT image (its solid color), proving a user-replaced
                   cover wins. Also asserts the app did NOT write covers/ (the agent
                   copied it: the dest path differs from the source).
  guarantee        a book with no embedded + no web still builds WITH a cover
                   (attached_pic present, mjpeg) — PRD G4, even offline.

It runs ONLY its own checks (cross-suite regression is orchestrated once by
``agent.selfcheck_all`` — there is no nested re-run here). Pillow + ffmpeg/ffprobe
are required (real build + pixel comparison); the run SKIPS (rc 1) if either is
missing.
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


# --- tool plumbing ----------------------------------------------------------


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe() -> str:
    return shutil.which("ffprobe") or "ffprobe"


def _has_tools() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _make_mp3(path: Path, *, seconds: float = 1.0, tags: dict | None = None) -> None:
    """Write a real (cover-less) mp3 chapter via an ffmpeg sine tone."""
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


# --- cover extraction + pixel comparison ------------------------------------


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


def _extract_cover(m4b: Path, out_jpg: Path) -> bool:
    """Pull the attached_pic out of ``m4b`` into ``out_jpg`` (True on success)."""
    proc = subprocess.run(
        [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(m4b), "-an", "-map", "0:v", "-frames:v", "1", str(out_jpg)],
        capture_output=True, text=True, check=False,
    )
    return proc.returncode == 0 and out_jpg.is_file() and out_jpg.stat().st_size > 0


def _pixels(im) -> list[tuple[int, int, int]]:
    """RGB pixel list for a (small) PIL image, across Pillow versions.

    Prefers ``get_flattened_data`` (Pillow ≥ 11) and falls back to the long-standing
    ``getdata`` on older Pillow — avoids the deprecation noise without pinning a
    version or pulling in numpy.
    """
    getter = getattr(im, "get_flattened_data", None) or im.getdata
    return list(getter())


def _signature(path: Path, *, n: int = 8) -> list[tuple[int, int, int]]:
    """Downscaled n×n RGB pixel signature of an image (order-stable list).

    Decoding both the candidate source and the extracted cover to the SAME tiny
    grid makes them comparable across formats (PNG source vs the mjpeg the build
    re-encodes) and sizes. JPEG re-encode shifts pixels slightly, so we compare by
    DISTANCE (below), never exact equality.
    """
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB").resize((n, n), Image.BILINEAR)
        return _pixels(im)


def _sig_distance(a: list, b: list) -> float:
    """Mean per-channel absolute difference between two equal-length signatures."""
    if not a or not b or len(a) != len(b):
        return float("inf")
    total = 0
    for (ar, ag, ab), (br, bg, bb) in zip(a, b):
        total += abs(ar - br) + abs(ag - bg) + abs(ab - bb)
    return total / (len(a) * 3)


# --- command helper (mirrors how the app drops a confirm-build) -------------


def _drop_command(commands_dir: Path, payload: dict) -> Path:
    commands_dir.mkdir(parents=True, exist_ok=True)
    cmd_id = payload.get("cmd_id") or str(uuid.uuid4())
    payload.setdefault("cmd_id", cmd_id)
    final = commands_dir / f"{cmd_id}.json"
    tmp = commands_dir / f".{cmd_id}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


def _confirm_build_cmd(manifest: dict, *, params_extra: dict | None = None) -> dict:
    """A confirm-build command for ``manifest`` with optional extra params.

    ``params_extra`` is merged into the manifest's params — this is exactly how the
    app rides the cover pick along (cover_id / cover_custom_path) without a separate
    command.
    """
    bid = manifest["book_id"]
    rev = manifest["source_rev"]
    params = dict(manifest.get("params", {}))
    if params_extra:
        params.update(params_extra)
    return {
        "cmd_id": str(uuid.uuid4()),
        "action": "confirm-build",
        "book_id": bid,
        "source_rev": rev,
        "confirm_token": manifest["confirm_token"],
        "idempotency_key": f"{bid}:{rev[:16]}",
        "params": params,
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
    try:
        import PIL  # noqa: F401
    except Exception:  # noqa: BLE001
        print("§cover-pick self-check: SKIPPED — Pillow not importable")
        return 1
    if not _has_tools():
        print("§cover-pick self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-coverpick-"))
    support = root / "support"
    watch = root / "watch"
    support.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)
    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)
    os.environ["MP3TOM4B_COVER_WEB"] = "0"  # offline determinism

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import config, dispatcher, scan, state  # noqa: E402

    print(f"self-check tree: {root}\n  support: {support}\n  watch: {watch}\n")

    # === Book A: no embedded cover → generated-only options ==================
    book_a = watch / "Чехов - Каштанка"
    _make_mp3(book_a / "01 - Глава первая.mp3", seconds=1.0,
              tags={"title": "Глава первая", "artist": "Чехов А.П.", "album": "Каштанка"})
    _make_mp3(book_a / "02 - Глава вторая.mp3", seconds=1.0,
              tags={"title": "Глава вторая"})

    scan.run_scan()
    man_a = _manifest_for(config, state, "Чехов - Каштанка")
    assert man_a is not None, "book A manifest not found"

    opts = man_a.get("cover_options") or []
    kinds = [o.get("kind") for o in opts]
    check("setup: book A has generated-only cover_options (offline, no embedded)",
          bool(opts) and set(kinds) == {"generated"} and len(opts) >= 3,
          f"kinds={kinds} n={len(opts)}")
    default_sel = man_a.get("cover_selected")
    check("setup: default cover_selected = first generated (generated-0)",
          default_sel == opts[0]["id"] == "generated-0",
          f"selected={default_sel!r}")

    # The id we deliberately PICK — NOT the default. Use the 3rd variant.
    pick_id = opts[2]["id"]            # "generated-2"
    pick_src = Path(opts[2]["path"])   # the generated-2 file on disk
    default_src = Path(opts[0]["path"])  # the default (generated-0) file
    check("setup: pick target is NOT the default", pick_id != default_sel,
          f"pick={pick_id} default={default_sel}")

    # --- pick-generated: confirm-build with cover_id = generated-2 -----------
    _drop_command(config.commands_dir(),
                  _confirm_build_cmd(man_a, params_extra={"cover_id": pick_id}))
    dispatcher.drain_commands()
    man_a = _manifest_for(config, state, "Чехов - Каштанка")

    check("pick-generated: build reached done",
          man_a.get("status") == "done",
          f"status={man_a.get('status')!r} error={man_a.get('error')}")
    check("pick-generated: manifest cover_selected updated to the pick",
          man_a.get("cover_selected") == pick_id,
          f"selected={man_a.get('cover_selected')!r}")

    res_a = man_a.get("result") if isinstance(man_a.get("result"), dict) else {}
    out_a = Path(res_a.get("output") or res_a.get("output_path") or "")
    has_pic, codec = _has_attached_pic(out_a) if out_a.is_file() else (False, "")
    check("pick-generated: output has an mjpeg attached_pic",
          has_pic and codec == "mjpeg", f"has={has_pic} codec={codec!r}")

    # PROVE it is the PICKED cover, by pixel content: extract → compare to both the
    # picked source and the default source; the picked one must be the closer match.
    extracted = root / "extracted-A.jpg"
    got = _extract_cover(out_a, extracted)
    check("pick-generated: extracted the embedded cover from the .m4b", got,
          str(extracted))
    if got:
        sig_embedded = _signature(extracted)
        d_pick = _sig_distance(sig_embedded, _signature(pick_src))
        d_default = _sig_distance(sig_embedded, _signature(default_src))
        check("pick-generated: embedded cover MATCHES the picked variant (generated-2)",
              d_pick < d_default and d_pick < 24.0,
              f"dist(pick)={d_pick:.1f} dist(default)={d_default:.1f}")
        check("pick-generated: embedded cover is NOT the default (generated-0)",
              d_default - d_pick > 6.0,
              f"default is farther by {d_default - d_pick:.1f}")

    # === pick-custom: a user-replaced file wins =============================
    # Re-arm a SECOND book (a fresh build for an unambiguous custom proof). Draw a
    # solid magenta image as the user's file OUTSIDE the support tree.
    from PIL import Image

    book_b = watch / "Толстой - Хаджи-Мурат"
    _make_mp3(book_b / "01 - Часть.mp3", seconds=1.0,
              tags={"title": "Часть", "artist": "Л. Толстой", "album": "Хаджи-Мурат"})
    scan.run_scan()
    man_b = _manifest_for(config, state, "Толстой - Хаджи-Мурат")
    assert man_b is not None, "book B manifest not found"

    user_file = root / "my-cover.png"           # the user's ORIGINAL file
    MAGENTA = (210, 30, 160)
    Image.new("RGB", (700, 700), MAGENTA).save(user_file, "PNG")

    covers_before = set(config.covers_dir().glob("*"))
    _drop_command(config.commands_dir(),
                  _confirm_build_cmd(man_b,
                                     params_extra={"cover_custom_path": str(user_file)}))
    dispatcher.drain_commands()
    man_b = _manifest_for(config, state, "Толстой - Хаджи-Мурат")

    check("pick-custom: build reached done",
          man_b.get("status") == "done",
          f"status={man_b.get('status')!r} error={man_b.get('error')}")

    # The agent (single writer) must have COPIED the file into covers/ — a new file
    # appeared there and the selected option points inside covers/, not at the
    # user's original path.
    covers_after = set(config.covers_dir().glob("*"))
    new_covers = covers_after - covers_before
    sel_opt = next((o for o in (man_b.get("cover_options") or [])
                    if o.get("id") == man_b.get("cover_selected")), None)
    check("pick-custom: selected option is the custom kind",
          isinstance(sel_opt, dict) and sel_opt.get("kind") == "custom",
          f"selected={man_b.get('cover_selected')!r} opt={sel_opt}")
    copied_into_covers = bool(sel_opt) and str(config.covers_dir()) in str(sel_opt.get("path", ""))
    check("pick-custom: AGENT copied the file into covers/ (app never writes it)",
          copied_into_covers and bool(new_covers)
          and Path(sel_opt["path"]).resolve() != user_file.resolve(),
          f"dest={sel_opt.get('path') if sel_opt else None}")

    res_b = man_b.get("result") if isinstance(man_b.get("result"), dict) else {}
    out_b = Path(res_b.get("output") or res_b.get("output_path") or "")
    has_pic_b, codec_b = _has_attached_pic(out_b) if out_b.is_file() else (False, "")
    check("pick-custom: output has an mjpeg attached_pic",
          has_pic_b and codec_b == "mjpeg", f"has={has_pic_b} codec={codec_b!r}")

    extracted_b = root / "extracted-B.jpg"
    got_b = _extract_cover(out_b, extracted_b) if out_b.is_file() else False
    check("pick-custom: extracted the embedded cover", got_b, str(extracted_b))
    if got_b:
        # The embedded cover should be ~solid magenta (the user's image), proven by
        # its average color sitting close to MAGENTA.
        with Image.open(extracted_b) as im:
            small = im.convert("RGB").resize((4, 4), Image.BILINEAR)
            px = _pixels(small)
        avg = tuple(sum(c[i] for c in px) // len(px) for i in range(3))
        dist = sum(abs(avg[i] - MAGENTA[i]) for i in range(3)) / 3
        check("pick-custom: embedded cover IS the user's image (≈magenta)",
              dist < 40.0, f"avg={avg} target={MAGENTA} dist={dist:.1f}")

    # === guarantee: no embedded + no web → still builds WITH a cover =========
    # Book A already proved this (offline, generated-only, attached_pic present),
    # but assert it explicitly as the PRD-G4 headline.
    check("guarantee: a book with no embedded/web still gets a cover (PRD G4)",
          has_pic, f"book A attached_pic present = {has_pic}")

    # --- summary ------------------------------------------------------------
    # Flat verification: this suite runs ONLY its own checks. Cross-suite
    # regression is orchestrated once by ``agent.selfcheck_all`` (no nested
    # re-runs here — that is what made a single pass take ~30 min).
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print(f"\n§cover-pick self-check: {passed}/{total} checks passed")
    print(f"(temp tree left at {root} for inspection; safe to delete)")

    # Exit honestly: green ⇔ every local check passed.
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
