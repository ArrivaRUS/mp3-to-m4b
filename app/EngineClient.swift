// EngineClient — the app's WRITE side of the app↔agent protocol.
//
// The agent (python `agent` package) is the SINGLE writer of authoritative state
// (state.json + per-book manifests). The app is a reader EXCEPT for one thing: it
// "drops commands". This file owns that one write (arch/synthesis.md §B, "Вверх"):
//
//   queue/commands/<cmd_id>.json   — app-owned command, written ATOMICALLY
//     (tmp file in the same dir → rename). The agent's WatchPaths includes
//     queue/commands/, so dropping a file here wakes it without a new mp3.
//
// M0.4 wires the single action we need first: `confirm-build` (the "Собрать"
// button). The command carries the tokens the agent validates against the live
// manifest before it ever runs ffmpeg (status==pending-confirm && source_rev
// matches && confirm_token valid — structural invariant I2):
//
//     { cmd_id, action:"confirm-build", book_id, source_rev, confirm_token,
//       build_token, idempotency_key, params:{bitrate,channels,samplerate,split}, ts }
//
// `build_token` (D17) is the newest field and the strictest one: it exists only in
// a COMPLETE manifest, so echoing it proves the command was minted from a book the
// app had actually finished reading. Without it the agent answers
// `confirm_rejected_not_ready` and builds nothing.
//
// Field names mirror agent/dispatcher.py's documented contract and agent/scan.py's
// manifest/params keys EXACTLY — a drift here would be silently dropped to
// queue/commands/bad/ by the agent. The app never writes state/manifests.
//
// Unsandboxed, no external Swift deps: Foundation only.

import Foundation

// MARK: - EngineClient

/// Writes app-owned commands into the agent's queue. All paths derive from a
/// `StateStore` (which honors MP3TOM4B_SUPPORT_DIR), so tests can point the whole
/// write at a scratch tree without touching the user's real queue.
struct EngineClient {

    let store: StateStore

    init(store: StateStore) {
        self.store = store
    }

    // MARK: - Command payload

    /// The on-disk shape of a `confirm-build` command. Codable so the exact JSON
    /// keys are declarative (CodingKeys = the agent's contract) and unit-testable.
    struct ConfirmBuildCommand: Codable, Equatable {
        let cmdID: String
        let action: String          // always "confirm-build" here
        let bookID: String
        let sourceRev: String
        let confirmToken: String
        /// D17 — the agent's proof that this command was minted from a COMPLETE
        /// manifest. Echoed VERBATIM from `BookManifest.buildToken`; the agent
        /// compares it with the token on disk and rejects anything else
        /// (`manifest_not_ready` when the manifest has none — i.e. a command born
        /// on a skeleton — or `build_token_mismatch`). It exists as a separate
        /// token, and not as a phase check agent-side, precisely because a command
        /// can outlive the phase it was written in: see `BookManifest.isBuildReady`.
        let buildToken: String
        let idempotencyKey: String
        let params: Params
        let ts: Double

        /// Build parameters echoed from the manifest (D2/D6 defaults at M0.4) plus
        /// the cover selection (M1). The bitrate/channels/samplerate/split keys match
        /// agent/scan.py DEFAULT_PARAMS and the app's BookParams. `coverID` /
        /// `coverCustomPath` carry the user's cover pick into the build per the
        /// protocol decision (no separate `cover-choice` command — the choice is
        /// local window state that rides in confirm-build's params):
        ///   · coverID         → id of the chosen option in the manifest's
        ///     cover_options (build_m4b resolves the path by it);
        ///   · coverCustomPath → the ORIGINAL path of a user-chosen file
        ///     ("Заменить"); the AGENT copies it under covers/ (the app never writes
        ///     the support tree). Both are OMITTED from the JSON when nil so a
        ///     no-cover-pick command stays byte-identical to the M0.4 shape.
        struct Params: Codable, Equatable {
            let bitrate: Int
            let channels: String
            /// Output sample rate in Hz, or `nil` = "as in source". Sent as an
            /// EXPLICIT JSON `null` (not omitted) when nil so the wire mirrors the
            /// agent's `samplerate: None` sentinel verbatim — see `encode(to:)`.
            let samplerate: Int?
            let split: Bool
            /// Part-size threshold in MEGABYTES; the agent (split.py) converts MB→bytes.
            /// Always emitted (number) like `split`, matching scan.py DEFAULT_PARAMS.
            let splitThresholdMB: Int
            /// Build mode (D15): "fast" | "seamless". Always emitted (string) like
            /// `split` so the user's toggle always reaches the engine (P-PARAMS
            /// whitelist folds it command→manifest); matches scan.py DEFAULT_PARAMS.
            let buildMode: String
            let coverID: String?
            let coverCustomPath: String?

