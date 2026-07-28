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
//     { book_id, src_dir, status, source_rev, confirm_token, title, author,
//       chapters:[{index,file,name,duration_ms}], total_duration_ms,
//       cover_state, cover_preview, params:{…}, ts }
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

/// Live build progress carried on a *converting* showcase row (`books[i].progress`).
/// The agent's dispatcher.py patches this dict into state.json off ffmpeg's
/// `-progress` stream (throttled ~1.5s) and the converting window renders a
/// DETERMINATE bar + chapter/elapsed/ETA lines from it. By the agent contract it
/// exists ONLY while `status == "converting"` — a done/error/pending row carries
/// no `progress`, so its absence is the "no live build" signal (the UI then shows
/// "Запуск…" until the first snapshot lands).
///
/// Contract (status.md / arch/speedup-synthesis.md):
///   { percent:0..100, out_time_ms, total_ms, elapsed_s, eta_s|null,
///     current_chapter_index|null, current_chapter_name|null, total_chapters }
///
/// Decoding is optional-tolerant like every model here: each field degrades to a
/// safe default (0 / nil) so a half-written snapshot or an older shape never
/// crashes the reader. `eta_s` / `current_chapter_*` are genuinely nullable in the
/// contract (the agent emits `null` early in the encode), so they stay optionals.
struct BuildProgress: Codable, Equatable {
    /// 0…100, already clamped by the agent. Drives `ProgressView(value:)`.
    let percent: Double
    /// Encoded position / total timeline in ms (kept for completeness/diagnostics).
    let outTimeMS: Int
    let totalMS: Int
    /// Wall seconds since ffmpeg started — the "Прошло mm:ss" line.
    let elapsedS: Int
    /// Estimated seconds remaining; `nil` until the agent has a stable estimate
    /// (early ticks) → the UI shows "оцениваю…" instead of a wild number.
    let etaS: Int?
    /// 1-based index of the chapter being encoded now; `nil` if not yet known.
    let currentChapterIndex: Int?
    /// Name of that chapter; `nil`/"" when unknown.
    let currentChapterName: String?
    /// Count of usable chapters (the "из Y" in "Глава X из Y").
    let totalChapters: Int

    /// `percent` as a 0…1 fraction for `ProgressView(value:total:)`.
    var fraction: Double { max(0, min(1, percent / 100.0)) }

    enum CodingKeys: String, CodingKey {
        case percent
        case outTimeMS = "out_time_ms"
        case totalMS = "total_ms"
        case elapsedS = "elapsed_s"
        case etaS = "eta_s"
        case currentChapterIndex = "current_chapter_index"
        case currentChapterName = "current_chapter_name"
        case totalChapters = "total_chapters"
    }

    init(percent: Double, outTimeMS: Int = 0, totalMS: Int = 0, elapsedS: Int = 0,
         etaS: Int? = nil, currentChapterIndex: Int? = nil,
         currentChapterName: String? = nil, totalChapters: Int = 0) {
        self.percent = percent
        self.outTimeMS = outTimeMS
        self.totalMS = totalMS
        self.elapsedS = elapsedS
        self.etaS = etaS
        self.currentChapterIndex = currentChapterIndex
        self.currentChapterName = currentChapterName
        self.totalChapters = totalChapters
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        percent = (try? c.decodeIfPresent(Double.self, forKey: .percent)) ?? 0
        outTimeMS = (try? c.decodeIfPresent(Int.self, forKey: .outTimeMS)) ?? 0
        totalMS = (try? c.decodeIfPresent(Int.self, forKey: .totalMS)) ?? 0
        elapsedS = (try? c.decodeIfPresent(Int.self, forKey: .elapsedS)) ?? 0
        etaS = (try? c.decodeIfPresent(Int.self, forKey: .etaS)) ?? nil
        currentChapterIndex = (try? c.decodeIfPresent(Int.self, forKey: .currentChapterIndex)) ?? nil
        currentChapterName = (try? c.decodeIfPresent(String.self, forKey: .currentChapterName)) ?? nil
        totalChapters = (try? c.decodeIfPresent(Int.self, forKey: .totalChapters)) ?? 0
    }
}

/// One row in the showcase `books[]`. Light by design — but scan.py's
/// `_build_state` carries a little more than id/title/status per row
/// (`author`, `total_duration_ms`, `chapters` count), enough for the QUEUE's qrow
/// sub-line ("Автор · N глав · ~T") without loading every manifest. The richer
/// per-book data (cover preview, result path) still lives in the manifest.
struct BookSummary: Codable, Identifiable, Equatable {
    let bookID: String
    let title: String
    let status: String
    /// Author resolved by the agent (ID3 → folder fallback); "" when unknown.
    let author: String
    /// Chapter count (scan.py writes `len(chapters)` into the showcase row).
    let chapterCount: Int
    /// Sum of readable chapter durations in ms (showcase `total_duration_ms`).
    let totalDurationMS: Int
    /// Live build progress, present ONLY while `status == "converting"` (the agent
    /// patches `books[i].progress` off ffmpeg's `-progress` stream; absent otherwise
    /// by contract). The converting window renders its determinate bar from this.
    let progress: BuildProgress?

    var id: String { bookID }

    /// Awaiting the user's "Собрать" — the trigger for the popup (rising-edge).
    var isPendingConfirm: Bool { status == "pending-confirm" }
    var isConverting: Bool { status == "converting" }
    var isDone: Bool { status == "done" }
    var isError: Bool { status == "error" }
    /// Taken off the pipeline by «Пропустить» (agent `skip`). Sources are intact;
    /// the scan never re-arms it. Deliberately NOT `isActive` — a skipped book must
    /// never be presented in the confirm window — it lives in the queue's
    /// ПРОПУЩЕНО section, where «Вернуть» re-arms it (a re-drop does too).
    var isSkipped: Bool { status == "skipped" }
    /// Books the confirm window should surface: awaiting confirm, mid-build, or
    /// failed (so the window reflects the live build, not only the pending step).
    var isActive: Bool { isPendingConfirm || isConverting || isError }

    /// Seconds for the queue sub-line. 0 ⇒ unknown (M0.5 not probed yet).
    var totalSeconds: Double { Double(totalDurationMS) / 1000.0 }

    enum CodingKeys: String, CodingKey {
        case bookID = "book_id"
        case title
        case status
        case author
        case chapterCount = "chapters"
        case totalDurationMS = "total_duration_ms"
        case progress
    }

    init(bookID: String, title: String, status: String,
         author: String = "", chapterCount: Int = 0, totalDurationMS: Int = 0,
         progress: BuildProgress? = nil) {
        self.bookID = bookID
        self.title = title
        self.status = status
        self.author = author
        self.chapterCount = chapterCount
        self.totalDurationMS = totalDurationMS
        self.progress = progress
    }

    // Tolerate missing fields inside an otherwise-present book object: a row with
    // no id is useless, so it falls back to "" (callers filter empties out). The
    // queue-only fields (author/chapters/duration) default to empty/0 on older
    // states so an old state.json still decodes.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bookID = (try? c.decode(String.self, forKey: .bookID)) ?? ""
        title  = (try? c.decode(String.self, forKey: .title)) ?? ""
        status = (try? c.decode(String.self, forKey: .status)) ?? ""
        author = (try? c.decodeIfPresent(String.self, forKey: .author)) ?? ""
        chapterCount = (try? c.decodeIfPresent(Int.self, forKey: .chapterCount)) ?? 0
        totalDurationMS = (try? c.decodeIfPresent(Int.self, forKey: .totalDurationMS)) ?? 0
        // Live build progress — present only on a converting row; tolerate absence
        // (every other status) and a half-written snapshot (→ nil) without crashing.
        progress = try? c.decodeIfPresent(BuildProgress.self, forKey: .progress)
    }
}

/// The agent's verdict on whether it can actually READ the watched folder
/// (`agent.folder_access` in state.json — plan v2 M4 + addendum §4.1).
///
/// FOUR known values, and they are NOT interchangeable — the addendum measured
/// what each one costs the user if we merge them:
///   · `ok`      — the probe listed the folder; nothing to show.
///   · `denied`  — TCC has a "no" on record (the user pressed «Не разрешать»), or
///                 plain chmod/ACL. The refusal is instant. The fix is a settings
///                 trip / a folder outside the protected zone.
///   · `blocked` — no decision exists yet: macOS is holding the call open while it
///                 waits for the user to answer the CONSENT DIALOG. The fix is to
///                 look at the screen and press «Разрешить». Telling this user to
///                 go to System Settings is wrong; telling a `denied` user to wait
///                 for a dialog that will never appear is worse.
///   · `missing` — the folder is gone.
///
/// `unknown(raw)` is the FIFTH, deliberate case: a value this app build has never
/// heard of. The tempting shape is "unknown → nil ⇒ no surface", and that is
/// exactly how the neighbour shipped a lie — a newer agent published a problem
/// state and the older UI rendered a calm "всё хорошо". So an unrecognized value
/// is CARRIED, not dropped, and the router turns it into its own honest surface
/// (`StatusSurface.accessUnknown`). Absent / empty stays nil = "the agent has not
/// told us anything yet", which is a different fact from "it told us something we
/// don't understand".
enum FolderAccess: Equatable {
    case ok
    case denied
    case blocked
    case missing
    case unknown(String)

    /// The exact strings the agent writes (agent/scan.py). Anything else is
    /// preserved verbatim as `.unknown`.
    init(raw: String) {
        switch raw {
        case "ok": self = .ok
        case "denied": self = .denied
        case "blocked": self = .blocked
        case "missing": self = .missing
        default: self = .unknown(raw)
        }
    }

    var rawValue: String {
        switch self {
        case .ok: return "ok"
        case .denied: return "denied"
        case .blocked: return "blocked"
        case .missing: return "missing"
        case .unknown(let raw): return raw
        }
    }

    /// The values that OWN the window when the access gate holds (a card, not a
    /// silent status). `ok` needs nothing; `unknown` gets its own surface because
    /// we cannot honestly claim to know what it means.
    var needsSurface: Bool {
        switch self {
        case .denied, .blocked, .missing: return true
        case .ok, .unknown: return false
        }
    }
}

extension FolderAccess: Codable {
    init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        self = FolderAccess(raw: try c.decode(String.self))
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        try c.encode(rawValue)
    }
}

