// mp3-to-m4b — native SwiftUI window (host).
//
// M0.3: the app becomes a READER of the agent's files. It loads state.json + the
// per-book manifests, shows the first `pending-confirm` book with its chapters in
// a minimal confirm window, and RAISES the window on the rising edge of a new
// pending-confirm book appearing. Live updates come from a DispatchSource watch on
// the `state/` directory (no timer) plus a catch-up refresh on window focus.
//
// This is the first real UI but NOT the final pixel: the full confirm window
// (cover chain, quality presets, estimate, all states) lands at M1. Here we render
// a recognizable working minimum on REAL manifest data: header + book title +
// chapter list + a "Собрать" button (a print-stub until M0.4 writes the command).
//
// Plain windowed app (NOT LSUIElement). Unsandboxed, no external deps:
// SwiftUI + AppKit + Foundation only. macOS 11.0 target. We drive the window via
// AppKit (NSApplication + NSWindow + NSHostingView) for precise control over the
// fixed width and the dark titlebar (cloned from the fb2-to-epub neighbor).

import AppKit
import SwiftUI

// MARK: - Reader model (observable; the app's read-only view)

/// Which screen the single window is showing. Destinations:
///   · status   — the "home" screen (spec §5), the resting/landing destination;
///   · confirm  — a specific book's confirm window (raised on the rising edge of a
///                new pending-confirm book, or opened from the queue);
///   · queue    — the full queue (spec §7);
///   · settings — a later screen (the "Сменить"/gear targets); a placeholder for now.
/// Status is HOME (decision D8): when no book awaits confirmation the window rests
/// on Status, not a bare idle card.
enum Screen {
    case setup     // first-launch Setup (spec §01): ffmpeg check + folder + install
    case updating  // brief auto-update of a stale staged agent (installer re-run)
    case status    // the home screen (spec §5)
    case confirm   // a book's confirm window
    case queue     // the full queue (spec §7)
    case settings  // settings (later slice; placeholder destination)

    /// The pinned window WIDTH for this screen (spec §2): the confirm window is 640,
    /// everything else (setup/updating/status/queue/settings) is the standard 400.
    var windowWidth: CGFloat {
        self == .confirm ? Tokens.M.windowConfirm : Tokens.M.windowStandard
    }
}

/// The confirm footer's "Применить параметры ко всем (N)" (US-3.7 / spec §3):
/// the build params the user approved once, plus the ids of the books that were
/// awaiting confirmation at that moment. Purely APP-SIDE — the protocol has no
/// `apply-to-all` command, so this only PRE-FILLS the confirm window of those
/// books; each one still rides to the agent in its own `confirm-build` after a
/// human "ок" (invariant I2). The COVER is deliberately not part of the preset —
/// it stays per-book (US-3.7 AC: «обложку всё равно подтверждаю по каждой»).
struct ParamsPreset: Equatable {
    let params: BookParams
    /// Snapshot of the `pending-confirm` ids at the moment of the click. A book
    /// recognized LATER was not "ожидающей" then, so it keeps its own defaults
    /// instead of silently inheriting a stale preset.
    let bookIDs: Set<String>

    func applies(to bookID: String) -> Bool { bookIDs.contains(bookID) }
}

/// Holds the showcase + the manifest for the book we are currently presenting.
/// Mutated only on the main thread (the watcher hops to main before refreshing).
final class ReaderModel: ObservableObject {
    @Published var state: ShowcaseState = .empty
    /// Manifest for the first pending-confirm book, when one exists.
    @Published var manifest: BookManifest?
    /// The summary row backing `manifest` (title/status come from the showcase).
    @Published var book: BookSummary?
    /// The current destination of the single window. Defaults to Status (home, D8);
    /// the AppDelegate flips to .confirm on the rising edge of a new pending book and
    /// back to .status when the active book clears.
    @Published var screen: Screen = .status
    /// Phase of the launch-time agent auto-update (drives the `.updating` screen).
    /// Published so the spinner/error updates live as the AppDelegate runs/completes
    /// the bundled installer off the main thread.
    @Published var agentUpdatePhase: InstallPhase = .running
    /// Session-scoped params preset from the confirm footer's "Применить параметры
    /// ко всем (N)". nil = every book seeds from its own manifest defaults.
    @Published var paramsPreset: ParamsPreset?
    /// The book the user explicitly picked in the queue («Подтвердить» on a ROW).
    /// nil = no pick → the window presents the first active book (the auto-surface
    /// default). Dropped automatically once the pick stops being active, and reset
    /// by the AppDelegate on the rising edge of a NEW pending book so the agent's
    /// raise always lands on the fresh queue, not on a stale hand-pick.
    @Published private(set) var selectedBookID: String?

    private let store: StateStore

    init(store: StateStore) {
        self.store = store
    }

    /// "Применить параметры ко всем (N)": remember `params` for every book that is
    /// awaiting confirmation RIGHT NOW, so their confirm windows open pre-filled
    /// with it. No command is written — the agent has no `apply-to-all` action and
    /// each book is still confirmed individually.
    func applyParamsToAllPending(_ params: BookParams) {
        paramsPreset = ParamsPreset(params: params,
                                    bookIDs: Set(state.pendingConfirm.map { $0.bookID }))
    }

    /// Per-book manifest lookup for the queue's qrows (cover preview + result path
    /// live in the manifest, not the lightweight showcase). Pure read.
    func manifest(for book: BookSummary) -> BookManifest? {
        store.loadManifest(bookID: book.bookID)
    }

    /// Re-read state.json and re-resolve the presented book + its manifest.
    /// Presenting converting/error books — not just pending — lets the window
    /// mirror the live build (spec §3 state table). The rising-edge WINDOW RAISE
    /// still keys on pending-confirm only (see AppDelegate).
    func refresh() {
        state = store.loadState()
        resolvePresented()
    }

    /// Queue «Подтвердить» on a ROW → present THAT book (QueueView contract, :41).
    /// Without this the window always showed `activeBooks.first`, so confirming the
    /// second book opened the first one's window — and «Собрать» built the wrong book.
    func present(_ picked: BookSummary) {
        selectedBookID = picked.bookID
        resolvePresented()
    }

    /// Queue «Подтвердить все по очереди» → start at the FIRST book awaiting
    /// confirmation (QueueView :220 contract), not at whatever active book happens
    /// to sort first (a converting one would otherwise win).
    func presentFirstPending() {
        selectedBookID = state.pendingConfirm.first?.bookID
        resolvePresented()
    }

    /// Drop the explicit pick → back to the first-active default. Used on the
    /// rising edge of a new pending book so the agent's raise is never swallowed
    /// by an older hand-picked book.
    func clearSelection() {
        guard selectedBookID != nil else { return }
        selectedBookID = nil
        resolvePresented()
    }

    /// 1-based position of the presented book among the active ones — the confirm
    /// header's «N из M». Falls back to 1 when nothing is presented.
    var presentedPosition: Int {
        guard let id = book?.bookID, let pos = state.activePosition(of: id) else { return 1 }
        return pos
    }

    /// Apply the routing rule (`ShowcaseState.presentedBook`) to the CURRENT
    /// showcase and load that book's manifest. A pick that no longer resolves is
    /// forgotten here, so the window follows the queue again instead of sticking
    /// to a book that finished or vanished.
    private func resolvePresented() {
        guard let target = state.presentedBook(selectedID: selectedBookID) else {
            selectedBookID = nil
            book = nil
            manifest = nil
            return
        }
        if selectedBookID != nil && selectedBookID != target.bookID { selectedBookID = nil }
        book = target
        manifest = store.loadManifest(bookID: target.bookID)
    }
}

// MARK: - Root view (idle ↔ confirm)

private struct RootView: View {
    @ObservedObject var model: ReaderModel
    /// Writes the `confirm-build` command with the user's EDITED params + cover pick
    /// (M1). Returns true on success so the confirm view can show its visual ack;
    /// false → the button surfaces an error.
    let onBuild: (BookManifest, BookParams, String?, String?) -> Bool
    /// Writes a `grouping-choice` command (combine / separate) for the loose-mp3
    /// set. Returns true on a successful drop. "Отмена" sends no command (the group
    /// stays pending) — handled locally in the sheet.
    let onGroupingChoice: (PendingGroup, EngineClient.GroupingChoice) -> Bool
    /// Writes a `cancel` command for a converting book (queue "Отмена", D13). Returns
    /// true on a successful drop so the row can show its ack; the agent kills ffmpeg
    /// and lands the book back at pending-confirm (the watcher then clears the row).
    let onCancel: (BookSummary) -> Bool
    /// Writes a `reconvert` command for a done book (queue "Собрать заново"). Returns
    /// true on a successful drop so the row can show its ack; the agent re-arms the
    /// book back to pending-confirm (fresh token + cleared idempotency ledger), so it
    /// leaves ГОТОВО and the confirm window surfaces via the watcher for a rebuild.
    let onReconvert: (BookSummary) -> Bool
    /// Writes a `skip` command (confirm footer «Пропустить»). Returns true on a
    /// successful drop so the button can show its ack; the agent marks the book
    /// `skipped` (sources untouched) and it moves to the queue's ПРОПУЩЕНО section.
    let onSkip: (BookSummary) -> Bool
    /// Navigate the single window to a screen (the AppDelegate resizes the window to
    /// that screen's width/height when this flips `model.screen`).
    let navigate: (Screen) -> Void
    /// Reveal an absolute path in Finder (done row's "Открыть" / "Открыть папку").
    let reveal: (String) -> Void
    /// Called by the Setup screen after a successful install (agent now live) — the
    /// AppDelegate flips to Status and starts the state watcher.
    let onInstalled: () -> Void
    /// "Очистить" the Status recent-built list (app-owned marker; state.json is never
    /// rewritten — D13). The host stamps the marker and refreshes.
    let onClearHistory: () -> Void
    /// The current "recent cleared at" cutoff (app-owned marker) — a done/error book
    /// built at or before this is hidden from the Status list. nil = nothing cleared.
    let recentClearedAt: () -> Date?
    /// "Сбросить статистику" (Настройки, danger). Captures baselines + clears the
    /// recent list (app-owned markers; state.json untouched — D13).
    let onResetStats: () -> Void
    /// "Открыть на GitHub" (Настройки, version card). Opens the repo in the browser.
    let onOpenGitHub: () -> Void
    /// Retry the agent auto-update after a failure (the `.updating` screen's button).
    /// The current phase is read reactively from `model.agentUpdatePhase`.
    var onRetryAgentUpdate: () -> Void = {}
    /// Current freshness of the staged agent vs. the bundled one — passed to Settings'
    /// «Обновить агент» card. Computed by the AppDelegate (has the StateStore).
    var agentFreshness: () -> AgentFreshness = { .undecidable }

    var body: some View {
        ZStack {
            Tokens.Canvas.windowGradient

            // The active destination. Status (home, spec §5) / Queue (spec §7) /
            // the confirm flow / settings. A pending grouping decision still
            // OUTRANKS everything (modal sheet, below).
            Group {
                switch model.screen {
                case .setup:
                    SetupView(onInstalled: onInstalled)
                case .updating:
                    AgentUpdatingView(phase: model.agentUpdatePhase, onRetry: onRetryAgentUpdate)
                case .status:
                    StatusView(
                        state: model.state,
                        manifestFor: { model.manifest(for: $0) },
                        onOpenQueue: { navigate(.queue) },
                        onOpenFolder: {
                            if let dir = model.state.agent.watchDir, !dir.isEmpty { reveal(dir) }
                        },
                        onOpenSettings: { navigate(.settings) },
                        onClearHistory: onClearHistory,
                        recentClearedAt: recentClearedAt()
                    )
                case .queue:
                    QueueView(
                        state: model.state,
                        manifestFor: { model.manifest(for: $0) },
                        // "Подтвердить" on a ROW must open THAT book (QueueView :41).
                        // The picked book is recorded on the model BEFORE navigating,
                        // so the confirm window presents it instead of the first one.
                        onConfirm: { book in
                            model.present(book)
                            navigate(.confirm)
                        },
                        // "Подтвердить все по очереди" deliberately starts at the
                        // FIRST pending book and walks the queue from there.
                        onConfirmAll: {
                            model.presentFirstPending()
                            navigate(.confirm)
                        },
                        onReveal: { manifest in
                            if let p = manifest.result?.outputPath, !p.isEmpty { reveal(p) }
                        },
                        onReconvert: { book in onReconvert(book) },
                        // «Вернуть» on a ПРОПУЩЕНО row is the SAME re-arm the agent
                        // already implements for «Собрать заново» — one mechanism,
                        // no second protocol command to undo a skip.
                        onRestore: { book in onReconvert(book) },
                        onCancel: { book in onCancel(book) },
                        onOpenFolder: {
                            if let dir = model.state.agent.watchDir, !dir.isEmpty { reveal(dir) }
                        },
                        onBack: { navigate(backFromQueue) }
                    )
                case .confirm:
                    confirmOrIdle
                case .settings:
                    SettingsView(
                        watchDir: model.state.agent.watchDir,
                        onBack: { navigate(.status) },
                        onResetStats: onResetStats,
                        onOpenGitHub: onOpenGitHub,
                        agentFreshness: agentFreshness(),
                        refreshFreshness: agentFreshness
                    )
                }
            }

            // Surface priority (spec §6 / brief): a pending grouping decision
            // OUTRANKS the confirm window — overlay the modal sheet with a scrim.
            if let group = model.state.firstPendingGroup {
                GroupingSheetOverlay(group: group, onGroupingChoice: onGroupingChoice)
                    .id(group.groupID)        // reset selection per new group
                    .transition(.opacity)
            }
        }
        // Window width tracks the screen (spec §2): confirm = 640, else 400.
        .frame(width: model.screen.windowWidth)
    }

    /// Back target from the queue: the confirm window if a book is actively being
    /// presented (we likely arrived from there), otherwise Status (home).
    private var backFromQueue: Screen {
        model.manifest != nil ? .confirm : .status
    }

    // The confirm-flow content: the active book's window, or — when nothing is
    // pending — the Status home screen (D8: Status is the resting destination, not a
    // bare idle card). This makes a window opened directly into .confirm with no
    // active book degrade gracefully to home.
    @ViewBuilder
    private var confirmOrIdle: some View {
        if let manifest = model.manifest, let book = model.book {
            ConfirmView(
                book: book,
                manifest: manifest,
                pendingCount: model.state.activeBooks.count,
                // Header "N из M": N is the presented book's own position, so a
                // book opened from the queue reads "2 из 2", not a misleading "1".
                position: model.presentedPosition,
                pendingConfirmCount: model.state.pendingConfirm.count,
                queueCount: model.state.books.count,
                // Seed from the "ко всем" preset when this book was one of the
                // pending ones at the time it was set (else: manifest defaults).
                paramsPreset: model.paramsPreset,
                onBuild: { params, coverID, coverCustomPath in
                    onBuild(manifest, params, coverID, coverCustomPath)
                },
                onApplyToAll: { params in model.applyParamsToAllPending(params) },
                onOpenQueue: { navigate(.queue) },
                // Reuse the SAME cancel path as the queue's "Отмена" (D13): the
                // converting footer's "Отменить конвертацию" drops the identical
                // `cancel` command; the agent kills ffmpeg and lands the book back at
                // pending-confirm, and the file-watch returns the window to confirm.
                onCancel: { onCancel(book) },
                onSkip: { onSkip(book) }
            )
            // Reset the per-book ack + edited params when the presented book
            // changes (a different book_id means a different confirm flow).
            .id(manifest.bookID)
        } else {
            StatusView(
                state: model.state,
                manifestFor: { model.manifest(for: $0) },
                onOpenQueue: { navigate(.queue) },
                onOpenFolder: {
                    if let dir = model.state.agent.watchDir, !dir.isEmpty { reveal(dir) }
                },
                onOpenSettings: { navigate(.settings) },
                onClearHistory: onClearHistory,
                recentClearedAt: recentClearedAt()
            )
        }
    }
}

// MARK: - Settings screen (in-app settings — watched-folder re-point + status)

/// The in-app Settings screen (400 wide, spec §6 family). The Status "Сменить" /
/// gear controls land here. Its one working job today: RE-POINT the background agent
/// at a new watched folder. The mechanism is NOT reinvented — it re-runs the SAME
/// bundled `installer.sh` the Setup screen uses, via the SAME `InstallRunner`,
/// passing the chosen folder as the WATCH_DIR (argv[1]). The installer regenerates
/// the LaunchAgent plist (WatchPaths[0] + MP3TOM4B_WATCH_DIR) and idempotently
/// reloads the agent (bootout→bootstrap→enable→kickstart).
///
/// SAFETY (the user has a LIVE install): a re-point either fully succeeds or the
/// agent stays on the OLD folder — the installer is idempotent and only swaps the
/// plist on a clean run. So the shown "current folder" flips to the new one ONLY
/// after `installer.sh` exits 0; a failure surfaces the installer's honest stderr in
/// red and leaves everything as it was. Cancelling the folder picker changes nothing.
///
/// Uses the SAME building blocks as Setup (no new visual language): `InstallRunner`
/// (bundled installer via Process), `FFmpegProbe` (engine check), `InstallPhase`
/// (idle/running/done/failed), and the folder-chooser pattern (NSOpenPanel +
/// "Создать" when missing + a real existence check via `SetupView.directoryExists`).
private struct SettingsView: View {
    /// The agent's CURRENT watched folder (from the showcase). The display flips to
    /// `appliedDir` after a successful re-point; until then this is the truth.
    let watchDir: String?
    let onBack: () -> Void
    /// "Сбросить статистику" (danger card, fb2 parity). Captures baselines + clears
    /// the recent list via app-owned markers — state.json is NEVER rewritten (D13).
    /// Shown behind a confirmation alert.
    var onResetStats: () -> Void = {}
    /// "Открыть на GitHub" (version card, fb2 parity). Opens the project repo.
    var onOpenGitHub: () -> Void = {}
    /// Test seam: extra env for the installer process (NO_LAUNCHCTL / NO_VENV /
    /// temp LABEL / scratch SUPPORT). Empty in production — a real re-point touches
    /// the live launchd agent.
    var installerExtraEnv: [String: String] = [:]
    /// The freshness of the STAGED agent vs. the one shipped in the bundle, computed
    /// by the AppDelegate (AgentUpdate.freshness). Drives the «Обновить агент» card's
    /// status line ("агент актуален / устарел / не удалось проверить"). Re-read after
    /// a manual update via `refreshFreshness`.
    var agentFreshness: AgentFreshness = .undecidable
    /// Re-compute `agentFreshness` on demand (after a manual update completes). The
    /// AppDelegate owns the `StateStore`, so it does the comparison and hands back the
    /// fresh verdict; SettingsView just reflects it.
    var refreshFreshness: () -> AgentFreshness = { .undecidable }

    // The folder the user has SELECTED to switch to (seeded from the current watch
    // dir so the field is never blank). A re-point sends THIS to the installer.
    @State private var selectedDir: String
    /// Whether `selectedDir` currently exists as a directory on disk (drives the
    /// "Создать" affordance + the button's enablement) — the same honest check Setup
    /// uses (`SetupView.directoryExists`), re-read on pick / after "Создать".
    @State private var selectedDirExists: Bool
    /// The re-point phase (reused verbatim from Setup). idle → running (spinner,
    /// disabled) → done (green ✓, current folder updates) / failed(msg) (red text).
    @State private var phase: InstallPhase = .idle
    /// The folder that is ACTUALLY watched now — the current dir until a re-point
    /// succeeds, then the newly-applied dir. Drives the "Сейчас отслеживается" block
    /// so it can never show a folder the agent isn't really on (safety).
    @State private var appliedDir: String
    /// The live ffmpeg probe (status block). Purely informational here.
    @State private var probe: FFmpegProbe = FFmpegProbe(ffmpegPath: nil, ffprobePath: nil, version: nil)
    @State private var probing = true
    /// Drives the "Сбросить статистику" confirmation alert (a reset is not undoable
    /// from the UI, so it's gated behind an explicit confirm — fb2 parity).
    @State private var showResetConfirm = false
    /// Set to true after a successful reset so the danger row shows a brief ack.
    @State private var didReset = false
    /// The «Обновить агент» phase — a SEPARATE install phase from the folder re-point
    /// `phase` (both re-run the installer, but their buttons/acks are independent).
    @State private var updatePhase: InstallPhase = .idle
    /// The live freshness verdict shown in the «Обновить агент» card (seeded from the
    /// AppDelegate's compute, refreshed after a manual update).
    @State private var freshness: AgentFreshness