            enum CodingKeys: String, CodingKey {
                case bitrate, channels, samplerate, split
                case splitThresholdMB = "split_threshold_mb"
                case buildMode = "build_mode"
                case coverID = "cover_id"
                case coverCustomPath = "cover_custom_path"
            }

            init(from p: BookParams, coverID: String? = nil,
                 coverCustomPath: String? = nil) {
                self.bitrate = p.bitrate
                self.channels = p.channels
                self.samplerate = p.samplerate
                self.split = p.split
                self.splitThresholdMB = p.splitThresholdMB
                self.buildMode = p.buildMode
                self.coverID = coverID
                self.coverCustomPath = coverCustomPath
            }

            // Custom encode so `samplerate == nil` lands as an EXPLICIT `null` (the
            // synthesized encoder would OMIT a nil Optional via encodeIfPresent).
            // "As in source" must be visible on the wire as `samplerate: null`,
            // matching scan.py's `samplerate: None`. The cover keys keep the OMIT
            // behavior (encodeIfPresent) so a no-cover-pick command stays byte-
            // identical to the prior shape.
            func encode(to encoder: Encoder) throws {
                var c = encoder.container(keyedBy: CodingKeys.self)
                try c.encode(bitrate, forKey: .bitrate)
                try c.encode(channels, forKey: .channels)
                try c.encode(samplerate, forKey: .samplerate)   // nil → explicit null
                try c.encode(split, forKey: .split)
                try c.encode(splitThresholdMB, forKey: .splitThresholdMB)
                try c.encode(buildMode, forKey: .buildMode)
                try c.encodeIfPresent(coverID, forKey: .coverID)
                try c.encodeIfPresent(coverCustomPath, forKey: .coverCustomPath)
            }
        }

        enum CodingKeys: String, CodingKey {
            case cmdID = "cmd_id"
            case action
            case bookID = "book_id"
            case sourceRev = "source_rev"
            case confirmToken = "confirm_token"
            case buildToken = "build_token"
            case idempotencyKey = "idempotency_key"
            case params
            case ts
        }
    }

    /// The on-disk shape of a `grouping-choice` command (decisions D1, flows S4).
    /// Sent when the user answers the grouping sheet for a loose-mp3 set: it carries
    /// the GROUP's tokens (group_id / rev / token — the group has no book_id yet) and
    /// the choice. The agent validates rev+token against the live pending group, then
    /// materializes 1 (combine) or N (separate) book manifests — it never builds.
    ///
    ///     { cmd_id, action:"grouping-choice", group_id, rev, token,
    ///       idempotency_key, choice:"combine"|"separate", ts }
    ///
    /// Keys mirror agent/dispatcher.py `_handle_grouping_choice` + scan.py's group
    /// shape EXACTLY — a drift would be journaled as `grouping_rejected`.
    struct GroupingChoiceCommand: Codable, Equatable {
        let cmdID: String
        let action: String          // always "grouping-choice"
        let groupID: String
        let rev: String
        let token: String
        let idempotencyKey: String
        let choice: String          // "combine" | "separate"
        let ts: Double

        enum CodingKeys: String, CodingKey {
            case cmdID = "cmd_id"
            case action
            case groupID = "group_id"
            case rev, token
            case idempotencyKey = "idempotency_key"
            case choice, ts
        }
    }

    /// The two grouping choices (matches dispatcher.py constants). Using an enum
    /// keeps call-sites from typo-ing the wire string.
    enum GroupingChoice: String {
        case combine
        case separate
    }

