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

/// Holds the showcase + the manifest for the book we are currently presenting.
/// Mutated only on the main thread (the watcher hops to main before refreshing).
final class ReaderModel: ObservableObject {
    @Published var state: ShowcaseState = .empty
    /// Manifest for the first pending-confirm book, when one exists.
    @Published var manifest: BookManifest?
    /// The summary row backing `manifest` (title/status come from the showcase).
    @Published var book: BookSummary?

    private let store: StateStore

    init(store: StateStore) {
        self.store = store
    }

    /// Re-read state.json and, for the first pending-confirm book, its manifest.
    /// Idle (no pending book) clears `book`/`manifest`.
    func refresh() {
        let s = store.loadState()
        state = s
        if let first = s.pendingConfirm.first {
            book = first
            manifest = store.loadManifest(bookID: first.bookID)
        } else {
            book = nil
            manifest = nil
        }
    }
}

// MARK: - Root view (idle ↔ confirm)

private struct RootView: View {
    @ObservedObject var model: ReaderModel
    /// Writes the `confirm-build` command (M0.4). Returns true on success so the
    /// confirm view can show its visual ack; false → the button surfaces an error.
    let onBuild: (BookManifest) -> Bool

    var body: some View {
        ZStack {
            Tokens.Canvas.windowGradient
            if let manifest = model.manifest, let book = model.book {
                ConfirmView(
                    book: book,
                    manifest: manifest,
                    pendingCount: model.state.pendingConfirm.count,
                    onBuild: { onBuild(manifest) }
                )
                // Reset the per-book ack state when the presented book changes
                // (a different book_id means a different confirm flow).
                .id(manifest.bookID)
            } else {
                IdleView(watchDir: model.state.agent.watchDir)
            }
        }
        .frame(width: Tokens.M.windowConfirm)
    }
}

// MARK: - Idle view (no pending book)

/// Calm empty state when nothing awaits confirmation. Real Status/queue screens
/// land later; this just keeps the window meaningful and not blank.
private struct IdleView: View {
    let watchDir: String?

    var body: some View {
        VStack(spacing: 8) {
            Text("Очередь пуста")
                .font(.system(size: Tokens.F.h1Confirm, weight: .semibold))
                .foregroundColor(Tokens.C.textHigh)
            Text("Нет книг, ожидающих подтверждения.")
                .font(.system(size: Tokens.F.small))
                .foregroundColor(Tokens.C.textSecondary)
            if let dir = watchDir, !dir.isEmpty {
                Text("Отслеживается: \((dir as NSString).lastPathComponent)")
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textTertiary)
                    .padding(.top, 2)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.vertical, 56)
    }
}

// MARK: - Confirm window (minimum, spec §3)

/// First pending-confirm book: header → book title → chapter list → "Собрать".
/// Minimal vs the full §3 layout (no cover/quality/estimate/footer-links yet —
/// those are M1), but rendered from real manifest data with exact tokens.
private struct ConfirmView: View {
    let book: BookSummary
    let manifest: BookManifest
    let pendingCount: Int
    /// Writes the command; returns true on success. Drives the footer's ack.
    let onBuild: () -> Bool

    /// App-side idempotency: once we've successfully dropped a command for this
    /// book, lock the button so a second click can't spawn another command before
    /// the agent flips the status (which clears this view via the rising-edge
    /// machinery). `failed` lets us re-enable + show an error if the write threw.
    @State private var sent = false
    @State private var failed = false

    var body: some View {
        VStack(spacing: 0) {
            header
            Hairline(color: Tokens.C.borderHairline)
            content
            Hairline(color: Tokens.C.borderCard)
            footer
        }
    }

    // header: padding 16 20 14, app-icon 34 (radius 9), h1 16/700 + sub 11, counter.
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

