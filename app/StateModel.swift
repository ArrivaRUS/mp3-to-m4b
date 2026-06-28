// StateModel — the app's READ-ONLY view of the agent's authoritative files.
//
// The agent (python `agent` package) is the SINGLE writer (arch/synthesis.md §B);
// the app only ever reads. Two file kinds matter at M0.3:
//
//   <support>/state/state.json            — the lightweight showcase
//     { schema, agent:{watch_dir}, books:[{book_id,title,status}],
//       batch:{active,total,done}, totals:{books}, ts }
//
//   <support>/queue/books/<book_id>.json   — per-book manifest (rich detail)
//     { book_id, src_dir, status, source_rev, confirm_token,
//       chapters:[{index,file,name}], cover_state, params:{…}, ts }
//
// Both are written atomically (tmp → rename), but the app can still race a read
// against the swap, so decoding is DEFENSIVE: a missing file / half-written file /
// unknown extra keys must degrade to an empty-but-valid value, never crash. The
// pattern (per-field `try?` decode init) is cloned from the fb2-to-epub neighbor's
// StateModel.swift.
//
// The support-dir root honors MP3TOM4B_SUPPORT_DIR (matches agent/config.py) so a
// dev/QA run can point the whole tree at a scratch location.

import Foundation

// MARK: - state.json — showcase

/// One row in the showcase `books[]`. Light by design (id/title/status only); the
/// rich per-book data lives in the manifest so the showcase isn't rewritten on
/// every chapter-level change.
struct BookSummary: Codable, Identifiable, Equatable {
    let bookID: String
    let title: String
    let status: String

    var id: String { bookID }

    /// Awaiting the user's "Собрать" — the trigger for the popup (rising-edge).
    var isPendingConfirm: Bool { status == "pending-confirm" }

    enum CodingKeys: String, CodingKey {
        case bookID = "book_id"
        case title
        case status
    }

    init(bookID: String, title: String, status: String) {
        self.bookID = bookID
        self.title = title
        self.status = status
    }

    // Tolerate missing fields inside an otherwise-present book object: a row with
    // no id is useless, so it falls back to "" (callers filter empties out).
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bookID = (try? c.decode(String.self, forKey: .bookID)) ?? ""
        title  = (try? c.decode(String.self, forKey: .title)) ?? ""
        status = (try? c.decode(String.self, forKey: .status)) ?? ""
    }
}

/// `agent` block of the showcase — currently just the watched folder.
struct AgentInfo: Codable, Equatable {
    var watchDir: String?

    enum CodingKeys: String, CodingKey {
        case watchDir = "watch_dir"
    }
}

/// Live batch progress (`batch` in state.json). Absent on idle / older state →
/// `nil` ("no active batch"). `done`/`total` count books in the current run.
struct BatchProgress: Codable, Equatable {
    var active: Bool
    var total: Int
    var done: Int

    enum CodingKeys: String, CodingKey {
        case active, total, done
    }

    init(active: Bool, total: Int, done: Int) {
        self.active = active
        self.total = total
        self.done = done
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        active = (try? c.decode(Bool.self, forKey: .active)) ?? false
        total  = (try? c.decode(Int.self,  forKey: .total)) ?? 0
        done   = (try? c.decode(Int.self,  forKey: .done)) ?? 0
    }
}

/// The full `state.json` showcase. Extra keys (e.g. `totals`, `ts`) are ignored.
struct ShowcaseState: Codable, Equatable {
    var schema: Int
    var agent: AgentInfo
    var books: [BookSummary]
    var batch: BatchProgress?

    enum CodingKeys: String, CodingKey {
        case schema, agent, books, batch
    }

    static let empty = ShowcaseState(
        schema: 1, agent: AgentInfo(watchDir: nil), books: [], batch: nil)

    init(schema: Int, agent: AgentInfo, books: [BookSummary], batch: BatchProgress?) {
        self.schema = schema
        self.agent = agent
        self.books = books
        self.batch = batch
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schema = (try? c.decode(Int.self, forKey: .schema)) ?? 1
        agent  = (try? c.decode(AgentInfo.self, forKey: .agent)) ?? AgentInfo(watchDir: nil)
        // Drop any rows that decoded with an empty id (half-written / malformed).
        let raw = (try? c.decode([BookSummary].self, forKey: .books)) ?? []
        books  = raw.filter { !$0.bookID.isEmpty }
        batch  = try? c.decodeIfPresent(BatchProgress.self, forKey: .batch)
    }

    /// Books still awaiting confirmation, in showcase order.
    var pendingConfirm: [BookSummary] { books.filter { $0.isPendingConfirm } }
}

// MARK: - queue/books/<book_id>.json — per-book manifest

/// One chapter row in a manifest. Duration arrives at M0.5 (ffprobe); until then
/// the manifest carries none, so `durationSeconds` is optional and the UI shows
/// an em-dash placeholder.
struct ChapterEntry: Codable, Identifiable, Equatable {
    let index: Int
    let file: String
    let name: String
    /// Seconds, present once M0.5 probes durations (key `duration` — absent now).
    let durationSeconds: Double?

    var id: Int { index }

    enum CodingKeys: String, CodingKey {
        case index, file, name
        case durationSeconds = "duration"
    }

    init(index: Int, file: String, name: String, durationSeconds: Double? = nil) {
        self.index = index
        self.file = file
        self.name = name
        self.durationSeconds = durationSeconds
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        index = (try? c.decode(Int.self, forKey: .index)) ?? 0
        file  = (try? c.decode(String.self, forKey: .file)) ?? ""
        name  = (try? c.decode(String.self, forKey: .name)) ?? ""
        durationSeconds = try? c.decodeIfPresent(Double.self, forKey: .durationSeconds)
    }
}

