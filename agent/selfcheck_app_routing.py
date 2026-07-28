"""§app-routing self-check — WHICH BOOK does the confirm window present?

Run it standalone:

    python3 -m agent.selfcheck_app_routing

The bug (shipped in v0.9, third of its class — lesson 005 "видимый кликабельный
контрол ОБЯЗАН работать"): the queue's «Подтвердить» on a ROW navigated to the
confirm window but THREW THE ROW'S BOOK AWAY (``onConfirm: { _ in … }``), so the
window always rendered ``activeBooks.first``. Confirming the second book opened
the first one's window — the user edited metadata/cover/quality and pressed
«Собрать» believing it was the book they picked, and a different book got built.

Since 2026-07-27 the same suite also guards WHETHER THE WINDOW IS SHOWN AT ALL —
the escalation ladder in ``app/WindowPresentation.swift`` (.patches/006). Ordering
a window is not showing it: on macOS 26 activation is cooperative, a
programmatically launched app is refused it, and the confirm window sat fully
occluded for 85 measured seconds while the user saw «ничего не произошло». Same
shape as the routing rule — the decision is a pure function, so it is unit-checked
exhaustively (4 rungs × 4 fact combinations), while the AppKit calls it drives are
not checkable here and are guarded structurally.

The routing rule now lives in ONE pure place, ``ShowcaseState.presentedBook``
(app/StateModel.swift), which both the queue pick and the agent's auto-surface go
through. This suite is a thin runner: it compiles the Foundation-only app sources
together with ``app/selfcheck_routing.swift`` (the assertions themselves — Swift,
because the contract is Swift) and surfaces that binary's verdict. The Swift file
is NOT part of the shipped app: build/build-app.sh lists its sources explicitly.

Since 2026-07-28 (D17 «ранний нудж», M-D) the same suite also guards the app's
half of the two-phase confirm protocol, in TWO stages:

  · **значением** — the pure rules in `app/selfcheck_routing.swift`: the build gate
    reads `build_token` and nothing else; `cover_web` is a LABEL that must never
    reach that gate (M-B measured 0.022 s between a live and a dead network at the
    button — wiring the flag in would hand that back); the whole pristine/dirty
    matrix of `ConfirmMerge` (SwiftUI keeps `@State` across a body update, so the
    skeleton's file-name title would otherwise outlive the real ID3 one); and the
    `NudgeEdge` keys that make «один подъём окна на публикацию» structural. Three
    of those are guarded by READING the sources, because a pure rule can be
    flawless while the host stops asking it — including one that reads
    `agent/scan.py` and rebuilds the agent's own ledger-key format from it, so the
    byte-for-byte mirror is a check rather than a comment.
  · **проводом** — `app/selfcheck_wire.swift`: a confirm-build command written by
    the REAL `EngineClient` and judged by the REAL
    `agent.dispatcher.validate_command`, closing the seam where each side is
    internally consistent and the two disagree. Headline case: I1/TOCTOU.

It touches no state tree, no launchd, no ffmpeg — it is a pure unit check of
value-level rules plus one file handshake, so it is grouped with the other
lightweight suites. The single «X/Y checks passed» line covers BOTH stages.

``swiftc`` (Xcode command line tools) is REQUIRED: build/build-app.sh needs it
anyway, and a missing toolchain is reported as a FAILURE, never a silent green
(the ``selfcheck_all`` contract).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

MARKER = "§app-routing self-check:"

REPO = Path(__file__).resolve().parent.parent

# The app sources the check needs (StateModel pulls the status markers in via
# loadState). Mostly Foundation-only value code; `Tokens.swift` +
# `FolderAccessCard.swift` import SwiftUI, which compiles and links fine in a
# command-line binary as long as nothing instantiates an NSApplication — and it is
# worth the two extra seconds: it means the M6 assertions run against the EXACT
# strings and rules the shipped card renders, not a parallel copy of them that can
# drift away from what the user sees.
SOURCES = [
    REPO / "app" / "StateModel.swift",
    REPO / "app" / "WindowGeometry.swift",
    REPO / "app" / "WindowPresentation.swift",
    REPO / "app" / "EngineClient.swift",
    REPO / "app" / "EngineClient+Status.swift",
    REPO / "app" / "Tokens.swift",
    REPO / "app" / "FolderAccessCard.swift",
    REPO / "app" / "selfcheck_routing.swift",
]


# --- WIRE stage (D17/M-D) ----------------------------------------------------
# The value checks above and ``agent.selfcheck_early`` each guard ONE side of the
# confirm protocol: the app's rules, and the agent's gate on commands this repo's
# own python mints. The seam between them is exactly where a protocol bug hides —
# the app could write a differently-shaped command (wrong key, wrong build_token
# for a skeleton) and both sides would stay green.
#
# So this stage closes the loop with no simulation on either end: the command is
# written by the REAL ``EngineClient.writeConfirmBuild`` (a tiny Foundation binary
# built from ``app/selfcheck_wire.swift`` + the same app sources) and judged by the
# REAL ``agent.dispatcher.validate_command``. Its headline case is I1/TOCTOU: the
# command the app mints while looking at a SKELETON stays rejected even after the
# very same ``source_rev`` has been finalized with the very same ``confirm_token``.
WIRE_SOURCES = [p for p in SOURCES if p.name != "selfcheck_routing.swift"] + [
    REPO / "app" / "selfcheck_wire.swift",
]

#: Exactly the keys ``dispatcher`` expects on a confirm-build command.
_COMMAND_KEYS = {"cmd_id", "action", "book_id", "source_rev", "confirm_token",
                 "build_token", "idempotency_key", "params", "ts"}


def _wire_manifest(**over) -> dict:
    manifest = {
        "book_id": "b1", "src_dir": "/tmp/src", "status": "pending-confirm",
        "source_rev": "a" * 64, "source_rev_v": 2, "confirm_token": "c" * 32,
        "title": "Война и мир", "author": "Лев Толстой",
        "chapters": [{"index": 1, "file": "01.mp3", "name": "Глава 1",
                      "duration_ms": None}],
        "total_duration_ms": 0, "cover_state": "none", "cover_preview": None,
        "params": {"bitrate": 192, "channels": "stereo", "samplerate": None,
                   "split": False, "split_threshold_mb": 300, "build_mode": "fast"},
        "processed_keys": [],
    }
    manifest.update(over)
    return manifest


def _wire_stage(binary: Path, results: list) -> None:
    """Run the app→agent wire checks, appending ``(name, ok, detail)`` to results."""
    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, bool(ok), detail))
        line = f"  [{'PASS' if ok else 'FAIL'}] {name}"
        if detail:
            line += f" — {detail}"
        print(line)

    sys.path.insert(0, str(REPO))
    from agent import dispatcher  # noqa: PLC0415 — needs REPO on sys.path first

    with tempfile.TemporaryDirectory(prefix="mp3tom4b-selfcheck-wire-") as tmp:
        root = Path(tmp)
        books = root / "queue" / "books"
        books.mkdir(parents=True, exist_ok=True)

        def app_writes_command(manifest: dict) -> tuple[dict | None, str]:
            """Приложение (настоящий EngineClient) кладёт confirm-build на диск."""
            (books / f"{manifest['book_id']}.json").write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            proc = subprocess.run([str(binary), str(root), manifest["book_id"]],
                                  capture_output=True, text=True)
            if proc.returncode != 0:
                return None, proc.stderr.strip()
            path = Path(proc.stdout.strip())
            try:
                return json.loads(path.read_text(encoding="utf-8")), proc.stderr.strip()
            except OSError as exc:
                return None, f"{proc.stderr.strip()} / {exc!r}"

        # 1. СКЕЛЕТ: build_token физически нет ни в манифесте, ни в команде.
        skeleton = _wire_manifest(phase="skeleton")
        cmd_from_skeleton, note = app_writes_command(skeleton)
        check("провод: приложение видит скелет как несобираемый",
              "isBuildReady=false" in note, note)
        check("провод: команда, отчеканенная по скелету, несёт ПУСТОЙ build_token",
              isinstance(cmd_from_skeleton, dict)
              and cmd_from_skeleton.get("build_token") == "",
              repr((cmd_from_skeleton or {}).get("build_token")))
        verdict, reason = dispatcher.validate_command(cmd_from_skeleton or {}, skeleton)
        check("провод: гейт агента отвергает команду по скелету",
              verdict == dispatcher.VERDICT_REJECT_NOT_READY
              and "manifest_not_ready" in reason, f"{verdict}/{reason}")

        # 2. READY: токен есть, команда принимается — иначе всё выше зеленело бы
        #    просто оттого, что провод не работает вовсе.
        ready = _wire_manifest(
            phase="ready", build_token="d" * 32, total_duration_ms=1000,
            chapters=[{"index": 1, "file": "01.mp3", "name": "Глава 1",
                       "duration_ms": 1000}])
        cmd_ready, note2 = app_writes_command(ready)
        check("провод: приложение видит ready как собираемый",
              "isBuildReady=true" in note2, note2)
        check("провод: команда по ready несёт ТОТ САМЫЙ build_token",
              (cmd_ready or {}).get("build_token") == "d" * 32)
        verdict, reason = dispatcher.validate_command(cmd_ready or {}, ready)
        check("провод: гейт агента ПРИНИМАЕТ команду по ready",
              verdict == dispatcher.VERDICT_ACCEPT, f"{verdict}/{reason}")

        # 3. I1/TOCTOU — дыра, ради которой build_token вообще существует.
        check("провод: сценарий TOCTOU выстроен корректно (тот же rev и confirm_token)",
              (cmd_from_skeleton or {}).get("source_rev") == ready["source_rev"]
              and (cmd_from_skeleton or {}).get("confirm_token") == ready["confirm_token"])
        verdict, reason = dispatcher.validate_command(cmd_from_skeleton or {}, ready)
        check("провод I1: команда, рождённая по скелету, отвергнута И ПОСЛЕ "
              "финализации того же source_rev",
              verdict == dispatcher.VERDICT_REJECT_NOT_READY
              and "build_token" in reason, f"{verdict}/{reason}")

        # 4. Форма провода не разъехалась.
        check("провод: набор ключей команды = контракт диспетчера",
              set(cmd_ready or {}) == _COMMAND_KEYS,
              str(sorted(set(cmd_ready or {}) ^ _COMMAND_KEYS)))
        check("провод: команда легла в queue/commands/ (её найдёт дренаж)",
              any((root / "queue" / "commands").glob("*.json")))
        check("провод: приложение НЕ трогает манифест агента",
              json.loads((books / "b1.json").read_text(encoding="utf-8")) == ready)


def _fail(reason: str) -> int:
    """Report a runner-level failure in the suites' own summary format."""
    print(f"  [FAIL] {reason}")
    print(f"\n{MARKER} 0/1 checks passed")
    print(f"  FAILED checks: {reason}")
    return 1