            VStack(alignment: .leading, spacing: 2) {
                Text("Подтверждение книги")
                    .font(.system(size: Tokens.F.h1Confirm, weight: .bold))
                    .foregroundColor(Tokens.C.textHigh)
                Text("проверьте данные и соберите .m4b")
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textSecondary)
            }

            Spacer(minLength: 8)

            if pendingCount > 1 {
                Text("1 из \(pendingCount)")
                    .font(.system(size: Tokens.F.caption).monospacedDigit())
                    .foregroundColor(Tokens.C.textMuted)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 4)
                    .background(
                        RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                            .fill(Color.white(0.04))
                    )
            }
        }
        .padding(.top, 16)
        .padding(.horizontal, 20)
        .padding(.bottom, 14)
    }

    // content: "КНИГА" cap + title, then "ГЛАВЫ" cap + count, then the scrollable
    // chapter list (min 180 / max 470 per spec §1 cap rule).
    private var content: some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 6) {
                cap("КНИГА")
                Text(book.title)
                    .font(.system(size: Tokens.F.body, weight: .semibold))
                    .foregroundColor(Tokens.C.textHigh)
            }

            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .firstTextBaseline) {
                    cap("ГЛАВЫ")
                    Spacer()
                    Text(chapterCountLabel)
                        .font(.system(size: Tokens.F.small).monospacedDigit())
                        .foregroundColor(Tokens.C.textSecondary)
                }
                chapterList
            }
        }
        .padding(.init(top: 18, leading: 20, bottom: 18, trailing: 20))
    }

    private var chapterList: some View {
        ScrollView {
            VStack(spacing: 0) {
                ForEach(Array(manifest.chapters.enumerated()), id: \.element.id) { idx, ch in
                    ChapterRow(chapter: ch)
                    if idx < manifest.chapters.count - 1 {
                        Hairline(color: Tokens.C.borderHairlineFaint)
                            .padding(.horizontal, 14)
                    }
                }
            }
        }
        .frame(minHeight: 180, maxHeight: 470)
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCard)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderCard, lineWidth: 1)
        )
    }

    // footer: padding 14 20, top border.card, bg surfaceFill.footer.
    // States: idle "Собрать" → on success a disabled "Отправлено…" ack (the agent
    // will flip the status and this whole view goes away); on failure a re-enabled
    // "Повторить" + a short error note (write threw — surfaced, not swallowed).
    private var footer: some View {
        HStack(spacing: 10) {
            if failed {
                Text("Не удалось отправить")
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textSecondary)
            }
            Spacer()
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

    // Active "Собрать": writes the command, then locks into the ack on success.
    private var buildButton: some View {
        Button(action: {
            failed = false
            if onBuild() {
                sent = true
            } else {
                failed = true
            }
        }) {
            Text(failed ? "Повторить" : "Собрать")
                .font(.system(size: 14, weight: .semibold))
                .foregroundColor(Tokens.C.textOnAccent)
                .padding(.horizontal, 18)
                .padding(.vertical, 9)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                        .fill(Tokens.Grad.brandButton)
                )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
    }

    // Disabled ack after a successful drop. Muted fill (not the bright brand
    // gradient) reads as "done, waiting" — the status change is the agent's job.
    private var sentAck: some View {
        Text("Отправлено…")
            .font(.system(size: 14, weight: .semibold))
            .foregroundColor(Tokens.C.textSecondary)
            .padding(.horizontal, 18)
            .padding(.vertical, 9)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .fill(Color.white(0.05))
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .stroke(Tokens.C.borderControl, lineWidth: 1)
            )
    }

    // "12 глав · длительности — позже" (durations arrive at M0.5).
    private var chapterCountLabel: String {
        let n = manifest.chapters.count
        let word = Plural.chapters(n)
        let probed = manifest.chapters.contains { $0.durationSeconds != nil }
        if probed {
            let total = manifest.chapters.reduce(0.0) { $0 + ($1.durationSeconds ?? 0) }
            return "\(n) \(word) · \(Duration.human(total))"
        }
        return "\(n) \(word) · длительности — позже"
    }

    private func cap(_ text: String) -> some View {
        Text(text)
            .font(.system(size: Tokens.F.cap, weight: .bold))
            .tracking(1.2)
            .foregroundColor(Tokens.C.textTertiary)
    }
}

/// One chapter row: grid 26 / 1fr / auto — № (quaternary tnum) · name (13 high) ·
/// duration (11.5 secondary tnum, em-dash until M0.5).
private struct ChapterRow: View {
    let chapter: ChapterEntry

