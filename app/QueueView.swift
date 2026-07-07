// QueueView — the "Очередь" screen (07 / spec §7), pixel-mapped to
// design/mockups/07-queue.html (CSS = pixel truth).
//
// DISPLAY + NAVIGATION ONLY (this slice). It projects the agent-owned showcase
// (state.json `books[]` partitioned by status + `batch`) into the four sections
// the spec names — ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ · В РАБОТЕ · ГОТОВО · ОШИБКА — plus the
// batch-chip and the empty state. The app NEVER writes status here; it only reads.
//
// Actions in scope:
//   · pending-confirm → "Подтвердить": open this book's confirm window (navigate).
//   · done            → "Открыть": reveal result.output_path in Finder.
//   · converting      → an indeterminate mini-progress (honest — the agent records
//                       only binary progress; no fake %) + "Отмена": drop a `cancel`
//                       command (D13). The app only WRITES the command; the agent
//                       kills ffmpeg and lands the book back at pending-confirm.
//   · error           → NON-actionable: the reason, disabled (retry deferred, D13).
//
// LAYOUT IS DETERMINISTIC by construction (lesson .patches/002): the sections are a
// plain VStack inside one ScrollView — height flows from content, there is NO
// GeometryReader-measured self-sizing and NO @State height feedback loop, so rows
// can never collapse/overlap. The fixed chrome (header · footer · credit) sits
// OUTSIDE the scroll; the sections ScrollView is the ONLY scrolling part and is
// bounded by a fixed max-height (M.queueListMax) so a long queue scrolls INSIDE its
// viewport rather than pushing the footer off-screen (a bare ScrollView under-reports
// its fitting height — the overlap trap StatusView also documents). The window-height
// cap (AppDelegate) is the outer belt.

import AppKit
import SwiftUI

// MARK: - Queue root

/// The full queue window content (400 wide). Header (back · title · batch-chip) →
/// scrolling sections (or empty state) → footer → credit.
struct QueueView: View {
    let state: ShowcaseState
    /// Per-book manifest lookup (cover preview + result path live there, not in the
    /// lightweight showcase). The host injects a closure backed by the StateStore so
    /// the queue stays a pure reader. Returns nil if a manifest is absent/half-written.
    let manifestFor: (BookSummary) -> BookManifest?
    /// "Подтвердить" on a pending row → present that book's confirm window.
    let onConfirm: (BookSummary) -> Void
    /// "Подтвердить все по очереди" → focus/confirm the first pending book.
    let onConfirmAll: () -> Void
    /// "Открыть" on a done row → reveal the finished .m4b in Finder.
    let onReveal: (BookManifest) -> Void
    /// "Собрать заново" on a done row → drop a `reconvert` command for that book. The
    /// agent re-arms the book back to pending-confirm (fresh token + cleared idempotency
    /// ledger); it leaves ГОТОВО, reappears under ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ, and the confirm
    /// window surfaces (file-watch) → the user presses «Собрать» to rebuild. Returns true
    /// on a successful drop so the row can show a brief ack (mirrors cancel/confirm).
    let onReconvert: (BookSummary) -> Bool
    /// "Отмена" on a converting row → drop a `cancel` command for that book (D13).
    /// The agent kills ffmpeg and lands the book back at pending-confirm; the row
    /// then clears itself via file-watch. Returns true on a successful drop so the
    /// row can show a brief "Отправлено…" ack (mirrors confirm-build's ack).
    let onCancel: (BookSummary) -> Bool
    /// "Открыть папку" (footer) → reveal the watched folder in Finder.
    let onOpenFolder: () -> Void
    /// Back chevron → return to the previous screen (the confirm window / idle).
    let onBack: () -> Void

    private var pending: [BookSummary] { state.pendingConfirm }
    private var converting: [BookSummary] { state.convertingBooks }
    private var done: [BookSummary] { state.doneBooks }
    private var errored: [BookSummary] { state.errorBooks }

    private var isEmpty: Bool {
        pending.isEmpty && converting.isEmpty && done.isEmpty && errored.isEmpty
    }

