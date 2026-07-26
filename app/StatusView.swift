// StatusView — the "Status" home screen (02 / spec §5), pixel-mapped to
// design/mockups/02-status.html (CSS = pixel truth).
//
// Status is the app's HOME (decision D8): when no book awaits confirmation the
// window rests here, showing the agent's live picture — a progress ring, three
// stat cards (Собрано / За сегодня / ffmpeg), the agent/folder/queue rows, the
// last-built books, a footer and the credit line.
//
// DISPLAY + NAVIGATION ONLY. It is a pure READER of the agent-owned showcase
// (state.json `batch` / `totals` / `engine` / `agent` + the per-book manifests for
// covers and build times). The app NEVER writes status here.
//
// LAYOUT IS DETERMINISTIC by construction (lesson .patches/002): the fixed chrome
// (hero · stats · group rows) sits OUTSIDE any scroll view, so it reports a true
// intrinsic height. Only the variable section (last-built books) scrolls — a plain
// VStack of rows inside a ScrollView bounded by a fixed max-height (M.recentListMax),
// placed ABOVE the footer. There is NO GeometryReader-measured self-sizing and NO
// @State height feedback loop, so rows can never collapse/overlap, and the bounded
// list can never push the footer off-screen: a longer history scrolls INSIDE its
// viewport. The progress ring is a trim-based Shape sized by a fixed .frame — no
// self-measurement either. The window-height cap (AppDelegate) is the outer belt.
//
// ★ SPEC↔REALITY (documented for Yurka, NOT guessed): the spec's hero line
// "Сейчас: … глава 7 из 12" assumes per-chapter build progress. There is NONE — a
// build is ONE ffmpeg concat call, not a per-chapter loop (the agent records only
// binary progress). So the hero shows "Сейчас: <title>" + an indeterminate bar
// while converting, WITHOUT a fake "глава X из Y". See `heroNowBuilding`.

import AppKit
import SwiftUI

// MARK: - Status root

/// The full Status home content (400 wide). Header → hero card → stat cards →
/// group rows → last-built books (scrolls) → footer → credit. Mirrors the mockup's
/// vertical rhythm (12pt between blocks, 14pt side gutters).
struct StatusView: View {
    let state: ShowcaseState
    /// Per-book manifest lookup (cover preview + build time live there, not in the
    /// lightweight showcase). Injected by the host as a StateStore-backed closure so
    /// Status stays a pure reader. nil if a manifest is absent/half-written.
    let manifestFor: (BookSummary) -> BookManifest?
    /// "Очередь подтверждения" row / chevron → open the queue (spec §7).
    let onOpenQueue: () -> Void
    /// Footer "Открыть папку" → reveal the watched folder in Finder.
    let onOpenFolder: () -> Void
    /// Header/footer gear → settings. Never a dead control.
    let onOpenSettings: () -> Void
    /// "Очистить" the recent-built list (app-owned marker; state.json untouched —
    /// D13). Ported from fb2's StatusView clear button.
    let onClearHistory: () -> Void
    /// The "recent cleared at" cutoff (app-owned marker, EngineClient+Status): a
    /// done/error book built AT OR BEFORE this instant is hidden from the list. nil
    /// = nothing cleared. Passed down by the host so Status stays a pure reader.
    let recentClearedAt: Date?
    /// M6 — the folder-access card, already wired by the host. nil = either the
    /// folder is fine or the fail-closed gate forbids claiming anything about it
    /// (`InstallTruth.allowsFolderAccessSurface`); Status never decides this itself.
    ///
    /// Two подачи, picked by whether there is anything to look at:
    ///   · no history yet → BLOCKER. There is nothing else on this screen worth
    ///     seeing (an empty hero next to "нет доступа" is noise), and access is the
    ///     one thing standing between the user and their first book.
    ///   · history exists → BANNER above the content. The books stay visible; the
    ///     conversion is equally stuck either way, but hiding what already works
    ///     would read as "the app lost my books".
    var accessCard: FolderAccessCard? = nil
    /// The window is at the screen ceiling, so the blocker's card really is
    /// scrolling. Drives a bottom fade + chevron so the cut is legible as "there is
    /// more below" instead of as a bug. Host-computed (`refitWindowHeight` owns both
    /// numbers); Status only renders it.
    var contentClipped: Bool = false

    // Showcase projections (agent-owned; we only read).
    private var pendingCount: Int { state.pendingConfirm.count }
    private var watchName: String {
        guard let dir = state.agent.watchDir, !dir.isEmpty else { return "—" }
        return StatusPath.tildeAbbreviate(dir)
    }

    var body: some View {
        if let card = accessCard, !hasHistory {
            blockedBody(card)
        } else {
            normalBody
        }
    }

