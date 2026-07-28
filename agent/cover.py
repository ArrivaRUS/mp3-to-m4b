"""Cover-art resolution chain: LOCAL (embedded → generated) now, web later.

M1 (arch/synthesis.md §C, decision R-S3 + D7/D9), re-cut by D17/M-B. The agent
guarantees a cover for **100 % of books** (PRD G4) without ever needing the
network, and — since M-B — without ever *waiting* for it either:

  1. ``cover_state=="embedded"`` — already extracted in :mod:`agent.scan`
     (``probe.extract_cover`` → ``covers/<id>-<gen>-embedded.jpg``); we surface it.
  2. :func:`generate_variants` — the **guarantee**: 3–4 *square* (≥1400×1400)
     covers rendered with **Pillow** (brand gradient D9 + title/author text in a
     Cyrillic-capable system font). No cairosvg (avoids the Cyrillic thin-stroke
     trap) and no WKWebView. This always succeeds as long as Pillow imports, so a
     book is never coverless even fully offline.
  3. :func:`search_web` — best-effort, graceful: query DuckDuckGo / Yandex images
     for "{author} {title} аудиокнига" via stdlib :mod:`urllib`, download a few
     candidates, keep only the *square* ones, save under ``covers/``. The network
     may be down / rate-limited / the markup may change → this NEVER raises and
     returns ``[]`` on any trouble. Every request carries a hard timeout.

**Steps 1–2 are the critical path; step 3 is NOT.** Until M-B the web lookup sat
between "the book was read" and ``phase=ready``, so «Собрать» went live only once
the network had answered — up to ~12 s on a dead one (two sources × the 6 s
timeout). It now runs as a separate pass AFTER the scan and the drain
(:func:`agent.scan.enrich_covers_web`), and its results are APPENDED to a list the
human is already looking at:

    scan (local) → drain → web enrichment
    ─ ready / «Собрать» ──┘        └─ лента обложек дополняется

Two rules make that safe, and both are load-bearing:

  · **APPEND-ONLY** (the human's choice, synthesis §4.2). The order is
    встроенная → сгенерированные → найденные в сети, and a late arrival may only
    be added at the END. Nothing already on screen changes id, path, label,
    position — or the current selection — while the search is running.
  · **GENERATION-SCOPED FILE NAMES** (:func:`cover_generation`, найдено
    Архитектором #2). A late web worker belongs to the preparation that started
    it; if the book has been re-armed since, the worker must not be able to write
    into the new preparation's paths.

:func:`resolve_cover_options` assembles the LOCAL candidate list and picks
``cover_selected``; :func:`web_options_for` produces the late additions. The
manifest is still written ONLY by the agent (:mod:`agent.scan`); this module just
produces the data and the image files. ``source_rev`` / ``confirm_token`` are
NEVER touched — cover data is display payload, not part of the book revision.
"""

from __future__ import annotations

import hashlib
import html as _html
import json
import re
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config

# --- brand palette (branding/brand-basics.md §2/§3, decision D9) -------------
# Firm gradient stops cyan → teal → indigo. Kept as RGB tuples so we can build
# many distinct variations (rotate the order, change the angle) off one source.
BRAND_CYAN = (0x34, 0xE0, 0xD2)    # #34E0D2 — gradient start / light accent
BRAND_TEAL = (0x22, 0xB5, 0xE0)    # #22B5E0 — mid / primary "audio" tone
BRAND_INDIGO = (0x4A, 0x6B, 0xFF)  # #4A6BFF — end / links / progress
BG_PANEL_EDGE = (0x07, 0x0B, 0x10)  # #070B10 — deep panel corner (vignette)
TEXT_HIGH = (0xEA, 0xF6, 0xFA)      # #EAF6FA — primary text
ON_ACCENT = (0xEA, 0xFB, 0xFF)      # #EAFBFF — text/marks on accent

# --- font resolution (Cyrillic MUST render — no tofu/□) ----------------------
# Ordered fallback chains of macOS system TTF/TTC files. Each candidate is probed
# at import-not-time but use-time so a missing file just falls through. Arial
# (Supplemental) carries full Cyrillic and is present on stock macOS; Arial
# Unicode is the broadest net; PingFang/Helvetica are last-ditch. Verified on the
# build machine that 'Ж' renders a real glyph (bbox wider than the .notdef box).
_FONT_BOLD_CHAIN = (
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)
_FONT_REGULAR_CHAIN = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)