def _compile(sources: list, binary: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["xcrun", "swiftc", *[str(p) for p in sources], "-o", str(binary)],
        capture_output=True, text=True,
    )


def run() -> int:
    missing = [str(p) for p in SOURCES + WIRE_SOURCES if not p.is_file()]
    if missing:
        return _fail("missing swift sources: " + ", ".join(sorted(set(missing))))
    if shutil.which("xcrun") is None:
        return _fail("xcrun/swiftc not found (install Xcode command line tools)")

    with tempfile.TemporaryDirectory(prefix="mp3tom4b-selfcheck-routing-") as tmp:
        binary = Path(tmp) / "routing"
        compile_proc = _compile(SOURCES, binary)
        if compile_proc.returncode != 0 or not binary.is_file():
            sys.stdout.write(compile_proc.stdout)
            sys.stderr.write(compile_proc.stderr)
            return _fail("swiftc failed to build the routing check")

        # The Swift binary prints the per-check lines AND a «X/Y checks passed»
        # summary. Pass the per-check lines through verbatim, but hold its summary
        # back: the suite's ONE summary line is printed at the end, over both
        # stages, so the runner reads a single verdict.
        run_proc = subprocess.run([str(binary)], capture_output=True, text=True)
        summary = ""
        for line in run_proc.stdout.splitlines():
            if MARKER in line:
                summary = line
                continue
            if line.startswith("  FAILED checks:"):
                continue
            print(line)
        sys.stderr.write(run_proc.stderr)
        if not summary:
            return _fail("routing check produced no summary line")
        try:
            counts = summary.split(":")[1].split("checks")[0].strip().split("/")
            value_passed, value_total = int(counts[0]), int(counts[1])
        except (IndexError, ValueError):
            return _fail(f"unparseable routing summary: {summary!r}")

        # --- stage 2: the app→agent wire -------------------------------------
        print("\n  — провод: команду пишет НАСТОЯЩИЙ EngineClient, судит "
              "НАСТОЯЩИЙ dispatcher —")
        wire_binary = Path(tmp) / "wire"
        wire_compile = _compile(WIRE_SOURCES, wire_binary)
        wire_results: list = []
        if wire_compile.returncode != 0 or not wire_binary.is_file():
            sys.stderr.write(wire_compile.stderr)
            wire_results.append(("провод: бинарь собран", False, "swiftc упал"))
            print("  [FAIL] провод: бинарь собран — swiftc упал")
        else:
            _wire_stage(wire_binary, wire_results)

    passed = value_passed + sum(1 for _, ok, _ in wire_results if ok)
    total = value_total + len(wire_results)
    print(f"\n{MARKER} {passed}/{total} checks passed")
    failed = [name for name, ok, _ in wire_results if not ok]
    if run_proc.returncode != 0 or value_passed != value_total:
        failed.insert(0, f"значением: {value_passed}/{value_total}")
    if failed:
        print("  FAILED checks: " + "; ".join(failed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(run())