    var body: some View {
        VStack(spacing: 0) {
            header
            if isEmpty {
                emptyState
            } else {
                sections
            }
            footer
            credit
        }
        .frame(width: Tokens.M.windowStandard)   // 400 (spec §7)
    }

    // MARK: Header — .hdr: padding 16 18 14, back-btn 30 + title + batch-chip.

    private var header: some View {
        HStack(spacing: 11) {
            backButton
            VStack(alignment: .leading, spacing: 1) {
                Text("Очередь")
                    .font(.system(size: Tokens.F.h1Confirm, weight: .bold))
                    .foregroundColor(Tokens.C.textHigh)
                Text(headerSub)
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textSecondary)
            }
            Spacer(minLength: 8)
            if let batch = state.batch, batch.active {
                BatchChip(batch: batch)
            }
        }
        .padding(.init(top: 16, leading: 18, bottom: 14, trailing: 18))
    }

    // Header sub-line mirrors the mockup ("Пачка из N книг" / "Нет активных книг").
    private var headerSub: String {
        let n = state.books.count
        if n == 0 { return "Нет активных книг" }
        return "\(n) \(QueuePlural.books(n)) в очереди"
    }

    // .back-btn 30 r8, border .10, bg .05, chevron-left #9fb2bd.
    private var backButton: some View {
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
    }

    // MARK: Sections — .sec: padding 6 14 0. One ScrollView holds every section so a
    // long queue scrolls and the window-cap keeps the footer visible.

    private var sections: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                // ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ + the dashed confirm-all, then the rows.
                if !pending.isEmpty {
                    sectionCap("ОЖИДАЕТ ПОДТВЕРЖДЕНИЯ", count: pending.count,
                               topGap: 10)
                    if pending.count > 1 {
                        confirmAllButton
                    }
                    ForEach(pending) { book in
                        PendingRow(book: book, cover: manifestFor(book),
                                   onConfirm: { onConfirm(book) })
                    }
                }

                if !converting.isEmpty {
                    sectionCap("В РАБОТЕ", count: converting.count, topGap: topGap(after: !pending.isEmpty))
                    ForEach(converting) { book in
                        ConvertingRow(book: book, cover: manifestFor(book),
                                      onCancel: { onCancel(book) })
                    }
                }

                if !done.isEmpty {
                    sectionCap("ГОТОВО", count: done.count,
                               topGap: topGap(after: !pending.isEmpty || !converting.isEmpty))
                    ForEach(done) { book in
                        DoneRow(book: book, manifest: manifestFor(book),
                                onReveal: onReveal,
                                onReconvert: { onReconvert(book) })
                    }
                }

                if !errored.isEmpty {
                    sectionCap("ОШИБКА", count: errored.count,
                               topGap: topGap(after: !pending.isEmpty || !converting.isEmpty || !done.isEmpty))
                    ForEach(errored) { book in
                        ErrorRow(book: book, manifest: manifestFor(book))
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.top, 6)
            .padding(.bottom, 4)
        }
        // Bound the variable sections list so a long queue (many books across the
        // four sections) scrolls INSIDE this viewport instead of growing the window
        // and pushing the footer/credit off-screen. A bare ScrollView under-reports
        // its fitting height (which let the footer overlap — same trap StatusView
        // documents); the fixed cap gives it an honest bounded height AND internal
        // scroll. A short queue shrinks to fit (the cap is a max, not a min); the
        // AppDelegate window-cap is the outer belt. (.patches/002: a fixed cap, no
        // GeometryReader self-measurement.)
        .frame(maxHeight: Tokens.M.queueListMax)
    }

    /// First section's caps top-margin is 10 (mockup); later sections use 16.
    private func topGap(after previous: Bool) -> CGFloat { previous ? 16 : 10 }

    // .sec-cap: 9px/700 #6E8390 +1.2, with a count pill (.n) on the right gap.
    private func sectionCap(_ text: String, count: Int, topGap: CGFloat) -> some View {
        HStack(spacing: 6) {
            Text(text)
                .font(.system(size: Tokens.F.cap, weight: .bold))
                .tracking(1.2)
                .foregroundColor(Tokens.C.textTertiary)
            Text("\(count)")
                .font(.system(size: Tokens.F.cap, weight: .bold).monospacedDigit())
                .foregroundColor(Tokens.C.textMuted)
                .padding(.horizontal, 7)
                .padding(.vertical, 1)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                        .fill(Tokens.C.secCapPillBg)
                )
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 4)
        .padding(.top, topGap)
        .padding(.bottom, 8)
    }

    // .confirm-all: dashed teal, centered. Opens the first pending book's confirm
    // window (D4: every book is confirmed individually — we do NOT bypass confirm;
    // "по очереди" walks the queue one window at a time, starting at the first).
    private var confirmAllButton: some View {
        Button(action: onConfirmAll) {
            Text("Подтвердить все по очереди →")
                .font(.system(size: Tokens.F.caption, weight: .semibold))
                .foregroundColor(Tokens.C.accentLabel)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 9)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                        .fill(Tokens.C.confirmAllBg)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                        .strokeBorder(Tokens.C.confirmAllBorder,
                                      style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
                )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .padding(.horizontal, 4)
        .padding(.bottom, 10)
    }

    // MARK: Empty state — .empty: padding 60 30, empty-ic 60 + h3 + p.

    private var emptyState: some View {
        VStack(spacing: 0) {
            ZStack {
                RoundedRectangle(cornerRadius: Tokens.R.window, style: .continuous)
                    .fill(Tokens.C.surfaceControlSoft)              // rgba(255,255,255,.04)
                RoundedRectangle(cornerRadius: Tokens.R.window, style: .continuous)
                    .stroke(Tokens.C.emptyIcBorder, lineWidth: 1)   // rgba(255,255,255,.07)
                Image(systemName: "folder.badge.plus")
                    .font(.system(size: 26, weight: .light))
                    .foregroundColor(Tokens.C.textTertiary)
            }
            .frame(width: Tokens.M.emptyIc, height: Tokens.M.emptyIc)
            .padding(.bottom, 18)

            Text("Очередь пуста")
                .font(.system(size: Tokens.F.emptyTitle, weight: .semibold))
                .foregroundColor(Tokens.C.textSoft)

            Text("Киньте папку-сборник с mp3 в отслеживаемую папку — книга появится здесь и всплывёт на подтверждение.")
                .font(.system(size: Tokens.F.emptyBody))
                .foregroundColor(Tokens.C.textSecondary)
                .multilineTextAlignment(.center)
                .lineSpacing(3)                                     // line-height 1.45
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 8)
        }
        .frame(maxWidth: .infinity)
        .padding(.init(top: 60, leading: 30, bottom: 60, trailing: 30))
    }

    // MARK: Footer — .footer: padding 13 16, top border .06, bg rgba(7,11,16,.5).
    // Live dot + status text + "Открыть папку".

    private var footer: some View {
        VStack(spacing: 0) {
            Rectangle().fill(Tokens.C.borderCard).frame(height: 1)  // border-top .06
            HStack(spacing: 10) {
                Circle()
                    .fill(Tokens.C.brandCyan)
                    .frame(width: 7, height: 7)
                    .shadow(color: Tokens.C.brandCyan.opacity(0.8), radius: 4)
                Text(footerText)
                    .font(.system(size: Tokens.F.caption))
                    .foregroundColor(Tokens.C.textSecondary)
                Spacer(minLength: 8)
                openFolderButton
            }
            .padding(.init(top: 13, leading: 16, bottom: 13, trailing: 16))
            .background(Tokens.C.surfaceFooter)
        }
    }

    // "Собираю N из M" when a batch is active; else "Агент работает" (mockup empty).
    private var footerText: String {
        if let batch = state.batch, batch.active, batch.total > 0 {
            return "Собираю \(batch.done) из \(batch.total)"
        }
        return "Агент работает"
    }

    // .btn: folder glyph (teal stroke) + label, neutral control surface.
    private var openFolderButton: some View {
        Button(action: onOpenFolder) {
            HStack(spacing: 6) {
                Image(systemName: "folder")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundColor(Tokens.C.brandCyan)
                Text("Открыть папку")
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
                    .foregroundColor(Tokens.C.textHigh)
            }
            .padding(.horizontal, 13)
            .padding(.vertical, 7)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.cvBtn, style: .continuous)
                    .fill(Tokens.C.surfaceControl)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.cvBtn, style: .continuous)
                    .stroke(Tokens.C.borderControl, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
    }

    // .credit: 11px #5a6b76 +0.1, GitHub link #5B9DF9 (display-only link styling).
    private var credit: some View {
        Text("mp3-to-m4b \(Tokens.appVersion) · by Alex Kovalev")
            .font(.system(size: Tokens.F.small))
            .tracking(0.1)
            .foregroundColor(Tokens.C.textQuaternary)
            .frame(maxWidth: .infinity)
            .padding(.init(top: 9, leading: 16, bottom: 13, trailing: 16))
    }
}

