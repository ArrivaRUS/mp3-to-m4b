// EngineClient+Status — app-side "Очистить" / "Сбросить статистику" (Status screen).
//
// PORTED 1:1 from the fb2-to-epub neighbor (fb2/app/EngineClient+Status.swift
// ~41-101 + 218-285). The architecture is identical: the AGENT is the SINGLE
// writer of state.json (D13). So the app NEVER rewrites state.json to clear the
// recent list or zero the counters — a write here would race the agent and
// corrupt the file. Instead we keep APP-OWNED MARKER FILES and apply them on READ:
//
//   • recent-cleared-at  — an ISO-8601 instant. The Status "Последние собранные
//     книги" list hides every done/error book built AT OR BEFORE this instant.
//     Future builds (newer than the marker) reappear naturally.
//   • stats-baseline     — {built} captured at reset. loadState() subtracts it
//     from `totals.built` so the "СОБРАНО" card reads from zero; new builds count
//     again (current − baseline, clamped ≥ 0).
//   • today-baseline     — {date, today} captured at reset. Subtracted from
//     `totals.today` ONLY while the day is unchanged; once the local day rolls
//     over the baseline expires (else it would bury tomorrow's count at 0).
//
// The markers live under the SAME support tree the reader uses
// (`~/Library/Application Support/mp3-to-m4b/state/`, honoring
// MP3TOM4B_SUPPORT_DIR), so throwaway-SUPPORT test runs stay isolated.
//
// NOTE ON OUR DATA SHAPE (vs. fb2): fb2 keeps a flat `recent[]` in state.json with
// a per-entry `ts`, so it filters that array. WE derive the recent list from
// `state.books[]` (status done/error) with the build time living in each book's
// MANIFEST (`result.built_at`). So the recent-list hiding is applied in the Status
// layer (where the manifest lookup is available) via `StatusMarkers.recentClearedAt`
// + a per-book `builtAt` compare — same SEMANTICS as fb2, adapted to our model.
// The `totals` baselining needs no manifest and is applied right in
// `StateStore.loadState()`.

import Foundation

// MARK: - StatusMarkers (app-owned marker files, rooted at a support tree)

/// The three app-owned marker files that back "Очистить" / "Сбросить статистику".
/// Rooted at a `supportRoot` (the SAME tree `StateStore` reads), so tests that
/// redirect MP3TOM4B_SUPPORT_DIR never touch the user's real markers.
///
/// This is a small value type shared by BOTH sides of the feature:
///   • the READ side (`StateStore.loadState()` + the Status list filter) reads the
///     markers and applies them;
///   • the WRITE side (`EngineClient.clearHistory()` / `resetStats()`) stamps them.
struct StatusMarkers {
    let stateDir: URL

    /// Build from the same support root `StateStore` uses. The markers sit next to
    /// the agent's state.json under `state/` — the app owns these files; the agent
    /// owns state.json.
    init(supportRoot: URL) {
        self.stateDir = supportRoot.appendingPathComponent("state", isDirectory: true)
    }

    // --- Marker paths --------------------------------------------------------
    // `~/Library/Application Support/mp3-to-m4b/state/<marker>` (fb2 parity).
    private var recentClearedAtURL: URL { stateDir.appendingPathComponent("recent-cleared-at") }
    private var statsBaselineURL: URL { stateDir.appendingPathComponent("stats-baseline") }
    private var todayBaselineURL: URL { stateDir.appendingPathComponent("today-baseline") }

    // MARK: - Read side (apply on load)

    /// The "recent cleared at" instant, or nil when absent / empty / unparseable.
    /// The Status list keeps only books built strictly AFTER this instant.
    func recentClearedAt() -> Date? {
        guard let raw = try? String(contentsOf: recentClearedAtURL, encoding: .utf8) else {
            return nil
        }
        return StatusMarkers.parseISO(raw.trimmingCharacters(in: .whitespacesAndNewlines))
    }