    /// Whether there is anything on this screen worth keeping visible behind a
    /// banner: a built/errored book in the digest, or a lifetime "собрано" count
    /// (the digest can be empty after «Очистить» while the counter is not).
    private var hasHistory: Bool {
        !orderedRecent.isEmpty || state.totals.built > 0 || !state.books.isEmpty
    }

    /// BLOCKER подача: header + the access card + the footer. No hero, no queue
    /// row, no recent list — none of it can change until the folder is readable.
    ///
    /// The card GROWS the window: opening the FDA fallback adds a measured +354 pt
    /// (409 → 763). The scroll area therefore has NO height cap of its own, and that
    /// is the fix, not an oversight.
    ///
    /// It used to carry a flat 460 pt. That constant bound long before the screen
    /// did — on the human's 897 pt display the window froze at 560 pt, cut the
    /// instruction mid-word and left ~300 pt of empty screen underneath (measured:
    /// the old cap clipped exactly 303 pt). Raising the constant would only move the
    /// wall. Two caps for one dimension is the actual bug: whichever binds first
    /// wins, and the OTHER one is the one that knows the screen.
    ///
    /// So there is one cap now — `cappedContentHeight` in the AppDelegate, which is
    /// derived from the real screen and the real titlebar inset. A bare ScrollView
    /// reports its content's ideal height (measured: 395 pt for a 395 pt card), so
    /// the window grows with the card; when the screen finally binds, the VStack
    /// squeezes the flexible child — the scroll area — and never the fixed chrome
    /// (measured at a 400 pt window: header kept all 72 pt, footer all 91 pt, the
    /// scroll area took the whole 237 pt hit). That is also what makes
    /// `contentClipped` honest: with no second cap swallowing the overflow, the
    /// window cap is the only thing that can clip, and it is the thing that reports.
    private func blockedBody(_ card: FolderAccessCard) -> some View {
        VStack(spacing: 0) {
            header
            ScrollView {
                card.presented(as: .blocker).padding(.bottom, 12)
            }
            .overlay(scrollMoreHint, alignment: .bottom)
            footer
            credit
        }
        .frame(width: Tokens.M.windowStandard)
    }

    /// Shown ONLY when the host says the window is at the screen ceiling. A pure
    /// overlay: it must not add height, or it would change the measurement that
    /// produced it. A fade alone reads as decoration, so it carries a chevron —
    /// the point is that the user understands there is more text, not that the edge
    /// looks softer.
    @ViewBuilder
    private var scrollMoreHint: some View {
        if contentClipped {
            VStack(spacing: 0) {
                Spacer(minLength: 0)
                LinearGradient(
                    gradient: Gradient(colors: [Tokens.C.bgApp.opacity(0), Tokens.C.bgApp]),
                    startPoint: .top, endPoint: .bottom)
                    .frame(height: 28)
                    .overlay(
                        Image(systemName: "chevron.down")
                            .font(.system(size: 11, weight: .bold))
                            .foregroundColor(Tokens.C.textTertiary)
                            .padding(.bottom, 2),
                        alignment: .bottom)
            }
            .allowsHitTesting(false)
        }
    }

    private var normalBody: some View {
        VStack(spacing: 0) {
            header

            // BANNER подача — above the hero, below the header (the access problem
            // outranks everything on this screen, but never hides it).
            if let card = accessCard { card.presented(as: .banner) }

            // FIXED chrome (always shown, true intrinsic height): the COMPACT hero
            // (badge + ring + the two counters stacked) and the queue row. Kept OUT
            // of the scroll area so the VStack's fitting height is honest and the
            // footer is always laid out below it (a bare ScrollView under-reports its
            // fitting height, which is what let the footer overlap). PORTED from fb2:
            // the 3 stat cards (Собрано/За сегодня/ffmpeg) are gone — the two counters
            // moved INTO the hero (stacked), and ffmpeg moved to Настройки.
            VStack(spacing: 12) {
                heroCard
                groupRows
            }
            .padding(.horizontal, 14)
            .padding(.top, 4)

            // VARIABLE section (0..N last-built books): the ONLY scrolling part, ABOVE
            // the footer and bounded by a fixed max-height. A long history scrolls
            // INSIDE this viewport — it can never grow the window or slide under the
            // footer. Deterministic cap (no GeometryReader self-measurement; .patches/002).
            recentBooksSection

            // No Spacer: the window height is derived from this VStack's fitting size
            // (AppDelegate), so the footer sits snugly below the content at any book
            // count — never overlapped, never a greedy gap. A long list scrolls inside
            // `recentBooksSection`; the window-cap is the outer belt.
            footer
            credit
        }
        .frame(width: Tokens.M.windowStandard)   // 400 (spec §5)
    }