// MARK: - Batch chip (.batch-chip: mini ring 16 + "N / M")

/// The header batch progress chip (spec §7): a 16px progress ring filled by
/// batch{done/total} + an "N из M" label. Shown only while a batch is active.
private struct BatchChip: View {
    let batch: BatchProgress

    private var fraction: Double {
        guard batch.total > 0 else { return 0 }
        return min(1, max(0, Double(batch.done) / Double(batch.total)))
    }

    var body: some View {
        HStack(spacing: 7) {
            MiniRing(fraction: fraction)
                .frame(width: Tokens.M.batchRing, height: Tokens.M.batchRing)
            Text("\(batch.done) из \(batch.total)")
                .font(.system(size: Tokens.F.caption, weight: .semibold).monospacedDigit())
                .foregroundColor(Tokens.C.textHigh)
        }
        .padding(.horizontal, 11)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.cvBtn, style: .continuous)
                .fill(Tokens.C.batchChipBg)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.cvBtn, style: .continuous)
                .stroke(Tokens.C.batchChipBorder, lineWidth: 1)
        )
    }
}

/// A small progress ring (mockup .batch-chip .ring): a faint track + a teal arc
/// filled to `fraction`, round-capped, starting at 12 o'clock (rotate -90).
private struct MiniRing: View {
    let fraction: Double