/// `agent` block of the showcase — the watched folder + a liveness flag the agent
/// stamps when it writes state (scan.py `build_state`: `agent.active = true`). The
/// Status screen (spec §5) reads `active` for the "Активен / Пауза" pill; absent on
/// older states → defaults to `true` (the file exists ⇒ the agent has run).
///
/// Three more fields land here with release 1.0 (plan v2 B3 / M4):
///   · `folder_access` + `folder_access_ts` — the probe verdict and its opaque
///     freshness token (the app compares tokens, it never parses the instant);
///   · `install_generation` — the UUID launchd handed the agent through the plist
///     env. Present ONLY when launchd started us, so a hand-run agent says nothing
///     instead of lying with a stale value. Compared against the receipt's
///     generation: that comparison is the ONLY proof that the job launchd is
///     actually running is the job we installed (a correct plist on disk is not).
struct AgentInfo: Codable, Equatable {
    var watchDir: String?
    var active: Bool
    /// The probe verdict. nil = the agent has not published one yet (older state,
    /// or a scan that predates 1.0) — distinct from `.unknown`, which means it
    /// published something this build does not recognize.
    var folderAccess: FolderAccess?
    /// Opaque freshness token for `folderAccess` (ISO-8601 UTC as written, but the
    /// app treats it as an opaque string: "changed" is the only question it asks).
    var folderAccessTs: String?
    /// The install generation launchd passed through. nil ⇒ no proof.
    var installGeneration: String?

    enum CodingKeys: String, CodingKey {
        case watchDir = "watch_dir"
        case active
        case folderAccess = "folder_access"
        case folderAccessTs = "folder_access_ts"
        case installGeneration = "install_generation"
    }

    init(watchDir: String?, active: Bool = true,
         folderAccess: FolderAccess? = nil, folderAccessTs: String? = nil,
         installGeneration: String? = nil) {
        self.watchDir = watchDir
        self.active = active
        self.folderAccess = folderAccess
        self.folderAccessTs = folderAccessTs
        self.installGeneration = installGeneration
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        watchDir = try? c.decodeIfPresent(String.self, forKey: .watchDir)
        active = (try? c.decodeIfPresent(Bool.self, forKey: .active)) ?? true
        // Decode the raw string ourselves: an unrecognized value must survive as
        // `.unknown(raw)` (see FolderAccess). An empty string is treated as absent.
        let rawAccess = (try? c.decodeIfPresent(String.self, forKey: .folderAccess)) ?? nil
        folderAccess = (rawAccess?.isEmpty == false) ? FolderAccess(raw: rawAccess!) : nil
        let rawTs = (try? c.decodeIfPresent(String.self, forKey: .folderAccessTs)) ?? nil
        folderAccessTs = (rawTs?.isEmpty == false) ? rawTs : nil
        let rawGen = (try? c.decodeIfPresent(String.self, forKey: .installGeneration)) ?? nil
        installGeneration = (rawGen?.isEmpty == false) ? rawGen : nil
    }
}

/// `totals` block of the showcase (scan.py `_project_totals`) — the Status stat
/// cards «Собрано» / «За сегодня» (spec §5). `built` = books at status done,
/// `today` = of those built on the current local day, `books` = showcase row count
/// (preserved for backward-compat). All default to 0 on an older/absent block.
struct ShowcaseTotals: Codable, Equatable {
    var built: Int
    var today: Int
    var books: Int

    static let zero = ShowcaseTotals(built: 0, today: 0, books: 0)

    enum CodingKeys: String, CodingKey {
        case built, today, books
    }

    init(built: Int, today: Int, books: Int) {
        self.built = built
        self.today = today
        self.books = books
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        built = (try? c.decodeIfPresent(Int.self, forKey: .built)) ?? 0
        today = (try? c.decodeIfPresent(Int.self, forKey: .today)) ?? 0
        books = (try? c.decodeIfPresent(Int.self, forKey: .books)) ?? 0
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

/// A set of LOOSE mp3 files sitting in the watch ROOT, awaiting the user's
/// grouping decision (agent scan.py `_build_pending_group`, decisions D1). The app
/// renders these in the grouping sheet (spec §6 / ref 06) and, on "Продолжить",
/// echoes `groupID`/`rev`/`token` back in a `grouping-choice` command. The agent
/// writes NO book manifest for these until the choice is made.
///
///   { group_id, status:"grouping-ask", rev, token, watch_dir,
///     files:[name], count, total_duration_ms, ts }
struct PendingGroup: Codable, Identifiable, Equatable {
    let groupID: String
    let rev: String
    let token: String
    /// File NAMES (not paths) in natural/track order — the chips in the sheet.
    let files: [String]
    let count: Int
    /// Sum of the loose files' durations in ms (nil/0 if unprobed).
    let totalDurationMS: Int

    var id: String { groupID }

    /// Seconds for the sheet's "N глав · ~X" line. 0 ⇒ unknown.
    var totalSeconds: Double { Double(totalDurationMS) / 1000.0 }

    enum CodingKeys: String, CodingKey {
        case groupID = "group_id"
        case rev, token, files, count
        case totalDurationMS = "total_duration_ms"
    }

    init(groupID: String, rev: String, token: String, files: [String],
         count: Int, totalDurationMS: Int) {
        self.groupID = groupID
        self.rev = rev
        self.token = token
        self.files = files
        self.count = count
        self.totalDurationMS = totalDurationMS
    }

    // Defensive per-field decode (mirrors the manifest pattern): a half-written
    // state must degrade, never crash. A group with no id is useless → "" (filtered
    // by ShowcaseState). `count` falls back to the files array length.
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        groupID = (try? c.decode(String.self, forKey: .groupID)) ?? ""
        rev     = (try? c.decode(String.self, forKey: .rev)) ?? ""
        token   = (try? c.decode(String.self, forKey: .token)) ?? ""
        let f   = (try? c.decode([String].self, forKey: .files)) ?? []
        files   = f
        count   = (try? c.decodeIfPresent(Int.self, forKey: .count)) ?? f.count
        totalDurationMS = (try? c.decodeIfPresent(Int.self, forKey: .totalDurationMS)) ?? 0
    }
}

/// The full `state.json` showcase. Extra keys (e.g. `totals`, `ts`,
/// `grouping_processed`) are ignored.
struct ShowcaseState: Codable, Equatable {
    var schema: Int
    var agent: AgentInfo
    var books: [BookSummary]
    /// Loose-mp3 sets awaiting a grouping decision (scan.py `pending_groups`).
    /// A list for forward-compat; today 0..1. Empty when none.
    var pendingGroups: [PendingGroup]
    var batch: BatchProgress?
    /// Stat-card counters (scan.py `totals`): «Собрано»/«За сегодня» (spec §5).
    var totals: ShowcaseTotals
    /// ffmpeg version string for the «ffmpeg» stat card (scan.py `engine`). Empty
    /// when ffmpeg is absent / not yet probed → the card shows a placeholder.
    var engine: String

    enum CodingKeys: String, CodingKey {
        case schema, agent, books, batch, totals, engine
        case pendingGroups = "pending_groups"
    }

    static let empty = ShowcaseState(
        schema: 1, agent: AgentInfo(watchDir: nil), books: [], pendingGroups: [],
        batch: nil, totals: .zero, engine: "")

    init(schema: Int, agent: AgentInfo, books: [BookSummary],
         pendingGroups: [PendingGroup] = [], batch: BatchProgress?,
         totals: ShowcaseTotals = .zero, engine: String = "") {
        self.schema = schema
        self.agent = agent
        self.books = books
        self.pendingGroups = pendingGroups
        self.batch = batch
        self.totals = totals
        self.engine = engine
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        schema = (try? c.decode(Int.self, forKey: .schema)) ?? 1
        agent  = (try? c.decode(AgentInfo.self, forKey: .agent)) ?? AgentInfo(watchDir: nil)
        // Drop any rows that decoded with an empty id (half-written / malformed).
        let raw = (try? c.decode([BookSummary].self, forKey: .books)) ?? []
        books  = raw.filter { !$0.bookID.isEmpty }
        let rawGroups = (try? c.decode([PendingGroup].self, forKey: .pendingGroups)) ?? []
        pendingGroups = rawGroups.filter { !$0.groupID.isEmpty }
        batch  = try? c.decodeIfPresent(BatchProgress.self, forKey: .batch)
        totals = (try? c.decodeIfPresent(ShowcaseTotals.self, forKey: .totals)) ?? .zero
        engine = (try? c.decodeIfPresent(String.self, forKey: .engine)) ?? ""
    }

    /// Books still awaiting confirmation, in showcase order.
    var pendingConfirm: [BookSummary] { books.filter { $0.isPendingConfirm } }

    /// Books in any "active" stage the confirm window can present (pending-confirm,
    /// converting, or error), in showcase order. With no explicit pick the window
    /// shows the FIRST of these; the rising-edge raise still keys on pending-confirm
    /// only. See `presentedBook(selectedID:)` for the full routing rule.
    var activeBooks: [BookSummary] { books.filter { $0.isActive } }

    // MARK: Confirm-window routing (WHICH book the window presents)

    /// The single source of truth for "which book does the confirm window show".
    ///
    /// `selectedID` is the book the user explicitly picked in the queue
    /// («Подтвердить» on a row); nil = no pick. Rules, in order:
    ///   1. an explicit pick that is STILL ACTIVE wins — pressing «Подтвердить» on
    ///      the second book must open the SECOND book (never the first);
    ///   2. a pick that stopped being active (built / cancelled / vanished) is
    ///      IGNORED, so the window can never stick to a retired book;
    ///   3. no (or stale) pick → the first active book, which is the auto-surface
    ///      default the agent's rising-edge raise relies on.
    /// Pure function of the showcase — unit-checked by `app/selfcheck_routing.swift`.
    func presentedBook(selectedID: String?) -> BookSummary? {
        if let id = selectedID, let picked = activeBooks.first(where: { $0.bookID == id }) {
            return picked
        }
        return activeBooks.first
    }

    /// 1-based position of `bookID` among the active books — the confirm header's
    /// «N из M» must count the book actually ON SCREEN, not always the first one.
    /// nil when the book is not active (nothing sensible to show).
    func activePosition(of bookID: String) -> Int? {
        guard let idx = activeBooks.firstIndex(where: { $0.bookID == bookID }) else { return nil }
        return idx + 1
    }

    // Status partitions for the QUEUE screen (spec §7 sections), all in showcase
    // order. The agent owns these statuses; the queue just projects them.
    var convertingBooks: [BookSummary] { books.filter { $0.isConverting } }
    var doneBooks: [BookSummary] { books.filter { $0.isDone } }
    var errorBooks: [BookSummary] { books.filter { $0.isError } }
    /// Books the user took off the pipeline with «Пропустить» — the queue's
    /// ПРОПУЩЕНО section. They are shown (never silently vanished) so the answer to
    /// «куда делась книга?» is one screen away, with «Вернуть» right on the row.
    var skippedBooks: [BookSummary] { books.filter { $0.isSkipped } }

    /// The first converting book, if any — the Status hero's "Сейчас: <title>" line
    /// (spec §5). nil when nothing is mid-build (the hero shows an idle sub instead).
    var currentlyBuilding: BookSummary? { convertingBooks.first }