    init(watchDir: String?, onBack: @escaping () -> Void,
         onResetStats: @escaping () -> Void = {},
         onOpenGitHub: @escaping () -> Void = {},
         installerExtraEnv: [String: String] = [:],
         agentFreshness: AgentFreshness = .undecidable,
         refreshFreshness: @escaping () -> AgentFreshness = { .undecidable }) {
        self.watchDir = watchDir
        self.onBack = onBack
        self.onResetStats = onResetStats
        self.onOpenGitHub = onOpenGitHub
        self.installerExtraEnv = installerExtraEnv
        self.agentFreshness = agentFreshness
        self.refreshFreshness = refreshFreshness
        let current = (watchDir?.isEmpty == false ? watchDir! : SetupView.defaultWatchDir)
        _selectedDir = State(initialValue: current)
        _selectedDirExists = State(initialValue: SetupView.directoryExists(at: current))
        _appliedDir = State(initialValue: current)
        _freshness = State(initialValue: agentFreshness)
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            SettingsHairline(color: Tokens.C.borderHairline)
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    watchFolderSection
                    if selectedIsInTCCZone {
                        fdaFootnote
                    }
                    engineSection
                    agentUpdateSection
                    dataAndAccessSection
                    versionSection
                }
                .padding(.init(top: 16, leading: 16, bottom: 20, trailing: 16))
            }
            // Cap the variable content within the screen; a taller status/error
            // scrolls internally instead of growing the window (spec §1 cap rule;
            // sibling lesson native-window-cap-height). The AppDelegate window-cap is
            // the outer belt.
            .frame(maxHeight: 520)
        }
        .frame(width: Tokens.M.windowStandard)   // 400 (spec §2)
        .onAppear(perform: runProbe)
        .alert(isPresented: $showResetConfirm) {
            Alert(
                title: Text("Сбросить статистику?"),
                message: Text("Счётчики «Собрано» и «За сегодня» и список последних книг обнулятся в приложении. Собранные файлы .m4b на диске НЕ удаляются."),
                primaryButton: .destructive(Text("Сбросить")) {
                    onResetStats()
                    didReset = true
                },
                secondaryButton: .cancel(Text("Отмена"))
            )
        }
    }

    // MARK: Header — back chevron (30 r8) + "Настройки", mirroring the queue chrome.

    private var header: some View {
        HStack(spacing: 11) {
            Button(action: onBack) {
                Image(systemName: "chevron.left")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(Tokens.C.textMuted)
                    .frame(width: Tokens.M.backBtn, height: Tokens.M.backBtn)
                    .background(
                        RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                            .fill(Tokens.C.surfaceControl)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                            .stroke(Tokens.C.borderControl, lineWidth: 1)
                    )
            }
            .buttonStyle(.plain)
            .contentShape(Rectangle())
            Text("Настройки")
                .font(.system(size: Tokens.F.h1Confirm, weight: .bold))
                .foregroundColor(Tokens.C.textHigh)
            Spacer(minLength: 8)
        }
        .padding(.init(top: 16, leading: 18, bottom: 14, trailing: 18))
    }

    // MARK: Watched-folder section — card: current folder + chooser + apply button.

    private var watchFolderSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            cap("ОТСЛЕЖИВАЕМАЯ ПАПКА")

            // Current (truth): the folder the agent is really on now (last path
            // component prominent + full path below). Updates only after success.
            currentFolderBlock

            // Chooser: field (folder glyph + selected path) + "Создать" (missing) +
            // "Сменить…" (NSOpenPanel) — the SAME shape as Setup's folderField.
            folderField

            // Re-point action + its state (idle/running/done/failed).
            applyRow
        }
        .padding(.init(top: 14, leading: 14, bottom: 14, trailing: 14))
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCard)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderCard, lineWidth: 1)
        )
    }

    // The agent's CURRENT folder (row-ic + last-path-component + full path). This is
    // the safety anchor: it shows only what the agent is really watching.
    private var currentFolderBlock: some View {
        HStack(spacing: 11) {
            ZStack {
                RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                    .fill(Tokens.C.rowIcBrandTealBg)
                Image(systemName: "folder.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(Tokens.C.brandTeal)
            }
            .frame(width: 28, height: 28)
            VStack(alignment: .leading, spacing: 1) {
                Text(currentLeaf)
                    .font(.system(size: Tokens.F.input, weight: .semibold))
                    .foregroundColor(Tokens.C.textHigh)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Text(tildeAbbrev(appliedDir))
                    .font(.system(size: Tokens.F.small, design: .monospaced))
                    .foregroundColor(Tokens.C.textSecondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 8)
        }
    }

    /// The current folder's last path component (the human name); "—" if none.
    private var currentLeaf: String {
        let leaf = (appliedDir as NSString).lastPathComponent
        return leaf.isEmpty ? "—" : leaf
    }

    // field-input (folder glyph + selected path, mono) + "Создать" (if missing) +
    // "Сменить…" — cloned from Setup's folderField so the visual language matches.
    private var folderField: some View {
        HStack(spacing: 8) {
            HStack(spacing: 7) {
                Image(systemName: "folder")
                    .font(.system(size: 13, weight: .regular))
                    .foregroundColor(Tokens.C.textSecondary)
                Text(tildeAbbrev(selectedDir))
                    .font(.system(size: 11.5, design: .monospaced))
                    .foregroundColor(Tokens.C.textHigh)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            .padding(.init(top: 9, leading: 11, bottom: 9, trailing: 11))
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .fill(Tokens.C.bgInput)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .stroke(Tokens.C.borderFieldInput, lineWidth: 1)
            )

            // "Создать" — shown only when the selected folder is missing. Accent
            // (brand gradient) recommended-next-action; on tap it creates the folder
            // at the shown path, re-checks, and disappears (same as Setup).
            if !selectedDirExists {
                Button(action: createSelectedFolder) {
                    Text("Создать")
                        .font(.system(size: Tokens.F.caption, weight: .semibold))
                        .foregroundColor(Tokens.C.textOnAccent)
                        .padding(.init(top: 9, leading: 13, bottom: 9, trailing: 13))
                        .background(
                            RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                                .fill(Tokens.Grad.brandButton)
                        )
                }
                .buttonStyle(.plain)
                .contentShape(Rectangle())
            }

            Button(action: chooseFolder) {
                Text("Сменить…")
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
                    .foregroundColor(Tokens.C.textHigh)
                    .padding(.init(top: 9, leading: 13, bottom: 9, trailing: 13))
                    .background(
                        RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                            .fill(Tokens.C.surfaceControl)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                            .stroke(Tokens.C.borderControlStrong, lineWidth: 1)
                    )
            }
            .buttonStyle(.plain)
            .contentShape(Rectangle())
        }
    }

    // The re-point action row: a status line on the left + the phased button/ack on
    // the right. States mirror Setup's install button (idle/running/done/failed).
    private var applyRow: some View {
        HStack(spacing: 10) {
            applyStatusLine
            Spacer(minLength: 8)
            applyControl
        }
    }

    // A small left-aligned status line describing the current phase / gating reason.
    @ViewBuilder
    private var applyStatusLine: some View {
        switch phase {
        case .running:
            Text("Перенастраиваю агента…")
                .font(.system(size: Tokens.F.small))
                .foregroundColor(Tokens.C.textSecondary)
        case .done:
            HStack(spacing: 6) {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundColor(Tokens.C.brandCyan)
                Text("Папка изменена")
                    .font(.system(size: Tokens.F.small, weight: .semibold))
                    .foregroundColor(Tokens.C.stepOkSub)
            }
        case .failed(let msg):
            Text(msg)
                .font(.system(size: Tokens.F.small))
                .foregroundColor(Tokens.C.dangerText)
                .lineLimit(3)
                .fixedSize(horizontal: false, vertical: true)
        case .idle:
            if isUnchanged {
                Text("Уже отслеживается")
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textTertiary)
            } else if !selectedDirExists {
                Text("Папки нет — создайте её")
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.stepCurText)
            } else {
                Text("Новая папка выбрана")
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textSecondary)
            }
        }
    }

    // The phased button: idle → "Сменить папку" (enabled iff changed + exists);
    // running → spinner "Применяю…"; done → "Сменить папку" ready again (for a
    // further change); failed → "Повторить".
    @ViewBuilder
    private var applyControl: some View {
        switch phase {
        case .running:
            HStack(spacing: 7) {
                ProgressView()
                    .controlSize(.small)
                    .progressViewStyle(.circular)
                Text("Применяю…")
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
                    .foregroundColor(Tokens.C.textSoft)
            }
            .padding(.init(top: 7, leading: 13, bottom: 7, trailing: 13))
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.appIconConfirm, style: .continuous)
                    .fill(Tokens.C.surfaceControl)
            )
        case .failed:
            applyButton(icon: "arrow.clockwise", title: "Повторить", enabled: canApply)
        case .idle, .done:
            applyButton(icon: "arrow.triangle.2.circlepath", title: "Сменить папку",
                        enabled: canApply)
        }
    }

    /// One pill-shaped apply label (.btn — radius 9). Enabled → brand gradient;
    /// disabled → dim flat fill (mockup .btn[disabled]). macOS-11-safe backgrounds.
    private func applyButton(icon: String, title: String, enabled: Bool) -> some View {
        Button(action: { if enabled { startRepoint() } }) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.system(size: 12, weight: .bold))
                Text(title)
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
            }
            .foregroundColor(enabled ? Tokens.C.textOnAccent : Tokens.C.textSecondary)
            .padding(.init(top: 7, leading: 13, bottom: 7, trailing: 13))
            .background(applyButtonBackground(enabled: enabled))
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .disabled(!enabled)
    }

    // Concrete-fill branches (macOS-11-safe: no AnyShapeStyle) — same shape as Setup.
    @ViewBuilder
    private func applyButtonBackground(enabled: Bool) -> some View {
        if enabled {
            RoundedRectangle(cornerRadius: Tokens.R.appIconConfirm, style: .continuous)
                .fill(Tokens.Grad.brandButton)
        } else {
            RoundedRectangle(cornerRadius: Tokens.R.appIconConfirm, style: .continuous)
                .fill(Color.white(0.05))
        }
    }

    /// The re-point is allowed only when the chosen folder DIFFERS from the current
    /// one AND exists on disk (launchd only watches paths that are present; the
    /// installer would create it, but we mirror Setup's "create first" affordance so
    /// the action is unambiguous). Guarding on "changed" prevents a pointless reload.
    private var canApply: Bool {
        !isUnchanged && selectedDirExists
    }

    /// The selected folder is the one already watched (no-op re-point).
    private var isUnchanged: Bool {
        normalize(selectedDir) == normalize(appliedDir)
    }

    // MARK: FDA footnote — shown when the SELECTED folder is a TCC-protected zone.

    private var fdaFootnote: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: "info.circle")
                .font(.system(size: 12, weight: .regular))
                .foregroundColor(Tokens.C.textSecondary)
                .padding(.top, 1)
            Text("Папка в Desktop / Documents / Downloads — может понадобиться разовый Full Disk Access для фонового агента.")
                .font(.system(size: Tokens.F.small))
                .foregroundColor(Tokens.C.textSecondary)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .padding(.init(top: 2, leading: 2, bottom: 0, trailing: 2))
    }

    /// True iff the SELECTED folder is inside a macOS TCC-protected zone (mirrors the
    /// installer's `needs_fda` check: Desktop / Documents / Downloads under $HOME).
    private var selectedIsInTCCZone: Bool {
        let home = NSHomeDirectory()
        guard !home.isEmpty else { return false }
        let dir = normalize(selectedDir)
        for zone in ["Desktop", "Documents", "Downloads"] {
            let prefix = normalize((home as NSString).appendingPathComponent(zone))
            if dir == prefix || dir.hasPrefix(prefix + "/") { return true }
        }
        return false
    }

    // MARK: Engine section — the conversion engine (ffmpeg) + the background agent.
    // fb2 parity: its settings card2 shows the engine (Calibre version + "движок
    // конвертации"); ours is ffmpeg. The engine row moved OFF the compact home into
    // Настройки (the home's ffmpeg stat card was removed). The agent-status row stays
    // here too (it's informative, not a control).

    private var engineSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            cap("ДВИЖОК КОНВЕРТАЦИИ")
            statusRow(icon: "waveform", tint: Tokens.C.brandCyan,
                      bg: Tokens.C.rowIcTealBg,
                      title: "ffmpeg", value: ffmpegValue, ok: probe.isFound,
                      pending: probing)
            SettingsHairline(color: Tokens.C.borderHairline)
            statusRow(icon: "bolt.fill", tint: Tokens.C.brandTeal,
                      bg: Tokens.C.rowIcBrandTealBg,
                      title: "Фоновый агент", value: "Активен", ok: true,
                      pending: false)
        }
        .padding(.init(top: 14, leading: 14, bottom: 14, trailing: 14))
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCard)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderCard, lineWidth: 1)
        )
    }

    private var ffmpegValue: String {
        if probing { return "Проверяю…" }
        if probe.isFound {
            if let v = probe.version { return "\(v) найден" }
            return "найден"
        }
        return "не найден"
    }

    // MARK: Agent-update section — «Обновить агент» (manual re-install) + freshness.
    // The app auto-updates a stale staged agent at launch (brief), but this manual
    // control is the fallback when auto didn't run/failed — and it makes the state
    // visible. The status line reflects AgentUpdate.freshness; the button re-runs the
    // SAME bundled installer with the CURRENT watch folder (appliedDir — the folder
    // truth this screen already maintains), so it never re-points the agent.

    private var agentUpdateSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            cap("ФОНОВЫЙ АГЕНТ")
            HStack(spacing: 11) {
                ZStack {
                    RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                        .fill(agentStatusBg)
                    Image(systemName: agentStatusIcon)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(agentStatusTint)
                }
                .frame(width: 28, height: 28)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Версия агента")
                        .font(.system(size: Tokens.F.body))
                        .foregroundColor(Tokens.C.textHigh)
                    Text(agentStatusSub)
                        .font(.system(size: Tokens.F.small))
                        .foregroundColor(agentStatusSubColor)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                updateControl
            }
        }
        .padding(.init(top: 14, leading: 14, bottom: 14, trailing: 14))
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCard)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderCard, lineWidth: 1)
        )
    }

    // The right-hand control: running spinner / done ack / "Обновить" (or "Повторить"
    // after a failure). "Обновить" stays available even when up-to-date (a harmless
    // idempotent reinstall — the fallback the brief asks for), but the copy leads with
    // the honest status so the user knows whether it's needed.
    @ViewBuilder
    private var updateControl: some View {
        switch updatePhase {
        case .running:
            HStack(spacing: 7) {
                ProgressView().controlSize(.small).progressViewStyle(.circular)
                Text("Обновляю…")
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
                    .foregroundColor(Tokens.C.textSoft)
            }
            .padding(.init(top: 7, leading: 13, bottom: 7, trailing: 13))
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .fill(Tokens.C.surfaceControl)
            )
        case .done:
            HStack(spacing: 6) {
                Image(systemName: "checkmark.circle.fill")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundColor(Tokens.C.brandCyan)
                Text("Обновлён")
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
                    .foregroundColor(Tokens.C.stepOkSub)
            }
        case .failed:
            updateButton(title: "Повторить", icon: "arrow.clockwise")
        case .idle:
            updateButton(title: "Обновить", icon: "arrow.triangle.2.circlepath")
        }
    }

    private func updateButton(title: String, icon: String) -> some View {
        Button(action: startManualUpdate) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.system(size: 12, weight: .semibold))
                Text(title)
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
            }
            .foregroundColor(Tokens.C.textHigh)
            .padding(.init(top: 7, leading: 13, bottom: 7, trailing: 13))
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .fill(Tokens.C.surfaceControl)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .stroke(Tokens.C.borderControl, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
    }

    // Status glyph/tint/subtitle derived from the freshness verdict (or a failed
    // manual run). Up-to-date → cyan check; outdated → amber warning; undecidable →
    // muted "can't check".
    private var agentStatusIcon: String {
        if case .failed = updatePhase { return "exclamationmark.triangle.fill" }
        switch freshness {
        case .upToDate: return "checkmark.seal.fill"
        case .outdated: return "exclamationmark.triangle.fill"
        case .undecidable: return "questionmark.circle"
        }
    }
    private var agentStatusTint: Color {
        if case .failed = updatePhase { return Tokens.C.dangerBase }
        switch freshness {
        case .upToDate: return Tokens.C.brandCyan
        case .outdated: return Tokens.C.stepCurText
        case .undecidable: return Tokens.C.textSecondary
        }
    }
    private var agentStatusBg: Color {
        switch freshness {
        case .upToDate: return Tokens.C.rowIcTealBg
        case .outdated: return Tokens.C.dangerTint10
        case .undecidable: return Tokens.C.surfaceControlSoft
        }
    }
    private var agentStatusSub: String {
        if case .failed(let msg) = updatePhase { return msg }
        switch freshness {
        case .upToDate: return "Актуален — совпадает с приложением"
        case .outdated: return "Устарел — доступно обновление"
        case .undecidable: return "Не удалось проверить"
        }
    }
    private var agentStatusSubColor: Color {
        if case .failed = updatePhase { return Tokens.C.dangerText }
        switch freshness {
        case .upToDate: return Tokens.C.stepOkSub
        case .outdated: return Tokens.C.stepCurText
        case .undecidable: return Tokens.C.textTertiary
        }
    }

    /// Manual «Обновить агент»: re-run the bundled installer with the CURRENT folder
    /// (appliedDir — never the default), off the main thread. On success re-read the
    /// freshness so the status flips to "актуален". Independent of the folder-re-point
    /// `phase`. Preserves FDA (runner.sh path unchanged).
    private func startManualUpdate() {
        guard let installer = InstallRunner.bundledInstallerPath() else {
            updatePhase = .failed("Установщик не найден в приложении (пересоберите .app).")
            return
        }
        let dir = appliedDir   // the folder the agent is really on (this screen's truth)
        let env = installerExtraEnv
        updatePhase = .running
        DispatchQueue.global(qos: .userInitiated).async {
            let result = InstallRunner.run(installerPath: installer, watchDir: dir, extraEnv: env)
            DispatchQueue.main.async {
                self.updatePhase = result
                if case .done = result {
                    // Re-derive freshness — after a successful reinstall the staged
                    // tree matches the bundle, so this flips to .upToDate.
                    self.freshness = self.refreshFreshness()
                }
            }
        }
    }

    private func statusRow(icon: String, tint: Color, bg: Color,
                           title: String, value: String, ok: Bool,
                           pending: Bool) -> some View {
        HStack(spacing: 11) {
            ZStack {
                RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                    .fill(bg)
                Image(systemName: icon)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(tint)
            }
            .frame(width: 28, height: 28)
            Text(title)
                .font(.system(size: Tokens.F.body))
                .foregroundColor(Tokens.C.textHigh)
            Spacer(minLength: 8)
            HStack(spacing: 5) {
                if !pending {
                    Image(systemName: ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundColor(ok ? Tokens.C.brandCyan : Tokens.C.dangerBase)
                }
                Text(value)
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
                    .foregroundColor(pending ? Tokens.C.textSecondary
                                     : (ok ? Tokens.C.brandCyan : Tokens.C.dangerText))
                    .lineLimit(1)
            }
        }
    }

    // MARK: Data & access — "Сбросить статистику" (danger) + "Full Disk Access ›".
    // fb2 parity: its settings has a "Сбросить статистику" + "Full Disk Access ›"
    // card (~69). "Сбросить статистику" only writes app-owned baseline markers (the
    // agent still owns state.json — D13); the confirm alert guards it. "Full Disk
    // Access ›" opens the exact System Settings privacy pane so the background agent
    // can be granted access to Desktop/Documents/Downloads folders.

    private var dataAndAccessSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            cap("ДАННЫЕ И ДОСТУП")

            // Сбросить статистику — danger row. Left: title + a one-line explainer /
            // ack. Right: a small danger-tinted button that raises the confirm alert.
            HStack(spacing: 11) {
                ZStack {
                    RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                        .fill(Tokens.C.dangerTint10)
                    Image(systemName: "arrow.counterclockwise")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(Tokens.C.dangerBase)
                }
                .frame(width: 28, height: 28)
                VStack(alignment: .leading, spacing: 1) {
                    Text("Сбросить статистику")
                        .font(.system(size: Tokens.F.body))
                        .foregroundColor(Tokens.C.textHigh)
                    Text(didReset ? "Счётчики обнулены"
                                  : "Обнулить счётчики и список")
                        .font(.system(size: Tokens.F.small))
                        .foregroundColor(didReset ? Tokens.C.stepOkSub : Tokens.C.textTertiary)
                }
                Spacer(minLength: 8)
                Button(action: { didReset = false; showResetConfirm = true }) {
                    Text("Сбросить")
                        .font(.system(size: Tokens.F.caption, weight: .semibold))
                        .foregroundColor(Tokens.C.dangerText)
                        .padding(.horizontal, 13)
                        .padding(.vertical, 7)
                        .background(
                            RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                                .fill(Tokens.C.dangerTint10)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                                .stroke(Tokens.C.dangerBorder30, lineWidth: 1)
                        )
                }
                .buttonStyle(.plain)
                .contentShape(Rectangle())
            }

            SettingsHairline(color: Tokens.C.borderHairline)

            // Full Disk Access › — opens the System Settings privacy pane (the exact
            // "Full Disk Access" list). A chevron marks it as an outward jump.
            Button(action: openFullDiskAccess) {
                HStack(spacing: 11) {
                    ZStack {
                        RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                            .fill(Tokens.C.rowIcBrandTealBg)
                        Image(systemName: "lock.shield")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundColor(Tokens.C.brandTeal)
                    }
                    .frame(width: 28, height: 28)
                    VStack(alignment: .leading, spacing: 1) {
                        Text("Full Disk Access")
                            .font(.system(size: Tokens.F.body))
                            .foregroundColor(Tokens.C.textHigh)
                        Text("Доступ фонового агента к папкам")
                            .font(.system(size: Tokens.F.small))
                            .foregroundColor(Tokens.C.textTertiary)
                    }
                    Spacer(minLength: 8)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundColor(Tokens.C.textTertiary)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
        .padding(.init(top: 14, leading: 14, bottom: 14, trailing: 14))
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCard)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderCard, lineWidth: 1)
        )
    }

    // MARK: Version — "Версия X.Y.Z" + "Открыть на GitHub". fb2 parity: its settings
    // has a version card + a button (fb2's is "Проверить обновление" via its
    // UpdateChecker; WE have none, so the button is a plain "Открыть на GitHub" link
    // — no auto-update machinery is invented).

    private var versionSection: some View {
        HStack(spacing: 11) {
            ZStack {
                RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                    .fill(Tokens.C.surfaceControlSoft)
                Image(systemName: "info.circle")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(Tokens.C.textSecondary)
            }
            .frame(width: 28, height: 28)
            VStack(alignment: .leading, spacing: 1) {
                Text("Версия \(Tokens.appVersion)")
                    .font(.system(size: Tokens.F.body))
                    .foregroundColor(Tokens.C.textHigh)
                Text("mp3-to-m4b · by Alex Kovalev")
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textTertiary)
            }
            Spacer(minLength: 8)
            Button(action: onOpenGitHub) {
                HStack(spacing: 6) {
                    Image(systemName: "arrow.up.right.square")
                        .font(.system(size: 12, weight: .semibold))
                    Text("Открыть на GitHub")
                        .font(.system(size: Tokens.F.caption, weight: .semibold))
                }
                .foregroundColor(Tokens.C.linkBlue)
                .padding(.horizontal, 13)
                .padding(.vertical, 7)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                        .fill(Tokens.C.surfaceControl)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                        .stroke(Tokens.C.borderControl, lineWidth: 1)
                )
            }
            .buttonStyle(.plain)
            .contentShape(Rectangle())
        }
        .padding(.init(top: 14, leading: 14, bottom: 14, trailing: 14))
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCard)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderCard, lineWidth: 1)
        )
    }

    // MARK: - Actions

    /// Probe ffmpeg off the main thread; update on completion (parity with Setup).
    private func runProbe() {
        probing = true
        DispatchQueue.global(qos: .userInitiated).async {
            let result = FFmpegProbe.detect()
            DispatchQueue.main.async {
                self.probe = result
                self.probing = false
            }
        }
    }

    /// NSOpenPanel to choose a NEW watched folder (directories only). Cancelling
    /// changes NOTHING (safety). A pick resets any prior done/failed phase so the
    /// button reads for the fresh selection.
    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.title = "Выберите отслеживаемую папку"
        panel.prompt = "Выбрать"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = URL(fileURLWithPath: selectedDir).deletingLastPathComponent()
        guard panel.runModal() == .OK, let url = panel.url else { return }  // cancel → no change
        selectedDir = url.path
        selectedDirExists = SetupView.directoryExists(at: selectedDir)
        phase = .idle   // a new selection clears a prior success/error banner
    }

    /// Create the selected folder (making intermediate dirs), then re-check. Shown
    /// only when it's missing. Errors surface via NSAlert (rare: permission/FDA);
    /// state stays honest either way (same as Setup's createWatchFolder).
    private func createSelectedFolder() {
        do {
            try FileManager.default.createDirectory(
                atPath: selectedDir, withIntermediateDirectories: true, attributes: nil)
        } catch {
            let alert = NSAlert()
            alert.messageText = "Не удалось создать папку"
            alert.informativeText = error.localizedDescription
            alert.alertStyle = .warning
            alert.runModal()
        }
        selectedDirExists = SetupView.directoryExists(at: selectedDir)
    }

    /// Re-point the agent: run the bundled installer with the chosen folder, off the
    /// main thread (the SAME InstallRunner Setup uses). On success flip the shown
    /// "current" folder to the chosen one; on failure keep it (agent stays on old).
    private func startRepoint() {
        guard canApply else { return }
        guard let installer = InstallRunner.bundledInstallerPath() else {
            phase = .failed("Установщик не найден в приложении (пересоберите .app).")
            return
        }
        let dir = selectedDir
        let env = installerExtraEnv
        phase = .running
        DispatchQueue.global(qos: .userInitiated).async {
            let result = InstallRunner.run(installerPath: installer, watchDir: dir,
                                           extraEnv: env)
            DispatchQueue.main.async {
                self.phase = result
                if case .done = result {
                    // SUCCESS: the agent is now on `dir`. Reflect that as the current
                    // folder (the display can never claim an un-applied folder).
                    self.appliedDir = dir
                    self.selectedDirExists = SetupView.directoryExists(at: dir)
                }
                // On .failed we intentionally leave appliedDir untouched — the agent
                // is still on the OLD folder, and the red stderr line says why.
            }
        }
    }

    /// Open the System Settings "Full Disk Access" privacy pane (fb2 parity, brief).
    /// The `Privacy_AllFiles` anchor lands directly on the Full Disk Access list on
    /// modern macOS; older systems open the Security & Privacy pane. Best-effort.
    private func openFullDiskAccess() {
        let urls = [
            "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
            "x-apple.systempreferences:com.apple.preference.security?Privacy",
        ]
        for s in urls {
            if let url = URL(string: s), NSWorkspace.shared.open(url) { return }
        }
    }

    // MARK: - Small helpers

    private func cap(_ text: String) -> some View {
        Text(text)
            .font(.system(size: Tokens.F.cap, weight: .bold))
            .tracking(1.2)
            .foregroundColor(Tokens.C.textTertiary)
    }

    /// Tilde-collapse the home prefix for display (matches Setup/Status paths).
    private func tildeAbbrev(_ path: String) -> String {
        let home = NSHomeDirectory()
        if path == home { return "~" }
        if !home.isEmpty, path.hasPrefix(home + "/") {
            return "~/" + String(path.dropFirst(home.count + 1))
        }
        return path
    }

    /// Normalize a path for equality (strip a single trailing slash). Cheap, enough
    /// to catch the common "same folder / trailing slash" no-op case.
    private func normalize(_ path: String) -> String {
        var p = path
        while p.count > 1 && p.hasSuffix("/") { p.removeLast() }
        return p
    }
}