# Output geometry. Square 1:1, generous size for crisp Apple Books / Retina.
CANVAS = 1600          # px — final square side (≥1400 per brief)
COVER_MARGIN = 130     # px — safe inset for text from each edge

# Web search: hard ceilings so a wedged/huge response can never hang the
# (launchd-fired, short-lived) agent or fill the disk.
WEB_TIMEOUT_S = 6          # per-request socket timeout
WEB_MAX_CANDIDATES = 4     # at most this many images downloaded
WEB_MAX_BYTES = 6_000_000  # skip anything larger than ~6 MB
WEB_SQUARE_TOL = 0.12      # |w/h - 1| must be ≤ this to count as "square"
#: Candidate images fetched concurrently per wave in :func:`search_web`. These
#: are independent GETs whose cost is round-trip, not bandwidth, so a wave turns
#: N waits into one. Kept small (and joined per wave) so the early stop still
#: bounds how much we ever ask for: at most one wave past the last useful hit.
WEB_FETCH_WORKERS = 6
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Generation: the namespace one PREPARATION of a book owns on disk
# ---------------------------------------------------------------------------
#: Length of the generation segment in a cover file name. 8 hex characters of a
#: SHA-256 — the same order of collision safety the 16-char ``book_id`` already
#: relies on, scoped to a single book's own files.
GENERATION_LEN = 8


def cover_generation(manifest: dict) -> str:
    """Short id of the book's CURRENT preparation — the namespace of its cover files.

    Cover files used to be named after the book alone (``<book_id>-web-0.jpg``),
    which is safe exactly as long as nothing writes them LATE. Since M-B the web
    leg runs after the scan and the drain, so a slow search can still be finishing
    for a book that has meanwhile been re-armed (new files dropped, «Собрать
    заново») — and with a book-only name it would write its image straight into
    the path the NEW preparation just published: the manifest says «Из сети 1» and
    the bytes underneath belong to a different edition of the book. Found by
    Архитектор #2; the fix is to make the collision impossible rather than to
    police it, so the name carries the preparation it was made for.

    Derived from ``book_id`` + ``source_rev`` + ``confirm_token``: the rev changes
    when the files change, the token is re-minted on every re-arm (including a
    forced reconvert at an unchanged rev), so every preparation gets its own
    namespace. Deterministic — a resumed or repeated preparation of the SAME
    generation reuses its own files instead of littering. Never raises: a manifest
    missing the fields simply hashes the empty strings.
    """
    h = hashlib.sha256()
    for part in ("book_id", "source_rev", "confirm_token"):
        h.update(str(manifest.get(part) or "").encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:GENERATION_LEN]


def cover_file_stem(book_id: str, generation: str | None) -> str:
    """``<book_id>-<generation>`` — the prefix every cover file of one preparation shares.

    ``generation=None`` yields the bare ``<book_id>``: the pre-M-B naming, kept for
    direct callers (self-checks) that have no manifest to scope by.
    """
    return f"{book_id}-{generation}" if generation else str(book_id)


def sweep_stale_cover_files(book_id: str, generation: str) -> int:
    """Delete this book's cover files from OTHER generations; return how many.

    Call this AFTER the manifest naming the current generation is on disk —
    publish first, delete second, so no reader can ever see a manifest pointing at
    a file we just removed.

    Deliberately narrow. Only entries of the form ``<book_id>-<8 hex>-…`` are
    considered, so a user's custom pick (``<book_id>-custom.jpg``) and any
    pre-generation file (``<book_id>-gen-grad-diag.png``, possibly still named by
    an older manifest) are left strictly alone. Never raises — a cover file we
    could not delete is a wasted megabyte, not a broken book.
    """
    prefix = f"{book_id}-"
    removed = 0
    try:
        entries = list(config.covers_dir().glob(f"{book_id}-*"))
    except OSError:
        return 0
    for path in entries:
        tail = path.name[len(prefix):]
        seg, sep, _ = tail.partition("-")
        if not sep or len(seg) != GENERATION_LEN:
            continue                      # not a generation-scoped name
        if any(c not in "0123456789abcdef" for c in seg):
            continue                      # …or not a generation at all
        if seg == generation:
            continue                      # the live one
        try:
            path.unlink()
        except OSError:
            continue
        removed += 1
    return removed