    /// "Последние собранные книги" (spec §5 block 4): finished books (done) and any
    /// that errored, in showcase order. The Status view sorts these by build time
    /// (from each manifest's `result.built_at`) and caps the list. A done OR error
    /// book is "recent history"; pending/converting are live, not history.
    var recentBooks: [BookSummary] { books.filter { $0.isDone || $0.isError } }

    /// The loose-mp3 group to prompt for, if any. Surface priority (spec §6 / brief):
    /// a pending grouping decision OUTRANKS the confirm window — the user must decide
    /// combine-vs-separate before any of the resulting books can be confirmed.
    var firstPendingGroup: PendingGroup? { pendingGroups.first }
}

// MARK: - queue/books/<book_id>.json — per-book manifest

/// One chapter row in a manifest. Duration is filled by M0.5's ffprobe pass and
/// carried as INTEGER MILLISECONDS under the key `duration_ms` (scan.py
/// `_build_chapters`). It is optional because an unreadable mp3 leaves it null
/// (surfaced as an em-dash, not hidden).
struct ChapterEntry: Codable, Identifiable, Equatable {
    let index: Int
    let file: String
    let name: String
    /// Milliseconds (manifest key `duration_ms`); nil when the file was unreadable.
    let durationMS: Int?

    var id: Int { index }

    /// Seconds for display formatting (`Duration.human`). nil ⇒ unknown.
    var durationSeconds: Double? {
        guard let ms = durationMS else { return nil }
        return Double(ms) / 1000.0
    }

    enum CodingKeys: String, CodingKey {
        case index, file, name
        case durationMS = "duration_ms"
    }

    init(index: Int, file: String, name: String, durationMS: Int? = nil) {
        self.index = index
        self.file = file
        self.name = name
        self.durationMS = durationMS
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        index = (try? c.decode(Int.self, forKey: .index)) ?? 0
        file  = (try? c.decode(String.self, forKey: .file)) ?? ""
        name  = (try? c.decode(String.self, forKey: .name)) ?? ""
        durationMS = try? c.decodeIfPresent(Int.self, forKey: .durationMS)
    }
}

/// The `error` block the agent writes onto a manifest when a build fails
/// (dispatcher.py: `manifest["error"] = {"reason", "detail"?, "at"}`). `reason` is
/// a short machine tag (e.g. `source_missing`, `no_usable_chapters`, `timeout`,
/// `interrupted`, `ffmpeg_*`); `detail` is an optional human-ish tail. The app maps
/// `reason` to a localized banner message (it never fabricates specifics the agent
/// did not provide — see ConfirmView.errorBanner).
struct BuildError: Codable, Equatable {
    let reason: String
    let detail: String?

    enum CodingKeys: String, CodingKey {
        case reason, detail
    }

    init(reason: String, detail: String?) {
        self.reason = reason
        self.detail = detail
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        reason = (try? c.decode(String.self, forKey: .reason)) ?? "unknown"
        detail = try? c.decodeIfPresent(String.self, forKey: .detail)
    }
}

/// One cover candidate in the manifest's `cover_options` (cover.py
/// `_option`): an ordered, pre-resolved list the agent built embedded → web →
/// generated (cover.py `resolve_cover_options`), always ≥1 (PRD G4). The picker in
/// the confirm window renders these as thumbnails; the selected one's `id` rides
/// back to the agent in the confirm-build command. `kind` is
/// `embedded`/`web`/`generated` (and, app-side only, `custom` for a user file the
/// agent will copy). `path` is an ABSOLUTE file path the agent wrote under covers/
/// (or, for `custom`, the user's original file the app puts in `cover_custom_path`).
struct CoverOption: Codable, Identifiable, Equatable {
    let optID: String
    let kind: String
    let path: String
    let label: String

    var id: String { optID }

    /// Human source line under the big preview, by `kind` (spec §4 badge text).
    var sourceLine: String {
        switch kind {
        case "embedded": return "Из файла"
        case "web": return "Из сети"
        case "generated": return "Сгенерирована"
        case "custom": return "Своя картинка"
        default: return label
        }
    }

    /// Short uppercase badge shown on the big preview (spec §4 cover-badge).
    var badgeText: String {
        switch kind {
        case "embedded": return "ИЗ ФАЙЛА"
        case "web": return "ИЗ СЕТИ"
        case "generated": return "СГЕНЕРИРОВАНО"
        case "custom": return "СВОЯ"
        default: return label.uppercased()
        }
    }

    enum CodingKeys: String, CodingKey {
        case optID = "id"
        case kind, path, label
    }

    init(optID: String, kind: String, path: String, label: String) {
        self.optID = optID
        self.kind = kind
        self.path = path
        self.label = label
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        optID = (try? c.decode(String.self, forKey: .optID)) ?? ""
        kind  = (try? c.decode(String.self, forKey: .kind)) ?? ""
        path  = (try? c.decode(String.self, forKey: .path)) ?? ""
        label = (try? c.decode(String.self, forKey: .label)) ?? ""
    }
}

/// The `result` block the agent stamps on a manifest once a build reaches `done`
/// (dispatcher.py: `manifest["result"] = {"output", "output_path", "built_at"}`).
/// `outputPath` is the ABSOLUTE path of the finished `.m4b` — the queue's "Открыть"
/// action reveals it in Finder. (`output` is the same path under the older key; we
/// read `output_path` and fall back to `output`.) Present only on `done`.
///
/// Decodable-only (the app never writes manifests): the `output` CodingKey is a
/// read alias with no stored property, which is incompatible with a synthesized
/// Encodable — and we don't need one.
struct BookResult: Decodable, Equatable {
    let outputPath: String?
    let builtAt: Double?

    enum CodingKeys: String, CodingKey {
        case outputPath = "output_path"
        case output
        case builtAt = "built_at"
    }

    init(outputPath: String?, builtAt: Double? = nil) {
        self.outputPath = outputPath
        self.builtAt = builtAt
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // Prefer output_path; fall back to the legacy `output` key (same value).
        let op = (try? c.decodeIfPresent(String.self, forKey: .outputPath)) ?? nil
        let o  = (try? c.decodeIfPresent(String.self, forKey: .output)) ?? nil
        outputPath = (op?.isEmpty == false ? op : nil) ?? (o?.isEmpty == false ? o : nil)
        builtAt = try? c.decodeIfPresent(Double.self, forKey: .builtAt)
    }
}

/// Build parameters for a book (decisions D2/D6 defaults: 192 · stereo · split off).
/// `samplerate` is OPTIONAL: `nil` is the "keep the source sample rate" sentinel
/// (matches agent scan.py DEFAULT_PARAMS["samplerate"] = None / build_m4b's
/// "as in source" fallback) — the agent resamples only when a concrete 44100 /
/// 48000 is set. Read-only here at M0.3; editing/echo back is M0.4+.
struct BookParams: Codable, Equatable {
    var bitrate: Int
    var channels: String
    /// Output sample rate in Hz, or `nil` = "as in source" (no resample). A
    /// concrete value (44100 / 48000) is the user's explicit override.
    var samplerate: Int?
    var split: Bool
    /// Part-size threshold in MEGABYTES (only meaningful when `split`). Matches
    /// agent/scan.py DEFAULT_PARAMS["split_threshold_mb"] (= 300) and the engine's
    /// `split_threshold_mb` param; the agent converts MB→bytes (split.py).
    var splitThresholdMB: Int
    /// Speed-up build mode (D15): `"fast"` (default) = parallel groups → concat
    /// stream-copy (×6–10, possible ~25 ms silence at chapter seams); `"seamless"`
    /// = single-pass bit-exact encode (slower). Matches agent/scan.py
    /// DEFAULT_PARAMS["build_mode"]; the confirm window's toggle overrides it and it
    /// rides to the engine in the confirm-build command params (P-PARAMS whitelist).
    var buildMode: String

    static let defaults = BookParams(
        bitrate: 192, channels: "stereo", samplerate: nil, split: false,
        splitThresholdMB: 300, buildMode: "fast")

    init(bitrate: Int, channels: String, samplerate: Int?, split: Bool,
         splitThresholdMB: Int = 300, buildMode: String = "fast") {
        self.bitrate = bitrate
        self.channels = channels
        self.samplerate = samplerate
        self.split = split
        self.splitThresholdMB = splitThresholdMB
        self.buildMode = buildMode
    }

    enum CodingKeys: String, CodingKey {
        case bitrate, channels, samplerate, split
        case splitThresholdMB = "split_threshold_mb"
        case buildMode = "build_mode"
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bitrate    = (try? c.decode(Int.self, forKey: .bitrate)) ?? 192
        channels   = (try? c.decode(String.self, forKey: .channels)) ?? "stereo"
        // `samplerate` may be a number, JSON null, or absent → all map to nil
        // ("as in source") except a concrete number. decodeIfPresent returns nil
        // for both an explicit null and a missing key, which is exactly right.
        samplerate = (try? c.decodeIfPresent(Int.self, forKey: .samplerate)) ?? nil
        split      = (try? c.decode(Bool.self, forKey: .split)) ?? false
        splitThresholdMB = (try? c.decode(Int.self, forKey: .splitThresholdMB)) ?? 300
        // Any value other than "seamless" (missing / null / older manifest) means
        // the default fast path — mirrors agent build_m4b._build_mode's resolution.
        let bm = (try? c.decodeIfPresent(String.self, forKey: .buildMode)) ?? nil
        buildMode = (bm == "seamless") ? "seamless" : "fast"
    }
}

// MARK: - Manifest phase (D17 — «ранний нудж»)

/// How far the agent has read a book, as a value.
///
/// The agent publishes the book TWICE per arrival (agent/scan.py): a `skeleton` at
/// ~0.8 s — everything the `stat` walk already paid for (chapter names from FILE
/// NAMES, author/title from the folder, file count, size) — and then the finished
/// manifest after ffprobe + the cover chain. The window therefore opens on a BOOK,
/// not on a spinner, and the phase is what lets it say so honestly.
///
/// `chapters` is the middle write: real ID3 names and durations are in, the cover
/// is not. `ready` is the final atomic write — and the ONLY one that carries a
/// `build_token`.
///
/// UNKNOWN / ABSENT → `.done`. Absent is the pre-D17 compat rule (a manifest
/// written by yesterday's agent is complete by definition — invariant I8), and an
/// unrecognized value is folded into the same bucket ON PURPOSE: the phase drives
/// nothing but WORDING here, while everything that matters (may we build? are we
/// still waiting?) is decided by `build_token`. So a newer agent inventing a phase
/// this build has never heard of degrades to «no extra explanation», never to a
/// wrong gate and never to a note that waits forever.
enum ManifestPhase: String, Equatable {
    case skeleton
    case chapters
    case ready
    case done