    var body: some View {
        ZStack {
            Circle()
                .stroke(Color.white(0.10), lineWidth: 2.4)
            Circle()
                .trim(from: 0, to: CGFloat(fraction))
                .stroke(Tokens.C.brandCyan,
                        style: StrokeStyle(lineWidth: 2.4, lineCap: .round))
                .rotationEffect(.degrees(-90))
        }
        .padding(1.2)   // keep the 2.4 stroke inside the 16×16 box
    }
}

// MARK: - Shared qrow chrome

/// The shared qrow container (.qrow: padding 11 12, radius 12, bg #11161d,
/// border .06, margin-bottom 8): a leading visual + title/sub body + a trailing
/// control. Built once so every status row is pixel-identical chrome.
private struct QRow<Leading: View, Trailing: View>: View {
    let title: String
    let sub: String
    let subColor: Color
    @ViewBuilder let leading: () -> Leading
    @ViewBuilder let trailing: () -> Trailing

    var body: some View {
        HStack(spacing: 11) {
            leading()
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: Tokens.F.body, weight: .semibold))
                    .foregroundColor(Tokens.C.textHigh)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Text(sub)
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(subColor)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            trailing()
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 11)
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCard)                  // #11161d
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderCard, lineWidth: 1)
        )
        .padding(.bottom, 8)
    }
}

/// The 38×38 leading cover for pending/converting rows (.qcov: radius 8, shadow).
/// Loads the book's selected/embedded/first cover from its manifest; falls back to
/// a neutral brand-tinted tile (NOT a crash) when there is no image yet.
private struct QCover: View {
    let manifest: BookManifest?

    /// Best available cover image: the selected option, else the first option, else
    /// the legacy embedded preview. Every option path is a real file the agent wrote.
    private var image: NSImage? {
        guard let m = manifest else { return nil }
        // Prefer the agent's selected option, then the first, then embedded preview.
        let chosen = m.coverOptions.first { $0.optID == m.coverSelected }
            ?? m.coverOptions.first
        if let p = chosen?.path, !p.isEmpty, let img = NSImage(contentsOfFile: p) {
            return img
        }
        if m.coverState == "embedded", let p = m.coverPreview, !p.isEmpty {
            return NSImage(contentsOfFile: p)
        }
        return nil
    }

