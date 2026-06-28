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
        static let textSoft        = Color(hex: "#cfe0e7") // ghost-button text
        static let textMuted       = Color(hex: "#9fb2bd") // muted mid
        static let textSecondary   = Color(hex: "#7e93a0") // default secondary
        static let textTertiary    = Color(hex: "#6E8390") // caps micro-labels
        static let textQuaternary  = Color(hex: "#5a6b76") // very muted
        static let textPlaceholder = Color(hex: "#4a5862") // input placeholder
        static let textOnAccent    = Color(hex: "#06121a") // on bright accent

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
    }

    // MARK: - Radii (radius.*)

    enum R {
        static let window      : CGFloat = 16 // .win / .sheet
        static let card        : CGFloat = 12 // cards/lists
        static let control     : CGFloat = 10 // input, buttons
        static let appIconConfirm: CGFloat = 9 // app-icon in confirm-core header (34px)
        static let chip        : CGFloat = 8  // small chips
        static let small       : CGFloat = 7  // preset/seg/badge
    }

    // MARK: - Metrics / sizes (size.window, layout)

    enum M {
        // Window widths (size.window)
        static let windowStandard : CGFloat = 400 // status/setup/queue
        static let windowConfirm  : CGFloat = 640 // confirm window (core)
        static let windowStates   : CGFloat = 560 // states window
        static let windowSheet    : CGFloat = 440 // grouping sheet
        static let windowPanel    : CGFloat = 300 // cover/split panels

        /// Default content width for the app's primary window. M0.1's empty
        /// window uses the standard 400px (the Status/Setup width).
        static let windowWidth = windowStandard
    }

    // MARK: - Type sizes (font.size) — seeded subset

    enum F {
        static let title    : CGFloat = 17  // hdr h1 (status/setup)
        static let h1Confirm: CGFloat = 16  // hdr h1 (confirm-core, queue)
        static let body     : CGFloat = 13  // primary body / ch-name
        static let caption  : CGFloat = 12  // caption
        static let chDur    : CGFloat = 11.5 // ch-dur, cv-btn, preset
        static let small    : CGFloat = 11  // secondary (hdr sub, ch-n)
        static let cap      : CGFloat = 9   // caps micro-labels (.sec-cap)
    }
}