    /// The on-disk shape of a `cancel` command (decision D13 — cooperative build
    /// cancellation; the agent-side mechanics are already proven by
    /// agent/selfcheck_cancel.py). Cancel targets a book BY ID only — unlike
    /// confirm-build it carries NO source_rev / confirm_token: the building agent
    /// matches by book_id, tears ffmpeg down, sweeps the temp, and lands the book
    /// back at pending-confirm (cancel ≠ failure). A cancel for a book that is no
    /// longer converting (already pending / done) is moot and dropped by the agent.
    ///
    ///     { cmd_id, action:"cancel", book_id, idempotency_key, ts }
    ///
    /// Keys mirror agent/selfcheck_cancel.py `_cancel_cmd` (= what dispatcher.py
    /// accepts) EXACTLY — a drift would be journaled to queue/commands/bad/.
    struct CancelCommand: Codable, Equatable {
        let cmdID: String
        let action: String          // always "cancel"
        let bookID: String
        let idempotencyKey: String
        let ts: Double

        enum CodingKeys: String, CodingKey {
            case cmdID = "cmd_id"
            case action
            case bookID = "book_id"
            case idempotencyKey = "idempotency_key"
            case ts
        }
    }

    /// The on-disk shape of a `reconvert` command («Собрать заново»). Sent from the
    /// queue's ГОТОВО section to rebuild an already-finished book with one click
    /// (instead of the non-obvious "rename the folder" workaround). Like `cancel` it
    /// targets a book BY ID only — it carries NO source_rev / confirm_token: the book
    /// is `done`, so there is no live token to echo. The agent re-arms the manifest
    /// `done` → `pending-confirm` with a fresh confirm_token AND a cleared idempotency
    /// ledger (so the same-source_rev rebuild is not deduped), then the normal confirm
    /// window → «Собрать» flow drives the build. A reconvert for a book that is not
    /// `done` (already pending / converting / error) is rejected as a no-op agent-side.
    ///
    ///     { cmd_id, action:"reconvert", book_id, idempotency_key, ts }
    ///
    /// Keys mirror agent/dispatcher.py `_handle_reconvert` (= what the dispatcher
    /// accepts) EXACTLY — a drift would be journaled to queue/commands/bad/.
    struct ReconvertCommand: Codable, Equatable {
        let cmdID: String
        let action: String          // always "reconvert"
        let bookID: String
        let idempotencyKey: String
        let ts: Double

        enum CodingKeys: String, CodingKey {
            case cmdID = "cmd_id"
            case action
            case bookID = "book_id"
            case idempotencyKey = "idempotency_key"
            case ts
        }
    }

    /// «Пропустить» (spec §3 footer `btn-skip`). Takes the book OFF the pipeline —
    /// the agent flips its manifest to `skipped`; the SOURCES ARE NEVER TOUCHED and
    /// nothing is built. Targets the book by id (like cancel/reconvert): no
    /// source_rev / confirm_token — there is no build to gate.
    ///
    ///     { cmd_id, action:"skip", book_id, idempotency_key, ts }
    ///
    /// Keys mirror agent/dispatcher.py `_handle_skip` EXACTLY. Accepted only for a
    /// `pending-confirm` or `error` book; anything else is a no-op reject agent-side.
    /// Reversible two ways: the queue's ПРОПУЩЕНО section has «Вернуть» (which sends
    /// a `reconvert`), and a conscious re-drop of the same folder re-arms the book on
    /// its own (agent lesson .patches/004 — a re-drop is user intent, not novelty).
    struct SkipCommand: Codable, Equatable {
        let cmdID: String
        let action: String          // always "skip"
        let bookID: String
        let idempotencyKey: String
        let ts: Double

        enum CodingKeys: String, CodingKey {
            case cmdID = "cmd_id"
            case action
            case bookID = "book_id"
            case idempotencyKey = "idempotency_key"
            case ts
        }
    }

    /// «Проверить снова» on the access card (plan v2 M5f + addendum §4.5).
    ///
    /// The lightest command in the protocol: no book, no manifest, no ledger, no
    /// build. The agent re-runs the folder probe (behind its own watchdog) and
    /// republishes `folder_access` + `folder_access_ts` — and `folder_access_ts`
    /// moves on EVERY probe, even when the verdict is unchanged, which is exactly
    /// what makes the app's wait terminable.
    ///
    /// Why a command file and not `launchctl kickstart`: kickstart without `-k` is
    /// a no-op on a job that is already running, so during a build the button would
    /// silently do nothing. `queue/commands/` is in the plist's `WatchPaths`, so
    /// dropping a file wakes the agent by itself — and if it is mid-build, the
    /// command simply waits its turn, which the UI reports as «проверим после
    /// сборки» rather than as a failure.
    struct RecheckAccessCommand: Codable, Equatable {
        let cmdID: String
        let action: String          // always "recheck-access"
        let ts: Double