/// A 1px full-width rule (main.swift's `Hairline` is used elsewhere; a tiny local
/// wrapper keeps this screen self-contained and matches Setup's SetupHairline).
private struct SettingsHairline: View {
    let color: Color
    var body: some View {
        Rectangle().fill(color).frame(height: 1)
    }
}

// MARK: - Agent auto-update screen (brief "Обновляю фоновый агент…" during re-install)

/// The short overlay shown while the app SELF-UPDATES a stale staged agent at launch
/// (AgentUpdate.freshness == .outdated → re-run the bundled installer with the CURRENT
/// watch folder). 400 wide (spec §2 standard). It is NOT a full Setup: no ffmpeg
/// re-probe, no folder picker — just an honest progress/failure card, because the
/// user already installed once and we're only refreshing the engine in place (FDA and
/// the watch folder are preserved — the runner.sh path never changes).
///
/// States mirror Setup's install phase: `.running` → spinner + "Обновляю фоновый
/// агент…"; `.failed(msg)` → the installer's honest stderr + a "Повторить" button.
/// `.idle`/`.done` are transient here (the AppDelegate flips off this screen on
/// success), so they render as the running state to avoid a flash of empty content.
private struct AgentUpdatingView: View {
    let phase: InstallPhase
    let onRetry: () -> Void

    var body: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 24)
            card
            Spacer(minLength: 24)
        }
        .frame(width: Tokens.M.windowStandard)   // 400 (spec §2)
        .frame(minHeight: 240)
    }

    private var card: some View {
        VStack(spacing: 14) {
            // App-glyph tile (brand teal) — the same family as Setup's app icon.
            ZStack {
                RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                    .fill(Tokens.C.rowIcBrandTealBg)
                Image(systemName: "arrow.triangle.2.circlepath")
                    .font(.system(size: 22, weight: .semibold))
                    .foregroundColor(Tokens.C.brandTeal)
            }
            .frame(width: 48, height: 48)

            Text("Обновление фонового агента")
                .font(.system(size: Tokens.F.h1Confirm, weight: .bold))
                .foregroundColor(Tokens.C.textHigh)
                .multilineTextAlignment(.center)

            body(for: phase)
        }
        .padding(.init(top: 22, leading: 20, bottom: 22, trailing: 20))
        .frame(maxWidth: .infinity)
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCard)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderCard, lineWidth: 1)
        )
        .padding(.horizontal, 16)
    }

    @ViewBuilder
    private func body(for phase: InstallPhase) -> some View {
        switch phase {
        case .failed(let msg):
            VStack(spacing: 12) {
                Text(msg)
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.dangerText)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                Button(action: onRetry) {
                    HStack(spacing: 6) {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 12, weight: .bold))
                        Text("Повторить")
                            .font(.system(size: Tokens.F.caption, weight: .semibold))
                    }
                    .foregroundColor(Tokens.C.textOnAccent)
                    .padding(.init(top: 8, leading: 15, bottom: 8, trailing: 15))
                    .background(
                        RoundedRectangle(cornerRadius: Tokens.R.appIconConfirm, style: .continuous)
                            .fill(Tokens.Grad.brandButton)
                    )
                }
                .buttonStyle(.plain)
                .contentShape(Rectangle())
            }
        default:
            // .running (and the transient .idle/.done): spinner + reassurance.
            VStack(spacing: 10) {
                HStack(spacing: 8) {
                    ProgressView()
                        .controlSize(.small)
                        .progressViewStyle(.circular)
                    Text("Обновляю фоновый агент…")
                        .font(.system(size: Tokens.F.body, weight: .semibold))
                        .foregroundColor(Tokens.C.textSoft)
                }
                Text("Приложение обновилось — переустанавливаю движок. Папка и доступ сохранятся.")
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textSecondary)
                    .multilineTextAlignment(.center)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

// MARK: - Grouping sheet (S4 / spec §6, pixel-mapped to mockups/06)

/// Modal scrim + the 440-wide grouping sheet, centered over the window. The scrim
/// dims the content underneath (spec §6: the sheet "перекрывает Status"). Clicking
/// the scrim does nothing — the decision is explicit (Отмена / Продолжить).
private struct GroupingSheetOverlay: View {
    let group: PendingGroup
    let onGroupingChoice: (PendingGroup, EngineClient.GroupingChoice) -> Bool

    var body: some View {
        ZStack {
            Color.black.opacity(0.55)         // modal scrim over the window
                .ignoresSafeArea()
            GroupingSheet(group: group, onGroupingChoice: onGroupingChoice)
                .frame(width: Tokens.M.windowSheet)   // 440 (spec §6)
                .padding(.vertical, 24)
        }
    }
}

/// The grouping dialog itself, mapped 1:1 to design/mockups/06-grouping-and-split
/// (CSS = pixel truth): a centered head (sheet-icon 48 · h2 18/-0.3 · sub), a
/// file-strip of name chips, two `choice` rows (radius 12; selected → accentBorder60
/// + accentTint10 + choiceInset, radio 20 teal-filled), and an actions row
/// (Отмена ghost / Продолжить primary). Default selection = "combine" (D1 / flows
/// §2.1 — the common flat-collection case).
private struct GroupingSheet: View {
    let group: PendingGroup
    let onGroupingChoice: (PendingGroup, EngineClient.GroupingChoice) -> Bool

    /// Default-highlighted choice = combine (the frequent flat-collection case).
    @State private var choice: EngineClient.GroupingChoice = .combine
    @State private var sent = false
    @State private var failed = false

    var body: some View {
        VStack(spacing: 0) {
            head
            fileStrip
            choices
            actions
        }
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.window, style: .continuous)
                .fill(Tokens.Canvas.sheetGradient)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.window, style: .continuous)
                .stroke(Tokens.C.borderFieldInput, lineWidth: 1)   // rgba(255,255,255,.08)
        )
        .clipShape(RoundedRectangle(cornerRadius: Tokens.R.window, style: .continuous))
        // shadow.sheet: 0 28px 70px -22px rgba(0,0,0,.85)
        .shadow(color: Color.black.opacity(0.85), radius: 35, x: 0, y: 28)
    }

    // sheet-head: padding 22 24 6, centered.
    private var head: some View {
        VStack(spacing: 0) {
            // sheet-icon 48 · radius 13 · accentTint12 bg · accentBorder30 contour.
            ZStack {
                RoundedRectangle(cornerRadius: Tokens.R.sheetIcon, style: .continuous)
                    .fill(Tokens.C.accentTint12)
                RoundedRectangle(cornerRadius: Tokens.R.sheetIcon, style: .continuous)
                    .stroke(Tokens.C.accentBorder30, lineWidth: 1)
                // Audiobook glyph (book + play), brand teal — matches the logo family.
                Image(systemName: "books.vertical.fill")
                    .font(.system(size: 22, weight: .regular))
                    .foregroundColor(Tokens.C.brandTeal)
            }
            .frame(width: Tokens.M.sheetIcon, height: Tokens.M.sheetIcon)
            .padding(.bottom, 14)

            Text("Найдены отдельные файлы")
                .font(.system(size: 18, weight: .bold))
                .tracking(-0.3)
                .foregroundColor(Tokens.C.textHigh)

            Text("\(group.count) \(Plural.files(group.count)) \(Plural.lieVerb(group.count)) прямо в корне папки.\nСобрать их в одну книгу или по отдельности?")
                .font(.system(size: 12.5))
                .foregroundColor(Tokens.C.textSecondary)
                .multilineTextAlignment(.center)
                .lineSpacing(3)                 // line-height 1.45 ≈ +3pt
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 6)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 22)
        .padding(.horizontal, 24)
        .padding(.bottom, 6)
    }

    // file-strip: margin 14 24 0, padding 10 12, radius 10, bgInput, .06 border,
    // wrapping chips. Up to 8 names then a "+N" more (file-more).
    private var fileStrip: some View {
        FileChipsFlow(names: group.files, maxChips: 8, spacing: 6)
            .padding(.horizontal, 12)
            .padding(.vertical, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.estimate, style: .continuous)
                    .fill(Tokens.C.bgInput)                        // #0a1018
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.estimate, style: .continuous)
                    .stroke(Tokens.C.borderCard, lineWidth: 1)     // rgba(255,255,255,.06)
            )
            .padding(.horizontal, 24)
            .padding(.top, 14)
    }

    // choices: padding 16 24 6, gap 10.
    private var choices: some View {
        VStack(spacing: 10) {
            choiceRow(
                kind: .combine,
                icon: "arrow.triangle.merge",
                title: "Объединить в одну книгу",
                sub: "\(group.count) \(Plural.chapters(group.count)) → один .m4b"
            )
            choiceRow(
                kind: .separate,
                icon: "square.split.2x1",
                title: "Собрать по отдельности",
                sub: "\(group.count) \(Plural.separateBooks(group.count)) по одному файлу"
            )
        }
        .padding(.horizontal, 24)
        .padding(.top, 16)
        .padding(.bottom, 6)
    }

    // One choice row (radius 12). Selected → accentBorder60 + accentTint10 +
    // choiceInset; choice-ic 42 (accentTint12 / .20 sel); radio 20 (teal fill sel).
    private func choiceRow(kind: EngineClient.GroupingChoice, icon: String,
                           title: String, sub: String) -> some View {
        let selected = (choice == kind)
        return Button(action: { if !sent { choice = kind; failed = false } }) {
            HStack(spacing: 14) {
                ZStack {
                    RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                        .fill(selected ? Tokens.C.accentTint20 : Tokens.C.accentTint12)
                    Image(systemName: icon)
                        .font(.system(size: 18, weight: .semibold))
                        .foregroundColor(Tokens.C.brandTeal)
                }
                .frame(width: Tokens.M.choiceIc, height: Tokens.M.choiceIc)

                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(.system(size: 14, weight: .bold))
                        .foregroundColor(Tokens.C.textHigh)
                    Text(sub)
                        .font(.system(size: 12))
                        .foregroundColor(Tokens.C.textSecondary)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                radio(selected: selected)
            }
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                    .fill(selected ? Tokens.C.accentTint10 : Tokens.C.surfaceChoice)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                    .stroke(selected ? Tokens.C.accentBorder60 : Tokens.C.borderFieldInput,
                            lineWidth: 1)
            )
            // choiceInset: an extra inner 1px accentBorder30 ring on the selected row.
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                    .inset(by: 1)
                    .stroke(selected ? Tokens.C.accentBorder30 : Color.clear, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .contentShape(RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous))
    }

    // radio 20: 2px ring (white .2 → teal sel) with a 3pt-inset teal dot when sel.
    private func radio(selected: Bool) -> some View {
        ZStack {
            Circle()
                .stroke(selected ? Tokens.C.brandTeal : Color.white(0.2), lineWidth: 2)
            if selected {
                Circle()
                    .fill(Tokens.C.brandTeal)
                    .padding(3)
            }
        }
        .frame(width: Tokens.M.choiceRadio, height: Tokens.M.choiceRadio)
    }

    // sheet-actions: padding 14 24 20, gap 10. Отмена ghost (fixed) + Продолжить
    // primary (fills). After a successful drop we show a muted ack in place.
    private var actions: some View {
        HStack(spacing: 10) {
            Button(action: { /* Отмена: send nothing — the group stays pending */ }) {
                Text("Отмена")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(Tokens.C.textSoft)
                    .padding(.horizontal, 18)
                    .padding(.vertical, 11)
                    .background(
                        RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                            .fill(Tokens.C.surfaceControl)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                            .stroke(Tokens.C.borderControlStrong, lineWidth: 1)  // .12
                    )
            }
            .buttonStyle(.plain)
            .contentShape(Rectangle())
            .disabled(sent)

            if sent {
                Text("Отправлено…")
                    .font(.system(size: 14, weight: .bold))
                    .foregroundColor(Tokens.C.textSecondary)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 11)
                    .background(
                        RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                            .fill(Tokens.C.surfaceControl)
                    )
            } else {
                continueButton
            }
        }
        .padding(.horizontal, 24)
        .padding(.top, 14)
        .padding(.bottom, 20)
    }

    private var continueButton: some View {
        Button(action: {
            failed = false
            if onGroupingChoice(group, choice) {
                sent = true
            } else {
                failed = true
            }
        }) {
            HStack(spacing: 7) {
                Text(failed ? "Повторить" : "Продолжить")
                    .font(.system(size: 14, weight: .bold))
                Image(systemName: "arrow.right")
                    .font(.system(size: 12, weight: .bold))
            }
            .foregroundColor(Tokens.C.textOnAccent)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 11)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .fill(Tokens.Grad.brandButton)
            )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
    }
}

// MARK: - Confirm window (full layout, spec §3)

/// The pending-confirm book, pixel-mapped to design/spec.md §3 and
/// design/mockups/03-confirm-core.html (CSS = pixel truth): a 640-wide, two-column
/// window — left "what" (book fields + chapter list), right "how" (cover · quality
/// box · split toggle · estimate) — over a footer of actions. Window height tracks
/// content and is capped to the screen by the AppDelegate. Layout is a FIXED header
/// + a SINGLE vertical ScrollView wrapping BOTH columns + a PINNED footer, so on a
/// long book (56 chapters) on a short screen the body scrolls and the «Собрать»
/// action stays reachable (see `body`).
private struct ConfirmView: View {
    let book: BookSummary
    let manifest: BookManifest
    let pendingCount: Int
    /// 1-based position of THIS book among the active ones — the "N из M" counter.
    /// Passed in (not assumed to be 1) because the queue can open any book directly.
    let position: Int
    /// Books still AWAITING CONFIRMATION — the audience of "Применить параметры ко
    /// всем (N)" (US-3.7: «ко всем ожидающим»). Distinct from `pendingCount`, which
    /// also counts converting/error books whose params can no longer be changed.
    let pendingConfirmCount: Int
    /// Total books in the showcase (any status) — drives the "В очередь" entry count.
    let queueCount: Int
    /// Session preset from a previous "Применить параметры ко всем" — seeds the
    /// quality/split controls when it covers THIS book (see `ParamsPreset`).
    let paramsPreset: ParamsPreset?
    /// Writes the command with the edited params + cover pick (cover_id for an
    /// agent-known option, or cover_custom_path for a user file); returns true on
    /// success.
    let onBuild: (BookParams, _ coverID: String?, _ coverCustomPath: String?) -> Bool
    /// "Применить параметры ко всем (N)" — hands the current params to the model,
    /// which remembers them for the books awaiting confirmation. App-side only: no
    /// command is written and no book is built (each still needs its own "Собрать").
    let onApplyToAll: (BookParams) -> Void
    /// Navigate to the full queue (footer "Позже в очередь" — the book stays pending,
    /// the app changes no status).
    let onOpenQueue: () -> Void
    /// Cancel the in-flight build (converting footer "Отменить конвертацию", D13).
    /// Drops the SAME `cancel` command the queue uses; returns true on a successful
    /// drop so the button can show its sent ack. The agent unwinds ffmpeg and the
    /// file-watch returns this window to the confirm step.
    let onCancel: () -> Bool
    /// «Пропустить» (footer `btn-skip`): drops a `skip` command for THIS book.
    /// Returns true on a successful drop so the button can show its ack; the agent
    /// marks the book `skipped` (sources untouched) and the file-watch retires this
    /// window. Undo: «Вернуть» in the queue's ПРОПУЩЕНО section, or a re-drop.
    let onSkip: () -> Bool

    // Editable book fields, seeded from the manifest. (The title field also drives
    // the disabled-state validation: an empty title blocks "Собрать", spec §3.)
    @State private var title: String
    @State private var author: String

    // Editable build params, seeded from the manifest defaults (D2). User edits
    // flow into the confirm-build command verbatim.
    @State private var bitrate: Int
    @State private var channels: String   // "stereo" | "mono"
    /// nil = "as in source" (no resample); a concrete value (44100/48000) = the
    /// user's explicit override. Seeds from the manifest's `samplerate` (nil default).
    @State private var samplerate: Int?
    @State private var split: Bool
    /// Part-size threshold in MB (slider 250…700, default 300 — spec §6 / D6).
    @State private var splitThresholdMB: Double
    /// Build mode (D15): "fast" (default) | "seamless". The КАЧЕСТВО segment writes
    /// it; it rides to the engine in confirm-build's params (build_m4b branches on it).
    @State private var buildMode: String

    // Cover pick — LOCAL window state (spec/protocol: the choice rides in
    // confirm-build's params, not a separate command). `coverSelectedID` is the id
    // of the chosen option (seeded from manifest.coverSelected). A user "Заменить"
    // appends a synthetic `custom` option to `extraCoverOptions` (the original file
    // path) and selects it; the agent copies that file under covers/ on build.
    @State private var coverSelectedID: String?
    @State private var extraCoverOptions: [CoverOption] = []

    /// App-side idempotency: once a command is dropped for this book, lock the
    /// button — the agent flips the status and the rising-edge watcher clears the
    /// whole view. `failed` re-enables + surfaces an error if the write threw.
    @State private var sent = false
    @State private var failed = false
    /// Converting footer: once "Отменить конвертацию" drops its command, lock the
    /// button to its ack ("Отмена отправлена…") — the agent unwinds the build and the
    /// file-watch swaps this window back to confirm, retiring the whole view.
    @State private var cancelSent = false
    /// Same lock for «Пропустить»: once the `skip` command is on disk the button
    /// shows its ack until the agent's status flip retires the window.
    @State private var skipSent = false