    // The recent-books block wrapped in a height-capped ScrollView, with the side
    // gutters + vertical rhythm the fixed chrome above uses. Renders nothing when
    // there are no done/error books yet (the `recentBooks` card hides itself).
    @ViewBuilder
    private var recentBooksSection: some View {
        if !orderedRecent.isEmpty {
            ScrollView {
                recentBooks
                    .padding(.horizontal, 14)
                    .padding(.top, 12)
                    .padding(.bottom, 4)
            }
            // Bounds the variable list: fits ~6 rows, then scrolls internally so the
            // footer below stays visible + clickable at ANY book count (spec §1 cap).
            .frame(maxHeight: Tokens.M.recentListMax)
        }
    }

    // MARK: Header — .hdr: padding 18 18 14, app-icon 40 (r11) + h1 17/700 + sub +
    // gear. The app-icon uses the brand radial backing + logo glyph (header family).

    private var header: some View {
        HStack(spacing: 12) {
            AppIconBadge(size: 40, radius: 11, glyph: 18)
            VStack(alignment: .leading, spacing: 2) {
                Text("mp3-to-m4b")
                    .font(.system(size: Tokens.F.title, weight: .bold))
                    .tracking(-0.2)
                    .foregroundColor(Tokens.C.textHigh)
                Text("Сборка аудиокниг MP3 → M4B")
                    .font(.system(size: Tokens.F.caption))
                    .foregroundColor(Tokens.C.textSecondary)
            }
            Spacer(minLength: 8)
            // .icon-btn 28 r8: a settings gear (no dead control — routes to settings).
            Button(action: onOpenSettings) {
                Image(systemName: "gearshape")
                    .font(.system(size: 14, weight: .regular))
                    .foregroundColor(Tokens.C.textMuted)
                    .frame(width: 28, height: 28)
                    .background(
                        RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                            .fill(Tokens.C.surfaceControlSoft)        // rgba(255,255,255,.04)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                            .stroke(Tokens.C.borderCard, lineWidth: 1) // rgba(255,255,255,.06)
                    )
            }
            .buttonStyle(.plain)
            .contentShape(Rectangle())
            .help("Настройки")
        }
        .padding(.init(top: 18, leading: 18, bottom: 14, trailing: 18))
    }

    // MARK: Hero — .hero card: margin(handled by stack) padding 18, radius 16, card
    // bg + heroInset top highlight. Ring 104 (left) + status column (right).

    private var heroCard: some View {
        HStack(alignment: .center, spacing: 18) {
            ProgressRing(batch: state.batch, idleCount: state.totals.built)
                .frame(width: Tokens.M.ringSizeHero, height: Tokens.M.ringSizeHero)

            VStack(alignment: .leading, spacing: 0) {
                statePill
                heroPath.padding(.top, 10)
                heroCounters.padding(.top, 12)
                heroNowBuilding.padding(.top, 8)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .padding(18)
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.heroCard, style: .continuous)
                .fill(Tokens.C.bgCard)                                  // #11161d
        )
        .overlay(
            // shadow.heroInset: inset 0 1px 0 rgba(255,255,255,.03) approximated as a
            // hairline card contour (the 1px top sheen reads as the card edge).
            RoundedRectangle(cornerRadius: Tokens.R.heroCard, style: .continuous)
                .stroke(Tokens.C.borderCard, lineWidth: 1)
        )
    }

