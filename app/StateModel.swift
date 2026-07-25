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

/// `agent` block of the showcase — the watched folder + a liveness flag the agent
/// stamps when it writes state (scan.py `build_state`: `agent.active = true`). The
/// Status screen (spec §5) reads `active` for the "Активен / Пауза" pill; absent on
/// older states → defaults to `true` (the file exists ⇒ the agent has run).
struct AgentInfo: Codable, Equatable {
    var watchDir: String?
    var active: Bool

    enum CodingKeys: String, CodingKey {
        case watchDir = "watch_dir"
        case active
    }

    init(watchDir: String?, active: Bool = true) {
        self.watchDir = watchDir
        self.active = active
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        watchDir = try? c.decodeIfPresent(String.self, forKey: .watchDir)
        active = (try? c.decodeIfPresent(Bool.self, forKey: .active)) ?? true
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
        case title
        case author
        case chapters
        case totalDurationMS = "total_duration_ms"
        case sourceSamplerate = "source_samplerate"
        case coverState = "cover_state"
        case coverPreview = "cover_preview"
        case coverOptions = "cover_options"
        case coverSelected = "cover_selected"
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

    init(bookID: String, srcDir: String, status: String, sourceRev: String,
         confirmToken: String, title: String, author: String,
         chapters: [ChapterEntry], totalDurationMS: Int,
         sourceSamplerate: Int? = nil, coverState: String,
         coverPreview: String?, coverOptions: [CoverOption] = [],
         coverSelected: String? = nil, params: BookParams,
         progress: Double = 0, error: BuildError? = nil,
         result: BookResult? = nil) {
        self.bookID = bookID
        self.srcDir = srcDir
        self.status = status
        self.sourceRev = sourceRev
        self.confirmToken = confirmToken
        self.title = title
        self.author = author
        self.chapters = chapters
        self.totalDurationMS = totalDurationMS
        self.sourceSamplerate = sourceSamplerate
        self.coverState = coverState
        self.coverPreview = coverPreview
        self.coverOptions = coverOptions
        self.coverSelected = coverSelected
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
        params       = (try? c.decode(BookParams.self, forKey: .params)) ?? .defaults
        progress     = (try? c.decodeIfPresent(Double.self, forKey: .progress)) ?? 0
        error        = try? c.decodeIfPresent(BuildError.self, forKey: .error)
        result       = try? c.decodeIfPresent(BookResult.self, forKey: .result)
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
