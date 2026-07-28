"""§cover self-check — the cover chain (generate / web / resolve), no UI.

Run it standalone:

    python3 -m agent.selfcheck_cover

It validates ``test-plan.md §M1 "Цепочка обложки"`` for the engine layer only
(no window picker, no ``cover-choice`` command — those are the next sub-step):

  generate    Cyrillic ("Чехов А.П." / "Каштанка") + a LONG title render to
              several variants; each file exists, is EXACTLY square 1:1, is a
              valid image Pillow can open, and actually has TEXT drawn — proven by
              measuring the "ink" (text-colored pixel) ratio in the text band, so
              an empty render / tofu (□) would FAIL, not pass on "file created".
  variety     the variants differ visibly (distinct gradient permutations).
  web         with the network mocked to fail (urlopen raises), search_web returns
              [] and never raises — the graceful "по возможности" contract.
  resolve     resolve_cover_options is LOCAL-ONLY (D17/M-B): priority embedded →
              generated, default embedded when present else the first generated
              (PRD G4: never coverless). No network is touched on this path at all
              — it is what «Собрать» waits for.
  enrich      the late web leg (scan.enrich_covers_web): what it finds is APPENDED
              — the pre-web list is an exact PREFIX of the post-web one, the
              selection does not move, the files carry the book's GENERATION, a
              search left over from a previous preparation is discarded, and a
              dead network costs nothing but a counted attempt.

It runs ONLY its own checks (cross-suite regression is orchestrated once by
``agent.selfcheck_all`` — there is no nested re-run here). Pillow is required
(the generation guarantee).
"""

from __future__ import annotations

import os
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


# --- image helpers ----------------------------------------------------------


def _is_valid_square(path: Path) -> tuple[bool, str]:
    """(ok, detail): file opens as a valid image AND is exactly 1:1."""
    try:
        from PIL import Image

        with Image.open(path) as im:
            im.load()  # force a full decode (catches truncated files)
            w, h = im.size
    except Exception as exc:  # noqa: BLE001
        return False, f"open-fail {exc!r}"
    return (w == h and w >= 1400), f"{w}x{h}"


def _text_ink_ratio(path: Path, *, band_top_frac: float = 0.40) -> float:
    """Fraction of pixels in the lower text band that are ~text-colored.

    Text is drawn near ``TEXT_HIGH`` (#EAF6FA, near-white) / ``ON_ACCENT``; the
    gradient + scrim behind it is darker/saturated, so counting near-white pixels
    in the band is a robust "is there actually text here" probe. An empty render
    or tofu boxes (which the font would NOT produce for our covered glyphs) drive
    this toward 0. Columns are subsampled for speed.
    """
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        px = im.load()
        target = (0xEA, 0xF6, 0xFA)
        ink = total = 0
        for y in range(int(h * band_top_frac), h):
            for x in range(0, w, 3):
                r, g, b = px[x, y]
                total += 1
                if (abs(r - target[0]) < 48 and abs(g - target[1]) < 48
                        and abs(b - target[2]) < 48):
                    ink += 1
    return ink / total if total else 0.0


# --- the run ----------------------------------------------------------------