    // .pill: padding 3 9, radius 7, teal .12 bg + .28 border, dot + "АКТИВНО"/"ПАУЗА".
    private var statePill: some View {
        let active = state.agent.active
        return HStack(spacing: 6) {
            Circle()
                .fill(active ? Tokens.C.liveDot : Tokens.C.textTertiary)
                .frame(width: Tokens.M.pillDot, height: Tokens.M.pillDot)
                .shadow(color: active ? Tokens.C.liveDot.opacity(0.9) : .clear, radius: 3)
            Text(active ? "АКТИВНО" : "ПАУЗА")
                .font(.system(size: Tokens.F.small, weight: .bold))
                .tracking(0.2)
                .foregroundColor(active ? Tokens.C.brandCyan : Tokens.C.textSecondary)
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 3)
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous)
                .fill(active ? Tokens.C.pillOkBg : Tokens.C.surfaceControlSoft)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous)
                .stroke(active ? Tokens.C.pillOkBorder : Tokens.C.borderCard, lineWidth: 1)
        )
    }

    // .hero-path: 13px secondary, folder glyph + mono watch path (ellipsized).
    private var heroPath: some View {
        HStack(spacing: 6) {
            Image(systemName: "folder")
                .font(.system(size: 11, weight: .regular))
                .foregroundColor(Tokens.C.textSecondary)
            Text(watchName)
                .font(.system(size: Tokens.F.body, design: .monospaced))
                .foregroundColor(Tokens.C.textHigh)
                .lineLimit(1)
                .truncationMode(.middle)
        }
    }

    // The two metrics that moved up from the stat-card row (fb2 parity, ~514-524):
    // «Собрано» (всего) + «За сегодня» — STACKED vertically under the path. Same big
    // number + caps-label scale the stat cards used (statVal value + 9/700 caps),
    // just without the card chrome / mini-bar. Stacked (not side-by-side) so the
    // full caps labels never truncate in the narrow right column.
    private var heroCounters: some View {
        VStack(alignment: .leading, spacing: 14) {
            HeroCounter(value: "\(state.totals.built)",
                        cap: "СОБРАНО",
                        valueColor: Tokens.C.statBuilt)     // brand cyan
            HeroCounter(value: "\(state.totals.today)",
                        cap: "ЗА СЕГОДНЯ",
                        valueColor: Tokens.C.statToday)     // brand indigo
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // .hero-sub: 11px secondary. SPEC↔REALITY (see file header): we show the current
    // book's title while converting — NO "глава X из Y" (no per-chapter progress
    // exists). Idle → a calm "Жду новые папки …" line so the slot is never empty.
    @ViewBuilder
    private var heroNowBuilding: some View {
        if let book = state.currentlyBuilding {
            HStack(spacing: 6) {
                Text("Сейчас:")
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textSecondary)
                Text(book.title.isEmpty ? "Без названия" : book.title)
                    .font(.system(size: Tokens.F.small, weight: .semibold))
                    .foregroundColor(Tokens.C.textMuted)
                    .lineLimit(1)
                    .truncationMode(.tail)
                // Honest indeterminate progress (one ffmpeg call → no fake %).
                MiniIndeterminateBar()
            }
        } else {
            Text(pendingCount > 0
                 ? "\(pendingCount) \(StatusPlural.books(pendingCount)) ждут подтверждения"
                 : "Жду новые папки с mp3")
                .font(.system(size: Tokens.F.small))
                .foregroundColor(Tokens.C.textSecondary)
                .lineLimit(1)
        }
    }

    // MARK: Group rows — the ONE row that stays on the compact home: «Очередь
    // подтверждения» (our KEY function, not a setting — fb2 has no queue, so this is
    // our specific addition). The «Фоновый агент» status moved into the hero badge,
    // and «Отслеживаемая папка / Сменить» moved to Настройки (fb2 parity: the folder
    // row lives on the settings page, not the home). Renders as a single-row card so
    // the user can open the queue (and see the pending count) from home. The row is
    // always shown; the count-badge appears only when pending > 0.

    private var groupRows: some View {
        VStack(spacing: 0) {
            Button(action: onOpenQueue) {
                HStack(spacing: 11) {
                    RowIcon(systemName: "arrow.right", tint: Tokens.C.brandIndigo,
                            bg: Tokens.C.rowIcIndigoBg)
                    Text("Очередь подтверждения")
                        .font(.system(size: Tokens.F.body))
                        .foregroundColor(Tokens.C.textHigh)
                    Spacer(minLength: 8)
                    if pendingCount > 0 {
                        CountBadge(count: pendingCount)
                    }
                    Image(systemName: "chevron.right")
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundColor(Tokens.C.textTertiary)
                        .padding(.leading, 2)
                }
                .padding(.init(top: 12, leading: 14, bottom: 12, trailing: 14))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
        }
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCard)                                  // #11161d
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(Tokens.C.borderCard, lineWidth: 1)
        )
        .clipShape(RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous))
    }

    // MARK: Recent books — .details card: padding 13 14 8. Header (cap + Очистить),
    // then the last-built rows (cover-22 · name · chapters · time), warn variant for
    // an errored book. THIS is the variable section; it sits in its own height-capped
    // ScrollView (`recentBooksSection`) ABOVE the footer — a deterministic VStack of
    // rows, no self-measured height (lesson .patches/002).

    @ViewBuilder
    private var recentBooks: some View {
        if !orderedRecent.isEmpty {
            VStack(alignment: .leading, spacing: 0) {
                HStack {
                    Text("ПОСЛЕДНИЕ СОБРАННЫЕ КНИГИ")
                        .font(.system(size: Tokens.F.cap, weight: .bold))
                        .tracking(1.2)
                        .foregroundColor(Tokens.C.textTertiary)
                    Spacer(minLength: 8)
                    clearButton      // "Очистить" (fb2 parity) — app-owned marker
                }
                .padding(.bottom, 6)

                ForEach(Array(orderedRecent.enumerated()), id: \.element.id) { idx, book in
                    RecentBookRow(book: book,
                                  manifest: manifestFor(book),
                                  isLast: idx == orderedRecent.count - 1)
                }
            }
            .padding(.init(top: 13, leading: 14, bottom: 8, trailing: 14))
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                    .fill(Tokens.C.bgCard)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                    .stroke(Tokens.C.borderCard, lineWidth: 1)
            )
        }
    }

    // Recent done/error books, most-recently-built first (build time from each
    // manifest's result.built_at; books without a time sort last, stable). Capped to
    // a sensible digest so the home doesn't grow unbounded — the full list is the
    // queue's job. The viewport (recentListMax) shows ~5 rows without an internal
    // scroll; a 6th..8th recent book scrolls INSIDE that capped viewport (spec §1 /
    // brief "до 5 книг, больше 5 — скроллится"). 8 keeps a little scroll headroom
    // past the 5 visible without turning the home digest into the full queue.
    private var orderedRecent: [BookSummary] {
        // Pair each recent book with its build time (result.built_at from the
        // manifest; 0 when unknown).
        let withTime: [(BookSummary, Double)] = state.recentBooks.map { b in
            (b, manifestFor(b)?.result?.builtAt ?? 0)
        }
        // "Очистить" hiding (fb2 parity): drop every book built AT OR BEFORE the
        // app-owned recent-cleared-at marker. A book with an unknown build time
        // (built_at == 0) is KEPT (fail-open — never hide a book just because its
        // manifest lacks a timestamp). Newer builds reappear naturally.
        let cutoff = recentClearedAt?.timeIntervalSince1970
        let visible = withTime.filter { _, ts in
            guard let cutoff = cutoff, ts > 0 else { return true }
            return ts > cutoff
        }
        return visible
            .sorted { $0.1 > $1.1 }
            .map { $0.0 }
            .prefix(8)
            .map { $0 }
    }

    // "Очистить" — clears the recent-built list via an app-owned marker (state.json
    // is never rewritten — D13). Ported from fb2's StatusView clearButton (~606):
    // a small ghost chip, trash glyph + label, tertiary text on a faint surface.
    private var clearButton: some View {
        Button(action: onClearHistory) {
            HStack(spacing: 3) {
                Image(systemName: "trash")
                    .font(.system(size: 9, weight: .regular))
                Text("Очистить")
                    .font(.system(size: Tokens.F.small, weight: .semibold))
            }
            .foregroundColor(Tokens.C.textTertiary)
            .padding(.horizontal, 5)
            .padding(.vertical, 1)
            .background(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(Color.white(0.03))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .stroke(Color.white(0.07), lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .help("Очистить список последних книг")
    }

    // MARK: Footer — .footer: padding 13 16, top border .06, bg rgba(7,11,16,.5).
    // Live dot + "Агент работает" + "Открыть папку" + gear (→ settings).

    private var footer: some View {
        VStack(spacing: 0) {
            Rectangle().fill(Tokens.C.borderCard).frame(height: 1)
            HStack(spacing: 10) {
                Circle()
                    .fill(footerDotColor)
                    .frame(width: 7, height: 7)
                    .shadow(color: footerDotGlows ? Tokens.C.liveDot.opacity(0.8) : .clear,
                            radius: 4)
                Text(footerText)
                    .font(.system(size: Tokens.F.caption))
                    .foregroundColor(Tokens.C.textSecondary)
                Spacer(minLength: 8)
                openFolderButton
                gearButton
            }
            .padding(.init(top: 13, leading: 16, bottom: 13, trailing: 16))
            .background(Tokens.C.surfaceFooter)
        }
    }

    /// The footer говорит про ПРОЦЕСС, карточка — про ДОСТУП, и по отдельности оба
    /// правы: агент действительно жив, а папку действительно не читает. Рядом это
    /// читается как «всё в порядке» — зелёная точка сильнее любого текста над ней.
    /// Поэтому при живой проблеме доступа точка гаснет до нейтральной, а подпись
    /// говорит, что именно агент сейчас делать не может. Никакого нового источника
    /// правды: тот же `accessCard`, который уже прошёл fail-closed-гейт.
    private var accessIsBroken: Bool { accessCard != nil }

    private var footerDotColor: Color {
        if accessIsBroken { return Tokens.C.warnBase }
        return state.agent.active ? Tokens.C.liveDot : Tokens.C.textTertiary
    }

    private var footerDotGlows: Bool { !accessIsBroken && state.agent.active }

    private var footerText: String {
        if accessIsBroken { return "Агент запущен, но папку не читает" }
        if let batch = state.batch, batch.active, batch.total > 0 {
            return "Собираю \(batch.done) из \(batch.total)"
        }
        return state.agent.active ? "Агент работает" : "Агент на паузе"
    }

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

    // .btn-icon 32 r9 — footer gear → settings.
    private var gearButton: some View {
        Button(action: onOpenSettings) {
            Image(systemName: "gearshape")
                .font(.system(size: 14, weight: .regular))
                .foregroundColor(Tokens.C.textMuted)
                .frame(width: 32, height: 32)
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
        .help("Настройки")
    }

    // .credit: 11px #5a6b76 +0.1, GitHub link #5B9DF9. (.tracking is applied per-Text
    // so it stays macOS 11-compatible — it is a Text modifier there, not a View one.)
    private var credit: some View {
        HStack(spacing: 4) {
            Text("mp3-to-m4b \(Tokens.appVersion) · by Alex Kovalev ·")
                .font(.system(size: Tokens.F.small))
                .tracking(0.1)
                .foregroundColor(Tokens.C.textQuaternary)
            Text("GitHub")
                .font(.system(size: Tokens.F.small))
                .tracking(0.1)
                .foregroundColor(Tokens.C.linkBlue)
        }
        .frame(maxWidth: .infinity)
        .padding(.init(top: 9, leading: 16, bottom: 13, trailing: 16))
    }
}

// MARK: - Hero progress ring (.ring-wrap: 104, r44, stroke8, gradient + glow)

/// The hero progress ring (spec §5): a faint full-circle track + a gradient arc
/// round-capped, starting at 12 o'clock (rotate -90). Built from a trim-based Circle
/// (a Shape) sized by the caller's fixed .frame — DETERMINISTIC, no GeometryReader
/// self-measurement (lesson .patches/002). Two readings:
///  · ACTIVE batch → arc trimmed to done/total, centre shows "done/total".
///  · IDLE (no active batch) → if anything was ever built, a FULL calm ring with the
///    lifetime count + "всего" (so the centre is meaningful, not blank); if nothing
///    built yet, an empty track with a "—" placeholder.
private struct ProgressRing: View {
    let batch: BatchProgress?
    /// Lifetime built count (totals.built) — the idle reading's centre number.
    let idleCount: Int

    /// A batch counts as live only when the agent flagged it active with real work.
    private var batchLive: Bool { (batch?.active ?? false) && total > 0 }
    private var total: Int { max(0, batch?.total ?? 0) }
    private var done: Int { max(0, batch?.done ?? 0) }
    /// Arc fill: batch progress when live; a full ring on idle-with-history (a calm
    /// "all built" complete circle); empty when truly nothing has been built.
    private var fraction: Double {
        if batchLive { return min(1, max(0, Double(done) / Double(total))) }
        return idleCount > 0 ? 1 : 0
    }
    // The ring stroke is 8 on a 104 box (r44). Inset by half the stroke so the
    // round caps stay fully inside the frame.
    private let stroke: CGFloat = 8

    var body: some View {
        ZStack {
            Circle()
                .stroke(Tokens.C.barTrack, lineWidth: stroke)           // track .07
            Circle()
                .trim(from: 0, to: CGFloat(fraction))
                .stroke(Tokens.Grad.ring,
                        style: StrokeStyle(lineWidth: stroke, lineCap: .round))
                .rotationEffect(.degrees(-90))
                .shadow(color: Tokens.C.brandTeal.opacity(0.5), radius: 4)  // ring glow
            // .ring-center: number 20px/700 tnum cyan. Live batch → "done/total";
            // idle with history → the lifetime count + a tiny "всего" caption; nothing
            // built yet → a calm dash so the centre is never blank.
            ringCenter
        }
        .padding(stroke / 2)
    }

    @ViewBuilder
    private var ringCenter: some View {
        if batchLive {
            Text("\(done)/\(total)")
                .font(.system(size: Tokens.F.ringCenter, weight: .bold).monospacedDigit())
                .foregroundColor(Tokens.C.brandCyan)
        } else if idleCount > 0 {
            VStack(spacing: 1) {
                Text("\(idleCount)")
                    .font(.system(size: Tokens.F.ringCenter, weight: .bold).monospacedDigit())
                    .foregroundColor(Tokens.C.brandCyan)
                Text("всего")
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textSecondary)
            }
        } else {
            Text("—")
                .font(.system(size: Tokens.F.ringCenter, weight: .bold).monospacedDigit())
                .foregroundColor(Tokens.C.brandCyan)
        }
    }
}

// MARK: - Hero counter (big number + caps label, in the hero column)

/// A compact hero counter (fb2 parity, HeroCounter ~327): a big colored number over
/// a small caps label. Reuses the stat-card type scale (statVal value + 9/700 caps
/// label) so the two counters that moved up into the hero keep the "крупное число +
/// подпись" look they had as stat cards — without the card chrome / mini-bar.
private struct HeroCounter: View {
    let value: String
    let cap: String
    let valueColor: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(value)
                .font(.system(size: Tokens.F.statVal, weight: .bold).monospacedDigit())
                .foregroundColor(valueColor)
                .lineLimit(1)
                .minimumScaleFactor(0.7)
            Text(cap)
                .font(.system(size: Tokens.F.cap, weight: .bold))
                .tracking(1.2)
                .foregroundColor(Tokens.C.textTertiary)
        }
    }
}

