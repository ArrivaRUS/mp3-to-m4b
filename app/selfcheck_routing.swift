// §app-routing self-check — two app-level rules that BOTH shipped broken in v0.9:
//   1. WHICH BOOK the confirm window presents (ShowcaseState.presentedBook);
//   2. that the window can never end up OFF SCREEN (WindowGeometry).
//
// This guards the contract behind the queue's «Подтвердить» button (QueueView :41):
// pressing it on a ROW must open THAT book's confirm window. It shipped broken in
// v0.9 — main.swift threw the row's book away (`onConfirm: { _ in … }`) and the
// window always rendered `activeBooks.first`, so confirming the SECOND book opened
// the FIRST one and «Собрать» built the wrong book. Third bug of the same class
// (lesson 005), hence a permanent regression guard rather than a one-off fix.
//
// It exercises the PURE routing rule that both paths now share —
// `ShowcaseState.presentedBook(selectedID:)` / `.activePosition(of:)` in
// app/StateModel.swift — so a future refactor cannot silently reintroduce
// "always the first book" without turning this red.
//
// The window-geometry half guards two measured off-screen bugs: the confirm
// window (640 wide, taller) opened from the queue (400 wide) used to hang up to
// 240pt past the RIGHT edge and up to 357pt BELOW the bottom edge — and the
// footer's «Собрать» lives in exactly those overhangs. The scenarios below are
// the five raw AppKit measurements (A/B/C/D + control E) that were taken with a
// live NSWindow probe, frozen here as fixtures against the pure rule so the
// guarantee is a permanent GATE rather than a one-off measurement.
//
// NOT part of the app binary: build/build-app.sh lists its sources explicitly
// (SWIFT_SRCS) and this file is not among them.
//
// Run it:
//     python3 -m agent.selfcheck_app_routing     (also inside selfcheck_all)

import Foundation

@main
struct RoutingSelfCheck {

    // MARK: tiny harness (same shape/format as the python suites)

    static var results: [(name: String, ok: Bool)] = []

    static func check(_ name: String, _ ok: Bool, _ detail: String = "") {
        results.append((name, ok))
        var line = "  [\(ok ? "PASS" : "FAIL")] \(name)"
        if !detail.isEmpty { line += " — \(detail)" }
        print(line)
    }

    // MARK: fixtures

    static func book(_ id: String, _ title: String, _ status: String) -> BookSummary {
        BookSummary(bookID: id, title: title, status: status,
                    author: "", chapterCount: 3, totalDurationMS: 600_000)
    }

    static func state(_ books: [BookSummary]) -> ShowcaseState {
        ShowcaseState(schema: 1, agent: AgentInfo(watchDir: "/tmp/watch"),
                      books: books, batch: nil)
    }

    // MARK: window geometry — «окно всегда целиком на экране»

    /// The real screen the live probe measured on: visibleFrame x 0…1512,
    /// y 52…949 (897 tall, 52pt Dock strip at the bottom, menu bar above 949).
    static let screen = CGRect(x: 0, y: 52, width: 1512, height: 897)

    /// Fully inside the visible area, with a 1pt tolerance for float noise.
    static func onScreen(_ f: CGRect, _ v: CGRect) -> Bool {
        f.minX >= v.minX - 1 && f.maxX <= v.maxX + 1
            && f.minY >= v.minY - 1 && f.maxY <= v.maxY + 1
    }

    static func checkWindowGeometry() {
        let v = screen

        // The frames below are what the app PRODUCES before clamping: the queue
        // window's top-left is pinned, so the wider/taller confirm window grows
        // right and down from it. Heights come from real AppKit measurements
        // (564 = 400-wide queue window, 732 = an ordinary book, 889 = the
        // cappedContentHeight ceiling on this screen — the 56-chapter case).

        // A — window parked low (top at 662), ordinary book: grows to h=732.
        let a = WindowGeometry.clampedVertically(
            CGRect(x: 556, y: -70, width: 640, height: 732), in: v)
        check("A: окно низко → низ был за экраном на 122 pt → поднято на экран",
              onScreen(a, v) && a.minY == v.minY, "y \(a.minY)…\(a.maxY)")

        // B — MAXIMUM content (56 chapters → capped h=889), same low window.
        let b = WindowGeometry.clampedVertically(
            CGRect(x: 556, y: -227, width: 640, height: 889), in: v)
        check("B: МАКСИМУМ контента (h=889) низко → было 279 pt за экраном → на экране",
              onScreen(b, v) && b.minY == v.minY, "y \(b.minY)…\(b.maxY)")

        // C — MAXIMUM content + window pressed to the very bottom (worst case).
        let c = WindowGeometry.clampedVertically(
            CGRect(x: 556, y: -305, width: 640, height: 889), in: v)
        check("C: МАКСИМУМ + окно у самого низа → было 357 pt за экраном → на экране",
              onScreen(c, v) && c.minY == v.minY, "y \(c.minY)…\(c.maxY)")

        // D — bottom-RIGHT corner: both axes, applied the way the app applies them
        // (width clamp in applyWindowWidth, then height clamp in refitWindowHeight).
        let dRaw = CGRect(x: 1112, y: -70, width: 640, height: 732)
        let d = WindowGeometry.clampedVertically(
            WindowGeometry.clampedHorizontally(dRaw, in: v), in: v)
        check("D: правый нижний угол → было 240 pt вправо и 122 pt вниз → на экране",
              onScreen(d, v) && d.maxX == v.maxX && d.minY == v.minY,
              "x \(d.minX)…\(d.maxX)  y \(d.minY)…\(d.maxY)")

        // E — CONTROL: a window that already fits must not be touched at all.
        let eRaw = CGRect(x: 556, y: 207, width: 640, height: 732)
        let e = WindowGeometry.clampedVertically(
            WindowGeometry.clampedHorizontally(eRaw, in: v), in: v)
        check("E КОНТРОЛЬ: окно и так помещается → не сдвинуто ни на пиксель",
              e == eRaw, "\(e)")

        // Degenerate cases — which edge wins when the window is BIGGER than the
        // screen. The cap keeps these out of reach; the priority is still fixed
        // here so a refactor cannot quietly flip it.
        let tall = WindowGeometry.clampedVertically(
            CGRect(x: 556, y: -200, width: 640, height: 1000), in: v)
        check("выше экрана → приколот ВЕРХ (тайтлбар доступен, окно можно утащить)",
              tall.maxY == v.maxY, "y \(tall.minY)…\(tall.maxY)")
        let wide = WindowGeometry.clampedHorizontally(
            CGRect(x: 300, y: 207, width: 1600, height: 732), in: v)
        check("шире экрана → приколот ЛЕВЫЙ край (видно начало содержимого)",
              wide.minX == v.minX, "x \(wide.minX)…\(wide.maxX)")

        // Axis independence: each caller fixes only the axis it just changed, so a
        // window the user dragged low is never yanked sideways and vice versa.
        let off = CGRect(x: -50, y: -50, width: 640, height: 732)
        check("clampedVertically не трогает X (ось, которую менял другой вызов)",
              WindowGeometry.clampedVertically(off, in: v).minX == off.minX)
        check("clampedHorizontally не трогает Y",
              WindowGeometry.clampedHorizontally(off, in: v).minY == off.minY)
        check("клэмп НИКОГДА не меняет размер окна (только позицию)",
              WindowGeometry.clampedVertically(off, in: v).size == off.size
                  && WindowGeometry.clampedHorizontally(off, in: v).size == off.size)
    }