def run() -> int:
    try:
        import PIL  # noqa: F401
    except Exception:  # noqa: BLE001
        print("§cover self-check: SKIPPED — Pillow not importable")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-cover-"))
    support = root / "support"
    support.mkdir(parents=True, exist_ok=True)
    # Redirect the data tree + force the offline path for determinism.
    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_COVER_WEB"] = "0"

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import config, cover  # noqa: E402

    covers = config.covers_dir()
    covers.mkdir(parents=True, exist_ok=True)
    print(f"self-check tree: {root}\n  covers: {covers}\n")

    # === generate: Cyrillic short title ====================================
    gen_cyr = cover.generate_variants("Чехов А.П.", "Каштанка", covers,
                                      "chk-kashtanka", count=4)
    check("generate: produced 4 variants", len(gen_cyr) == 4,
          f"got {len(gen_cyr)}")
    all_square = True
    sq_detail = ""
    for p in gen_cyr:
        ok, d = _is_valid_square(p)
        all_square = all_square and ok
        if not ok:
            sq_detail = f"{p.name}: {d}"
    check("generate: every variant is a valid square 1:1 image ≥1400px",
          all_square, sq_detail or "all 1600x1600")
    # Text actually rendered (not empty / not tofu): ink in the text band.
    inks = [_text_ink_ratio(p) for p in gen_cyr]
    min_ink = min(inks) if inks else 0.0
    check("generate: Cyrillic text actually drawn (ink ratio > 0.5%% — not empty/tofu)",
          min_ink > 0.005, f"min ink ratio = {min_ink:.4f}")

    # === generate: LONG title (auto-fit + wrap must still render text) ======
    long_title = "Анна Каренина: роман в восьми частях с эпилогом"
    gen_long = cover.generate_variants("Лев Николаевич Толстой", long_title,
                                       covers, "chk-long", count=2)
    long_ok = True
    for p in gen_long:
        ok, _ = _is_valid_square(p)
        long_ok = long_ok and ok
    long_ink = min((_text_ink_ratio(p) for p in gen_long), default=0.0)
    check("generate: long title renders square + has text (auto-fit/wrap works)",
          long_ok and long_ink > 0.005,
          f"square={long_ok} min_ink={long_ink:.4f}")

    # === variety: the variants are visibly different =======================
    from PIL import Image

    quad_colors = []
    for p in gen_cyr:
        with Image.open(p) as im:
            im = im.convert("RGB")
            w, h = im.size
            quad_colors.append(im.getpixel((w // 4, h // 4)))
    check("variety: variants differ (≥3 distinct top-left-quadrant colors)",
          len(set(quad_colors)) >= 3, f"colors={quad_colors}")

    # === web: graceful on a dead network (urlopen mocked to raise) =========
    import urllib.request as _urlreq

    real_urlopen = _urlreq.urlopen

    def _boom(*_a, **_k):
        raise OSError("network disabled for self-check")

    web_paths = None
    web_raised = False
    try:
        _urlreq.urlopen = _boom  # type: ignore[assignment]
        web_paths = cover.search_web("Чехов А.П.", "Каштанка", covers, "chk-web")
    except Exception as exc:  # the point: it must NOT raise
        web_raised = True
        check("web: search_web survives a dead network without raising", False,
              repr(exc))
    finally:
        _urlreq.urlopen = real_urlopen  # type: ignore[assignment]
    if not web_raised:
        check("web: search_web survives a dead network without raising", True)
        check("web: returns [] when nothing can be fetched",
              web_paths == [], f"got {web_paths!r}")

    # === resolve: local-only, no embedded → generated-only, default = gen-0 ==
    man_plain = {
        "book_id": "chk-resolve-plain",
        "author": "Чехов А.П.",
        "title": "Каштанка",
        "cover_state": "none",
        "cover_preview": None,
    }
    res_plain = cover.resolve_cover_options(man_plain)
    opts_plain = res_plain["cover_options"]
    kinds_plain = [o["kind"] for o in opts_plain]
    check("resolve: no embedded → options are generated-only (no web on this path)",
          bool(opts_plain) and set(kinds_plain) == {"generated"},
          f"kinds={kinds_plain}")
    check("resolve: option dicts carry {id,kind,path,label}",
          all({"id", "kind", "path", "label"} <= set(o) for o in opts_plain))
    check("resolve: every option path exists on disk",
          all(Path(o["path"]).is_file() for o in opts_plain))
    sel_plain = res_plain["cover_selected"]
    check("resolve: default selection = first generated variant",
          sel_plain == opts_plain[0]["id"] and opts_plain[0]["kind"] == "generated",
          f"selected={sel_plain!r}")
    check("resolve: never coverless (≥1 option) — PRD G4 guarantee",
          len(opts_plain) >= 1, f"n={len(opts_plain)}")

    # === resolve: with an embedded cover → embedded FIRST + default =========
    # Forge a real embedded preview file the way scan would have left it.
    emb_path = covers / "chk-resolve-emb-embedded.jpg"
    Image.new("RGB", (300, 300), (200, 30, 30)).save(emb_path, "JPEG")
    man_emb = {
        "book_id": "chk-resolve-emb",
        "author": "Чехов А.П.",
        "title": "Каштанка",
        "cover_state": "embedded",
        "cover_preview": str(emb_path),
    }
    res_emb = cover.resolve_cover_options(man_emb)
    opts_emb = res_emb["cover_options"]
    kinds_emb = [o["kind"] for o in opts_emb]
    check("resolve: embedded present → first option is 'embedded' (priority)",
          bool(opts_emb) and opts_emb[0]["kind"] == "embedded",
          f"kinds={kinds_emb}")
    check("resolve: priority order is embedded → generated (web appended later)",
          kinds_emb == sorted(
              kinds_emb,
              key=lambda k: {"embedded": 0, "web": 1, "generated": 2}[k]),
          f"kinds={kinds_emb}")
    check("resolve: default selection = the embedded cover when present",
          res_emb["cover_selected"] == opts_emb[0]["id"]
          and opts_emb[0]["kind"] == "embedded",
          f"selected={res_emb['cover_selected']!r}")
    check("resolve: generated variants still offered as alternatives",
          "generated" in kinds_emb, f"kinds={kinds_emb}")

    # === selected_cover_path resolves the default id → an existing file =====
    man_emb.update(res_emb)
    sel_path = cover.selected_cover_path(man_emb)
    check("selected_cover_path: resolves cover_selected → existing file",
          isinstance(sel_path, Path) and sel_path.is_file()
          and sel_path == emb_path,
          f"path={sel_path}")

    # === enrich: the LATE web leg appends and never disturbs (D17 / M-B) =====
    # The web lookup no longer runs inside the scan, so what has to be proven here
    # is what the human sees: the strip he is already looking at is a PREFIX of the
    # strip he gets when the search lands, and a search that belongs to a previous
    # preparation of the book cannot write into the current one.
    from agent import scan, state  # noqa: E402 — after the support-dir redirect

    def _mk_book(bid: str, token: str) -> tuple[Path, dict]:
        """A ready, pending-confirm manifest on disk with local covers only."""
        man = {
            "book_id": bid,
            "src_dir": str(root / bid),
            "status": scan.MANIFEST_STATUS_PENDING,
            "phase": scan.MANIFEST_PHASE_READY,
            "source_rev": "rev-fixed",
            "confirm_token": token,
            "build_token": "bt-" + token,
            "author": "Чехов А.П.",
            "title": "Каштанка",
            "cover_state": "none",
            "cover_preview": None,
        }
        man.update(cover.resolve_cover_options(man))
        man["cover_web"] = scan.COVER_WEB_PENDING
        man["cover_web_tries"] = 0
        p = config.books_dir() / f"{bid}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        state.write_json_atomic(p, man)
        return p, man

    def _fake_search(author, title, out_dir, book_id, *, exclude=None,
                     generation=None, start_index=0):
        """Stand-in for the network: writes two square images the real way."""
        out = []
        for k in range(2):
            dest = Path(out_dir) / (
                f"{cover.cover_file_stem(book_id, generation)}-web-{start_index + k}.jpg"
            )
            Image.new("RGB", (400, 400), (10, 60 + 40 * k, 120)).save(dest, "JPEG")
            out.append(dest)
        return out

    path_a, man_a = _mk_book("chk-enrich", "tok-gen-one")
    gen_a = cover.cover_generation(man_a)
    before_opts = [dict(o) for o in man_a["cover_options"]]
    before_sel = man_a["cover_selected"]

    real_search = cover.search_web
    try:
        cover.search_web = _fake_search  # type: ignore[assignment]
        appended = scan._enrich_book_covers(path_a, man_a)
    finally:
        cover.search_web = real_search  # type: ignore[assignment]
    after_a = state.read_json(path_a, default={})
    after_opts = after_a.get("cover_options") or []

    check("enrich: the web leg appended what it found",
          appended == 2 and len(after_opts) == len(before_opts) + 2,
          f"appended={appended} before={len(before_opts)} after={len(after_opts)}")
    check("enrich: APPEND-ONLY — the pre-web list is an exact PREFIX of the new one",
          after_opts[:len(before_opts)] == before_opts,
          f"prefix={[o.get('id') for o in after_opts[:len(before_opts)]]} "
          f"was={[o.get('id') for o in before_opts]}")
    check("enrich: the new tiles are at the END and are the web ones",
          all(o.get("kind") == "web" for o in after_opts[len(before_opts):]),
          f"kinds={[o.get('kind') for o in after_opts]}")
    check("enrich: the human's selection is not moved by a late arrival",
          after_a.get("cover_selected") == before_sel,
          f"selected={after_a.get('cover_selected')!r} was={before_sel!r}")
    check("enrich: revision/build fields untouched (cover is display payload)",
          all(after_a.get(k) == man_a.get(k) for k in
              ("source_rev", "confirm_token", "build_token", "phase", "status")))
    check("enrich: every web file is named for THIS generation",
          all(Path(o["path"]).name.startswith(f"chk-enrich-{gen_a}-web-")
              for o in after_opts[len(before_opts):]),
          f"names={[Path(o['path']).name for o in after_opts[len(before_opts):]]}")
    check("enrich: the leg is marked done, so a later pass will not redo it",
          after_a.get("cover_web") == scan.COVER_WEB_DONE
          and after_a.get("cover_web_tries") == 1,
          f"cover_web={after_a.get('cover_web')!r} "
          f"tries={after_a.get('cover_web_tries')!r}")

    # A search that belongs to the PREVIOUS preparation must not land in the new
    # one: re-arm the book (fresh confirm_token → fresh generation) while holding
    # the stale manifest, then let the stale search finish.
    stale_man = dict(man_a)
    rearmed = dict(after_a)
    rearmed["confirm_token"] = "tok-gen-two"
    rearmed["cover_web"] = scan.COVER_WEB_PENDING
    rearmed["cover_web_tries"] = 0
    state.write_json_atomic(path_a, rearmed)
    try:
        cover.search_web = _fake_search  # type: ignore[assignment]
        late = scan._enrich_book_covers(path_a, stale_man)
    finally:
        cover.search_web = real_search  # type: ignore[assignment]
    after_rearm = state.read_json(path_a, default={})
    check("enrich: a search from the PREVIOUS generation is discarded, not appended",
          late == 0
          and (after_rearm.get("cover_options") or []) == (rearmed.get("cover_options") or [])
          and after_rearm.get("confirm_token") == "tok-gen-two",
          f"late={late} options={len(after_rearm.get('cover_options') or [])}")

    # A dead network: nothing is appended, nothing is disturbed, the try is
    # counted (so the retry budget is finite) and NOTHING raises.
    path_b, man_b = _mk_book("chk-enrich-offline", "tok-offline")
    opts_b_before = [dict(o) for o in man_b["cover_options"]]
    offline_raised = ""
    try:
        _urlreq.urlopen = _boom  # type: ignore[assignment]
        dead = scan._enrich_book_covers(path_b, man_b)
    except Exception as exc:  # noqa: BLE001 — the point: it must NOT raise
        dead, offline_raised = -1, repr(exc)
    finally:
        _urlreq.urlopen = real_urlopen  # type: ignore[assignment]
    after_b = state.read_json(path_b, default={})
    check("enrich: a dead network appends nothing and never raises",
          dead == 0 and not offline_raised
          and (after_b.get("cover_options") or []) == opts_b_before,
          offline_raised or f"appended={dead}")
    check("enrich: a fruitless attempt is counted (the retry budget is finite)",
          after_b.get("cover_web_tries") == 1
          and after_b.get("cover_web") == scan.COVER_WEB_PENDING,
          f"tries={after_b.get('cover_web_tries')!r} "
          f"state={after_b.get('cover_web')!r}")
    check("enrich: an offline book is still buildable — build_token intact (I1)",
          after_b.get("build_token") == man_b["build_token"]
          and after_b.get("phase") == scan.MANIFEST_PHASE_READY)

    # The pass itself obeys the offline switch (the self-check env sets it): with
    # MP3TOM4B_COVER_WEB=0 it must do nothing at all, not even read a manifest.
    check("enrich: the pass is a no-op when the web is switched off",
          scan.enrich_covers_web() == 0
          and (state.read_json(path_b, default={}) or {}).get("cover_web_tries") == 1,
          f"MP3TOM4B_COVER_WEB={os.environ.get('MP3TOM4B_COVER_WEB')!r}")

    # --- summary ------------------------------------------------------------
    # Flat verification: this suite runs ONLY its own checks. Cross-suite
    # regression is orchestrated once by ``agent.selfcheck_all`` (no nested
    # re-runs here — that is what made a single pass take ~30 min).
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print(f"\n§cover self-check: {passed}/{total} checks passed")
    print(f"(temp tree left at {root} for inspection; safe to delete)")

    # Exit honestly: green ⇔ every local check passed.
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