# ---------------------------------------------------------------------------
# Pillow import is deferred so importing this module never fails (the agent may
# run a scan that has no cover work). Generation is the guarantee, so if Pillow
# is genuinely unavailable we surface it loudly only when generation is asked for.
# ---------------------------------------------------------------------------
def _pil():
    from PIL import Image, ImageDraw, ImageFont  # noqa: PLC0415 (deferred on purpose)

    return Image, ImageDraw, ImageFont


def _load_font(size: int, *, bold: bool):
    """Load a Cyrillic-capable TrueType font at ``size``; walk the fallback chain.

    Returns a PIL ``FreeTypeFont``. Raises ``RuntimeError`` only if NONE of the
    candidates load (so the caller — generation, our guarantee — fails loudly
    rather than silently drawing tofu with the bitmap default font).
    """
    _, _, ImageFont = _pil()
    chain = _FONT_BOLD_CHAIN if bold else _FONT_REGULAR_CHAIN
    for path in chain:
        if Path(path).is_file():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    raise RuntimeError("no Cyrillic-capable system font found for cover generation")


# --- gradient helpers --------------------------------------------------------


def _gradient_strip(stops: list, length: int):
    """Build a ``length``×1 RGB image ramping through ``stops`` (C-resized).

    A tiny ``len(stops)``-wide source is resized to ``length`` with bilinear
    interpolation — Pillow does the smooth multi-stop blend in C, so the gradient
    costs microseconds instead of a per-pixel Python loop.
    """
    Image, _, _ = _pil()
    src = Image.new("RGB", (len(stops), 1))
    for i, c in enumerate(stops):
        src.putpixel((i, 0), c)
    return src.resize((length, 1), Image.BILINEAR)


def _draw_diagonal_gradient(img, stops: list, angle_deg: float) -> None:
    """Paint ``img`` in place with a diagonal multi-stop gradient at ``angle_deg``.

    Builds a 1-D gradient strip (C-speed), stretches it to an oversized square,
    rotates by ``angle_deg`` and crops the center — so the whole fill is a couple
    of Pillow C ops, not a 2.5-million-iteration Python loop. The oversize (×√2)
    guarantees the rotated strip fully covers the canvas with no empty corners.
    """
    import math

    Image, _, _ = _pil()
    w, h = img.size
    big = int(max(w, h) * 1.45) + 2  # ≥ diagonal so rotation leaves no gaps
    strip = _gradient_strip(stops, big).resize((big, big))
    rotated = strip.rotate(angle_deg, resample=Image.BILINEAR, expand=False)
    left = (big - w) // 2
    top = (big - h) // 2
    img.paste(rotated.crop((left, top, left + w, top + h)), (0, 0))


def _apply_corner_vignette(img, strength: float = 0.55) -> None:
    """Darken the corners toward ``BG_PANEL_EDGE`` for depth (radial falloff).

    Uses ``Image.radial_gradient`` as an L-mask (C-speed) — bright center fades to
    the deep panel edge color at the corners — instead of a per-pixel Python loop.
    Operates on an RGB image (run before the RGBA conversion / scrim).
    """
    Image, _, _ = _pil()
    w, h = img.size
    # radial_gradient: 0 (black) at center → 255 (white) at the corners on a
    # square; resize to our canvas. Center stays ~0 for ~half the radius so the
    # text area is untouched, then ramps up toward the corners.
    grad = Image.radial_gradient("L").resize((w, h), Image.BILINEAR)
    # Compress the ramp: keep the inner ~55 % near 0, scale the rest to strength.
    lut = []
    for v in range(256):
        d = v / 255.0
        f = max(0.0, (d - 0.55) / 0.45) ** 1.6 * strength
        lut.append(int(max(0, min(255, round(f * 255)))))
    mask = grad.point(lut)
    edge = Image.new("RGB", (w, h), BG_PANEL_EDGE)
    img.paste(Image.composite(edge, img, mask), (0, 0))