    init(raw: String?) {
        self = ManifestPhase(rawValue: raw ?? "") ?? .done
    }
}

/// WHAT THE HUMAN IS TOLD while the rest of the book is still arriving.
///
/// Derived from ONE fact — the presence of `build_token` — plus the phase for
/// wording only. That is deliberate: the note and the «Собрать» button then read
/// the same bit, so the window can never say «готово» over a dead button, nor
/// «ещё читаю» over a live one.
///
/// `.ready` also covers the offline case, and that is the honest answer rather
/// than a gap: web covers left the critical path entirely (D17 §1), so a book
/// reaches `ready` with no network at all. When the note disappears, there is
/// nothing left to wait for — full stop.
enum ConfirmPreparation: Equatable {
    /// `build_token` present: the manifest is complete, nothing is pending.
    case ready
    /// Skeleton: names come from file names, durations are unknown.
    case readingTags
    /// Chapters are real; the cover chain has not landed yet.
    case resolvingCover
    /// Not ready, phase unrecognized (a newer agent) — say the true, generic thing.
    case preparing

    static func forManifest(_ m: BookManifest) -> ConfirmPreparation {
        guard !m.isBuildReady else { return .ready }
        switch m.phaseValue {
        case .skeleton: return .readingTags
        case .chapters: return .resolvingCover
        case .ready, .done: return .preparing
        }
    }

    /// The one-line note under the header, or nil when there is nothing to say.
    /// Wording rule: name what is happening and that it is SHORT — never «ошибка»,
    /// never a spinner with no noun. The book is already on screen; this only
    /// explains why two fields are still moving.
    var note: String? {
        switch self {
        case .ready:          return nil
        case .readingTags:    return "Читаю теги — названия глав и длительность появятся через пару секунд"
        case .resolvingCover: return "Подбираю обложку — ещё пара секунд"
        case .preparing:      return "Дочитываю книгу — ещё пара секунд"
        }
    }

    /// Why «Собрать» is inert right now (hover text — a dimmed control must always
    /// be able to explain itself; lesson 005).
    var buildHint: String? {
        guard self != .ready else { return nil }
        return "Книга ещё читается. «Собрать» включится, когда агент дочитает её — обычно пара секунд."
    }
}

/// The full per-book manifest. The app reads it to render the confirm window; the
/// agent owns/writes it. Extra keys (`ts`, `processed_keys`) are ignored.
///
/// `title`/`author` are resolved by M0.5 (ID3 tags → folder fallback). `coverState`
/// is `embedded`/`none`; when `embedded`, `coverPreview` is an ABSOLUTE path to the
/// jpg the agent extracted from the first mp3 (scan.py `_resolve_cover`).
///
/// Decodable-only: the agent is the single writer of manifests; the app never
/// encodes one (and `result`'s read-alias key makes a synthesized Encodable
/// impossible anyway). Decoding stays fully custom below.
struct BookManifest: Decodable, Equatable {
    let bookID: String
    let srcDir: String
    let status: String
    let sourceRev: String
    let confirmToken: String
    /// How far the agent has read this book (D17): `skeleton` → `chapters` →
    /// `ready` → `done`. ABSENT = `done` — pre-D17 manifests are complete by
    /// definition, so there is no migration. See `ManifestPhase`.
    let phase: String
    /// The agent's proof that a COMPLETE manifest existed when this was read
    /// (D17). It is created ONLY in the final atomic write (`agent/scan.py
    /// _finish_manifest`) and does not exist at all in the earlier phases — see
    /// `isBuildReady` for why the gate reads THIS and never the phase.
    let buildToken: String
    let title: String
    let author: String
    let chapters: [ChapterEntry]
    /// Sum of readable chapter durations in ms (manifest `total_duration_ms`).
    let totalDurationMS: Int
    /// Source sample rate (Hz) the agent recorded — the MAX across the source mp3s
    /// (scan.py `source_samplerate`). nil on an older manifest / when unprobed. The
    /// confirm window shows it as the "Как в источнике" hint; `params.samplerate ==
    /// nil` means the build keeps exactly this rate (no resample).
    let sourceSamplerate: Int?
    let coverState: String
    /// Absolute path to the extracted embedded cover jpg (only when `embedded`).
    let coverPreview: String?
    /// Ordered cover candidates (cover.py `cover_options`): embedded → web →
    /// generated, always ≥1 (PRD G4). Empty array on an older/half-written manifest
    /// → the picker degrades to the embedded preview / placeholder.
    let coverOptions: [CoverOption]
    /// Id of the default-selected option (cover.py `cover_selected`): embedded when
    /// present, else the first generated. The picker seeds its selection from this.
    let coverSelected: String?
    /// Is the WEB cover search still running (M-B: `cover_web` = `pending`/`done`)?
    /// Web enrichment happens AFTER the drain, off the critical path, so a book can
    /// sit at `ready` — buildable, «Собрать» lit — while more cover tiles are still
    /// on their way. See `isCoverWebPending`.
    let coverWeb: String
    let params: BookParams
    /// Coarse build progress the agent records (dispatcher.py): 0.0 at start, 1.0
    /// at done. NOTE: the agent does NOT emit per-chapter progress — a build is one
    /// ffmpeg call — so this is binary in practice. The converting UI treats it as
    /// indeterminate rather than inventing a fake "chapter N of M" / percentage.
    let progress: Double
    /// Present only when `status == error` (dispatcher build_failed / interrupted).
    let error: BuildError?
    /// Present only when `status == done` — the finished `.m4b` path (+ built_at).
    /// The queue's "Открыть" reveals `result.output_path` in Finder.
    let result: BookResult?

    enum CodingKeys: String, CodingKey {
        case bookID = "book_id"
        case srcDir = "src_dir"
        case status
        case sourceRev = "source_rev"
        case confirmToken = "confirm_token"
        case phase
        case buildToken = "build_token"
        case title
        case author
        case chapters
        case totalDurationMS = "total_duration_ms"
        case sourceSamplerate = "source_samplerate"
        case coverState = "cover_state"
        case coverPreview = "cover_preview"
        case coverOptions = "cover_options"
        case coverSelected = "cover_selected"
        case coverWeb = "cover_web"
        case params
        case progress
        case error
        case result
    }

    var isPendingConfirm: Bool { status == "pending-confirm" }
    var isConverting: Bool { status == "converting" }
    var isError: Bool { status == "error" }
    /// Taken off the pipeline by «Пропустить» — see `BookSummary.isSkipped`.
    var isSkipped: Bool { status == "skipped" }
    /// Books the confirm window should present: awaiting confirm OR mid-build OR
    /// failed (the window mirrors the live build, not just the pending step).
    var isActive: Bool { isPendingConfirm || isConverting || isError }

    /// Total length in seconds for the header/estimate. Prefers the manifest's
    /// precomputed sum; falls back to summing chapter durations if it is 0/absent.
    var totalSeconds: Double {
        if totalDurationMS > 0 { return Double(totalDurationMS) / 1000.0 }
        return chapters.reduce(0.0) { $0 + ($1.durationSeconds ?? 0) }
    }

    /// True once at least one chapter carries a probed duration (M0.5 ran).
    var hasDurations: Bool {
        totalDurationMS > 0 || chapters.contains { $0.durationMS != nil }
    }

    /// The phase as a value (absent / unrecognized → `.done`, see `ManifestPhase`).
    var phaseValue: ManifestPhase { ManifestPhase(raw: phase) }

    /// MAY THIS BOOK BE BUILT — and the ONLY question «Собрать» is allowed to ask.
    ///
    /// It reads the PRESENCE of `build_token`, deliberately NOT `phase == ready`,
    /// and the difference is the whole point (D17, Архитектор #2). A phase check
    /// asks «is the book complete RIGHT NOW»; the build gate must ask «did the
    /// SENDER ever see a complete book». Those come apart exactly once, and it is
    /// the case that loses a build: the app writes a command off a SKELETON → the
    /// command waits in queue/commands/ until the drain → meanwhile the agent
    /// finishes the same revision and writes the full manifest with the SAME
    /// source_rev and confirm_token → a validator that looks at the phase now sees
    /// `ready` and ACCEPTS a command that was born on an empty chapter list.
    ///
    /// A skeleton has no `build_token` physically, so a command minted from one
    /// carries none (or an empty one) and stays invalid FOREVER — including after
    /// finalization. The app therefore echoes this token verbatim in
    /// `confirm-build` (EngineClient); the agent refuses anything else
    /// (`manifest_not_ready` / `build_token_mismatch`).
    ///
    /// Monotone within a revision by construction: the agent mints the token once,
    /// in the final atomic write, and every later write of the SAME `source_rev`
    /// (resume, params/cover patch, re-scan short-circuit) carries it forward. So
    /// «Собрать» makes exactly ONE disabled → enabled transition per publication —
    /// it cannot blink. A NEW `source_rev` is a different book revision: it starts
    /// at a skeleton again and the button honestly goes back to waiting.
    var isBuildReady: Bool { !buildToken.isEmpty }

    /// The web cover search has not finished (M-B `cover_web == "pending"`).
    ///
    /// ⚠️ THIS MUST NEVER REACH A BUILD DECISION. `isBuildReady` above does not
    /// look at it, and neither does `ConfirmPreparation` — both read `build_token`
    /// only. Taking web enrichment OFF the build's critical path is the whole point
    /// of D17 (measured: live network vs. dead network = 0.022 s difference at the
    /// gate); wiring this flag into the gate would hand that back. It is allowed to
    /// drive exactly ONE thing: a line of text saying tiles may still appear.
    ///
    /// ABSENT ⇒ done. An older manifest never ran a staged web pass, and a book
    /// that is not going to look for anything must not claim it is looking.
    var isCoverWebPending: Bool { coverWeb == "pending" }