    var body: some View {
        Group {
            if let img = image {
                Image(nsImage: img)
                    .resizable()
                    .aspectRatio(contentMode: .fill)
            } else {
                // Neutral placeholder tile (no demo gradient — spec §0.8): a deep
                // card with a small book glyph, so a cover-less row still reads.
                ZStack {
                    Tokens.C.bgCardDeep
                    Image(systemName: "book.closed")
                        .font(.system(size: 14, weight: .regular))
                        .foregroundColor(Tokens.C.textTertiary)
                }
            }
        }
        .frame(width: Tokens.M.qrowCover, height: Tokens.M.qrowCover)
        .clipShape(RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous))
        .shadow(color: Color.black.opacity(0.5), radius: 3, x: 0, y: 2)
    }
}

/// A 26×26 status disc (.qstatus-ic) for done/error rows: a tinted rounded square
/// with a check (done) or warning triangle (error).
private struct QStatusDisc: View {
    enum Kind { case done, error }
    let kind: Kind

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous)
                .fill(kind == .done ? Tokens.C.qStatusOkBg : Tokens.C.qStatusErrBg)
            Image(systemName: kind == .done ? "checkmark" : "exclamationmark.triangle.fill")
                .font(.system(size: kind == .done ? 12 : 11,
                              weight: kind == .done ? .heavy : .semibold))
                .foregroundColor(kind == .done ? Tokens.C.brandCyan : Tokens.C.warnBase)
        }
        .frame(width: Tokens.M.qStatusIc, height: Tokens.M.qStatusIc)
    }
}

// MARK: - qbtn styles (.qbtn / .qbtn.ghost / .qbtn.warn)

/// A queue action button (.qbtn): padding 7 13, radius 9, 12px/600. Five variants:
/// base (accent), ghost, warn, danger (the converting row's "Отмена" — spec §7:
/// `btn-cancel`, danger-soft `#FF6B6B`; semantics §7 "отмена = danger", NOT warn), and
/// link (a borderless accent text button for a low-emphasis SECONDARY action — the done
/// row's "Собрать заново"; brandCyan text with no fill/border so it reads as a clickable
/// link beside the framed "Открыть", never competes with it and is clearly not danger).
/// `enabled=false` dims it (for
/// non-actionable states — no dead buttons, but visibly inert).
private struct QButton: View {
    enum Style { case base, ghost, warn, danger, link }
    let label: String
    let style: Style
    var enabled: Bool = true
    let action: () -> Void

    private var fg: Color {
        switch style {
        case .base:   return Tokens.C.brandCyan
        case .ghost:  return Tokens.C.textSoft
        case .warn:   return Tokens.C.warnBase
        case .danger: return Tokens.C.dangerTextSoft   // #FFB0B0 — .btn-cancel text
        case .link:   return Tokens.C.brandCyan        // #34E0D2 — interactive accent link (reads clickable, not the muted grey that looked disabled)
        }
    }
    private var bg: Color {
        switch style {
        case .base:   return Tokens.C.qbtnBg
        case .ghost:  return Tokens.C.surfaceControl
        case .warn:   return Tokens.C.qbtnWarnBg
        case .danger: return Tokens.C.dangerTint10     // rgba(255,99,99,.10) — btn-cancel bg
        case .link:   return Color.clear               // borderless link → no fill
        }
    }
    private var border: Color {
        switch style {
        case .base:   return Tokens.C.qbtnBorder
        case .ghost:  return Tokens.C.borderControlStrong
        case .warn:   return Tokens.C.qbtnWarnBorder
        case .danger: return Tokens.C.dangerBorder30   // rgba(255,99,99,.30) — btn-cancel border
        case .link:   return Color.clear               // borderless link → no border
        }
    }
    // The link variant trims the horizontal padding so it sits tight next to the
    // primary control (it has no chip to breathe inside) while keeping the same
    // vertical rhythm so the row height is unchanged.
    private var hPad: CGFloat { style == .link ? 4 : 13 }