    /// The "built" baseline captured at reset (raw `totals.built` at that moment).
    /// nil when absent / unreadable / malformed (fail-open: show the raw total
    /// rather than hide a number because the marker is odd).
    private func statsBaselineBuilt() -> Int? {
        guard let data = try? Data(contentsOf: statsBaselineURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let base = obj["built"] as? Int else { return nil }
        return base
    }

    /// The "за сегодня" baseline as (date, today). nil when absent / unreadable /
    /// malformed (fail-open: show the raw `today`).
    private func todayBaseline() -> (date: String, today: Int)? {
        guard let data = try? Data(contentsOf: todayBaselineURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let date = obj["date"] as? String,
              let today = obj["today"] as? Int else { return nil }
        return (date, today)
    }

    /// Apply the "Сбросить статистику" baselines to a decoded showcase's `totals`
    /// (built + today). Pure — returns a copy with adjusted counters; recent-list
    /// hiding is separate (it needs manifests, done in the Status layer).
    ///
    ///   • built  → max(0, raw built − baseline.built)  (card reads 0 at reset).
    ///   • today  → max(0, raw today − baseline.today) WHILE the local day matches
    ///     the baseline's date; once the day rolls over the baseline is ignored so
    ///     tomorrow's fresh count from the agent shows through (fb2 D13-safe rule).
    ///     Our state.json has no `_today_date` (the agent recomputes `today` each
    ///     scan from `datetime.now()`), so "current day" = our local day, which
    ///     matches the agent's local `datetime.now()` exactly.
    func applyingBaselines(to state: ShowcaseState) -> ShowcaseState {
        var s = state
        if let base = statsBaselineBuilt() {
            s.totals.built = max(0, s.totals.built - base)
        }
        if let tb = todayBaseline() {
            if tb.date == StatusMarkers.localDayString() {
                s.totals.today = max(0, s.totals.today - tb.today)
            }
            // Different day → baseline expired: leave s.totals.today as-is.
        }
        return s
    }

    // MARK: - Write side (stamp the markers)

    /// Stamp "recent-cleared-at" = now (ISO-8601 UTC, Z). The Status list then hides
    /// every done/error book built at or before this instant. Atomic (tmp → rename
    /// via .atomic). Best-effort: a write failure simply leaves the history as-is.
    func stampRecentCleared() {
        ensureDir()
        let stamp = StatusMarkers.iso8601Now()
        try? stamp.write(to: recentClearedAtURL, atomically: true, encoding: .utf8)
    }

    /// Stamp the "built" baseline = the given raw lifetime built count. Atomic.
    func stampStatsBaseline(built: Int) {
        ensureDir()
        let payload: [String: Any] = ["built": built, "ts": StatusMarkers.iso8601Now()]
        if let data = try? JSONSerialization.data(withJSONObject: payload,
                                                  options: [.sortedKeys]) {
            try? data.write(to: statsBaselineURL, options: .atomic)
        }
    }

    /// Stamp the "за сегодня" baseline = (today's local day, the given raw `today`).
    /// The date lets `applyingBaselines` expire the baseline once the day rolls over.
    func stampTodayBaseline(today: Int) {
        ensureDir()
        let payload: [String: Any] = [
            "date": StatusMarkers.localDayString(),
            "today": today,
            "ts": StatusMarkers.iso8601Now(),
        ]
        if let data = try? JSONSerialization.data(withJSONObject: payload,
                                                  options: [.sortedKeys]) {
            try? data.write(to: todayBaselineURL, options: .atomic)
        }
    }

    private func ensureDir() {
        try? FileManager.default.createDirectory(at: stateDir,
                                                 withIntermediateDirectories: true)
    }

    // MARK: - Time helpers (match the agent's shapes)

    /// ISO-8601 UTC with trailing Z — the shape `parseISO` expects and the agent's
    /// Python side writes.
    static func iso8601Now() -> String {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f.string(from: Date())
    }

    /// Parse an ISO-8601 instant (with or without fractional seconds). nil on junk.
    static func parseISO(_ s: String) -> Date? {
        guard !s.isEmpty else { return nil }
        let f1 = ISO8601DateFormatter()
        f1.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f1.date(from: s) { return d }
        let f2 = ISO8601DateFormatter()
        f2.formatOptions = [.withInternetDateTime]
        return f2.date(from: s)
    }

    /// Local-day string "yyyy-MM-dd" in the CURRENT timezone — matches the agent's
    /// Python `datetime.now(tz).date()` (local, no tz suffix). POSIX locale keeps it
    /// digits-only regardless of the user's locale.
    static func localDayString(_ now: Date = Date()) -> String {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.calendar = Calendar(identifier: .gregorian)
        f.timeZone = TimeZone.current
        f.dateFormat = "yyyy-MM-dd"
        return f.string(from: now)
    }
}

// MARK: - StateStore convenience (read side)

extension StateStore {
    /// The app-owned markers rooted at THIS store's support tree.
    var statusMarkers: StatusMarkers { StatusMarkers(supportRoot: supportRoot) }

    /// The "recent cleared at" cutoff, for the Status list's per-book `builtAt`
    /// filter (a done/error book built at or before this is hidden). nil = no clear.
    func recentClearedAt() -> Date? { statusMarkers.recentClearedAt() }
}

// MARK: - EngineClient actions (write side)

extension EngineClient {
    /// App-owned markers rooted at the same support tree the client writes commands
    /// into (via its StateStore).
    private var markers: StatusMarkers { store.statusMarkers }

    /// "Очистить" the recent-books list on the Status screen.
    ///
    /// We do NOT rewrite state.json — the agent owns it (D13), so touching it would
    /// race the agent. Instead we stamp an app-owned "recent-cleared-at" marker;
    /// the Status list then hides every done/error book built at or before that
    /// instant. Newer builds reappear; the lifetime `totals` are unaffected (we
    /// clear the visible list, not the counters). Best-effort.
    func clearHistory() {
        markers.stampRecentCleared()
    }

    /// "Сбросить статистику": zero the visible "СОБРАНО" + "ЗА СЕГОДНЯ" counters AND
    /// hide the current recent list.
    ///
    /// Same contract as clearHistory(): we NEVER rewrite state.json (the agent owns
    /// it — D13). We capture the current raw counters as app-owned baselines;
    /// `StateStore.loadState()` then subtracts them so the cards read zero now and
    /// future builds count again. Three app-owned markers, each written atomically:
    ///   • stats-baseline = {built} → zeroes "СОБРАНО" (current − base).
    ///   • today-baseline = {date, today} → zeroes "ЗА СЕГОДНЯ" for TODAY only; it
    ///     expires once the local day rolls over (so tomorrow is never buried at 0).
    ///   • recent-cleared-at (via clearHistory) → hides the current recent list.
    /// Best-effort: a failure on any marker simply leaves that part as-is.
    func resetStats() {
        // Read the CURRENT raw counters straight from the store (unbaselined — this
        // is the raw agent snapshot, the same one loadState decodes before applying
        // markers). Capturing the raw values is what makes the cards read zero.
        let raw = store.loadRawTotals()
        markers.stampStatsBaseline(built: raw.built)
        markers.stampTodayBaseline(today: raw.today)
        clearHistory()
    }
}