    init(book: BookSummary, manifest: BookManifest, pendingCount: Int,
         position: Int, pendingConfirmCount: Int, queueCount: Int,
         paramsPreset: ParamsPreset?,
         onBuild: @escaping (BookParams, String?, String?) -> Bool,
         onApplyToAll: @escaping (BookParams) -> Void,
         onOpenQueue: @escaping () -> Void,
         onCancel: @escaping () -> Bool,
         onSkip: @escaping () -> Bool) {
        self.book = book
        self.manifest = manifest
        self.pendingCount = pendingCount
        self.position = position
        self.pendingConfirmCount = pendingConfirmCount
        self.queueCount = queueCount
        self.paramsPreset = paramsPreset
        self.onBuild = onBuild
        self.onApplyToAll = onApplyToAll
        self.onOpenQueue = onOpenQueue
        self.onCancel = onCancel
        self.onSkip = onSkip
        // Prefer the manifest's resolved title/author; fall back to the showcase
        // title (which the agent also fills) so the field is never blank-by-bug.
        _title = State(initialValue: manifest.title.isEmpty ? book.title : manifest.title)
        _author = State(initialValue: manifest.author)
        // Build params come from the "ко всем" preset when it covers this book,
        // otherwise from the manifest defaults (D2). Title/author/cover are NEVER
        // preset — they are per-book by definition (US-3.7 AC).
        let seed = paramsPreset.flatMap { $0.applies(to: manifest.bookID) ? $0.params : nil }
            ?? manifest.params
        _bitrate = State(initialValue: seed.bitrate)
        _channels = State(initialValue: seed.channels)
        _samplerate = State(initialValue: seed.samplerate)
        _split = State(initialValue: seed.split)
        // Seed the threshold from the manifest, clamped into the slider's 250…700 МБ
        // range so a stray param can't push the knob off-track (default 300, D6).
        _splitThresholdMB = State(initialValue:
            Double(min(700, max(250, seed.splitThresholdMB))))
        _buildMode = State(initialValue: seed.buildMode)
        // Seed the cover pick from the agent's default (cover_selected); fall back to
        // the first option so something is always selected when options exist.
        _coverSelectedID = State(initialValue:
            manifest.coverSelected ?? manifest.coverOptions.first?.optID)
    }

    /// Bitrate presets (kbps) offered in the quality box (spec §3, D2 default 192).
    private static let bitratePresets = [64, 96, 128, 192, 256]

    /// The edited params as they'll be sent to the agent (keys 1:1 with the manifest).
    private var editedParams: BookParams {
        BookParams(bitrate: bitrate, channels: channels,
                   samplerate: samplerate, split: split,
                   splitThresholdMB: Int(splitThresholdMB.rounded()),
                   buildMode: buildMode)
    }

    // MARK: Cover-pick derivation

    /// All cover options the picker shows: the agent's resolved list (embedded → web
    /// → generated) followed by any in-session `custom` options the user added via
    /// "Заменить". Order matters — the agent's defaults come first, the user's pick last.
    private var allCoverOptions: [CoverOption] {
        manifest.coverOptions + extraCoverOptions
    }

    /// The currently-selected option (by id), if any. Drives the big preview/label.
    private var selectedCoverOption: CoverOption? {
        allCoverOptions.first { $0.optID == coverSelectedID }
    }

    /// The `cover_id` to send in confirm-build: the selected option's id, but ONLY
    /// when it is one the agent already knows (embedded/web/generated). For a custom
    /// pick we send `cover_custom_path` instead (below) and omit cover_id, so the
    /// agent copies the file rather than trying to resolve a client-only id.
    private var sendCoverID: String? {
        guard let opt = selectedCoverOption, opt.kind != "custom" else { return nil }
        return opt.optID
    }

    /// The original file path to send as `cover_custom_path` when the user picked
    /// their own file ("Заменить"); nil otherwise. The agent copies it under covers/.
    private var sendCoverCustomPath: String? {
        guard let opt = selectedCoverOption, opt.kind == "custom" else { return nil }
        return opt.path
    }

    private var titleIsEmpty: Bool {
        title.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    /// Which visual state the window is in, derived from the agent-owned status.
    /// The app never sets these — it mirrors the manifest the agent wrote.
    private enum Mode { case confirm, converting, error }
    private var mode: Mode {
        if manifest.isConverting { return .converting }
        if manifest.isError { return .error }
        return .confirm
    }

    var body: some View {
        VStack(spacing: 0) {
            // FIXED header (stays put at every book length / screen height).
            header
            Hairline(color: Tokens.C.borderHairline)
            // MIDDLE area — the flexible band between the fixed header and pinned
            // footer. There is NO outer scroll around it any more: each mode owns
            // its OWN internal scrolling so the surrounding chrome never moves.
            //  · confirm  → body2col: two columns side-by-side. The RIGHT column
            //    (all settings) shows in FULL at its natural height and DRIVES the
            //    body height; the LEFT (chapters) fills that same height and scrolls
            //    the chapter list INSIDE its card. Reading a 56-chapter book moves
            //    only the list — the right column stays put and fully visible.
            //  · converting/error → their own bodies, capped so a tall banner/
            //    progress scrolls instead of pushing the footer off-screen.
            // The body's variable height is bounded by `bodyAreaCap` (screen-derived)
            // and the AppDelegate window-cap (cappedContentHeight/refitWindowHeight)
            // is the outer belt — so the whole window is always ≤ the screen and the
            // «Собрать» footer is ALWAYS reachable. The old single outer ScrollView
            // scrolled the WHOLE body (the reported regression: right column uphill).
            switch mode {
            case .confirm:
                body2col
            case .converting:
                convertingBody
            case .error:
                errorBody
            }
            Hairline(color: Tokens.C.borderCard)
            // PINNED footer (action bar: Пропустить / Позже в очередь / Собрать) —
            // OUTSIDE any scroll, so «Собрать» is ALWAYS visible.
            footer
        }
    }

    /// Screen-derived height budget for the MIDDLE area (the band between the fixed
    /// header and the pinned footer) — i.e. the most the two-column body may be tall
    /// before the window would exceed the screen. Deterministic per session — read
    /// once off `NSScreen.visibleFrame` exactly like the AppDelegate's
    /// `cappedContentHeight`; NOT GeometryReader content self-measurement (no
    /// feedback loop — see .patches/002), it is a fixed budget by construction.
    ///
    /// The RIGHT column (all settings) is the primary content: it must be visible in
    /// FULL, so it shows at its NATURAL height and DRIVES the body/window height. This
    /// budget is only a SAFEGUARD ceiling for it — the reserve (~150) covers just the
    /// window chrome (titlebar + margin ≈ 36) + the fixed header (~66) + the pinned
    /// footer (~80). On a normal screen the right column is far shorter than this, so
    /// the ceiling never bites and every control shows; only a genuinely oversized
    /// right column (tiny screen / everything expanded) scrolls inside its own
    /// safeguard. The LEFT chapter list fills WHATEVER height the right column sets
    /// (it does not use this budget directly). The AppDelegate window-cap is the
    /// outer belt. `max(320, …)` keeps a usable area on a short screen.
    private var bodyAreaCap: CGFloat {
        let visible = NSScreen.main?.visibleFrame.height ?? 900
        return max(320, visible - 150)
    }

    // MARK: Header — padding 16 20 14, app-icon 34 (r9), h1 16/700 + sub 11, counter.
    // Title/sub track the state (spec §3 / mockup 05): "Подтверждение книги" /
    // "Сборка книги" (converting) / "Сборка прервана" (hard error).

    private var headerTitle: String {
        switch mode {
        case .confirm: return "Подтверждение книги"
        case .converting: return "Сборка книги"
        case .error: return errorIsRecoverable ? "Подтверждение книги" : "Сборка прервана"
        }
    }

    private var headerSub: String {
        switch mode {
        case .confirm:
            return "Проверьте качество и обложку — сборка стартует только по «Собрать»"
        case .converting, .error:
            // Identify the book by its resolved title (мокап: «Война и мир»).
            return title.isEmpty ? book.title : title
        }
    }

    private var header: some View {
        HStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: Tokens.R.appIconConfirm, style: .continuous)
                    .fill(Tokens.Canvas.appIconGradient)
                Image(systemName: "books.vertical.fill")
                    .font(.system(size: 16, weight: .semibold))
                    .foregroundColor(Tokens.C.brandCyan)
            }
            .frame(width: 34, height: 34)
            .shadow(color: Tokens.C.brandTeal.opacity(0.45), radius: 7, x: 0, y: 5)

            VStack(alignment: .leading, spacing: 1) {
                Text(headerTitle)
                    .font(.system(size: Tokens.F.h1Confirm, weight: .bold))
                    .tracking(-0.2)
                    .foregroundColor(Tokens.C.textHigh)
                Text(headerSub)
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textSecondary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
            }

            Spacer(minLength: 8)