    var body: some View {
        Button(action: { if enabled { action() } }) {
            Text(label)
                .font(.system(size: Tokens.F.caption, weight: .semibold))
                .foregroundColor(fg)
                // A qbtn ALWAYS takes its natural width and never wraps/hyphenates:
                // in a tight row the TITLE truncates (QRow), the buttons stay whole.
                .lineLimit(1)
                .fixedSize(horizontal: true, vertical: false)
                .padding(.horizontal, hPad)
                .padding(.vertical, 7)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.cvBtn, style: .continuous)
                        .fill(bg)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Tokens.R.cvBtn, style: .continuous)
                        .stroke(border, lineWidth: 1)
                )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.55)
    }
}

/// A COMPACT square icon button (ghost chrome) for a row action whose text label
/// would eat the horizontal budget — the done row's "Открыть" (reveal .m4b in
/// Finder). At window width 400pt two text buttons truncated the title
/// ("Рассечение С…"); an icon square frees that space so the title fits whole. It
/// reuses the SAME ghost visual as the text qbtn (surfaceControl fill,
/// borderControlStrong border, cvBtn radius, textSoft glyph) so it reads as a real
/// clickable control — never the muted grey that looked disabled. The SF Symbol is
/// mandatory (studio lesson: tiny hand-drawn Paths are unreadable; only SF Symbols
/// stay legible small). `enabled=false` dims it (visibly inert, not a dead click).
/// A `.help` tooltip + accessibilityLabel surface the meaning on hover / for VO.
private struct QIconButton: View {
    let systemImage: String
    let label: String            // tooltip + accessibility label (the icon has no text)
    var enabled: Bool = true
    let action: () -> Void

    // ~30pt square: compact enough to free the row for the title, big enough that
    // the 13px glyph and a comfortable tap target stay honest.
    private let side: CGFloat = 30

    var body: some View {
        Button(action: { if enabled { action() } }) {
            Image(systemName: systemImage)
                .font(.system(size: 13, weight: .semibold))
                .foregroundColor(Tokens.C.textSoft)   // same as ghost qbtn text
                .frame(width: side, height: side)
                .background(
                    RoundedRectangle(cornerRadius: Tokens.R.cvBtn, style: .continuous)
                        .fill(Tokens.C.surfaceControl)          // ghost fill
                )
                .overlay(
                    RoundedRectangle(cornerRadius: Tokens.R.cvBtn, style: .continuous)
                        .stroke(Tokens.C.borderControlStrong, lineWidth: 1)  // ghost border
                )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .disabled(!enabled)
        .opacity(enabled ? 1 : 0.55)
        .help(label)
        .accessibilityLabel(label)
    }
}

// MARK: - Per-status rows

/// Pending-confirm row: cover-38 · "Автор · N глав · ~T" · "Подтвердить".
private struct PendingRow: View {
    let book: BookSummary
    let cover: BookManifest?
    let onConfirm: () -> Void

    var body: some View {
        QRow(title: book.title.isEmpty ? "Без названия" : book.title,
             sub: QueueSub.pending(book),
             subColor: Tokens.C.textSecondary,
             leading: { QCover(manifest: cover) },
             trailing: { QButton(label: "Подтвердить", style: .base, action: onConfirm) })
    }
}

/// Converting row: cover-38 · "идёт сборка…" · indeterminate mini-progress +
/// "Отмена" (D13 — drop a `cancel` command; danger-soft per spec §7). On a
/// successful drop the trailing control swaps to a muted "Отправлено…" ack (mirrors
/// the confirm window) and the button disables; the row itself disappears once the
/// agent lands the book back at pending-confirm and the file-watch refreshes state.
private struct ConvertingRow: View {
    let book: BookSummary
    let cover: BookManifest?
    /// Drops the cancel command; returns true on a successful write.
    let onCancel: () -> Bool

    @State private var sent = false