def _scrim_for_text(img, top_frac: float = 0.42) -> None:
    """Lay a soft dark scrim over the lower ``(1-top_frac)`` of the canvas.

    Guarantees text contrast regardless of which (bright cyan vs deep indigo)
    part of the gradient sits behind the title — the bottom always reads on a
    darkened band. Built as a tiny vertical alpha ramp resized to full size
    (C-speed), then alpha-composited — no per-pixel Python loop.
    """
    Image, _, _ = _pil()
    w, h = img.size
    start = int(h * top_frac)
    ramp_h = max(1, h - start)
    # 1×ramp_h alpha column: 0 at the band top → ~150 at the bottom.
    col = Image.new("L", (1, ramp_h))
    for y in range(ramp_h):
        col.putpixel((0, y), int(150 * y / max(1, ramp_h - 1)))
    alpha_full = Image.new("L", (w, h), 0)
    alpha_full.paste(col.resize((w, ramp_h)), (0, start))
    overlay = Image.new("RGBA", (w, h), (5, 9, 14, 0))
    overlay.putalpha(alpha_full)
    img.alpha_composite(overlay)


# --- text layout -------------------------------------------------------------


def _text_w(draw, text: str, font) -> int:
    """Pixel width of ``text`` in ``font`` (uses textbbox; handles empty)."""
    if not text:
        return 0
    l, _, r, _ = draw.textbbox((0, 0), text, font=font)
    return r - l


def _wrap_to_width(draw, text: str, font, max_w: int) -> list:
    """Greedy word-wrap ``text`` to lines no wider than ``max_w`` px.

    Words longer than ``max_w`` (rare, e.g. a very long single token) are kept on
    their own line rather than dropped — we'd rather a slightly tight line than
    losing content. Returns a list of line strings (≥1).
    """
    words = text.split()
    if not words:
        return [text]
    lines: list = []
    cur = words[0]
    for word in words[1:]:
        trial = f"{cur} {word}"
        if _text_w(draw, trial, font) <= max_w:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    return lines


def _fit_title(draw, title: str, max_w: int, max_h: int,
               *, hi: int, lo: int, max_lines: int):
    """Auto-size the title: largest font (lo..hi) that fits ``max_w``×``max_h``.

    Binary-ish descending search over point sizes; for each size we wrap to width
    and check the wrapped block fits within ``max_lines`` and ``max_h``. Returns
    ``(font, lines, line_height)``. Always returns *something* (at ``lo`` with a
    forced wrap) so a pathologically long title still renders, just smaller.
    """
    size = hi
    while size >= lo:
        font = _load_font(size, bold=True)
        lines = _wrap_to_width(draw, title, font, max_w)
        lh = int(size * 1.18)
        if len(lines) <= max_lines and lh * len(lines) <= max_h:
            return font, lines, lh
        size -= 6
    font = _load_font(lo, bold=True)
    lines = _wrap_to_width(draw, title, font, max_w)
    return font, lines, int(lo * 1.18)