// MARK: - Recent-book row (.book: grid 22 / 1fr / auto, cover-22 + name + time)

/// One row of the last-built list (spec §5). Done → cover-22 + "name.m4b · N глав" +
/// clock time; error → warn tile + muted name + warn "битый файл" sub. A hairline
/// divides rows (none after the last). Deterministic grid (no self-measurement).
private struct RecentBookRow: View {
    let book: BookSummary
    let manifest: BookManifest?
    let isLast: Bool

    private var isError: Bool { book.isError }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                // 22×22 leading: cover (done) or a warn triangle tile (error).
                Group {
                    if isError {
                        ZStack {
                            RoundedRectangle(cornerRadius: 5, style: .continuous)
                                .fill(Tokens.C.bookWarnTileBg)
                            Image(systemName: "exclamationmark.triangle.fill")
                                .font(.system(size: 11, weight: .semibold))
                                .foregroundColor(Tokens.C.warnBase)
                        }
                    } else {
                        RecentCover(manifest: manifest)
                    }
                }
                .frame(width: 22, height: 22)

                // name + inline meta. Done: "<title>.m4b · N глав". Error: "<title>"
                // + warn "битый файл …" reason tail.
                HStack(spacing: 6) {
                    Text(displayName)
                        .font(.system(size: Tokens.F.caption))
                        .foregroundColor(isError ? Tokens.C.bookNameErr : Tokens.C.textHigh)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    Text(metaTail)
                        .font(.system(size: 10.5))
                        .foregroundColor(isError ? Tokens.C.warnBase : Tokens.C.bookSmall)
                        .lineLimit(1)
                        .layoutPriority(-1)
                }
                .frame(maxWidth: .infinity, alignment: .leading)

