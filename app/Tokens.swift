// Tokens — the single source of truth for the native UI's colors, type and
// metrics. Every value here is lifted VERBATIM from design/tokens.json (derived
// from design/mockups/*.html + branding/brand-basics.md). Views must pull from
// Tokens, never hardcode hex/sizes inline — that keeps pixel-perfect (G3) a
// one-file diff.
//
// The app is unconditionally dark (utility app): colors are fixed and do NOT
// follow the system appearance.
//
// M0.1 SCOPE: only the values the empty dark window needs are seeded (window
// canvas gradient + brand + key text + radii + the standard window width). The
// full token set is filled in as later milestones render real screens.

import SwiftUI

// MARK: - Color(hex:) helper

extension Color {
    /// Build a Color from "#RRGGBB" / "RRGGBB" / "#RRGGBBAA". Falls back to
    /// opaque magenta on a malformed string so a typo is loud, not silent.
    init(hex: String) {
        let s = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        var v: UInt64 = 0
        guard Scanner(string: s).scanHexInt64(&v) else {
            self = Color(.sRGB, red: 1, green: 0, blue: 1, opacity: 1)
            return
        }
        let r, g, b, a: Double
        switch s.count {
        case 6:
            r = Double((v & 0xFF0000) >> 16) / 255
            g = Double((v & 0x00FF00) >> 8) / 255
            b = Double(v & 0x0000FF) / 255
            a = 1
        case 8:
            r = Double((v & 0xFF000000) >> 24) / 255
            g = Double((v & 0x00FF0000) >> 16) / 255
            b = Double((v & 0x0000FF00) >> 8) / 255
            a = Double(v & 0x000000FF) / 255
        default:
            r = 1; g = 0; b = 1; a = 1
        }
        self = Color(.sRGB, red: r, green: g, blue: b, opacity: a)
    }

    /// White at a given opacity — tokens express most hairlines/surfaces as
    /// rgba(255,255,255,a).
    static func white(_ opacity: Double) -> Color { Color(.sRGB, white: 1, opacity: opacity) }
}

enum Tokens {

    // MARK: - Links / provenance

    /// The project repository (Settings "Открыть на GitHub" + the Status credit
    /// link). Plain link — we have NO auto-updater (unlike the fb2 neighbor).
    static let githubURL = "https://github.com/ArrivaRUS/mp3-to-m4b"
    /// The shipped app version shown on the Settings version card + the credit line.
    /// Kept in sync with build/build-app.sh's default VERSION.
    static let appVersion = "1.0"

    // MARK: - Colors (role -> hex), from tokens.json "color"

    enum C {
        // Backgrounds (color.bg)
        static let bgVoid     = Color(hex: "#050709") // desktop behind window
        static let bgApp      = Color(hex: "#0E1A22") // UI surfaces
        static let bgInput    = Color(hex: "#0a1018") // field-input / install-box
        static let bgCard     = Color(hex: "#11161d") // status/queue cards
        static let bgCardDeep = Color(hex: "#0c121a") // nested blocks on canvas

        // Brand (color.brand) — cyan→teal→indigo
        static let brandCyan   = Color(hex: "#34E0D2")
        static let brandTeal   = Color(hex: "#22B5E0")
        static let brandIndigo = Color(hex: "#4A6BFF")

        // Accent aliases (color.accent)
        static let accentPrimary = brandTeal   // primary action
        static let accentLink    = brandIndigo // links
        static let accentHover   = brandCyan   // hover/active

        // Text (color.text)
        static let textHigh        = Color(hex: "#EAF6FA") // titles/values
        static let textSoft        = Color(hex: "#cfe0e7") // ghost-button text / cv-btn
        static let textMuted       = Color(hex: "#9fb2bd") // muted mid (q-counter, preset off)
        static let textMutedAlt    = Color(hex: "#8a99a3") // skip-button text (.btn-skip)
        static let textSecondary   = Color(hex: "#7e93a0") // default secondary
        static let textTertiary    = Color(hex: "#6E8390") // caps micro-labels
        static let textQuaternary  = Color(hex: "#5a6b76") // very muted (ch-n, q-suffix)
        static let textPlaceholder = Color(hex: "#4a5862") // input placeholder
        static let textOnAccent    = Color(hex: "#06121a") // on bright accent
        static let textOnAccentHigh = Color(hex: "#EAFBFF") // cover-badge label
        static let onCoverSub      = Color.white(0.85)      // author on cover (.ca)