def _draw_centered_block(draw, lines: list, font, lh: int, top: int,
                         w: int, fill) -> int:
    """Draw ``lines`` horizontally centered starting at ``top``; return new y."""
    y = top
    for line in lines:
        lw = _text_w(draw, line, font)
        draw.text(((w - lw) // 2, y), line, font=font, fill=fill)
        y += lh
    return y


# --- variant style table -----------------------------------------------------
# Each variant differs visibly: a distinct ordering of the three brand stops and
# a distinct gradient angle (D9 says "different angles / stop permutations").
_VARIANT_STYLES = [
    {"id": "grad-diag", "stops": [BRAND_CYAN, BRAND_TEAL, BRAND_INDIGO], "angle": 115},
    {"id": "grad-rev", "stops": [BRAND_INDIGO, BRAND_TEAL, BRAND_CYAN], "angle": 35},
    {"id": "grad-deep", "stops": [BRAND_TEAL, BRAND_INDIGO, BRAND_CYAN], "angle": 160},
    {"id": "grad-vert", "stops": [BRAND_CYAN, BRAND_INDIGO, BRAND_TEAL], "angle": 80},
]


def _accent_dot(img, draw, style: dict) -> None:
    """Small brand-tinted geometric mark (play-ish wedge) for subtle variety.

    A filled triangle in ``ON_ACCENT`` on a darkened disc near the top — echoes
    the logo's book+play motif without redrawing it. Purely decorative; never
    overlaps the text band (kept in the top ~30 %).
    """
    w, _ = img.size
    cx = w // 2
    cy = int(w * 0.30)
    r = int(w * 0.085)
    # Dark disc to separate the wedge from the gradient.
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(11, 17, 24, 210))
    # Play triangle, slightly right-shifted for optical centering.
    s = int(r * 0.62)
    tri = [(cx - s + r * 0.10, cy - s),
           (cx - s + r * 0.10, cy + s),
           (cx + s + r * 0.10, cy)]
    draw.polygon(tri, fill=ON_ACCENT)


def generate_variants(author: str, title: str, out_dir: Path, book_id: str,
                      *, count: int = 4, generation: str | None = None) -> list:
    """Render ``count`` square brand covers for (author, title); return file paths.

    GUARANTEE of the chain (PRD G4): this works fully offline. Each variant uses
    a distinct gradient permutation/angle (D9) plus a corner vignette and a text
    scrim, then the title (large, auto-fit, wrapped) and author (smaller) centered
    on the lower half. Cyrillic renders for real (Arial/Arial-Unicode). Files are
    deterministic: ``<book_id>-<generation>-gen-<style_id>.png`` (see
    :func:`cover_generation`; without a generation, the bare ``<book_id>-gen-…``).
    ``out_dir`` is created.

    Returns the list of written ``Path``s (length ``count`` on success). Raises
    only if Pillow / a Cyrillic font is entirely unavailable (a real environment
    fault we must not paper over — the whole guarantee rests on this path).
    """
    Image, ImageDraw, _ = _pil()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    title = (title or "Без названия").strip()
    author = (author or "").strip()
    count = max(1, min(count, len(_VARIANT_STYLES)))

    paths: list = []
    for style in _VARIANT_STYLES[:count]:
        # Paint gradient + vignette on an RGB canvas (3-channel pixels), THEN
        # promote to RGBA so the scrim can alpha-composite a soft dark band.
        base = Image.new("RGB", (CANVAS, CANVAS), (0, 0, 0))
        _draw_diagonal_gradient(base, style["stops"], style["angle"])
        _apply_corner_vignette(base)
        img = base.convert("RGBA")
        _scrim_for_text(img, top_frac=0.40)

        draw = ImageDraw.Draw(img, "RGBA")
        _accent_dot(img, draw, style)

        inner_w = CANVAS - 2 * COVER_MARGIN
        # Title occupies the lower-middle; author sits just under it.
        title_font, title_lines, title_lh = _fit_title(
            draw, title, inner_w, int(CANVAS * 0.34),
            hi=160, lo=58, max_lines=4,
        )
        author_font = _load_font(64, bold=False)
        author_lines = _wrap_to_width(draw, author, author_font, inner_w) if author else []
        author_lh = int(64 * 1.2)

        title_block_h = title_lh * len(title_lines)
        author_block_h = author_lh * len(author_lines)
        gap = 46 if author_lines else 0
        total_h = title_block_h + gap + author_block_h
        # Anchor the block so it sits in the lower 58 % region, bottom-padded.
        top = int(CANVAS * 0.92) - total_h
        top = max(int(CANVAS * 0.46), top)

        y = _draw_centered_block(draw, title_lines, title_font, title_lh, top,
                                 CANVAS, TEXT_HIGH)
        if author_lines:
            y += gap
            _draw_centered_block(draw, author_lines, author_font, author_lh, y,
                                 CANVAS, ON_ACCENT)

        out_path = out_dir / f"{cover_file_stem(book_id, generation)}-gen-{style['id']}.png"
        img.convert("RGB").save(out_path, "PNG", optimize=True)
        paths.append(out_path)
    return paths


# ---------------------------------------------------------------------------
# Web search (best-effort, graceful — never raises, returns [] on any trouble)
# ---------------------------------------------------------------------------


def _http_get(url: str, *, max_bytes: int = WEB_MAX_BYTES) -> bytes | None:
    """GET ``url`` with a UA + hard timeout; return bytes or None on any failure.

    Caps the body at ``max_bytes`` (read incrementally) so a giant/streaming
    response cannot exhaust memory. Swallows EVERY exception — this is a
    best-effort enrichment path; the generation fallback is the guarantee.
    """
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=WEB_TIMEOUT_S) as resp:
            return resp.read(max_bytes + 1)[:max_bytes]
    except Exception:  # noqa: BLE001 — best-effort by contract
        return None