    init(bookID: String, srcDir: String, status: String, sourceRev: String,
         confirmToken: String, title: String, author: String,
         chapters: [ChapterEntry], totalDurationMS: Int,
         sourceSamplerate: Int? = nil, coverState: String,
         coverPreview: String?, coverOptions: [CoverOption] = [],
         coverSelected: String? = nil, params: BookParams,
         progress: Double = 0, error: BuildError? = nil,
         result: BookResult? = nil,
         phase: String = ManifestPhase.done.rawValue,
         buildToken: String = "",
         coverWeb: String = "done") {
        self.bookID = bookID
        self.srcDir = srcDir
        self.status = status
        self.sourceRev = sourceRev
        self.confirmToken = confirmToken
        self.phase = phase
        self.buildToken = buildToken
        self.title = title
        self.author = author
        self.chapters = chapters
        self.totalDurationMS = totalDurationMS
        self.sourceSamplerate = sourceSamplerate
        self.coverState = coverState
        self.coverPreview = coverPreview
        self.coverOptions = coverOptions
        self.coverSelected = coverSelected
        self.coverWeb = coverWeb
        self.params = params
        self.progress = progress
        self.error = error
        self.result = result
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        bookID       = (try? c.decode(String.self, forKey: .bookID)) ?? ""
        srcDir       = (try? c.decode(String.self, forKey: .srcDir)) ?? ""
        status       = (try? c.decode(String.self, forKey: .status)) ?? ""
        sourceRev    = (try? c.decode(String.self, forKey: .sourceRev)) ?? ""
        confirmToken = (try? c.decode(String.self, forKey: .confirmToken)) ?? ""
        // Absent / null / empty `phase` ⇒ `done` — a pre-D17 manifest was written
        // complete or not at all, so «no phase» is the same fact as «finished».
        // That one default is the entire backward-compat story (invariant I8).
        let rawPhase = (try? c.decodeIfPresent(String.self, forKey: .phase)) ?? nil
        phase        = (rawPhase?.isEmpty == false) ? rawPhase! : ManifestPhase.done.rawValue
        // Absent ⇒ "" ⇒ NOT buildable. Fail-closed on purpose: a manifest we could
        // not read the token from must never light «Собрать» up.
        buildToken   = ((try? c.decodeIfPresent(String.self, forKey: .buildToken)) ?? nil) ?? ""
        title        = (try? c.decode(String.self, forKey: .title)) ?? ""
        author       = (try? c.decode(String.self, forKey: .author)) ?? ""
        chapters     = (try? c.decode([ChapterEntry].self, forKey: .chapters)) ?? []
        totalDurationMS = (try? c.decodeIfPresent(Int.self, forKey: .totalDurationMS)) ?? 0
        sourceSamplerate = (try? c.decodeIfPresent(Int.self, forKey: .sourceSamplerate)) ?? nil
        coverState   = (try? c.decode(String.self, forKey: .coverState)) ?? "unknown"
        coverPreview = try? c.decodeIfPresent(String.self, forKey: .coverPreview)
        // Drop any option that decoded with an empty id (half-written / malformed),
        // mirroring the showcase's empty-id filter. Empty array on an older manifest.
        let rawOptions = (try? c.decode([CoverOption].self, forKey: .coverOptions)) ?? []
        coverOptions = rawOptions.filter { !$0.optID.isEmpty }
        coverSelected = try? c.decodeIfPresent(String.self, forKey: .coverSelected)
        // Absent / null / anything unrecognized ⇒ "done": only an explicit
        // `pending` makes the window claim a search is running.
        coverWeb = ((try? c.decodeIfPresent(String.self, forKey: .coverWeb)) ?? nil) ?? "done"
        params       = (try? c.decode(BookParams.self, forKey: .params)) ?? .defaults
        progress     = (try? c.decodeIfPresent(Double.self, forKey: .progress)) ?? 0
        error        = try? c.decodeIfPresent(BuildError.self, forKey: .error)
        result       = try? c.decodeIfPresent(BookResult.self, forKey: .result)
    }
}

// MARK: - Confirm-window draft (the human's fields) + the MERGE rule

/// The confirm footer's "Применить параметры ко всем (N)" (US-3.7 / spec §3):
/// the build params the user approved once, plus the ids of the books that were
/// awaiting confirmation at that moment. Purely APP-SIDE — the protocol has no
/// `apply-to-all` command, so this only PRE-FILLS the confirm window of those
/// books; each one still rides to the agent in its own `confirm-build` after a
/// human "ок" (invariant I2). The COVER is deliberately not part of the preset —
/// it stays per-book (US-3.7 AC: «обложку всё равно подтверждаю по каждой»).
///
/// Lives in the model layer (not next to the view) so `ConfirmMerge` — which
/// consumes it — stays a pure, unit-checkable value rule.
struct ParamsPreset: Equatable {
    let params: BookParams
    /// Snapshot of the `pending-confirm` ids at the moment of the click. A book
    /// recognized LATER was not "ожидающей" then, so it keeps its own defaults
    /// instead of silently inheriting a stale preset.
    let bookIDs: Set<String>

    func applies(to bookID: String) -> Bool { bookIDs.contains(bookID) }
}

/// Which fields the HUMAN has touched in this window. Everything not in the set is
/// «pristine» = still the agent's value, and therefore still free to be updated
/// from a newer manifest.
struct TouchedFields: OptionSet, Equatable {
    let rawValue: Int
    init(rawValue: Int) { self.rawValue = rawValue }

    static let title  = TouchedFields(rawValue: 1 << 0)
    static let author = TouchedFields(rawValue: 1 << 1)
    /// One flag for the whole build-params group (bitrate / channels / samplerate /
    /// split / threshold / mode): they are seeded from ONE source (preset или
    /// manifest) and the human edits them as one decision.
    static let params = TouchedFields(rawValue: 1 << 2)
    static let cover  = TouchedFields(rawValue: 1 << 3)
}

/// Everything the confirm window OWNS about a book: the editable fields, which of
/// them the human has touched, and the manifest revision they were seeded from.
///
/// One `@State` of this type replaces the eight separate ones the window used to
/// carry. That is not tidiness — it is the fix. SwiftUI keeps `@State` alive across
/// a body update, so with the D17 two-phase publication the SAME view instance is
/// handed a NEWER manifest for the same `book_id`, and there is no mechanism that
/// re-seeds the old fields: the skeleton's file-name title would survive the real
/// ID3 title forever (both architects flagged this independently as the app's main
/// hazard). Merging in one place, by one rule, is what makes that impossible.
struct ConfirmDraft: Equatable {
    /// The book this draft belongs to. A DIFFERENT book is a different
    /// presentation, not an update — that is the only full reset (see `merge`).
    let bookID: String
    /// The revision the pristine fields were last seeded from. It MOVES with the
    /// manifest (the same book with new files is still the same book, and the
    /// human's typing survives it) — it is carried so the draft can always say
    /// which revision its untouched values came from.
    var sourceRev: String

    var title: String
    var author: String
    var params: BookParams
    /// Id of the selected cover option (manifest option, or a client-only `custom`
    /// one the user picked with «Заменить»). nil = nothing selectable yet.
    var coverSelectedID: String?

    var touched: TouchedFields
}

/// The merge rule: OLD DRAFT × NEW MANIFEST → NEW DRAFT.
///
/// A pure function of its arguments — no `@State`, no view, no file access — so it
/// can be exhaustively unit-checked (the whole pristine/dirty matrix) rather than
/// eyeballed in a running window. This is the only place in the app allowed to
/// decide whose value wins; the view assigns its result wholesale
/// (`draft = ConfirmMerge.merge(...)`) and never patches fields one by one.
///
/// THE MATRIX (per field):
///
/// | поле      | pristine (человек не трогал)                        | dirty (трогал)                       |
/// |-----------|-----------------------------------------------------|--------------------------------------|
/// | title     | manifest.title, пусто → `fallbackTitle` (строка очереди) | правка человека                  |
/// | author    | manifest.author                                     | правка человека                      |
/// | params    | preset (если покрывает книгу) иначе manifest.params, порог зажат в 250…700 | правка человека |
/// | cover     | manifest.coverSelected ?? первый вариант             | выбор человека, ЕСЛИ id ещё существует |
///
/// Three rules that are not in the table because they are about identity, not
/// fields:
///
///  1. **Только смена `book_id` выбрасывает черновик целиком** (`seed`). A
///     different book is a different presentation; the same book is the same book.
///
///     A changed `source_rev` — the human added a file to the folder while the
///     window was open — deliberately does NOT reset anything: it runs through the
///     ordinary matrix, so the title he is halfway through typing survives and only
///     the untouched fields are re-seeded from the new manifest (решение человека,
///     2026-07-28). Text a person typed must not disappear because of a background
///     event; «формально это другая ревизия» does not buy back the vanished input,
///     and if the title really no longer fits, he can see that and fix it himself.
///     `sourceRev` is carried forward so the draft always records which revision
///     its pristine values came from.
///  2. **Выбор обложки не имеет права зависнуть.** A touched cover id survives —
///     unless it no longer exists among the manifest's options plus the client-only
///     ones. That happens for real: the skeleton has NO options, so anything picked
///     before `ready`… cannot exist; and a NEW REVISION regenerates the whole
///     option list (which is exactly why rule 1 cannot simply keep everything).
///     A dangling selection would silently send a `cover_id` the agent cannot
///     resolve, so the pick falls back to the agent's default AND the `.cover` flag
///     is CLEARED — otherwise the field would stay frozen at a value nobody chose.
///  3. **То же для параметров: значение вне диапазона не зависает.** The threshold
///     is clamped into `thresholdRangeMB` on EVERY merge, not only at seeding — a
///     touched value can now outlive the manifest that produced it, so «человек
///     тронул» must not become a way to preserve a number the slider cannot show
///     and the human never chose. (The preset itself cannot go stale here: it is
///     keyed on `book_id`, which by rule 1 has not changed.)
enum ConfirmMerge {

    /// Slider bounds for `split_threshold_mb` (spec §6 / D6 default 300). Seeding
    /// clamps into them so the draft always holds a value the control can render —
    /// and, because the draft is what gets sent, a value the agent can act on.
    static let thresholdRangeMB: ClosedRange<Int> = 250...700

    /// Build params as they should look for a book nobody has edited yet: the
    /// session preset when it covers THIS book (US-3.7), otherwise the manifest's
    /// own params.
    static func seedParams(manifest: BookManifest, preset: ParamsPreset?) -> BookParams {
        var p = preset.flatMap { $0.applies(to: manifest.bookID) ? $0.params : nil }
            ?? manifest.params
        p.splitThresholdMB = min(thresholdRangeMB.upperBound,
                                 max(thresholdRangeMB.lowerBound, p.splitThresholdMB))
        return p
    }

    /// The cover the agent would pick: its own default, else the first option.
    /// nil while the book has no options at all (the skeleton phase).
    static func seedCoverID(manifest: BookManifest) -> String? {
        manifest.coverSelected ?? manifest.coverOptions.first?.optID
    }

    /// A brand-new draft for `manifest` — nothing touched, every field the agent's.
    ///
    /// `fallbackTitle` is the showcase row's title, used only when the manifest has
    /// none, so the field is never blank-by-bug.
    static func seed(manifest: BookManifest,
                     fallbackTitle: String = "",
                     preset: ParamsPreset? = nil) -> ConfirmDraft {
        ConfirmDraft(
            bookID: manifest.bookID,
            sourceRev: manifest.sourceRev,
            title: manifest.title.isEmpty ? fallbackTitle : manifest.title,
            author: manifest.author,
            params: seedParams(manifest: manifest, preset: preset),
            coverSelectedID: seedCoverID(manifest: manifest),
            touched: [])
    }

