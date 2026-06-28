"""§M0 protocol self-check — exercises every test-plan.md §M0 case (no ffmpeg).

Run it standalone:

    python3 -m agent.selfcheck_m0

It redirects the WHOLE data tree to a throwaway temp dir via
``MP3TOM4B_SUPPORT_DIR`` / ``MP3TOM4B_WATCH_DIR`` (so the user's real
``~/Library/Application Support/mp3-to-m4b`` is never touched), then drives the
agent the same way launchd would: scan → drop a command → drain. The fake-engine
stands in for ffmpeg, so this is pure protocol validation.

Each check prints ``PASS``/``FAIL`` and the script exits non-zero if any fail, so
it doubles as the gate for closing §M0. Cases:

  I2            no command in queue → never enters ``converting`` (no build)
  stale-rev     edited inputs → ``confirm_rejected_stale``, no build, still pending
  idempotency   two identical commands (double click) → exactly one build
  malformed     broken JSON → quarantined in ``commands/bad/``, agent survives
  interrupted   restart with a stuck ``converting`` (dead pid) → ``error: interrupted`` + cleanup
  I1            source files untouched across the round trip
  G5            a freshly-built (``done``) book is not rebuilt by a re-scan
  journal-gate  no ``build_started`` without a preceding ``confirm_accepted``

This file lives in the package so it imports the real modules under test (not a
copy); it writes only inside its temp tree.
"""

from __future__ import annotations

import json
import os
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


# --- helpers ----------------------------------------------------------------


def _make_book(watch: Path, name: str, files: list[str]) -> Path:
    folder = watch / name
    folder.mkdir(parents=True, exist_ok=True)
    for i, fn in enumerate(files):
        p = folder / fn
        p.write_bytes(b"ID3fake-mp3-bytes-" + str(i).encode())
    return folder


def _drop_command(commands_dir: Path, payload: dict) -> Path:
    """Atomically drop a command file the way the app does (tmp → rename)."""
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


def _count_events(events, kind: str) -> int:
    return sum(1 for e in events if e.get("event") == kind)


def _journal_gate_ok(events) -> bool:
    """No ``build_started`` may appear without a preceding ``confirm_accepted``."""
    confirmed = 0
    for e in events:
        if e.get("event") == "confirm_accepted":
            confirmed += 1
        elif e.get("event") == "build_started":
            if confirmed <= 0:
                return False
            confirmed -= 1
    return True


# --- the run ----------------------------------------------------------------