    var body: some View {
        QRow(title: book.title.isEmpty ? "Без названия" : book.title,
             sub: sent ? "отмена…" : "идёт сборка…",
             subColor: Tokens.C.textSecondary,
             leading: { QCover(manifest: cover) },
             trailing: {
                HStack(spacing: 8) {
                    // The honest indeterminate bar (one ffmpeg call → no fake %).
                    MiniIndeterminate()
                    if sent {
                        Text("Отправлено…")
                            .font(.system(size: Tokens.F.caption, weight: .semibold))
                            .foregroundColor(Tokens.C.textSecondary)
                    } else {
                        QButton(label: "Отмена", style: .danger) {
                            if onCancel() { sent = true }
                        }
                    }
                }
             })
    }
}

/// Done row: status disc · "N глав · 25 ч 56 м" (ok-cyan sub) · "Собрать заново"
/// (secondary link) + "Открыть". "Собрать заново" drops a `reconvert` command (the
/// app never changes status); on a successful drop it swaps to a muted "Отправлено…"
/// ack and the row disappears once the agent re-arms the book back to pending-confirm
/// and the file-watch refreshes state — mirroring the converting row's cancel ack.
private struct DoneRow: View {
    let book: BookSummary
    let manifest: BookManifest?
    let onReveal: (BookManifest) -> Void
    /// Drops the reconvert command; returns true on a successful write.
    let onReconvert: () -> Bool

    @State private var sent = false

    private var canReveal: Bool {
        (manifest?.result?.outputPath?.isEmpty == false)
    }

    var body: some View {
        QRow(title: book.title.isEmpty ? "Без названия" : book.title,
             sub: sent ? "Отправлено…" : QueueSub.done(book),
             subColor: sent ? Tokens.C.textSecondary : Tokens.C.qSubOk,
             leading: { QStatusDisc(kind: .done) },
             trailing: {
                HStack(spacing: 8) {
                    if !sent {
                        // Low-emphasis SECONDARY action: re-arm this finished book so
                        // the normal confirm→build flow rebuilds it (one click, no
                        // folder rename). Accent link style (brandCyan, borderless) so
                        // it reads as clickable yet never competes with the framed
                        // "Открыть" and is clearly not danger.
                        QButton(label: "Собрать заново", style: .link) {
                            if onReconvert() { sent = true }
                        }
                    }
                    // "Открыть" reveals the .m4b in Finder — a COMPACT icon square
                    // (folder glyph) so the title fits whole at 400pt instead of being
                    // truncated by a second text button. If the path is missing
                    // (older/odd manifest), it is visibly inert rather than a dead click.
                    QIconButton(systemImage: "folder", label: "Открыть", enabled: canReveal) {
                        if let m = manifest { onReveal(m) }
                    }
                }
             })
    }
}

/// Error row: warn disc · "<reason>" (warn sub) · "Повторить" DISABLED (retry is
/// deferred — D13). The button is shown inert so the row matches the mockup shape
/// without wiring an action the agent does not yet accept.
private struct ErrorRow: View {
    let book: BookSummary
    let manifest: BookManifest?

    var body: some View {
        QRow(title: book.title.isEmpty ? "Без названия" : book.title,
             sub: QueueSub.error(book, manifest: manifest),
             subColor: Tokens.C.qSubErr,
             leading: { QStatusDisc(kind: .error) },
             trailing: { QButton(label: "Повторить", style: .warn, enabled: false) {} })
    }
}

/// A compact indeterminate progress for converting rows (honest — the agent emits
/// only binary progress, so NO fake percentage): a faint mini-track with a teal
/// highlight sliding across (spec §1 motion; mirrors the confirm window's bar).
private struct MiniIndeterminate: View {
    @State private var slide = false
    private let trackW: CGFloat = 70
    private let chunkW: CGFloat = 28

    var body: some View {
        ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: 3, style: .continuous)
                .fill(Tokens.C.progressTrack)              // rgba(255,255,255,.08)
            RoundedRectangle(cornerRadius: 3, style: .continuous)
                .fill(Tokens.Grad.brandTealIndigo)
                .frame(width: chunkW)
                .offset(x: slide ? (trackW - chunkW) : 0)
                .animation(.easeInOut(duration: 1.1).repeatForever(autoreverses: true),
                           value: slide)
        }
        .frame(width: trackW, height: 5)
        .clipShape(RoundedRectangle(cornerRadius: 3, style: .continuous))
        .onAppear { slide = true }
    }
}