                Text(timeText)
                    .font(.system(size: Tokens.F.small).monospacedDigit())
                    .foregroundColor(Tokens.C.bookSmall)
            }
            .padding(.vertical, 8)

            if !isLast {
                Rectangle()
                    .fill(Tokens.C.borderHairlineFaint)                 // rgba(255,255,255,.045)
                    .frame(height: 1)
            }
        }
    }

    // Done rows append ".m4b" to the title (the mockup shows the output filename);
    // error rows show the bare title.
    private var displayName: String {
        let base = book.title.isEmpty ? "Без названия" : book.title
        return isError ? base : "\(base).m4b"
    }

    // Inline meta after the name: chapters count (done) or the broken-file reason
    // (error). Honest — the error tail maps the manifest's machine reason.
    private var metaTail: String {
        if isError {
            return RecentError.tail(manifest)
        }
        if book.chapterCount > 0 {
            return "· \(book.chapterCount) \(StatusPlural.chapters(book.chapterCount))"
        }
        return ""
    }

    // Trailing time: the build clock-of-day (HH:MM) from result.built_at when known;
    // else the total length as a fallback; else blank.
    private var timeText: String {
        if let ts = manifest?.result?.builtAt, ts > 0 {
            return StatusTime.clockOfDay(ts)
        }
        if book.totalSeconds > 0 {
            return StatusDuration.clock(book.totalSeconds)
        }
        return ""
    }
}