    /// Fold `manifest` into `draft` per the matrix above.
    ///
    /// - Parameters:
    ///   - draft: what the window holds right now.
    ///   - manifest: the manifest as it is on disk NOW (possibly a later phase).
    ///   - fallbackTitle: showcase title, used only when the manifest has none.
    ///   - preset: the «ко всем» session preset, if any.
    ///   - extraCoverIDs: ids of client-only options (a «Заменить» file) — they are
    ///     not in the manifest, so without them a custom pick would read as dangling.
    static func merge(_ draft: ConfirmDraft,
                      with manifest: BookManifest,
                      fallbackTitle: String = "",
                      preset: ParamsPreset? = nil,
                      extraCoverIDs: Set<String> = []) -> ConfirmDraft {
        // Identity rule 1: only a DIFFERENT BOOK is a new presentation. A new
        // revision of the same book goes through the ordinary matrix below.
        guard draft.bookID == manifest.bookID else {
            return seed(manifest: manifest, fallbackTitle: fallbackTitle, preset: preset)
        }

        var out = draft
        // The pristine fields now come from THIS revision — record it.
        out.sourceRev = manifest.sourceRev
        if !draft.touched.contains(.title) {
            out.title = manifest.title.isEmpty ? fallbackTitle : manifest.title
        }
        if !draft.touched.contains(.author) {
            out.author = manifest.author
        }
        if !draft.touched.contains(.params) {
            out.params = seedParams(manifest: manifest, preset: preset)
        }
        // Identity rule 3: clamp on EVERY merge, touched or not.
        out.params.splitThresholdMB = min(thresholdRangeMB.upperBound,
                                          max(thresholdRangeMB.lowerBound,
                                              out.params.splitThresholdMB))

        // Identity rule 2: the cover pick must never dangle.
        let known = Set(manifest.coverOptions.map { $0.optID }).union(extraCoverIDs)
        if draft.touched.contains(.cover),
           let picked = draft.coverSelectedID, known.contains(picked) {
            out.coverSelectedID = picked          // человек выбрал, вариант жив
        } else {
            out.coverSelectedID = seedCoverID(manifest: manifest)
            out.touched.remove(.cover)            // выбор испарился — поле снова живое
        }
        return out
    }
}

// MARK: - Window-raise edges (I2 — ОДИН подъём окна на публикацию)

/// The identity of a «publication» — the thing that is allowed to raise the window
/// exactly once.
///
/// These strings are a DELIBERATE, byte-for-byte mirror of the agent's own ledger
/// keys (`agent/scan.py` `_book_edge_key` / `_group_edge_key` /`_edge_keys`, file
/// `state/notified.json`). That is the entire mechanism: the app and the agent
/// decide «is this new?» by computing the SAME function over the SAME files, so
/// the two raise channels — the agent's `open -b` nudge and the app's own
/// rising-edge watcher — cannot disagree about what counts as one appearance.
///
/// Why identity and not just `book_id` (the old baseline): D17 publishes a book
/// TWICE (skeleton, then ready), and a book can also re-enter `pending-confirm`
/// without being new (a cancelled build lands back there with its token intact).
/// `book_id` alone cannot tell those apart from a real arrival — it happened to
/// agree with the agent so far because both writes project the same row, which is
/// luck, not structure. `confirm_token` is minted ONCE per publication, on the
/// skeleton, and carried through every later phase of that revision, so keying on
/// it makes «one raise per publication» hold BY CONSTRUCTION: the phase flip is
/// literally the same key, and a reconvert (fresh token) is legitimately a new one.
enum NudgeEdge {

    /// `book:<book_id>:<source_rev[:16]>:<confirm_token[:16]>` — mirrors
    /// `agent/scan.py::_book_edge_key` exactly, including the 16-char truncation.
    static func bookKey(bookID: String, sourceRev: String, confirmToken: String) -> String {
        "book:\(bookID):\(sourceRev.prefix(16)):\(confirmToken.prefix(16))"
    }

    /// `group:<group_id>:<rev[:16]>:<token[:16]>` — mirrors `_group_edge_key`.
    /// The showcase carries rev+token for a group, so no manifest read is needed.
    static func groupKey(groupID: String, rev: String, token: String) -> String {
        "group:\(groupID):\(rev.prefix(16)):\(token.prefix(16))"
    }

    static func key(for group: PendingGroup) -> String {
        groupKey(groupID: group.groupID, rev: group.rev, token: group.token)
    }

    /// Every edge currently on screen, exactly as the agent would enumerate them:
    /// one per `pending-confirm` showcase row (rev/token read from its manifest —
    /// they are not in state.json, same as agent-side) plus one per pending group.
    ///
    /// A book whose manifest cannot be read degrades to empty rev/token segments
    /// rather than vanishing: it still functions as an appearance edge for that
    /// book, which is what the agent does too — and losing the edge entirely would
    /// mean raising the window again on the next refresh, forever.
    static func keys(state: ShowcaseState,
                     manifest: (String) -> BookManifest?) -> Set<String> {
        var keys = Set<String>()
        for row in state.pendingConfirm where !row.bookID.isEmpty {
            let m = manifest(row.bookID)
            keys.insert(bookKey(bookID: row.bookID,
                                sourceRev: m?.sourceRev ?? "",
                                confirmToken: m?.confirmToken ?? ""))
        }
        for g in state.pendingGroups where !g.groupID.isEmpty {
            keys.insert(key(for: g))
        }
        return keys
    }

    /// Does `keys` contain a BOOK edge (as opposed to a grouping one)? The window
    /// changes screen only for a book; a grouping prompt overlays whatever is shown.
    static func containsBook(_ keys: Set<String>) -> Bool {
        keys.contains { $0.hasPrefix("book:") }
    }

    static func containsGroup(_ keys: Set<String>) -> Bool {
        keys.contains { $0.hasPrefix("group:") }
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
    ///
    /// "Сбросить статистику" is applied HERE (fb2 parity): the app-owned baselines
    /// are subtracted from `totals` so the "СОБРАНО"/"ЗА СЕГОДНЯ" cards read from
    /// zero after a reset while new builds count again. state.json itself is NEVER
    /// rewritten (the agent owns it — D13); the markers live under `state/` and are
    /// applied on read. Recent-list hiding ("Очистить") needs per-book manifests and
    /// is applied in the Status layer (see `recentClearedAt()`).
    func loadState() -> ShowcaseState {
        statusMarkers.applyingBaselines(to: loadRawState())
    }

    /// The RAW showcase exactly as the agent wrote it — NO marker baselining. Used
    /// by `resetStats()` to capture the current lifetime counters as the reset
    /// baseline (baselining the already-baselined value would double-subtract).
    func loadRawState() -> ShowcaseState {
        guard let data = try? Data(contentsOf: stateFile),
              let state = try? JSONDecoder().decode(ShowcaseState.self, from: data)
        else { return .empty }
        return state
    }

    /// The RAW `totals` (built/today) straight from the agent snapshot, before any
    /// reset baseline is applied. `resetStats()` stamps these as the new baseline.
    func loadRawTotals() -> ShowcaseTotals { loadRawState().totals }

    /// Load + decode one book manifest. `nil` on any failure (absent / half-written).
    func loadManifest(bookID: String) -> BookManifest? {
        guard let data = try? Data(contentsOf: manifestURL(bookID: bookID)),
              let m = try? JSONDecoder().decode(BookManifest.self, from: data)
        else { return nil }
        return m
    }
}

// MARK: - install-receipt.json — the installer's proof-of-install (plan v2 B3)

/// `<support>/install-receipt.json`, written by `packaging/installer.sh` as the
/// VERY LAST step of a successful install (after `launchctl print` confirmed the
/// loaded ProgramArguments[0]). Its existence is therefore the only honest signal
/// that an install went all the way through; a half-finished one leaves no receipt.
///
/// It lives in the App Support ROOT — deliberately NOT under `state/` — so writing
/// it never wakes the app's state-directory watcher.
///
/// Parsed with JSONSerialization rather than Codable on purpose: the file is
/// produced by `plutil -convert json`, and a single unexpected/renamed key must
/// degrade one field, never the whole receipt (the receipt is what the fail-closed
/// gate leans on — losing it wholesale would be the worst possible failure mode).
struct InstallReceipt: Equatable {
    let schema: Int
    /// The install generation UUID. The agent echoes it back through state.json
    /// only when launchd handed it over — equality of the two is the proof.
    let generation: String
    /// The app version this install shipped (receipt's `engine_version`), used by
    /// the `bundled >= installed` rule (M11f) so a downgrade is never mistaken for
    /// an update.
    let engineVersion: String
    let installedAt: String
    /// "full" | "repair" — which installer mode wrote this receipt.
    let mode: String
    let watchDir: String
    let helperPath: String
    let plistPath: String
    let supportDir: String

    /// nil when the file is absent / unreadable / not a JSON object, or when it
    /// carries no `generation` (a receipt without a generation proves nothing, so
    /// it is treated as no receipt at all — fail-closed).
    init?(data: Data) {
        guard let obj = try? JSONSerialization.jsonObject(with: data),
              let d = obj as? [String: Any] else { return nil }
        let gen = (d["generation"] as? String) ?? ""
        guard !gen.isEmpty else { return nil }
        schema = (d["schema"] as? Int) ?? 0
        generation = gen
        engineVersion = (d["engine_version"] as? String) ?? ""
        installedAt = (d["installed_at"] as? String) ?? ""
        mode = (d["mode"] as? String) ?? ""
        watchDir = (d["watch_dir"] as? String) ?? ""
        helperPath = (d["helper_path"] as? String) ?? ""
        plistPath = (d["plist"] as? String) ?? ""
        supportDir = (d["support_dir"] as? String) ?? ""
    }
}

/// `bundled >= installed` (M11f), as a pure value rule.
///
/// Lives here rather than next to `AgentUpdate` (SetupView.swift) so the Swift
/// self-check can drive it without dragging the view layer in — and because it is
/// a comparison of two strings, not a UI concern.
///
/// Mirrors `installer.sh::ver_ge`: dotted components compared as integers, each
/// component truncated at its first non-digit ("1.0-beta" → 1.0), missing
/// components = 0. Kept identical on purpose — a Swift side that judged downgrades
/// differently from the installer would either block updates the installer accepts
/// or wave through ones it refuses.
enum EngineVersion {
    static func atLeast(_ a: String, _ b: String) -> Bool {
        func parts(_ s: String) -> [Int] {
            s.split(separator: ".").map { comp in
                Int(String(comp.prefix { $0.isNumber })) ?? 0
            }
        }
        let x = parts(a), y = parts(b)
        for i in 0..<max(x.count, y.count) {
            let l = i < x.count ? x[i] : 0
            let r = i < y.count ? y[i] : 0
            if l != r { return l > r }
        }
        return true
    }
}

// MARK: - LaunchAgent plist reads (the DISK truth about ProgramArguments[0])

/// Reads facts out of a LaunchAgent plist. Two of them matter:
///
///   · `programArgument0` — WHICH executable the plist points launchd at. On macOS
///     26 the TCC subject of the job is the Mach-O image of exactly this path, so
///     it is the single fact that decides whether a folder-access grant can even
///     exist. Read through `plutil -extract … raw` with **no fallback**: if we
///     cannot prove what PA0 is, we must report "unknown" and let the caller fail
///     closed. A fallback that guesses the expected path would turn "I don't know"
///     into "it's fine", which is the precise lie this whole milestone exists to
///     prevent.
///   · `environmentValue` — the plist's `EnvironmentVariables.<key>` (watch dir,
///     install generation). Read in-process (PropertyListSerialization) because
///     nothing fails closed on it — it is a fallback source, not a proof.
enum LaunchAgentPlist {
    /// `ProgramArguments[0]` exactly as the plist carries it, or nil when the file
    /// is absent / unreadable / has no ProgramArguments / plutil is unavailable.
    /// NEVER substitutes a default.
    static func programArgument0(plistPath: String) -> String? {
        guard !plistPath.isEmpty,
              FileManager.default.fileExists(atPath: plistPath) else { return nil }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/plutil")
        p.arguments = ["-extract", "ProgramArguments.0", "raw", "-o", "-", plistPath]
        let out = Pipe()
        p.standardOutput = out
        p.standardError = Pipe()
        do { try p.run() } catch { return nil }
        let data = out.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        guard p.terminationStatus == 0 else { return nil }
        let value = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return value.isEmpty ? nil : value
    }