            // "N из M" counter pill (q-counter). Shown whenever there is a queue.
            // N is the PRESENTED book's position — opening the 2nd book from the
            // queue reads "2 из 2" (it used to be hardcoded "1", which quietly
            // claimed the first book was on screen).
            if pendingCount >= 1 {
                HStack(spacing: 0) {
                    Text("\(position)")
                        .font(.system(size: Tokens.F.caption, weight: .bold).monospacedDigit())
                        .foregroundColor(Tokens.C.textHigh)
                    Text(" из \(pendingCount)").font(.system(size: Tokens.F.caption).monospacedDigit())
                        .foregroundColor(Tokens.C.textMuted)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                        .fill(Tokens.C.surfaceControlSoft)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                        .stroke(Tokens.C.borderFieldInput, lineWidth: 1)
                )
            }
        }
        .padding(.top, 16)
        .padding(.horizontal, 20)
        .padding(.bottom, 14)
    }

    // MARK: Body — grid 1fr / 280, gap 0, divided by a vertical hairline.

    private var body2col: some View {
        HStack(alignment: .top, spacing: 0) {
            leftColumn
            Rectangle()
                .fill(Tokens.C.borderHairline)
                .frame(width: 1)
            rightColumn
                .frame(width: Tokens.M.windowRightColumn)
        }
        // No outer ScrollView any more (removed from `body`). `HStack(alignment:.top)`:
        // the RIGHT column is NATURAL height and shows in FULL — it DRIVES the HStack's
        // height. The LEFT column's chapter list fills that height (`.frame(maxHeight:
        // .infinity)`, no definite height, no GeometryReader — see .patches/002) and
        // scrolls the chapters INSIDE its card. So reading the chapter list moves ONLY
        // the list; the right column (cover/quality/mode/split/size) stays put and fully
        // visible. The right column self-caps at `bodyAreaCap` as a safeguard only (does
        // not bite on a normal screen), so the window stays ≤ the screen (belt = outer).
    }

    // MARK: Converting body (status == converting) — spec §3 / mockup 05.
    // Two columns: left = live progress block, right = cover. Fields are gone
    // (locked during build). Progress is now REAL + DETERMINATE: the agent streams
    // ffmpeg's position into `book.progress` (the BuildProgress contract), so we
    // render a determinate bar + "Глава X из Y", "Прошло", "Осталось ≈" instead of
    // the old fabricated indeterminate pulse. Until the first snapshot lands
    // (`book.progress == nil`) we show an honest "Запуск…" — no deceptive bar.
    private var convertingBody: some View {
        // Own bounded scroll (the outer body scroll is gone): on a very short screen
        // this body scrolls internally instead of pushing the converting footer
        // (Отменить конвертацию) off the bottom. Normally it's short and shows in full.
        ScrollView(.vertical, showsIndicators: false) {
            HStack(alignment: .top, spacing: 18) {
                ConvertingBlock(progress: book.progress,
                                bitrate: manifest.params.bitrate,
                                channels: manifest.params.channels)
                    .frame(maxWidth: .infinity, alignment: .leading)
                coverColumnCompact
            }
            .padding(.init(top: 18, leading: 20, bottom: 18, trailing: 20))
        }
        .frame(maxHeight: bodyAreaCap)
    }

    // MARK: Error body (status == error) — spec §3 / mockup 05.
    // A banner (danger for hard reasons, warn for recoverable) over the locked
    // book fields + cover. The banner text is mapped from the agent's real
    // `error.reason`; we do NOT invent specifics the agent didn't record (which
    // file / how many GB) — see errorBanner.
    private var errorBody: some View {
        // Own bounded scroll (the outer body scroll is gone): a tall error banner
        // scrolls internally instead of clipping the footer note. Normally short.
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 16) {
                errorBanner
                HStack(alignment: .top, spacing: 18) {
                    VStack(alignment: .leading, spacing: 0) {
                        lockedField(label: "Автор / чтец", value: author.isEmpty ? "—" : author,
                                    weight: .medium)
                            .padding(.bottom, 14)
                        lockedField(label: "Название", value: title, weight: .semibold)
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    coverColumnCompact
                }
            }
            .padding(.init(top: 18, leading: 20, bottom: 18, trailing: 20))
        }
        .frame(maxHeight: bodyAreaCap)
    }

    // Compact cover column reused by converting/error (cap «ОБЛОЖКА» + cover, no
    // action buttons — those belong to the editable confirm state only).
    private var coverColumnCompact: some View {
        VStack(alignment: .leading, spacing: 10) {
            coverCap(coverStateSourceBadge)
            CoverBox(coverState: manifest.coverState,
                     coverPreview: manifest.coverPreview,
                     title: title, author: author)
        }
        .frame(width: 200)
    }

    // A read-only field (label + boxed value) for the locked converting/error
    // states — same metrics as the editable `.inp` but non-interactive.
    private func lockedField(label: String, value: String,
                             weight: Font.Weight) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label)
                .font(.system(size: Tokens.F.fieldLabel))
                .foregroundColor(Tokens.C.textSecondary)
            Text(value)
                .font(.system(size: Tokens.F.input, weight: weight))
                .foregroundColor(Tokens.C.textHigh)
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                        .fill(Tokens.C.bgInput)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                        .stroke(Tokens.C.borderControl, lineWidth: 1)
                )
        }
    }

    // MARK: Error classification + banner

    /// Recoverable (warn) vs hard (danger). The only per-chapter-recoverable agent
    /// reason today is an unreadable source chapter ("собрать без неё" UX); every
    /// other failure (missing sources, ffmpeg/timeout/interrupted, unwritable dir)
    /// is a hard danger banner. Mapped from the real `error.reason`.
    private var errorIsRecoverable: Bool {
        guard let r = manifest.error?.reason else { return false }
        return r == "no_usable_chapters" || r == "unreadable_chapter"
    }

    /// Human banner title for a machine `reason`. Generic-but-honest — we never
    /// fabricate the specifics the mockup shows as demo data (a filename, a GB
    /// figure) because the agent does not record them. If a `detail` string is
    /// present we surface it verbatim as the sub-line.
    private var errorBannerTitle: String {
        switch manifest.error?.reason {
        case "no_usable_chapters", "unreadable_chapter":
            return "Некоторые файлы не читаются"
        case "source_missing":
            return "Исходные файлы не найдены"
        case "no_space":
            return "Недостаточно места на диске"
        case "output_dir_unwritable":
            return "Не удалось записать результат"
        case "timeout":
            return "Сборка заняла слишком долго"
        case "interrupted":
            return "Сборка была прервана"
        case "empty_output":
            return "ffmpeg не создал файл"
        case "ffmpeg_missing":
            return "ffmpeg не найден"
        default:
            return "Сборка не удалась"
        }
    }

    private var errorBannerSub: String {
        if let d = manifest.error?.detail, !d.isEmpty { return d }
        return errorIsRecoverable
            ? "Часть файлов не удалось прочитать. Книга осталась в очереди."
            : "Частичный файл удалён, исходники целы."
    }

    private var errorBanner: some View {
        let warn = errorIsRecoverable
        return HStack(alignment: .top, spacing: 12) {
            Image(systemName: warn ? "exclamationmark.triangle"
                                   : "exclamationmark.circle")
                .font(.system(size: 20, weight: .semibold))
                .foregroundColor(warn ? Tokens.C.warnBase : Tokens.C.dangerBase)
            VStack(alignment: .leading, spacing: 2) {
                Text(errorBannerTitle)
                    .font(.system(size: Tokens.F.body, weight: .bold))
                    .foregroundColor(Tokens.C.textHigh)
                Text(errorBannerSub)
                    .font(.system(size: Tokens.F.chDur))
                    .foregroundColor(Tokens.C.textMuted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
        }
        .padding(.init(top: 13, leading: 16, bottom: 13, trailing: 16))
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(warn ? Tokens.C.warnTint10 : Tokens.C.dangerTint10)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(warn ? Tokens.C.warnBorder30 : Tokens.C.dangerBorder30,
                        lineWidth: 1)
        )
    }

    // Left column (what): author/title fields, then ГЛАВЫ caps + total,
    // then the chapter list that stretches to the bottom of the column.
    // Padding 18 18 18 20 (top right bottom left).
    private var leftColumn: some View {
        VStack(alignment: .leading, spacing: 0) {
            field(label: "Автор / чтец", text: $author, weight: .medium)
                .padding(.bottom, 14)
            field(label: "Название", text: $title, weight: .semibold, invalid: titleIsEmpty)
            if titleIsEmpty {
                Text("Укажите название — оно станет именем .m4b")
                    .font(.system(size: Tokens.F.chDur))
                    .foregroundColor(Tokens.C.dangerText)
                    .padding(.top, 5)
            }

            HStack(alignment: .firstTextBaseline) {
                // ГЛАВЫ cap bumped +1 (capLg) vs the shared 9pt cap(), per feedback.
                Text("ГЛАВЫ")
                    .font(.system(size: Tokens.F.capLg, weight: .bold))
                    .tracking(1.2)
                    .foregroundColor(Tokens.C.textTertiary)
                Spacer()
                Text(chapterTotalLabel)
                    .font(.system(size: Tokens.F.small).monospacedDigit())
                    .foregroundColor(Tokens.C.textSecondary)
            }
            .padding(.top, 18)
            .padding(.bottom, 8)

            chapterList
        }
        // Fill the middle area's height (set by the RIGHT column, which shows in full)
        // so the fields sit at the top and `chapterList` (maxHeight: .infinity) expands
        // to fill the space beneath them — D10 «главы на всю высоту окна». No definite
        // height / GeometryReader: the column just takes its sibling's height.
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
        .padding(.init(top: 18, leading: 20, bottom: 18, trailing: 18))
    }

    // A labelled input field (.inp): label 11 secondary + a real editable field
    // (radius 10, bg #0a1018, border .10; invalid → danger border).
    private func field(label: String, text: Binding<String>,
                       weight: Font.Weight, invalid: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(label)
                .font(.system(size: Tokens.F.fieldLabel))
                .foregroundColor(Tokens.C.textSecondary)
            TextField("", text: text)
                .textFieldStyle(.plain)
                .font(.system(size: Tokens.F.input, weight: weight))
                .foregroundColor(Tokens.C.textHigh)
                .padding(.horizontal, 12)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                        .fill(Tokens.C.bgInput)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                        .stroke(invalid ? Tokens.C.dangerBorder55 : Tokens.C.borderControl,
                                lineWidth: 1)
                )
        }
    }

    // Chapter list: rounded card (radius 12, bg #0c121a, border .06) whose ROWS
    // scroll INSIDE the card (D10 «главы на всю высоту окна»). The outer body scroll
    // is gone: now ONLY this list scrolls, so reading a 56-chapter book no longer
    // drags the right column up — it stays put and fully visible.
    //
    // Height: the card FILLS the middle area — `.frame(maxHeight: .infinity)` inside
    // `body2col`'s `HStack(alignment:.top)`, so it stretches to the height the RIGHT
    // column (natural, shown in full) establishes. There is NO definite height and NO
    // GeometryReader self-measurement (see .patches/002 — no feedback loop): the list
    // simply takes the height its sibling column defines. The inner ScrollView absorbs
    // any overflow (56 chapters scroll; a short book leaves the card taller than its
    // rows, which is the intended "chapters on the full window height" look). A
    // `minHeight` floor keeps the card usable if the right column is ever very short.
    private var chapterList: some View {
        ScrollView(.vertical, showsIndicators: true) {
            VStack(spacing: 0) {
                ForEach(Array(manifest.chapters.enumerated()), id: \.element.id) { idx, ch in
                    ChapterRow(chapter: ch)
                    if idx < manifest.chapters.count - 1 {
                        Hairline(color: Tokens.C.borderHairlineFaint)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(minHeight: 180, maxHeight: .infinity)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCardDeep)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderCard, lineWidth: 1)
        )
    }

    // MARK: Right column (how) — cover · quality · split · estimate. Padding 18 20 18 18.

    private var rightColumn: some View {
        // The PRIMARY content — ALL settings must be visible in FULL. It shows at its
        // NATURAL height and DRIVES the body/window height. The `.frame(maxHeight:
        // bodyAreaCap)` is a SAFEGUARD only: `bodyAreaCap` (≈ visibleFrame − header −
        // footer) is far taller than the column on a normal screen, so `min(content,
        // cap)` = content → the whole column (cover · quality · Стерео/Моно · частота ·
        // Быстрый/Бесшовный · нарезка · размер) shows, nothing is clipped. Only a
        // genuinely oversized column (tiny screen / everything expanded) exceeds the
        // cap and scrolls inside — INDEPENDENTLY of, and unaffected by, the chapter
        // list's scroll. (The earlier bug was a too-SMALL cap that clipped the lower
        // controls; the fix is a cap keyed to the whole body area, not the list.)
        ScrollView(.vertical, showsIndicators: false) {
            // Section gap tightened 18→14 (compact D-fit) so the whole column
            // clears a ~896pt screen without the safeguard scroll kicking in.
            VStack(alignment: .leading, spacing: 14) {
                VStack(alignment: .leading, spacing: 8) {
                    coverCap(coverSourceBadge)
                    coverPicker
                }

                VStack(alignment: .leading, spacing: 7) {
                    cap("КАЧЕСТВО")
                    qualityBox
                }

                splitSection

                estimateBox
            }
            .padding(.init(top: 18, leading: 18, bottom: 18, trailing: 20))
        }
        .frame(maxHeight: bodyAreaCap)
    }

    // Cover picker (spec §3/§4): big preview of the SELECTED option + a row of
    // clickable thumbnails (cover_options, the agent's embedded→web→generated
    // list) + a "Заменить" file button. The source is named by the pill in the
    // «ОБЛОЖКА» header (coverCap) — no text line under the preview. Clicking a thumb
    // changes the LOCAL selection; the chosen id rides into confirm-build's params.
    // ("Искать в сети" is intentionally NOT shown — a re-search needs a new agent
    // command, deferred; web candidates already resolved at scan are still pickable.)
    @ViewBuilder
    private var coverPicker: some View {
        let options = allCoverOptions
        // Big preview: the selected option's image, or the legacy embedded/
        // placeholder fallback when the manifest predates cover_options.
        CoverPreview(option: selectedCoverOption,
                     fallbackState: manifest.coverState,
                     fallbackPreview: manifest.coverPreview,
                     title: title, author: author)

        // Thumbnail strip — only when there is a real choice (≥2 options).
        if options.count > 1 {
            CoverThumbStrip(options: options, selectedID: $coverSelectedID)
        }

        replaceButton
    }

    // "Заменить" — pick a jpg/png via NSOpenPanel; add it as a `custom` option and
    // select it. The app sends the ORIGINAL path as cover_custom_path; the AGENT
    // copies it under covers/ (the app never writes the support tree).
    private var replaceButton: some View {
        Button(action: chooseCustomCover) {
            HStack(spacing: 5) {
                Image(systemName: "plus")
                    .font(.system(size: 11, weight: .semibold))
                Text("Заменить")
                    .font(.system(size: Tokens.F.chDur, weight: .semibold))
            }
            .foregroundColor(Tokens.C.textSoft)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 8)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.cvBtn, style: .continuous)
                    .fill(Tokens.C.surfaceControl)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.cvBtn, style: .continuous)
                    .stroke(Tokens.C.borderControlStrong, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
    }

    /// Open an NSOpenPanel for an image; append it as a `custom` cover option and
    /// select it. Re-choosing replaces the single custom slot (we keep at most one).
    private func chooseCustomCover() {
        let panel = NSOpenPanel()
        panel.title = "Выберите обложку"
        panel.allowedFileTypes = ["jpg", "jpeg", "png"]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let opt = CoverOption(optID: "custom-0", kind: "custom",
                              path: url.path, label: url.lastPathComponent)
        extraCoverOptions = [opt]            // keep a single custom slot
        coverSelectedID = opt.optID
    }

    // Compact quality box (D10): bitrate preset row + (channels seg · samplerate
    // seg + "кГц" suffix) row, divided by a hairline. Border .08, bg #0c121a.
    private var qualityBox: some View {
        VStack(spacing: 0) {
            HStack(spacing: 9) {
                Text("Битрейт")
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textSecondary)
                    .frame(width: 48, alignment: .leading)
                HStack(spacing: 5) {
                    ForEach(Self.bitratePresets, id: \.self) { preset in
                        PresetButton(label: "\(preset)", isOn: bitrate == preset) {
                            bitrate = preset
                        }
                    }
                }
            }

            Hairline(color: Tokens.C.borderHairline)
                .padding(.top, 8)
                .padding(.bottom, 7)

            HStack(spacing: 9) {
                SegControl(options: [("Стерео", "stereo"), ("Моно", "mono")],
                           selection: $channels)
            }

            Hairline(color: Tokens.C.borderHairline)
                .padding(.top, 8)
                .padding(.bottom, 7)

            // Sample rate row: "Как в источнике" (default) → 44.1 → 48. The first
            // option is the sentinel → params.samplerate = nil (no resample). When
            // the agent recorded the source rate, a hint line below names it.
            VStack(alignment: .leading, spacing: 6) {
                SegControl(options: [("Как в источнике", Self.srSourceSentinel),
                                     ("44.1", "44100"), ("48", "48000")],
                           selection: samplerateBinding)
                Text(samplerateHint)
                    .font(.system(size: Tokens.F.qSuffix))
                    .foregroundColor(Tokens.C.textQuaternary)
                    .lineLimit(1)
                    .minimumScaleFactor(0.85)
            }

            Hairline(color: Tokens.C.borderHairline)
                .padding(.top, 8)
                .padding(.bottom, 7)

            // Build-mode row (D15): «Быстрый» (default, parallel groups → ×6–10) vs
            // «Бесшовный» (single-pass, bit-exact). Same segmented style as the
            // channels / sample-rate rows; a hint below explains the trade-off. The
            // choice writes `buildMode` → confirm-build params → build_m4b branches.
            VStack(alignment: .leading, spacing: 6) {
                SegControl(options: [("Быстрый", "fast"),
                                     ("Бесшовный", "seamless")],
                           selection: $buildMode)
                Text(buildModeHint)
                    .font(.system(size: Tokens.F.qSuffix))
                    .foregroundColor(Tokens.C.textQuaternary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.init(top: 11, leading: 12, bottom: 12, trailing: 12))
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCardDeep)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderFieldInput, lineWidth: 1)
        )
    }

    /// Wire/seg sentinel for the "as in source" segment (maps to samplerate = nil).
    private static let srSourceSentinel = "source"

    // The samplerate seg works in Hz strings; the "as in source" option maps to the
    // sentinel ⇄ nil, a concrete number ⇄ its Hz string. nil/unknown selects source.
    private var samplerateBinding: Binding<String> {
        Binding(
            get: { samplerate.map(String.init) ?? Self.srSourceSentinel },
            set: { sel in
                samplerate = (sel == Self.srSourceSentinel) ? nil : Int(sel)
            }
        )
    }

    /// Hint under the sample-rate seg. With a recorded source rate it spells the
    /// "as in source" target ("Как в источнике · 44,1 кГц"); a concrete pick shows
    /// the chosen rate; otherwise a generic "no resample" note.
    private var samplerateHint: String {
        if let sr = samplerate {
            return "Частота дискретизации: \(Self.khzLabel(sr)) кГц"
        }
        if let src = manifest.sourceSamplerate {
            return "Как в источнике · \(Self.khzLabel(src)) кГц"
        }
        return "Как в источнике · без пересэмплинга"
    }

    /// Hint under the build-mode segment (D15) — explains the trade-off per mode.
    private var buildModeHint: String {
        // Kept short so it fits the ~236pt column with NO truncation (the old
        // "…на стыках гл…" clip). Two compact lines, one per mode.
        buildMode == "seamless"
            ? "Бесшовный: бит-в-бит, медленнее."
            : "Быстрый: параллельно; ~25 мс пауза на стыке глав."
    }

    /// Format an Hz rate as a kHz label with a comma decimal (44100 → "44,1",
    /// 48000 → "48", 32000 → "32"). Drops a trailing ",0" for whole-kHz rates.
    private static func khzLabel(_ hz: Int) -> String {
        let khz = Double(hz) / 1000.0
        let s = String(format: "%.1f", khz)            // "44.1" / "48.0"
        let trimmed = s.hasSuffix(".0") ? String(s.dropLast(2)) : s
        return trimmed.replacingOccurrences(of: ".", with: ",")
    }

    // Split toggle row (D6, default OFF). Off → "Выключена — один файл". On → a
    // threshold slider (250…700 МБ, default 300) + a live split-preview that mirrors
    // agent/split.py.plan_parts EXACTLY (spec §6, mockup 06 split-preview). The
    // preview + part chips + the oversize warning recompute on every slider move AND
    // on every bitrate change (both feed SplitPlanner).
    private var splitSection: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center) {
                VStack(alignment: .leading, spacing: 3) {
                    cap("НАРЕЗКА НА ЧАСТИ")
                    Text(split ? "Включена — несколько частей" : "Выключена — один файл")
                        .font(.system(size: Tokens.F.small))
                        .foregroundColor(Tokens.C.textSecondary)
                }
                Spacer()
                Toggle2(isOn: $split)
            }
            if split {
                splitThresholdSlider
                splitPreview
            }
        }
    }

    // Threshold slider (spec §6 / mockup 06): track 6, fill gradient.brandTealIndigo,
    // knob 16; range 250…700 МБ (default 300). A label line above shows the live МБ.
    private var splitThresholdSlider: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Порог размера части")
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textSecondary)
                Spacer()
                Text("\(Int(splitThresholdMB.rounded())) МБ")
                    .font(.system(size: Tokens.F.small).monospacedDigit())
                    .foregroundColor(Tokens.C.textSoft)
            }
            GradientSlider(value: $splitThresholdMB, range: 250...700)
        }
    }

    // Split-preview panel (spec §6, mockup 06 .split-preview): indigoTint08 bg /
    // indigoBorder20 border, sp-big 16/700 indigo "≈ N частей по ~X МБ", a row of
    // part chips (gradient.brandTealIndigo, radius 7). E15: if any part is oversize
    // (one chapter alone over the threshold) warn "части будут крупнее порога".
    private var splitPreview: some View {
        let parts = splitParts
        return VStack(alignment: .leading, spacing: 10) {
            Text(splitPreviewLabel)
                .font(.system(size: 16, weight: .bold).monospacedDigit())
                .foregroundColor(Tokens.C.brandIndigo)
                .fixedSize(horizontal: false, vertical: true)

            if !parts.isEmpty {
                SplitPartChips(parts: parts)
            }

            if parts.contains(where: { $0.oversize }) {
                Text("Часть-глава больше порога — части будут крупнее порога (минимум — глава).")
                    .font(.system(size: Tokens.F.chDur))
                    .foregroundColor(Tokens.C.warnBase)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.init(top: 12, leading: 14, bottom: 12, trailing: 14))
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.estimate, style: .continuous)
                .fill(Tokens.C.indigoTint08)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.estimate, style: .continuous)
                .stroke(Tokens.C.indigoBorder20, lineWidth: 1)
        )
    }

    // Estimate block (accentTint07 bg, border .18, radius 11): big size + sub.
    private var estimateBox: some View {
        VStack(alignment: .leading, spacing: 3) {
            Text(estimateBigLabel)
                .font(.system(size: Tokens.F.estBig, weight: .bold).monospacedDigit())
                .foregroundColor(Tokens.C.brandCyan)
            Text(estimateSubLabel)
                .font(.system(size: Tokens.F.small))
                .foregroundColor(Tokens.C.textSecondary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        // Vertical padding tightened 12→9 (compact D-fit) to reclaim ~6pt.
        .padding(.init(top: 9, leading: 14, bottom: 9, trailing: 14))
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.estimate, style: .continuous)
                .fill(Tokens.C.accentTint07)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.estimate, style: .continuous)
                .stroke(Tokens.C.accentBorder18, lineWidth: 1)
        )
    }

    // MARK: Footer — padding 14 20, top border.card, bg surfaceFill.footer.
    // Status-aware (spec §3 state table):
    //  · confirm  → "Применить ко всем (N)" (P1, queue>1) + Пропустить · Позже ·
    //               Собрать (primary); disabled/sent/failed variants.
    //  · converting/error → an honest status note. The mockup's Cancel/Retry/Skip/
    //    "собрать без неё" buttons are NOT rendered: the agent today accepts only
    //    `confirm-build` (no cancel/retry/skip command), and "no dead buttons" is a
    //    hard rule. Wiring those needs new agent actions — flagged to Yurka.

    @ViewBuilder
    private var footer: some View {
        switch mode {
        case .confirm:   confirmFooter
        case .converting: convertingFooter
        case .error:     noteFooter(errorIsRecoverable
                                    ? "Книга ждёт в очереди — переподтвердите сборку позже"
                                    : "Сборка прервана — книга осталась в очереди")
        }
    }

    // Converting footer (D13 + spec §3 state table): a status note on the left and a
    // danger "Отменить конвертацию" on the right (replacing the old buttonless note).
    // It reuses the queue's cancel path verbatim (onCancel → the same `cancel`
    // command) — NOT a new action. After a successful drop the button locks to its
    // ack; the agent kills ffmpeg and the file-watch returns this window to confirm.
    private var convertingFooter: some View {
        HStack(spacing: 10) {
            Text("Идёт сборка — можно отменить")
                .font(.system(size: Tokens.F.chDur))
                .foregroundColor(Tokens.C.textSecondary)
            Spacer(minLength: 8)
            if cancelSent {
                HStack(spacing: 7) {
                    Image(systemName: "checkmark.circle.fill")
                        .font(.system(size: 12, weight: .bold))
                    Text("Отмена отправлена…")
                        .font(.system(size: Tokens.F.input, weight: .semibold))
                }
                .foregroundColor(Tokens.C.dangerTextSoft)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
            } else {
                cancelButton
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
        .background(Tokens.C.surfaceFooter)
    }

    // Danger "Отменить конвертацию" — same danger-soft styling as the queue's
    // `btn-cancel` (spec §7: textSoft / tint10 bg / border30). `.lineLimit(1)` so the
    // label never wraps by syllable.
    private var cancelButton: some View {
        Button(action: {
            guard !cancelSent else { return }
            if onCancel() { cancelSent = true }
        }) {
            HStack(spacing: 7) {
                Image(systemName: "xmark")
                    .font(.system(size: 12, weight: .bold))
                Text("Отменить конвертацию")
                    .font(.system(size: Tokens.F.input, weight: .bold))
                    .lineLimit(1)
            }
            .foregroundColor(Tokens.C.dangerTextSoft)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .fill(Tokens.C.dangerTint10)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .stroke(Tokens.C.dangerBorder30, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
    }

    private var confirmFooter: some View {
        HStack(spacing: 10) {
            // Only meaningful when more than one book is awaiting confirmation
            // (spec §3: «показывать, если в очереди >1 книги»).
            if pendingConfirmCount > 1 {
                applyAllLink
            }
            Spacer(minLength: 8)
            // "Пропустить" — LIVE (agent action `skip`, arch/plan-claude.md §2.3):
            // takes the book off the pipeline, sources untouched. It is not a
            // deletion and not a dead end — the book lands in the queue's ПРОПУЩЕНО
            // section with «Вернуть» on the row, and a conscious re-drop of the
            // folder re-arms it too (lesson .patches/004). Once the command is on
            // disk the button locks to its ack; the agent's status flip then retires
            // this window (the book stops being active).
            if skipSent {
                skipAck
            } else {
                ghostButton("Пропустить", skip: true,
                            help: "Снять книгу с обработки. Файлы не тронуты — "
                                + "книга уйдёт в раздел «Пропущено» в очереди, "
                                + "откуда её можно вернуть.") {
                    if onSkip() { skipSent = true }
                }
            }
            // "Позже в очередь" → open the queue; the book stays pending (no status
            // change here — the agent owns status). This is the window's entry point
            // into the queue screen (spec §7).
            ghostButton("Позже в очередь", skip: false,
                        help: "Открыть очередь. Книга останется ожидать "
                            + "подтверждения — ничего не удаляется.") { onOpenQueue() }
            if sent {
                sentAck
            } else {
                buildButton
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
        .background(Tokens.C.surfaceFooter)
    }

    // Honest footer for non-actionable states: a single left-aligned status line
    // (no buttons — see footer note). Same padding/fill as the action bar so the
    // window chrome is consistent across states.
    private func noteFooter(_ text: String) -> some View {
        HStack(spacing: 8) {
            Text(text)
                .font(.system(size: Tokens.F.chDur))
                .foregroundColor(Tokens.C.textSecondary)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 14)
        .background(Tokens.C.surfaceFooter)
    }

    /// True while the preset in force covers this book AND still equals what the
    /// controls show — i.e. the click actually took effect and nothing was edited
    /// afterwards. Derived (no extra @State), so editing a knob honestly flips the
    /// link back from "Применено" to "Применить".
    private var appliedToAll: Bool {
        guard let preset = paramsPreset, preset.applies(to: manifest.bookID) else { return false }
        return preset.params == editedParams
    }

    /// Footer link "Применить параметры ко всем (N)" (spec §3 / US-3.7): stores the
    /// current quality/split params as the preset for the books awaiting
    /// confirmation, so each opens pre-filled. It builds NOTHING and sends no
    /// command — the cover and the final "Собрать" stay per-book.
    private var applyAllLink: some View {
        Button(action: { onApplyToAll(editedParams) }) {
            HStack(spacing: 6) {
                Image(systemName: appliedToAll ? "checkmark.circle.fill" : "checkmark")
                    .font(.system(size: 11, weight: .bold))
                Text(appliedToAll
                        ? "Применено ко всем (\(pendingConfirmCount))"
                        : "Применить параметры ко всем (\(pendingConfirmCount))")
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
            }
            .foregroundColor(Tokens.C.accentLabel)
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .help("Битрейт, каналы, частота, режим и нарезка станут значениями по "
            + "умолчанию для остальных книг, ожидающих подтверждения. "
            + "Обложку и «Собрать» подтверждаете по каждой книге отдельно.")
    }

    /// Footer ghost button. `enabled: false` renders it visibly inert (dimmed, no
    /// click) instead of a dead control — the queue's disabled "Повторить" pattern
    /// (QueueView `QButton(enabled:)`). `help` is REQUIRED (never empty, so no blank
    /// tooltip bubble can appear) and, for an inert button, explains WHY on hover.
    private func ghostButton(_ title: String, skip: Bool,
                             enabled: Bool = true, help: String,
                             action: @escaping () -> Void) -> some View {
        Button(action: { if enabled { action() } }) {
            Text(title)
                .font(.system(size: Tokens.F.body, weight: .semibold))
                .foregroundColor(Tokens.C.textSoft)
                .padding(.horizontal, 16)
                .padding(.vertical, 10)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                        .fill(Tokens.C.surfaceControl)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                        .stroke(skip ? Tokens.C.borderFieldInput : Tokens.C.borderControlStrong,
                                lineWidth: 1)
                )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.55)
        .help(help)
        .accessibilityLabel("\(title). \(help)")
    }

    // Active "Собрать": writes the command with edited params, then locks the ack.
    // Disabled (dimmed) when the title is empty — spec §3 disabled state.
    private var buildButton: some View {
        Button(action: {
            guard !titleIsEmpty else { return }
            failed = false
            if onBuild(editedParams, sendCoverID, sendCoverCustomPath) {
                sent = true
            } else {
                failed = true
            }
        }) {
            HStack(spacing: 7) {
                Image(systemName: "play.fill")
                    .font(.system(size: 12, weight: .bold))
                Text(failed ? "Повторить" : "Собрать")
                    .font(.system(size: Tokens.F.input, weight: .bold))
            }
            .foregroundColor(titleIsEmpty ? Tokens.C.textQuaternary : Tokens.C.textOnAccent)
            .padding(.horizontal, 22)
            .padding(.vertical, 10)
            .background(buildButtonBackground)
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .disabled(titleIsEmpty)
    }

    // Disabled → flat dim fill (rgba(255,255,255,.08)) per spec §3; enabled → the
    // brand gradient. Branched as whole backgrounds (concrete ShapeStyles) to stay
    // macOS-11-safe — AnyShapeStyle is 12+.
    @ViewBuilder
    private var buildButtonBackground: some View {
        if titleIsEmpty {
            RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                .fill(Color.white(0.08))
        } else {
            RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                .fill(Tokens.Grad.brandButton)
        }
    }

    // Disabled ack after a successful drop — muted, reads "done, waiting".
    private var sentAck: some View {
        Text("Отправлено…")
            .font(.system(size: Tokens.F.input, weight: .bold))
            .foregroundColor(Tokens.C.textSecondary)
            .padding(.horizontal, 22)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .fill(Tokens.C.surfaceControl)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .stroke(Tokens.C.borderControl, lineWidth: 1)
            )
    }

    /// Ack for «Пропустить», in the ghost button's own chrome so the footer does
    /// not reflow. It says where the book WENT — never a bare "готово" that leaves
    /// the user wondering what just happened to it.
    private var skipAck: some View {
        Text("Пропущено →")
            .font(.system(size: Tokens.F.body, weight: .semibold))
            .foregroundColor(Tokens.C.textSecondary)
            .padding(.horizontal, 16)
            .padding(.vertical, 10)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .fill(Tokens.C.surfaceControl)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .stroke(Tokens.C.borderFieldInput, lineWidth: 1)
            )
            .help("Книга снята с обработки и лежит в разделе «Пропущено» в очереди.")
    }

    // MARK: Computed labels

    // "12 глав · ~14 ч 20 мин" (tnum). Falls back gracefully if M0.5 hasn't probed.
    private var chapterTotalLabel: String {
        let n = manifest.chapters.count
        let word = Plural.chapters(n)
        if manifest.hasDurations {
            return "\(n) \(word) · ~\(Duration.humanCoarse(manifest.totalSeconds))"
        }
        return "\(n) \(word)"
    }

    // Estimate big line: "≈ 1.18 ГБ". Mirrors agent/build_m4b.estimate_output_size:
    // audio = bitrate_bps/8 × seconds, + cover allowance (embedded) + mux overhead.
    private var estimateBigLabel: String {
        "≈ \(ByteSize.human(estimatedBytes))"
    }

    private var estimateSubLabel: String {
        let parts = split ? "несколько .m4b" : "один .m4b"
        let ch = channels == "mono" ? "моно" : "стерео"
        let dur = manifest.hasDurations ? "\(Duration.humanCoarse(manifest.totalSeconds)) · " : ""
        return "\(dur)\(manifest.chapters.count) \(Plural.chapters(manifest.chapters.count)) · \(parts) · AAC \(bitrate)k \(ch)"
    }

    private var estimatedBytes: Int {
        let totalSeconds = manifest.totalSeconds
        let audio = Int(Double(bitrate) * 1000.0 / 8.0 * totalSeconds)
        let cover = manifest.coverState == "embedded" ? 60 * 1024 : 0
        let overhead = 64 * 1024
        return audio + cover + overhead
    }

    // The live split plan — a 1:1 mirror of agent/split.py.plan_parts run against
    // the CURRENT bitrate and the slider's threshold (MB→bytes). Recomputes whenever
    // either changes, so the preview matches the build the agent will make.
    private var splitParts: [SplitPart] {
        let thresholdBytes = Int(splitThresholdMB.rounded()) * 1024 * 1024
        return SplitPlanner.plan(chapters: manifest.chapters,
                                 bitrateKbps: bitrate,
                                 thresholdBytes: thresholdBytes)
    }

    // Split preview big line (spec §6): "≈ N частей по ~X МБ", N = part count from
    // the planner, X = the average part size. Falls back to a single-part framing
    // when durations are unknown (planner returns []).
    private var splitPreviewLabel: String {
        let parts = splitParts
        guard !parts.isEmpty else {
            // No usable per-chapter durations — degrade to one whole-book "part".
            return "≈ 1 \(Plural.parts(1)) по ~\(ByteSize.human(estimatedBytes))"
        }
        let n = parts.count
        let avg = parts.reduce(0) { $0 + $1.estSize } / n
        return "≈ \(n) \(Plural.parts(n)) по ~\(ByteSize.human(avg))"
    }

    private func cap(_ text: String) -> some View {
        Text(text)
            .font(.system(size: Tokens.F.cap, weight: .bold))
            .tracking(1.2)
            .foregroundColor(Tokens.C.textTertiary)
    }

    // MARK: Cover source label (in the «ОБЛОЖКА» header line)

    /// Source badge for the picker header: the SELECTED option's kind/text
    /// (embedded → «ИЗ ФАЙЛА», web → «ИЗ СЕТИ», …); legacy manifests without
    /// cover_options fall back to `coverState`. Nil (no cover) → no label.
    private var coverSourceBadge: CoverBadgeStyle? {
        if let opt = selectedCoverOption {
            return CoverBadgeStyle.forKind(opt.kind, text: opt.badgeText)
        }
        return coverStateSourceBadge
    }

    /// Source badge derived from the legacy `coverState` alone — used by the
    /// compact converting/error column, whose CoverBox only ever shows the
    /// embedded jpg. "none"/"unknown" → nil → no label.
    private var coverStateSourceBadge: CoverBadgeStyle? {
        switch manifest.coverState {
        case "embedded":
            return CoverBadgeStyle.forKind("embedded", text: "ИЗ ФАЙЛА")
        case "web", "downloaded":
            return CoverBadgeStyle.forKind("web", text: "ИЗ СЕТИ")
        default:
            return nil
        }
    }

    /// The «ОБЛОЖКА» section cap with the optional source badge to its right —
    /// the same dot + caps-text pill the on-image badge had (spec §4 colors,
    /// 9/4 padding), moved into the header per feedback so the cover art
    /// itself stays clean.
    private func coverCap(_ style: CoverBadgeStyle?) -> some View {
        HStack(spacing: 8) {
            cap("ОБЛОЖКА")
            if let style = style {
                HStack(spacing: 5) {
                    Circle().fill(style.dot).frame(width: 5, height: 5)
                    Text(style.text)
                        .font(.system(size: Tokens.F.badge, weight: .bold))
                        .tracking(0.3)
                        .foregroundColor(Tokens.C.textOnAccentHigh)
                }
                .padding(.horizontal, 9)
                .padding(.vertical, 4)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous)
                        .fill(style.bg)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous)
                        .stroke(style.border, lineWidth: 1)
                )
            }
        }
    }
}

// MARK: - Converting block (status == converting)

/// The live-build block (mockup 05 .conv-wrap): pulse + "Собираю .m4b…", an
/// encoding sub-line, a DETERMINATE progress bar, and the live status lines
/// "Глава X из Y: <имя>" / "Прошло mm:ss" / "Осталось ≈mm:ss".
///
/// Driven by the agent's `BuildProgress` contract (state.json `books[i].progress`),
/// streamed off ffmpeg's `-progress`. This replaces the old indeterminate pulse:
/// the agent now reports a real position, so the bar is honest. Two phases:
///   · `progress == nil` (build just entered converting, no snapshot yet) → a
///     determinate bar pinned at 0 + "Запуск…", NOT a deceptive sliding highlight;
///   · `progress != nil` → `ProgressView(value:)` at `percent`, the chapter line,
///     and elapsed/ETA (ETA `nil` early in the encode → "оцениваю…").
/// Pulse dot = spec §1 (1.2s, opacity 1↔.35).
private struct ConvertingBlock: View {
    let progress: BuildProgress?
    let bitrate: Int
    let channels: String

    @State private var pulseOn = false

    private var channelWord: String { channels == "mono" ? "моно" : "стерео" }

    /// "Глава X из Y: <имя>" once the agent knows the chapter; a neutral
    /// "Подготовка глав…" before the first snapshot. The name is omitted when blank.
    private var chapterLine: String {
        guard let p = progress, let idx = p.currentChapterIndex, p.totalChapters > 0 else {
            return "Подготовка глав…"
        }
        let name = (p.currentChapterName ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let head = "Глава \(idx) из \(p.totalChapters)"
        return name.isEmpty ? head : "\(head): \(name)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Circle()
                    .fill(Tokens.C.brandCyan)
                    .frame(width: 8, height: 8)
                    .shadow(color: Tokens.C.brandCyan.opacity(0.8), radius: 4)
                    .opacity(pulseOn ? 0.35 : 1.0)
                Text("Собираю .m4b…")
                    .font(.system(size: Tokens.F.input, weight: .bold))
                    .foregroundColor(Tokens.C.textHigh)
                Spacer(minLength: 8)
                // Percent readout (monospaced so it doesn't jitter as it ticks).
                if let p = progress {
                    Text("\(Int(p.percent.rounded())) %")
                        .font(.system(size: Tokens.F.caption, weight: .bold).monospacedDigit())
                        .foregroundColor(Tokens.C.brandCyan)
                }
            }
            Text("Кодирование AAC \(bitrate)k \(channelWord)")
                .font(.system(size: Tokens.F.caption))
                .foregroundColor(Tokens.C.textMuted)
                .padding(.top, 4)

            // Determinate track: a filled bar at `percent` (0 until the first
            // snapshot). The gradient fill + glow match the old look; only the width
            // is now real instead of a sliding highlight.
            GeometryReader { geo in
                let trackW = geo.size.width
                let frac = progress?.fraction ?? 0
                let fillW = max(0, min(trackW, trackW * frac))
                ZStack(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .fill(Tokens.C.progressTrack)
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .fill(Tokens.Grad.progressFill)
                        .frame(width: fillW)
                        .shadow(color: Tokens.C.brandTeal.opacity(0.5), radius: 6)
                        .animation(.easeInOut(duration: 0.45), value: fillW)
                }
            }
            .frame(height: 8)
            .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
            .padding(.top, 14)

            // Chapter line — "Глава X из Y: <имя>" (or a prep note before snapshot).
            Text(chapterLine)
                .font(.system(size: Tokens.F.chDur, weight: .medium))
                .foregroundColor(Tokens.C.textSecondary)
                .lineLimit(1)
                .truncationMode(.tail)
                .padding(.top, 10)

            // Elapsed + ETA, or an honest "Запуск…" before the first snapshot.
            if let p = progress {
                HStack(spacing: 0) {
                    Text("Прошло \(Self.clock(p.elapsedS))")
                    Text("  ·  ")
                    if let eta = p.etaS {
                        Text("Осталось ≈\(Self.clock(eta))")
                    } else {
                        Text("Осталось оцениваю…")
                    }
                    Spacer(minLength: 0)
                }
                .font(.system(size: Tokens.F.chDur).monospacedDigit())
                .foregroundColor(Tokens.C.textMuted)
                .padding(.top, 4)
            } else {
                Text("Запуск…")
                    .font(.system(size: Tokens.F.chDur))
                    .foregroundColor(Tokens.C.textMuted)
                    .padding(.top, 4)
            }
        }
        .onAppear {
            withAnimation(.easeInOut(duration: 1.2).repeatForever(autoreverses: true)) {
                pulseOn = true
            }
        }
    }

    /// Seconds → "mm:ss" (or "h:mm:ss" past an hour). Used for both elapsed + ETA so
    /// the two read consistently. Negative/garbage clamps to 0.
    static func clock(_ seconds: Int) -> String {
        let s = max(0, seconds)
        let h = s / 3600
        let m = (s % 3600) / 60
        let sec = s % 60
        if h > 0 {
            return String(format: "%d:%02d:%02d", h, m, sec)
        }
        return String(format: "%d:%02d", m, sec)
    }
}