        // Window canvas radial-gradient stops (color.canvas.window).
        // shape: 120% 70% at 50% -6% — stops #14202A→#0E1822→#0a1018→#070B10.
        static let canvasStop0 = Color(hex: "#14202A") // 0%
        static let canvasStop1 = Color(hex: "#0E1822") // 38%  ← the "#0E1822" the brief names
        static let canvasStop2 = Color(hex: "#0a1018") // 64%
        static let canvasStop3 = Color(hex: "#070B10") // 100%

        // Borders (color.border)
        static let borderWindow        = Color.white(0.07) // window contour
        static let borderCard          = Color.white(0.06) // card contour
        static let borderHairline      = Color.white(0.05) // dividers (header/footer)
        static let borderHairlineFaint = Color.white(0.04) // chapter-row divider (.ch)
        static let borderControl       = Color.white(0.10) // control contour

        // Accent action text (color.accent.tealText) — links / action labels.
        static let accentTealText = brandCyan // #34E0D2

        // Footer / actions-bar fill (color.surfaceFill.footer = rgba(7,11,16,.5)).
        static let surfaceFooter = Color(.sRGB, red: 7/255, green: 11/255, blue: 16/255, opacity: 0.5)

        // Control surface fills (color.surfaceFill.*) — translucent button/control bg.
        static let surfaceControl     = Color.white(0.05) // .preset off, .cv-btn, .btn-ghost fill
        static let surfaceControlSoft = Color.white(0.04) // quieter (preset off in box)

        // Field-input / quality-box contour (color.border.fieldInput = .08).
        static let borderFieldInput   = Color.white(0.08)
        static let borderControlStrong = Color.white(0.12) // cv-btn / ghost-button contour

        // Accent (teal #22B5E0) selection tints — color.state.*
        static let accentTintBg     = Color(hex: "#22B5E0").opacity(0.16) // preset.on / seg.on bg
        static let accentBorder60   = Color(hex: "#22B5E0").opacity(0.60) // preset.on border
        static let accentBorder55   = Color(hex: "#22B5E0").opacity(0.55) // input:focus border
        static let accentBorder30   = Color(hex: "#22B5E0").opacity(0.30) // preset.on inset ring
        static let accentTint07     = Color(hex: "#22B5E0").opacity(0.07) // estimate bg
        static let accentBorder18   = Color(hex: "#22B5E0").opacity(0.18) // estimate border
        static let accentFocusRing  = Color(hex: "#22B5E0").opacity(0.14) // input focus ring

        // split-preview (spec §6 / tokens state.indigoTint08, state.indigoBorder20) — indigo #4A6BFF.
        static let indigoTint08     = brandIndigo.opacity(0.08) // .split-preview bg
        static let indigoBorder20   = brandIndigo.opacity(0.20) // .split-preview border

        // Grouping sheet (06 / spec §6) selection tints — teal #22B5E0.
        static let accentTint12     = Color(hex: "#22B5E0").opacity(0.12) // sheet-icon / choice-ic bg
        static let accentTint10     = Color(hex: "#22B5E0").opacity(0.10) // choice.sel bg
        static let accentTint20     = Color(hex: "#22B5E0").opacity(0.20) // choice.sel choice-ic bg
        static let surfaceChoice    = Color.white(0.03)                   // choice (unselected) fill
        // file-strip chip text / strip text (spec §6 file-strip).
        static let fileStripText    = Color(hex: "#9fb2bd")               // strip body text

        // Cover "from-file"/"own" badge (color.state.success*) — teal #34E0D2.
        static let coverBadgeBg     = Color(hex: "#34E0D2").opacity(0.18) // cover-badge bg
        static let coverBadgeBorder = Color(hex: "#34E0D2").opacity(0.50) // cover-badge border