/// Build parameters for a book (decisions D2/D6 defaults: 192 · stereo · 44100 ·
/// no split). Read-only here at M0.3; editing/echo back is M0.4+.
struct BookParams: Codable, Equatable {
    var bitrate: Int
    var channels: String
    var samplerate: Int
    var split: Bool

    static let defaults = BookParams(
        bitrate: 192, channels: "stereo", samplerate: 44100, split: false)

    init(bitrate: Int, channels: String, samplerate: Int, split: Bool) {
        self.bitrate = bitrate
        self.channels = channels
        self.samplerate = samplerate
        self.split = split
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bitrate    = (try? c.decode(Int.self, forKey: .bitrate)) ?? 192
        channels   = (try? c.decode(String.self, forKey: .channels)) ?? "stereo"
        samplerate = (try? c.decode(Int.self, forKey: .samplerate)) ?? 44100
        split      = (try? c.decode(Bool.self, forKey: .split)) ?? false
    }
}

/// The full per-book manifest. The app reads it to render the confirm window; the
/// agent owns/writes it. Extra keys (`ts`) are ignored.
struct BookManifest: Codable, Equatable {
    let bookID: String
    let srcDir: String
    let status: String
    let sourceRev: String
    let confirmToken: String
    let chapters: [ChapterEntry]
    let coverState: String
    let params: BookParams

    enum CodingKeys: String, CodingKey {
        case bookID = "book_id"
        case srcDir = "src_dir"
        case status
        case sourceRev = "source_rev"
        case confirmToken = "confirm_token"
        case chapters
        case coverState = "cover_state"
        case params
    }

    var isPendingConfirm: Bool { status == "pending-confirm" }

    init(bookID: String, srcDir: String, status: String, sourceRev: String,
         confirmToken: String, chapters: [ChapterEntry], coverState: String,
         params: BookParams) {
        self.bookID = bookID
        self.srcDir = srcDir
        self.status = status
        self.sourceRev = sourceRev
        self.confirmToken = confirmToken
        self.chapters = chapters
        self.coverState = coverState
        self.params = params
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bookID       = (try? c.decode(String.self, forKey: .bookID)) ?? ""
        srcDir       = (try? c.decode(String.self, forKey: .srcDir)) ?? ""
        status       = (try? c.decode(String.self, forKey: .status)) ?? ""
        sourceRev    = (try? c.decode(String.self, forKey: .sourceRev)) ?? ""
        confirmToken = (try? c.decode(String.self, forKey: .confirmToken)) ?? ""
        chapters     = (try? c.decode([ChapterEntry].self, forKey: .chapters)) ?? []
        coverState   = (try? c.decode(String.self, forKey: .coverState)) ?? "unknown"
        params       = (try? c.decode(BookParams.self, forKey: .params)) ?? .defaults
    }
}

// MARK: - Store (paths + defensive loaders)

/// Reads the agent's files from the Application Support tree. All paths derive
/// from `supportRoot`, which honors MP3TOM4B_SUPPORT_DIR (matches agent/config.py)
/// so dev/QA runs redirect the whole tree without touching real data.
struct StateStore {
    let supportRoot: URL

    init() {
        if let override = ProcessInfo.processInfo.environment["MP3TOM4B_SUPPORT_DIR"],
           !override.isEmpty {
            self.supportRoot = URL(fileURLWithPath: (override as NSString).expandingTildeInPath,
                                   isDirectory: true)
        } else {
            let home = NSHomeDirectory()
            self.supportRoot = URL(fileURLWithPath: home, isDirectory: true)
                .appendingPathComponent("Library/Application Support/mp3-to-m4b", isDirectory: true)
        }
    }

    /// Allow tests to pin an explicit root.
    init(supportRoot: URL) { self.supportRoot = supportRoot }

    var stateDir: URL { supportRoot.appendingPathComponent("state", isDirectory: true) }
    var stateFile: URL { stateDir.appendingPathComponent("state.json") }
    var booksDir: URL {
        supportRoot.appendingPathComponent("queue/books", isDirectory: true)
    }
    /// queue/commands/ — the ONLY directory the app writes to (app-owned commands;
    /// matches agent/config.py `commands_dir`). Also a launchd WatchPaths entry, so
    /// dropping a file here wakes the agent without a new mp3 (synthesis §B).
    var commandsDir: URL {
        supportRoot.appendingPathComponent("queue/commands", isDirectory: true)
    }

    func manifestURL(bookID: String) -> URL {
        booksDir.appendingPathComponent("\(bookID).json")
    }

    func commandURL(cmdID: String) -> URL {
        commandsDir.appendingPathComponent("\(cmdID).json")
    }

    /// Load + decode the showcase. Returns `.empty` for any failure (absent /
    /// unreadable / malformed) so the UI always has a valid model to render.
    func loadState() -> ShowcaseState {
        guard let data = try? Data(contentsOf: stateFile),
              let state = try? JSONDecoder().decode(ShowcaseState.self, from: data)
        else { return .empty }
        return state
    }

    /// Load + decode one book manifest. `nil` on any failure (absent / half-written).
    func loadManifest(bookID: String) -> BookManifest? {
        guard let data = try? Data(contentsOf: manifestURL(bookID: bookID)),
              let m = try? JSONDecoder().decode(BookManifest.self, from: data)
        else { return nil }
        return m
    }
}