// MARK: - Cover box (spec §3 / §4): square 1:1, embedded image or placeholder

/// The 1:1 cover. `embedded` → the extracted jpg; anything else → a neutral
/// placeholder card with a "нет обложки" note (the web/generate chain is the
/// NEXT slice). The source badge ("ИЗ ФАЙЛА"/"ИЗ СЕТИ") moved OFF the image
/// into the «ОБЛОЖКА» section header (ConfirmView.coverCap) per feedback —
/// the art stays clean. Never crashes on a nil/bad path: a missing file
/// falls back to the placeholder.
private struct CoverBox: View {
    let coverState: String
    let coverPreview: String?
    let title: String
    let author: String

    private var embeddedImage: NSImage? {
        guard coverState == "embedded", let path = coverPreview, !path.isEmpty else { return nil }
        return NSImage(contentsOfFile: path)
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            if let img = embeddedImage {
                Image(nsImage: img)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .frame(maxWidth: .infinity)
                    .aspectRatio(1, contentMode: .fit)
                    .clipped()
            } else {
                placeholder
            }
        }
        .aspectRatio(1, contentMode: .fit)
        .frame(maxWidth: .infinity)
        .background(Tokens.C.bgCardDeep)
        .clipShape(RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderFieldInput, lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(0.7), radius: 12, x: 0, y: 8)
    }

    // Neutral "no cover yet" state (cover-empty): icon + two lines. NOT the demo
    // fallback gradient — the real generator draws fallbacks later (spec §0.8).
    private var placeholder: some View {
        VStack(spacing: 10) {
            Image(systemName: "photo")
                .font(.system(size: 30, weight: .light))
                .foregroundColor(Tokens.C.textSecondary.opacity(0.5))
            Text("Обложка не найдена")
                .font(.system(size: Tokens.F.caption))
                .foregroundColor(Tokens.C.textMuted)
            Text("Подберём из сети или сгенерируем")
                .font(.system(size: Tokens.F.small))
                .foregroundColor(Tokens.C.textTertiary)
                .multilineTextAlignment(.center)
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Cover badge style (per kind) — spec §4

/// The badge colors for one cover `kind` (embedded/web/generated/custom): bg,
/// border and dot, lifted from mockup 04 (.cover-badge / .web / .gen / .user).
private struct CoverBadgeStyle {
    let text: String
    let bg: Color
    let border: Color
    let dot: Color

    static func forKind(_ kind: String, text: String) -> CoverBadgeStyle {
        switch kind {
        case "web":
            return .init(text: text, bg: Tokens.C.coverBadgeWebBg,
                         border: Tokens.C.coverBadgeWebBorder, dot: Tokens.C.brandIndigo)
        case "generated":
            return .init(text: text, bg: Tokens.C.coverBadgeGenBg,
                         border: Tokens.C.coverBadgeGenBorder, dot: Tokens.C.coverBadgeGenDot)
        default: // embedded + custom (user) share the teal "success" badge
            return .init(text: text, bg: Tokens.C.coverBadgeBg,
                         border: Tokens.C.coverBadgeBorder, dot: Tokens.C.brandCyan)
        }
    }
}

// MARK: - Cover preview (selected option, big 1:1) — spec §3/§4

/// The large 1:1 preview of the SELECTED cover option. Loads the option's file via
/// NSImage(contentsOfFile:) (every option is a real file the agent wrote, or — for
/// `custom` — the user's original); on a nil/bad path or no options it degrades to
/// the legacy embedded image / neutral placeholder (older manifests). The per-kind
/// source badge moved OFF the image into the «ОБЛОЖКА» section header
/// (ConfirmView.coverCap) per feedback — the art stays clean. Never crashes on a
/// missing file.
private struct CoverPreview: View {
    let option: CoverOption?
    let fallbackState: String
    let fallbackPreview: String?
    let title: String
    let author: String

    /// The image to show: the selected option's file, else the legacy embedded jpg.
    private var image: NSImage? {
        if let p = option?.path, !p.isEmpty, let img = NSImage(contentsOfFile: p) {
            return img
        }
        if fallbackState == "embedded", let p = fallbackPreview, !p.isEmpty {
            return NSImage(contentsOfFile: p)
        }
        return nil
    }

    var body: some View {
        ZStack(alignment: .topLeading) {
            if let img = image {
                Image(nsImage: img)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
                    .frame(maxWidth: .infinity)
                    .aspectRatio(1, contentMode: .fit)
                    .clipped()
            } else {
                placeholder
            }
        }
        .aspectRatio(1, contentMode: .fit)
        // Cap the 1:1 preview to a compact side (was full-column ~242pt, which
        // pushed the lower right-column controls off a ~896pt screen). Sits
        // left-aligned in the column's .leading VStack — value from
        // Tokens.M.confirmCoverMax, not an eyeballed number.
        .frame(maxWidth: Tokens.M.confirmCoverMax)
        .background(Tokens.C.bgCardDeep)
        .clipShape(RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderFieldInput, lineWidth: 1)
        )
        .shadow(color: Color.black.opacity(0.7), radius: 12, x: 0, y: 8)
    }

    // Neutral "no cover yet" placeholder (only when even the fallback is empty —
    // with the cover chain this is rare; the agent always resolves ≥1 option).
    private var placeholder: some View {
        VStack(spacing: 10) {
            Image(systemName: "photo")
                .font(.system(size: 30, weight: .light))
                .foregroundColor(Tokens.C.textSecondary.opacity(0.5))
            Text("Обложка не найдена")
                .font(.system(size: Tokens.F.caption))
                .foregroundColor(Tokens.C.textMuted)
            Text("Подберём из сети или сгенерируем")
                .font(.system(size: Tokens.F.small))
                .foregroundColor(Tokens.C.textTertiary)
                .multilineTextAlignment(.center)
        }
        .padding(16)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Cover thumbnail strip (the picker) — spec §3/§4

/// A horizontally-scrolling row of clickable cover thumbnails (cover_options). The
/// selected one carries the accent ring + glow (mockup .gen-cell.sel: 0 0 0 2px
/// #22B5E0 + glow) and a check mark. Tapping a thumb sets `selectedID` (a working
/// control with .contentShape — sibling lesson: transparent SwiftUI tap targets
/// need an explicit hit shape). Each thumb is a 1:1 square; the row scrolls if the
/// options overflow the column width.
private struct CoverThumbStrip: View {
    let options: [CoverOption]
    @Binding var selectedID: String?

    private let thumbSize: CGFloat = Tokens.M.confirmCoverThumb  // 46 (compact D-fit; was 54)

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(options) { opt in
                    thumb(opt)
                }
            }
            .padding(.vertical, 2)   // room for the selected glow/ring
            .padding(.horizontal, 1)
        }
    }

    private func thumb(_ opt: CoverOption) -> some View {
        let isOn = opt.optID == selectedID
        return Button(action: { selectedID = opt.optID }) {
            ZStack(alignment: .topTrailing) {
                thumbImage(opt)
                    .frame(width: thumbSize, height: thumbSize)
                    .clipShape(RoundedRectangle(cornerRadius: Tokens.R.cvBtn, style: .continuous))
                    .overlay(
                        // Unselected: faint contour (.gen-cell border .08). Selected:
                        // 2px accent ring (drawn as a stroked rounded rect on top).
                        RoundedRectangle(cornerRadius: Tokens.R.cvBtn, style: .continuous)
                            .stroke(isOn ? Tokens.C.coverSelRing : Tokens.C.borderFieldInput,
                                    lineWidth: isOn ? 2 : 1)
                    )
                    .shadow(color: isOn ? Tokens.C.coverSelGlow : .clear,
                            radius: isOn ? 7 : 0)
                if isOn {
                    checkMark
                }
            }
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
    }

    // The thumbnail image, or a neutral tile if the file can't be loaded.
    @ViewBuilder
    private func thumbImage(_ opt: CoverOption) -> some View {
        if !opt.path.isEmpty, let img = NSImage(contentsOfFile: opt.path) {
            Image(nsImage: img)
                .resizable()
                .aspectRatio(contentMode: .fill)
        } else {
            ZStack {
                Tokens.C.bgCardDeep
                Image(systemName: "photo")
                    .font(.system(size: 16, weight: .light))
                    .foregroundColor(Tokens.C.textTertiary)
            }
        }
    }

    // Selected check (mockup .gen-cell .check): teal disc, dark ring, white tick.
    private var checkMark: some View {
        ZStack {
            Circle().fill(Tokens.C.coverSelRing)
            Image(systemName: "checkmark")
                .font(.system(size: 8, weight: .heavy))
                .foregroundColor(.white)
        }
        .frame(width: 16, height: 16)
        .overlay(Circle().stroke(Tokens.C.bgInput, lineWidth: 2))
        .padding(4)
    }
}

// MARK: - Preset button (quality bitrate)

/// One bitrate preset cell (.preset): tnum label, flex width. Selected → accent
/// tint bg + accent border + inset ring (spec §3 / mockup .preset.on).
private struct PresetButton: View {
    let label: String
    let isOn: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text(label)
                .font(.system(size: Tokens.F.chDur, weight: .semibold).monospacedDigit())
                .foregroundColor(isOn ? Tokens.C.textHigh : Tokens.C.textMuted)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 6)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous)
                        .fill(isOn ? Tokens.C.accentTintBg : Tokens.C.surfaceControlSoft)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous)
                        .stroke(isOn ? Tokens.C.accentBorder60 : Tokens.C.borderControl,
                                lineWidth: isOn ? 1 : 1)
                )
                .overlay(
                    // inset accent ring on the selected preset (presetInset).
                    isOn
                    ? RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous)
                        .inset(by: 1)
                        .stroke(Tokens.C.accentBorder30, lineWidth: 1)
                    : nil
                )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
    }
}

// MARK: - Segmented control (channels / samplerate)

/// A flat 2-segment control (.seg): equal-width buttons, single 1px divider,
/// selected segment gets the accent tint (spec §3 / mockup .seg button.on). The
/// control hugs its content height (buttons are `padding 6 2` ≈ 28pt) — the
/// dividers match the buttons because the row is fixed-size vertically.
private struct SegControl: View {
    /// (display label, stored value) pairs.
    let options: [(String, String)]
    @Binding var selection: String

    var body: some View {
        HStack(spacing: 0) {
            ForEach(Array(options.enumerated()), id: \.offset) { idx, opt in
                if idx > 0 {
                    Rectangle().fill(Tokens.C.borderControl).frame(width: 1)
                }
                let isOn = selection == opt.1
                Button(action: { selection = opt.1 }) {
                    Text(opt.0)
                        .font(.system(size: Tokens.F.chDur, weight: .semibold).monospacedDigit())
                        .foregroundColor(isOn ? Tokens.C.textHigh : Tokens.C.textMuted)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 6)
                        .background(isOn ? Tokens.C.accentTintBg : Color.clear)
                }
                .buttonStyle(.plain)
                .contentShape(Rectangle())
            }
        }
        .fixedSize(horizontal: false, vertical: true) // hug button height; no tall dividers
        .frame(maxWidth: .infinity)
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous)
                .stroke(Tokens.C.borderControl, lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous))
    }
}

// MARK: - Toggle (split on/off)

/// The split toggle (40×23 track, 19 knob). Off → track .10; on → teal→indigo
/// gradient + knob slid right. Animated .15s (spec §1 motion).
private struct Toggle2: View {
    @Binding var isOn: Bool

    var body: some View {
        Button(action: { withAnimation(.easeInOut(duration: 0.15)) { isOn.toggle() } }) {
            ZStack(alignment: isOn ? .trailing : .leading) {
                track // branched concrete fills — macOS-11-safe (no AnyShapeStyle)
                    .frame(width: 40, height: 23)
                Circle()
                    .fill(Color.white)
                    .frame(width: 19, height: 19)
                    .shadow(color: Color.black.opacity(0.4), radius: 1.5, x: 0, y: 1)
                    .padding(.horizontal, 2)
            }
            .frame(width: 40, height: 23)
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
    }

    @ViewBuilder
    private var track: some View {
        if isOn {
            RoundedRectangle(cornerRadius: Tokens.R.togglePill, style: .continuous)
                .fill(Tokens.Grad.brandTealIndigo)
        } else {
            RoundedRectangle(cornerRadius: Tokens.R.togglePill, style: .continuous)
                .fill(Tokens.C.progressTrack)
        }
    }
}

// MARK: - Gradient slider (split threshold)

/// Custom horizontal slider for the split threshold (spec §6 / mockup 06): a 6px
/// track (radius 4), a teal→indigo gradient fill, and a 16px white knob with a soft
/// shadow. macOS-11-safe (concrete fills, no AnyShapeStyle). Only the track WIDTH is
/// read from GeometryReader — the height is fixed, so this never self-measures its
/// own height (lesson .patches/002: GeometryReader height collapse).
private struct GradientSlider: View {
    @Binding var value: Double
    let range: ClosedRange<Double>

    private let trackHeight: CGFloat = 6
    private let knob: CGFloat = 16

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            let span = range.upperBound - range.lowerBound
            let frac = span > 0
                ? CGFloat((value - range.lowerBound) / span)
                : 0
            let clamped = min(1, max(0, frac))
            // Keep the knob fully on-track: its centre travels within [knob/2, w-knob/2].
            let knobX = knob / 2 + clamped * max(0, w - knob)
            let fillW = knobX

            ZStack(alignment: .leading) {
                // Track
                Capsule(style: .continuous)
                    .fill(Tokens.C.progressTrack)
                    .frame(height: trackHeight)
                // Fill (teal→indigo)
                Capsule(style: .continuous)
                    .fill(Tokens.Grad.brandTealIndigo)
                    .frame(width: fillW, height: trackHeight)
                // Knob
                Circle()
                    .fill(Color.white)
                    .frame(width: knob, height: knob)
                    .shadow(color: Color.black.opacity(0.5), radius: 2, x: 0, y: 1)
                    .offset(x: knobX - knob / 2)
            }
            .frame(width: w, height: knob, alignment: .leading)
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { g in
                        let travel = max(1, w - knob)
                        let x = min(travel, max(0, g.location.x - knob / 2))
                        value = range.lowerBound + Double(x / travel) * span
                    }
            )
        }
        .frame(height: knob)              // fixed height — no vertical self-sizing
    }
}

// MARK: - Split part chips (split-preview)

/// The row of part chips under the split-preview (spec §6 / mockup 06 .sp-parts):
/// equal-width chips (flex 1), height 30, radius 7, gradient.brandTealIndigo, the
/// part number in dark ink. With many parts the numbers would be unreadable in a
/// thin sliver, so above a threshold the chips become plain bars (no number) — the
/// count is still conveyed visually. Deterministic (count-based), no measurement.
private struct SplitPartChips: View {
    let parts: [SplitPart]

    /// Above this many parts, drop the per-chip number (chips get too thin to read).
    private let numberLimit = 14

    var body: some View {
        let showNumbers = parts.count <= numberLimit
        HStack(spacing: 6) {
            ForEach(parts, id: \.index) { part in
                ZStack {
                    RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous)
                        .fill(Tokens.Grad.brandTealIndigo)
                    if showNumbers {
                        Text("\(part.index)")
                            .font(.system(size: 10, weight: .bold).monospacedDigit())
                            .foregroundColor(Color(hex: "#06121a"))
                    }
                }
                .frame(maxWidth: .infinity)
                .frame(height: 30)
            }
        }
    }
}

/// One chapter row: grid 26 / 1fr / auto — № (quaternary tnum, right-aligned) ·
/// name (13 high, middle-truncated) · duration (11.5 secondary tnum, em-dash if
/// the file was unreadable).
private struct ChapterRow: View {
    let chapter: ChapterEntry