        // Cover "from web" badge (.cover-badge.web) — indigo #4A6BFF.
        static let coverBadgeWebBg     = Color(hex: "#4A6BFF").opacity(0.20) // cover-badge.web bg
        static let coverBadgeWebBorder = Color(hex: "#4A6BFF").opacity(0.50) // cover-badge.web border
        // Cover "generated" badge (.cover-badge.gen) — neutral white.
        static let coverBadgeGenBg     = Color.white(0.12)                   // cover-badge.gen bg
        static let coverBadgeGenBorder = Color.white(0.25)                   // cover-badge.gen border
        static let coverBadgeGenDot    = Color(hex: "#cfe0e7")               // cover-badge.gen dot

        // Selected cover thumbnail ring (.gen-cell.sel: 0 0 0 2px #22B5E0 + glow).
        static let coverSelRing  = Color(hex: "#22B5E0")               // 2px selected ring
        static let coverSelGlow  = Color(hex: "#22B5E0").opacity(0.40) // selected glow

        // Action-label text (links / "Применить ко всем") — color.accent.tealText.
        static let accentLabel      = brandCyan // #34E0D2

        // danger (#FF6B6B) — hard errors (no space, empty title, cancel).
        static let dangerBase       = Color(hex: "#FF6B6B") // icon / stroke
        static let dangerText       = Color(hex: "#FF8B8B") // inline-err text
        static let dangerTextSoft   = Color(hex: "#FFB0B0") // btn-cancel text
        static let dangerTint10     = Color(hex: "#FF6B6B").opacity(0.10) // banner / btn-cancel bg
        static let dangerBorder55   = Color(hex: "#FF6B6B").opacity(0.55) // invalid input border
        static let dangerBorder30   = Color(hex: "#FF6B6B").opacity(0.30) // banner / btn-cancel border
        static let dangerFocusRing  = Color(hex: "#FF6B6B").opacity(0.12) // invalid input focus ring
        static let dangerTint13     = Color(hex: "#FF6B6B").opacity(0.13) // step-num bad bg (Setup)
        static let dangerBorder40   = Color(hex: "#FF6B6B").opacity(0.40) // step-num bad border (Setup)
        static let dangerStepSub    = Color(hex: "#FF8B8B")               // step-bad-sub (= dangerText)

        // Setup screen (01 / spec §6) — verbatim from mockups/01-setup.html.
        // step-num circles: ok teal .14/.40, cur cyan .14/.45, bad danger above.
        // (mockup step-ok uses the same teal #34E0D2 as the success family.)
        static let stepOkBg         = Color(hex: "#34E0D2").opacity(0.14) // .step-ok bg
        static let stepOkBorder     = Color(hex: "#34E0D2").opacity(0.40) // .step-ok border
        static let stepOkSub        = Color(hex: "#34E0D2")               // .step-ok-sub text
        static let stepCurBg        = Color(hex: "#22B5E0").opacity(0.14) // .step-cur bg
        static let stepCurBorder    = Color(hex: "#22B5E0").opacity(0.45) // .step-cur border
        static let stepCurText      = Color(hex: "#22B5E0")               // .step-cur number
        // recheck button (cyan tint .12 / border .40, teal text) — Setup "Проверить снова".
        static let recheckBg        = Color(hex: "#22B5E0").opacity(0.12) // .recheck bg
        static let recheckBorder    = Color(hex: "#22B5E0").opacity(0.40) // .recheck border
        // copy-btn / field-btn text (.copy-btn #9fb2bd = textMuted; field-btn text = textHigh).
        static let installBtnText   = Color(hex: "#9fb2bd")               // .copy-btn text

        // warn (#FFB454) — recoverable (broken mp3 "build without it").
        static let warnBase         = Color(hex: "#FFB454") // icon / stroke
        static let warnTextSoft     = Color(hex: "#FFE3B8") // warn primary bbtn text
        static let warnTint10       = Color(hex: "#FFB454").opacity(0.10) // banner bg
        static let warnTint16       = Color(hex: "#FFB454").opacity(0.16) // warn primary bbtn bg
        static let warnBorder50     = Color(hex: "#FFB454").opacity(0.50) // warn primary bbtn border
        static let warnBorder30     = Color(hex: "#FFB454").opacity(0.30) // banner border

        // Progress (converting state) — color.surfaceFill.progressTrack + gradient.progressFill.
        static let progressTrack    = Color.white(0.08) // .progress-track bg