    var body: some View {
        HStack(spacing: 10) {
            Text("\(chapter.index)")
                .font(.system(size: Tokens.F.small).monospacedDigit())
                .foregroundColor(Tokens.C.textQuaternary)
                .frame(width: 26, alignment: .leading)
            Text(chapter.name)
                .font(.system(size: Tokens.F.body))
                .foregroundColor(Tokens.C.textHigh)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: 8)
            Text(durationText)
                .font(.system(size: Tokens.F.chDur).monospacedDigit())
                .foregroundColor(Tokens.C.textSecondary)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
    }

    private var durationText: String {
        if let s = chapter.durationSeconds { return Duration.human(s) }
        return "—"
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

    func applicationDidFinishLaunching(_ notification: Notification) {
        // Initial read BEFORE building the view so the window opens at the right size.
        model.refresh()
        lastPendingIDs = Set(model.state.pendingConfirm.map { $0.bookID })

        let root = RootView(model: model, onBuild: { [weak self] manifest in
            self?.handleBuild(manifest) ?? false
        })
        let hosting = NSHostingView(rootView: root)
        self.hosting = hosting

        let fitting = hosting.fittingSize
        let contentSize = NSSize(width: Tokens.M.windowConfirm, height: fitting.height)

        let window = NSWindow(
            contentRect: NSRect(origin: .zero, size: contentSize),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "mp3-to-m4b"
        window.contentView = hosting
        window.isReleasedWhenClosed = false

        // Fixed width; height tracks content (capped by SwiftUI's max on the list).
        window.minSize = NSSize(width: Tokens.M.windowConfirm, height: 200)
        window.maxSize = NSSize(width: Tokens.M.windowConfirm, height: .greatestFiniteMagnitude)

        window.appearance = NSAppearance(named: .darkAqua)
        window.center()
        window.makeKeyAndOrderFront(nil)
        self.window = window

        NSApp.activate(ignoringOtherApps: true)

        installFocusObservers()
        startStateWatcher()
        refitWindowHeight()
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
        model.refresh()

        let nowPending = Set(model.state.pendingConfirm.map { $0.bookID })
        // Re-arm the watcher if the dir only just appeared.
        if stateWatcher == nil { startStateWatcher() }

        if !nowPending.subtracting(lastPendingIDs).isEmpty {
            bringWindowForward()
        }
        lastPendingIDs = nowPending

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

    /// Re-fit the window height to the content's fitting size (width stays fixed).
    private func refitWindowHeight() {
        guard let window = window, let hosting = hosting else { return }
        let fitting = hosting.fittingSize
        let newHeight = max(200, fitting.height)
        guard abs(window.frame.height - newHeight) > 0.5 else { return }
        // Keep the top-left corner pinned while the height changes.
        var frame = window.frame
        let delta = newHeight - frame.height
        frame.origin.y -= delta
        frame.size.height = newHeight
        window.setFrame(frame, display: true, animate: false)
    }

    // MARK: Actions

    /// "Собрать" (M0.4): drop a `confirm-build` command into queue/commands/ for
    /// the agent to validate + run. Returns true on a successful write so the
    /// confirm view can show its "Отправлено…" ack. The app does NOT change the
    /// book's status — the agent owns that (the status flip clears this view via
    /// the rising-edge watcher). On failure we log and return false so the button
    /// re-enables with an error note.
    @discardableResult
    private func handleBuild(_ manifest: BookManifest) -> Bool {
        do {
            let url = try engine.writeConfirmBuild(manifest: manifest)
            print("[M0.4] confirm-build dropped: \(url.lastPathComponent) "
                + "book_id=\(manifest.bookID) "
                + "source_rev=\(manifest.sourceRev.prefix(8))… "
                + "confirm_token=\(manifest.confirmToken.prefix(8))…")
            return true
        } catch {
            NSLog("[M0.4] confirm-build write FAILED for book_id=%@: %@",
                  manifest.bookID, String(describing: error))
            return false
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopStateWatcher()
        focusObservers.forEach { NotificationCenter.default.removeObserver($0) }
    }
}

// MARK: - Entry point

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