    /// `EnvironmentVariables.<key>` from the plist, or nil when absent/empty.
    static func environmentValue(_ key: String, plistPath: String) -> String? {
        guard !plistPath.isEmpty,
              let data = try? Data(contentsOf: URL(fileURLWithPath: plistPath)),
              let obj = try? PropertyListSerialization.propertyList(
                  from: data, options: [], format: nil),
              let dict = obj as? [String: Any],
              let env = dict["EnvironmentVariables"] as? [String: Any],
              let value = env[key] as? String,
              !value.isEmpty
        else { return nil }
        return value
    }

    /// Path equality for the helper check: trailing-slash / `~` / `.` noise removed
    /// on BOTH sides. Deliberately NOT `resolvingSymlinksInPath` — a symlinked
    /// helper path is a DIFFERENT file to TCC (installer guard `nosymlink`), so
    /// resolving one here would paper over exactly the case that breaks the grant.
    static func samePath(_ a: String?, _ b: String?) -> Bool {
        guard let a = a, let b = b, !a.isEmpty, !b.isEmpty else { return false }
        return (a as NSString).standardizingPath == (b as NSString).standardizingPath
    }
}

// MARK: - Which surface owns the window (the fail-closed router, plan v2 §6.3)

/// Why the app says "агент не запустился" instead of offering access help.
enum AgentStallReason: String, Equatable {
    /// The plist on disk points launchd at something that is not our frozen helper
    /// (a v0.9 install, or a repair that never happened).
    case pa0Mismatch = "pa0"
    /// PA0 is right, but no install receipt exists — nothing proves the install
    /// finished, so nothing may be claimed about access.
    case receiptMissing = "receipt"
    /// The receipt exists; the agent has published no generation at all (it has
    /// not run under this install yet, past the grace window).
    case generationMissing = "generation-missing"
    /// The agent is running a DIFFERENT install than the one on disk — the classic
    /// "installer died between publish and bootstrap" window.
    case generationMismatch = "generation-mismatch"
}

/// The single destination router. Priority is fixed by plan v2 §6.3:
/// `agentRepair > agentNotRunning > folderAccess > normal`.
enum StatusSurface: Equatable {
    /// An install/repair is running or has failed — that screen owns the window.
    case agentRepair
    /// We cannot prove launchd is running the job we installed.
    case agentNotRunning(AgentStallReason)
    /// The gate holds AND the agent reports a problem we understand.
    case folderAccess(FolderAccess)
    /// The gate holds and the agent reported a value this build does not know.
    /// Never folded into `normal`: a silent calm status over an unknown problem is
    /// the exact failure this case exists to prevent.
    case accessUnknown(String)
    case normal

    /// The stall reason when this is `.agentNotRunning`, else nil.
    var stallReason: AgentStallReason? {
        if case .agentNotRunning(let reason) = self { return reason }
        return nil
    }

    /// True when this surface must OWN the window (a full screen), as opposed to
    /// the access family, which M6 renders as a card over the normal landing.
    var ownsWindow: Bool {
        switch self {
        case .agentRepair, .agentNotRunning: return true
        case .folderAccess, .accessUnknown, .normal: return false
        }
    }
}

/// Everything the router needs, as PLAIN VALUES — no disk, no processes, no
/// clocks. That is the point: the fail-closed rule is then a pure function that a
/// self-check can drive through every combination, including the ones that are
/// hard to stage on a real machine (installer killed mid-bootstrap, agent from a
/// previous generation still alive, an agent newer than the app).
struct InstallTruth: Equatable {
    /// An install exists at all (a receipt or a LaunchAgent plist). False ⇒ Setup
    /// owns the window and none of this applies.
    var hasInstall: Bool
    /// `disk PA0 == installedHelperPath`, read live. False also covers "could not
    /// read PA0" — unknown is treated as wrong (fail-closed).
    var pa0IsHelper: Bool
    /// Generation from `install-receipt.json` (nil = no receipt / no generation).
    var receiptGeneration: String?
    /// Generation the RUNNING agent published into state.json (nil = it published
    /// none, e.g. it has not ticked yet or launchd did not hand it one).
    var stateGeneration: String?
    /// The agent's access verdict (nil = never published).
    var folderAccess: FolderAccess?
    /// An install/repair is running, or failed and still owns the screen.
    var updateOccupiesWindow: Bool
    /// Seconds since the install "settled" (app launch, or the moment an install
    /// finished). Only used to hold back the "агент не запустился" verdict while
    /// the freshly-bootstrapped agent has not had its first tick yet.
    var secondsSinceInstallSettled: TimeInterval

    /// How long a fresh install is allowed to have no/stale generation before we
    /// call it stalled (plan v2 §6.3: "отсутствует дольше ~15 с").
    static let generationGrace: TimeInterval = 15

    init(hasInstall: Bool, pa0IsHelper: Bool,
         receiptGeneration: String?, stateGeneration: String?,
         folderAccess: FolderAccess?, updateOccupiesWindow: Bool = false,
         secondsSinceInstallSettled: TimeInterval = .greatestFiniteMagnitude) {
        self.hasInstall = hasInstall
        self.pa0IsHelper = pa0IsHelper
        self.receiptGeneration = receiptGeneration
        self.stateGeneration = stateGeneration
        self.folderAccess = folderAccess
        self.updateOccupiesWindow = updateOccupiesWindow
        self.secondsSinceInstallSettled = secondsSinceInstallSettled
    }

    /// THE invariant (plan v2 §6.3 as amended by addendum §4.5):
    ///
    ///     показываем поверхность доступа  ⇔  disk PA0 == installedHelperPath
    ///                                     ∧  state.install_generation == receipt.generation
    ///                                     ∧  updatePhase ∉ {running, failed}
    ///
    /// Why both halves are load-bearing: a CORRECT plist on disk does not prove
    /// launchd is running it. The installer can die between `publish plist` and
    /// `bootstrap`, leaving a perfect plist while the OLD job keeps running. If we
    /// showed the access card then, the user would grant access to the NEW binary
    /// while the OLD one kept doing the work — a grant that looks given and does
    /// nothing, with no way for the user to tell.
    var allowsFolderAccessSurface: Bool {
        guard hasInstall, !updateOccupiesWindow, pa0IsHelper else { return false }
        guard let receipt = receiptGeneration, !receipt.isEmpty else { return false }
        guard let live = stateGeneration, !live.isEmpty else { return false }
        return live == receipt
    }