        // Queue screen (07 / spec §7) — lifted verbatim from mockups/07-queue.html.
        // qbtn (base accent): border rgba(34,181,224,.4) · bg rgba(34,181,224,.14).
        static let qbtnBorder       = Color(hex: "#22B5E0").opacity(0.40) // .qbtn border
        static let qbtnBg           = Color(hex: "#22B5E0").opacity(0.14) // .qbtn bg
        // qbtn.warn: border rgba(255,180,84,.4) · bg rgba(255,180,84,.12).
        static let qbtnWarnBorder   = Color(hex: "#FFB454").opacity(0.40) // .qbtn.warn border
        static let qbtnWarnBg       = Color(hex: "#FFB454").opacity(0.12) // .qbtn.warn bg
        // batch-chip: bg rgba(34,181,224,.1) · border rgba(34,181,224,.25).
        static let batchChipBg      = Color(hex: "#22B5E0").opacity(0.10) // .batch-chip bg
        static let batchChipBorder  = Color(hex: "#22B5E0").opacity(0.25) // .batch-chip border
        // sec-cap count pill (.sec-cap .n): bg rgba(255,255,255,.07).
        static let secCapPillBg     = Color.white(0.07)                    // .sec-cap .n bg
        // confirm-all (dashed): border rgba(34,181,224,.35) · bg rgba(34,181,224,.06).
        static let confirmAllBorder = Color(hex: "#22B5E0").opacity(0.35) // .confirm-all border
        static let confirmAllBg     = Color(hex: "#22B5E0").opacity(0.06) // .confirm-all bg
        // qstatus-ic disc backings: done rgba(52,224,210,.14) · error rgba(255,180,84,.14).
        static let qStatusOkBg      = Color(hex: "#34E0D2").opacity(0.14) // done disc bg
        static let qStatusErrBg     = Color(hex: "#FFB454").opacity(0.14) // error disc bg
        // empty-ic backing: bg rgba(255,255,255,.04) · border rgba(255,255,255,.07).
        static let emptyIcBorder    = Color.white(0.07)                    // .empty-ic border
        // qsub semantic colors: ok #34E0D2 (= brandCyan) · err #FFB454 (= warnBase) — aliases.
        static let qSubOk           = brandCyan                            // .qsub.ok
        static let qSubErr          = warnBase                             // .qsub.err

        // Status screen (02 / spec §5) — verbatim from mockups/02-status.html.
        // hero/footer "Активно" pill + "Активен" badge-ok: teal #34E0D2 tint .12 /
        // border .28, live dot #34E0D2 (color.state.successTint12 / successBorder28).
        static let pillOkBg         = Color(hex: "#34E0D2").opacity(0.12) // .pill / .badge-ok bg
        static let pillOkBorder     = Color(hex: "#34E0D2").opacity(0.28) // .pill / .badge-ok border
        static let liveDot          = brandCyan                            // pill/foot live dot #34E0D2
        // stat-bar / ring track (color.surfaceFill.barTrack = rgba(255,255,255,.07)).
        static let barTrack         = Color.white(0.07)                    // .bar bg / ring background
        // recent-book error tile + name (spec §5 warn variant): warn tint .14, name muted.
        static let bookWarnTileBg   = Color(hex: "#FFB454").opacity(0.14) // .book.err cover tile
        static let bookNameErr      = Color(hex: "#8a99a3")               // .book.err .book-name (= textMutedAlt-ish #8a99a3)
        static let bookSmall        = Color(hex: "#6E8390")               // .book-name small / .book-time (= textTertiary)
        // row-ic tint backings (spec §5 group rows): teal .12 / brandTeal .12 / indigo .12.
        static let rowIcTealBg      = Color(hex: "#34E0D2").opacity(0.12) // agent row-ic (cyan)
        static let rowIcBrandTealBg = Color(hex: "#22B5E0").opacity(0.12) // folder row-ic (teal)
        static let rowIcIndigoBg    = Color(hex: "#4A6BFF").opacity(0.12) // queue row-ic (indigo)
        // clear-btn (recent-books header): faint surface .03 / border .07, text tertiary.
        static let clearBtnBg       = Color.white(0.03)                    // .clear-btn bg
        static let clearBtnBorder   = Color.white(0.07)                    // .clear-btn border
        // credit GitHub link (spec §5/6): color.accent.linkBlue #5B9DF9.
        static let linkBlue         = Color(hex: "#5B9DF9")               // .credit a
        // stat-value semantic colors (spec §5): Собрано cyan · Сегодня indigo · ffmpeg cyan.
        static let statBuilt        = brandCyan                            // .stat-val #34E0D2
        static let statToday        = brandIndigo                          // .stat-val #4A6BFF
        static let statEngine       = brandCyan                            // .stat-val ffmpeg #34E0D2
    }