    var body: some View {
        HStack(spacing: 10) {
            Text("\(chapter.index)")
                .font(.system(size: Tokens.F.small).monospacedDigit())
                .foregroundColor(Tokens.C.textQuaternary)
                .frame(width: 26, alignment: .trailing)
            Text(chapter.name)
                .font(.system(size: Tokens.F.body))
                .foregroundColor(Tokens.C.textHigh)
                .lineLimit(1)
                .truncationMode(.tail)
            Spacer(minLength: 8)
            Text(durationText)
                .font(.system(size: Tokens.F.chDur).monospacedDigit())
                .foregroundColor(Tokens.C.textSecondary)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 9)
    }

    private var durationText: String {
        if let s = chapter.durationSeconds { return Duration.human(s) }
        return "—"
    }
}

// MARK: - Split planner (Swift port of agent/split.py plan_parts)

/// One part of the live split preview — the SAME shape agent/split.py.plan_parts
/// returns, computed client-side so the confirm window's "≈ N частей по ~X МБ"
/// preview matches EXACTLY what the agent will produce on build.
private struct SplitPart {
    let index: Int          // 1-based
    let estSize: Int        // estimated bytes (audio + one per-part overhead)
    let oversize: Bool      // true iff a single chapter alone exceeds the threshold (E15)
}

/// Greedy grouping of CONSECUTIVE chapters into parts each ≤ a byte threshold —
/// a 1:1 port of agent/split.py `plan_parts` so the preview never lies against the
/// real build (the engine is the single source of the math; this only mirrors it).
///
/// Coefficients copied verbatim from split.py:
///   · per-chapter audio  = `bitrate_kbps × 1000 / 8 × seconds` (= `_chapter_size_bytes`,
///     reusing build_m4b's bitrate×duration coefficient);
///   · per-part overhead  = `60·1024 + 64·1024` (= `_PER_PART_OVERHEAD_BYTES`,
///     cover + moov/chapter-track/mux slack, ONE per part);
///   · E15: a single chapter whose own estimate already exceeds the threshold
///     stands alone, flagged `oversize` (we never split mid-chapter).
/// Only chapters with a positive duration are placed (an unreadable one has no
/// timeline position). Returns [] for no usable chapters.
private enum SplitPlanner {
    /// Fixed per-part allowance — must equal split.py `_PER_PART_OVERHEAD_BYTES`.
    static let perPartOverheadBytes = 60 * 1024 + 64 * 1024

    /// Estimated audio bytes for `durationMS` at `bitrateKbps` (= `_chapter_size_bytes`).
    static func chapterAudioBytes(durationMS: Int, bitrateKbps: Int) -> Int {
        let bitrateBps = bitrateKbps * 1000
        let seconds = Double(max(0, durationMS)) / 1000.0
        return Int(Double(bitrateBps) / 8.0 * seconds)
    }

    /// Group consecutive chapters into parts each ≤ `thresholdBytes` (split.py order).
    static func plan(chapters: [ChapterEntry], bitrateKbps: Int,
                     thresholdBytes: Int) -> [SplitPart] {
        // A non-positive threshold is meaningless — split.py clamps it; mirror that.
        let threshold = thresholdBytes > 0 ? thresholdBytes : 300 * 1024 * 1024

        // Placeable chapters = those with a positive duration (split.py filter).
        let sizes: [Int] = chapters.compactMap { ch in
            guard let ms = ch.durationMS, ms > 0 else { return nil }
            return chapterAudioBytes(durationMS: ms, bitrateKbps: bitrateKbps)
        }
        if sizes.isEmpty { return [] }

        var parts: [SplitPart] = []
        var curAudio = 0          // accumulated audio bytes of the current part
        var curHasItems = false

        func flush(oversize: Bool) {
            guard curHasItems else { return }
            parts.append(SplitPart(index: parts.count + 1,
                                   estSize: curAudio + perPartOverheadBytes,
                                   oversize: oversize))
            curAudio = 0
            curHasItems = false
        }

        for size in sizes {
            // E15: a single chapter bigger than the threshold stands alone, flagged.
            if size + perPartOverheadBytes > threshold {
                flush(oversize: false)
                curAudio = size
                curHasItems = true
                flush(oversize: true)
                continue
            }
            // Would adding this chapter push the current part over the threshold?
            let prospective = curAudio + size + perPartOverheadBytes
            if curHasItems && prospective > threshold {
                flush(oversize: false)
            }
            curAudio += size
            curHasItems = true
        }
        flush(oversize: false)

        // total is implicit (parts.count); index is already 1-based.
        return parts
    }
}

// MARK: - Hairline (1px divider, macOS 11-safe)

/// A 1px full-width rule. `Divider().overlay(ShapeStyle)` is macOS 12+, so we draw
/// the hairline ourselves for the 11.0 deployment target.
private struct Hairline: View {
    let color: Color
    var body: some View {
        Rectangle().fill(color).frame(height: 1)
    }
}

// MARK: - Small formatters

private enum Duration {
    /// "1:12:40" / "4:05" from seconds (tnum-friendly, no localization).
    static func human(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        let h = total / 3600, m = (total % 3600) / 60, s = total % 60
        return h > 0
            ? String(format: "%d:%02d:%02d", h, m, s)
            : String(format: "%d:%02d", m, s)
    }

    /// Coarse, human total for headers/estimate: "14 ч 20 мин" / "47 мин".
    static func humanCoarse(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        let h = total / 3600, m = (total % 3600) / 60
        if h > 0 { return "\(h) ч \(m) мин" }
        return "\(m) мин"
    }
}

/// Human byte sizes for the estimate: "≈ 1.18 ГБ" / "742 МБ". Decimal (÷1000) to
/// match the brief's "~300 МБ" framing and Finder-style sizes.
private enum ByteSize {
    static func human(_ bytes: Int) -> String {
        let kb = 1000.0, mb = kb * 1000, gb = mb * 1000
        let b = Double(bytes)
        if b >= gb { return String(format: "%.2f ГБ", b / gb) }
        if b >= mb { return String(format: "%.0f МБ", b / mb) }
        if b >= kb { return String(format: "%.0f КБ", b / kb) }
        return "\(bytes) Б"
    }
}

private enum Plural {
    /// Russian plural for "глава": 1 глава / 2 главы / 5 глав.
    static func chapters(_ n: Int) -> String {
        let mod100 = n % 100, mod10 = n % 10
        if mod100 >= 11 && mod100 <= 14 { return "глав" }
        switch mod10 {
        case 1: return "глава"
        case 2, 3, 4: return "главы"
        default: return "глав"
        }
    }

    /// Russian plural for "часть": 1 часть / 2 части / 5 частей.
    static func parts(_ n: Int) -> String {
        let mod100 = n % 100, mod10 = n % 10
        if mod100 >= 11 && mod100 <= 14 { return "частей" }
        switch mod10 {
        case 1: return "часть"
        case 2, 3, 4: return "части"
        default: return "частей"
        }
    }

    /// Russian plural for "файл": 1 файл / 2 файла / 5 файлов (grouping sheet).
    static func files(_ n: Int) -> String {
        let mod100 = n % 100, mod10 = n % 10
        if mod100 >= 11 && mod100 <= 14 { return "файлов" }
        switch mod10 {
        case 1: return "файл"
        case 2, 3, 4: return "файла"
        default: return "файлов"
        }
    }

    /// Verb agreement for "лежит/лежат N файлов" in the sheet sub-line.
    static func lieVerb(_ n: Int) -> String {
        (n % 10 == 1 && n % 100 != 11) ? "лежит" : "лежат"
    }

    /// "N отдельная книга / N отдельные книги / N отдельных книг" — the separate
    /// choice's sub-line ("N … по одному файлу").
    static func separateBooks(_ n: Int) -> String {
        let mod100 = n % 100, mod10 = n % 10
        if mod100 >= 11 && mod100 <= 14 { return "отдельных книг" }
        switch mod10 {
        case 1: return "отдельная книга"
        case 2, 3, 4: return "отдельные книги"
        default: return "отдельных книг"
        }
    }
}

// MARK: - FileChipsFlow — wrapping row of file-name chips (grouping sheet)

/// A wrapping flow of the loose-file name chips for the grouping sheet's
/// file-strip. macOS-11-safe (no SwiftUI `Layout`, which is 13+).
///
/// Layout is **deterministic and self-reserving**: chips are packed into rows at
/// build time by measuring each chip's width synchronously (`NSAttributedString`,
/// AppKit), then rendered as a `VStack` of `HStack` rows. The `VStack`'s height is
/// the sum of its row heights — there is no measured-height feedback loop and no
/// `GeometryReader` for height, so the strip can never collapse onto the head
/// (that *was* the bug: a wrapping `ZStack` of `alignmentGuide`-positioned children
/// does not self-size to the flow — it takes the height of its single tallest child,
/// so multi-row content overlapped). Height here is reserved by construction.
/// Renders up to `maxChips` names + a "+N" overflow tag.
private struct FileChipsFlow: View {
    let names: [String]
    let maxChips: Int
    let spacing: CGFloat

    /// Content width budget for one row, in points. The sheet is 440 wide
    /// (spec §6); the strip strips 24·2 outer margin + 12·2 inner padding, leaving
    /// 440 − 48 − 24 = 368. A 1pt safety epsilon absorbs measure/render drift so a
    /// chip is never packed flush to the edge and forced to truncate. Fixed (not
    /// read at runtime) so the very first frame already wraps correctly — no jump.
    private static let rowWidth: CGFloat = 368 - 1

    /// The chip strings to render: the shown names plus a synthetic "+N" tag.
    private var items: [(text: String, more: Bool)] {
        let shown = Array(names.prefix(maxChips))
        var out: [(String, Bool)] = shown.map { ($0, false) }
        let extra = names.count - shown.count
        if extra > 0 { out.append(("+\(extra)", true)) }
        return out
    }

    var body: some View {
        // VStack height = Σ row heights — reserved by construction, no collapse.
        VStack(alignment: .leading, spacing: spacing) {
            ForEach(Array(packedRows().enumerated()), id: \.offset) { _, row in
                HStack(spacing: spacing) {
                    ForEach(Array(row.enumerated()), id: \.offset) { _, item in
                        chip(item.text, more: item.more)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Greedy first-fit packing of `items` into rows that fit `rowWidth`. Widths are
    /// measured synchronously with the chips' real rendering metrics, so the packed
    /// rows match what SwiftUI draws. A chip wider than a whole row gets its own row
    /// (SwiftUI then truncates within it — preferable to overlap). Pure build-time
    /// computation: no async, no GeometryReader, no `@State`.
    private func packedRows() -> [[(text: String, more: Bool)]] {
        var rows: [[(text: String, more: Bool)]] = []
        var current: [(text: String, more: Bool)] = []
        var used: CGFloat = 0
        for item in items {
            let w = chipWidth(item.text, more: item.more)
            // Width this chip would add: its own width, plus inter-chip spacing if
            // it isn't the first on the row.
            let add = current.isEmpty ? w : w + spacing
            if !current.isEmpty && used + add > Self.rowWidth {
                rows.append(current)
                current = [item]
                used = w
            } else {
                current.append(item)
                used += add
            }
        }
        if !current.isEmpty { rows.append(current) }
        return rows
    }

    /// The on-screen width of a chip, measured with the exact font SwiftUI renders
    /// (`.system(size: 11.5, design: .monospaced)` → `NSFont.monospacedSystemFont`),
    /// plus the chip's own chrome: 8pt horizontal padding each side + 1pt border
    /// each side. The "+N" more-tag has no padding/border (matches `chip(_:more:)`).
    private func chipWidth(_ text: String, more: Bool) -> CGFloat {
        let font: NSFont = more
            ? .systemFont(ofSize: 11.5)
            : .monospacedSystemFont(ofSize: 11.5, weight: .regular)
        let textW = (text as NSString)
            .size(withAttributes: [.font: font])
            .width
        // ceil so we never under-budget by a sub-pixel and overflow the row.
        let chrome: CGFloat = more ? 0 : (8 * 2 + 1 * 2)   // padding + border
        return ceil(textW) + chrome
    }

    @ViewBuilder
    private func chip(_ text: String, more: Bool) -> some View {
        if more {
            Text(text)
                .font(.system(size: 11.5))
                .foregroundColor(Tokens.C.textTertiary)        // file-more #6E8390
        } else {
            Text(text)
                .font(.system(size: 11.5, design: .monospaced))
                .foregroundColor(Tokens.C.fileStripText)
                .lineLimit(1)
                .padding(.horizontal, 8)
                .padding(.vertical, 3)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.fileChip, style: .continuous)
                        .fill(Tokens.C.surfaceControl)          // rgba(255,255,255,.05)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Tokens.R.fileChip, style: .continuous)
                        .stroke(Tokens.C.borderFieldInput, lineWidth: 1)
                )
        }
    }
}

// MARK: - App delegate (AppKit lifecycle + live refresh)

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var hosting: NSHostingView<RootView>!
    private let store = StateStore()
    private lazy var model = ReaderModel(store: store)
    private lazy var engine = EngineClient(store: store)

    /// Directory watch on `state/` (the agent rewrites state.json via tmp→rename,
    /// swapping the inode — so we watch the DIRECTORY, not the file).
    private var stateWatcher: DispatchSourceFileSystemObject?
    private var watchDebounce: DispatchWorkItem?
    private var focusObservers: [NSObjectProtocol] = []

    /// Rising-edge baseline: the set of pending-confirm book ids last seen. A new
    /// id appearing flips us to "raise the window". Seeded at launch from the
    /// initial read so an agent-launched "already pending" case doesn't trigger a
    /// redundant raise on top of the launch-time NSApp.activate.
    private var lastPendingIDs: Set<String> = []
    /// Same rising-edge baseline for loose-mp3 grouping prompts (a new group id is
    /// just as much a reason to surface the window as a new pending book).
    private var lastGroupIDs: Set<String> = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Standard main menu FIRST (independent of the landing screen): without it
        // a bare-NSApplication app has no "Quit" item, so Cmd+Q does nothing and
        // Cmd+C/V/X/A/Z are dead in text fields (no Edit menu).
        installMainMenu()
        // Initial read BEFORE building the view so the window opens at the right size.
        model.refresh()
        lastPendingIDs = Set(model.state.pendingConfirm.map { $0.bookID })
        lastGroupIDs = Set(model.state.pendingGroups.map { $0.groupID })
        // Landing screen. Three cases, in order:
        //   1) FIRST LAUNCH (agent not installed) → Setup (spec §01): the only GUI
        //      path that installs the background agent.
        //   2) INSTALLED but STALE (the bundled agent is newer than the staged one —
        //      the update-app-but-not-agent bug) → the brief `.updating` screen while
        //      we re-run the bundled installer with the CURRENT watch folder, then the
        //      normal landing. Only when we can determine that folder (never silently
        //      re-point to the default) AND the freshness verdict is `.outdated`.
        //   3) INSTALLED and current (or undecidable, e.g. a dev run with no bundled
        //      agent) → the normal landing (D8): a pending book's confirm window, else
        //      Status (home). A pending grouping prompt overlays the landing screen.
        if !isAgentInstalled() {
            model.screen = .setup
        } else if shouldAutoUpdateAgent() {
            model.screen = .updating
            model.agentUpdatePhase = .running
        } else {
            model.screen = model.manifest != nil ? .confirm : .status
        }

        let root = RootView(
            model: model,
            onBuild: { [weak self] manifest, params, coverID, coverCustomPath in
                self?.handleBuild(manifest, params: params,
                                  coverID: coverID, coverCustomPath: coverCustomPath) ?? false
            },
            onGroupingChoice: { [weak self] group, choice in
                self?.handleGroupingChoice(group, choice: choice) ?? false
            },
            onCancel: { [weak self] book in
                self?.handleCancel(book) ?? false
            },
            onReconvert: { [weak self] book in
                self?.handleReconvert(book) ?? false
            },
            onSkip: { [weak self] book in
                self?.handleSkip(book) ?? false
            },
            navigate: { [weak self] screen in self?.navigate(to: screen) },
            reveal: { [weak self] path in self?.reveal(path) },
            onInstalled: { [weak self] in self?.handleInstalled() },
            onClearHistory: { [weak self] in self?.handleClearHistory() },
            recentClearedAt: { [weak self] in self?.store.recentClearedAt() },
            onResetStats: { [weak self] in self?.handleResetStats() },
            onOpenGitHub: { [weak self] in self?.handleOpenGitHub() },
            onRetryAgentUpdate: { [weak self] in self?.startAgentAutoUpdate() },
            agentFreshness: { [weak self] in
                guard let self = self else { return .undecidable }
                return AgentUpdate.freshness(store: self.store)
            })
        let hosting = NSHostingView(rootView: root)
        self.hosting = hosting

        let fitting = hosting.fittingSize
        let contentSize = NSSize(width: currentScreenWidth,
                                 height: cappedContentHeight(fitting.height))

        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: contentSize),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "mp3-to-m4b"
        window.contentView = hosting
        window.isReleasedWhenClosed = false

        // Width tracks the active screen (confirm 640 / queue 400); height tracks
        // content (capped to the screen by refitWindowHeight). Width is pinned
        // (min==max) per screen so the user can't drag it off the design.
        window.minSize = NSSize(width: currentScreenWidth, height: 200)
        window.maxSize = NSSize(width: currentScreenWidth, height: .greatestFiniteMagnitude)

        window.appearance = NSAppearance(named: .darkAqua)
        window.center()
        window.makeKeyAndOrderFront(nil)
        self.window = window

        NSApp.activate(ignoringOtherApps: true)

        installFocusObservers()
        startStateWatcher()
        refitWindowHeight()

        // If we landed on the auto-update screen, kick off the re-install now (off the
        // main thread; the phase flips the view live). Done AFTER the window is up so
        // the "Обновляю…" card is visible while the installer runs.
        if model.screen == .updating {
            startAgentAutoUpdate()
        }
    }

    // MARK: First-launch detection (show Setup when the agent isn't installed)

    /// Whether the background agent has been installed by installer.sh. We treat
    /// the agent as installed if EITHER signal is present:
    ///   · the LaunchAgent plist exists (the normal install path writes it — the
    ///     label honors $MP3TOM4B_LABEL exactly like installer.sh), OR
    ///   · the staged engine exists under App Support (bin/agent/__main__.py) —
    ///     this is what a `MP3TOM4B_NO_LAUNCHCTL=1` test install leaves behind, and
    ///     it also covers a launchd plist that was manually removed but a live tree.
    /// Both checks honor the SAME env overrides as the installer/agent
    /// (MP3TOM4B_LABEL, MP3TOM4B_SUPPORT_DIR via the StateStore), so a scratch-HOME
    /// test run is recognized without touching the real system. A clean machine
    /// (no plist, no tree) → Setup; an installed machine → the normal flow.
    private func isAgentInstalled() -> Bool {
        let fm = FileManager.default
        // 1) LaunchAgent plist (the real install marker).
        let label = ProcessInfo.processInfo.environment["MP3TOM4B_LABEL"]
            ?? "com.arrivarus.mp3tom4b.agent"
        let plist = URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
            .appendingPathComponent("Library/LaunchAgents/\(label).plist")
        if fm.fileExists(atPath: plist.path) { return true }
        // 2) Staged engine under App Support (NO_LAUNCHCTL test installs, or a
        //    plist that was removed while the tree remains).
        let agentMain = store.supportRoot
            .appendingPathComponent("bin/agent/__main__.py")
        if fm.fileExists(atPath: agentMain.path) { return true }
        return false
    }

    // MARK: Agent auto-update (staged agent behind the bundled one → re-install)

    /// Whether to AUTO-UPDATE the staged agent at launch. True iff ALL hold:
    ///   · the bundled agent is NEWER than the staged one (AgentUpdate.freshness ==
    ///     .outdated — a byte-level `*.py` mismatch; `.undecidable` on a dev run with
    ///     no bundled agent does NOT trigger, so we never wrongly reinstall), AND
    ///   · we can determine the CURRENT watch folder (state.json → plist). Without it
    ///     an installer run would default to ~/Desktop/mp3-to-m4b and silently
    ///     RE-POINT the agent — the brief forbids that; the user can still use the
    ///     explicit Settings button, which resolves the folder the same way.
    /// Callers guard on `isAgentInstalled()` first (auto-update only makes sense for a
    /// non-first launch), so this does not re-check that.
    private func shouldAutoUpdateAgent() -> Bool {
        guard AgentUpdate.freshness(store: store) == .outdated else { return false }
        return currentWatchDir() != nil
    }

    /// The folder the agent is CURRENTLY watching, resolved WITHOUT falling back to
    /// the default (a re-install must keep the user's folder, brief §2):
    ///   1) state.json `agent.watch_dir` (the agent stamps it on every write), then
    ///   2) the LaunchAgent plist's `EnvironmentVariables.MP3TOM4B_WATCH_DIR` (survives
    ///      even if state.json is absent/stale), else
    ///   3) nil — we don't know, so we must NOT auto-run the installer.
    /// Honors MP3TOM4B_LABEL (plist path) + MP3TOM4B_SUPPORT_DIR (via the store), so a
    /// scratch-tree test resolves its own folder and never the real one.
    private func currentWatchDir() -> String? {
        // 1) state.json
        let fromState = model.state.agent.watchDir
        if let d = fromState, !d.isEmpty { return d }
        // 2) LaunchAgent plist EnvironmentVariables.MP3TOM4B_WATCH_DIR
        let label = ProcessInfo.processInfo.environment["MP3TOM4B_LABEL"]
            ?? "com.arrivarus.mp3tom4b.agent"
        let plist = URL(fileURLWithPath: NSHomeDirectory(), isDirectory: true)
            .appendingPathComponent("Library/LaunchAgents/\(label).plist")
        if let data = try? Data(contentsOf: plist),
           let obj = try? PropertyListSerialization.propertyList(from: data, options: [], format: nil),
           let dict = obj as? [String: Any],
           let env = dict["EnvironmentVariables"] as? [String: Any],
           let watch = env["MP3TOM4B_WATCH_DIR"] as? String,
           !watch.isEmpty {
            return watch
        }
        return nil   // unknown → caller must not auto-run the installer
    }

