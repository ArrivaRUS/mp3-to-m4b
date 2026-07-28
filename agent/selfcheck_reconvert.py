"""§reconvert self-check — empirical proof of «Собрать заново» (re-arm + rebuild).

Run it standalone:

    python3 -m agent.selfcheck_reconvert

«Собрать заново» lets the user rebuild an ALREADY-FINISHED book with one click
(instead of the non-obvious "rename the folder" workaround). The subtle,
correctness-critical part is the IDEMPOTENCY LEDGER: the book was built at its
current ``source_rev``, so its build key (``book_id:source_rev[:16]``) already
sits in ``manifest['processed_keys']``. Reconvert keeps ``source_rev`` UNCHANGED
(the files did not move), so the next ``confirm-build`` derives the SAME key — and
WITHOUT clearing the ledger :func:`dispatcher.validate_command` would short-circuit
to ``idempotent_skip`` and NO rebuild would run. A compile-check cannot prove any
of that; only a REAL build → reconvert → REAL rebuild can. So this suite drives the
real engine on a throwaway tree (``MP3TOM4B_SUPPORT_DIR`` / ``MP3TOM4B_WATCH_DIR``
redirect everything; the user's real Application Support is never touched) and
asserts the OBSERVABLE outcomes:

  re-arms          a done book, after a reconvert command, is back at
                   ``pending-confirm`` with a FRESH confirm_token and a CLEARED
                   ``processed_keys`` (and no stale result/error/build markers).
  old-agent probe  a done book whose manifest LACKS a field a newer agent adds
                   (``source_samplerate`` — emulating a book built by an old agent)
                   gets that field BACK after a reconvert, because reconvert
                   re-SCANS the sources (a real re-probe) instead of re-arming the
                   stale manifest in place; the rebuild then still runs to
                   ``build_done`` (the re-scan cleared the dedup ledger).
  dedup reset      the SAME idempotency_key that was recorded by the first build is
                   NOT in the ledger after reconvert → the next confirm-build with
                   that exact key REALLY builds (reaches ``build_done`` + a new
                   .m4b), rather than collapsing to ``build_skipped_idempotent``.
                   THIS is the proof the dedup was reset — asserted three ways:
                   the ledger is empty, a build_started/build_done pair fires for
                   the rebuild, and NO build_skipped_idempotent event is emitted.
  source gone      a reconvert for a done book whose source folder VANISHED is
                   REJECTED (``reconvert_rejected`` reason=source_missing), the
                   book stays ``done`` (never re-armed into a build that can't run),
                   and the command is consumed.
  status guard     a reconvert for a book that is NOT done (still pending-confirm)
                   is a no-op reject (reason=status_not_done); status untouched.
  well-formed      the app-shaped reconvert command (dispatcher's accepted shape) is
                   accepted; a reconvert with a bogus book_id is rejected
                   (manifest_missing) and never crashes the drain.

To keep it FAST + deterministic the books are SHORT (a couple of seconds of
silence — we only need a real .m4b to exist, not a long encode), and the copy
stability debounce is disabled (fixtures are already stable). It runs ONLY its own
checks (cross-suite regression is orchestrated once by ``agent.selfcheck_all`` — no
nested re-runs) and returns 0 ⇔ every check here passed. Requires ffmpeg + ffprobe
on PATH; if either is missing it says so and exits non-zero. It writes only inside
its temp tree (plus each book's ``.m4b`` next to its source folder, inside the temp
watch dir).
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


# --- ffmpeg helpers ----------------------------------------------------------


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _has_tools() -> bool:
    return bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))


def _make_silence_mp3(path: Path, *, seconds: float, tags: dict | None = None) -> None:
    """Write a real (silent) mp3 of ``seconds`` virtual length via anullsrc.

    Short is fine here — the suite only needs a REAL .m4b to be produced so the
    idempotency ledger records a key; it is not testing encode duration.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        _ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(seconds), "-c:a", "libmp3lame", "-b:a", "192k",
    ]
    for k, v in (tags or {}).items():
        argv += ["-metadata", f"{k}={v}"]
    argv.append(str(path))
    subprocess.run(argv, check=True, capture_output=True)