# DuckDuckGo's lightweight HTML images endpoint returns JSON with `image` URLs.
# We hit the public `i.js` token flow defensively; if anything in the markup
# shifts we simply get [] and fall back to generation. Yandex is a secondary
# scrape of `<img ... src>` from the images SERP.
_DDG_VQD_RE = re.compile(r"vqd=['\"]?([\d-]+)['\"]?")
_IMG_SRC_RE = re.compile(r"https?://[^\s\"'<>]+?\.(?:jpg|jpeg|png)", re.IGNORECASE)


def _ddg_image_urls(query: str, *, limit: int) -> list:
    """Best-effort DuckDuckGo image URLs for ``query`` (``[]`` on any trouble)."""
    q = urllib.parse.quote(query)
    # 1) fetch a token (vqd) from the HTML endpoint.
    token_page = _http_get(f"https://duckduckgo.com/?q={q}&iax=images&ia=images")
    if not token_page:
        return []
    m = _DDG_VQD_RE.search(token_page.decode("utf-8", "ignore"))
    if not m:
        return []
    vqd = m.group(1)
    api = (
        f"https://duckduckgo.com/i.js?l=ru-ru&o=json&q={q}"
        f"&vqd={vqd}&f=,,,&p=1"
    )
    raw = _http_get(api)
    if not raw:
        return []
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
        results = data.get("results", []) if isinstance(data, dict) else []
    except (ValueError, AttributeError):
        return []
    urls: list = []
    for r in results:
        if isinstance(r, dict) and isinstance(r.get("image"), str):
            urls.append(r["image"])
        if len(urls) >= limit:
            break
    return urls


def _yandex_image_urls(query: str, *, limit: int) -> list:
    """Best-effort Yandex image URLs for ``query`` (``[]`` on any trouble)."""
    q = urllib.parse.quote(query)
    page = _http_get(f"https://yandex.ru/images/search?text={q}")
    if not page:
        return []
    text = page.decode("utf-8", "ignore")
    # Yandex embeds escaped URLs in JSON; unescape then regex direct image links.
    text = _html.unescape(text).replace("\\/", "/")
    seen: list = []
    for m in _IMG_SRC_RE.finditer(text):
        u = m.group(0)
        if u not in seen:
            seen.append(u)
        if len(seen) >= limit:
            break
    return seen


def _is_square_image(data: bytes) -> bool:
    """True iff ``data`` decodes to a roughly 1:1 image (within ``WEB_SQUARE_TOL``)."""
    try:
        from io import BytesIO

        Image, _, _ = _pil()
        with Image.open(BytesIO(data)) as im:
            w, h = im.size
            im.verify()  # ensure it's a real, decodable image
    except Exception:  # noqa: BLE001
        return False
    if w < 200 or h < 200:  # too small to be a useful cover
        return False
    return abs((w / h) - 1.0) <= WEB_SQUARE_TOL