    // MARK: - Window canvas gradient (color.canvas.window)

    enum Canvas {
        /// The window-background radial gradient, verbatim from
        /// color.canvas.window: radial "120% 70% at 50% -6%" with four stops.
        /// SwiftUI RadialGradient is centered+radius; we approximate the CSS
        /// "at 50% -6%" by anchoring near the top edge. (Refined per-screen as
        /// real screens land; M0.1 only needs the empty window filled.)
        static let windowGradient = RadialGradient(
            gradient: Gradient(stops: [
                .init(color: C.canvasStop0, location: 0.00),
                .init(color: C.canvasStop1, location: 0.38),
                .init(color: C.canvasStop2, location: 0.64),
                .init(color: C.canvasStop3, location: 1.00),
            ]),
            center: UnitPoint(x: 0.5, y: -0.06),
            startRadius: 0,
            endRadius: 520
        )

        /// Grouping sheet background radial (06 mockup `.sheet`):
        /// "120% 80% at 50% -8%" stops #14202A→#0E1822→#0a1018→#070B10. The sheet is
        /// a compact 440-wide card, so a tighter endRadius than the full window.
        static let sheetGradient = RadialGradient(
            gradient: Gradient(stops: [
                .init(color: C.canvasStop0, location: 0.00),
                .init(color: C.canvasStop1, location: 0.42),
                .init(color: C.canvasStop2, location: 0.70),
                .init(color: C.canvasStop3, location: 1.00),
            ]),
            center: UnitPoint(x: 0.5, y: -0.08),
            startRadius: 0,
            endRadius: 360
        )

        /// App-icon backing radial (color.canvas.appIcon): "120% 120% at 50% 28%"
        /// stops #15212B→#0C141C→#070B10. Used behind the header app-icon.
        static let appIconGradient = RadialGradient(
            gradient: Gradient(stops: [
                .init(color: Color(hex: "#15212B"), location: 0.00),
                .init(color: Color(hex: "#0C141C"), location: 0.60),
                .init(color: Color(hex: "#070B10"), location: 1.00),
            ]),
            center: UnitPoint(x: 0.5, y: 0.28),
            startRadius: 0,
            endRadius: 26
        )
    }

    // MARK: - Brand gradients (gradient.*)

    enum Grad {
        /// 135° brand gradient (gradient.brand): cover / mini-cover.
        static let brand = LinearGradient(
            gradient: Gradient(stops: [
                .init(color: C.brandCyan, location: 0.00),
                .init(color: C.brandTeal, location: 0.48),
                .init(color: C.brandIndigo, location: 1.00),
            ]),
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )

        /// 135° primary-button gradient (gradient.brandButton): mid at 45%.
        static let brandButton = LinearGradient(
            gradient: Gradient(stops: [
                .init(color: C.brandCyan, location: 0.00),
                .init(color: C.brandTeal, location: 0.45),
                .init(color: C.brandIndigo, location: 1.00),
            ]),
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )

        /// 135° two-tone teal→indigo (gradient.brandTealIndigo): toggle.on,
        /// count-badge, slider-fill.
        static let brandTealIndigo = LinearGradient(
            gradient: Gradient(stops: [
                .init(color: C.brandTeal, location: 0.00),
                .init(color: C.brandIndigo, location: 1.00),
            ]),
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )

        /// 90° linear build-progress bar (gradient.progressFill): converting state.
        static let progressFill = LinearGradient(
            gradient: Gradient(stops: [
                .init(color: C.brandCyan, location: 0.00),
                .init(color: C.brandTeal, location: 0.60),
                .init(color: C.brandIndigo, location: 1.00),
            ]),
            startPoint: .leading,
            endPoint: .trailing
        )