        enum CodingKeys: String, CodingKey {
            case cmdID = "cmd_id"
            case action
            case ts
        }
    }

    /// Build the `recheck-access` payload (no I/O).
    func makeRecheckAccess() -> RecheckAccessCommand {
        RecheckAccessCommand(cmdID: UUID().uuidString,
                             action: "recheck-access",
                             ts: Date().timeIntervalSince1970)
    }

    // MARK: - Idempotency

    /// Stable idempotency key for "build THIS book at THIS revision". Two clicks on
    /// the same pending book (same `source_rev`) yield the SAME key, so the agent's
    /// `idempotency_key` dedup collapses them into a single build even if two
    /// command files slip through. A changed `source_rev` (edited inputs) yields a
    /// different key → a legitimately new build. Deterministic = no UUID here.
    ///
    /// Format: "<book_id>:<source_rev_prefix>" — book_id is already a sha256
    /// prefix and source_rev is a full sha256, so this is collision-safe and human
    /// readable in the queue. (cmd_id stays a fresh UUID per file, so files never
    /// clobber; dedup is the agent's job via idempotency_key.)
    static func idempotencyKey(bookID: String, sourceRev: String) -> String {
        "\(bookID):\(sourceRev.prefix(16))"
    }

    /// Build the command struct for a manifest (no I/O). Split out so it is unit
    /// testable and so callers can inspect/log the payload before writing.
    ///
    /// `params` lets the confirm window send the user's EDITED build settings
    /// (bitrate / channels / samplerate / split) instead of the manifest defaults.
    /// It defaults to the manifest's params so existing call-sites/tests are
    /// unchanged. The idempotency_key is keyed on book_id+source_rev ONLY (not the
    /// params) — re-clicking the same pending book collapses to one build even if
    /// the user toggled a preset between clicks; a changed source_rev (edited
    /// inputs) is what legitimately re-arms a new build.
    /// `coverID` is the id of the cover the user picked in the window (one of the
    /// manifest's cover_options). `coverCustomPath` is the original path of a
    /// user-chosen file ("Заменить") the agent will copy into covers/. Both are
    /// optional and rolled into `params` (the protocol carries the cover choice in
    /// confirm-build, not a separate command). The idempotency_key stays keyed on
    /// book_id+source_rev ONLY (not the cover) — re-clicking the same pending book
    /// collapses to one build even if the user re-picked the cover between clicks.
    func makeConfirmBuild(
        for manifest: BookManifest,
        params: BookParams? = nil,
        coverID: String? = nil,
        coverCustomPath: String? = nil
    ) -> ConfirmBuildCommand {
        ConfirmBuildCommand(
            cmdID: UUID().uuidString,
            action: "confirm-build",
            bookID: manifest.bookID,
            sourceRev: manifest.sourceRev,
            confirmToken: manifest.confirmToken,
            // Echoed as-is, including "" for a manifest that has none. An empty
            // token can only come from an incomplete manifest, and the agent
            // refuses it — which is the point: the gate is fail-closed, and the
            // button that produces this command is itself gated on
            // `manifest.isBuildReady`, so a "" here means something upstream lied.
            buildToken: manifest.buildToken,
            idempotencyKey: Self.idempotencyKey(
                bookID: manifest.bookID, sourceRev: manifest.sourceRev),
            params: .init(from: params ?? manifest.params,
                          coverID: coverID, coverCustomPath: coverCustomPath),
            ts: Date().timeIntervalSince1970
        )
    }

    /// Stable idempotency key for "resolve THIS group at THIS revision". Mirrors the
    /// book key format AND agent/scan.py `grouping_idempotency_key` exactly
    /// ("<group_id>:<rev_prefix16>") so a double-click on the sheet collapses to a
    /// single materialization; a changed loose set (new rev) re-arms a new decision.
    static func groupingIdempotencyKey(groupID: String, rev: String) -> String {
        "\(groupID):\(rev.prefix(16))"
    }