def search_web(author: str, title: str, out_dir: Path, book_id: str,
               *, exclude: list | None = None, generation: str | None = None,
               start_index: int = 0) -> list:
    """Best-effort web search for SQUARE cover candidates; never raises.

    Queries "{author} {title} аудиокнига" (D7) on DuckDuckGo then Yandex, downloads
    up to :data:`WEB_MAX_CANDIDATES` images, keeps only the square ones, and saves
    them under ``out_dir`` as ``<book_id>-<generation>-web-<n>.<ext>``. Returns the
    list of written ``Path``s — possibly empty (no net / nothing square / markup
    changed).

    ``generation`` scopes the file names to ONE preparation of the book, so a
    search that finishes after the book was re-armed writes into names nothing
    points at (see :func:`cover_generation`). ``start_index`` continues the
    numbering past whatever web candidates a previous attempt already saved, which
    is what lets the caller APPEND results without ever renaming or replacing an
    option the human can already see.

    This is the "по возможности" link, and since M-B it is also entirely off the
    critical path: the guarantee is :func:`generate_variants`, which has already
    run by the time anyone calls this. Any network/parse/decoding failure degrades
    to ``[]`` silently.
    """
    out_dir = Path(out_dir)
    query = " ".join(p for p in (author, title, "аудиокнига") if p).strip()
    if not query:
        return []

    candidates: list = []
    for fetch in (_ddg_image_urls, _yandex_image_urls):
        try:
            candidates.extend(fetch(query, limit=WEB_MAX_CANDIDATES * 3))
        except Exception:  # noqa: BLE001 — never let a source kill the chain
            continue
        if len(candidates) >= WEB_MAX_CANDIDATES * 3:
            break

    saved: list = []
    seen_hashes: set = set()
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return []

    # Candidate bodies are fetched a WAVE at a time and then consumed in the
    # ORIGINAL candidate order, so the chosen files (and their -web-N names) are
    # exactly what the one-request-at-a-time loop produced — only the waiting is
    # shared. These are ~12 independent GETs of a few hundred KB each: serially
    # they cost ~2.8 s of pure round-trip, which was ~30 % of the whole scan.
    # Waves rather than "submit everything": the early stop below is preserved,
    # so at most one wave beyond the last useful candidate is ever requested, and
    # each wave is joined before the next starts — no request outlives the call.
    n = max(0, int(start_index))
    i = 0
    while i < len(candidates) and len(saved) < WEB_MAX_CANDIDATES:
        wave = candidates[i:i + WEB_FETCH_WORKERS]
        i += len(wave)
        if len(wave) == 1:
            bodies = [_http_get(wave[0])]
        else:
            with ThreadPoolExecutor(
                max_workers=len(wave), thread_name_prefix="mp3tom4b-cover"
            ) as pool:
                bodies = list(pool.map(_http_get, wave))
        for data in bodies:
            if len(saved) >= WEB_MAX_CANDIDATES:
                break
            if not data or not _is_square_image(data):
                continue
            digest = hashlib.sha256(data).hexdigest()
            if digest in seen_hashes:
                continue
            seen_hashes.add(digest)
            ext = "png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "jpg"
            dest = out_dir / f"{cover_file_stem(book_id, generation)}-web-{n}.{ext}"
            try:
                dest.write_bytes(data)
            except OSError:
                continue
            saved.append(dest)
            n += 1
    return saved


# ---------------------------------------------------------------------------
# Assemble the ordered candidate list + default selection
# ---------------------------------------------------------------------------


def _option(kind: str, path, label: str, index: int) -> dict:
    """One cover candidate descriptor for the manifest's ``cover_options``."""
    return {
        "id": f"{kind}-{index}",
        "kind": kind,           # "embedded" | "web" | "generated"
        "path": str(path),
        "label": label,
    }


