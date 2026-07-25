"""§skip self-check — «Пропустить»: снять книгу с обработки, но НЕ навсегда.

Run it standalone:

    python3 -m agent.selfcheck_skip

«Пропустить» (design/spec.md:108 `btn-skip`, design/flows.md:63, prd/PRD.md:261,
arch/plan-claude.md §2.3) takes a book OFF the pipeline: the agent marks its
manifest ``skipped``, the SOURCES ARE NEVER TOUCHED, and the scan stops offering
it. Two properties make or break the feature, and neither can be proven by a
compile-check — only by driving the real scan/dispatch on a throwaway tree:

  1. **the mark holds** — a skipped book must not come back on the next scan (or
     the button would be a lie), and it must not raise the app either;
  2. **the mark is NOT permanent** — a conscious RE-DROP of the same book must
     resurrect it. This is lesson ``.patches/004`` («намерение пользователя ≠
     новизна контента»): if the user puts the folder back, he wants it processed,
     and an app that stays silent is the exact bug we already fixed once. macOS
     has TWO drop shapes and they hit DIFFERENT mechanisms, so both are asserted
     separately:
       · COPY  → new inodes → new ``source_rev`` (v2 folds st_ino/st_dev) →
         ``scan._write_manifest`` re-arms on its own;
       · MOVE out→in → the inode SURVIVES, so only the presence ledger
         (``scan._reconcile_presence``) can see the gesture — ``skipped`` has to be
         in its re-arm set next to ``done``.

Assertions:
  marks            skip of a pending book → manifest ``skipped``, mp3s still on
                   disk, command consumed, nothing built.
  holds            a full ``run_scan`` after the skip leaves it ``skipped`` (the
                   showcase row too) and fires NO app-raise for it.
  re-drop COPY     the same folder removed and copied back (same path → same
                   book_id, fresh inodes) → back to ``pending-confirm`` WITHOUT
                   any command, and the app IS raised (the user's gesture is
                   answered).
  re-drop MOVE     the same folder moved out (a scan sees it gone), then moved
                   back → ``pending-confirm`` via ``book_rearmed_reappeared``.
  «Вернуть»        a ``reconvert`` for a skipped book re-arms it (fresh
                   confirm_token, cleared ledger) — this is how the queue's
                   ПРОПУЩЕНО row undoes a mis-click without a second protocol
                   action.
  error book       ``skip`` is accepted for an ``error`` book too
                   (design/flows.md:90 lists «Пропустить» among its actions).
  guards           skip of a ``converting`` book (use cancel) and of a ``done``
                   book → no-op reject ``status_not_skippable``, status untouched;
                   a bogus ``book_id`` → ``manifest_missing`` and no crash.

The app is NEVER opened: the raise goes through the ``MP3TOM4B_NUDGE_CMD``
recorder seam (a shell script that appends a line). Books are SHORT (a couple of
seconds of silence) — this suite never builds, it only needs real mp3s to probe.
Runs ONLY its own checks (cross-suite orchestration is ``agent.selfcheck_all``'s
job) and returns 0 ⇔ every check passed. Requires ffmpeg + ffprobe on PATH.
Writes only inside its temp tree.
"""

from __future__ import annotations

import json
import os
import shlex
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
    """Write a real (silent) mp3 of ``seconds`` virtual length via anullsrc."""
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


def _make_book(folder: Path, *, chapters: int = 3, album: str = "Книга") -> None:
    for i in range(1, chapters + 1):
        _make_silence_mp3(
            folder / f"{i:02d} - Глава {i}.mp3", seconds=1.0,
            tags={"title": f"Глава {i}", "album": album,
                  "album_artist": "Тест Пропуска"},
        )


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


def _skip_cmd(book_id: str) -> dict:
    """A skip command exactly as the app (EngineClient.makeSkip) drops it: no
    source_rev / confirm_token — it targets the book by id."""
    return {
        "cmd_id": str(uuid.uuid4()),
        "action": "skip",
        "book_id": book_id,
        "idempotency_key": f"skip:{book_id}",
        "ts": time.time(),
    }