        /// Hero progress-ring stroke (gradient.ring): linear cyan→teal→indigo applied
        /// to the arc (Status §5). Top-leading → bottom-trailing approximates the
        /// CSS linearGradient(x1 0 y1 0 x2 1 y2 1) the mockup uses.
        static let ring = LinearGradient(
            gradient: Gradient(stops: [
                .init(color: C.brandCyan, location: 0.00),
                .init(color: C.brandTeal, location: 0.50),
                .init(color: C.brandIndigo, location: 1.00),
            ]),
            startPoint: .topLeading,
            endPoint: .bottomTrailing
        )

        /// 90° stat-bar fills (Status §5 mini-bars). Three tints by card:
        /// barSolid cyan→teal (Собрано), barTealIndigo teal→indigo (За сегодня),
        /// barDeep deep-teal→cyan (ffmpeg). Verbatim from gradient.bar* in tokens.
        static let barSolid = LinearGradient(
            gradient: Gradient(stops: [
                .init(color: C.brandCyan, location: 0.00),
                .init(color: C.brandTeal, location: 1.00),
            ]),
            startPoint: .leading, endPoint: .trailing
        )
        static let barTealIndigo = LinearGradient(
            gradient: Gradient(stops: [
                .init(color: C.brandTeal, location: 0.00),
                .init(color: C.brandIndigo, location: 1.00),
            ]),
            startPoint: .leading, endPoint: .trailing
        )
        static let barDeep = LinearGradient(
            gradient: Gradient(stops: [
                .init(color: Color(hex: "#1d9e96"), location: 0.00),
                .init(color: C.brandCyan, location: 1.00),
            ]),
            startPoint: .leading, endPoint: .trailing
        )
    }

    // MARK: - Radii (radius.*)

    enum R {
        static let window      : CGFloat = 16 // .win / .sheet
        static let card        : CGFloat = 12 // cards/lists, cover-box, quality box
        static let heroCard    : CGFloat = 16 // status hero-card (radius.window)
        static let statCard    : CGFloat = 13 // status stat-card (.stat radius 13)
        static let estimate    : CGFloat = 11 // estimate block, split-preview
        static let control     : CGFloat = 10 // input, buttons
        static let appIconConfirm: CGFloat = 9 // app-icon in confirm-core header (34px)
        static let cvBtn       : CGFloat = 9  // cv-btn, controlSmall
        static let sheetIcon   : CGFloat = 13 // grouping sheet-icon (radius.sheetIcon)
        static let chip        : CGFloat = 8  // small chips
        static let fileChip    : CGFloat = 6  // grouping file-chip
        static let small       : CGFloat = 7  // preset/seg/badge
        static let togglePill  : CGFloat = 12 // toggle track (pill)
        static let sliderTrack : CGFloat = 4  // split threshold slider track (radius.sliderTrack)
    }

    // MARK: - Metrics / sizes (size.window, layout)

    enum M {
        // Window widths (size.window)
        static let windowStandard : CGFloat = 400 // status/setup/queue
        static let windowConfirm  : CGFloat = 640 // confirm window (core)
        static let windowStates   : CGFloat = 560 // states window
        static let windowSheet    : CGFloat = 440 // grouping sheet
        static let windowPanel    : CGFloat = 300 // cover/split panels

        /// Right column width in the confirm window (grid 1fr / 280px).
        static let windowRightColumn: CGFloat = 280

        /// Confirm right-column cover controls (compact D-fit): the big selected-
        /// cover preview is capped to a 1:1 square of this side (was full-column
        /// ~242pt — it pushed the lower controls off a ~896pt screen), and the
        /// picker thumbnails to this side. Shrinking the preview is the main
        /// height saving so the whole right column (cover → quality → split →
        /// estimate) fits with no inner scroll on a typical laptop screen.
        static let confirmCoverMax  : CGFloat = 160 // big preview 1:1 cap
        static let confirmCoverThumb: CGFloat = 46  // picker thumbnail side