/// The 22×22 cover for a recent done row. Loads the book's selected/embedded/first
/// cover from its manifest; falls back to a neutral deep tile with a tiny glyph (no
/// demo gradient — spec §0.8) so a cover-less row still reads. Mirrors QueueView's
/// QCover loading order.
private struct RecentCover: View {
    let manifest: BookManifest?

    private var image: NSImage? {
        guard let m = manifest else { return nil }
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
                ZStack {
                    Tokens.C.bgCardDeep
                    Image(systemName: "book.closed")
                        .font(.system(size: 10, weight: .regular))
                        .foregroundColor(Tokens.C.textTertiary)
                }
            }
        }
        .frame(width: 22, height: 22)
        .clipShape(RoundedRectangle(cornerRadius: 5, style: .continuous))
        .shadow(color: Color.black.opacity(0.5), radius: 2, x: 0, y: 1)
    }
}

// MARK: - Small shared pieces

/// The header/footer app-icon badge: the brand radial backing + the logo glyph
/// (books + play family used across the app's headers). Sized + cornered by the
/// caller. A teal drop-shadow matches the mockup's app-icon glow.
private struct AppIconBadge: View {
    let size: CGFloat
    let radius: CGFloat
    let glyph: CGFloat

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .fill(Tokens.Canvas.appIconGradient)
            Image(systemName: "books.vertical.fill")
                .font(.system(size: glyph, weight: .semibold))
                .foregroundColor(Tokens.C.brandCyan)
        }
        .frame(width: size, height: size)
        .overlay(
            RoundedRectangle(cornerRadius: radius, style: .continuous)
                .stroke(Color.white(0.18), lineWidth: 0.5)             // inset top sheen approx
        )
        .shadow(color: Tokens.C.brandTeal.opacity(0.45), radius: 8, x: 0, y: 6)
    }
}