// MARK: - Sub-line composers + queue-local formatters

/// Builds the qrow sub-line per status from the (cheap) showcase row — author,
/// chapter count, coarse duration — without loading a manifest.
private enum QueueSub {
    /// "Лев Толстой · 8 глав · ~9 ч" (parts present only when known).
    static func pending(_ b: BookSummary) -> String {
        var parts: [String] = []
        if !b.author.isEmpty { parts.append(b.author) }
        if b.chapterCount > 0 { parts.append("\(b.chapterCount) \(QueuePlural.chapters(b.chapterCount))") }
        if b.totalSeconds > 0 { parts.append("~\(QueueDuration.coarse(b.totalSeconds))") }
        return parts.isEmpty ? "ожидает подтверждения" : parts.joined(separator: " · ")
    }

    /// "56 глав · 25 ч 56 м" (chapters/duration present only when known). No "Готово"
    /// word — the done status is already shown by the check disc on the left.
    static func done(_ b: BookSummary) -> String {
        var parts: [String] = []
        if b.chapterCount > 0 { parts.append("\(b.chapterCount) \(QueuePlural.chapters(b.chapterCount))") }
        if b.totalSeconds > 0 { parts.append(QueueDuration.hoursMinutes(b.totalSeconds)) }
        return parts.isEmpty ? "готово" : parts.joined(separator: " · ")
    }

    /// The error reason, mapped from the manifest's machine `reason` (honest — no
    /// invented specifics). Falls back to a generic line if the manifest is absent.
    static func error(_ b: BookSummary, manifest: BookManifest?) -> String {
        guard let reason = manifest?.error?.reason else { return "Сборка не удалась" }
        switch reason {
        case "no_usable_chapters", "unreadable_chapter": return "Некоторые файлы не читаются"
        case "source_missing":        return "Исходные файлы не найдены"
        case "output_dir_unwritable": return "Не удалось записать результат"
        case "timeout":               return "Сборка заняла слишком долго"
        case "interrupted":           return "Сборка была прервана"
        case "empty_output":          return "ffmpeg не создал файл"
        case "ffmpeg_missing":        return "ffmpeg не найден"
        default:                      return "Сборка не удалась"
        }
    }
}

/// Queue-local duration formatting (file-private so the queue slice stays isolated
/// and the proven confirm-window formatters are untouched). Same output shapes.
private enum QueueDuration {
    /// Coarse total: "9 ч" / "14 ч 20 мин" / "47 мин".
    static func coarse(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        let h = total / 3600, m = (total % 3600) / 60
        if h > 0 { return m > 0 ? "\(h) ч \(m) мин" : "\(h) ч" }
        return "\(m) мин"
    }

    /// Done-row total with letters, NO seconds: "25 ч 56 м" / "25 ч" / "56 м".
    /// (Deliberately "м", not "мин" — matches the done sub's compact wording; the
    /// coarse "~9 ч / 14 ч 20 мин" line above is a separate pending estimate.)
    static func hoursMinutes(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        let h = total / 3600, m = (total % 3600) / 60
        if h > 0 { return m > 0 ? "\(h) ч \(m) м" : "\(h) ч" }
        return "\(m) м"
    }
}

/// Queue-local Russian plurals (file-private; same rules as the confirm window).
private enum QueuePlural {
    static func chapters(_ n: Int) -> String {
        let mod100 = n % 100, mod10 = n % 10
        if mod100 >= 11 && mod100 <= 14 { return "глав" }
        switch mod10 {
        case 1: return "глава"
        case 2, 3, 4: return "главы"
        default: return "глав"
        }
    }

    static func books(_ n: Int) -> String {
        let mod100 = n % 100, mod10 = n % 10
        if mod100 >= 11 && mod100 <= 14 { return "книг" }
        switch mod10 {
        case 1: return "книга"
        case 2, 3, 4: return "книги"
        default: return "книг"
        }
    }
}
