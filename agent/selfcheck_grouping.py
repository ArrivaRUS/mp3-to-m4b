"""§grouping self-check — loose mp3s in the watch ROOT → grouping prompt → choice.

Run it standalone:

    python3 -m agent.selfcheck_grouping

This proves the D1 "single-file grouping" layer end to end on a REAL temp tree with
REAL (tiny, ffmpeg-generated) mp3s, driving the PRODUCTION path
(scan → state pending-group → emulate a `grouping-choice` command →
:func:`dispatcher.drain_commands` → materialized manifest(s)):

  detect        loose mp3s in the watch root project a pending GROUP into
                state.json (stable group_id, rev/token, file names, count, total
                duration) — and the agent writes NO manifest for them yet.
  combine       a `grouping-choice` with choice=combine materializes EXACTLY ONE
                book whose chapters are the loose files in natural order, and the
                group leaves state.
  separate      choice=separate materializes EXACTLY N one-chapter books (book_id =
                hash of each single path, == scan.book_id_for), group leaves state.
  idempotent    a duplicate choice (same idempotency_key) does NOT materialize a
                second time.
  stale/forged  a choice with a wrong rev (stale) or wrong token/group_id (forged)
                is rejected and materializes nothing.
  coexist       a subfolder-book in the SAME watch keeps working alongside loose
                files (it gets its own pending-confirm manifest as before).

It runs ONLY its own checks (cross-suite regression is orchestrated once by
``agent.selfcheck_all`` — there is no nested re-run here). ffmpeg/ffprobe are
required (real mp3s); the run SKIPS (rc 1) if missing.
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


def _has_tools() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _make_mp3(path: Path, *, seconds: float = 1.0, tags: dict | None = None) -> None:
    """Write a real (cover-less) mp3 via an ffmpeg sine tone, with optional ID3."""
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


# --- command helper (mirrors how the app drops a grouping-choice) -----------


def _drop_command(commands_dir: Path, payload: dict) -> Path:
    commands_dir.mkdir(parents=True, exist_ok=True)
    cmd_id = payload.get("cmd_id") or str(uuid.uuid4())
    payload.setdefault("cmd_id", cmd_id)
    final = commands_dir / f"{cmd_id}.json"
    tmp = commands_dir / f".{cmd_id}.json.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


def _grouping_cmd(group: dict, choice: str, *, scan) -> dict:
    """A grouping-choice command for ``group`` (exactly the app's shape)."""
    gid = group["group_id"]
    rev = group["rev"]
    return {
        "cmd_id": str(uuid.uuid4()),
        "action": "grouping-choice",
        "group_id": gid,
        "rev": rev,
        "token": group["token"],
        "idempotency_key": scan.grouping_idempotency_key(gid, rev),
        "choice": choice,
        "ts": time.time(),
    }


def _pending_group(state) -> dict | None:
    s = state.read_state(default=None)
    if not isinstance(s, dict):
        return None
    groups = s.get("pending_groups")
    if isinstance(groups, list) and groups and isinstance(groups[0], dict):
        return groups[0]
    return None


def _manifests(config, state) -> list[dict]:
    out = []
    for p in sorted(config.books_dir().glob("*.json")):
        m = state.read_json(p)
        if isinstance(m, dict):
            out.append(m)
    return out


def _manifest_for_src(config, state, suffix: str) -> dict | None:
    for m in _manifests(config, state):
        if str(m.get("src_dir", "")).endswith(suffix):
            return m
    return None


# --- the run ----------------------------------------------------------------


def run() -> int:
    if not _has_tools():
        print("§grouping self-check: SKIPPED — ffmpeg/ffprobe not on PATH")
        return 1

    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-grouping-"))
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

    # === detect: loose mp3s in the watch ROOT (+ a subfolder book alongside) =====
    # Three loose files (out-of-order names to prove natural sort), all sharing an
    # album so combine can title from tags. Plus a normal subfolder book.
    _make_mp3(watch / "глава-2.mp3", seconds=1.0,
              tags={"title": "Глава вторая", "album": "Сборник рассказов",
                    "album_artist": "Чехов А.П.", "track": "2"})
    _make_mp3(watch / "глава-10.mp3", seconds=1.0,
              tags={"title": "Глава десятая", "album": "Сборник рассказов",
                    "album_artist": "Чехов А.П.", "track": "10"})
    _make_mp3(watch / "глава-1.mp3", seconds=1.0,
              tags={"title": "Глава первая", "album": "Сборник рассказов",
                    "album_artist": "Чехов А.П.", "track": "1"})

    sub = watch / "Толстой - Война и мир"
    _make_mp3(sub / "01 - Том 1.mp3", seconds=1.0,
              tags={"title": "Том 1", "album": "Война и мир"})

    scan.run_scan()

    group = _pending_group(state)
    check("detect: a pending group appears in state for the loose files",
          group is not None, f"group={'present' if group else 'MISSING'}")
    if group is None:
        return _finish(root)

    check("detect: group has a 16-hex group_id + rev + token",
          isinstance(group.get("group_id"), str) and len(group["group_id"]) == 16
          and bool(group.get("rev")) and bool(group.get("token")),
          f"id={group.get('group_id')!r} rev={str(group.get('rev'))[:8]}…")
    check("detect: group count = 3 loose files",
          group.get("count") == 3, f"count={group.get('count')}")
    check("detect: group files are natural-sorted (1,2,10), not lexicographic",
          group.get("files") == ["глава-1.mp3", "глава-2.mp3", "глава-10.mp3"],
          f"files={group.get('files')}")
    check("detect: group total_duration_ms ≈ 3×1s (>2.5s, <4s)",
          isinstance(group.get("total_duration_ms"), int)
          and 2500 <= group["total_duration_ms"] <= 4000,
          f"total_ms={group.get('total_duration_ms')}")

    # The agent must NOT have written a manifest for the loose files yet — only the
    # subfolder book has one.
    mans = _manifests(config, state)
    loose_manifest_now = any(
        os.path.abspath(str(watch)) == str(m.get("src_dir", "")) for m in mans
    )
    check("detect: NO book manifest for the loose set yet (awaits the choice)",
          not loose_manifest_now and len(mans) == 1,
          f"manifests={len(mans)} loose_manifest={loose_manifest_now}")

    # The subfolder book coexists: it has its own pending-confirm manifest.
    sub_man = _manifest_for_src(config, state, "Война и мир")
    check("coexist: the subfolder book got its own pending-confirm manifest",
          sub_man is not None and sub_man.get("status") == "pending-confirm"
          and len(sub_man.get("chapters", [])) == 1,
          f"sub={'present' if sub_man else 'MISSING'} "
          f"status={sub_man.get('status') if sub_man else '—'}")

    # === forged: wrong token / wrong group_id reject, materialize nothing ========
    bad_cmd = _grouping_cmd(group, "combine", scan=scan)
    bad_cmd["token"] = "deadbeef" * 4
    _drop_command(config.commands_dir(), bad_cmd)
    dispatcher.drain_commands()
    check("forged: wrong-token choice rejected (no manifest materialized)",
          not any(os.path.abspath(str(watch)) == str(m.get("src_dir", ""))
                  for m in _manifests(config, state))
          and _pending_group(state) is not None,
          "group still pending, no loose manifest")

    bad_gid = _grouping_cmd(group, "combine", scan=scan)
    bad_gid["group_id"] = "0" * 16
    bad_gid["idempotency_key"] = scan.grouping_idempotency_key("0" * 16, group["rev"])
    _drop_command(config.commands_dir(), bad_gid)
    dispatcher.drain_commands()
    check("forged: unknown group_id rejected (group_missing)",
          _pending_group(state) is not None
          and not any(os.path.abspath(str(watch)) == str(m.get("src_dir", ""))
                      for m in _manifests(config, state)),
          "group still pending")

    # === stale: wrong rev rejected =============================================
    stale = _grouping_cmd(group, "combine", scan=scan)
    stale["rev"] = "f" * 64
    _drop_command(config.commands_dir(), stale)
    dispatcher.drain_commands()
    check("stale: wrong-rev choice rejected (rejected_stale, nothing built)",
          _pending_group(state) is not None
          and not any(os.path.abspath(str(watch)) == str(m.get("src_dir", ""))
                      for m in _manifests(config, state)),
          "group still pending after stale reject")

    # === combine: ONE book of N chapters in natural order =======================
    group = _pending_group(state)  # refresh (token preserved across re-scans)
    combine_cmd = _grouping_cmd(group, "combine", scan=scan)
    _drop_command(config.commands_dir(), combine_cmd)
    dispatcher.drain_commands()

    # src_dir for the combined book is the watch dir itself.
    combined = next((m for m in _manifests(config, state)
                     if str(m.get("src_dir", "")) == os.path.abspath(str(watch))), None)
    check("combine: exactly ONE book materialized for the loose set",
          combined is not None, f"combined={'present' if combined else 'MISSING'}")
    if combined is not None:
        names = [c.get("name") for c in combined.get("chapters", [])]
        files = [c.get("file") for c in combined.get("chapters", [])]
        check("combine: the one book has 3 chapters in natural order",
              len(combined.get("chapters", [])) == 3
              and files == ["глава-1.mp3", "глава-2.mp3", "глава-10.mp3"],
              f"files={files}")
        check("combine: book status is pending-confirm (normal confirm path next)",
              combined.get("status") == "pending-confirm",
              f"status={combined.get('status')}")
        check("combine: title resolved from the shared album tag",
              combined.get("title") == "Сборник рассказов",
              f"title={combined.get('title')!r} author={combined.get('author')!r}")
        check("combine: a cover was resolved (PRD G4, even offline)",
              bool(combined.get("cover_options")) and combined.get("cover_selected"),
              f"options={len(combined.get('cover_options', []))} "
              f"selected={combined.get('cover_selected')!r}")
        _ = names  # (kept for debugging)

    check("combine: the pending group left state.json",
          _pending_group(state) is None, "group cleared")

    # === idempotent: a duplicate combine does NOT make a second book ============
    n_before = len(_manifests(config, state))
    dup = dict(combine_cmd); dup["cmd_id"] = str(uuid.uuid4())
    _drop_command(config.commands_dir(), dup)
    dispatcher.drain_commands()
    n_after = len(_manifests(config, state))
    check("idempotent: duplicate combine choice materializes nothing new",
          n_after == n_before, f"before={n_before} after={n_after}")
    check("idempotent: group does NOT re-appear (loose files linger in root)",
          _pending_group(state) is None,
          "resolved ledger suppresses the re-scan prompt")

    # === separate: a FRESH loose set → N one-chapter books ======================
    # New watch to keep separate's proof unambiguous (3 loose files → 3 books).
    root2 = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-grouping2-"))
    support2 = root2 / "support"; watch2 = root2 / "watch"
    support2.mkdir(parents=True); watch2.mkdir(parents=True)
    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support2)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch2)

    for nm, ttl in (("a.mp3", "Рассказ А"), ("b.mp3", "Рассказ Б"),
                    ("c.mp3", "Рассказ В")):
        _make_mp3(watch2 / nm, seconds=1.0, tags={"title": ttl})
    scan.run_scan()
    g2 = _pending_group(state)
    check("separate: a fresh loose set projects a new pending group",
          g2 is not None and g2.get("count") == 3,
          f"count={g2.get('count') if g2 else '—'}")
    if g2 is not None:
        sep_cmd = _grouping_cmd(g2, "separate", scan=scan)
        _drop_command(config.commands_dir(), sep_cmd)
        dispatcher.drain_commands()

        mans2 = _manifests(config, state)
        single_chapter = [m for m in mans2 if len(m.get("chapters", [])) == 1]
        check("separate: exactly 3 one-chapter books materialized",
              len(mans2) == 3 and len(single_chapter) == 3,
              f"manifests={len(mans2)} single_chapter={len(single_chapter)}")

        # book_id of each separate book == scan.book_id_for(<single file path>).
        ok_ids = True
        for nm in ("a.mp3", "b.mp3", "c.mp3"):
            expect = scan.book_id_for(watch2 / nm)
            got = next((m for m in mans2 if m.get("book_id") == expect), None)
            if got is None or len(got.get("chapters", [])) != 1:
                ok_ids = False
        check("separate: each book_id == scan.book_id_for(single path)", ok_ids,
              "per-file ids match book_id_for")
        check("separate: the pending group left state.json",
              _pending_group(state) is None, "group cleared")

    # --- summary ------------------------------------------------------------
    return _finish(root, extra_root=root2)


def _finish(root: Path, *, extra_root: Path | None = None) -> int:
    # Flat verification: this suite runs ONLY its own checks. Cross-suite
    # regression is orchestrated once by ``agent.selfcheck_all`` (no nested
    # re-runs here — that is what made a single pass take ~30 min).
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print(f"\n§grouping self-check: {passed}/{total} checks passed")
    print(f"(temp tree left at {root} for inspection; safe to delete)")
    if extra_root is not None:
        print(f"(second tree at {extra_root})")

    # Exit honestly: green ⇔ every local check passed.
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