    // MARK: checks

    static func main() {
        // The reproduction from the bug report: two books awaiting confirmation.
        let first = book("b1", "Двенадцать стульев", "pending-confirm")
        let second = book("b2", "Тайна старого маяка", "pending-confirm")
        let queue = state([first, second])

        // 1. THE BUG. Pressing «Подтвердить» on the second row opens the SECOND book.
        check("pick #2 → окно показывает именно #2 (был показ #1)",
              queue.presentedBook(selectedID: "b2")?.bookID == "b2",
              "got \(queue.presentedBook(selectedID: "b2")?.title ?? "nil")")

        // 2. …and the header counter follows it, so it can't claim "1" while
        //    rendering book two.
        check("позиция #2 в шапке = 2 (было жёстко «1»)",
              queue.activePosition(of: "b2") == 2,
              "got \(String(describing: queue.activePosition(of: "b2")))")
        check("позиция #1 в шапке = 1",
              queue.activePosition(of: "b1") == 1)

        // 3. Auto-surface path is untouched: no pick → first active book, as before.
        check("без выбора → первая активная книга (авто-всплытие на дроп)",
              queue.presentedBook(selectedID: nil)?.bookID == "b1")

        // 4. «Подтвердить все по очереди» starts at the first PENDING book even when
        //    a converting book sorts ahead of it in the showcase.
        let mixed = state([book("b0", "В работе", "converting"), first, second])
        check("«все по очереди» → первая ОЖИДАЮЩАЯ книга, не converting-первая",
              mixed.pendingConfirm.first?.bookID == "b1",
              "got \(mixed.pendingConfirm.first?.bookID ?? "nil")")
        check("первая активная в смешанной очереди — converting (контроль)",
              mixed.activeBooks.first?.bookID == "b0")

        // 5. A pick that stopped being active is ignored — the window follows the
        //    queue again instead of sticking to a finished/vanished book.
        let afterBuild = state([first, book("b2", "Тайна старого маяка", "done")])
        check("выбранная книга собралась (done) → возврат к первой активной",
              afterBuild.presentedBook(selectedID: "b2")?.bookID == "b1",
              "got \(afterBuild.presentedBook(selectedID: "b2")?.bookID ?? "nil")")
        check("done-книга не имеет позиции в «N из M»",
              afterBuild.activePosition(of: "b2") == nil)
        check("неизвестный id → первая активная (мусорный выбор не ломает окно)",
              queue.presentedBook(selectedID: "нет-такой")?.bookID == "b1")

        // 6. A picked book that went converting STAYS presented — the window mirrors
        //    the live build of the book the user actually confirmed (spec §3).
        let building = state([first, book("b2", "Тайна старого маяка", "converting")])
        check("выбранная книга в сборке → окно остаётся на ней (зеркалит сборку)",
              building.presentedBook(selectedID: "b2")?.bookID == "b2")
        check("позиция выбранной книги в сборке = 2",
              building.activePosition(of: "b2") == 2)

        // 7. Degenerate inputs never crash / never invent a book.
        check("пустая очередь → показывать нечего (nil, без падения)",
              ShowcaseState.empty.presentedBook(selectedID: "b2") == nil)
        let onlyDone = state([book("b9", "Готовая", "done")])
        check("только собранные книги → активных нет → nil",
              onlyDone.presentedBook(selectedID: nil) == nil)

        checkWindowGeometry()

        let passed = results.filter { $0.ok }.count
        let total = results.count
        print("\n§app-routing self-check: \(passed)/\(total) checks passed")
        let failed = results.filter { !$0.ok }.map { $0.name }
        if !failed.isEmpty { print("  FAILED checks: " + failed.joined(separator: "; ")) }
        exit(passed == total ? 0 : 1)
    }
}