    /// Stable idempotency key for "cancel THIS book". Deterministic (no UUID): two
    /// clicks on the same converting row yield the SAME key. Cancel safety does NOT
    /// actually rely on this dedup — the agent makes a repeat cancel MOOT off the
    /// book's status (a 2nd cancel for an already-pending/done book is dropped) — but
    /// a stable key keeps the queue readable and the contract honest. The cmd_id
    /// stays a fresh UUID per file, so files never clobber each other.
    static func cancelIdempotencyKey(bookID: String) -> String {
        "cancel:\(bookID)"
    }

    /// Build the `cancel` command for a book (no I/O). Split out so it is
    /// unit-testable and inspectable before the write. `bookID` is the converting
    /// book's `book_id` (BookSummary.bookID) — the only field the agent needs to
    /// target the in-flight build.
    func makeCancel(bookID: String) -> CancelCommand {
        CancelCommand(
            cmdID: UUID().uuidString,
            action: "cancel",
            bookID: bookID,
            idempotencyKey: Self.cancelIdempotencyKey(bookID: bookID),
            ts: Date().timeIntervalSince1970
        )
    }

    /// Stable idempotency key for "reconvert THIS book". Deterministic (no UUID): two
    /// clicks on the same done row yield the SAME key. Reconvert safety does NOT rely
    /// on this dedup — the agent makes a repeat reconvert a no-op off the book's status
    /// (a 2nd reconvert for an already-re-armed / pending book is rejected
    /// `status_not_done`) — but a stable key keeps the queue readable and the contract
    /// honest. The cmd_id stays a fresh UUID per file, so files never clobber.
    static func reconvertIdempotencyKey(bookID: String) -> String {
        "reconvert:\(bookID)"
    }

    /// Idempotency key for a `skip`. Same shape/rationale as the reconvert key:
    /// deterministic per book, so a double click collapses to one skip.
    static func skipIdempotencyKey(bookID: String) -> String {
        "skip:\(bookID)"
    }

    /// Build the `skip` command for a book (no I/O) — unit-testable and inspectable
    /// before the write. `bookID` is all the agent needs to take the book off the
    /// pipeline; the sources are never touched.
    func makeSkip(bookID: String) -> SkipCommand {
        SkipCommand(
            cmdID: UUID().uuidString,
            action: "skip",
            bookID: bookID,
            idempotencyKey: Self.skipIdempotencyKey(bookID: bookID),
            ts: Date().timeIntervalSince1970
        )
    }

    /// Build the `reconvert` command for a book (no I/O). Split out so it is
    /// unit-testable and inspectable before the write. `bookID` is the done book's
    /// `book_id` (BookSummary.bookID) — the only field the agent needs to re-arm it.
    func makeReconvert(bookID: String) -> ReconvertCommand {
        ReconvertCommand(
            cmdID: UUID().uuidString,
            action: "reconvert",
            bookID: bookID,
            idempotencyKey: Self.reconvertIdempotencyKey(bookID: bookID),
            ts: Date().timeIntervalSince1970
        )
    }

    /// Build the grouping-choice command for a pending group (no I/O). Split out so
    /// it is unit-testable and inspectable before the write.
    func makeGroupingChoice(
        for group: PendingGroup, choice: GroupingChoice
    ) -> GroupingChoiceCommand {
        GroupingChoiceCommand(
            cmdID: UUID().uuidString,
            action: "grouping-choice",
            groupID: group.groupID,
            rev: group.rev,
            token: group.token,
            idempotencyKey: Self.groupingIdempotencyKey(
                groupID: group.groupID, rev: group.rev),
            choice: choice.rawValue,
            ts: Date().timeIntervalSince1970
        )
    }

    // MARK: - Atomic write

    /// Errors surfaced to the UI so a failed drop is loud, not silent.
    enum WriteError: Error {
        case encodeFailed(Error)
        case writeFailed(Error)
    }