        // Status screen (02 / spec §5) — verbatim from mockups/02-status.html.
        static let ringSizeHero : CGFloat = 104 // hero progress ring (size.ring.heroSize)
        static let pillDot      : CGFloat = 6   // pill live dot (size.dot.pill)
        /// Max viewport height for the Status "last-built books" list (the only
        /// variable-length, scrollable section on Status). Bounds the section so it
        /// can never push the footer off-screen: a longer/taller history scrolls
        /// INSIDE this viewport instead of growing the window. Sized to hold FIVE
        /// rows without an internal scroll (measured 39pt/row + the card header +
        /// card padding: 5 rows → section ≈ 248pt < 264; a 6th row → section hits
        /// this 264 cap and the list scrolls). Matches the digest cap (orderedRecent
        /// prefix = 8) so "до 5 книг видно, больше 5 — скроллится" holds. Deterministic
        /// by construction — a fixed cap, no GeometryReader self-measurement (.patches/002).
        static let recentListMax: CGFloat = 264

        /// Max viewport height for the Queue's sections list (07 / spec §7). The queue
        /// is ALL variable length (four status sections of qrows), so the whole
        /// sections ScrollView is bounded here: a long queue (many books across
        /// sections) scrolls INSIDE this viewport instead of growing the window and
        /// pushing the footer/credit off-screen (the bug fixed here — same trap +
        /// remedy as Status's recentListMax / sibling lesson
        /// [[native-window-cap-height-test-max-content]]). Larger than recentListMax
        /// because the queue is the dedicated full list (its own screen), not a home
        /// digest. A short queue shrinks to fit; the AppDelegate window-cap is the
        /// outer belt. Deterministic — a fixed cap, no GeometryReader self-measurement
        /// (.patches/002).
        static let queueListMax: CGFloat = 460

        // Grouping sheet thumbnails (size.thumb.* — spec §6 / ref 06).
        static let sheetIcon : CGFloat = 48 // dialog icon (48×48)
        static let choiceIc  : CGFloat = 42 // per-choice icon (42×42)
        static let choiceRadio: CGFloat = 20 // choice radio (20×20)

        // Queue screen (07 / spec §7) — verbatim from mockups/07-queue.html.
        static let qrowCover  : CGFloat = 38 // .qcov / .qstatus-ic row leading square is 38/26
        static let qStatusIc  : CGFloat = 26 // .qstatus-ic (done/error disc) 26×26
        static let batchRing  : CGFloat = 16 // .batch-chip .ring (mini progress ring)
        static let backBtn    : CGFloat = 30 // .back-btn (header back) 30×30
        static let emptyIc    : CGFloat = 60 // .empty-ic 60×60

        /// Default content width for the app's primary window. M0.1's empty
        /// window uses the standard 400px (the Status/Setup width).
        static let windowWidth = windowStandard
    }

    // MARK: - Type sizes (font.size) — seeded subset

    enum F {
        static let title     : CGFloat = 17  // hdr h1 (status/setup)
        static let estBig    : CGFloat = 17  // estimate big number (.est-big)
        static let h1Confirm : CGFloat = 16  // hdr h1 (confirm-core, queue)
        static let heroMetric: CGFloat = 24  // status hero-metric number (.hero-metric)
        static let statVal   : CGFloat = 21  // status stat-val (.stat-val)
        static let ringCenter: CGFloat = 20  // status ring-center number (.ring-center)
        static let input     : CGFloat = 14  // .inp (title/author fields), btn-primary
        static let coverTitle: CGFloat = 18  // cover-grad title (.ct)
        static let body      : CGFloat = 13  // primary body / ch-name / btn-ghost
        static let caption   : CGFloat = 12  // caption / cover author (.ca)
        static let chDur     : CGFloat = 11.5 // ch-dur, cv-btn, preset, seg
        static let small     : CGFloat = 11  // secondary (hdr sub, ch-n, lbl, est-sub)
        static let fieldLabel : CGFloat = 12  // Автор/Название field labels (small +1, per feedback)
        static let capLg     : CGFloat = 10  // ГЛАВЫ cap (cap +1, per feedback)
        static let qSuffix   : CGFloat = 10.5 // q-suffix "кГц"
        static let badge     : CGFloat = 10  // cover-badge label
        static let cap       : CGFloat = 9   // caps micro-labels (.sec-cap)

        // Queue empty-state (07 / spec §7): heading 15 + body 12.5 (.empty h3 / p).
        static let emptyTitle: CGFloat = 15   // .empty h3
        static let emptyBody : CGFloat = 12.5 // .empty p / .caption
    }
}
