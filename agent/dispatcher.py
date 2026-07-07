"""Command dispatch: read app-owned commands and run the right handler.

M0.5 (arch/synthesis.md §B): the app drops ``queue/commands/<cmd_id>.json``
atomically; launchd wakes the agent because ``queue/commands/`` is a
``WatchPaths`` entry. Each command carries ``action``
(``confirm-build`` | ``grouping-choice`` | ``cover-choice`` | ``cancel`` |
``skip`` | ``apply-to-all``), ``book_id``, ``source_rev``, ``confirm_token`` and
``idempotency_key``.

This module wires the happy path plus the M0.6 protocol protections:
  - malformed / unreadable command JSON → quarantined in ``queue/commands/bad/``
    (the agent never crashes on a bad file);
  - ``validate_command`` returns a *verdict* (accept / reject / reject-stale):
      * bad/missing ``confirm_token``, missing manifest, wrong status → REJECT
        (``command_rejected``, command dropped, no build);
      * valid token but the inputs changed after recognition (``source_rev``
        mismatch) → REJECT_STALE (``confirm_rejected_stale`` event, book STAYS
        ``pending-confirm`` — the scan re-armed it with a fresh token — only the
        stale command is dropped);
      * a known ``idempotency_key`` (double click / retry) → ACCEPT-as-skip
        (``build_skipped_idempotent``, no second build);
  - ONLY ``confirm-build`` may invoke the build (structural guarantee I2);
  - **real engine** (M1): flip the manifest ``pending-confirm`` → ``converting``
    → ``done``, stamp a ``build`` marker (pid) + the planned ``output_path`` on
    the way in, run :func:`build_m4b.build` (real ffmpeg → ``.m4b``), then record
    the ``idempotency_key`` and the real ``result`` (output path + built_at) on
    the way out. A :class:`build_m4b.BuildError` flips the book to ``error`` with
    the reason and sweeps any half-written temp. The state showcase is refreshed
    from the manifests after.
  - :func:`recover_interrupted` (run at startup): a manifest stuck at
    ``converting`` with no live build pid → ``error`` (``reason=interrupted``) +
    temp sweep, so a crash/kill mid-build surfaces instead of dangling.

A processed command file is removed only AFTER its handler completes, never
before — so a crash mid-handle leaves the command to be retried, not lost.
The ``events.jsonl`` journal records ``confirm_accepted`` → ``build_started`` →
``build_done``; the §M0 gate-test asserts no ``build_started`` without a
preceding ``confirm_accepted``.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from . import build_m4b, config, scan, split, state

# Actions that may trigger a build. Per I2 this is the ONLY gate to the engine.
BUILD_ACTION = "confirm-build"

# Cooperative cancel (D13). The app drops a ``cancel`` command targeting a
# ``book_id``; ownership of that command is SPLIT to avoid double-processing:
#   - if the book is ``converting`` with a LIVE build pid, the building agent's
#     own poll loop (build_m4b._cancel_requested) sees the command and tears down
#     ffmpeg; the dispatcher then consumes the command in _real_build's
#     BuildCancelled handler (it is the owner of that unwind).
#   - otherwise (book not converting / no live pid) the cancel is moot — drain
#     deletes the command itself and journals ``cancel_moot``.
# A cancel is never validated against source_rev/confirm_token: it targets a book
# by id, and re-cancelling an already-handled build is a harmless no-op.
CANCEL_ACTION = "cancel"

# «Собрать заново» (reconvert). The app drops a ``reconvert`` command targeting a
# ``book_id`` of an ALREADY-BUILT (``done``) book so the user can rebuild it with one
# click instead of the non-obvious "rename the folder" workaround. Like ``cancel`` it
# targets a book by id and is NOT validated against source_rev/confirm_token — the
# book is finished, its inputs are unchanged, so there is no live token to echo.
#
# Effect: RE-ARM the manifest done → ``pending-confirm`` with a FRESH confirm_token,
# so the book flows through the normal confirm window → «Собрать» path again.
#
# CRITICAL — reset the idempotency ledger. The book was built at this exact
# ``source_rev``, so its build key (``book_id:source_rev[:16]``) already sits in
# ``manifest['processed_keys']``. Because reconvert keeps ``source_rev`` UNCHANGED
# (the files did not move), the next ``confirm-build`` would derive the SAME key,
# ``validate_command`` would short-circuit to ``idempotent_skip`` (M0.6) and NO
# build would run. So reconvert MUST clear ``processed_keys`` — that is the precise
# state that blocks a repeat build (verified against validate_command/_already_processed
# / EngineClient.idempotencyKey which all key on book_id+source_rev). A cleared ledger
# lets the legitimate re-build through while a changed source_rev is not required.
RECONVERT_ACTION = "reconvert"

# The grouping decision for loose mp3s in the watch root (D1, flows S4). Handled
# in its own branch (it carries group_id, not book_id) and materializes book
# manifest(s) — it NEVER builds (each new book then goes the normal confirm path).
GROUPING_ACTION = "grouping-choice"
GROUPING_CHOICE_COMBINE = "combine"
GROUPING_CHOICE_SEPARATE = "separate"
GROUPING_VALID_CHOICES = (GROUPING_CHOICE_COMBINE, GROUPING_CHOICE_SEPARATE)

# Manifest status transitions driven by the fake-engine.
STATUS_PENDING = "pending-confirm"
STATUS_CONVERTING = "converting"
STATUS_DONE = "done"
STATUS_ERROR = "error"

# validate_command verdicts → how handle_command reacts (M0.6).
#   ACCEPT          build (or, for an already-processed key, idempotent-skip)
#   REJECT_STALE    inputs changed after recognition → confirm_rejected_stale,
#                   book STAYS pending-confirm (it was re-armed by the scan with a
#                   fresh token); only the stale command is dropped.
#   REJECT          any other invalid command (bad/missing token, no manifest,
#                   wrong status, malformed) → command_rejected, command dropped.
VERDICT_ACCEPT = "accept"
VERDICT_REJECT_STALE = "reject_stale"
VERDICT_REJECT = "reject"

# Live-progress throttle (Task 2). The engine streams a progress snapshot many
# times a second; we persist to state.json only every ~1.5s OR when the percent
# moved ≥1 point — enough for a smooth determinate bar without hammering the disk
# on a minutes-long encode. The FIRST snapshot is always written (so the bar leaves
# "Запуск…" promptly) and the LAST is forced by the done/cancel/error transitions.
PROGRESS_MIN_INTERVAL_S = 1.5
PROGRESS_MIN_PERCENT_DELTA = 1.0


def _move_to_bad(command_path: Path, reason: str) -> None:
    """Quarantine an unusable command file into ``queue/commands/bad/``.

    Best-effort: the whole point is to keep the drain loop alive, so any failure
    to move (already gone, permissions) is swallowed after journaling. A name
    clash in ``bad/`` is avoided by suffixing the nanosecond clock.
    """
    bad_dir = config.commands_bad_dir()
    try:
        bad_dir.mkdir(parents=True, exist_ok=True)
        dest = bad_dir / command_path.name
        if dest.exists():
            dest = bad_dir / f"{command_path.stem}.{time.time_ns()}{command_path.suffix}"
        command_path.replace(dest)
    except OSError:
        # Could not move it; try to remove so it is not retried forever.
        try:
            command_path.unlink()
        except OSError:
            pass
    state.append_event("command_bad", file=command_path.name, reason=reason)


def _already_processed(command: dict, manifest: dict) -> bool:
    """True if this command's ``idempotency_key`` was already built for this book.

    The ledger (``manifest['processed_keys']``) is revision-scoped because the app
    derives the key from ``book_id`` + ``source_rev`` — so a repeat key means "the
    SAME build was already done", which is exactly the double-click case.
    """
    key = command.get("idempotency_key")
    if not key:
        return False
    keys = manifest.get("processed_keys")
    return isinstance(keys, list) and key in keys


def validate_command(command: dict, manifest: dict | None) -> tuple[str, str]:
    """Return ``(verdict, reason)`` for a parsed command against its manifest.

    Verdict is one of ``VERDICT_ACCEPT`` / ``VERDICT_REJECT_STALE`` /
    ``VERDICT_REJECT``; ``reason`` is a short machine-ish tag for the journal.

    Ordering matters (M0.6):
      1. structural sanity (object, ``book_id``, manifest exists);
      2. **idempotency** — a known ``idempotency_key`` is an ACCEPT (the caller
         turns it into an idempotent *skip*, not a rebuild) regardless of the now
         ``done`` status, so a double-click collapses to one build;
      3. **stale source_rev** — the inputs changed after recognition →
         ``REJECT_STALE`` (book stays pending, re-armed by scan). This is checked
         BEFORE the token because a re-arming scan rotates BOTH ``source_rev`` and
         ``confirm_token``: a genuinely stale command therefore carries the *old*
         token too, but a ``source_rev`` mismatch already proves "inputs moved",
         which is the precise diagnosis the user is owed (``confirm_rejected_stale``)
         rather than a generic token reject;
      4. **confirm_token** — for a command that DOES match the current rev, the
         token must match (anti-forgery / anti-replay on the live revision);
      5. status must be ``pending-confirm`` to build.
    """
    if not isinstance(command, dict):
        return VERDICT_REJECT, "command_not_object"
    if not command.get("book_id"):
        return VERDICT_REJECT, "missing_book_id"
    if manifest is None:
        return VERDICT_REJECT, "manifest_missing"

    # An already-processed key short-circuits to an idempotent skip even if the
    # book is now ``done`` and (harmlessly) the token still matches.
    if _already_processed(command, manifest):
        return VERDICT_ACCEPT, "idempotent_skip"

    if command.get("source_rev") != manifest.get("source_rev"):
        # Inputs changed after the app captured the manifest → explicit stale.
        # (A re-arm also rotated the token, so we deliberately do NOT fall through
        # to a token reject here — staleness is the more informative cause.)
        return VERDICT_REJECT_STALE, "source_rev_mismatch"

    # Same revision → the command must prove it holds the live token.
    if command.get("confirm_token") != manifest.get("confirm_token"):
        return VERDICT_REJECT, "confirm_token_mismatch"

    if manifest.get("status") != STATUS_PENDING:
        return VERDICT_REJECT, f"status_not_pending:{manifest.get('status')!r}"
    return VERDICT_ACCEPT, "ok"


def _record_processed_key(manifest: dict, command: dict) -> None:
    """Append this command's ``idempotency_key`` to the manifest ledger (no I/O).

    Idempotent within the dict: a key is never duplicated. The caller persists the
    manifest atomically right after.
    """
    key = command.get("idempotency_key")
    if not key:
        return
    keys = manifest.get("processed_keys")
    if not isinstance(keys, list):
        keys = []
    if key not in keys:
        keys.append(key)
    manifest["processed_keys"] = keys


# Audio/build params the app's confirm window lets the user pick, carried in the
# confirm-build command's ``params`` (the SAME block that carries the cover keys).
# Only these keys are folded command→manifest before the build reads them — a
# whitelist so a forged/extra key can never reach the engine. ``build_mode`` (D15,
# Ступень 2) selects the fast (parallel groups) vs seamless (single-pass) engine —
# build_m4b.build() now honors it. The cover keys (cover_id / cover_custom_path) are
# NOT here — they are owned by :func:`_apply_cover_choice`.
_PARAM_KEYS_FROM_COMMAND = (
    "bitrate", "channels", "samplerate", "split", "split_threshold_mb",
    "build_mode",
)


def _apply_params_choice(manifest: dict, command: dict) -> None:
    """Fold the user's AUDIO/BUILD param picks (confirm-build ``params``) into the manifest.

    The confirm window lets the user override the scanner defaults (bitrate,
    channels, sample rate, split + its threshold, and — for the future toggle —
    ``build_mode``). Those edits ride in the command's ``params`` exactly like the
    cover pick, but until now nothing merged them: the build read
    ``manifest['params']`` (the scanner defaults), so the window's choices never
    reached the engine. This applies them, mirroring :func:`_apply_cover_choice`'s
    persistence contract (mutate ``manifest`` IN PLACE; the caller persists it
    atomically on the converting write).

    Rules (deliberate):
      · WHITELIST only (:data:`_PARAM_KEYS_FROM_COMMAND`) — an unknown/forged key is
        ignored, so a bad command can never inject an arbitrary param;
      · PARTIAL — only keys actually PRESENT in the command are applied; a key the
        command omits leaves the manifest value untouched. This is what keeps a
        cover-only command (no audio keys) a pure no-op → the manifest defaults
        stand exactly as before;
      · the ``samplerate: null`` sentinel ("as in source") is preserved verbatim:
        ``null`` is a present key with value ``None``, so we WRITE ``None`` (we do
        NOT coerce it to 44100). Absence of the key → no change either way;
      · NO validation/clamping here — the existing resolvers own that
        (build_m4b._samplerate / _bitrate_kbps / _channels_count, and the split
        threshold clamp in split.plan_parts). We only transport the choice; the
        engine still defends itself against a garbage value.

    ``source_rev`` / ``confirm_token`` are untouched — params are build payload, not
    a revision. Never raises (a non-dict ``params`` is simply ignored).
    """
    params = command.get("params")
    if not isinstance(params, dict):
        return

    manifest_params = manifest.get("params")
    if not isinstance(manifest_params, dict):
        # A real manifest always has params (scan.DEFAULT_PARAMS); seed from those
        # for an exotic/forged one so the user's pick still has a base to land on.
        manifest_params = dict(scan.DEFAULT_PARAMS)

    applied: dict = {}
    for key in _PARAM_KEYS_FROM_COMMAND:
        # ``in`` (not ``.get``) so an explicit ``samplerate: null`` is treated as a
        # PRESENT key whose value (None) must be written — only a MISSING key is a
        # no-op for that field. A partial command touches only the keys it carries.
        if key in params:
            manifest_params[key] = params[key]
            applied[key] = params[key]

    if applied:
        manifest["params"] = manifest_params
        state.append_event("build_params_applied",
                           book_id=manifest.get("book_id"), keys=sorted(applied))


def _apply_cover_choice(manifest: dict, command: dict) -> None:
    """Fold the user's cover pick (from the confirm-build command) into the manifest.

    The protocol carries the cover choice in the command's ``params`` (no separate
    ``cover-choice`` command — it is local confirm-window state that rides along):

      · ``params.cover_custom_path`` — the ORIGINAL path of a file the user picked
        via "Заменить". The AGENT is the single writer of the support tree, so we
        COPY it into ``covers/<book_id>-custom<ext>``, append a ``custom`` entry to
        ``cover_options`` (idempotent — one custom slot) and select it. The app
        never writes ``covers/`` itself; it only names the source file.
      · ``params.cover_id`` — the id of an already-resolved option
        (embedded/web/generated). We set ``cover_selected`` to it, but only if it
        actually exists in ``cover_options`` (a stale/forged id is ignored — the
        existing default stays, so the book is never coverless).

    Mutates ``manifest`` IN PLACE; the caller persists it atomically. Never raises:
    a copy failure / missing source degrades to "keep the current selection" so a
    bad pick can never break the build (PRD G4 — always a cover). ``source_rev`` /
    ``confirm_token`` are untouched — the cover is display payload, not revision.
    """
    params = command.get("params")
    if not isinstance(params, dict):
        return

    options = manifest.get("cover_options")
    if not isinstance(options, list):
        options = []

    # --- custom file: copy into covers/ (agent-owned), add + select it ----------
    custom_path = params.get("cover_custom_path")
    if isinstance(custom_path, str) and custom_path:
        src = Path(custom_path)
        if src.is_file():
            bid = str(manifest.get("book_id", "book"))
            ext = src.suffix.lower() or ".jpg"
            if ext not in (".jpg", ".jpeg", ".png"):
                ext = ".jpg"  # keep the on-disk name sane; ffmpeg sniffs content
            try:
                covers = config.covers_dir()
                covers.mkdir(parents=True, exist_ok=True)
                dest = covers / f"{bid}-custom{ext}"
                shutil.copyfile(src, dest)
            except OSError as exc:
                state.append_event(
                    "cover_custom_copy_failed", book_id=bid, error=repr(exc)
                )
            else:
                # Drop any prior custom slot, then add the fresh one and select it.
                options = [o for o in options
                           if not (isinstance(o, dict) and o.get("kind") == "custom")]
                opt = {
                    "id": "custom-0",
                    "kind": "custom",
                    "path": str(dest),
                    "label": "Своя картинка",
                }
                options.append(opt)
                manifest["cover_options"] = options
                manifest["cover_selected"] = opt["id"]
                state.append_event("cover_custom_applied", book_id=bid,
                                   path=str(dest))
                return  # custom wins; ignore any cover_id in the same command

    # --- explicit cover_id: select it iff it is a known option ------------------
    cover_id = params.get("cover_id")
    if isinstance(cover_id, str) and cover_id:
        known = {o.get("id") for o in options if isinstance(o, dict)}
        if cover_id in known:
            manifest["cover_selected"] = cover_id
            state.append_event("cover_selected_applied",
                               book_id=manifest.get("book_id"), cover_id=cover_id)
        else:
            # Stale/forged id → keep the current default (never coverless).
            state.append_event("cover_id_ignored_unknown",
                               book_id=manifest.get("book_id"), cover_id=cover_id)


def _real_build(manifest: dict, manifest_path: Path, command: dict) -> dict:
    """Real engine: move the manifest through ``converting`` → ``done``/``error``.

    Walks the real status transitions, writing each one atomically so a reader
    (the app) observes a coherent sequence, and runs the real ffmpeg pipeline
    (:func:`build_m4b.build`) in between. Returns the final manifest dict.

    Flow & protections:
      - On entering ``converting`` we stamp a ``build`` marker (pid + start time)
        AND the *planned* ``result.output_path`` into the manifest, THEN persist.
        The pid marker is what makes an *interrupted* ``converting`` detectable on
        the next launch (a manifest left at ``converting`` whose pid is dead is a
        build that never reached ``done`` — see :func:`recover_interrupted`); the
        early ``output_path`` lets that recovery sweep our half-written temp.
      - On success we flip to ``done`` with a real ``result`` (output path +
        built_at), record the command's ``idempotency_key`` in the ledger and
        clear the live marker, so a second identical command is an idempotent skip
        rather than a second build.
      - On a :class:`build_m4b.BuildError` we flip to ``error`` with the reason,
        sweep any half-written output temp, clear the marker and journal
        ``build_failed``. The book is NOT re-armed here — surfacing the failure is
        the point; the user re-triggers (or an input edit re-arms it via scan).
      - I1: ``build_m4b.build`` only reads the source mp3s; nothing here writes
        them.
    """
    book_id = manifest.get("book_id")

    # Fold the user's confirm-window choices (carried in the command's ``params``)
    # into the manifest BEFORE the build reads it — and before the converting write
    # below persists the manifest:
    #   · _apply_params_choice — the AUDIO/BUILD picks (bitrate / channels /
    #     samplerate / split / split_threshold_mb / build_mode). Whitelisted +
    #     partial: only keys the command carries are applied, so a cover-only
    #     command leaves the scanner defaults intact, and an explicit
    #     ``samplerate: null`` ("as in source") is preserved (not coerced to 44100).
    #     Final validation/clamping stays with the existing resolvers (_samplerate /
    #     _bitrate_kbps / _channels_count, split-threshold clamp) — we only transport.
    #   · _apply_cover_choice — the cover pick (cover_id / cover_custom_path). The
    #     agent is the single writer, so a custom file is COPIED into covers/ here.
    # Both are display/build payload — source_rev / confirm_token are untouched.
    _apply_params_choice(manifest, command)
    _apply_cover_choice(manifest, command)

    # Resolve the output path up front so we can persist it BEFORE running ffmpeg.
    # That way recover_interrupted (which sweeps ``result.output_path`` + its temp
    # siblings) can clean a half-written file even if we are killed mid-encode.
    out_path = build_m4b.default_output_path(manifest)

    # pending-confirm → converting. Stamp the live build marker + planned output.
    manifest["status"] = STATUS_CONVERTING
    manifest["progress"] = 0.0
    manifest["build"] = {"pid": os.getpid(), "started_at": time.time()}
    manifest["result"] = {"output_path": str(out_path)}
    manifest.pop("error", None)  # clear any prior error from a re-run
    state.write_json_atomic(manifest_path, manifest)
    # Project the converting status into the showcase NOW — the build below runs for
    # minutes, and without this the В РАБОТЕ section would never see the book (it
    # would jump pending-confirm → done on the next scan). The live pid we just
    # stamped keeps a concurrent scan/recover from flipping it to error.
    scan.refresh_showcase()
    state.append_event("build_started", book_id=book_id)

    # Live progress (Task 2): a throttled callback persists the engine's snapshots
    # into the converting book's showcase row (state.json). State lives in a small
    # mutable box because the cb fires from the ffmpeg reader thread. We write the
    # FIRST snapshot immediately, then only every PROGRESS_MIN_INTERVAL_S or when
    # percent moved ≥ PROGRESS_MIN_PERCENT_DELTA — a smooth bar, minimal disk churn.
    _pstate = {"last_t": 0.0, "last_pct": -1.0}

    def _on_progress(snap: dict) -> None:
        now = time.monotonic()
        pct = float(snap.get("percent") or 0.0)
        first = _pstate["last_pct"] < 0
        if (first
                or (now - _pstate["last_t"]) >= PROGRESS_MIN_INTERVAL_S
                or abs(pct - _pstate["last_pct"]) >= PROGRESS_MIN_PERCENT_DELTA):
            _pstate["last_t"] = now
            _pstate["last_pct"] = pct
            _patch_book_progress(book_id, snap)

    try:
        final_path = build_m4b.build(manifest, out_path=out_path,
                                     progress_cb=_on_progress)
    except build_m4b.BuildCancelled:
        # converting → BACK to pending-confirm (D13). Cancel is NOT a failure: the
        # build() poll already SIGTERM→SIGKILL'd its own ffmpeg and swept the temp;
        # we belt-and-suspenders sweep again, then re-arm the book for a future
        # rebuild. Crucially we DO NOT touch source_rev / confirm_token — they stay
        # valid, so the existing confirm command/token can drive a fresh build and
        # the book simply returns to the queue. We clear the live build marker, the
        # planned-output result and progress so no stale half-state lingers.
        _cleanup_build_temps(manifest)
        manifest["status"] = STATUS_PENDING
        manifest["progress"] = 0.0
        manifest.pop("build", None)
        manifest.pop("result", None)
        manifest.pop("error", None)
        state.write_json_atomic(manifest_path, manifest)
        # As the SINGLE owner of the unwind, consume the cancel command(s) here so
        # drain never re-handles them (no double processing).
        consumed = _consume_cancel_commands(book_id)
        scan.refresh_showcase()  # surface the back-to-pending status at once
        state.append_event("build_cancelled", book_id=book_id, consumed=consumed)
        return manifest
    except build_m4b.BuildError as exc:
        # converting → error. Sweep the half-written temp (build() already swept
        # on its way out, but be belt-and-suspenders) and surface the reason.
        _cleanup_build_temps(manifest)
        manifest["status"] = STATUS_ERROR
        manifest["error"] = {"reason": exc.reason, "detail": exc.detail, "at": time.time()}
        manifest["progress"] = 0.0
        manifest.pop("build", None)
        manifest.pop("result", None)
        state.write_json_atomic(manifest_path, manifest)
        scan.refresh_showcase()  # surface the error status in the showcase at once
        state.append_event("build_failed", book_id=book_id, reason=exc.reason)
        return manifest

    # P1 split (params.split == True): the full .m4b is built; now cut it into
    # parts on chapter boundaries and PUBLISH THE PARTS as the result. The
    # intermediate full file is removed (we never publish both). On a split
    # failure this flips the book to ``error`` (the full file + any parts are
    # swept) — surfacing the failure beats shipping a confusing half-set. When
    # ``split`` is False (the default) this is a no-op and the single-file path is
    # byte-for-byte unchanged.
    params = manifest.get("params") or {}
    if params.get("split"):
        try:
            result = _split_built_m4b(manifest, final_path)
        except build_m4b.BuildError as exc:
            build_m4b._unlink_quiet(final_path)  # drop the now-orphan full file
            manifest["status"] = STATUS_ERROR
            manifest["error"] = {"reason": exc.reason, "detail": exc.detail,
                                 "at": time.time()}
            manifest["progress"] = 0.0
            manifest.pop("build", None)
            manifest.pop("result", None)
            state.write_json_atomic(manifest_path, manifest)
            scan.refresh_showcase()
            state.append_event("build_failed", book_id=book_id, reason=exc.reason)
            return manifest
    else:
        result = {
            "output": str(final_path),
            "output_path": str(final_path),
            "built_at": time.time(),
        }

    # converting → done. Real output marker (no ``fake`` field).
    manifest["status"] = STATUS_DONE
    manifest["progress"] = 1.0
    manifest["result"] = result
    _record_processed_key(manifest, command)
    manifest.pop("build", None)  # build finished → no live marker
    state.write_json_atomic(manifest_path, manifest)
    scan.refresh_showcase()  # reflect the done status immediately (ГОТОВО section)
    state.append_event(
        "build_done", book_id=book_id,
        output=result.get("output_path"), parts=len(result.get("parts") or []),
    )
    return manifest


def _split_built_m4b(manifest: dict, full_path: Path) -> dict:
    """Cut the built ``full_path`` into parts and return the split ``result`` dict.

    Called from :func:`_real_build` only when ``params.split`` is True. Steps
    (recipe research §3, via :mod:`split`):
      1. :func:`split.plan_parts` groups consecutive chapters into parts each ≤ the
         ``split_threshold_mb`` budget (a single over-threshold chapter becomes its
         own ``oversize`` part — E15);
      2. if there is only ONE part (the whole book already fits the threshold)
         there is nothing to cut: keep the single full file and return the normal
         single-file result (no needless re-copy);
      3. otherwise :func:`split.split` stream-copies each part next to the source
         folder (the same dir the full file lives in), then we REMOVE the
         intermediate full file so only the parts remain.

    The returned ``result`` carries ``parts`` (the list of part paths) plus an
    ``output_path`` pointing at the CONTAINING FOLDER so the app's "Открыть в
    Finder" reveals the set; ``output`` is the first part (a stable single handle).
    Raises :class:`build_m4b.BuildError` (from :func:`split.split`) on any part
    failure — the caller flips the book to ``error`` and sweeps the full file.
    """
    book_id = manifest.get("book_id")
    out_dir = full_path.parent

    parts_plan = split.plan_parts(manifest)

    # Nothing to split (no usable chapters, or the whole book fits one part):
    # ship the single file unchanged — splitting one part would just re-copy it.
    if len(parts_plan) <= 1:
        return {
            "output": str(full_path),
            "output_path": str(full_path),
            "built_at": time.time(),
        }

    state.append_event("split_started", book_id=book_id, parts=len(parts_plan))
    part_paths = split.split(
        full_path, parts_plan, out_dir=out_dir, manifest=manifest
    )

    # The parts are published; drop the intermediate full file so we never present
    # both the whole book AND its parts.
    build_m4b._unlink_quiet(full_path)

    oversize = sum(1 for p in parts_plan if p.get("oversize"))
    state.append_event(
        "split_done", book_id=book_id, parts=len(part_paths), oversize=oversize
    )
    return {
        "output": str(part_paths[0]) if part_paths else str(out_dir),
        "output_path": str(out_dir),  # the folder → "Открыть в Finder" reveals all
        "parts": [str(p) for p in part_paths],
        "oversize_parts": oversize,
        "built_at": time.time(),
    }


def _current_pending_group(group_id: str) -> dict | None:
    """Return the live pending group with ``group_id`` from state.json, or ``None``.

    The agent is the single writer of state, so reading it back is the authoritative
    way to validate a grouping-choice (there is no per-group manifest yet). Defensive
    against a missing/odd-shaped state.
    """
    cur = state.read_state(default=None)
    if not isinstance(cur, dict):
        return None
    groups = cur.get("pending_groups")
    if not isinstance(groups, list):
        return None
    for g in groups:
        if isinstance(g, dict) and g.get("group_id") == group_id:
            return g
    return None


def _grouping_resolved_keys() -> set:
    """Set of grouping idempotency_keys already materialized (state ledger)."""
    cur = state.read_state(default=None)
    if not isinstance(cur, dict):
        return set()
    led = cur.get("grouping_processed")
    return {k for k in led if isinstance(k, str)} if isinstance(led, list) else set()


def _handle_grouping_choice(command: dict, command_path: Path) -> bool:
    """Materialize the user's grouping decision for a loose-mp3 set (D1, flows S4).

    Validation mirrors the build protocol's protections (M0.6), keyed on the live
    pending group in state.json (the group has no manifest of its own):
      1. structural sanity — ``group_id`` + a valid ``choice``
         (``combine`` | ``separate``);
      2. **idempotency** — a ``idempotency_key`` already in the resolved ledger is an
         idempotent skip (double-click / retry after the group was consumed), even
         though the group is now gone;
      3. group must exist in state (else REJECT ``group_missing`` — a forged/stale
         group id with no prior resolve);
      4. **stale rev** — the loose set changed after recognition (``rev`` mismatch)
         → REJECT_STALE (the scan re-armed a fresh group/token; only this command
         dies). Checked before the token because a re-arm rotates BOTH;
      5. **token** — must match the live group's token (anti-forgery/replay).

    On accept it materializes the manifest(s) from the LIVE loose files in the
    group's ``watch_dir`` (combine → 1 book of N chapters; separate → N 1-chapter
    books), records the resolved key in the state ledger (so the scanner stops
    re-prompting for the files that physically linger in the root), and journals the
    outcome. The closing :func:`drain_commands` re-scan then drops the group from
    state. Returns ``False`` always — grouping NEVER counts as a build (I2).
    """
    group_id = command.get("group_id")
    choice = command.get("choice")
    idem = command.get("idempotency_key")

    # 1. structural sanity.
    if not group_id:
        state.append_event("grouping_rejected", file=command_path.name,
                           reason="missing_group_id")
        _delete_command(command_path)
        return False
    if choice not in GROUPING_VALID_CHOICES:
        state.append_event("grouping_rejected", file=command_path.name,
                           group_id=group_id, reason=f"bad_choice:{choice!r}")
        _delete_command(command_path)
        return False

    # 2. idempotency — already materialized → skip (no second materialization).
    if idem and idem in _grouping_resolved_keys():
        state.append_event("grouping_skipped_idempotent", file=command_path.name,
                           group_id=group_id, idempotency_key=idem)
        _delete_command(command_path)
        return False

    group = _current_pending_group(group_id)

    # 3. group must exist.
    if group is None:
        state.append_event("grouping_rejected", file=command_path.name,
                           group_id=group_id, reason="group_missing")
        _delete_command(command_path)
        return False

    # 4. stale rev — inputs changed after recognition (book stays re-armed by scan).
    if command.get("rev") != group.get("rev"):
        state.append_event("grouping_rejected_stale", file=command_path.name,
                           group_id=group_id, command_rev=command.get("rev"),
                           group_rev=group.get("rev"))
        _delete_command(command_path)
        return False

    # 5. token — must hold the live token.
    if command.get("token") != group.get("token"):
        state.append_event("grouping_rejected", file=command_path.name,
                           group_id=group_id, reason="token_mismatch")
        _delete_command(command_path)
        return False

    # --- ACCEPT: materialize from the LIVE loose files in the watch root ---------
    watch = Path(str(group.get("watch_dir") or scan.watch_dir()))
    loose = scan._list_loose_mp3s(watch)
    if not loose:
        # The files vanished between recognition and the choice — nothing to make.
        # Record the key so a late duplicate is still a no-op, drop the command.
        key = idem or scan.grouping_idempotency_key(group_id, str(group.get("rev", "")))
        scan.record_grouping_resolved(key)
        state.append_event("grouping_rejected", file=command_path.name,
                           group_id=group_id, reason="loose_files_gone")
        _delete_command(command_path)
        return False

    if choice == GROUPING_CHOICE_COMBINE:
        man = scan.materialize_combined(watch, loose)
        made = [man.get("book_id")]
    else:
        mans = scan.materialize_separate(watch, loose)
        made = [m.get("book_id") for m in mans]

    # Record resolved BEFORE the closing re-scan so the now-resolved group is
    # suppressed (the loose files still sit in the root). Key = command's own
    # idempotency_key when present, else derived from the live group identity.
    key = idem or scan.grouping_idempotency_key(group_id, str(group.get("rev", "")))
    scan.record_grouping_resolved(key)

    state.append_event("grouping_materialized", file=command_path.name,
                       group_id=group_id, choice=choice, books=made,
                       count=len(loose))
    _delete_command(command_path)
    return False


def _handle_reconvert(command: dict, command_path: Path, manifest: dict | None,
                      manifest_path: Path | None) -> bool:
    """Re-SCAN an already-built book so «Собрать заново» rebuilds it from fresh probe.

    Targets a book BY ID (like ``cancel``): NOT validated against
    ``source_rev`` / ``confirm_token`` — the book is ``done``, so there is no live
    token for the app to echo. Validation is deliberately narrow and safe:

      1. structural — the command carries a ``book_id`` and its manifest exists
         (else REJECT ``manifest_missing`` — a garbage/forged id is not re-armed);
      2. **source alive** — the book's ``src_dir`` (folder / combined watch dir /
         separate file) must still be on disk with ≥1 mp3
         (:func:`scan._manifest_source_alive`). If the source VANISHED we REJECT
         (``source_missing``) instead of re-arming a book that can never build — a
         confirm against gone inputs would only fail later at ffmpeg; rejecting here
         is the honest, early diagnosis;
      3. **status** — only a ``done`` book is re-armed. A book already
         ``pending-confirm`` needs nothing; a ``converting`` one is mid-build (use
         cancel); an ``error`` one is surfaced already. Any non-``done`` status →
         REJECT ``status_not_done`` (a no-op, never a corruption).

    On ACCEPT it RE-DISCOVERS the book from its CURRENT source files rather than
    merely flipping the old manifest's status — :func:`scan.rescan_book_manifest`
    re-runs the SAME scan-build that first created it (probe every chapter, ID3 tags,
    ``source_samplerate``, embedded-cover detect, fresh ``source_rev`` +
    ``confirm_token``, and ``processed_keys=[]``). This is THE fix: a book built by an
    OLD agent has a manifest missing today's fields (e.g. ``source_samplerate`` — so
    the confirm window's «Как в источнике · N кГц» hint never shows). A plain in-place
    re-arm would preserve those gaps; a full re-probe refills every field. The rebuilt
    manifest lands at ``status=pending-confirm`` — the app re-confirms params in the
    window (they reset to the scanner's defaults D2/D6, which is the correct clean
    slate for a re-discovered book).

    Two properties the rebuild must (and does) keep:
      · **fresh ``source_rev``** — recomputed from the (unchanged) files, so it lands
        on the same value; combined with the CLEARED ``processed_keys`` the next
        ``confirm-build`` derives the same key BUT is no longer in the ledger, so it
        is NOT ``idempotent_skip``'d and really rebuilds to ``build_done`` (self-check
        ae63 / §reconvert). Without the ledger reset the rebuild would collapse to
        ``build_skipped_idempotent``;
      · **no stale build markers** — the fresh manifest carries no ``result`` /
        ``error`` / ``build`` and ``progress`` starts absent, so the re-armed book is
        a clean pending book (the old finished-build leftovers are gone by construction
        — the scan writes a brand-new manifest, it does not inherit them).

    A ``reconvert`` event is journalled, and :func:`scan.refresh_showcase` re-projects
    state.json so the queue's ГОТОВО row moves to ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ and the confirm
    window surfaces (file-watch). Returns ``False`` always — reconvert NEVER builds
    itself (I2): it only re-arms; the subsequent confirm-build is the sole path to the
    engine.
    """
    book_id = command.get("book_id")

    # 1. structural — need a real manifest to re-arm (no book_id was already handled
    #    by the caller falling through to the noop path, but be explicit/defensive).
    if not isinstance(manifest, dict) or manifest_path is None:
        state.append_event("reconvert_rejected", file=command_path.name,
                           book_id=book_id, reason="manifest_missing")
        _delete_command(command_path)
        return False

    # 2. source alive — never re-arm a book whose inputs vanished (would only fail
    #    later at the build). scan._manifest_source_alive is the SAME liveness rule
    #    the showcase uses to drop dead books, applied here as an early reject.
    if not scan._manifest_source_alive(manifest):
        state.append_event("reconvert_rejected", file=command_path.name,
                           book_id=book_id, reason="source_missing",
                           src_dir=manifest.get("src_dir"))
        _delete_command(command_path)
        return False

    # 3. status — only a finished (done) book is re-armed; anything else is a no-op.
    if manifest.get("status") != STATUS_DONE:
        state.append_event("reconvert_rejected", file=command_path.name,
                           book_id=book_id, reason="status_not_done",
                           status=manifest.get("status"))
        _delete_command(command_path)
        return False

    # --- ACCEPT: re-SCAN the book from its live sources → fresh pending manifest ---
    # DRY: dispatch to the same scan-build that minted the book (subfolder / combined /
    # separate), forced to re-probe even at an unchanged source_rev. The freshly
    # written manifest has today's fields (source_samplerate, chapters, tags, cover),
    # status=pending-confirm, a new confirm_token and an empty processed_keys ledger.
    rebuilt = scan.rescan_book_manifest(manifest, force=True)
    if not isinstance(rebuilt, dict):
        # Late race: the source went away between the liveness gate and the re-probe
        # (e.g. the folder was deleted mid-drain). Do NOT corrupt the book — leave the
        # done manifest as-is, journal the reject, and drop the command.
        state.append_event("reconvert_rejected", file=command_path.name,
                           book_id=book_id, reason="source_missing",
                           src_dir=manifest.get("src_dir"))
        _delete_command(command_path)
        return False

    state.append_event("reconvert", book_id=book_id, src_dir=rebuilt.get("src_dir"))
    # Re-project state.json now so the book moves ГОТОВО → ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ and
    # the confirm window surfaces via the app's file-watch. refresh_showcase re-reads
    # the just-written manifest from disk (no folder walk → no second re-arm).
    scan.refresh_showcase()
    _delete_command(command_path)
    return False


def handle_command(command_path: Path) -> bool:
    """Parse, validate and dispatch a single command file.

    Returns ``True`` if a build ran, ``False`` otherwise. The command file is
    always removed after handling (success, validation-fail, or non-build
    action); malformed files are routed to ``bad/`` instead by the caller's
    parse step. A build runs ONLY for ``action == confirm-build`` that passes
    validation (structural I2).
    """
    command = state.read_json(command_path, default=None)
    if command is None or not isinstance(command, dict):
        # Unreadable / not an object → quarantine, do not delete from queue.
        _move_to_bad(command_path, "malformed_json")
        return False

    book_id = command.get("book_id")
    action = command.get("action")

    # Grouping decisions are keyed on group_id (no book_id / manifest yet) and never
    # build — dispatch them in their own validated branch before the book lookup.
    if action == GROUPING_ACTION:
        return _handle_grouping_choice(command, command_path)

    manifest_path = config.books_dir() / f"{book_id}.json" if book_id else None
    manifest = state.read_json(manifest_path, default=None) if manifest_path else None

    # Cooperative cancel (D13) — split ownership to avoid double-processing:
    #   · target book is ``converting`` with a LIVE build pid → a building agent
    #     owns this command (its poll loop will see it and tear ffmpeg down, then
    #     consume it in _real_build). LEAVE the command on disk: returning here
    #     without deleting hands it to that owner.
    #   · otherwise (book done/error/pending/absent, or a dead pid) → cancel is
    #     MOOT (too late, or nothing to cancel). Delete it + journal ``cancel_moot``.
    #     A re-cancel of an already-unwound build also lands here → harmless no-op.
    if action == CANCEL_ACTION:
        build = manifest.get("build") if isinstance(manifest, dict) else None
        pid = build.get("pid") if isinstance(build, dict) else None
        is_converting = (
            isinstance(manifest, dict)
            and manifest.get("status") == STATUS_CONVERTING
        )
        if is_converting and _pid_alive(pid):
            # Owned by the live build — do NOT consume it here.
            return False
        state.append_event(
            "cancel_moot", file=command_path.name, book_id=book_id,
            status=(manifest.get("status") if isinstance(manifest, dict) else None),
        )
        _delete_command(command_path)
        return False

    # «Собрать заново» (reconvert) — re-arm a done book to pending-confirm with a
    # fresh token + cleared idempotency ledger so the normal confirm→build path can
    # rebuild it. Targets book_id only (no source_rev/token to echo — the book is
    # finished). Dispatched in its own validated branch; it NEVER builds itself (I2).
    if action == RECONVERT_ACTION:
        return _handle_reconvert(command, command_path, manifest, manifest_path)

    # A non-build action is dispatched without ever touching build validation
    # (cover/… land in M1). It still requires a real manifest so a garbage
    # file does not masquerade as a no-op.
    if action != BUILD_ACTION:
        state.append_event(
            "command_noop", file=command_path.name, book_id=book_id, action=action
        )
        _delete_command(command_path)
        return False

    verdict, reason = validate_command(command, manifest)

    if verdict == VERDICT_REJECT_STALE:
        # Inputs changed after recognition. Do NOT build and do NOT silently drop:
        # emit the explicit status event. The book stays pending-confirm — the
        # scan re-armed it with a fresh source_rev/confirm_token, so the app can
        # confirm again against the current inputs. Only this stale command dies.
        state.append_event(
            "confirm_rejected_stale",
            file=command_path.name,
            book_id=book_id,
            command_rev=command.get("source_rev"),
            manifest_rev=(manifest or {}).get("source_rev"),
        )
        _delete_command(command_path)
        return False

    if verdict == VERDICT_REJECT:
        # Any other invalid command (bad/missing token, no manifest, wrong status):
        # no build, reason journaled, command dropped.
        state.append_event(
            "command_rejected", file=command_path.name, book_id=book_id, reason=reason
        )
        _delete_command(command_path)
        return False

    # VERDICT_ACCEPT. Two sub-cases:
    if reason == "idempotent_skip":
        # A second command with an already-processed idempotency_key (double click
        # / retry). The build already happened → skip it, but still consume the
        # duplicate command. No confirm_accepted/build_* events: the gate stays
        # "exactly one build per key".
        state.append_event(
            "build_skipped_idempotent",
            file=command_path.name,
            book_id=book_id,
            idempotency_key=command.get("idempotency_key"),
        )
        _delete_command(command_path)
        return False

    # confirm-build, validated, first time for this key → the ONLY path to the
    # engine (I2). confirm_accepted is emitted BEFORE build_started so the journal
    # gate (no build_started without a preceding confirm_accepted) holds.
    state.append_event("confirm_accepted", book_id=book_id, file=command_path.name)
    _real_build(manifest, manifest_path, command)  # type: ignore[arg-type]

    # Remove the command only AFTER the build completed.
    _delete_command(command_path)
    return True


def _delete_command(command_path: Path) -> None:
    """Remove a fully-handled command file (best-effort; idempotent)."""
    try:
        command_path.unlink()
    except OSError:
        pass


def _consume_cancel_commands(book_id: object) -> int:
    """Delete every pending ``cancel`` command targeting ``book_id`` (D13 owner).

    Called by :func:`_real_build` once it has caught :class:`BuildCancelled` and
    fully unwound the build — the building agent is the SINGLE owner of the cancel
    command it acted on, so it (not drain) removes it, which is what prevents a
    second, stray re-processing. We sweep ALL matching cancel commands (the user
    may have clicked twice) so no duplicate lingers to be re-handled as moot.
    Best-effort and tolerant: a missing dir / unreadable file is skipped, never
    raised. Returns the count removed (for the journal).
    """
    if not book_id:
        return 0
    cmd_dir = config.commands_dir()
    if not cmd_dir.is_dir():
        return 0
    removed = 0
    for path in cmd_dir.glob("*.json"):
        cmd = state.read_json(path, default=None)
        if not isinstance(cmd, dict):
            continue
        if cmd.get("action") == CANCEL_ACTION and cmd.get("book_id") == book_id:
            _delete_command(path)
            removed += 1
    return removed


def _pending_command_files() -> list[Path]:
    """List queued command files (``*.json``), oldest-``ts``-first.

    Ordering is by the command's own ``ts`` field when readable (the app stamps
    it), falling back to file mtime — so commands are processed roughly in the
    order the user issued them. The ``bad/`` subdir is skipped (it is not a
    command source). Unreadable files still sort (last) and get quarantined when
    handled.
    """
    cmd_dir = config.commands_dir()
    if not cmd_dir.is_dir():
        return []
    files = [p for p in cmd_dir.glob("*.json") if p.is_file()]

    def sort_key(p: Path) -> tuple[float, str]:
        data = state.read_json(p, default=None)
        if isinstance(data, dict) and isinstance(data.get("ts"), (int, float)):
            return (float(data["ts"]), p.name)
        try:
            return (p.stat().st_mtime, p.name)
        except OSError:
            return (float("inf"), p.name)

    return sorted(files, key=sort_key)


def drain_commands() -> int:
    """Process every currently-queued command once; return the count handled.

    "Handled" = the command file was consumed (built, rejected, no-op'd, or
    quarantined). One bad file never stops the drain — each is handled in
    isolation. After draining, the state showcase is refreshed so the app sees
    the new ``done`` statuses.
    """
    files = _pending_command_files()
    handled = 0
    for command_path in files:
        try:
            handle_command(command_path)
        except Exception as exc:  # defensive: never let one command kill the loop
            state.append_event(
                "command_error", file=command_path.name, error=repr(exc)
            )
            _move_to_bad(command_path, f"handler_exception:{type(exc).__name__}")
        handled += 1

    if handled:
        # Reflect the new manifest statuses in the showcase the app reads.
        scan.run_scan()
    return handled


def _pid_alive(pid: object) -> bool:
    """Best-effort liveness check for a recorded build pid.

    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` if the pid is gone and
    ``PermissionError`` if it exists but is owned by another user (still alive).
    A missing / non-int pid counts as not-alive. The fake engine never leaves a
    *separate* live process, so in practice any manifest found at ``converting``
    on startup is orphaned; the pid check keeps the logic honest for the real
    engine in M1.
    """
    if not isinstance(pid, int):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _patch_book_progress(book_id: object, progress: dict | None) -> None:
    """Targeted atomic patch of one book's ``progress`` in state.json (Task 2).

    Read-modify-write the showcase, setting ``books[i].progress`` for the matching
    ``book_id`` (or removing it when ``progress`` is None). The agent is the single
    writer of state and the build runs serially, so this is safe; it is also
    best-effort and FULLY tolerant — it never raises (it runs from the ffmpeg
    reader thread; a hiccup must never disturb the encode). If the row is not in
    the showcase yet (a refresh hasn't projected it), the patch is simply a no-op
    this tick — the next snapshot will land once the converting row exists.

    Only the ``progress`` field is touched; every other field the scan owns is left
    verbatim. Pairs with :func:`scan.refresh_showcase`, which PRESERVES an existing
    ``progress`` for a converting book so a concurrent refresh can't wipe it.
    """
    if not book_id:
        return
    try:
        cur = state.read_state(default=None)
        if not isinstance(cur, dict):
            return
        books = cur.get("books")
        if not isinstance(books, list):
            return
        touched = False
        for b in books:
            if isinstance(b, dict) and b.get("book_id") == book_id:
                if progress is None:
                    b.pop("progress", None)
                else:
                    b["progress"] = progress
                touched = True
                break
        if touched:
            state.write_state(cur)
    except (OSError, ValueError, TypeError):
        pass


def _cleanup_build_temps(manifest: dict) -> list[str]:
    """Remove any half-written output temp files for an interrupted build.

    The real engine (M1) writes the ``.m4b`` to a hidden temp in the output dir
    and atomically renames on success, so an interrupt can leave a ``.<name>.*.tmp``
    behind. The fake engine writes none, so this is normally a no-op — but wiring
    the sweep now means the recovery path is already correct when the real engine
    lands. Returns the list of removed paths (for the journal).
    """
    removed: list[str] = []
    out = manifest.get("result", {})
    out_path = out.get("output_path") if isinstance(out, dict) else None
    candidates: list[Path] = []
    if isinstance(out_path, str) and out_path:
        p = Path(out_path)
        candidates.append(p)
        # tmp siblings: .<name>.*.tmp in the same dir (matches state.write_json_atomic style)
        try:
            candidates.extend(p.parent.glob(f".{p.name}.*"))
        except OSError:
            pass
    for c in candidates:
        try:
            if c.exists():
                c.unlink()
                removed.append(str(c))
        except OSError:
            pass
    return removed


def recover_interrupted() -> int:
    """Reconcile manifests left mid-build after a crash/kill (run at startup).

    A manifest at ``status == converting`` whose recorded build pid is not alive
    is an *orphan*: the process that owned it died before reaching ``done``. We
    flip it to ``error`` with ``reason="interrupted"``, sweep any output temp
    files, clear the live ``build`` marker, and journal an ``interrupted`` event.
    The book is NOT silently re-armed to pending here — surfacing the failure is
    the point; the user re-triggers (or a later edit re-arms it via the scan).

    Returns the number of manifests recovered. Safe to call on every launch
    (idempotent: a manifest already at ``error`` is skipped).
    """
    books_dir = config.books_dir()
    if not books_dir.is_dir():
        return 0
    recovered = 0
    for manifest_path in sorted(books_dir.glob("*.json")):
        manifest = state.read_json(manifest_path, default=None)
        if not isinstance(manifest, dict):
            continue
        if manifest.get("status") != STATUS_CONVERTING:
            continue
        build = manifest.get("build")
        pid = build.get("pid") if isinstance(build, dict) else None
        if _pid_alive(pid):
            # A live build owns this manifest (real engine, M1) → leave it alone.
            continue

        book_id = manifest.get("book_id")
        removed = _cleanup_build_temps(manifest)
        manifest["status"] = STATUS_ERROR
        manifest["error"] = {"reason": "interrupted", "at": time.time()}
        manifest.pop("build", None)
        manifest["progress"] = manifest.get("progress", 0.0)
        state.write_json_atomic(manifest_path, manifest)
        state.append_event(
            "interrupted", book_id=book_id, cleaned=removed, prior_pid=pid
        )
        recovered += 1
    return recovered


def run_once() -> int:
    """Backwards-compatible alias for :func:`drain_commands` (used by __main__)."""
    return drain_commands()