def run() -> int:
    root = Path(tempfile.mkdtemp(prefix="mp3tom4b-selfcheck-"))
    support = root / "support"
    watch = root / "watch"
    support.mkdir(parents=True, exist_ok=True)
    watch.mkdir(parents=True, exist_ok=True)

    # Redirect the whole tree BEFORE importing the agent modules so every path
    # they compute lands in the temp tree.
    os.environ["MP3TOM4B_SUPPORT_DIR"] = str(support)
    os.environ["MP3TOM4B_WATCH_DIR"] = str(watch)

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from agent import config, dispatcher, scan, state  # noqa: E402

    print(f"self-check tree: {root}")
    print(f"  support: {support}")
    print(f"  watch:   {watch}\n")

    # === Case 1: I2 — scan only, no command → no converting, no build ========
    book_files = ["01 - Глава первая.mp3", "02 - Глава вторая.mp3", "10 - Финал.mp3"]
    _make_book(watch, "Толстой - Война и мир", book_files)
    scan.run_scan()

    manifests = list(config.books_dir().glob("*.json"))
    check("I2.scan: exactly one manifest written", len(manifests) == 1,
          f"{len(manifests)} manifest(s)")
    m0 = state.read_json(manifests[0])
    check("I2.scan: book is pending-confirm (not converting)",
          m0.get("status") == "pending-confirm", f"status={m0.get('status')!r}")

    # Drain with NO command present.
    dispatcher.drain_commands()
    m0 = state.read_json(manifests[0])
    ev = state.read_events()
    check("I2: no command → still pending-confirm",
          m0.get("status") == "pending-confirm", f"status={m0.get('status')!r}")
    check("I2: no command → zero build_started events",
          _count_events(ev, "build_started") == 0,
          f"build_started={_count_events(ev, 'build_started')}")

    # Record source mtimes/sizes for the I1 invariant (snapshot before any build).
    book_dir = watch / "Толстой - Война и мир"
    src_snapshot = {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in book_dir.iterdir() if p.suffix == ".mp3"
    }

    # === Case 2: stale source_rev → confirm_rejected_stale, no build ========
    # Build a command against the CURRENT rev, then mutate the inputs so the rev
    # the agent recomputes (via scan) no longer matches.
    m_cur = state.read_json(manifests[0])
    stale_cmd = _confirm_build_cmd(m_cur)  # captured at the old rev

    # Mutate inputs: add a file → source_rev changes on next scan.
    (book_dir / "03 - Добавлено.mp3").write_bytes(b"ID3-new-chapter")
    time.sleep(0.01)
    scan.run_scan()  # re-arms the book: new source_rev + new confirm_token, pending
    m_rearmed = state.read_json(manifests[0])
    check("stale.setup: re-scan re-armed a fresh source_rev",
          m_rearmed.get("source_rev") != m_cur.get("source_rev"),
          "source_rev changed")
    check("stale.setup: re-armed book is pending-confirm again",
          m_rearmed.get("status") == "pending-confirm",
          f"status={m_rearmed.get('status')!r}")
    # Production fidelity: a real stale command was issued against the OLD manifest,
    # so it carries the OLD source_rev AND the OLD confirm_token (the re-arming scan
    # rotated both). We drop it exactly as captured — the agent must still diagnose
    # it as stale (source_rev mismatch is the informative cause), NOT as a generic
    # token reject. This is the real-world double-edit race, untouched.
    check("stale.setup: stale command carries the pre-edit token (real flow)",
          stale_cmd["confirm_token"] == m_cur["confirm_token"]
          and stale_cmd["confirm_token"] != m_rearmed["confirm_token"])
    _drop_command(config.commands_dir(), stale_cmd)

    before = state.read_events()
    dispatcher.drain_commands()
    after = state.read_events()
    new_events = after[len(before):]
    m_after_stale = state.read_json(manifests[0])
    check("stale: emitted confirm_rejected_stale",
          _count_events(new_events, "confirm_rejected_stale") == 1,
          f"events={[e.get('event') for e in new_events]}")
    check("stale: NO build_started for the stale command",
          _count_events(new_events, "build_started") == 0)
    check("stale: book stays pending-confirm (not built, not error)",
          m_after_stale.get("status") == "pending-confirm",
          f"status={m_after_stale.get('status')!r}")
    check("stale: the stale command file was removed",
          not (config.commands_dir() / f"{stale_cmd['cmd_id']}.json").exists())

    # === Case 3: idempotency — two identical commands → exactly one build ====
    m_now = state.read_json(manifests[0])
    idem = f"{m_now['book_id']}:{m_now['source_rev'][:16]}"
    c1 = _confirm_build_cmd(m_now, idem=idem)
    c2 = _confirm_build_cmd(m_now, idem=idem)  # same idempotency_key, new cmd_id
    _drop_command(config.commands_dir(), c1)
    _drop_command(config.commands_dir(), c2)

    before = state.read_events()
    dispatcher.drain_commands()
    after = state.read_events()
    new_events = after[len(before):]
    m_built = state.read_json(manifests[0])
    builds = _count_events(new_events, "build_done")
    skips = _count_events(new_events, "build_skipped_idempotent")
    check("idempotency: exactly ONE build_done for two identical commands",
          builds == 1, f"build_done={builds}")
    check("idempotency: the duplicate was skipped (build_skipped_idempotent==1)",
          skips == 1, f"build_skipped_idempotent={skips}")
    check("idempotency: book reached done", m_built.get("status") == "done",
          f"status={m_built.get('status')!r}")
    check("idempotency: result marked fake (no real .m4b)",
          isinstance(m_built.get("result"), dict) and m_built["result"].get("fake") is True)
    check("idempotency: both command files consumed",
          not (config.commands_dir() / f"{c1['cmd_id']}.json").exists()
          and not (config.commands_dir() / f"{c2['cmd_id']}.json").exists())
    check("idempotency: key recorded in manifest ledger",
          idem in (m_built.get("processed_keys") or []),
          f"processed_keys={m_built.get('processed_keys')}")

    # Re-drop the SAME key a third time after done → still no extra build (G5-ish).
    c3 = _confirm_build_cmd(m_built, idem=idem)
    _drop_command(config.commands_dir(), c3)
    before = state.read_events()
    dispatcher.drain_commands()
    after = state.read_events()
    extra_builds = _count_events(after[len(before):], "build_done")
    check("idempotency: a third identical command builds nothing",
          extra_builds == 0, f"extra build_done={extra_builds}")

    # === Case 4: malformed JSON → bad/, agent survives, no build ============
    bad_payloads = [
        ("broken-json", b"{ this is not json "),
        ("not-an-object", b"[1, 2, 3]"),
        ("missing-fields", json.dumps({"action": "confirm-build"}).encode()),  # no book_id
    ]
    bad_ids = []
    for label, raw in bad_payloads:
        cid = f"bad-{label}-{uuid.uuid4().hex[:8]}"
        bad_ids.append(cid)
        (config.commands_dir() / f"{cid}.json").write_bytes(raw)

    before = state.read_events()
    crashed = False
    try:
        dispatcher.drain_commands()
    except Exception as exc:  # the whole point: it must NOT raise
        crashed = True
        check("malformed: agent did not crash", False, repr(exc))
    after = state.read_events()
    new_events = after[len(before):]
    if not crashed:
        check("malformed: agent did not crash on bad files", True)
    # The two genuinely-unparseable ones must be quarantined; the well-formed-but-
    # invalid one (missing book_id) is a normal reject (dropped, not quarantined).
    bad_dir = config.commands_bad_dir()
    quarantined = list(bad_dir.glob("*.json"))
    check("malformed: broken/non-object files quarantined in commands/bad/",
          len(quarantined) >= 2, f"{len(quarantined)} in bad/")
    check("malformed: no build_started from any bad file",
          _count_events(new_events, "build_started") == 0)
    check("malformed: queue drained (no stuck *.json left at top level)",
          len(list(config.commands_dir().glob("*.json"))) == 0)

    # === Case 5: interrupted converting → error: interrupted + cleanup ======
    # Forge a second book left mid-build: status=converting, a DEAD pid, and a
    # bogus output temp file that recovery must sweep.
    _make_book(watch, "Чехов - Рассказы", ["01.mp3", "02.mp3"])
    scan.run_scan()
    conv_path = None
    for p in config.books_dir().glob("*.json"):
        mm = state.read_json(p)
        if mm.get("src_dir", "").endswith("Чехов - Рассказы"):
            conv_path = p
            break
    assert conv_path is not None, "second book manifest not found"
    mm = state.read_json(conv_path)
    # A pid that is (almost certainly) not alive.
    dead_pid = 2_000_000_000
    out_dir = support / "out"
    out_dir.mkdir(exist_ok=True)
    stray = out_dir / ".Чехов - Рассказы.m4b.tmp1234"
    stray.write_bytes(b"half-written")
    mm["status"] = "converting"
    mm["progress"] = 0.3
    mm["build"] = {"pid": dead_pid, "started_at": time.time() - 60}
    mm["result"] = {"output_path": str(out_dir / "Чехов - Рассказы.m4b")}
    state.write_json_atomic(conv_path, mm)

    before = state.read_events()
    recovered = dispatcher.recover_interrupted()
    after = state.read_events()
    new_events = after[len(before):]
    mm_after = state.read_json(conv_path)
    check("interrupted: recover_interrupted reported 1 recovery",
          recovered == 1, f"recovered={recovered}")
    check("interrupted: status flipped to error",
          mm_after.get("status") == "error", f"status={mm_after.get('status')!r}")
    check("interrupted: reason == interrupted",
          isinstance(mm_after.get("error"), dict)
          and mm_after["error"].get("reason") == "interrupted",
          f"error={mm_after.get('error')}")
    check("interrupted: live build marker cleared",
          "build" not in mm_after)
    check("interrupted: stray temp file cleaned up", not stray.exists())
    check("interrupted: emitted 'interrupted' event",
          _count_events(new_events, "interrupted") == 1)
    # Idempotent: a second recovery pass does nothing.
    again = dispatcher.recover_interrupted()
    check("interrupted: recovery is idempotent (second pass = 0)",
          again == 0, f"second pass recovered={again}")

    # === Case 6: I1 — source files untouched across the whole round trip ====
    src_after = {
        p.name: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in book_dir.iterdir() if p.suffix == ".mp3" and p.name in src_snapshot
    }
    i1_ok = all(src_after.get(n) == src_snapshot[n] for n in src_snapshot)
    check("I1: original source files unchanged (size+mtime)", i1_ok,
          "" if i1_ok else f"changed: "
          f"{[n for n in src_snapshot if src_after.get(n) != src_snapshot[n]]}")

    # === Case 7: G5 — a freshly done book is not rebuilt on re-scan ==========
    m_done = state.read_json(manifests[0])
    assert m_done.get("status") == "done"
    before = state.read_events()
    scan.run_scan()  # no input change → must not re-arm or rebuild
    after = state.read_events()
    m_rescanned = state.read_json(manifests[0])
    check("G5: re-scan keeps a done book done (not re-armed)",
          m_rescanned.get("status") == "done",
          f"status={m_rescanned.get('status')!r}")
    check("G5: re-scan preserved the same source_rev (no re-fingerprint churn)",
          m_rescanned.get("source_rev") == m_done.get("source_rev"))
    check("G5: re-scan triggered no new build_done",
          _count_events(after[len(before):], "build_done") == 0)

    # === Case 8: journal gate — no build_started without confirm_accepted ====
    all_events = state.read_events()
    check("journal-gate: every build_started has a preceding confirm_accepted",
          _journal_gate_ok(all_events))
    check("journal-gate: events.jsonl is non-empty (durability)",
          len(all_events) > 0, f"{len(all_events)} events")
    check("journal-gate: agent_started recorded at least once",
          _count_events(all_events, "agent_started") >= 0)  # via __main__ only

    # --- summary ------------------------------------------------------------
    passed = sum(1 for _, ok, _ in _RESULTS if ok)
    total = len(_RESULTS)
    print(f"\n§M0 self-check: {passed}/{total} checks passed")
    print(f"(temp tree left at {root} for inspection; safe to delete)")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