# --- command helpers (mirror how the app drops commands) ---------------------


def _drop_command(commands_dir: Path, payload: dict) -> Path:
    commands_dir.mkdir(parents=True, exist_ok=True)
    cmd_id = payload.get("cmd_id") or str(uuid.uuid4())
    payload.setdefault("cmd_id", cmd_id)
    final = commands_dir / f"{cmd_id}.json"
    tmp = commands_dir / f".{cmd_id}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


def _reconvert_cmd(book_id: str) -> dict:
    """A reconvert command exactly as the app (EngineClient.makeReconvert) drops it:
    no source_rev / confirm_token — it targets the book by id."""
    return {
        "cmd_id": str(uuid.uuid4()),
        "action": "reconvert",
        "book_id": book_id,
        "idempotency_key": f"reconvert:{book_id}",
        "ts": time.time(),
    }


def _confirm_build_cmd(manifest: dict) -> dict:
    """A confirm-build command with the EXACT deterministic idempotency_key the app
    derives (``book_id:source_rev[:16]`` — EngineClient.idempotencyKey). Using the
    real key (not a random suffix) is what lets us prove the dedup was reset: the
    first build records THIS key; the post-reconvert build reuses THE SAME key and
    must still run."""
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
        "idempotency_key": f"{bid}:{rev[:16]}",  # deterministic, = the app's key
        "params": dict(manifest.get("params", {})),
        "ts": time.time(),
    }


def _manifest_for(config, state, suffix: str) -> dict | None:
    for p in config.books_dir().glob("*.json"):
        m = state.read_json(p)
        if str(m.get("src_dir", "")).endswith(suffix):
            return m
    return None


def _events_of(state, kind: str) -> list[dict]:
    return [e for e in state.read_events() if e.get("event") == kind]