def _reconvert_cmd(book_id: str) -> dict:
    """«Вернуть» on a ПРОПУЩЕНО row = the SAME reconvert the done row drops."""
    return {
        "cmd_id": str(uuid.uuid4()),
        "action": "reconvert",
        "book_id": book_id,
        "idempotency_key": f"reconvert:{book_id}",
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


def _row_for(state, book_id: str) -> dict | None:
    showcase = state.read_state(default=None) or {}
    for row in showcase.get("books", []) or []:
        if isinstance(row, dict) and row.get("book_id") == book_id:
            return row
    return None


def _nudge_count(nudge_log: Path) -> int:
    if not nudge_log.exists():
        return 0
    return len([ln for ln in nudge_log.read_text(encoding="utf-8").splitlines() if ln])


def _mp3s_intact(folder: Path, expected: int) -> bool:
    """The whole «исходники целы» promise, checked on disk."""
    return folder.is_dir() and len(sorted(folder.glob("*.mp3"))) == expected


def _finish(root: Path) -> int:
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    failed = [name for name, ok, _ in _RESULTS if not ok]
    print(f"\n§skip self-check: {passed}/{total} checks passed")
    if failed:
        print("  FAILED checks: " + "; ".join(failed))
    print(f"(temp tree left at {root} for inspection; safe to delete)")
    return 0 if passed == total else 1


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not _has_tools():
        print("§skip self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-skip-"))
    support = root / "support"
    watch = root / "watch"
    stash = root / "stash"          # outside the watch dir — re-drop source
    support.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)
    stash.mkdir(parents=True, exist_ok=True)

    # Recorder seam: every "app raise" appends a line — the REAL app never opens.
    nudge_log = root / "nudges.log"
    recorder = root / "recorder.sh"
    recorder.write_text(
        f"#!/bin/sh\nprintf 'nudge\\n' >> {shlex.quote(str(nudge_log))}\n",
        encoding="utf-8",
    )
    recorder.chmod(0o755)

    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)
    os.environ["MP3TOM4B_COVER_WEB"] = "0"             # offline determinism
    os.environ["MP3TOM4B_STABILITY_DEBOUNCE_S"] = "0"  # fixtures already stable
    os.environ["MP3TOM4B_NUDGE_CMD"] = shlex.quote(str(recorder))

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import config, dispatcher, scan, state  # noqa: E402

    print(f"self-check tree: {root}\n  support: {support}\n  watch:   {watch}\n")

    # === A. skip marks the book, sources untouched ============================
    book_a = watch / "Пропуск - Книга А"
    _make_book(book_a, chapters=3, album="Книга А")
    scan.run_scan()
    man_a = _manifest_for(config, state, "Пропуск - Книга А")
    assert man_a is not None, "scan did not arm book A"
    id_a = man_a["book_id"]
    check("setup: книга А поднялась в pending-confirm",
          man_a.get("status") == "pending-confirm", f"status={man_a.get('status')}")

    _drop_command(config.commands_dir(), _skip_cmd(id_a))
    dispatcher.drain_commands()   # returns commands HANDLED, not builds — see below
    man_a = state.read_json(config.books_dir() / f"{id_a}.json")
    check("skip: манифест помечен skipped",
          man_a.get("status") == "skipped", f"status={man_a.get('status')}")
    # I2 is about the ENGINE, so assert on the engine's own journal, not on the
    # drain count (which counts consumed command files, skip included).
    check("skip: skip НИЧЕГО не собирает (I2 — движок не запускался)",
          not _events_of(state, "build_started") and not _events_of(state, "build_done"),
          f"build_started={len(_events_of(state, 'build_started'))}")
    check("skip: исходники целы — все 3 mp3 на месте",
          _mp3s_intact(book_a, 3), f"files={len(sorted(book_a.glob('*.mp3')))}")
    check("skip: событие book_skipped записано",
          any(e.get("book_id") == id_a for e in _events_of(state, "book_skipped")))
    check("skip: команда съедена (очередь команд пуста)",
          not list(config.commands_dir().glob("*.json")))
    row_a = _row_for(state, id_a)
    check("skip: строка в state.json тоже skipped (секция ПРОПУЩЕНО увидит книгу)",
          isinstance(row_a, dict) and row_a.get("status") == "skipped",
          f"row={row_a.get('status') if row_a else None}")

    # === B. the mark HOLDS across a full scan, and does not raise the app =====
    nudges_before = _nudge_count(nudge_log)
    scan.run_scan()
    man_a = state.read_json(config.books_dir() / f"{id_a}.json")
    check("держится: полный run_scan НЕ поднимает пропущенную книгу заново",
          man_a.get("status") == "skipped", f"status={man_a.get('status')}")
    check("держится: пропущенная книга не дёргает приложение (0 всплытий)",
          _nudge_count(nudge_log) == nudges_before,
          f"{nudges_before} → {_nudge_count(nudge_log)}")
    scan.run_scan()
    man_a = state.read_json(config.books_dir() / f"{id_a}.json")
    check("держится: и на втором скане тоже skipped (не одноразовая удача)",
          man_a.get("status") == "skipped", f"status={man_a.get('status')}")

    # === C. RE-DROP by COPY (new inodes) resurrects it ========================
    # Isolate the source_rev mechanism: no scan runs while the folder is away, so
    # the presence ledger never sees it absent — only the inodes changed.
    token_before = man_a.get("confirm_token")
    shutil.copytree(book_a, stash / "А")          # copy2 → mtime/size preserved
    shutil.rmtree(book_a)
    shutil.copytree(stash / "А", book_a)          # same path → same book_id
    nudges_before = _nudge_count(nudge_log)
    scan.run_scan()
    man_a = state.read_json(config.books_dir() / f"{id_a}.json")
    check("ПЕРЕДРОП (копирование): книга вернулась в pending-confirm",
          man_a.get("status") == "pending-confirm", f"status={man_a.get('status')}")
    check("ПЕРЕДРОП (копирование): свежий confirm_token",
          man_a.get("confirm_token") and man_a.get("confirm_token") != token_before)
    check("ПЕРЕДРОП (копирование): приложение всплыло — жест человека отвечен",
          _nudge_count(nudge_log) > nudges_before,
          f"{nudges_before} → {_nudge_count(nudge_log)}")
    check("ПЕРЕДРОП (копирование): сработала смена source_rev, не presence-ledger",
          not any(e.get("book_id") == id_a
                  for e in _events_of(state, "book_rearmed_reappeared")))

    # === D. RE-DROP by MOVE (inode survives) resurrects it too ================
    book_b = watch / "Пропуск - Книга Б"
    _make_book(book_b, chapters=2, album="Книга Б")
    scan.run_scan()
    man_b = _manifest_for(config, state, "Пропуск - Книга Б")
    assert man_b is not None, "scan did not arm book B"
    id_b = man_b["book_id"]
    _drop_command(config.commands_dir(), _skip_cmd(id_b))
    dispatcher.drain_commands()
    scan.run_scan()                                # ledger records it PRESENT
    check("setup Б: книга пропущена и лежит пропущенной",
          state.read_json(config.books_dir() / f"{id_b}.json").get("status") == "skipped")

    # Measure the inode for real — the whole point of this case is that the
    # source_rev signal is BLIND here, so the presence ledger must carry it.
    ino_before = sorted(book_b.glob("*.mp3"))[0].stat().st_ino
    away = stash / "Б-унесли"
    shutil.move(str(book_b), str(away))            # MOVE out (inode survives)
    scan.run_scan()                                # ledger flips it to ABSENT
    shutil.move(str(away), str(book_b))            # MOVE back in
    ino_after = sorted(book_b.glob("*.mp3"))[0].stat().st_ino
    ino_same = ino_before == ino_after
    nudges_before = _nudge_count(nudge_log)
    scan.run_scan()
    man_b = state.read_json(config.books_dir() / f"{id_b}.json")
    check("ПЕРЕДРОП (перемещение): книга вернулась в pending-confirm",
          man_b.get("status") == "pending-confirm", f"status={man_b.get('status')}")
    check("ПЕРЕДРОП (перемещение): сработал presence-ledger (inode ИЗМЕРЕН, не менялся)",
          ino_same and any(e.get("book_id") == id_b
                           for e in _events_of(state, "book_rearmed_reappeared")),
          f"inode {ino_before} → {ino_after}")
    check("ПЕРЕДРОП (перемещение): приложение всплыло",
          _nudge_count(nudge_log) > nudges_before,
          f"{nudges_before} → {_nudge_count(nudge_log)}")

    # === E. «Вернуть» — undo a mis-click without touching the folder ==========
    book_c = watch / "Пропуск - Книга В"
    _make_book(book_c, chapters=2, album="Книга В")
    scan.run_scan()
    man_c = _manifest_for(config, state, "Пропуск - Книга В")
    assert man_c is not None, "scan did not arm book C"
    id_c = man_c["book_id"]
    _drop_command(config.commands_dir(), _skip_cmd(id_c))
    dispatcher.drain_commands()
    token_c = state.read_json(config.books_dir() / f"{id_c}.json").get("confirm_token")
    check("setup В: книга пропущена",
          state.read_json(config.books_dir() / f"{id_c}.json").get("status") == "skipped")

    _drop_command(config.commands_dir(), _reconvert_cmd(id_c))
    dispatcher.drain_commands()
    man_c = state.read_json(config.books_dir() / f"{id_c}.json")
    check("«Вернуть»: reconvert пропущенной книги → pending-confirm",
          man_c.get("status") == "pending-confirm", f"status={man_c.get('status')}")
    check("«Вернуть»: свежий confirm_token + пустой ledger идемпотентности",
          man_c.get("confirm_token") != token_c and man_c.get("processed_keys") == [])
    check("«Вернуть»: исходники не тронуты",
          _mp3s_intact(book_c, 2))

    # === F. skip is accepted for an ERROR book (design/flows.md:90) ===========
    book_d = watch / "Пропуск - Книга Г"
    _make_book(book_d, chapters=1, album="Книга Г")
    scan.run_scan()
    man_d = _manifest_for(config, state, "Пропуск - Книга Г")
    assert man_d is not None, "scan did not arm book D"
    id_d = man_d["book_id"]
    path_d = config.books_dir() / f"{id_d}.json"
    man_d["status"] = "error"                      # fixture: emulate a failed build
    man_d["error"] = {"reason": "test"}
    state.write_json_atomic(path_d, man_d)
    _drop_command(config.commands_dir(), _skip_cmd(id_d))
    dispatcher.drain_commands()
    check("error-книга: «Пропустить» принимается и для неё",
          state.read_json(path_d).get("status") == "skipped",
          f"status={state.read_json(path_d).get('status')}")

    # === G. guards — statuses that must NOT be skippable ======================
    for status, label in (("converting", "в сборке (для этого есть «Отмена»)"),
                          ("done", "уже собранной")):
        man_c = state.read_json(config.books_dir() / f"{id_c}.json")
        man_c["status"] = status                   # fixture: force the status under test
        state.write_json_atomic(config.books_dir() / f"{id_c}.json", man_c)
        _drop_command(config.commands_dir(), _skip_cmd(id_c))
        dispatcher.drain_commands()
        after = state.read_json(config.books_dir() / f"{id_c}.json")
        rejects = [e for e in _events_of(state, "skip_rejected")
                   if e.get("book_id") == id_c
                   and e.get("reason") == "status_not_skippable"
                   and e.get("status") == status]
        check(f"guard: «Пропустить» для книги {label} — отказ, статус не тронут",
              after.get("status") == status and rejects,
              f"status={after.get('status')} rejects={len(rejects)}")

    _drop_command(config.commands_dir(), _skip_cmd("нет-такой-книги"))
    dispatcher.drain_commands()
    bogus = [e for e in _events_of(state, "skip_rejected")
             if e.get("book_id") == "нет-такой-книги"
             and e.get("reason") == "manifest_missing"]
    check("guard: «Пропустить» с несуществующим book_id — отказ, без падения",
          bool(bogus) and not list(config.commands_dir().glob("*.json")))

    return _finish(root)


if __name__ == "__main__":
    sys.exit(run())
