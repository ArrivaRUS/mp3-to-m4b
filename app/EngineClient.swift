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
//       idempotency_key, params:{bitrate,channels,samplerate,split}, ts }
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
        let idempotencyKey: String
        let params: Params
        let ts: Double

        /// Build parameters echoed from the manifest (D2/D6 defaults at M0.4).
        /// Keys match agent/scan.py DEFAULT_PARAMS and the app's BookParams.
        struct Params: Codable, Equatable {
            let bitrate: Int
            let channels: String
            let samplerate: Int
            let split: Bool

            enum CodingKeys: String, CodingKey {
                case bitrate, channels, samplerate, split
            }

            init(from p: BookParams) {
                self.bitrate = p.bitrate
                self.channels = p.channels
                self.samplerate = p.samplerate
                self.split = p.split
            }
        }

        enum CodingKeys: String, CodingKey {
            case cmdID = "cmd_id"
            case action
            case bookID = "book_id"
            case sourceRev = "source_rev"
            case confirmToken = "confirm_token"
            case idempotencyKey = "idempotency_key"
            case params
            case ts
        }
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
    func makeConfirmBuild(for manifest: BookManifest) -> ConfirmBuildCommand {
        ConfirmBuildCommand(
            cmdID: UUID().uuidString,
            action: "confirm-build",
            bookID: manifest.bookID,
            sourceRev: manifest.sourceRev,
            confirmToken: manifest.confirmToken,
            idempotencyKey: Self.idempotencyKey(
                bookID: manifest.bookID, sourceRev: manifest.sourceRev),
            params: .init(from: manifest.params),
            ts: Date().timeIntervalSince1970
        )
    }

    // MARK: - Atomic write

    /// Errors surfaced to the UI so a failed drop is loud, not silent.
    enum WriteError: Error {
        case encodeFailed(Error)
        case writeFailed(Error)
    }

    /// Write a `confirm-build` command for `manifest` into queue/commands/ and
    /// return the URL of the file that now exists. Atomic: the JSON is written to a
    /// hidden temp file in the SAME directory and then renamed over the final name
    /// (same-filesystem rename = no half-file ever observable by the agent — the
    /// same tmp→replace guarantee the agent uses in state.py).
    @discardableResult
    func writeConfirmBuild(manifest: BookManifest) throws -> URL {
        let command = makeConfirmBuild(for: manifest)

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data: Data
        do {
            data = try encoder.encode(command)
        } catch {
            throw WriteError.encodeFailed(error)
        }

        let dir = store.commandsDir
        let finalURL = store.commandURL(cmdID: command.cmdID)

        do {
            // Ensure queue/commands/ exists (the installer normally creates the
            // whole tree, but a dev run may not have it yet).
            try FileManager.default.createDirectory(
                at: dir, withIntermediateDirectories: true)

            // Temp file in the SAME directory → rename is atomic on one volume.
            let tmpURL = dir.appendingPathComponent(
                ".\(command.cmdID).json.tmp")
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
}