/// A tinted 28×28 row icon (.row-ic: radius 8). Glyph color + backing tint by row.
private struct RowIcon: View {
    let systemName: String
    let tint: Color
    let bg: Color

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                .fill(bg)
            Image(systemName: systemName)
                .font(.system(size: 13, weight: .semibold))
                .foregroundColor(tint)
        }
        .frame(width: 28, height: 28)
    }
}

/// The queue count-badge (.count-badge: min 20×20, radius 10, brandTealIndigo
/// gradient, white 11px/700 tnum). Shown on the "Очередь подтверждения" row.
private struct CountBadge: View {
    let count: Int

    var body: some View {
        Text("\(count)")
            .font(.system(size: Tokens.F.small, weight: .bold).monospacedDigit())
            .foregroundColor(.white)
            .padding(.horizontal, 6)
            .frame(minWidth: 20, minHeight: 20)
            .background(
                Capsule(style: .continuous).fill(Tokens.Grad.brandTealIndigo)
            )
    }
}

/// A compact indeterminate progress for the hero "Сейчас:" line (honest — the agent
/// emits only binary progress, so NO fake percentage): a faint mini-track with a
/// teal highlight sliding across (spec §1 motion). Mirrors QueueView's pattern.
private struct MiniIndeterminateBar: View {
    @State private var slide = false
    private let trackW: CGFloat = 56
    private let chunkW: CGFloat = 22

    var body: some View {
        ZStack(alignment: .leading) {
            RoundedRectangle(cornerRadius: 3, style: .continuous)
                .fill(Tokens.C.progressTrack)
            RoundedRectangle(cornerRadius: 3, style: .continuous)
                .fill(Tokens.Grad.brandTealIndigo)
                .frame(width: chunkW)
                .offset(x: slide ? (trackW - chunkW) : 0)
                .animation(.easeInOut(duration: 1.1).repeatForever(autoreverses: true),
                           value: slide)
        }
        .frame(width: trackW, height: 4)
        .clipShape(RoundedRectangle(cornerRadius: 3, style: .continuous))
        .onAppear { slide = true }
    }
}

// MARK: - Status-local formatters (file-private; isolated from other screens)

/// Watched-folder path display: abbreviate the home prefix to "~" (mockup shows
/// "~/Desktop/mp3-to-m4b"), keep the rest verbatim.
private enum StatusPath {
    static func tildeAbbreviate(_ path: String) -> String {
        let home = NSHomeDirectory()
        if !home.isEmpty, path.hasPrefix(home) {
            return "~" + String(path.dropFirst(home.count))
        }
        return path
    }
}

/// Build-time formatting for recent rows.
private enum StatusTime {
    /// Clock-of-day "HH:MM" in local time from an epoch (mockup's right-aligned time).
    static func clockOfDay(_ epoch: Double) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "ru_RU")
        f.dateFormat = "HH:mm"
        return f.string(from: Date(timeIntervalSince1970: epoch))
    }
}

/// Status-local duration formatting (file-private; same shapes as the other screens).
private enum StatusDuration {
    static func clock(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        let h = total / 3600, m = (total % 3600) / 60, s = total % 60
        return h > 0
            ? String(format: "%d:%02d:%02d", h, m, s)
            : String(format: "%d:%02d", m, s)
    }
}

/// The recent error-row reason tail, mapped from the manifest's machine `reason`
/// (honest — no invented specifics). Falls back to a generic broken-file line.
private enum RecentError {
    static func tail(_ manifest: BookManifest?) -> String {
        guard let reason = manifest?.error?.reason else { return "· сборка не удалась" }
        switch reason {
        case "no_usable_chapters", "unreadable_chapter": return "· битый файл"
        case "source_missing":        return "· файлы не найдены"
        case "output_dir_unwritable": return "· не записать результат"
        case "timeout":               return "· слишком долго"
        case "interrupted":           return "· сборка прервана"
        case "empty_output":          return "· ffmpeg не создал файл"
        case "ffmpeg_missing":        return "· ffmpeg не найден"
        default:                      return "· сборка не удалась"
        }
    }
}

/// Status-local Russian plurals (file-private; same rules as the other screens).
private enum StatusPlural {
    static func books(_ n: Int) -> String {
        let mod100 = n % 100, mod10 = n % 10
        if mod100 >= 11 && mod100 <= 14 { return "книг" }
        switch mod10 {
        case 1: return "книга"
        case 2, 3, 4: return "книги"
        default: return "книг"
        }
    }

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