def resolve_cover_options(manifest: dict) -> dict:
    """Build the LOCAL cover-candidate list + default for one book. No network.

    Order is **embedded → generated**, and it always ends with ≥1 generated variant
    so the list is never empty (PRD G4). Returns a dict to MERGE into the
    manifest::

        {"cover_options": [ {id,kind,path,label}, ... ],
         "cover_selected": "<option id>"}

    The default (``cover_selected``) is the embedded cover if present, otherwise
    the first generated variant — a real cover on day one; the user can re-pick in
    the confirm window.

    **This function no longer touches the network, and that is the point of M-B.**
    It used to run :func:`search_web` in the middle of the list (embedded → web →
    generated), which put the whole cover chain — and therefore ``phase=ready``,
    ``build_token`` and an active «Собрать» — behind the round-trip time of two
    image search engines. Web candidates are now produced by
    :func:`web_options_for` in a later pass and APPENDED to the end of this list,
    so what the human waits for is only ever local work: a file read and a Pillow
    render. Ordering is unchanged for everything that was already here, which is
    what makes the append safe (see the module docstring).

    Does NOT write the manifest itself — the caller (:mod:`agent.scan`) owns that,
    keeping "agent is the single writer" intact. Never touches ``source_rev`` /
    ``confirm_token``: cover data is display payload, not part of the revision.
    """
    bid = str(manifest.get("book_id", "book"))
    author = str(manifest.get("author") or "")
    title = str(manifest.get("title") or "")
    covers = config.covers_dir()
    generation = cover_generation(manifest)

    options: list = []

    # 1) embedded (already extracted by scan into covers/<id>-<gen>-embedded.jpg).
    embedded_default = None
    if manifest.get("cover_state") == "embedded":
        preview = manifest.get("cover_preview")
        if isinstance(preview, str) and preview and Path(preview).is_file():
            opt = _option("embedded", preview, "Из файла", 0)
            options.append(opt)
            embedded_default = opt["id"]

    # 2) generated (the guarantee — always present). Runs inline: with the web leg
    # gone there is nothing left to overlap it with, and the thread that used to
    # hide the drawing behind the network would now only add a handoff.
    try:
        gen_paths = generate_variants(author, title, covers, bid,
                                      generation=generation)
    except Exception:  # noqa: BLE001 — a coverless book is worse than a bare list
        gen_paths = []

    gen_default = None
    for i, gp in enumerate(gen_paths):
        opt = _option("generated", gp, f"Вариант {i + 1}", i)
        options.append(opt)
        if gen_default is None:
            gen_default = opt["id"]

    selected = embedded_default or gen_default
    if selected is None and options:
        selected = options[0]["id"]

    return {"cover_options": options, "cover_selected": selected}


def web_options_for(manifest: dict, *, start_index: int = 0) -> list:
    """Search the web for this book's cover and return the options to APPEND.

    The late half of the chain (D7 «по возможности»), run by
    :func:`agent.scan.enrich_covers_web` after the scan and the drain. Returns
    option dicts in the same shape :func:`resolve_cover_options` produces, for the
    images that were actually saved — numbered from ``start_index`` so their ids
    (``web-N``) cannot collide with anything already in the list.

    The caller appends these and writes nothing else: no existing option is
    reordered, renamed or re-pathed, and ``cover_selected`` is left exactly where
    the human (or the default) put it. A tile that is on screen stays where it is
    while the search runs — the reason the list is append-only at all.

    Never raises: no network, no results, a wedged source → ``[]``.
    """
    bid = str(manifest.get("book_id", "book"))
    author = str(manifest.get("author") or "")
    title = str(manifest.get("title") or "")
    start = max(0, int(start_index))
    try:
        paths = search_web(author, title, config.covers_dir(), bid,
                           generation=cover_generation(manifest),
                           start_index=start)
    except Exception:  # noqa: BLE001 — defensive; search_web already guards
        return []
    return [_option("web", p, f"Из сети {start + i + 1}", start + i)
            for i, p in enumerate(paths)]


def selected_cover_path(manifest: dict) -> Path | None:
    """Resolve the manifest's ``cover_selected`` id → its file ``Path``.

    Used by the engine to know which cover to burn into the ``.m4b``. Falls back
    to the embedded preview (back-compat with the pre-options manifest shape) and
    finally ``None``. Only returns a path that actually exists on disk.
    """
    sel = manifest.get("cover_selected")
    for opt in manifest.get("cover_options") or []:
        if isinstance(opt, dict) and opt.get("id") == sel:
            p = opt.get("path")
            if isinstance(p, str) and p and Path(p).is_file():
                return Path(p)
    # Back-compat: older manifests only had cover_state/cover_preview.
    if manifest.get("cover_state") == "embedded":
        preview = manifest.get("cover_preview")
        if isinstance(preview, str) and preview and Path(preview).is_file():
            return Path(preview)
    return None


# Kept for API stability / clarity: the embedded extraction itself lives in
# probe.extract_cover (called by scan); this thin alias documents the chain's
# step 1 without duplicating the ffmpeg logic.
def extract_embedded(mp3_path: Path, out_path: Path) -> Path | None:
    """Extract the embedded picture from ``mp3_path`` → ``out_path`` (or None).

    Delegates to :func:`agent.probe.extract_cover`. Present so the cover chain
    reads top-to-bottom in one module; the real ffmpeg copy stays in ``probe``.
    """
    from . import probe

    return out_path if probe.extract_cover(mp3_path, out_path) else None