    /// Atomically drop ONE encodable command (identified by `cmdID`) into
    /// queue/commands/ and return the URL that now exists. The JSON is written to a
    /// hidden temp file in the SAME directory and then renamed over the final name
    /// (same-filesystem rename = no half-file ever observable by the agent — the
    /// same tmp→replace guarantee the agent uses in state.py). Shared by every
    /// command kind so the atomic-drop contract lives in exactly one place.
    @discardableResult
    private func dropCommand<C: Encodable>(_ command: C, cmdID: String) throws -> URL {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data: Data
        do {
            data = try encoder.encode(command)
        } catch {
            throw WriteError.encodeFailed(error)
        }

        let dir = store.commandsDir
        let finalURL = store.commandURL(cmdID: cmdID)

        do {
            // Ensure queue/commands/ exists (the installer normally creates the
            // whole tree, but a dev run may not have it yet).
            try FileManager.default.createDirectory(
                at: dir, withIntermediateDirectories: true)

            // Temp file in the SAME directory → rename is atomic on one volume.
            let tmpURL = dir.appendingPathComponent(".\(cmdID).json.tmp")
            try data.write(to: tmpURL, options: .atomic)
            // Replace into place. The destination does not pre-exist (cmd_id is a
            // fresh UUID), so this is a plain rename; replaceItemAt also handles
            // the (impossible) collision case cleanly.
            _ = try FileManager.default.replaceItemAt(finalURL, withItemAt: tmpURL)
        } catch {
            throw WriteError.writeFailed(error)
        }

        return finalURL
    }

    /// Write a `confirm-build` command for `manifest` into queue/commands/ and
    /// return the URL of the file that now exists.
    @discardableResult
    func writeConfirmBuild(manifest: BookManifest, params: BookParams? = nil,
                           coverID: String? = nil,
                           coverCustomPath: String? = nil) throws -> URL {
        let command = makeConfirmBuild(for: manifest, params: params,
                                       coverID: coverID,
                                       coverCustomPath: coverCustomPath)
        return try dropCommand(command, cmdID: command.cmdID)
    }

    /// Write a `grouping-choice` command for `group` into queue/commands/ and return
    /// the URL that now exists. The agent validates rev+token against the live
    /// pending group, then materializes 1 (combine) / N (separate) book manifests.
    @discardableResult
    func writeGroupingChoice(group: PendingGroup,
                             choice: GroupingChoice) throws -> URL {
        let command = makeGroupingChoice(for: group, choice: choice)
        return try dropCommand(command, cmdID: command.cmdID)
    }

    /// Write a `cancel` command for `bookID` into queue/commands/ and return the URL
    /// that now exists. The building agent matches by book_id, kills ffmpeg, sweeps
    /// the temp, and lands the book back at pending-confirm (file-watch then clears
    /// the converting row). A cancel for a non-converting book is moot (agent-side).
    @discardableResult
    func writeCancel(bookID: String) throws -> URL {
        let command = makeCancel(bookID: bookID)
        return try dropCommand(command, cmdID: command.cmdID)
    }

    /// Write a `reconvert` command for `bookID` into queue/commands/ and return the
    /// URL that now exists. The agent re-arms the done book back to pending-confirm
    /// (fresh confirm_token + cleared idempotency ledger), so it leaves the ГОТОВО
    /// section, reappears under ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ, and the confirm window surfaces
    /// via the file-watch → the user presses «Собрать» to rebuild. A reconvert for a
    /// non-done book is a no-op (agent-side).
    @discardableResult
    func writeReconvert(bookID: String) throws -> URL {
        let command = makeReconvert(bookID: bookID)
        return try dropCommand(command, cmdID: command.cmdID)
    }

    /// Write a `skip` command for `bookID`. The agent marks the manifest `skipped`
    /// (sources untouched, nothing built), so the book leaves ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ
    /// and appears in the queue's ПРОПУЩЕНО section — where «Вернуть» brings it back.
    @discardableResult
    func writeSkip(bookID: String) throws -> URL {
        let command = makeSkip(bookID: bookID)
        return try dropCommand(command, cmdID: command.cmdID)
    }

    /// Write a `recheck-access` command and return the URL that now exists. The
    /// caller then waits for `agent.folder_access_ts` to MOVE (never for a
    /// particular verdict — "checked again, still denied" is the most common
    /// outcome and must terminate the wait just as cleanly as a success).
    @discardableResult
    func writeRecheckAccess() throws -> URL {
        let command = makeRecheckAccess()
        return try dropCommand(command, cmdID: command.cmdID)
    }
}