    /// Run the bundled installer to REFRESH the stale staged agent in place, keeping
    /// the current watch folder (never the default) and preserving FDA (the runner.sh
    /// path is unchanged). Off the main thread; the phase drives the `.updating` card.
    /// On success → the normal landing (handleInstalled). On failure → `.failed(msg)`
    /// so the card shows the honest stderr + a "Повторить" button (never a hard block:
    /// the user can retry, and a further launch will retry the detection anyway).
    /// Also the fallback path for the Settings "Обновить агент" button (extraEnv +
    /// completion let that caller drive its own UI).
    private func startAgentAutoUpdate() {
        guard let installer = InstallRunner.bundledInstallerPath() else {
            model.agentUpdatePhase = .failed("Установщик не найден в приложении (пересоберите .app).")
            return
        }
        guard let dir = currentWatchDir() else {
            // We reached here without a known folder — bail to the normal landing
            // rather than risk a silent re-point. (shouldAutoUpdateAgent guards this
            // for the launch path; this covers a manual retry after state changed.)
            model.agentUpdatePhase = .failed("Не удалось определить отслеживаемую папку — обновите через Настройки.")
            return
        }
        model.agentUpdatePhase = .running
        if model.screen != .updating { navigate(to: .updating) }
        let env = agentInstallerExtraEnv
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let result = InstallRunner.run(installerPath: installer, watchDir: dir, extraEnv: env)
            DispatchQueue.main.async {
                guard let self = self else { return }
                self.model.agentUpdatePhase = result
                if case .done = result {
                    // Engine refreshed → proceed exactly like a fresh install (re-read,
                    // re-arm the watcher, land on Status / a pending book's confirm).
                    self.handleInstalled()
                }
                // On .failed we STAY on the `.updating` screen (the card shows the
                // error + "Повторить"); the agent is untouched-or-old but the app is
                // not bricked. refit so the taller error card fits.
                self.hosting?.layoutSubtreeIfNeeded()
                self.refitWindowHeight()
            }
        }
    }

    /// Extra env for the auto-update installer run. Empty in production (a real update
    /// touches the live launchd agent). A test seam can inject NO_LAUNCHCTL/NO_VENV +
    /// a scratch LABEL/SUPPORT so a run never touches the real system.
    var agentInstallerExtraEnv: [String: String] = [:]

    /// Called by the Setup screen after a successful install (the agent is now
    /// live). Flip to the normal landing (Status, or a freshly-pending book's
    /// confirm window), (re)arm the state watcher against the now-existing tree,
    /// and refit. Idempotent — safe if the watcher was already running.
    private func handleInstalled() {
        model.refresh()
        lastPendingIDs = Set(model.state.pendingConfirm.map { $0.bookID })
        lastGroupIDs = Set(model.state.pendingGroups.map { $0.groupID })
        let target: Screen = model.manifest != nil ? .confirm : .status
        navigate(to: target)
        startStateWatcher()
        bringWindowForward()
    }

    // MARK: Live refresh

    /// Absolute path to the agent's state directory (our change signal).
    private var stateDirPath: String { store.stateDir.path }

    private func startStateWatcher() {
        stopStateWatcher()
        let fd = open(stateDirPath, O_EVTONLY)
        guard fd >= 0 else {
            // Dir not there yet (no scan run) — focus observers keep us fresh; a
            // later refresh re-arms once the agent creates it.
            return
        }
        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .delete, .rename, .extend],
            queue: DispatchQueue.global(qos: .utility)
        )
        // Coalesce a burst (a scan writes several files) into one refresh ~150ms
        // after the last event, then hop to main to mutate the model.
        source.setEventHandler { [weak self] in
            guard let self = self else { return }
            self.watchDebounce?.cancel()
            let work = DispatchWorkItem { [weak self] in
                DispatchQueue.main.async { self?.refreshNow() }
            }
            self.watchDebounce = work
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 0.15, execute: work)
        }
        source.setCancelHandler { close(fd) }
        source.resume()
        stateWatcher = source
    }

    private func stopStateWatcher() {
        watchDebounce?.cancel(); watchDebounce = nil
        stateWatcher?.cancel(); stateWatcher = nil
    }

    private func installFocusObservers() {
        let nc = NotificationCenter.default
        let becameKey = nc.addObserver(
            forName: NSWindow.didBecomeKeyNotification, object: window, queue: .main
        ) { [weak self] _ in self?.refreshNow() }
        let becameActive = nc.addObserver(
            forName: NSApplication.didBecomeActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.refreshNow() }
        focusObservers = [becameKey, becameActive]
    }

    /// Re-read state + manifest, raise the window if a NEW pending book appeared
    /// (rising edge), then refit the window height. Main thread only.
    private func refreshNow() {
        // While the Setup or auto-update screen is up, a focus/activate event must NOT
        // yank the user off it — Setup is left only via the explicit install handoff
        // (handleInstalled), and `.updating` is left only when the re-install finishes
        // (also handleInstalled) or on a retry. Just refit and bail.
        if model.screen == .setup || model.screen == .updating {
            hosting?.layoutSubtreeIfNeeded()
            refitWindowHeight()
            return
        }

        model.refresh()

        let nowPending = Set(model.state.pendingConfirm.map { $0.bookID })
        let nowGroups = Set(model.state.pendingGroups.map { $0.groupID })
        let hasActive = !model.state.activeBooks.isEmpty
        // Re-arm the watcher if the dir only just appeared.
        if stateWatcher == nil { startStateWatcher() }

        // Surface on the rising edge of EITHER a new pending book or a new loose-mp3
        // grouping prompt (the user must decide grouping before those books exist).
        let newPending = !nowPending.subtracting(lastPendingIDs).isEmpty
        let newGroup = !nowGroups.subtracting(lastGroupIDs).isEmpty
        if newPending || newGroup {
            // A new book to confirm → bring its confirm window to the front (a new
            // grouping prompt raises the window too; the modal sheet overlays
            // whatever screen is showing, so we don't force a screen change for it).
            // Drop any hand-pick from the queue FIRST: an auto-surface must land on
            // the fresh queue (first active book), not stay parked on a book the
            // user opened by hand earlier.
            if newPending {
                model.clearSelection()
                navigate(to: .confirm)
            }
            bringWindowForward()
        } else if !hasActive && model.screen == .confirm {
            // The active book cleared while we were on the confirm window (the build
            // finished / was skipped and nothing else is pending) → fall back to the
            // Status home (D8), not a stale/empty confirm view.
            navigate(to: .status)
        }
        lastPendingIDs = nowPending
        lastGroupIDs = nowGroups

        hosting?.layoutSubtreeIfNeeded()
        refitWindowHeight()
    }

    /// Bring the already-running window forward (rising edge of a new pending book).
    private func bringWindowForward() {
        guard let window = window else { return }
        window.deminiaturize(nil)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Re-fit the window height to the content's fitting size (width stays fixed),
    /// CAPPED to the screen's visible frame (spec §1 hard rule / sibling lesson
    /// [[native-window-cap-height-test-max-content]]). On the confirm window the
    /// body is wrapped in a single ScrollView (see ConfirmView.body): when a long
    /// book makes the natural fitting height exceed the screen, this cap shrinks the
    /// window and that ScrollView absorbs the overflow (its content scrolls) while
    /// the fixed header and pinned footer stay on-screen — so the «Собрать» action
    /// is never pushed off the bottom, at any book length or display height.
    private func refitWindowHeight() {
        guard let window = window, let hosting = hosting else { return }
        // `fittingSize` and `cappedContentHeight` are CONTENT-view heights. The
        // window FRAME is content + titlebar (~32pt). Convert content→frame via
        // AppKit before comparing/setting, else we'd assign a content-sized value
        // to the frame and lose the titlebar's worth of content off the bottom —
        // the exact clip that pushed the last list row under the footer at EVERY
        // book count (a constant 32pt frame/content unit mismatch, independent of
        // content). frameRect(forContentRect:) is the single source of truth for
        // the chrome inset, so this stays correct if the style mask changes.
        let contentHeight = cappedContentHeight(hosting.fittingSize.height)
        let contentRect = NSRect(x: 0, y: 0, width: currentScreenWidth, height: contentHeight)
        let newFrameHeight = window.frameRect(forContentRect: contentRect).height
        guard abs(window.frame.height - newFrameHeight) > 0.5 else { return }
        // Keep the top-left corner pinned while the height changes.
        var frame = window.frame
        let delta = newFrameHeight - frame.height
        frame.origin.y -= delta
        frame.size.height = newFrameHeight
        // …but keep the whole window ON SCREEN. `cappedContentHeight` bounds the
        // HEIGHT, not the POSITION: a window parked low grows DOWNWARD from its
        // pinned top, so a fitting-but-lower window still slides its footer —
        // «Собрать» — under the bottom edge of the screen (measured up to 357pt on
        // a long book). Only the VERTICAL axis is touched here, and only because
        // WE just changed the height: a window the user dragged low himself never
        // reaches this line (the guard above returns first). Rule + edge priority
        // live in WindowGeometry (unit-checked); this is the only caller for Y.
        if let visible = (window.screen ?? NSScreen.main)?.visibleFrame {
            frame = WindowGeometry.clampedVertically(frame, in: visible)
        }
        window.setFrame(frame, display: true, animate: false)
    }

    /// Clamp a desired CONTENT height to [200, max-content-that-fits-on-screen].
    /// Uses the window's current screen (falls back to main). The max is derived
    /// from the screen's visible frame by subtracting the REAL window chrome inset
    /// (titlebar) via `contentRect(forFrameRect:)` — not a hardcoded titlebar
    /// guess — so the whole window (titlebar + content) provably fits inside the
    /// visible frame after refitWindowHeight converts this back to a frame height.
    private func cappedContentHeight(_ desired: CGFloat) -> CGFloat {
        let screen = window?.screen ?? NSScreen.main
        let visible = screen?.visibleFrame.height ?? 900
        // Largest window FRAME allowed on this screen (small margin off the visible
        // frame), then convert that frame height to the content it can hold.
        let maxFrame = NSRect(x: 0, y: 0, width: currentScreenWidth, height: max(200, visible - 8))
        let maxContent: CGFloat
        if let window = window {
            maxContent = max(200, window.contentRect(forFrameRect: maxFrame).height)
        } else {
            // Pre-window (initial sizing): approximate the titlebar (~28pt).
            maxContent = max(200, visible - 8 - 28)
        }
        return min(max(200, desired), maxContent)
    }

    // MARK: Navigation (single window ↔ confirm / queue)

    /// The window width for the current screen (spec §2): the confirm flow is 640,
    /// status/queue/settings are 400. Read off `model.screen` so launch + refit pick
    /// the right width.
    private var currentScreenWidth: CGFloat { model.screen.windowWidth }

    /// Switch the single window to `screen`: flip the model (SwiftUI re-lays-out the
    /// content to the new width via RootView.frame), then resize the NSWindow to that
    /// screen's pinned width and refit its height. Main thread only.
    private func navigate(to screen: Screen) {
        guard model.screen != screen else { return }
        model.screen = screen
        applyWindowWidth()
        // Let SwiftUI lay out at the new width before measuring the fitting height.
        hosting?.layoutSubtreeIfNeeded()
        refitWindowHeight()
    }

    /// Pin the window to `currentScreenWidth` (min==max so it can't be dragged off
    /// the design). minSize/maxSize are updated FIRST so AppKit doesn't clamp the new
    /// frame to the old width's range; the top-left corner stays put.
    private func applyWindowWidth() {
        guard let window = window else { return }
        let w = currentScreenWidth
        window.minSize = NSSize(width: w, height: 200)
        window.maxSize = NSSize(width: w, height: .greatestFiniteMagnitude)
        guard abs(window.frame.width - w) > 0.5 else { return }
        var frame = window.frame
        frame.size.width = w               // top-left pinned (origin.x unchanged)
        // …but keep the whole window ON SCREEN. AppKit does NOT constrain a frame
        // horizontally (measured: a 400-wide window parked at the right edge keeps
        // its origin and hangs 240pt past it when the confirm window widens it to
        // 640) — and that overhang is exactly where the footer's «Собрать» sits, so
        // the primary action would be unclickable. Only the HORIZONTAL axis is
        // touched here — the axis this method just changed; the height refit owns Y.
        // Rule + edge priority live in WindowGeometry (unit-checked).
        if let visible = (window.screen ?? NSScreen.main)?.visibleFrame {
            frame = WindowGeometry.clampedHorizontally(frame, in: visible)
        }
        window.setFrame(frame, display: true, animate: false)
    }

    /// Reveal an absolute path in Finder (the queue's "Открыть" / "Открыть папку").
    /// Selects the item in its containing folder (a file) or opens the folder
    /// itself. Guards a missing path so a stale manifest never throws.
    private func reveal(_ path: String) {
        guard !path.isEmpty else { return }
        let url = URL(fileURLWithPath: path)
        if FileManager.default.fileExists(atPath: path) {
            NSWorkspace.shared.activateFileViewerSelecting([url])
        } else {
            // The exact item is gone (moved/deleted) — fall back to its parent dir
            // if that still exists, so the action is never a silent no-op.
            let parent = url.deletingLastPathComponent()
            if FileManager.default.fileExists(atPath: parent.path) {
                NSWorkspace.shared.activateFileViewerSelecting([parent])
            }
        }
    }

    // MARK: Actions

    /// "Собрать": drop a `confirm-build` command into queue/commands/ for the agent
    /// to validate + run, carrying the user's EDITED params (bitrate/channels/
    /// samplerate/split). Returns true on a successful write so the confirm view can
    /// show its "Отправлено…" ack. The app does NOT change the book's status — the
    /// agent owns that (the status flip clears this view via the rising-edge
    /// watcher). On failure we log and return false so the button re-enables.
    @discardableResult
    private func handleBuild(_ manifest: BookManifest, params: BookParams,
                            coverID: String? = nil,
                            coverCustomPath: String? = nil) -> Bool {
        do {
            let url = try engine.writeConfirmBuild(
                manifest: manifest, params: params,
                coverID: coverID, coverCustomPath: coverCustomPath)
            print("[build] confirm-build dropped: \(url.lastPathComponent) "
                + "book_id=\(manifest.bookID) "
                + "source_rev=\(manifest.sourceRev.prefix(8))… "
                + "params=\(params.bitrate)k/\(params.channels)/\(params.samplerate.map(String.init) ?? "source")/split=\(params.split)/mode=\(params.buildMode) "
                + "cover_id=\(coverID ?? "—") cover_custom=\(coverCustomPath != nil ? "yes" : "—")")
            return true
        } catch {
            NSLog("[build] confirm-build write FAILED for book_id=%@: %@",
                  manifest.bookID, String(describing: error))
            return false
        }
    }

    /// "Продолжить" on the grouping sheet: drop a `grouping-choice` command for the
    /// agent to validate (rev+token vs. the live pending group) and materialize 1
    /// (combine) / N (separate) book manifests. Returns true on a successful write so
    /// the sheet shows its ack. The app does NOT touch state — the agent removes the
    /// group and arms the new books, which clears the sheet via the watcher.
    @discardableResult
    private func handleGroupingChoice(_ group: PendingGroup,
                                      choice: EngineClient.GroupingChoice) -> Bool {
        do {
            let url = try engine.writeGroupingChoice(group: group, choice: choice)
            print("[grouping] grouping-choice dropped: \(url.lastPathComponent) "
                + "group_id=\(group.groupID) rev=\(group.rev.prefix(8))… "
                + "choice=\(choice.rawValue) files=\(group.count)")
            return true
        } catch {
            NSLog("[grouping] grouping-choice write FAILED for group_id=%@: %@",
                  group.groupID, String(describing: error))
            return false
        }
    }

    /// "Отмена" on a converting queue row: drop a `cancel` command for the book (D13).
    /// Returns true on a successful write so the row shows its "Отправлено…" ack. The
    /// app does NOT change status — the building agent kills ffmpeg, sweeps the temp,
    /// and lands the book back at pending-confirm (cancel ≠ failure); the status flip
    /// clears the converting row via the state watcher. A cancel for a book no longer
    /// converting is moot (agent-side). On failure we log and return false.
    @discardableResult
    private func handleCancel(_ book: BookSummary) -> Bool {
        do {
            let url = try engine.writeCancel(bookID: book.bookID)
            print("[cancel] cancel dropped: \(url.lastPathComponent) "
                + "book_id=\(book.bookID)")
            return true
        } catch {
            NSLog("[cancel] cancel write FAILED for book_id=%@: %@",
                  book.bookID, String(describing: error))
            return false
        }
    }

    /// "Собрать заново" on a done queue row: drop a `reconvert` command for the book.
    /// Returns true on a successful write so the row shows its "Отправлено…" ack. The
    /// app does NOT change status — the agent re-arms the book `done` → pending-confirm
    /// (fresh confirm_token + cleared idempotency ledger so the same-source rebuild is
    /// not deduped); the status flip moves it out of ГОТОВО and surfaces the confirm
    /// window via the state watcher, where the user presses «Собрать» to rebuild. A
    /// reconvert for a book no longer `done` is a no-op (agent-side). On failure we log
    /// and return false.
    @discardableResult
    private func handleReconvert(_ book: BookSummary) -> Bool {
        do {
            let url = try engine.writeReconvert(bookID: book.bookID)
            print("[reconvert] reconvert dropped: \(url.lastPathComponent) "
                + "book_id=\(book.bookID)")
            return true
        } catch {
            NSLog("[reconvert] reconvert write FAILED for book_id=%@: %@",
                  book.bookID, String(describing: error))
            return false
        }
    }

    /// «Пропустить» in the confirm footer: drop a `skip` command for the book.
    /// Returns true on a successful write so the button shows its ack. The app does
    /// NOT change status (the agent owns it — D13): the agent marks the manifest
    /// `skipped`, the SOURCES ARE NEVER TOUCHED, and the status flip moves the book
    /// out of ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ into the queue's ПРОПУЩЕНО section via the state
    /// watcher (so it is never "just gone"). Reversible: «Вернуть» on that row, or a
    /// conscious re-drop of the folder. On failure we log and return false.
    @discardableResult
    private func handleSkip(_ book: BookSummary) -> Bool {
        do {
            let url = try engine.writeSkip(bookID: book.bookID)
            print("[skip] skip dropped: \(url.lastPathComponent) "
                + "book_id=\(book.bookID)")
            return true
        } catch {
            NSLog("[skip] skip write FAILED for book_id=%@: %@",
                  book.bookID, String(describing: error))
            return false
        }
    }

    /// "Очистить" the Status recent-built list. Stamps the app-owned
    /// recent-cleared-at marker (EngineClient+Status) — state.json is NEVER rewritten
    /// (the agent owns it — D13). Then re-read + refit so the list re-filters
    /// immediately. Ported from fb2's clear-history wiring.
    private func handleClearHistory() {
        engine.clearHistory()
        model.refresh()
        hosting?.layoutSubtreeIfNeeded()
        refitWindowHeight()
    }

    /// "Сбросить статистику" (Настройки, danger action). Captures the current raw
    /// counters as app-owned baselines + clears the recent list (EngineClient+Status)
    /// — again WITHOUT touching state.json. Then re-read + refit so the cards read
    /// zero immediately. Ported from fb2's reset-stats wiring.
    private func handleResetStats() {
        engine.resetStats()
        model.refresh()
        hosting?.layoutSubtreeIfNeeded()
        refitWindowHeight()
    }

    /// "Открыть на GitHub" (Настройки version card + Status credit link). Opens the
    /// project repo in the default browser. We have NO UpdateChecker (unlike fb2) —
    /// this is a plain link, no auto-update machinery.
    private func handleOpenGitHub() {
        guard let url = URL(string: Tokens.githubURL) else { return }
        NSWorkspace.shared.open(url)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopStateWatcher()
        focusObservers.forEach { NotificationCenter.default.removeObserver($0) }
    }

    // MARK: Main menu (Cmd+Q, clipboard shortcuts)

    /// Builds the standard AppKit main menu. All actions use standard selectors
    /// with target = nil so they resolve via the responder chain (active text
    /// field / key window / NSApp).
    private func installMainMenu() {
        let mainMenu = NSMenu()

        // Application menu
        let appMenuItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(
            title: "О программе mp3-to-m4b",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(
            title: "Скрыть mp3-to-m4b",
            action: #selector(NSApplication.hide(_:)),
            keyEquivalent: "h"))
        let hideOthers = NSMenuItem(
            title: "Скрыть остальные",
            action: #selector(NSApplication.hideOtherApplications(_:)),
            keyEquivalent: "h")
        hideOthers.keyEquivalentModifierMask = [.command, .option]
        appMenu.addItem(hideOthers)
        appMenu.addItem(NSMenuItem(
            title: "Показать все",
            action: #selector(NSApplication.unhideAllApplications(_:)),
            keyEquivalent: ""))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(
            title: "Выйти из mp3-to-m4b",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"))
        appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)

        // Edit menu — enables Cmd+C/V/X/A/Z in text fields via the responder chain.
        let editMenuItem = NSMenuItem()
        let editMenu = NSMenu(title: "Правка")
        editMenu.addItem(NSMenuItem(
            title: "Отменить",
            action: Selector(("undo:")),
            keyEquivalent: "z"))
        let redo = NSMenuItem(
            title: "Повторить",
            action: Selector(("redo:")),
            keyEquivalent: "z")
        redo.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(redo)
        editMenu.addItem(.separator())
        editMenu.addItem(NSMenuItem(
            title: "Вырезать",
            action: #selector(NSText.cut(_:)),
            keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(
            title: "Скопировать",
            action: #selector(NSText.copy(_:)),
            keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(
            title: "Вставить",
            action: #selector(NSText.paste(_:)),
            keyEquivalent: "v"))
        editMenu.addItem(NSMenuItem(
            title: "Выбрать всё",
            action: #selector(NSText.selectAll(_:)),
            keyEquivalent: "a"))
        editMenuItem.submenu = editMenu
        mainMenu.addItem(editMenuItem)

        // Window menu
        let windowMenuItem = NSMenuItem()
        let windowMenu = NSMenu(title: "Окно")
        windowMenu.addItem(NSMenuItem(
            title: "Свернуть",
            action: #selector(NSWindow.performMiniaturize(_:)),
            keyEquivalent: "m"))
        windowMenu.addItem(NSMenuItem(
            title: "Закрыть",
            action: #selector(NSWindow.performClose(_:)),
            keyEquivalent: "w"))
        windowMenuItem.submenu = windowMenu
        mainMenu.addItem(windowMenuItem)

        NSApp.mainMenu = mainMenu
        NSApp.windowsMenu = windowMenu
    }
}

// MARK: - Entry point

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