    /// The destination, in fixed priority order.
    var surface: StatusSurface {
        if updateOccupiesWindow { return .agentRepair }
        guard hasInstall else { return .normal }
        // Disk proof first: a wrong PA0 is not a timing problem, so no grace.
        guard pa0IsHelper else { return .agentNotRunning(.pa0Mismatch) }
        // A generation that has not landed yet is a TIMING problem right after an
        // install — hold the verdict for the grace window, then be honest.
        let settling = secondsSinceInstallSettled < InstallTruth.generationGrace
        guard let receipt = receiptGeneration, !receipt.isEmpty else {
            return settling ? .normal : .agentNotRunning(.receiptMissing)
        }
        guard let live = stateGeneration, !live.isEmpty else {
            return settling ? .normal : .agentNotRunning(.generationMissing)
        }
        guard live == receipt else {
            return settling ? .normal : .agentNotRunning(.generationMismatch)
        }
        // Gate holds — now, and only now, the agent's own verdict is trustworthy.
        guard let access = folderAccess else { return .normal }
        if access.needsSurface { return .folderAccess(access) }
        if case .unknown(let raw) = access { return .accessUnknown(raw) }
        return .normal
    }
}

// MARK: - What the app does to the install AT LAUNCH (plan v2 §6.2)

/// The one thing launch is allowed to do to the installation, before any UI.
enum StartupInstallAction: Equatable {
    /// Nothing is installed — the Setup screen owns the window.
    case setup
    /// The staged bytes are behind the bundle → the FULL installer, asynchronously,
    /// behind the `.updating` screen (venv/pip can take tens of seconds).
    case fullInstall
    /// The bytes are current but `ProgramArguments[0]` is wrong → the OFFLINE
    /// `--repair-launchd-only`, synchronously, before the first frame.
    case repairLaunchdOnly
    /// Touch nothing.
    case none
}

/// The launch decision, as a pure function of six facts.
///
/// The ORDER is the part that bites, and it bit once already while writing this:
/// on a v0.9 install `ProgramArguments[0]` is wrong AND the bytes are stale. Doing
/// the "cheap" offline repair first looks right and is wrong — v0.9 never staged
/// the frozen helper, so the repair has nothing to point launchd at and dies on its
/// golden-SHA check, every launch, while the actual fix (the full install that
/// stages the helper) never runs. Full update wins; the offline repair is only for
/// the case it cannot help with — bytes already current, job pointed at the wrong
/// executable (the installer died between `publish plist` and `bootstrap`).
enum StartupPlan {
    static func decide(isInstalled: Bool,
                       bytesStale: Bool,
                       bundledIsOlderThanInstall: Bool,
                       watchDirKnown: Bool,
                       pa0IsHelper: Bool,
                       helperStaged: Bool) -> StartupInstallAction {
        guard isInstalled else { return .setup }
        // M11f — an older .app must never "update" a newer install: its installer
        // would downgrade the engine, and a v0.9 installer re-points PA0 back at
        // runner.sh, killing folder access for good.
        guard !bundledIsOlderThanInstall else { return .none }
        // Our advantage over the donor, kept deliberately: with no PROVEN watch
        // folder we run nothing at all. The donor falls back to its default here,
        // which silently re-points the user's agent at a folder they left.
        if bytesStale && watchDirKnown { return .fullInstall }
        // The offline repair needs a staged helper to point at; without one this is
        // a full-install case that we simply cannot do automatically.
        if !pa0IsHelper && helperStaged { return .repairLaunchdOnly }
        return .none
    }
}

// MARK: - Which folder is really watched (plan v2 M2f)

/// Resolves the watched folder from the three sources IN ORDER OF PROOF, and
/// never invents a default.
///
/// The order is not cosmetic. `state.json` is written by the agent on every scan,
/// so it can easily be NEWER in wall-clock terms while describing an OLDER install
/// — the classic case being an agent from the previous generation that is still
/// alive and still stamping the old folder. Re-running the installer with that
/// value would silently RE-POINT the user's agent at a folder they moved away
/// from. So: the receipt (written last, after launchd was verified) wins; the
/// plist (what launchd was actually handed) is second; state.json is accepted ONLY
/// when it proves it belongs to the current install by carrying the same
/// generation.
///
/// nil is a real, useful answer: "we do not know" ⇒ the caller must NOT run the
/// installer, because that would fall back to `~/Desktop/mp3-to-m4b`.
enum WatchDirTruth {
    static func resolve(receiptWatchDir: String?,
                        receiptGeneration: String?,
                        plistWatchDir: String?,
                        stateWatchDir: String?,
                        stateGeneration: String?) -> String? {
        if let d = receiptWatchDir, !d.isEmpty { return d }
        if let d = plistWatchDir, !d.isEmpty { return d }
        // state.json only counts when it demonstrably belongs to this install.
        guard let d = stateWatchDir, !d.isEmpty,
              let live = stateGeneration, !live.isEmpty,
              let receipt = receiptGeneration, !receipt.isEmpty,
              live == receipt
        else { return nil }
        return d
    }
}

// MARK: - StateStore: the disk side of the install truth

extension StateStore {
    /// The frozen helper's file name. It is a grant identity (the string the user
    /// sees in the privacy panel and in the consent dialog), so it is a literal in
    /// exactly one place per language: `installer.sh` (HELPER_NAME),
    /// `build/helper-guard.sh`, and here.
    static let helperName = "mp3-to-m4b-agent"

    /// `<support>/bin/mp3-to-m4b-agent` — where the installer puts PA0. This path
    /// is half of the TCC grant identity (path + bytes), which is why it is derived
    /// from `supportRoot` and never hardcoded to the production tree.
    var installedHelperPath: String {
        supportRoot.appendingPathComponent("bin/\(StateStore.helperName)").path
    }

    /// `<support>/bin/runner.sh` — the helper's sibling by a contract baked into
    /// the frozen bytes.
    var installedRunnerPath: String {
        supportRoot.appendingPathComponent("bin/runner.sh").path
    }

    /// The LaunchAgent label, honoring MP3TOM4B_LABEL exactly like installer.sh.
    var launchAgentLabel: String {
        let raw = ProcessInfo.processInfo.environment["MP3TOM4B_LABEL"] ?? ""
        return raw.isEmpty ? "com.arrivarus.mp3tom4b.agent" : raw
    }

    /// Where the LaunchAgent plist lives. Honors MP3TOM4B_LAUNCHAGENTS_DIR too (the
    /// installer supports it under its test latch), so a scratch run never reads
    /// the human's real plist.
    var launchAgentPlistPath: String {
        let override = ProcessInfo.processInfo.environment["MP3TOM4B_LAUNCHAGENTS_DIR"] ?? ""
        let dir = override.isEmpty
            ? URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
                .appendingPathComponent("Library/LaunchAgents", isDirectory: true)
            : URL(fileURLWithPath: (override as NSString).expandingTildeInPath, isDirectory: true)
        return dir.appendingPathComponent("\(launchAgentLabel).plist").path
    }

    /// `<support>/install-receipt.json` (App Support ROOT, not `state/`).
    var receiptPath: String {
        supportRoot.appendingPathComponent("install-receipt.json").path
    }

    /// The install receipt, or nil when absent / unreadable / generation-less.
    func loadReceipt() -> InstallReceipt? {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: receiptPath)) else {
            return nil
        }
        return InstallReceipt(data: data)
    }

    /// LIVE read of `ProgramArguments[0]` — no cache, no fallback. Every caller
    /// that is about to tell the user something about access must go through this
    /// AT THE MOMENT OF THE CLAIM, not through a value read at launch.
    func diskProgramArgument0() -> String? {
        LaunchAgentPlist.programArgument0(plistPath: launchAgentPlistPath)
    }

    /// LIVE `disk PA0 == installedHelperPath`. Unreadable ⇒ false (fail-closed).
    func installedRunnerIsHelper() -> Bool {
        LaunchAgentPlist.samePath(diskProgramArgument0(), installedHelperPath)
    }

    /// The watch dir the plist hands launchd, or nil.
    func plistWatchDir() -> String? {
        LaunchAgentPlist.environmentValue("MP3TOM4B_WATCH_DIR",
                                          plistPath: launchAgentPlistPath)
    }

    /// True when SOMETHING is installed: a receipt or a LaunchAgent plist. A bare
    /// staged tree (a NO_LAUNCHCTL dev install) deliberately does not count — there
    /// is no job to be "not running", so nothing should be claimed about one.
    func hasInstallEvidence() -> Bool {
        let fm = FileManager.default
        return fm.fileExists(atPath: receiptPath)
            || fm.fileExists(atPath: launchAgentPlistPath)
    }

    /// Assemble the live truth. One disk pass, then a pure decision.
    /// `state` is passed in so the caller reuses the showcase it already loaded.
    func installTruth(state: ShowcaseState,
                      updateOccupiesWindow: Bool,
                      secondsSinceInstallSettled: TimeInterval) -> InstallTruth {
        InstallTruth(
            hasInstall: hasInstallEvidence(),
            pa0IsHelper: installedRunnerIsHelper(),
            receiptGeneration: loadReceipt()?.generation,
            stateGeneration: state.agent.installGeneration,
            folderAccess: state.agent.folderAccess,
            updateOccupiesWindow: updateOccupiesWindow,
            secondsSinceInstallSettled: secondsSinceInstallSettled)
    }

    /// The watched folder, resolved receipt → plist → same-generation state, with
    /// NO default (M2f). nil ⇒ callers must not run the installer.
    func resolvedWatchDir(state: ShowcaseState) -> String? {
        let receipt = loadReceipt()
        return WatchDirTruth.resolve(
            receiptWatchDir: receipt?.watchDir,
            receiptGeneration: receipt?.generation,
            plistWatchDir: plistWatchDir(),
            stateWatchDir: state.agent.watchDir,
            stateGeneration: state.agent.installGeneration)
    }

    /// Everything the diagnostics block needs, read fresh. `stderrTail` comes from
    /// the caller (the last installer run) — the disk cannot know it.
    func diagnostics(state: ShowcaseState, stderrTail: String = "") -> InstallDiagnostics {
        let receipt = loadReceipt()
        return InstallDiagnostics(
            expectedHelperPath: installedHelperPath,
            actualPA0: diskProgramArgument0(),
            plistPath: launchAgentPlistPath,
            receiptPath: receiptPath,
            receiptGeneration: receipt?.generation,
            stateGeneration: state.agent.installGeneration,
            receiptWatchDir: receipt?.watchDir,
            plistWatchDir: plistWatchDir(),
            stateWatchDir: state.agent.watchDir,
            folderAccess: state.agent.folderAccess?.rawValue,
            installerStderrTail: stderrTail)
    }
}

// MARK: - Diagnostics (what the dead-end screens must show — M12f)

/// The facts a stuck user (or a support conversation) needs, all in one value.
///
/// M12f: the `.failed` update screen used to show ONE line — «обновите через
/// Настройки» — from a screen with no way to reach Настройки. The block below is
/// the other half of that fix: not "an error happened", but WHICH of the three
/// sources disagree, so the next action is obvious rather than guessed.
struct InstallDiagnostics: Equatable {
    /// Where PA0 must point for a folder grant to be possible at all.
    var expectedHelperPath: String
    /// Where it actually points. nil = could not be read (which is why the gate
    /// fails closed — unknown is not "fine").
    var actualPA0: String?
    var plistPath: String
    var receiptPath: String
    var receiptGeneration: String?
    var stateGeneration: String?
    /// The watch dir as each source sees it. Shown side by side on purpose: a
    /// disagreement here is the whole reason `WatchDirTruth` refuses to guess.
    var receiptWatchDir: String?
    var plistWatchDir: String?
    var stateWatchDir: String?
    var folderAccess: String?
    /// Last few stderr lines of the most recent installer run ("" when none).
    var installerStderrTail: String

    static let empty = InstallDiagnostics(
        expectedHelperPath: "", actualPA0: nil, plistPath: "", receiptPath: "",
        receiptGeneration: nil, stateGeneration: nil,
        receiptWatchDir: nil, plistWatchDir: nil, stateWatchDir: nil,
        folderAccess: nil, installerStderrTail: "")

    /// `(label, value)` rows in display order. One place builds them so the screen
    /// stays a renderer and every field is guaranteed to be shown.
    var rows: [(String, String)] {
        func show(_ v: String?) -> String {
            guard let v = v, !v.isEmpty else { return "—" }
            return v
        }
        return [
            ("Запускается сейчас (PA0)", show(actualPA0)),
            ("Должен запускаться", show(expectedHelperPath)),
            ("LaunchAgent", show(plistPath)),
            ("Чек установки", show(receiptPath)),
            ("Поколение: чек / агент", "\(show(receiptGeneration)) / \(show(stateGeneration))"),
            ("Папка: чек", show(receiptWatchDir)),
            ("Папка: LaunchAgent", show(plistWatchDir)),
            ("Папка: агент", show(stateWatchDir)),
            ("Доступ к папке", show(folderAccess)),
        ]
    }

    /// One-line copyable summary (the «Скопировать диагностику» action).
    var plainText: String {
        var out = rows.map { "\($0.0): \($0.1)" }.joined(separator: "\n")
        if !installerStderrTail.isEmpty {
            out += "\n\nУстановщик (stderr):\n" + installerStderrTail
        }
        return out
    }
}