def _reconvert_cmds_on_disk(config, state) -> list[Path]:
    out = []
    for p in config.commands_dir().glob("*.json"):
        c = state.read_json(p, default=None)
        if isinstance(c, dict) and c.get("action") == "reconvert":
            out.append(p)
    return out


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not _has_tools():
        print("§reconvert self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-reconvert-"))
    support = root / "support"
    watch = root / "watch"
    support.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)
    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)
    os.environ["MP3TOM4B_COVER_WEB"] = "0"       # offline determinism (no web cover)
    os.environ["MP3TOM4B_STABILITY_DEBOUNCE_S"] = "0"  # fixtures already stable

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import build_m4b, config, dispatcher, scan, state  # noqa: E402

    print(f"self-check tree: {root}\n  support: {support}\n  watch:   {watch}\n")

    # === Arm + BUILD a book all the way to done ===============================
    book_dir = watch / "Пересборка - Книга"
    for i in range(1, 4):
        _make_silence_mp3(
            book_dir / f"{i:02d} - Глава {i}.mp3", seconds=2.0,
            tags={"title": f"Глава {i}", "album": "Книга",
                  "album_artist": "Тест Пересборки"},
        )
    scan.run_scan()
    man0 = _manifest_for(config, state, "Пересборка - Книга")
    assert man0 is not None, "scan did not arm the book"
    book_id = man0["book_id"]
    manifest_path = config.books_dir() / f"{book_id}.json"
    source_rev0 = man0["source_rev"]
    token0 = man0["confirm_token"]
    # D17: доказательство полноты манифеста. Перечеканивается на каждой подготовке,
    # поэтому годится как отпечаток «та же это подготовка или уже другая».
    build_token0 = scan.manifest_build_token(man0)

    # First build → done. Use the DETERMINISTIC key so the ledger records exactly
    # the key a re-click would reuse (that is the dedup we must later prove reset).
    build_cmd1 = _confirm_build_cmd(man0)
    the_key = build_cmd1["idempotency_key"]
    _drop_command(config.commands_dir(), build_cmd1)
    dispatcher.drain_commands()

    man_built = state.read_json(manifest_path)
    out_path = build_m4b.default_output_path(man_built)
    check("setup: first build reached done with a real .m4b",
          man_built.get("status") == "done"
          and out_path.is_file() and out_path.stat().st_size > 0,
          f"status={man_built.get('status')!r} out_exists={out_path.exists()}")
    check("setup: the build recorded its idempotency_key in processed_keys (ledger armed)",
          the_key in (man_built.get("processed_keys") or []),
          f"processed_keys={man_built.get('processed_keys')}")

    # Sanity: a repeat confirm-build with the SAME key is deduped WHILE the book is
    # done (proves the ledger genuinely blocks — the exact thing reconvert must clear).
    _drop_command(config.commands_dir(), _confirm_build_cmd(man_built))
    dispatcher.drain_commands()
    dedup_events_before = _events_of(state, "build_skipped_idempotent")
    check("setup: a repeat build (same key) IS deduped while done (ledger blocks)",
          any(e.get("book_id") == book_id for e in dedup_events_before),
          f"build_skipped_idempotent={dedup_events_before}")

    # === RECONVERT: re-arm done → pending-confirm ============================
    n_started_before = len(_events_of(state, "build_started"))
    n_done_before = len(_events_of(state, "build_done"))
    n_dedup_before = len(_events_of(state, "build_skipped_idempotent"))
    # D17: скелеты, опубликованные ДО этой точки (обычный ранний нудж при
    # первом скане), к пути reconvert отношения не имеют — считаем дельту.
    n_skel_before = len(_events_of(state, "manifest_skeleton"))

    _drop_command(config.commands_dir(), _reconvert_cmd(book_id))
    dispatcher.drain_commands()

    man_rearmed = state.read_json(manifest_path)

    # re-arms → pending-confirm.
    check("reconvert: done book is re-armed to pending-confirm",
          man_rearmed.get("status") == "pending-confirm",
          f"status={man_rearmed.get('status')!r}")
    # fresh confirm_token (a re-arm rotates it, mirroring the scan).
    check("reconvert: confirm_token was refreshed (new value)",
          isinstance(man_rearmed.get("confirm_token"), str)
          and man_rearmed.get("confirm_token") != token0
          and len(man_rearmed.get("confirm_token", "")) >= 16,
          f"token_changed={man_rearmed.get('confirm_token') != token0}")
    # source_rev UNCHANGED (the inputs did not move — re-fingerprinting would lie).
    check("reconvert: source_rev is UNCHANGED (inputs did not move)",
          man_rearmed.get("source_rev") == source_rev0,
          f"rev_same={man_rearmed.get('source_rev') == source_rev0}")
    # THE fix: the idempotency ledger is CLEARED.
    check("reconvert: processed_keys is CLEARED (the dedup reset)",
          man_rearmed.get("processed_keys") == [],
          f"processed_keys={man_rearmed.get('processed_keys')}")
    # finished-build leftovers dropped → a clean pending book. Reconvert now writes a
    # BRAND-NEW manifest via the re-scan (it does not inherit the old one), so there is
    # no result/error/build to carry, and no progress key (a scan-built pending book
    # never carries progress — that field lives only on a converting showcase row).
    check("reconvert: no stale result/error/build markers, no lingering progress",
          man_rearmed.get("result") is None and man_rearmed.get("error") is None
          and man_rearmed.get("build") is None
          and (man_rearmed.get("progress") in (None, 0, 0.0)),
          f"result={man_rearmed.get('result')} error={man_rearmed.get('error')} "
          f"build={man_rearmed.get('build')} progress={man_rearmed.get('progress')}")
    # journalled.
    check("reconvert: a reconvert event was recorded",
          any(e.get("book_id") == book_id for e in _events_of(state, "reconvert")),
          f"events={_events_of(state, 'reconvert')}")
    # command consumed.
    check("reconvert: the reconvert command was consumed (none left on disk)",
          _reconvert_cmds_on_disk(config, state) == [],
          f"leftover={[p.name for p in _reconvert_cmds_on_disk(config, state)]}")
    # showcase re-projected: the book is now a pending row (left ГОТОВО).
    sc = state.read_state(default={})
    row = next((b for b in sc.get("books", []) if b.get("book_id") == book_id), None)
    check("reconvert: showcase row moved to pending-confirm (left ГОТОВО)",
          isinstance(row, dict) and row.get("status") == "pending-confirm",
          f"row={row}")

    # --- D17: «Собрать заново» отдаёт СРАЗУ полный манифест -------------------
    # Двухфазная публикация существует ради одного случая — только что дропнутой
    # книги, у окна которой человек сидит прямо сейчас. Reconvert не такой: книга
    # уже разобрана, окна никто не ждёт, и промежуточная фаза здесь была бы чистым
    # вредом — этот путь (`_write_manifest`, staged=False) не проходит через
    # resume, поэтому крах между двумя записями оставил бы книгу на `chapters` без
    # единого тика, который её достроит. Одна запись, окна нет.
    n_skeletons = len(_events_of(state, "manifest_skeleton")) - n_skel_before
    check("D17: reconvert публикует ГОТОВЫЙ манифест — фаза ready + build_token",
          scan.manifest_phase(man_rearmed) == "ready"
          and len(scan.manifest_build_token(man_rearmed)) == 32,
          f"phase={scan.manifest_phase(man_rearmed)} "
          f"token={bool(scan.manifest_build_token(man_rearmed))}")
    check("D17: reconvert НЕ публикует промежуточный скелет "
          "(этот путь не резюмируется — некому было бы достроить)",
          n_skeletons == 0, f"manifest_skeleton={n_skeletons}")
    check("D17: build_token перечеканен вместе с confirm_token (новая подготовка)",
          scan.manifest_build_token(man_rearmed) != build_token0,
          f"same={scan.manifest_build_token(man_rearmed) == build_token0}")
    stale_cmd = _confirm_build_cmd(man_rearmed)
    stale_cmd["build_token"] = build_token0          # эхо СТАРОГО токена
    stale_verdict = dispatcher.validate_command(stale_cmd, man_rearmed)
    check("D17: команда с ПРОШЛЫМ build_token отвергается (не собираем по устаревшему)",
          stale_verdict[0] == dispatcher.VERDICT_REJECT_NOT_READY
          and stale_verdict[1] == "build_token_mismatch", str(stale_verdict))

    # === PROVE the rebuild REALLY runs (not deduped) ==========================
    # Re-read the freshly re-armed manifest → the confirm-build uses the SAME
    # deterministic key (book_id:source_rev[:16]) that the first build recorded.
    # Because reconvert cleared the ledger, this MUST build to done (not skip).
    build_cmd2 = _confirm_build_cmd(man_rearmed)
    check("rebuild: the confirm key is the SAME as the first build's (dedup would bite)",
          build_cmd2["idempotency_key"] == the_key,
          f"key2={build_cmd2['idempotency_key']} key1={the_key}")
    # The prior .m4b is removed so its (re)appearance is unambiguous proof of a real
    # rebuild — not the leftover from the first build.
    build_m4b._unlink_quiet(out_path)
    _drop_command(config.commands_dir(), build_cmd2)
    dispatcher.drain_commands()

    man_rebuilt = state.read_json(manifest_path)
    n_started_after = len(_events_of(state, "build_started"))
    n_done_after = len(_events_of(state, "build_done"))
    n_dedup_after = len(_events_of(state, "build_skipped_idempotent"))

    check("rebuild: the book reached done again (rebuild completed)",
          man_rebuilt.get("status") == "done",
          f"status={man_rebuilt.get('status')!r} error={man_rebuilt.get('error')}")
    res = man_rebuilt.get("result") if isinstance(man_rebuilt.get("result"), dict) else {}
    rebuilt_out = res.get("output") or res.get("output_path")
    check("rebuild: a fresh .m4b exists on disk after the rebuild",
          isinstance(rebuilt_out, str) and rebuilt_out.endswith(".m4b")
          and Path(rebuilt_out).is_file() and Path(rebuilt_out).stat().st_size > 0,
          f"result={res}")
    # The decisive dedup-reset proof: a build_started/build_done pair fired for the
    # rebuild, and NO new build_skipped_idempotent was emitted for it.
    check("rebuild: a build_started + build_done pair fired (engine actually ran)",
          n_started_after == n_started_before + 1
          and n_done_after == n_done_before + 1,
          f"started {n_started_before}->{n_started_after} "
          f"done {n_done_before}->{n_done_after}")
    check("rebuild: NO new build_skipped_idempotent — the rebuild was NOT deduped",
          n_dedup_after == n_dedup_before,
          f"dedup {n_dedup_before}->{n_dedup_after}")
    check("rebuild: the key is back in the ledger after the successful rebuild",
          the_key in (man_rebuilt.get("processed_keys") or []),
          f"processed_keys={man_rebuilt.get('processed_keys')}")

    # === OLD-AGENT RE-PROBE: reconvert refills fields a stale manifest lacks =====
    # THE reconvert upgrade: a book built by an OLD agent carries a manifest WITHOUT
    # today's fields (e.g. source_samplerate — without it the confirm window's
    # «Как в источнике · N кГц» hint never shows). A plain in-place re-arm would keep
    # the gap; reconvert must RE-SCAN the sources and refill it. We emulate the old
    # agent by building a fresh book to done, then STRIPPING source_samplerate from
    # its manifest, and assert reconvert brings it back (from a real re-probe) AND the
    # rebuild still runs to build_done (dedup reset survives the re-scan).
    old_dir = watch / "Старый Агент - Книга"
    for i in range(1, 3):
        _make_silence_mp3(
            old_dir / f"{i:02d} - Глава {i}.mp3", seconds=2.0,
            tags={"title": f"Глава {i}", "album": "Старая",
                  "album_artist": "Тест"},
        )
    scan.run_scan()
    man_old0 = _manifest_for(config, state, "Старый Агент - Книга")
    assert man_old0 is not None, "scan did not arm the old-agent book"
    old_id = man_old0["book_id"]
    old_path = config.books_dir() / f"{old_id}.json"
    old_build_cmd = _confirm_build_cmd(man_old0)
    old_key = old_build_cmd["idempotency_key"]
    _drop_command(config.commands_dir(), old_build_cmd)
    dispatcher.drain_commands()
    assert state.read_json(old_path).get("status") == "done", "old-agent book not built"

    # Emulate the OLD manifest: drop the field a newer agent added. (A real old agent
    # simply never wrote it; deleting it reproduces that exact shape.)
    man_stale = state.read_json(old_path)
    real_sr = man_stale.get("source_samplerate")  # what a re-probe should recover
    man_stale.pop("source_samplerate", None)
    state.write_json_atomic(old_path, man_stale)
    check("old-agent: setup — the stale manifest is missing source_samplerate",
          "source_samplerate" not in state.read_json(old_path),
          f"keys_has_sr={'source_samplerate' in state.read_json(old_path)}")

    n_started_old = len(_events_of(state, "build_started"))
    n_done_old = len(_events_of(state, "build_done"))
    n_dedup_old = len(_events_of(state, "build_skipped_idempotent"))

    _drop_command(config.commands_dir(), _reconvert_cmd(old_id))
    dispatcher.drain_commands()
    man_reprobed = state.read_json(old_path)

    check("old-agent: reconvert re-armed the book to pending-confirm",
          man_reprobed.get("status") == "pending-confirm",
          f"status={man_reprobed.get('status')!r}")
    # THE assertion: source_samplerate is BACK (only a real re-probe can put it there).
    check("old-agent: source_samplerate REAPPEARS after reconvert (re-probe ran)",
          isinstance(man_reprobed.get("source_samplerate"), int)
          and man_reprobed.get("source_samplerate") > 0,
          f"source_samplerate={man_reprobed.get('source_samplerate')!r} "
          f"(expected the probed value {real_sr!r})")
    # The re-probe should recover the SAME rate the original scan measured (44100 for
    # the anullsrc fixtures) — proof it re-read the files, not fabricated a value.
    check("old-agent: the re-probed source_samplerate matches the sources (44100)",
          man_reprobed.get("source_samplerate") == real_sr
          and man_reprobed.get("source_samplerate") == 44100,
          f"reprobed={man_reprobed.get('source_samplerate')!r} original={real_sr!r}")
    # Chapters were rebuilt from a fresh probe too (real per-file durations present).
    chs = man_reprobed.get("chapters") or []
    check("old-agent: chapters were rebuilt from the re-probe (durations present)",
          len(chs) == 2 and all(isinstance(c.get("duration_ms"), int) for c in chs),
          f"chapters={[c.get('duration_ms') for c in chs]}")
    # The ledger was cleared by the re-scan → the rebuild must NOT dedup.
    check("old-agent: processed_keys cleared by the re-scan (dedup reset)",
          man_reprobed.get("processed_keys") == [],
          f"processed_keys={man_reprobed.get('processed_keys')}")

    # Prove the rebuild REALLY runs to done on the re-probed manifest (same key as the
    # first build — the dedup would bite if the re-scan hadn't cleared the ledger).
    reprobe_build = _confirm_build_cmd(man_reprobed)
    check("old-agent: the confirm key equals the first build's (dedup would bite)",
          reprobe_build["idempotency_key"] == old_key,
          f"key2={reprobe_build['idempotency_key']} key1={old_key}")
    _drop_command(config.commands_dir(), reprobe_build)
    dispatcher.drain_commands()
    man_old_rebuilt = state.read_json(old_path)
    check("old-agent: the re-probed book rebuilt to done (reached build_done)",
          man_old_rebuilt.get("status") == "done",
          f"status={man_old_rebuilt.get('status')!r} error={man_old_rebuilt.get('error')}")
    check("old-agent: a build_started + build_done pair fired for the rebuild",
          len(_events_of(state, "build_started")) == n_started_old + 1
          and len(_events_of(state, "build_done")) == n_done_old + 1,
          f"started+{len(_events_of(state, 'build_started')) - n_started_old} "
          f"done+{len(_events_of(state, 'build_done')) - n_done_old}")
    check("old-agent: NO new build_skipped_idempotent — the rebuild was NOT deduped",
          len(_events_of(state, "build_skipped_idempotent")) == n_dedup_old,
          f"dedup {n_dedup_old}->{len(_events_of(state, 'build_skipped_idempotent'))}")

    # === EDGE: source folder VANISHED → reject, stays done ===================
    # Build a SECOND book to done, delete its source, then reconvert it: the agent
    # must reject (source_missing) and NOT re-arm a book that could never build.
    gone_dir = watch / "Исчезнет - Книга"
    _make_silence_mp3(gone_dir / "01 - Глава.mp3", seconds=2.0,
                      tags={"title": "Глава", "album": "Исчезнет",
                            "album_artist": "Тест"})
    scan.run_scan()
    man_gone0 = _manifest_for(config, state, "Исчезнет - Книга")
    assert man_gone0 is not None, "scan did not arm the vanishing book"
    gone_id = man_gone0["book_id"]
    gone_path = config.books_dir() / f"{gone_id}.json"
    _drop_command(config.commands_dir(), _confirm_build_cmd(man_gone0))
    dispatcher.drain_commands()
    assert state.read_json(gone_path).get("status") == "done", "vanishing book not built"

    # Remove the source folder (the manifest lingers — the app hasn't re-scanned).
    shutil.rmtree(gone_dir, ignore_errors=True)
    _drop_command(config.commands_dir(), _reconvert_cmd(gone_id))
    dispatcher.drain_commands()
    man_gone_after = state.read_json(gone_path)
    gone_rejects = [e for e in _events_of(state, "reconvert_rejected")
                    if e.get("book_id") == gone_id
                    and e.get("reason") == "source_missing"]
    check("source-gone: reconvert is rejected with reason=source_missing",
          len(gone_rejects) >= 1, f"rejects={gone_rejects}")
    check("source-gone: the book stays done (NOT re-armed into a build that can't run)",
          man_gone_after.get("status") == "done",
          f"status={man_gone_after.get('status')!r}")
    check("source-gone: the reconvert command was consumed",
          _reconvert_cmds_on_disk(config, state) == [],
          f"leftover={[p.name for p in _reconvert_cmds_on_disk(config, state)]}")

    # === EDGE: status guard — reconvert a NON-done (pending) book is a no-op ==
    # The main book is now done again (rebuilt). Reconvert it back to pending, then
    # reconvert the ALREADY-pending book: it must be rejected (status_not_done) and
    # left exactly pending — no corruption.
    _drop_command(config.commands_dir(), _reconvert_cmd(book_id))
    dispatcher.drain_commands()
    assert state.read_json(manifest_path).get("status") == "pending-confirm"
    token_pending = state.read_json(manifest_path).get("confirm_token")

    _drop_command(config.commands_dir(), _reconvert_cmd(book_id))
    dispatcher.drain_commands()
    man_pending_after = state.read_json(manifest_path)
    status_rejects = [e for e in _events_of(state, "reconvert_rejected")
                      if e.get("book_id") == book_id
                      and e.get("reason") == "status_not_done"]
    check("status-guard: reconvert of a NON-done (pending) book is rejected (status_not_done)",
          len(status_rejects) >= 1, f"rejects={status_rejects}")
    check("status-guard: the pending book is untouched (status + token stable)",
          man_pending_after.get("status") == "pending-confirm"
          and man_pending_after.get("confirm_token") == token_pending,
          f"status={man_pending_after.get('status')!r} "
          f"token_stable={man_pending_after.get('confirm_token') == token_pending}")

    # === EDGE: bogus book_id → reject (manifest_missing), never crashes ======
    bogus_id = "deadbeefdeadbeef"
    _drop_command(config.commands_dir(), _reconvert_cmd(bogus_id))
    handled = dispatcher.drain_commands()
    bogus_rejects = [e for e in _events_of(state, "reconvert_rejected")
                     if e.get("book_id") == bogus_id
                     and e.get("reason") == "manifest_missing"]
    check("bogus-id: reconvert with an unknown book_id is rejected (manifest_missing)",
          len(bogus_rejects) >= 1, f"rejects={bogus_rejects}")
    check("bogus-id: the drain survived the bogus command (no crash, command consumed)",
          handled >= 1 and _reconvert_cmds_on_disk(config, state) == [],
          f"handled={handled} "
          f"leftover={[p.name for p in _reconvert_cmds_on_disk(config, state)]}")

    return _finish(root)


def _finish(root: Path) -> int:
    # Flat verification: this suite runs ONLY its own checks. Cross-suite
    # regression is orchestrated once by ``agent.selfcheck_all`` (no nested re-runs).
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    failed = [name for name, ok, _ in _RESULTS if not ok]
    print(f"\n§reconvert self-check: {passed}/{total} checks passed")
    if failed:
        print("  FAILED checks: " + "; ".join(failed))
    print(f"(temp tree left at {root} for inspection; safe to delete)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
