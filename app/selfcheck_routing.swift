// §app-routing self-check — app-level rules that must never regress:
//   1. WHICH BOOK the confirm window presents (ShowcaseState.presentedBook);
//   2. that the window can never end up OFF SCREEN (WindowGeometry);
//   3. (M5) WHICH SURFACE owns the window — the fail-closed install-truth gate,
//      the single-flight over the installer, and the tolerant decode of
//      `folder_access`. See `checkInstallTruth` and friends at the bottom.
//   4. that the window is actually SHOWN TO THE HUMAN — the escalation ladder in
//      WindowPresentation. Ordering a window is not showing it: on macOS 26
//      activation is cooperative and a programmatically launched app is refused,
//      so the window can exist, fully occluded, while the user sees nothing
//      (.patches/006 — measured at 85 seconds). See `checkWindowPresentation`.
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

        checkContentOverflow()
        checkRefitWiring()
    }

    /// STRUCTURAL guard for the one link the value-level checks provably cannot see.
    ///
    /// The rule «раскрыл блок → окно переизмерилось» spans two worlds: SwiftUI owns
    /// the content height, AppKit owns the window, and the only thing joining them is
    /// a notification. Deleting the post leaves every other check in this suite green
    /// — verified: NEG 8 removed the post and the suite still passed 199/199 — while
    /// the user gets the original bug back, text cut mid-word under an empty screen.
    ///
    /// Driving the real button headlessly was tried first and does not work: SwiftUI
    /// only publishes its accessibility tree once the view is in a window, and this
    /// suite must not open one. So the guard reads the sources instead. It is a
    /// coarse instrument and it is honest about that — it proves the wiring EXISTS,
    /// not that it fires. `#filePath` points at this file inside the repo, which is
    /// what the runner compiles, so the paths cannot drift.
    static func checkRefitWiring() {
        let appDir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        func source(_ name: String) -> String {
            (try? String(contentsOf: appDir.appendingPathComponent(name), encoding: .utf8)) ?? ""
        }
        let card = source("FolderAccessCard.swift")
        let host = source("main.swift")
        check("исходники карточки и хоста прочитаны",
              !card.isEmpty && !host.isEmpty)

        let signal = "mp3ContentHeightDidChange"
        check("раскрывашка FDA сообщает о смене высоты",
              card.contains(".onChange(of: expanded)") && card.contains("post(name: .\(signal)"))
        check("раскрывашка «Подробности» сообщает о смене высоты",
              host.contains(".onChange(of: expanded)") && host.contains("post(name: .\(signal)"))
        check("хост слушает сигнал и переизмеряет окно",
              host.contains("forName: .\(signal)") && host.contains("refitWindowHeight()"))
        check("переизмерение считает признак обрезания",
              host.contains("WindowGeometry.contentOverflows"))
        check("у прокручиваемой области карточки НЕТ второго потолка высоты",
              !source("StatusView.swift").contains("accessBlockerMaxHeight"))
    }

    // MARK: M6 — «прокрутка, о которой не знаешь, — это обрезанный текст»
    //
    // Замеренный баг: раскрывающийся FDA-блок добавляет карточке +354 pt (409 → 763),
    // а окно оставалось 560 pt — жёсткий потолок 460 pt связывал раньше экрана и
    // обрезал ровно 303 pt, оставляя столько же пустого места под окном. Потолок
    // теперь один (оконный, от экрана), и там же считается признак «упёрлись»: если
    // прокрутка реальна, её должно быть ВИДНО.

    static func checkContentOverflow() {
        check("содержимое влезло → подсказки о прокрутке нет",
              !WindowGeometry.contentOverflows(desired: 700, cap: 861))
        check("содержимое выше потолка → подсказка показана",
              WindowGeometry.contentOverflows(desired: 963, cap: 861))
        check("ровно по потолку → это не прокрутка",
              !WindowGeometry.contentOverflows(desired: 861, cap: 861))
        check("округление на пол-пункта не мигает подсказкой",
              !WindowGeometry.contentOverflows(desired: 861.4, cap: 861))

        // The measured card, against the screens it will actually meet. The point of
        // the fixture is that the SAME content is honest on both: no hint where it
        // fits, a hint where it does not.
        let cardOpen: CGFloat = 763        // measured: blocker + FDA block open
        let chrome: CGFloat = 163          // measured: header 72 + footer 57 + credit 34
        let ceiling: (CGFloat) -> CGFloat = { screen in screen - 8 - 28 }  // margin + titlebar
        check("экран 1050: раскрытая карточка влезает целиком, подсказки нет",
              !WindowGeometry.contentOverflows(desired: cardOpen + chrome, cap: ceiling(1050)))
        check("экран 897 (машина человека): не влезает → подсказка показана",
              WindowGeometry.contentOverflows(desired: cardOpen + chrome, cap: ceiling(897)))
        check("экран 700 (маленький): не влезает → подсказка показана",
              WindowGeometry.contentOverflows(desired: cardOpen + chrome, cap: ceiling(700)))
        check("закрытая карточка влезает даже на маленьком экране",
              !WindowGeometry.contentOverflows(desired: 409 + chrome, cap: ceiling(700)))

        // --- НЕГАТИВНЫЙ КОНТРОЛЬ ------------------------------------------------
        // Правило, которое тут было: жёсткий потолок 460 pt у прокручиваемой области.
        // Он связывал ВСЕГДА — и на экране, где всё влезало, и на маленьком, — то
        // есть окно не росло никогда. Фикстура обязана различать два правила.
        let oldHardCap: CGFloat = 460
        check("НЕГ.КОНТРОЛЬ: старый жёсткий потолок обрезал раскрытую карточку на 303 pt",
              cardOpen - oldHardCap == 303)
        check("НЕГ.КОНТРОЛЬ: старый потолок резал даже на экране, где всё влезает",
              WindowGeometry.contentOverflows(desired: cardOpen, cap: oldHardCap)
                  && !WindowGeometry.contentOverflows(desired: cardOpen + chrome,
                                                      cap: ceiling(1050)))
    }

    // MARK: «окно создано» ≠ «человек его видит» — лестница показа (.patches/006)
    //
    // Замеренный инцидент: агент нуджит приложение (`open -b`, rc=0), окно рождается
    // за 0.6 с — и 85 секунд стоит ПОЛНОСТЬЮ перекрытым чужим окном, пока человек не
    // кликнет сам. На macOS 26 активация кооперативная: программно поднятому процессу
    // WindowServer отказывает («activation count being 0 … window count = 1»), а
    // deprecated-флаг `ignoringOtherApps` — no-op. Лечится не «ещё одним вызовом», а
    // лестницей с самопроверкой на КАЖДОЙ ступени; правило лестницы вынесено чистой
    // функцией в app/WindowPresentation.swift ровно затем, чтобы его можно было
    // закрыть юнитом (сами вызовы AppKit юнитом не проверяются — см. checkRefitWiring
    // про честные границы такой проверки).
    //
    // Таблица здесь ИСЧЕРПЫВАЮЩАЯ (4 ступени × 4 комбинации фактов = 16), а не
    // выборочная: цена дырки в ней несимметрична, но плоха с обеих сторон — лишняя
    // эскалация даёт подскок дока у уже показанного окна, пропущенная возвращает
    // человеку пустой экран, с которого вся эпопея и началась.

    /// Порядок ступеней, заданный ЗДЕСЬ отдельным списком, а не выведенный из самого
    /// правила. Это принципиально: инварианты «не идёт назад» и «не перепрыгивает
    /// ступень» должны сверяться с независимой фикстурой — ранг, вычисленный из
    /// проверяемого кода, согласится с ним всегда, в том числе когда тот сломан.
    static let ladder: [PresentationStep] = [
        .ordered, .orderedRegardless, .attentionRequested, .done,
    ]

    static func rung(_ step: PresentationStep) -> Int {
        ladder.firstIndex(of: step) ?? -1
    }

    static func name(_ step: PresentationStep) -> String {
        switch step {
        case .ordered:            return "ordered"
        case .orderedRegardless:  return "orderedRegardless"
        case .attentionRequested: return "attentionRequested"
        case .done:               return "done"
        }
    }

    static func name(_ outcome: PresentationOutcome) -> String {
        switch outcome {
        case .satisfied:            return "satisfied"
        case .escalate(let step):   return "escalate(\(name(step)))"
        }
    }

    /// Все три комбинации, в которых хотя бы один признак показа истинен.
    static let shownCombos: [(isActive: Bool, isVisible: Bool)] = [
        (true, false), (false, true), (true, true),
    ]

    static func checkWindowPresentation() {
        checkPresentationMatrix()
        checkPresentationShortCircuit()
        checkPresentationMonotonic()
        checkPresentationTerminal()
        checkPresentationDelays()
        checkPresentationTermination()
        checkPresentationWiring()
    }

    /// 1. ПОЛНАЯ матрица решателя: ожидание задано явно для каждого из 16 случаев.
    static func checkPresentationMatrix() {
        let matrix: [(step: PresentationStep, isActive: Bool, isVisible: Bool,
                      expected: PresentationOutcome, why: String)] = [
            // .ordered — makeKeyAndOrderFront + activate() уже выполнены.
            (.ordered, false, false, .escalate(.orderedRegardless),
             "ФАКТ ИНЦИДЕНТА: не впереди и не видно → поднять поверх чужих окон"),
            (.ordered, false, true, .satisfied,
             "активации не дали, но окно видно — цель достигнута, эскалировать незачем"),
            (.ordered, true, false, .satisfied,
             "приложение впереди: occlusion мог не успеть пересчитаться, дёргать док рано"),
            (.ordered, true, true, .satisfied,
             "и впереди, и видно — показ состоялся"),

            // .orderedRegardless — окно подняли поверх чужих приложений.
            (.orderedRegardless, false, false, .escalate(.attentionRequested),
             "подняли поверх всех и всё равно не видно → дело не в порядке окон, зовём человека"),
            (.orderedRegardless, false, true, .satisfied,
             "ступень сработала: окно видно без активации — ровно то, ради чего она есть"),
            (.orderedRegardless, true, false, .satisfied,
             "приложение вышло вперёд — подскок дока был бы навязчивостью"),
            (.orderedRegardless, true, true, .satisfied,
             "видно и впереди"),

            // .attentionRequested — иконка в доке уже подскочила один раз.
            (.attentionRequested, false, false, .escalate(.done),
             "человека позвали и он не пришёл: звать второй раз — навязчивость, лестница исчерпана"),
            (.attentionRequested, false, true, .satisfied,
             "окно показалось после запроса внимания → гасим лестницу и снимаем запрос"),
            (.attentionRequested, true, false, .satisfied,
             "человек переключился на нас — дальше эскалировать нечего и незачем"),
            (.attentionRequested, true, true, .satisfied,
             "пришёл и видит"),

            // .done — лестница исчерпана или не начиналась.
            (.done, false, false, .escalate(.done),
             "терминал: ноль действий, ноль таймеров — поздний тик отменённого таймера безвреден"),
            (.done, false, true, .satisfied,
             "окно видно на терминале → satisfied, а не «мы уже сдались»"),
            (.done, true, false, .satisfied,
             "приложение впереди на терминале → satisfied"),
            (.done, true, true, .satisfied,
             "видно и впереди на терминале"),
        ]

        // Мета-проверка самой фикстуры: «исчерпывающая» — это утверждение о таблице,
        // и оно тоже должно ломаться, если строку удалят или задвоят.
        let keys = Set(matrix.map { "\(name($0.step))|\($0.isActive)|\($0.isVisible)" })
        check("матрица исчерпывающая: 4 ступени × 4 комбинации = 16 случаев, каждый ровно один раз",
              matrix.count == 16 && keys.count == 16,
              "строк \(matrix.count), уникальных \(keys.count)")

        for row in matrix {
            let got = WindowPresentation.next(after: row.step,
                                              isActive: row.isActive,
                                              isVisible: row.isVisible)
            check("[\(name(row.step)) · active=\(row.isActive) · visible=\(row.isVisible)] "
                  + "→ \(name(row.expected)) — \(row.why)",
                  got == row.expected, "got \(name(got))")
        }
    }

    /// 2. Короткое замыкание: любой признак показа гасит лестницу на ЛЮБОЙ ступени.
    ///
    /// Дизъюнкция, а не конъюнкция, и это не небрежность: `isVisible` — прямое
    /// доказательство цели, `isActive` нужен отдельно, потому что окно может стать
    /// активным раньше, чем система пересчитает occlusion.
    static func checkPresentationShortCircuit() {
        for step in ladder {
            let ok = shownCombos.allSatisfy {
                WindowPresentation.next(after: step,
                                        isActive: $0.isActive,
                                        isVisible: $0.isVisible) == .satisfied
            }
            check("короткое замыкание на ступени \(name(step)): isActive || isVisible → satisfied",
                  ok)
        }
        // Терминал назван отдельно: именно сюда прилетает поздний тик уже отменённого
        // таймера у окна, которое человек тем временем увидел, — и он обязан гасить
        // лестницу (снять запрос внимания), а не «доводить её до конца».
        check("короткое замыкание работает и на ТЕРМИНАЛЬНОЙ .done (поздний тик у видимого окна)",
              WindowPresentation.next(after: .done, isActive: false, isVisible: true) == .satisfied
                  && WindowPresentation.next(after: .done, isActive: true, isVisible: false) == .satisfied)
        let all = ladder.allSatisfy { step in
            shownCombos.allSatisfy {
                WindowPresentation.next(after: step,
                                        isActive: $0.isActive,
                                        isVisible: $0.isVisible) == .satisfied
            }
        }
        check("ни одна из 12 «показанных» комбинаций не эскалирует (0 лишних подскоков дока)",
              all)
    }

    /// 3. Монотонность: эскалация не идёт назад и не перепрыгивает ступень.
    ///
    /// Обе половины нужны порознь. «Назад» вернуло бы лестницу в цикл; «перепрыгнуть»
    /// — это подскок дока вместо тихого `orderFrontRegardless()`, то есть заметное
    /// человеку действие там, где хватило бы незаметного.
    static func checkPresentationMonotonic() {
        for step in ladder {
            let outcome = WindowPresentation.next(after: step, isActive: false, isVisible: false)
            guard case .escalate(let target) = outcome else {
                check("невидимое окно на ступени \(name(step)) → эскалация, а не satisfied",
                      false, "got \(name(outcome))")
                continue
            }
            check("монотонность: \(name(step)) → \(name(target)) не идёт НАЗАД",
                  rung(target) >= rung(step),
                  "ранги \(rung(step)) → \(rung(target))")
            check("монотонность: \(name(step)) → \(name(target)) не ПЕРЕПРЫГИВАЕТ ступень",
                  rung(target) - rung(step) <= 1,
                  "ранги \(rung(step)) → \(rung(target))")
        }
        // Ровно один шаг за раз для непустых ступеней; терминал остаётся на месте.
        let advances = ladder.map { step -> Int in
            guard case .escalate(let t) = WindowPresentation.next(after: step,
                                                                  isActive: false,
                                                                  isVisible: false)
            else { return -99 }
            return rung(t) - rung(step)
        }
        check("лестница проходится ровно по одной ступени за раз, терминал стоит на месте",
              advances == [1, 1, 1, 0], "шаги: \(advances)")
    }

    /// 4. Идемпотентный терминал: из `.done` некуда идти, и повтор безвреден.
    static func checkPresentationTerminal() {
        let terminal = WindowPresentation.next(after: .done, isActive: false, isVisible: false)
        check("терминал: из .done при невидимом окне → escalate(.done), а не откат на ступень",
              terminal == .escalate(.done), "got \(name(terminal))")

        var step = PresentationStep.done
        var stayed = true
        for _ in 0..<10 {
            let outcome = WindowPresentation.next(after: step, isActive: false, isVisible: false)
            guard case .escalate(let t) = outcome, t == .done else { stayed = false; break }
            step = t
        }
        check("терминал идемпотентен: 10 повторных входов не меняют исход и не поднимают ступень",
              stayed && step == .done)
        check("терминал не заводит таймер (ноль действий, ноль ретейнов без адресата)",
              WindowPresentation.recheckDelay(after: .done) == nil)
    }

    /// 5. `recheckDelay` — таймер ровно там, где есть куда подниматься.
    ///
    /// Значения сверяются С КОНСТАНТАМИ, а не с литералами 1.0/1.5: подстройка паузы
    /// — это настройка, а не регрессия, и тест не имеет права её ловить. Ловит он
    /// другое: НАЛИЧИЕ таймера, его конечность и порядок двух пауз.
    static func checkPresentationDelays() {
        check("задержка после .ordered = константа recheckAfterOrdered (не литерал)",
              WindowPresentation.recheckDelay(after: .ordered)
                  == WindowPresentation.recheckAfterOrdered,
              "\(String(describing: WindowPresentation.recheckDelay(after: .ordered)))")
        check("задержка после .orderedRegardless = константа recheckAfterOrderedRegardless",
              WindowPresentation.recheckDelay(after: .orderedRegardless)
                  == WindowPresentation.recheckAfterOrderedRegardless,
              "\(String(describing: WindowPresentation.recheckDelay(after: .orderedRegardless)))")
        check("nil РОВНО для .attentionRequested (выше подниматься нечем)",
              WindowPresentation.recheckDelay(after: .attentionRequested) == nil)
        check("nil РОВНО для .done (терминал)",
              WindowPresentation.recheckDelay(after: .done) == nil)

        let silent = ladder.filter { WindowPresentation.recheckDelay(after: $0) == nil }
        check("таймер не заводится РОВНО у двух ступеней и ровно у этих",
              silent == [.attentionRequested, .done],
              "без таймера: \(silent.map(name).joined(separator: ", "))")

        for step in [PresentationStep.ordered, .orderedRegardless] {
            let delay = WindowPresentation.recheckDelay(after: step)
            check("задержка после \(name(step)) — конечная положительная величина",
                  (delay ?? 0) > 0 && (delay ?? .infinity).isFinite,
                  "\(String(describing: delay))")
        }

        // Перекрёстный инвариант двух функций: таймер существует ТОГДА И ТОЛЬКО ТОГДА,
        // когда следующая ступень действительно что-то делает. Расхождение здесь —
        // это либо живой таймер без адресата, либо ступень, до которой никто не
        // доедет, и каждая из двух функций по отдельности выглядит корректной.
        for step in ladder {
            var leadsToAction = false
            if case .escalate(let t) = WindowPresentation.next(after: step,
                                                               isActive: false,
                                                               isVisible: false) {
                leadsToAction = (t != .done && t != step)
            }
            check("таймер заводится ⇔ есть куда подниматься (\(name(step)))",
                  (WindowPresentation.recheckDelay(after: step) != nil) == leadsToAction)
        }

        check("вторая пауза не короче первой (следующая ступень — единственная заметная человеку)",
              WindowPresentation.recheckAfterOrderedRegardless
                  >= WindowPresentation.recheckAfterOrdered,
              "\(WindowPresentation.recheckAfterOrdered) → "
                  + "\(WindowPresentation.recheckAfterOrderedRegardless)")
    }

    /// 6. Отсутствие цикла: невидимое окно доводит автомат до `.done` за конечное
    /// число шагов, и суммарное ожидание человека ограничено.
    static func checkPresentationTermination() {
        var step = PresentationStep.ordered
        var path: [PresentationStep] = []
        var armed: [TimeInterval] = []
        var iterations = 0
        let hardStop = 16          // сторож: цикл в правиле не должен вешать сьюту

        while iterations < hardStop {
            iterations += 1
            if let delay = WindowPresentation.recheckDelay(after: step) { armed.append(delay) }
            guard case .escalate(let target) = WindowPresentation.next(after: step,
                                                                      isActive: false,
                                                                      isVisible: false)
            else { break }         // satisfied — не наш вход, окно невидимо всю дорогу
            if target == step { break }   // неподвижная точка = терминал
            step = target
            path.append(target)
        }

        let trace = path.map(name).joined(separator: " → ")
        check("невидимое окно от .ordered: лестница ДОХОДИТ до .done",
              step == .done, "путь: \(trace)")
        check("…за конечное число шагов (≤ 4), сторож не сработал",
              path.count <= 4 && iterations < hardStop,
              "шагов \(path.count), итераций \(iterations)")
        check("…ни одна ступень не повторилась — цикла нет",
              Set(path.map(name)).count == path.count, "путь: \(trace)")
        check("…путь ровно тот, что задуман: orderedRegardless → attentionRequested → done",
              path == [.orderedRegardless, .attentionRequested, .done], "путь: \(trace)")
        check("на всём пути заведено ровно 2 таймера (по числу ступеней, с которых есть подъём)",
              armed.count == 2, "таймеров \(armed.count)")
        check("суммарное ожидание человека ограничено и равно сумме двух констант",
              armed.reduce(0, +) == WindowPresentation.recheckAfterOrdered
                  + WindowPresentation.recheckAfterOrderedRegardless
                  && armed.reduce(0, +) <= 5,
              "\(armed.reduce(0, +)) c")

        // --- НЕГАТИВНЫЙ КОНТРОЛЬ ------------------------------------------------
        // Правило, которое тут было до .patches/006: «вызвал makeKeyAndOrderFront +
        // activate и надеюсь». Оно рапортовало успех ровно в той ситуации, где человек
        // 85 секунд смотрел на чужое окно. Фикстура обязана различать два правила:
        // при факте инцидента НИ ОДНА ступень не имеет права сказать «показано».
        check("НЕГ.КОНТРОЛЬ: факт инцидента (не активны И окно перекрыто) не считается показом ни на одной ступени",
              ladder.allSatisfy {
                  WindowPresentation.next(after: $0, isActive: false, isVisible: false) != .satisfied
              })
        check("НЕГ.КОНТРОЛЬ: базовая ступень не самоуспокаивается — из .ordered есть куда идти",
              WindowPresentation.next(after: .ordered, isActive: false, isVisible: false)
                  == .escalate(.orderedRegardless))
    }

    // MARK: структурные помощники — «провод ВНУТРИ этой функции», а не «слово в файле»

    /// Исходник без строк-комментариев.
    ///
    /// Комментарий — не провод. В `main.swift` прозы больше, чем кода, и она цитирует
    /// ровно те вызовы, которые мы стережём: `orderFrontRegardless` встречается в файле
    /// ТРИЖДЫ, и два раза — в комментариях; `NSApp.unhide` живёт ТОЛЬКО в комментарии.
    /// Значит утверждение по всему файлу «вызов есть» удовлетворяется пересказом, а
    /// «вызова нет» ломается о цитату. Сначала выкидываем комментарии, потом утверждаем.
    ///
    /// Выкидываются только ЦЕЛИКОМ комментарные строки (`//` или `///` в начале):
    /// резать хвостовые `//` нельзя — они неотличимы от `//` внутри строкового литерала,
    /// а в файле есть URL'ы.
    static func strippedCode(_ source: String) -> String {
        source.split(separator: "\n", omittingEmptySubsequences: false)
            .map(String.init)
            .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("//") }
            .joined(separator: "\n")
    }

    /// Тело функции `decl` из УЖЕ очищенного исходника: от строки объявления до первой
    /// строки, состоящей ровно из `}` на том же отступе.
    ///
    /// Это не парсер Swift и не претендует им быть: один файл, одно форматирование,
    /// закрывающая скобка метода — на отступе метода. Промах якоря отдаётся ПУСТЫМ
    /// телом, то есть в красное для положительных утверждений. Отрицательные («в теле
    /// НЕТ Х») от пустого тела, наоборот, зазеленели бы — поэтому каждое из них ниже
    /// склеено с положительным фактом из того же тела, а «все тела найдены» проверяется
    /// отдельной строкой.
    static func bodyOfFunction(_ decl: String, in code: String) -> String {
        let lines = code.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        guard let start = lines.firstIndex(where: { $0.contains(decl) }) else { return "" }
        let close = String(lines[start].prefix(while: { $0 == " " })) + "}"
        guard let end = lines[(start + 1)...].firstIndex(of: close) else { return "" }
        return lines[start...end].joined(separator: "\n")
    }

    /// Порядок двух вызовов внутри одного тела. Fail-closed: нет любого из них — false.
    ///
    /// ⚠️ Сравниваются ПЕРВЫЕ вхождения, поэтому по телу с несколькими однотипными
    /// блоками этим спрашивать нельзя — сначала вырежи блок через `slice`.
    static func precedes(_ first: String, _ second: String, in body: String) -> Bool {
        guard let a = body.range(of: first), let b = body.range(of: second) else { return false }
        return a.lowerBound < b.lowerBound
    }

    /// Кусок тела от маркера до следующего `stop` (или до конца, если его нет).
    ///
    /// Нужен, чтобы «в обработчике X стоит вызов Y» не удовлетворялось вызовом Y в
    /// СОСЕДНЕМ обработчике. Это не теория: в `installFocusObservers` четыре
    /// регистрации, `endPresentationEscalation()` стоит в двух из них, и поиск по
    /// всему телу их не различает — первая же черновая редакция этих проверок на том
    /// и споткнулась (покраснела на исправном коде). Границей служит следующая
    /// регистрация — по коду, не по имени локальной переменной.
    static func slice(from marker: String, upTo stop: String, in body: String) -> String {
        guard let start = body.range(of: marker) else { return "" }
        let tail = body[start.lowerBound...]
        guard let end = tail.range(of: stop) else { return String(tail) }
        return String(tail[..<end.lowerBound])
    }

    static func occurrences(of needle: String, in text: String) -> Int {
        needle.isEmpty ? 0 : text.components(separatedBy: needle).count - 1
    }

    /// STRUCTURAL guard, тот же приём и те же честные границы, что в checkRefitWiring:
    /// чистое правило может быть безупречным, а хост — не спрашивать его или кормить
    /// не теми фактами, и все проверки выше останутся зелёными. Проверить это
    /// значением нельзя: `NSApp.isActive` и `occlusionState` существуют только внутри
    /// живого приложения с окном, а сьюта окон не открывает. Поэтому — чтение
    /// исходника: инструмент грубый, и он доказывает, что связь ЕСТЬ, а не что она
    /// срабатывает. Ровно этот разрыв («проверялся факт вызова, а не факт эффекта»)
    /// и дал инцидент .patches/006.
    ///
    /// Утверждения по ТЕЛАМ функций, а не по файлу. Разница не косметическая: «слово
    /// встречается в main.swift» зеленеет от вызова на мёртвой ветке и от упоминания в
    /// комментарии, то есть ровно тот тихий зелёный, ради которого весь этот файл и
    /// написан. Провод считается на месте, только если он стоит в той функции, которая
    /// его обязана дёргать, и — где порядок несущий — в правильном месте её тела.
    static func checkPresentationWiring() {
        let appDir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        let raw = (try? String(contentsOf: appDir.appendingPathComponent("main.swift"),
                               encoding: .utf8)) ?? ""
        check("исходник хоста прочитан", !raw.isEmpty)
        let host = strippedCode(raw)

        check("самопроверка кормит решатель ДВУМЯ фактами реальности, а не догадкой",
              host.contains("WindowPresentation.next(")
                  && host.contains("NSApp.isActive")
                  && host.contains("occlusionState.contains(.visible)"))
        check("таймер перепроверки заводится по правилу, а не по литералу в хосте",
              host.contains("WindowPresentation.recheckDelay(after:"))
        check("ступень orderedRegardless выполняет orderFrontRegardless() — подъём без активации",
              host.contains("window.orderFrontRegardless()"))
        check("ступень attentionRequested — РАЗОВЫЙ informationalRequest",
              host.contains("requestUserAttention(.informationalRequest)"))
        check("подскок дока никогда не .criticalRequest (он бился бы до реакции человека)",
              !host.contains("requestUserAttention(.criticalRequest)"))

        // --- тела-адресаты ------------------------------------------------------
        // Промах якоря обязан быть КРАСНЫМ, а не «нечего проверять»: без этой строки
        // одно переименование функции превратило бы каждое отрицательное утверждение
        // ниже в зелёное по пустому телу.
        let bodies = [
            "applicationDidFinishLaunching": bodyOfFunction("func applicationDidFinishLaunching",
                                                            in: host),
            "presentWindow": bodyOfFunction("func presentWindow()", in: host),
            "installFocusObservers": bodyOfFunction("func installFocusObservers()", in: host),
            "checkWindowVisibility": bodyOfFunction("func checkWindowVisibility()", in: host),
            "performPresentationStep": bodyOfFunction("func performPresentationStep(", in: host),
            "withdrawAttentionRequest": bodyOfFunction("func withdrawAttentionRequest()", in: host),
            "endPresentationEscalation": bodyOfFunction("func endPresentationEscalation()",
                                                        in: host),
            "applicationWillTerminate": bodyOfFunction("func applicationWillTerminate", in: host),
            "applicationShouldHandleReopen": bodyOfFunction("func applicationShouldHandleReopen",
                                                            in: host),
        ]
        let lost = bodies.filter { $0.value.isEmpty }.keys.sorted()
        check("все 9 тел-адресатов найдены в хосте (промах якоря = красное, не «нечего проверять»)",
              lost.isEmpty, lost.isEmpty ? "" : "не найдены: \(lost.joined(separator: ", "))")
        func body(_ name: String) -> String { bodies[name] ?? "" }

        // --- ЛЕСТНИЦА — ЕДИНСТВЕННЫЙ СПОСОБ ПОДНЯТЬ ОКНО ------------------------
        // Инцидент .patches/006 начался с «подниму окно вот здесь руками»: вызов
        // сработал, окно осталось перекрытым, и никто этого не заметил, потому что
        // самопроверки на том пути не было. Считаем ВСЕ подъёмы в хосте и требуем,
        // чтобы каждый стоял внутри лестницы — presentWindow (ступень 4) или
        // performPresentationStep (ступени .ordered / .orderedRegardless).
        //
        // Образцы БЕЗ получателя (`makeKeyAndOrderFront(`, а не `window.makeKeyAnd…`):
        // получателей у окна много — `window.`, `window?.`, `self.window!.`, локальная
        // переменная, — и сторож, привязанный к одному написанию, обходится вторым.
        // Это не гипотеза: первая редакция ловила только `window.`, и мутант с
        // `window?.makeKeyAndOrderFront(nil)` в applicationWillTerminate прошёл её
        // насквозь, 317/317 зелёных.
        //
        // `mustExist` = ступень лестницы, которая обязана существовать; false — API,
        // которого просто не должно быть в обход (0 вхождений — законный ответ).
        let raises: [(call: String, mustExist: Bool)] = [
            ("makeKeyAndOrderFront(", true),   // шаг 4 presentWindow + ступень .ordered
            ("orderFrontRegardless(", true),   // ступень .orderedRegardless
            (".activate(", true),              // шаг 5 presentWindow (через activateApp)
            ("orderFront(", false),            // ещё один способ поднять окно мимо лестницы
            ("deminiaturize(", false),         // и ещё один: развернуть из дока
        ]
        for (call, mustExist) in raises {
            let inFile = occurrences(of: call, in: host)
            let inLadder = occurrences(of: call, in: body("presentWindow"))
                + occurrences(of: call, in: body("performPresentationStep"))
                + occurrences(of: call, in: bodyOfFunction("func activateApp()", in: host))
            check("подъём окна `\(call)` живёт ТОЛЬКО внутри лестницы, мимо неё окно не поднимают",
                  inFile == inLadder && (!mustExist || inFile > 0),
                  "в файле \(inFile), в лестнице \(inLadder)")
        }

        // --- НАБЛЮДАТЕЛЬ ОККЛЮЗИИ ------------------------------------------------
        // Единственное ПОЛОЖИТЕЛЬНОЕ доказательство, что человек видит окно (приложению
        // для этого не обязательно быть активным). Потеряется он — лестница не узнает
        // об успехе, догонит человека подскоком дока по уже открытому окну, и ни одна
        // проверка выше не покраснеет: решатель-то останется безупречным.
        // Обработчики режутся поштучно: утверждение «гасит лестницу» обязано попадать
        // в СВОЙ блок, а не собирать вызов из соседнего.
        let occlusionHandler = slice(from: "forName: NSWindow.didChangeOcclusionStateNotification",
                                     upTo: "nc.addObserver(", in: body("installFocusObservers"))
        let activeHandler = slice(from: "forName: NSApplication.didBecomeActiveNotification",
                                  upTo: "nc.addObserver(", in: body("installFocusObservers"))
        check("наблюдатель окклюзии зарегистрирован ВНУТРИ installFocusObservers",
              !occlusionHandler.isEmpty)
        check("окклюзия гасит лестницу по ПОЛОЖИТЕЛЬНОМУ признаку (стало видно), а не на любое событие",
              precedes("occlusionState.contains(.visible)", "endPresentationEscalation()",
                       in: occlusionHandler))
        check("человек посмотрел (приложение стало активным) → лестница гаснет",
              activeHandler.contains("endPresentationEscalation()"))
        let registered = occurrences(of: "nc.addObserver(", in: body("installFocusObservers"))
        let retained = body("installFocusObservers")
            .components(separatedBy: "focusObservers = [").last?
            .components(separatedBy: "]").first?
            .components(separatedBy: ",").count ?? 0
        check("сколько наблюдателей завели, столько и удерживаем (иначе снять на выходе нечем)",
              registered > 0 && registered == retained,
              "заведено \(registered), удержано \(retained)")

        // --- ПОРЯДОК УСТАНОВКИ (несущий) ----------------------------------------
        // Уведомление, посланное ДО установки наблюдателя, не доставляется никому.
        // Переставь эти две строки местами — и переход, ради которого лестница
        // существует (запуск), пройдёт мимо неё; заметить это сможет только поллинг
        // через 1.0 с. Компилятор такой перестановки не видит, тесты значений — тоже.
        check("наблюдатели ставятся СТРОГО ДО первого показа окна (уведомление без слушателя — в никуда)",
              precedes("installFocusObservers()", "presentWindow()",
                       in: body("applicationDidFinishLaunching")))

        // --- ВХОДЫ В ЛЕСТНИЦУ ----------------------------------------------------
        // Зеркало проверки выше. Та доказывает, что МИМО лестницы окно не поднимают;
        // эта — что в неё заходят. Без неё выпавший `presentWindow()` из фронта «пришла
        // новая книга» оставил бы сьюту полностью зелёной, а человека — опять перед
        // невидимым окном: подниматься мимо лестницы никто не начал, просто показывать
        // перестали совсем. Проверяется факт вызова на каждом пути показа; правильность
        // САМОГО УСЛОВИЯ фронта — не здесь, она значениями в сьютах queue/nudge.
        let entries = ["applicationDidFinishLaunching",   // запуск
                       "applicationShouldHandleReopen",   // пинок агента в живое приложение
                       "refreshNow",                      // фронт: появилась новая книга
                       "handleInstalled",                 // передача после установки
                       "requestConsentNotice"]            // карточка согласия
        let silent = entries.filter {
            !bodyOfFunction("func \($0)", in: host).contains("presentWindow()")
        }
        check("ВСЕ \(entries.count) путей показа заходят в лестницу через presentWindow()",
              silent.isEmpty, silent.isEmpty ? "" : "показывают мимо неё: \(silent.joined(separator: ", "))")

        // Агент пинает `open -b`; приложение уже запущено, значит LaunchServices
        // превращает пинок в reopen. Без этого делегата пинок получает дефолтную
        // обработку AppKit — на Tahoe это ровно то же молчание, что и в инциденте.
        check("пинок в уже живое приложение (reopen) ведёт в ту же лестницу",
              body("applicationShouldHandleReopen").contains("presentWindow()"))
        check("…и не поднимает окно мимо неё своими руками",
              body("applicationShouldHandleReopen").contains("presentWindow()")
                  && !body("applicationShouldHandleReopen").contains("makeKeyAndOrderFront")
                  && !body("applicationShouldHandleReopen").contains("orderFrontRegardless"))

        // --- РАЗ-ПРЯТЫВАНИЕ ------------------------------------------------------
        // Окна скрытого приложения (Cmd+H, «Скрыть остальные») лежат в слое, которого
        // никто не видит: без этого шага КАЖДАЯ ступень лестницы отработает вхолостую,
        // а самопроверка честно доложит «не видно» и дойдёт до подскока дока — с
        // окном, которое так и осталось скрытым.
        check("presentWindow снимает скрытость приложения перед подъёмом",
              body("presentWindow").contains("NSApp.isHidden")
                  && body("presentWindow").contains("NSApp.unhideWithoutActivation()"))
        check("раз-прятывание БЕЗ активации: plain unhide протащил бы отказной запрос активации",
              body("presentWindow").contains("NSApp.unhideWithoutActivation()")
                  && !body("presentWindow").contains("NSApp.unhide("))

        // --- МОНОТОННОСТЬ ПОВТОРНОГО ВХОДА ---------------------------------------
        // Одна упавшая книга даёт минимум два показа (reopen от агента + фронт часов
        // через 0.15–0.5 с). Решатель монотонен — это доказано матрицей выше, — но
        // хост может отменить его свойство сбоку: перезапустить прогон с базовой
        // ступени, потеряв уже достигнутый подъём. Зелёный тест над свойством, которого
        // у работающей системы нет, — тот самый класс, ради которого всё это писалось.
        check("повторный вход ПОВТОРЯЕТ текущую ступень, а не сбрасывает лестницу вниз",
              body("presentWindow").contains("if presentationCheck != nil")
                  && body("presentWindow").contains("performPresentationStep(presentationStep"))
        check("сброс на базовую ступень в presentWindow РОВНО ОДИН (в ветке нового прогона)",
              occurrences(of: "enterPresentationStep(", in: body("presentWindow")) == 1
                  && body("presentWindow").contains("enterPresentationStep(.ordered)"))
        check("новый прогон отзывает унаследованный подскок ДО подъёма (иначе своего он не получит)",
              precedes("withdrawAttentionRequest()", "enterPresentationStep(.ordered)",
                       in: body("presentWindow")))

        // --- ЗАКРЫТОЕ ОКНО ОСТАНАВЛИВАЕТ ЛЕСТНИЦУ --------------------------------
        // Человек закрыл окно, пока проверка была в воздухе. occlusionState не отличает
        // закрытое окно от перекрытого — оба «не видно», — поэтому решателю такой случай
        // показывать НЕЛЬЗЯ: он честно эскалирует, и orderFrontRegardless вернёт на
        // экран окно, которое человек только что убрал. Отсюда и порядок: guard раньше
        // решателя, иначе он бесполезен.
        check("закрытое окно останавливает лестницу насмерть (isVisible, не occlusion)",
              body("checkWindowVisibility").contains("guard window.isVisible else")
                  && body("checkWindowVisibility").contains("presentationStep = .done"))
        check("…и этот guard стоит СТРОГО ДО решателя (после — окно вернулось бы на экран)",
              precedes("guard window.isVisible else", "WindowPresentation.next(",
                       in: body("checkWindowVisibility")))

        // --- СНЯТИЕ ЗАПРОСА ВНИМАНИЯ ---------------------------------------------
        // Было: `host.contains("cancelUserAttentionRequest")` — слово где-то в файле.
        // Стало: цепочка целиком, каждое звено отдельно. Снятие живёт в одном месте, к
        // нему ведут ровно два пути, и путь выхода из приложения — один из них.
        check("снятие запроса внимания живёт РОВНО В ОДНОМ месте — withdrawAttentionRequest",
              occurrences(of: "NSApp.cancelUserAttentionRequest(", in: host) == 1
                  && body("withdrawAttentionRequest").contains("NSApp.cancelUserAttentionRequest("))
        check("…и идемпотентно: guard на сохранённый id + обнуление, двойного снятия быть не может",
              precedes("guard let request = presentationAttentionRequest else { return }",
                       "NSApp.cancelUserAttentionRequest(", in: body("withdrawAttentionRequest"))
                  && body("withdrawAttentionRequest").contains("presentationAttentionRequest = nil"))
        check("человек посмотрел → лестница гаснет И снимает невыполненный запрос внимания",
              body("endPresentationEscalation").contains("withdrawAttentionRequest()")
                  && body("endPresentationEscalation").contains("presentationCheck?.cancel()"))
        check("ВЫХОД из приложения ведёт в то же снятие (terminate → лестница → отзыв), а не мимо",
              body("applicationWillTerminate").contains("endPresentationEscalation()")
                  && body("endPresentationEscalation").contains("withdrawAttentionRequest()")
                  && body("withdrawAttentionRequest").contains("NSApp.cancelUserAttentionRequest("))
        check("…и гасит лестницу ДО снятия наблюдателей (иначе работа останется в очереди)",
              precedes("endPresentationEscalation()", "removeObserver",
                       in: body("applicationWillTerminate")))
    }

    // MARK: M5 — the fail-closed install-truth gate (plan v2 B3 / §6.3)

    /// A truth fixture with everything HEALTHY; each check mutates one field, so a
    /// failure names exactly which condition stopped mattering.
    static func healthyTruth() -> InstallTruth {
        InstallTruth(hasInstall: true, pa0IsHelper: true,
                     receiptGeneration: "GEN-1", stateGeneration: "GEN-1",
                     folderAccess: .denied, updateOccupiesWindow: false,
                     secondsSinceInstallSettled: 3600)
    }

    /// THE invariant: the access surface may appear ONLY when the job on disk is
    /// provably the job we installed AND the running agent proves it belongs to it.
    static func checkAccessGate() {
        var t = healthyTruth()
        check("КАРТОЧКА ДОСТУПА: всё сходится (PA0=helper, поколения равны) → показываем",
              t.allowsFolderAccessSurface && t.surface == .folderAccess(.denied),
              "\(t.surface)")

        // --- each half of the gate, removed one at a time -----------------------
        t = healthyTruth(); t.pa0IsHelper = false
        check("PA0 ≠ helper → карточка ЗАПРЕЩЕНА, вместо неё «агент не запустился»",
              !t.allowsFolderAccessSurface && t.surface == .agentNotRunning(.pa0Mismatch),
              "\(t.surface)")

        t = healthyTruth(); t.stateGeneration = "GEN-0"
        check("поколение агента ≠ поколению чека → карточка ЗАПРЕЩЕНА (работает старый job)",
              !t.allowsFolderAccessSurface
                  && t.surface == .agentNotRunning(.generationMismatch),
              "\(t.surface)")

        t = healthyTruth(); t.stateGeneration = nil
        check("агент не сообщил поколение (после грейса) → карточка ЗАПРЕЩЕНА",
              !t.allowsFolderAccessSurface
                  && t.surface == .agentNotRunning(.generationMissing),
              "\(t.surface)")

        t = healthyTruth(); t.receiptGeneration = nil
        check("нет чека установки → карточка ЗАПРЕЩЕНА (установка не дошла до конца)",
              !t.allowsFolderAccessSurface
                  && t.surface == .agentNotRunning(.receiptMissing),
              "\(t.surface)")

        t = healthyTruth(); t.updateOccupiesWindow = true
        check("идёт/упала установка → окно за ней, карточка ЗАПРЕЩЕНА",
              !t.allowsFolderAccessSurface && t.surface == .agentRepair, "\(t.surface)")

        // --- fail-closed on «не смогли прочитать» -------------------------------
        // pa0IsHelper=false is ALSO what an unreadable plist produces: unknown must
        // behave exactly like wrong, never like fine.
        t = healthyTruth(); t.pa0IsHelper = false; t.folderAccess = .blocked
        check("PA0 не прочитан (=false) при blocked → всё равно «агент не запустился»",
              t.surface == .agentNotRunning(.pa0Mismatch), "\(t.surface)")

        // --- grace window: a just-installed agent is not accused of being dead ---
        t = healthyTruth(); t.stateGeneration = nil; t.secondsSinceInstallSettled = 3
        check("поколение ещё не приехало, прошло 3 с → молчим (грейс 15 с), не пугаем",
              t.surface == .normal, "\(t.surface)")
        t.secondsSinceInstallSettled = InstallTruth.generationGrace + 1
        check("то же самое после грейса → честное «агент не запустился»",
              t.surface == .agentNotRunning(.generationMissing), "\(t.surface)")

        // --- nothing installed → Setup owns the window, we claim nothing ---------
        t = healthyTruth(); t.hasInstall = false; t.pa0IsHelper = false
        check("ничего не установлено → normal (экран Setup), без ложных диагнозов",
              t.surface == .normal, "\(t.surface)")

        // --- which access values actually surface --------------------------------
        for value in [FolderAccess.denied, .blocked, .missing] {
            t = healthyTruth(); t.folderAccess = value
            check("folder_access=\(value.rawValue) → поверхность доступа",
                  t.surface == .folderAccess(value), "\(t.surface)")
        }
        t = healthyTruth(); t.folderAccess = .ok
        check("folder_access=ok → обычный экран", t.surface == .normal, "\(t.surface)")
        t = healthyTruth(); t.folderAccess = nil
        check("агент ещё не сообщал доступ (nil) → обычный экран", t.surface == .normal)

        // --- НЕГАТИВНЫЙ КОНТРОЛЬ ------------------------------------------------
        // The rule this milestone replaces: «показываем карточку, если
        // folder_access == denied» — plist on disk считался достаточным. Фикстура
        // ниже проходит по наивному правилу и обязана НЕ проходить по нашему.
        // Если кто-то вернёт наивное правило, это расхождение исчезнет и чек
        // покраснеет — то есть фикстура доказуемо различает две реализации.
        let naiveWouldShow: (InstallTruth) -> Bool = { $0.folderAccess == .denied }
        let killer = InstallTruth(hasInstall: true, pa0IsHelper: true,
                                  receiptGeneration: "GEN-2", stateGeneration: "GEN-1",
                                  folderAccess: .denied,
                                  secondsSinceInstallSettled: 3600)
        check("НЕГ.КОНТРОЛЬ: наивное правило показало бы карточку — наше запрещает",
              naiveWouldShow(killer) && !killer.allowsFolderAccessSurface)
        let killer2 = InstallTruth(hasInstall: true, pa0IsHelper: false,
                                   receiptGeneration: "GEN-1", stateGeneration: "GEN-1",
                                   folderAccess: .denied,
                                   secondsSinceInstallSettled: 3600)
        check("НЕГ.КОНТРОЛЬ: правильный plist ≠ загруженный job — карточка запрещена",
              naiveWouldShow(killer2) && !killer2.allowsFolderAccessSurface)
    }

    // MARK: M6 — the access card: which state, which words, which buttons
    //
    // The expensive mistake this section exists to prevent is NOT a crash, it is a
    // plausible sentence. `denied` and `blocked` look like the same problem («папка
    // не читается») and are opposite ones: in `blocked` there is no decision and the
    // fix is to press «Разрешить» in a dialog that is on screen RIGHT NOW; in
    // `denied` the decision exists, is "no", and that dialog will never appear
    // again. Merging their copy sends half the users to wait forever and the other
    // half to miss the dialog they were being shown (addendum §4.1).

    /// Every automat state, so a new case cannot be added without copy.
    static let allCardStates: [FolderAccessCardState] = [
        .problem(.denied), .problem(.blocked), .problem(.missing),
        .problem(.unknown("quarantined")),
        .checking(.denied), .checking(.blocked),
        .stillDenied(.denied), .stillDenied(.blocked), .stillDenied(.missing),
        .busy(.denied), .busy(.blocked),
        .timeout(.denied), .timeout(.blocked),
    ]

    static func checkFolderAccessCard() {
        // --- 1. THE invariant: no access card outside the access surfaces --------
        check("КАРТОЧКА: .folderAccess(denied) → карточка denied",
              FolderAccessCardState.forSurface(.folderAccess(.denied)) == .problem(.denied))
        check("КАРТОЧКА: .accessUnknown(raw) тоже получает карточку (а не тишину)",
              FolderAccessCardState.forSurface(.accessUnknown("quarantined"))
                  == .problem(.unknown("quarantined")))
        for surface in [StatusSurface.normal, .agentRepair,
                        .agentNotRunning(.pa0Mismatch), .agentNotRunning(.generationMismatch)] {
            check("КАРТОЧКА ЗАПРЕЩЕНА на поверхности \(surface)",
                  FolderAccessCardState.forSurface(surface) == nil)
        }
        // A pin only survives over the SAME problem: a `blocked` memory sitting on a
        // live `denied` would keep offering «Показать запрос ещё раз» for a dialog
        // macOS has already decided never to show again.
        check("КАРТОЧКА: закреплённое состояние живёт, пока проблема та же",
              FolderAccessCardState.forSurface(.folderAccess(.denied),
                                               pinned: .stillDenied(.denied))
                  == .stillDenied(.denied))
        check("КАРТОЧКА: закреплённое состояние ЧУЖОЙ проблемы отбрасывается",
              FolderAccessCardState.forSurface(.folderAccess(.denied),
                                               pinned: .busy(.blocked))
                  == .problem(.denied))

        // --- 2. every state has words, and they are its own ---------------------
        var seenTitles: Set<String> = []
        for state in allCardStates {
            let title = FolderAccessCopy.title(state)
            let body = FolderAccessCopy.body(state)
            check("тексты есть у состояния \(state)",
                  !title.isEmpty && !body.isEmpty && body.count > title.count)
            seenTitles.insert(title)
        }
        check("состояния автомата не схлопываются в один заголовок",
              seenTitles.count >= 7, "уникальных заголовков: \(seenTitles.count)")

        // --- 3. denied ≠ blocked, in every load-bearing place --------------------
        let denied = FolderAccessCardState.denied
        let blocked = FolderAccessCardState.awaitingConsent
        check("denied и blocked: РАЗНЫЕ заголовки",
              FolderAccessCopy.title(denied) != FolderAccessCopy.title(blocked),
              FolderAccessCopy.title(denied))
        check("denied и blocked: РАЗНЫЕ тексты",
              FolderAccessCopy.body(denied) != FolderAccessCopy.body(blocked))
        check("denied и blocked: РАЗНЫЕ первые кнопки",
              FolderAccessCopy.actions(denied).primary
                  != FolderAccessCopy.actions(blocked).primary)
        check("denied и blocked: РАЗНЫЕ подписи в баннере",
              FolderAccessCopy.bannerSub(denied) != FolderAccessCopy.bannerSub(blocked))

        // The words that must / must not be there — this is what makes the two texts
        // different in SUBSTANCE and not merely in wording.
        check("blocked зовёт нажать «Разрешить» (окно системы сейчас на экране)",
              FolderAccessCopy.body(blocked).contains("«Разрешить»"),
              FolderAccessCopy.body(blocked))
        check("blocked цитирует диалог по имени файла-агента (docs/consent-dialog.png)",
              FolderAccessCopy.body(blocked).contains(StateStore.helperName)
                  && FolderAccessCopy.body(blocked).contains("запрашивает доступ к файлам"))
        check("denied НЕ зовёт ждать окно, которого больше не будет",
              !FolderAccessCopy.body(denied).contains("«Разрешить»"),
              FolderAccessCopy.body(denied))
        check("denied честно говорит, что отказ запомнен",
              FolderAccessCopy.body(denied).contains("«Не разрешать»")
                  && FolderAccessCopy.body(denied).contains("не будет"))
        check("blocked подсказывает, что делать, если окна не видно",
              (FolderAccessCopy.hint(blocked) ?? "").contains("Не видите окно"))

        // --- 4. denied leads with the folder move; FDA is folded away -----------
        check("denied: ПЕРВАЯ кнопка — папка вне защищённой зоны (addendum §5.3)",
              FolderAccessCopy.actions(denied).primary == .moveOutOfProtectedZone)
        check("blocked: ПЕРВАЯ кнопка — показать запрос ещё раз",
              FolderAccessCopy.actions(blocked).primary == .showRequestAgain)
        check("«Показать запрос ещё раз» не предлагается там, где запроса не будет",
              FolderAccessCopy.actions(denied).primary != .showRequestAgain
                  && FolderAccessCopy.actions(denied).secondary != .showRequestAgain)
        for state in allCardStates {
            let a = FolderAccessCopy.actions(state)
            check("FDA-инструкция не вылезает в кнопки состояния \(state)",
                  a.primary != .openPrivacyPane && a.primary != .copyHelperPath
                      && a.secondary != .openPrivacyPane && a.secondary != .copyHelperPath)
        }
        check("denied: инструкция про Системные настройки — в раскрывающемся блоке",
              FolderAccessCopy.disclosureTitle(denied)?.contains("Системных настройках") == true)
        check("blocked: раскрывающийся блок — на случай «запрос не появился»",
              FolderAccessCopy.disclosureTitle(blocked)?.contains("не появился") == true)
        check("нет доступа ≠ нет папки: у missing FDA-блока нет вовсе",
              FolderAccessCopy.disclosureTitle(.problem(.missing)) == nil)
        check("шаги «добавить вручную» называют именно файл-агент, а не приложение",
              FolderAccessCopy.routeAddSteps.contains { $0.contains(StateStore.helperName) })
        check("FDA-блок предупреждает, что панель врёт про свежие записи (урок донора 020)",
              FolderAccessCopy.fdaCaveat.contains("не показывает свежие записи"))

        // --- 5. the phases: inert actions, honest recaps -------------------------
        check("во время проверки кнопки заморожены", FolderAccessCardState.checking(.denied).actionsAreInert)
        check("во время сборки кнопки заморожены (переезд в сборку запрещён, §3.2 п.5)",
              FolderAccessCardState.busy(.denied).actionsAreInert)
        check("в состоянии покоя кнопки живые", !denied.actionsAreInert && !blocked.actionsAreInert)
        check("после таймаута кнопки живые (иначе выхода нет)",
              !FolderAccessCardState.timeout(.denied).actionsAreInert)
        check("busy напоминает, о какой проблеме речь",
              (FolderAccessCopy.hint(.busy(.denied)) ?? "").contains(FolderAccessCopy.shortProblem(.denied)))
        check("busy отличает «занят» от «не ответил»",
              FolderAccessCopy.title(.busy(.denied)) != FolderAccessCopy.title(.timeout(.denied)))
        check("busy обещает проверку после сборки, а не ошибку",
              FolderAccessCopy.body(.busy(.denied)).contains("после текущей книги"))
        check("таймаут ведёт в Настройки к агенту, а не в переезд папки",
              FolderAccessCopy.actions(.timeout(.denied)).primary == .recheck
                  && FolderAccessCopy.actions(.timeout(.denied)).secondary == .openAppSettings)
        check("закрепляются только исходы проверки",
              FolderAccessCardState.stillDenied(.denied).isPinned
                  && FolderAccessCardState.busy(.denied).isPinned
                  && FolderAccessCardState.timeout(.denied).isPinned
                  && !denied.isPinned && !FolderAccessCardState.checking(.denied).isPinned)

        // --- 6. НЕГАТИВНЫЙ КОНТРОЛЬ: «одна карточка на всё» --------------------
        // The implementation this milestone replaces (and the neighbour's shipped
        // one) has ONE text for "нет доступа". The fixture below passes under that
        // rule and must fail under ours — so the check provably distinguishes the
        // two, instead of asserting something both would satisfy.
        let naiveTitle: (FolderAccessCardState) -> String = { _ in "Нет доступа к папке" }
        let naivePrimary: (FolderAccessCardState) -> FolderAccessAction = { _ in .openPrivacyPane }
        check("НЕГ.КОНТРОЛЬ: слитая карточка дала бы denied и blocked один заголовок",
              naiveTitle(denied) == naiveTitle(blocked)
                  && FolderAccessCopy.title(denied) != FolderAccessCopy.title(blocked))
        check("НЕГ.КОНТРОЛЬ: слитая карточка вела бы обоих в Системные настройки",
              naivePrimary(denied) == naivePrimary(blocked)
                  && FolderAccessCopy.actions(denied).primary
                      != FolderAccessCopy.actions(blocked).primary)
    }

    // MARK: M6 — куда ведёт ручной фолбэк и как кнопки ОТВЕЧАЮТ на нажатие
    //
    // Два боевых бага, оба у человека на рабочей установке:
    //   1. ссылка вела в «Полный доступ к диску», а наш грант живёт в «Файлах и
    //      папках» — человек искал `mp3-to-m4b-agent` в списке, где его быть не
    //      может, и решил, что приложение врёт;
    //   2. «Скопировать путь агента» копировала исправно и молчала — а молчащая
    //      кнопка неотличима от мёртвой.

    static func checkManualFallback() {
        // --- 1. якоря разделов --------------------------------------------------
        check("«Файлы и папки» открываются якорем Privacy_FilesAndFolders",
              FolderAccessAction.openFilesAndFolders.settingsAnchor == "Privacy_FilesAndFolders")
        check("«Полный доступ к диску» — якорь Privacy_AllFiles",
              FolderAccessAction.openPrivacyPane.settingsAnchor == "Privacy_AllFiles")
        check("два РАЗНЫХ раздела, а не один и тот же дважды",
              FolderAccessAction.openFilesAndFolders.settingsAnchor
                  != FolderAccessAction.openPrivacyPane.settingsAnchor)
        check("у кнопок, не ведущих в настройки, якоря нет",
              FolderAccessAction.recheck.settingsAnchor == nil
                  && FolderAccessAction.copyHelperPath.settingsAnchor == nil)

        // Якоря сверяются С САМОЙ СИСТЕМОЙ, а не с документацией: панель уже один раз
        // соврала этому проекту (урок донора 020B). `TCCServiceList.plist` и таблица
        // строк расширения — то, по чему System Settings реально ищет раздел.
        let ext = "/System/Library/ExtensionKit/Extensions/SecurityPrivacyExtension.appex"
        let declared = declaredPrivacyAnchors(extensionPath: ext)
        if declared.isEmpty {
            check("якоря разделов сверены с системой (расширение не найдено — пропуск)", true,
                  "нет \(ext)")
        } else {
            for action in [FolderAccessAction.openFilesAndFolders, .openPrivacyPane] {
                let anchor = action.settingsAnchor ?? ""
                check("система знает якорь \(anchor)", declared.contains(anchor),
                      "объявленных якорей: \(declared.count)")
            }
            check("НЕГ.КОНТРОЛЬ: выдуманный якорь система НЕ знает",
                  !declared.contains("Privacy_FolderAccessDefinitelyNotAThing"))
        }

        // --- 2. два маршрута, а не один -----------------------------------------
        let zoned = FolderAccessCopy.routeToggleSteps(zone: "Рабочий стол")
        check("маршрут 1 ведёт в «Файлы и папки»",
              zoned.contains { $0.contains("Файлы и папки") })
        check("маршрут 1 называет КОНКРЕТНЫЙ переключатель",
              zoned.contains { $0.contains("«Рабочий стол»") })
        check("без защищённой зоны переключатель не выдумывается",
              !FolderAccessCopy.routeToggleSteps(zone: nil).contains { $0.contains("«Рабочий стол»") })
        check("маршрут 2 ведёт в «Полный доступ к диску» и жмёт «+»",
              FolderAccessCopy.routeAddSteps.contains { $0.contains("Полный доступ к диску") }
                  && FolderAccessCopy.routeAddSteps.contains { $0.contains("«+»") })
        check("маршруты различаются и по заголовку",
              FolderAccessCopy.routeToggleTitle != FolderAccessCopy.routeAddTitle)
        check("сначала дешёвый ремонт (тумблер), потом тяжёлый (+)",
              FolderAccessCopy.routeToggleSteps(zone: "Документы").count
                  < FolderAccessCopy.routeAddSteps.count)

        // --- 3. имена зон — те же, что показывает macOS -------------------------
        let home = "/Users/tester"
        check("Рабочий стол опознан",
              LocalWatchFolder.protectedZoneName(for: "\(home)/Desktop/mp3-to-m4b", home: home)
                  == "Рабочий стол")
        check("Документы опознаны",
              LocalWatchFolder.protectedZoneName(for: "\(home)/Documents/x", home: home) == "Документы")
        check("Загрузки опознаны",
              LocalWatchFolder.protectedZoneName(for: "\(home)/Downloads", home: home) == "Загрузки")
        check("вне защищённых зон переключателя нет",
              LocalWatchFolder.protectedZoneName(for: LocalWatchFolder.path(home: home),
                                                 home: home) == nil)

        // --- 4. кнопка ОБЯЗАНА показать, что сработала --------------------------
        check("успех копирования: заголовок меняется",
              FolderAccessAck.copyTitle(copied: true) != FolderAccessAck.copyTitle(copied: false))
        check("успех копирования: меняется ИКОНКА, не только текст",
              FolderAccessAck.copyIcon(copied: true) != FolderAccessAck.copyIcon(copied: false),
              FolderAccessAck.copyIcon(copied: true))
        check("успех копирования: меняется ЦВЕТ кнопки",
              FolderAccessAck.copyTone(copied: true, refused: false) == .success
                  && FolderAccessAck.copyTone(copied: false, refused: false) == .neutral)
        check("отказ виден на самой кнопке, а не только в журнале",
              FolderAccessAck.copyTone(copied: false, refused: true) == .danger)
        check("отказ НИКОГДА не красится в цвет успеха",
              FolderAccessAck.copyTone(copied: true, refused: true) == .danger)
        check("расписка об успехе гаснет (иначе второе нажатие ничего не меняет)",
              FolderAccessAck.successLingers > 0 && FolderAccessAck.successLingers <= 5,
              "\(FolderAccessAck.successLingers) с")
        check("текст расписки читается как подтверждение",
              FolderAccessCopy.copyDone.contains("✓"), FolderAccessCopy.copyDone)
        check("у отказа есть объяснение, а не просто красный цвет",
              FolderAccessCopy.copyRefused.contains("не скопирован"))
        check("не открывшаяся панель настроек тоже отвечает пользователю",
              FolderAccessCopy.paneOpenFailed.contains("Файлы и папки"))

        // --- 5. НЕГАТИВНЫЙ КОНТРОЛЬ --------------------------------------------
        // Как было: один маршрут, один якорь (FDA), расписка — только смена текста.
        // Фикстуры обязаны различать старое поведение и новое.
        let oldSingleAnchor = "Privacy_AllFiles"
        check("НЕГ.КОНТРОЛЬ: старый код вёл ОБЕ кнопки в Полный доступ к диску",
              FolderAccessAction.openPrivacyPane.settingsAnchor == oldSingleAnchor
                  && FolderAccessAction.openFilesAndFolders.settingsAnchor != oldSingleAnchor)
        let textOnlyAck: (Bool) -> (String, String, String) = { copied in
            (copied ? "Путь скопирован" : "Скопировать путь агента", "doc.on.doc", "neutral")
        }
        check("НЕГ.КОНТРОЛЬ: расписка только текстом — иконка и цвет не менялись",
              textOnlyAck(true).1 == textOnlyAck(false).1
                  && FolderAccessAck.copyIcon(copied: true) != FolderAccessAck.copyIcon(copied: false))
    }

    /// Anchors this macOS actually knows, read out of the Settings privacy extension
    /// (`TCCServiceList.plist` + the binary's string table). Empty ⇒ the extension is
    /// not where we expect, and the caller skips rather than failing on a machine
    /// that simply differs.
    static func declaredPrivacyAnchors(extensionPath: String) -> Set<String> {
        var found: Set<String> = []
        let fm = FileManager.default
        guard fm.fileExists(atPath: extensionPath) else { return [] }
        let resources = extensionPath + "/Contents/Resources"
        var candidates = [resources + "/TCCServiceList.plist"]
        if let macos = try? fm.contentsOfDirectory(atPath: extensionPath + "/Contents/MacOS") {
            candidates += macos.map { extensionPath + "/Contents/MacOS/" + $0 }
        }
        for path in candidates {
            guard let data = fm.contents(atPath: path) else { continue }
            // Scan bytes for the literal anchor names — works for both the plist and
            // the Mach-O string table without parsing either.
            guard let text = String(data: data, encoding: .isoLatin1) else { continue }
            for name in ["Privacy_FilesAndFolders", "Privacy_AllFiles",
                         "Privacy_DesktopFolder", "Privacy_DownloadsFolder"]
            where text.contains(name) {
                found.insert(name)
            }
        }
        return found
    }

    // MARK: M6 — «Проверить снова»: three outcomes, not two

    static func checkFolderRecheck() {
        // The wait is on the TOKEN, not on the verdict: `folder_access_ts` moves on
        // every probe, so "проверил, всё так же denied" terminates the wait.
        check("токен сдвинулся, доступ ok → карточка растворяется",
              FolderRecheck.evaluate(tokenBefore: "t1", tokenAfter: "t2",
                                     verdict: .ok, agentIsBuilding: false) == .ok)
        check("токен сдвинулся, всё так же denied → честное «по-прежнему нет»",
              FolderRecheck.evaluate(tokenBefore: "t1", tokenAfter: "t2",
                                     verdict: .denied, agentIsBuilding: false)
                  == .stillProblem(.denied))
        check("проверка увидела ДРУГУЮ проблему (denied → blocked) — несёт её с собой",
              FolderRecheck.evaluate(tokenBefore: "t1", tokenAfter: "t2",
                                     verdict: .blocked, agentIsBuilding: false)
                  == .stillProblem(.blocked))
        check("токен не сдвинулся, идёт сборка → «проверим после сборки» (M5f)",
              FolderRecheck.evaluate(tokenBefore: "t1", tokenAfter: "t1",
                                     verdict: .denied, agentIsBuilding: true) == .busy)
        check("токен не сдвинулся, сборки нет → агент не ответил",
              FolderRecheck.evaluate(tokenBefore: "t1", tokenAfter: "t1",
                                     verdict: .denied, agentIsBuilding: false) == .probeFailed)
        check("агент ещё ни разу не публиковал токен (nil→nil) → не выдаём это за успех",
              FolderRecheck.evaluate(tokenBefore: nil, tokenAfter: nil,
                                     verdict: nil, agentIsBuilding: false) == .probeFailed)
        check("первый в жизни токен (nil→t1) считается сдвигом",
              FolderRecheck.evaluate(tokenBefore: nil, tokenAfter: "t1",
                                     verdict: .ok, agentIsBuilding: false) == .ok)
        check("токен сдвинулся, а вердикта нет → мы ничего не узнали, а не «всё хорошо»",
              FolderRecheck.evaluate(tokenBefore: "t1", tokenAfter: "t2",
                                     verdict: nil, agentIsBuilding: false) == .probeFailed)
        check("незнакомый вердикт после проверки не выдаётся за ok",
              FolderRecheck.evaluate(tokenBefore: "t1", tokenAfter: "t2",
                                     verdict: .unknown("quarantined"), agentIsBuilding: false)
                  == .stillProblem(.unknown("quarantined")))

        // --- pinned card dissolves on its own ------------------------------------
        check("живой ok растворяет закреплённую карточку «доступа нет»",
              FolderRecheck.terminalRecheckDissolves(pinned: .stillDenied(.denied), live: .ok))
        check("живой ok растворяет и «занят сборкой», и «не ответил»",
              FolderRecheck.terminalRecheckDissolves(pinned: .busy(.denied), live: .ok)
                  && FolderRecheck.terminalRecheckDissolves(pinned: .timeout(.denied), live: .ok))
        check("сменившаяся проблема тоже растворяет закреплённую карточку",
              FolderRecheck.terminalRecheckDissolves(pinned: .stillDenied(.denied),
                                                     live: .blocked))
        check("та же проблема — карточка остаётся закреплённой",
              !FolderRecheck.terminalRecheckDissolves(pinned: .stillDenied(.denied),
                                                      live: .denied))
        check("незакрепляемые состояния не «растворяются» (их и не закрепляли)",
              !FolderRecheck.terminalRecheckDissolves(pinned: .problem(.denied), live: .ok)
                  && !FolderRecheck.terminalRecheckDissolves(pinned: .checking(.denied), live: .ok))
        check("вердикта нет вовсе → закреплённую карточку не снимаем (нечем)",
              !FolderRecheck.terminalRecheckDissolves(pinned: .timeout(.denied), live: nil))

        // «Проверим после сборки» freezes every button (a second recheck would queue
        // behind the same build; a folder move mid-build would orphan a half-written
        // .m4b). That is only defensible WHILE the build lasts: if the pin outlived
        // it, the user would be left with a card whose every control is dead —
        // lesson 005, the exact class of bug this project keeps paying for.
        check("сборка кончилась → «проверим после сборки» снимается, кнопки оживают",
              FolderRecheck.terminalRecheckDissolves(pinned: .busy(.denied), live: .denied,
                                                     agentIsBuilding: false))
        check("сборка идёт → «проверим после сборки» держится",
              !FolderRecheck.terminalRecheckDissolves(pinned: .busy(.denied), live: .denied,
                                                      agentIsBuilding: true))
        check("конец сборки не снимает ОСТАЛЬНЫЕ закреплённые состояния",
              !FolderRecheck.terminalRecheckDissolves(pinned: .stillDenied(.denied),
                                                      live: .denied, agentIsBuilding: false)
                  && !FolderRecheck.terminalRecheckDissolves(pinned: .timeout(.denied),
                                                             live: .denied, agentIsBuilding: false))

        // --- НЕГАТИВНЫЙ КОНТРОЛЬ -------------------------------------------------
        // The two-outcome version (moved ? ok : failed) is the natural thing to
        // write, and it calls a busy agent broken — sending the user to fix an agent
        // that is working perfectly. The fixture below separates the two rules.
        let naive: (String?, String?) -> FolderRecheck.Outcome = { before, after in
            (after != nil && after != before) ? .ok : .probeFailed
        }
        check("НЕГ.КОНТРОЛЬ: наивная проверка назвала бы занятого агента сломанным",
              naive("t1", "t1") == .probeFailed
                  && FolderRecheck.evaluate(tokenBefore: "t1", tokenAfter: "t1",
                                            verdict: .denied, agentIsBuilding: true) == .busy)
        check("НЕГ.КОНТРОЛЬ: наивная проверка объявила бы «всё хорошо» на сдвиге токена",
              naive("t1", "t2") == .ok
                  && FolderRecheck.evaluate(tokenBefore: "t1", tokenAfter: "t2",
                                            verdict: .denied, agentIsBuilding: false)
                      != .ok)
    }

    // MARK: M6 — the repair folder (~/mp3-to-m4b is outside TCC entirely)

    static func checkLocalWatchFolder() {
        let home = "/Users/tester"
        check("Рабочий стол — защищённая зона",
              LocalWatchFolder.isProtected("\(home)/Desktop/mp3-to-m4b", home: home))
        check("Документы — защищённая зона",
              LocalWatchFolder.isProtected("\(home)/Documents/книги", home: home))
        check("Загрузки — защищённая зона (это её показал системный диалог в T0)",
              LocalWatchFolder.isProtected("\(home)/Downloads", home: home))
        check("корень домашней папки — НЕ защищённая зона (в этом весь смысл ремонта)",
              !LocalWatchFolder.isProtected(LocalWatchFolder.path(home: home), home: home))
        check("цель ремонта — ~/mp3-to-m4b",
              LocalWatchFolder.path(home: home) == "\(home)/mp3-to-m4b")
        check("папка с похожим именем не считается защищённой по префиксу",
              !LocalWatchFolder.isProtected("\(home)/Desktopish/x", home: home))
        check("чужой домашний каталог не путается с нашим",
              !LocalWatchFolder.isProtected("/Users/other/Desktop/x", home: home))
        check("хвостовой слэш не ломает сравнение",
              LocalWatchFolder.isProtected("\(home)/Desktop/", home: home))
        check("~ раскрывается при отображении",
              LocalWatchFolder.tildeAbbreviated("\(home)/mp3-to-m4b", home: home)
                  == "~/mp3-to-m4b")
    }

    // MARK: M5 — tolerant decode of folder_access (a NEW value must not go silent)

    static func decodeAgent(_ json: String) -> AgentInfo {
        let data = Data(json.utf8)
        let state = (try? JSONDecoder().decode(ShowcaseState.self, from: data))
        return state?.agent ?? AgentInfo(watchDir: nil)
    }

    static func checkFolderAccessDecode() {
        let known = decodeAgent(#"{"agent":{"watch_dir":"/w","folder_access":"blocked","folder_access_ts":"2026-07-26T10:00:00Z","install_generation":"G1"}}"#)
        check("декод: blocked распознан явно (не «неизвестное»)",
              known.folderAccess == .blocked, "\(String(describing: known.folderAccess))")
        check("декод: folder_access_ts прочитан", known.folderAccessTs == "2026-07-26T10:00:00Z")
        check("декод: install_generation прочитан", known.installGeneration == "G1")

        // The bug this guards: "unknown → nil ⇒ нет поверхности". A newer agent
        // publishing a state this build never heard of must NOT render as calm.
        let alien = decodeAgent(#"{"agent":{"watch_dir":"/w","folder_access":"quarantined","install_generation":"G1"}}"#)
        check("декод: НЕЗНАКОМОЕ значение сохранено как .unknown, а не проглочено в nil",
              alien.folderAccess == .unknown("quarantined"),
              "\(String(describing: alien.folderAccess))")

        // Fed the DECODED value on purpose: a decoder that swallows the unknown
        // value would otherwise be caught by the check above alone, while the
        // router half kept passing on a hand-built fixture that never occurs.
        var t = healthyTruth(); t.folderAccess = alien.folderAccess
        check("роутер: неизвестный доступ → своя поверхность, НЕ «всё хорошо»",
              t.surface == .accessUnknown("quarantined"), "\(t.surface)")

        // Absent / empty stays nil — "агент ничего не говорил" ≠ "сказал непонятное".
        let silent = decodeAgent(#"{"agent":{"watch_dir":"/w"}}"#)
        check("декод: поля нет → nil (это другое утверждение, чем .unknown)",
              silent.folderAccess == nil)
        let empty = decodeAgent(#"{"agent":{"watch_dir":"/w","folder_access":""}}"#)
        check("декод: пустая строка → nil, а не .unknown(\"\")", empty.folderAccess == nil)

        // Old states (no agent block at all) still decode.
        let legacy = decodeAgent(#"{"schema":1,"books":[]}"#)
        check("декод: старый state.json без новых полей не падает",
              legacy.folderAccess == nil && legacy.installGeneration == nil)
    }

    // MARK: M5 — which folder is really watched (M2f)

    static func checkWatchDirTruth() {
        check("порядок: чек установки побеждает plist и state",
              WatchDirTruth.resolve(receiptWatchDir: "/R", receiptGeneration: "G1",
                                    plistWatchDir: "/P", stateWatchDir: "/S",
                                    stateGeneration: "G1") == "/R")
        check("нет чека → plist",
              WatchDirTruth.resolve(receiptWatchDir: nil, receiptGeneration: nil,
                                    plistWatchDir: "/P", stateWatchDir: "/S",
                                    stateGeneration: "G1") == "/P")
        // The hazard: an agent from the PREVIOUS generation keeps rewriting
        // state.json with the OLD folder. Taking it would re-point the user silently.
        check("state с ЧУЖИМ поколением игнорируется (иначе тихий репойнт)",
              WatchDirTruth.resolve(receiptWatchDir: nil, receiptGeneration: "G2",
                                    plistWatchDir: nil, stateWatchDir: "/S",
                                    stateGeneration: "G1") == nil)
        check("state со СВОИМ поколением принимается последним источником",
              WatchDirTruth.resolve(receiptWatchDir: nil, receiptGeneration: "G1",
                                    plistWatchDir: nil, stateWatchDir: "/S",
                                    stateGeneration: "G1") == "/S")
        check("ничего не известно → nil (а НЕ ~/Desktop/mp3-to-m4b по умолчанию)",
              WatchDirTruth.resolve(receiptWatchDir: nil, receiptGeneration: nil,
                                    plistWatchDir: nil, stateWatchDir: nil,
                                    stateGeneration: nil) == nil)
        check("пустые строки не считаются значением",
              WatchDirTruth.resolve(receiptWatchDir: "", receiptGeneration: "G1",
                                    plistWatchDir: "", stateWatchDir: "/S",
                                    stateGeneration: "G1") == "/S")
    }

    // MARK: M5 — the LIVE disk read (fail-closed by construction)

    /// Writes a throwaway LaunchAgent plist + receipt in a temp tree and drives the
    /// REAL readers over them (`plutil -extract ProgramArguments.0`). Nothing here
    /// touches launchd, the production support tree or the human's plist: the whole
    /// fixture lives under one temp dir that is removed at the end.
    static func checkDiskTruth() {
        let fm = FileManager.default
        let root = URL(fileURLWithPath: NSTemporaryDirectory(), isDirectory: true)
            .appendingPathComponent("mp3tom4b-selfcheck-truth-\(UUID().uuidString)",
                                    isDirectory: true)
        let support = root.appendingPathComponent("support", isDirectory: true)
        let agents = root.appendingPathComponent("LaunchAgents", isDirectory: true)
        defer { try? fm.removeItem(at: root) }
        try? fm.createDirectory(at: support.appendingPathComponent("bin"),
                                withIntermediateDirectories: true)
        try? fm.createDirectory(at: agents, withIntermediateDirectories: true)

        // Redirect BOTH knobs the store honors, so nothing can resolve to the real
        // tree even if a path is built wrong.
        setenv("MP3TOM4B_LAUNCHAGENTS_DIR", agents.path, 1)
        setenv("MP3TOM4B_LABEL", "com.arrivarus.mp3tom4b.selfcheck", 1)
        defer {
            unsetenv("MP3TOM4B_LAUNCHAGENTS_DIR")
            unsetenv("MP3TOM4B_LABEL")
        }
        let store = StateStore(supportRoot: support)

        func writePlist(pa0: String, watch: String) {
            let dict: [String: Any] = [
                "Label": "com.arrivarus.mp3tom4b.selfcheck",
                "ProgramArguments": [pa0],
                "EnvironmentVariables": ["MP3TOM4B_WATCH_DIR": watch],
            ]
            let data = try! PropertyListSerialization.data(
                fromPropertyList: dict, format: .xml, options: 0)
            try? data.write(to: URL(fileURLWithPath: store.launchAgentPlistPath))
        }

        check("нет plist → PA0 неизвестен (nil), и это НЕ считается «всё хорошо»",
              store.diskProgramArgument0() == nil && !store.installedRunnerIsHelper())
        check("нет ни plist, ни чека → установки нет", !store.hasInstallEvidence())

        // v0.9 shape: PA0 = runner.sh (the construction 1.0 exists to remove).
        writePlist(pa0: store.installedRunnerPath, watch: "/tmp/watch-plist")
        check("PA0 = runner.sh (v0.9) → installedRunnerIsHelper == false",
              !store.installedRunnerIsHelper(),
              store.diskProgramArgument0() ?? "nil")
        check("plist есть → установка обнаружена", store.hasInstallEvidence())
        check("watch_dir читается из plist", store.plistWatchDir() == "/tmp/watch-plist")

        // 1.0 shape: PA0 = the frozen helper.
        writePlist(pa0: store.installedHelperPath, watch: "/tmp/watch-plist")
        check("PA0 = замороженный helper → installedRunnerIsHelper == true",
              store.installedRunnerIsHelper(), store.diskProgramArgument0() ?? "nil")

        // A path that only LOOKS right (trailing slash / dot segments) is fine…
        writePlist(pa0: support.path + "/bin/./mp3-to-m4b-agent", watch: "/tmp/w")
        check("шум в пути (./) нормализуется — это тот же файл",
              store.installedRunnerIsHelper())
        // …but a neighbour with a similar name is NOT.
        writePlist(pa0: store.installedHelperPath + "-t0", watch: "/tmp/w")
        check("похожее имя (…-agent-t0) НЕ считается нашим helper'ом",
              !store.installedRunnerIsHelper())

        // Receipt parsing.
        let receipt = URL(fileURLWithPath: store.receiptPath)
        try? Data(#"{"schema":1,"generation":"GEN-9","engine_version":"1.0","mode":"full","watch_dir":"/tmp/watch-receipt","helper_path":"/x","plist":"/y","support_dir":"/z","installed_at":"2026-07-26T00:00:00Z"}"#.utf8)
            .write(to: receipt)
        check("чек установки разобран", store.loadReceipt()?.generation == "GEN-9")
        check("чек: engine_version прочитан (правило bundled >= installed)",
              store.loadReceipt()?.engineVersion == "1.0")

        // A receipt WITHOUT a generation proves nothing → treated as no receipt.
        try? Data(#"{"schema":1,"watch_dir":"/tmp/x"}"#.utf8).write(to: receipt)
        check("чек без generation → считается отсутствующим (fail-closed)",
              store.loadReceipt() == nil)
        try? Data("{ this is not json".utf8).write(to: receipt)
        check("битый чек → nil, без падения", store.loadReceipt() == nil)

        // The full disk→truth path, end to end.
        try? Data(#"{"schema":1,"generation":"GEN-9","engine_version":"1.0","watch_dir":"/tmp/watch-receipt"}"#.utf8)
            .write(to: receipt)
        writePlist(pa0: store.installedHelperPath, watch: "/tmp/watch-plist")
        let stateOK = ShowcaseState(
            schema: 1,
            agent: AgentInfo(watchDir: "/tmp/watch-state", active: true,
                             folderAccess: .denied, folderAccessTs: "t",
                             installGeneration: "GEN-9"),
            books: [], batch: nil)
        let truthOK = store.installTruth(state: stateOK, updateOccupiesWindow: false,
                                         secondsSinceInstallSettled: 3600)
        check("диск→истина: всё сходится → поверхность доступа разрешена",
              truthOK.allowsFolderAccessSurface
                  && truthOK.surface == .folderAccess(.denied), "\(truthOK.surface)")
        check("диск→истина: watch_dir берётся из ЧЕКА, а не из state",
              store.resolvedWatchDir(state: stateOK) == "/tmp/watch-receipt")

        let stateOld = ShowcaseState(
            schema: 1,
            agent: AgentInfo(watchDir: "/tmp/watch-state", active: true,
                             folderAccess: .denied, folderAccessTs: "t",
                             installGeneration: "GEN-1"),
            books: [], batch: nil)
        let truthOld = store.installTruth(state: stateOld, updateOccupiesWindow: false,
                                          secondsSinceInstallSettled: 3600)
        check("диск→истина: живёт агент прошлого поколения → карточка запрещена",
              !truthOld.allowsFolderAccessSurface
                  && truthOld.surface == .agentNotRunning(.generationMismatch),
              "\(truthOld.surface)")

        // Diagnostics must actually carry every source (the dead-end screens).
        let diag = store.diagnostics(state: stateOld, stderrTail: "boom")
        check("диагностика: показаны обе величины поколения",
              diag.receiptGeneration == "GEN-9" && diag.stateGeneration == "GEN-1")
        check("диагностика: три источника папки видны по отдельности",
              diag.receiptWatchDir == "/tmp/watch-receipt"
                  && diag.plistWatchDir == "/tmp/watch-plist"
                  && diag.stateWatchDir == "/tmp/watch-state")
        check("диагностика: 9 строк + stderr установщика",
              diag.rows.count == 9 && diag.plainText.contains("boom"))
    }

    // MARK: M5 — single-flight over the installer (B4)

    static func checkSingleFlight() {
        let coord = InstallCoordinator(
            workQueue: DispatchQueue(label: "selfcheck.install.work"),
            completionQueue: DispatchQueue(label: "selfcheck.install.done"))

        let entered = DispatchSemaphore(value: 0)
        let release = DispatchSemaphore(value: 0)
        let finished = DispatchSemaphore(value: 0)
        let lock = NSLock()
        var workRuns = 0
        var outcomes: [String: InstallOutcome] = [:]

        func record(_ tag: String) -> (InstallOutcome) -> Void {
            return { outcome in
                lock.lock(); outcomes[tag] = outcome; lock.unlock()
                finished.signal()
            }
        }

        let first = coord.submit(id: "agent-update", work: {
            lock.lock(); workRuns += 1; lock.unlock()
            entered.signal()
            release.wait()          // hold the flow open while we probe admission
            return .done
        }, completion: record("first"))
        check("single-flight: первый запуск стартует", first == .started, "\(first)")
        _ = entered.wait(timeout: .now() + 5)

        let sameAgain = coord.submit(id: "agent-update",
                                     work: { lock.lock(); workRuns += 1; lock.unlock(); return .done },
                                     completion: record("same"))
        check("single-flight: ТОТ ЖЕ запуск во время работы → присоединяется, не стартует",
              sameAgain == .joined, "\(sameAgain)")

        let other = coord.submit(id: "watch-repoint",
                                 work: { lock.lock(); workRuns += 1; lock.unlock(); return .done },
                                 completion: record("other"))
        check("single-flight: ДРУГАЯ операция во время работы → отклонена",
              other == .refused, "\(other)")
        check("single-flight: во время работы координатор знает, чем занят",
              coord.busyWith == "agent-update", coord.busyWith ?? "nil")

        release.signal()
        for _ in 0..<3 { _ = finished.wait(timeout: .now() + 5) }

        lock.lock()
        let runs = workRuns
        let firstOut = outcomes["first"], sameOut = outcomes["same"], otherOut = outcomes["other"]
        lock.unlock()

        check("single-flight: установщик запущен РОВНО ОДИН раз (а не трижды)",
              runs == 1, "runs=\(runs)")
        check("single-flight: присоединившийся получил результат общего прогона",
              firstOut == .done && sameOut == .done,
              "\(String(describing: firstOut)) / \(String(describing: sameOut))")
        if case .failed(let msg)? = otherOut {
            check("single-flight: отклонённый получил честную причину, а не «успех»",
                  msg.contains("обновляется"), msg)
        } else {
            check("single-flight: отклонённый получил .failed", false,
                  "\(String(describing: otherOut))")
        }
        check("single-flight: после завершения координатор свободен",
              coord.busyWith == nil)

        // Sequential re-use: a coordinator that stayed "busy" forever would silently
        // block every later install.
        let after = coord.submit(id: "agent-update", work: { .done },
                                 completion: { _ in finished.signal() })
        check("single-flight: следующая установка после завершения стартует",
              after == .started, "\(after)")
        _ = finished.wait(timeout: .now() + 5)
    }

    // MARK: M5 — what launch does to the install (plan v2 §6.2)

    static func checkStartupPlan() {
        // Healthy 1.0 install — touch nothing.
        check("старт: всё в порядке → ничего не трогаем",
              StartupPlan.decide(isInstalled: true, bytesStale: false,
                                 bundledIsOlderThanInstall: false, watchDirKnown: true,
                                 pa0IsHelper: true, helperStaged: true) == .none)
        check("старт: ничего не установлено → Setup",
              StartupPlan.decide(isInstalled: false, bytesStale: true,
                                 bundledIsOlderThanInstall: false, watchDirKnown: true,
                                 pa0IsHelper: false, helperStaged: false) == .setup)

        // THE ordering bug this rule exists for: on v0.9 BOTH conditions hold, and
        // the offline repair cannot help (no staged helper → it dies on golden SHA).
        check("старт: v0.9 (байты старые И PA0 кривой, helper не уложен) → ПОЛНАЯ установка",
              StartupPlan.decide(isInstalled: true, bytesStale: true,
                                 bundledIsOlderThanInstall: false, watchDirKnown: true,
                                 pa0IsHelper: false, helperStaged: false) == .fullInstall)
        check("старт: байты старые, но helper уже уложен → всё равно ПОЛНАЯ (не починка)",
              StartupPlan.decide(isInstalled: true, bytesStale: true,
                                 bundledIsOlderThanInstall: false, watchDirKnown: true,
                                 pa0IsHelper: false, helperStaged: true) == .fullInstall)

        // The case the offline repair actually owns: installer died between publish
        // and bootstrap — bytes fine, job pointed at the wrong executable.
        check("старт: байты свежие, PA0 кривой → офлайн-починка launchd",
              StartupPlan.decide(isInstalled: true, bytesStale: false,
                                 bundledIsOlderThanInstall: false, watchDirKnown: true,
                                 pa0IsHelper: false, helperStaged: true)
                  == .repairLaunchdOnly)
        check("старт: PA0 кривой, но helper не уложен → починка НЕ запускается",
              StartupPlan.decide(isInstalled: true, bytesStale: false,
                                 bundledIsOlderThanInstall: false, watchDirKnown: true,
                                 pa0IsHelper: false, helperStaged: false) == .none)

        // Our advantage over the donor — never guess the folder.
        check("старт: папка НЕИЗВЕСТНА → авто-обновление НЕ запускается (донор тут падает на дефолт)",
              StartupPlan.decide(isInstalled: true, bytesStale: true,
                                 bundledIsOlderThanInstall: false, watchDirKnown: false,
                                 pa0IsHelper: true, helperStaged: true) == .none)
        check("старт: папка неизвестна, но PA0 кривой → починка можно (она переносит папку сама)",
              StartupPlan.decide(isInstalled: true, bytesStale: true,
                                 bundledIsOlderThanInstall: false, watchDirKnown: false,
                                 pa0IsHelper: false, helperStaged: true)
                  == .repairLaunchdOnly)

        // M11f — an older app must never touch a newer install.
        check("старт: приложение СТАРШЕ установки → не трогаем ничего (иначе даунгрейд)",
              StartupPlan.decide(isInstalled: true, bytesStale: true,
                                 bundledIsOlderThanInstall: true, watchDirKnown: true,
                                 pa0IsHelper: false, helperStaged: true) == .none)
    }

    // MARK: M5 — bundled >= installed (M11f)

    static func checkVersionRule() {
        check("версии: 1.0 ≥ 0.9", EngineVersion.atLeast("1.0", "0.9"))
        check("версии: 1.0 ≥ 1.0", EngineVersion.atLeast("1.0", "1.0"))
        check("версии: 0.9 НЕ ≥ 1.0 (даунгрейд ловится)",
              !EngineVersion.atLeast("0.9", "1.0"))
        check("версии: 1.10 ≥ 1.9 (числовое, не строковое сравнение)",
              EngineVersion.atLeast("1.10", "1.9"))
        check("версии: 1.0.1 ≥ 1.0", EngineVersion.atLeast("1.0.1", "1.0"))
        check("версии: 1.0 НЕ ≥ 1.0.1", !EngineVersion.atLeast("1.0", "1.0.1"))
        check("версии: суффиксы отбрасываются (1.0-beta ≥ 1.0)",
              EngineVersion.atLeast("1.0-beta", "1.0"))
    }

    // MARK: - D17 «ранний нудж» — сторона приложения (M-D)
    //
    // Что здесь охраняется. D17 разрезал публикацию книги на фазы
    // (skeleton → chapters → ready) ради окна за ~0.8 с вместо ~12 с, и это дало
    // приложению три новые возможности молча всё испортить:
    //
    //   1. СОБРАТЬ НЕПОЛНУЮ КНИГУ. Гейт «Собрать» обязан спрашивать «видел ли
    //      отправитель ПОЛНУЮ книгу», а не «готова ли книга сейчас» — иначе
    //      команда, рождённая по скелету, дождётся дренажа и соберёт обрезанную
    //      аудиокнигу (TOCTOU). Ответ — наличие `build_token`, см. `isBuildReady`.
    //   2. СЪЕСТЬ ДАННЫЕ ЧЕЛОВЕКА. SwiftUI держит `@State` живым между
    //      обновлениями, так что ТО ЖЕ окно получает более новый манифест той же
    //      книги. Оба архитектора назвали это главной опасностью на стороне
    //      приложения. Ответ — одна чистая функция `ConfirmMerge.merge`, и она
    //      здесь проверяется всей матрицей pristine/dirty.
    //   3. ПОДНЯТЬ ОКНО ДВАЖДЫ. У приложения свой канал подъёма (rising-edge по
    //      state.json) мимо агентского леджера. Ответ — `NudgeEdge`: те же ключи,
    //      что у агента, побайтово. Скелет и ready дают ОДИН ключ ⇒ второго
    //      подъёма нет. Зеркальность проверяется ЧТЕНИЕМ `agent/scan.py`, а не
    //      комментарием (см. `checkNudgeEdgeMirror`).

    /// Фикстура-манифест для проверок фаз/слияния.
    static func d17Manifest(phase: String, token: String, title: String, author: String,
                            rev: String = "rev-aaaaaaaaaaaaaaaaaaaa", bid: String = "book1",
                            options: [CoverOption] = [], selected: String? = nil,
                            params: BookParams = .defaults,
                            chapters: [ChapterEntry] = []) -> BookManifest {
        BookManifest(bookID: bid, srcDir: "/src", status: "pending-confirm",
                     sourceRev: rev, confirmToken: "tok-bbbbbbbbbbbbbbbbbbbb",
                     title: title, author: author, chapters: chapters,
                     totalDurationMS: 0, coverState: "none", coverPreview: nil,
                     coverOptions: options, coverSelected: selected, params: params,
                     phase: phase, buildToken: token)
    }

    /// Манифест ИЗ JSON — единственный честный способ проверить «поля нет вовсе»
    /// (до-D17 манифест, манифест от более нового агента). Промах разбора отдаётся
    /// `nil` и краснеет у вызывающего, а не превращается в «нечего проверять».
    static func d17Decode(_ json: String) -> BookManifest? {
        try? JSONDecoder().decode(BookManifest.self, from: Data(json.utf8))
    }

    static let d17ManifestJSONFields =
        "\"book_id\":\"b\",\"src_dir\":\"/s\",\"status\":\"pending-confirm\"," +
        "\"source_rev\":\"r\",\"confirm_token\":\"t\",\"title\":\"T\",\"author\":\"A\"," +
        "\"chapters\":[],\"params\":{}"

    /// ФАЗА И ГЕЙТ СБОРКИ: что приложение вправе строить, а что — нет.
    static func checkManifestPhaseGate() {
        let skel = d17Manifest(phase: "skeleton", token: "", title: "01 Файл", author: "Папка")
        let chap = d17Manifest(phase: "chapters", token: "", title: "Война и мир",
                               author: "Толстой")
        let ready = d17Manifest(
            phase: "ready", token: "bt-1", title: "Война и мир", author: "Лев Толстой",
            options: [CoverOption(optID: "emb-0", kind: "embedded", path: "/a.jpg", label: "a"),
                      CoverOption(optID: "gen-0", kind: "generated", path: "/b.jpg", label: "b")],
            selected: "emb-0")

        check("D17: скелет не даёт собирать", !skel.isBuildReady)
        check("D17: фаза chapters не даёт собирать (главы есть, обложки нет)",
              !chap.isBuildReady)
        check("D17: ready даёт собирать", ready.isBuildReady)
        check("D17: гейт читает НАЛИЧИЕ build_token, а не имя фазы",
              !d17Manifest(phase: "ready", token: "", title: "T", author: "A").isBuildReady
                  && d17Manifest(phase: "skeleton", token: "bt",
                                 title: "T", author: "A").isBuildReady)

        // I8 — до-D17 манифест: поля phase/build_token нет вовсе.
        let legacy = d17Decode("{\(d17ManifestJSONFields)}")
        check("I8: до-D17 манифест разобран (иначе проверки ниже пусты)", legacy != nil)
        check("I8: манифест без phase читается как done", legacy?.phaseValue == .done)
        check("I8: манифест без build_token НЕ собирается (fail-closed)",
              legacy?.isBuildReady == false)
        let upgraded = d17Decode(
            "{\(d17ManifestJSONFields),\"phase\":\"ready\",\"build_token\":\"xyz\"}")
        check("I8: поднятый агентом pre-D17 манифест снова собирается",
              upgraded?.isBuildReady == true)

        // Манифест от БОЛЕЕ НОВОГО агента: незнакомая фаза не имеет права подвесить окно.
        let future = d17Decode("{\(d17ManifestJSONFields),\"phase\":\"covers-v2\"}")
        check("незнакомая фаза → done (не зависаем на заметке)", future?.phaseValue == .done)
        check("незнакомая фаза без токена → всё равно не собираем",
              future?.isBuildReady == false)
        check("незнакомая фаза без токена → говорим общее «готовлю»",
              future.map { ConfirmPreparation.forManifest($0) == .preparing } == true)

        check("подготовка: skeleton → «читаю теги»",
              ConfirmPreparation.forManifest(skel) == .readingTags)
        check("подготовка: chapters → «подбираю обложку»",
              ConfirmPreparation.forManifest(chap) == .resolvingCover)
        check("подготовка: ready → готово", ConfirmPreparation.forManifest(ready) == .ready)
        check("у ready нет заметки «ещё едет»",
              ConfirmPreparation.forManifest(ready).note == nil)
        check("у скелета заметка есть (окно объясняет, чего ждёт)",
              ConfirmPreparation.forManifest(skel).note != nil)
    }

    /// `cover_web` — ЯРЛЫК, а не гейт. Граница, которую поставил M-B: живая сеть
    /// против мёртвой = 0.022 с на кнопке; дотянись веб до гейта — вся работа M-B
    /// обнулится, причём молча.
    static func checkCoverWebIsALabel() {
        func webManifest(_ raw: String?, phase: String = "ready",
                         token: String = "bt") -> BookManifest? {
            let web = raw.map { ",\"cover_web\":\"\($0)\"" } ?? ""
            return d17Decode("{\(d17ManifestJSONFields),\"phase\":\"\(phase)\"," +
                             "\"build_token\":\"\(token)\",\"cover_web_tries\":2\(web)}")
        }
        guard let webPending = webManifest("pending"),
              let webDone = webManifest("done"),
              let webAbsent = webManifest(nil),
              let webJunk = webManifest("что-то новое"),
              let pendingSkeleton = webManifest("pending", phase: "skeleton", token: "") else {
            check("cover_web: фикстуры разобраны", false, "JSON не разобрался")
            return
        }
        check("cover_web=pending → показываем строку «ищем обложку»",
              webPending.isCoverWebPending)
        check("cover_web=done → строки нет", !webDone.isCoverWebPending)
        check("cover_web отсутствует (старый манифест) → ведём себя как done",
              !webAbsent.isCoverWebPending)
        check("незнакомое значение cover_web → как done (не врём про поиск)",
              !webJunk.isCoverWebPending)
        check("лишнее поле cover_web_tries не ломает разбор манифеста",
              webPending.title == "T")

        // ГЛАВНОЕ: веб-поиск не имеет права дотянуться до гейта сборки.
        check("ГЕЙТ: cover_web=pending НЕ гасит «Собрать» (веб вне критического пути)",
              webPending.isBuildReady)
        check("ГЕЙТ: build_token решает в одиночку — pending и done одинаково собираемы",
              webPending.isBuildReady == webDone.isBuildReady)
        check("ГЕЙТ: подготовка («ещё едет») слепа к cover_web",
              ConfirmPreparation.forManifest(webPending) == .ready
                  && ConfirmPreparation.forManifest(webPending)
                      == ConfirmPreparation.forManifest(webDone))
        check("ГЕЙТ: скелет с pending не собирается — из-за ТОКЕНА, а не из-за веба",
              !pendingSkeleton.isBuildReady
                  && ConfirmPreparation.forManifest(pendingSkeleton) == .readingTags)
    }

    /// МАТРИЦА pristine/dirty + три правила идентичности (`ConfirmMerge`).
    static func checkConfirmMerge() {
        let skel = d17Manifest(phase: "skeleton", token: "", title: "01 Файл", author: "Папка")
        let ready = d17Manifest(
            phase: "ready", token: "bt-1", title: "Война и мир", author: "Лев Толстой",
            options: [CoverOption(optID: "emb-0", kind: "embedded", path: "/a.jpg", label: "a"),
                      CoverOption(optID: "gen-0", kind: "generated", path: "/b.jpg", label: "b")],
            selected: "emb-0")

        let seeded = ConfirmMerge.seed(manifest: skel, fallbackTitle: "Из очереди")
        check("посев: title из манифеста", seeded.title == "01 Файл")
        check("посев: ничего не тронуто", seeded.touched == [])
        check("посев: обложки нет (у скелета нет вариантов)", seeded.coverSelectedID == nil)
        check("посев: пустой title подменяется строкой очереди, а не остаётся пустым",
              ConfirmMerge.seed(manifest: d17Manifest(phase: "skeleton", token: "",
                                                      title: "", author: "A"),
                                fallbackTitle: "Из очереди").title == "Из очереди")

        // pristine — всё обновляется из нового манифеста
        let m1 = ConfirmMerge.merge(seeded, with: ready, fallbackTitle: "Из очереди")
        check("pristine title ← манифест (скелетное имя файла уступает ID3)",
              m1.title == "Война и мир")
        check("pristine author ← манифест", m1.author == "Лев Толстой")
        check("pristine cover ← cover_selected", m1.coverSelectedID == "emb-0")

        // dirty — правка человека выигрывает
        var edited = seeded
        edited.title = "Моё название"
        edited.touched.insert(.title)
        let m2 = ConfirmMerge.merge(edited, with: ready, fallbackTitle: "Из очереди")
        check("dirty title переживает обновление манифеста", m2.title == "Моё название")
        check("…а pristine author всё равно обновился", m2.author == "Лев Толстой")

        var editedP = seeded
        editedP.params.bitrate = 64
        editedP.touched.insert(.params)
        let readyHi = d17Manifest(phase: "ready", token: "bt", title: "T", author: "A",
                                  params: BookParams(bitrate: 256, channels: "mono",
                                                     samplerate: 48000, split: true))
        let m3 = ConfirmMerge.merge(editedP, with: readyHi)
        check("dirty params переживают манифест ЦЕЛИКОМ (одно решение — один флаг)",
              m3.params.bitrate == 64 && !m3.params.split)
        let m3p = ConfirmMerge.merge(seeded, with: readyHi)
        check("pristine params ← манифест", m3p.params.bitrate == 256 && m3p.params.split)

        let preset = ParamsPreset(params: BookParams(bitrate: 96, channels: "mono",
                                                     samplerate: nil, split: false),
                                  bookIDs: ["book1"])
        check("pristine params ← пресет «ко всем», если он покрывает книгу",
              ConfirmMerge.merge(seeded, with: readyHi, preset: preset).params.bitrate == 96)
        let presetOther = ParamsPreset(params: preset.params, bookIDs: ["другая"])
        check("пресет чужой книги игнорируется",
              ConfirmMerge.merge(seeded, with: readyHi,
                                 preset: presetOther).params.bitrate == 256)

        let wild = d17Manifest(phase: "ready", token: "b", title: "T", author: "A",
                               params: BookParams(bitrate: 192, channels: "stereo",
                                                  samplerate: nil, split: true,
                                                  splitThresholdMB: 9000))
        check("порог нарезки зажат в 250…700 при посеве",
              ConfirmMerge.seed(manifest: wild).params.splitThresholdMB
                  == ConfirmMerge.thresholdRangeMB.upperBound)
        var wildDirty = seeded
        wildDirty.params.splitThresholdMB = 9000
        wildDirty.touched.insert(.params)
        check("правило 3: порог зажимается на КАЖДОМ merge, не только при посеве",
              ConfirmMerge.merge(wildDirty, with: ready).params.splitThresholdMB
                  == ConfirmMerge.thresholdRangeMB.upperBound)

        // Правило 2: выбор обложки не имеет права зависнуть.
        var pickedGen = ConfirmMerge.merge(seeded, with: ready)
        pickedGen.coverSelectedID = "gen-0"
        pickedGen.touched.insert(.cover)
        check("dirty cover переживает обновление манифеста",
              ConfirmMerge.merge(pickedGen, with: ready).coverSelectedID == "gen-0")
        let regenerated = d17Manifest(
            phase: "ready", token: "bt-2", title: "Война и мир", author: "Лев Толстой",
            options: [CoverOption(optID: "web-7", kind: "web", path: "/w.jpg", label: "w")],
            selected: "web-7")
        let m7 = ConfirmMerge.merge(pickedGen, with: regenerated)
        check("исчезнувший выбор обложки не зависает → дефолт агента",
              m7.coverSelectedID == "web-7")
        check("…и флаг .cover снят, поле снова живое", !m7.touched.contains(.cover))

        var pickedCustom = pickedGen
        pickedCustom.coverSelectedID = "custom-0"
        let m8 = ConfirmMerge.merge(pickedCustom, with: ready, extraCoverIDs: ["custom-0"])
        check("custom-обложка переживает манифест (её знает только клиент)",
              m8.coverSelectedID == "custom-0" && m8.touched.contains(.cover))
        check("…но без объявленного extraCoverIDs она считается протухшей",
              ConfirmMerge.merge(pickedCustom, with: ready).coverSelectedID == "emb-0")

        // Правило 1: смена source_rev при ТОЙ ЖЕ книге — обычная матрица.
        // Решение человека 2026-07-28: набранный им текст не должен исчезать из-за
        // фонового события (в папку доехал ещё один файл).
        var dirtyAll = seeded
        dirtyAll.title = "Моё"
        dirtyAll.author = "Мой"
        dirtyAll.touched = [.title, .author]
        let newRev = d17Manifest(phase: "skeleton", token: "", title: "Новое", author: "Новый",
                                 rev: "rev-ZZZZZZZZZZZZZZZZZZZZ")
        let m10 = ConfirmMerge.merge(dirtyAll, with: newRev)
        check("смена source_rev: правка человека ПЕРЕЖИВАЕТ (title)", m10.title == "Моё")
        check("смена source_rev: правка человека ПЕРЕЖИВАЕТ (author)", m10.author == "Мой")
        check("смена source_rev: флаги touched сохранены", m10.touched == [.title, .author])
        check("смена source_rev: черновик записал НОВУЮ ревизию",
              m10.sourceRev == newRev.sourceRev)
        var halfDirty = seeded
        halfDirty.title = "Моё"
        halfDirty.touched = [.title]
        let m10b = ConfirmMerge.merge(halfDirty, with: newRev)
        check("смена source_rev: pristine поле всё же обновилось из нового манифеста",
              m10b.author == "Новый" && m10b.title == "Моё")

        var dirtyCover = ConfirmMerge.merge(seeded, with: ready)
        dirtyCover.coverSelectedID = "gen-0"
        dirtyCover.touched.insert(.cover)
        let newRevReady = d17Manifest(
            phase: "ready", token: "bt-9", title: "Новое", author: "Новый",
            rev: "rev-ZZZZZZZZZZZZZZZZZZZZ",
            options: [CoverOption(optID: "emb-9", kind: "embedded", path: "/n.jpg", label: "n")],
            selected: "emb-9")
        let m10c = ConfirmMerge.merge(dirtyCover, with: newRevReady)
        check("смена source_rev: протухший выбор обложки НЕ зависает",
              m10c.coverSelectedID == "emb-9" && !m10c.touched.contains(.cover))
        let presetSameBook = ParamsPreset(params: BookParams(bitrate: 96, channels: "mono",
                                                             samplerate: nil, split: false),
                                          bookIDs: ["book1"])
        check("смена source_rev: пресет привязан к book_id и остаётся применим",
              ConfirmMerge.merge(seeded, with: newRevReady,
                                 preset: presetSameBook).params.bitrate == 96)
        var dirtyParams = seeded
        dirtyParams.params.bitrate = 64
        dirtyParams.touched.insert(.params)
        check("смена source_rev: правка параметров переживает и пресет её не перебивает",
              ConfirmMerge.merge(dirtyParams, with: newRevReady,
                                 preset: presetSameBook).params.bitrate == 64)

        // Единственный полный сброс — другая книга.
        let otherBook = d17Manifest(phase: "ready", token: "t", title: "Другая", author: "X",
                                    bid: "book2")
        let m11 = ConfirmMerge.merge(dirtyAll, with: otherBook)
        check("смена book_id → черновик пересеян целиком (единственный полный сброс)",
              m11.title == "Другая" && m11.bookID == "book2" && m11.touched == [])
        let a = ConfirmMerge.merge(seeded, with: ready)
        check("merge идемпотентен (повторное применение того же манифеста — no-op)",
              ConfirmMerge.merge(a, with: ready) == a)
    }

    /// РЁБРА ПОДЪЁМА ОКНА (I2). Ключ приложения обязан совпадать с ключом агента —
    /// иначе два канала отвечают на «это новое?» РАЗНЫМИ функциями и окно встаёт
    /// дважды: один раз от агентского нуджа по скелету, второй — от собственного
    /// rising-edge приложения, когда манифест перевернулся в ready.
    static func checkNudgeEdgeKeys() {
        let skel = d17Manifest(phase: "skeleton", token: "", title: "01 Файл", author: "Папка")
        let ready = d17Manifest(phase: "ready", token: "bt-1", title: "Война и мир",
                                author: "Лев Толстой")

        func edgeState(_ rows: [BookSummary], groups: [PendingGroup] = []) -> ShowcaseState {
            var s = ShowcaseState.empty
            s.books = rows
            s.pendingGroups = groups
            return s
        }
        let st = edgeState([BookSummary(bookID: "book1", title: "Война и мир",
                                        status: "pending-confirm")])

        let kSkeleton = NudgeEdge.keys(state: st) { _ in skel }
        let kReady = NudgeEdge.keys(state: st) { _ in ready }
        check("I2: скелет → ready даёт ТОТ ЖЕ ключ ребра (второго подъёма окна нет)",
              kSkeleton == kReady, "\(kSkeleton) vs \(kReady)")
        check("I2: …и он непустой (иначе равенство было бы пустым)", !kSkeleton.isEmpty)
        check("I2: после скелета новых рёбер не появляется",
              kReady.subtracting(kSkeleton).isEmpty)

        // reconvert чеканит свежий confirm_token ⇒ ЗАКОННО новое ребро.
        let reconverted = BookManifest(
            bookID: "book1", srcDir: "/src", status: "pending-confirm",
            sourceRev: skel.sourceRev, confirmToken: "tok-НОВЫЙ", title: "T", author: "A",
            chapters: [], totalDurationMS: 0, coverState: "none", coverPreview: nil,
            params: .defaults, phase: "ready", buildToken: "bt")
        check("«Собрать заново» (новый confirm_token) → новое ребро, один законный подъём",
              !NudgeEdge.keys(state: st) { _ in reconverted }.subtracting(kReady).isEmpty)

        check("книга в сборке ребром не считается",
              NudgeEdge.keys(state: edgeState([BookSummary(bookID: "book1", title: "T",
                                                           status: "converting")]))
                  { _ in ready }.isEmpty)
        check("нечитаемый манифест → ребро всё равно есть (иначе подъём на каждом обновлении)",
              NudgeEdge.keys(state: st) { _ in nil } == ["book:book1::"])

        let g = PendingGroup(groupID: "g1", rev: "r1", token: "t1", files: ["a.mp3"],
                             count: 1, totalDurationMS: 0)
        let kg = NudgeEdge.keys(state: edgeState([], groups: [g])) { _ in nil }
        check("группировочный запрос даёт своё ребро", kg == ["group:g1:r1:t1"])
        check("книжные и групповые рёбра различимы",
              NudgeEdge.containsGroup(kg) && !NudgeEdge.containsBook(kg))
    }

    // MARK: - Сторожа по исходнику (значением не проверяются)

    /// Питоновский исходник без комментариев и докстрингов.
    ///
    /// Нужен именно для зеркала ключей: докстринг `_book_edge_key` СОДЕРЖИТ
    /// образец `book:<id>:<rev[:16]>:<token[:16]>`, и наивный разбор насчитывает
    /// в теле четыре среза вместо двух. Разбирать надо код, а не заметку о коде.
    static func pyStripped(_ source: String) -> String {
        var out: [String] = []
        var inDoc = false
        for line in source.split(separator: "\n", omittingEmptySubsequences: false)
            .map(String.init) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            let quotes = occurrences(of: "\"\"\"", in: line)
            if inDoc {
                if quotes % 2 == 1 { inDoc = false }
                continue
            }
            if trimmed.hasPrefix("#") { continue }
            if quotes % 2 == 1 { inDoc = true; continue }
            if quotes >= 2 && trimmed.hasPrefix("\"\"\"") { continue }
            out.append(line)
        }
        return out.joined(separator: "\n")
    }

    /// Тело функции Python верхнего уровня: от `def <name>(` до следующего
    /// объявления нулевого отступа. Не парсер — и не претендует; промах якоря
    /// отдаётся пустой строкой, то есть красным у вызывающего.
    static func pyFunctionBody(_ decl: String, in code: String) -> String {
        guard let start = code.range(of: decl) else { return "" }
        let tail = code[start.lowerBound...]
        let stops = [tail.range(of: "\ndef "), tail.range(of: "\nclass ")].compactMap { $0 }
        guard let end = stops.min(by: { $0.lowerBound < $1.lowerBound }) else {
            return String(tail)
        }
        return String(tail[..<end.lowerBound])
    }

    /// Все длины срезов `[:N]` в порядке появления.
    static func pySliceLengths(in body: String) -> [Int] {
        var out: [Int] = []
        var rest = Substring(body)
        while let r = rest.range(of: "[:") {
            let after = rest[r.upperBound...]
            let digits = after.prefix(while: { $0.isNumber })
            if !digits.isEmpty, after.dropFirst(digits.count).first == "]",
               let n = Int(digits) {
                out.append(n)
            }
            rest = after
        }
        return out
    }

    /// Содержимое f-строки из `return f"…"`.
    static func pyReturnTemplate(in body: String) -> String {
        guard let r = body.range(of: "return f\"") else { return "" }
        let tail = body[r.upperBound...]
        guard let end = tail.firstIndex(of: "\"") else { return "" }
        return String(tail[..<end])
    }

    /// Пути вида `<root>.a.b`, которым в теле ПРИСВАИВАЮТ (чтения не считаются).
    ///
    /// Отличать чтение от записи обязательно: `draft.params.bitrate` встречается в
    /// хосте десяток раз как чтение, и запрет «слова draft.params» покраснел бы на
    /// исправном коде — сторож, который приходится ослаблять, чтобы он не мешал,
    /// перестаёт быть сторожем.
    static func assignedPaths(of root: String, in body: String) -> [String] {
        var found: [String] = []
        for part in body.components(separatedBy: root + ".").dropFirst() {
            let path = part.prefix(while: { $0.isLetter || $0.isNumber || $0 == "_" || $0 == "." })
            let rest = part.dropFirst(path.count).drop(while: { $0 == " " })
            if rest.first == "=" && rest.dropFirst().first != "=" {
                found.append(String(path))
            }
        }
        return found
    }

    /// Тело члена: однострочное вычисляемое свойство отдаётся СВОЕЙ строкой.
    ///
    /// `bodyOfFunction` ищет закрывающую `}` на отступе объявления, а у
    /// `var isBuildReady: Bool { !buildToken.isEmpty }` такой строки нет — он
    /// проглатывал следующий член вместе с его комментарием, и отрицательное
    /// утверждение краснело на исправном коде (поймано первым же прогоном).
    static func memberBody(_ decl: String, in code: String) -> String {
        let lines = code.split(separator: "\n", omittingEmptySubsequences: false).map(String.init)
        guard let i = lines.firstIndex(where: { $0.contains(decl) }) else { return "" }
        let line = lines[i]
        if line.contains("{") && line.trimmingCharacters(in: .whitespaces).hasSuffix("}") {
            return line
        }
        return bodyOfFunction(decl, in: code)
    }

    static func fill(_ template: String, _ values: [String: String]) -> String {
        var out = template
        for (k, v) in values { out = out.replacingOccurrences(of: "{\(k)}", with: v) }
        return out
    }

    /// ЗЕРКАЛО КЛЮЧЕЙ: формы `NudgeEdge` против ЖИВОГО `agent/scan.py`.
    ///
    /// До сегодняшнего дня побайтовое совпадение двух реализаций держалось
    /// КОММЕНТАРИЕМ («deliberate, byte-for-byte mirror»). На нём стоит инвариант I2:
    /// разъедутся молча — и вернётся второй подъём окна, причём ни одна проверка
    /// значением этого не заметит, потому что каждая сторона внутренне согласована.
    ///
    /// Поэтому ожидание не зашито здесь литералом, а ВЫВОДИТСЯ из питоновского
    /// исходника: шаблон берётся из `return f"…"`, длины отсечения — из срезов
    /// `[:N]` того же тела. Меняется питон — меняется ожидание, и Swift обязан
    /// поменяться следом (или покраснеть). Меняется Swift — краснеет сразу.
    static func checkNudgeEdgeMirror() {
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
        let scanPy = pyStripped(
            (try? String(contentsOf: repo.appendingPathComponent("agent/scan.py"),
                         encoding: .utf8)) ?? "")
        check("зеркало: agent/scan.py прочитан", !scanPy.isEmpty)

        let bookBody = pyFunctionBody("def _book_edge_key(", in: scanPy)
        let groupBody = pyFunctionBody("def _group_edge_key(", in: scanPy)
        check("зеркало: тела _book_edge_key / _group_edge_key найдены",
              !bookBody.isEmpty && !groupBody.isEmpty)

        let bookTpl = pyReturnTemplate(in: bookBody)
        let bookCuts = pySliceLengths(in: bookBody)
        check("зеркало: книжный шаблон разобран (3 подстановки + 2 отсечения)",
              bookTpl.contains("{bid}") && bookTpl.contains("{rev}")
                  && bookTpl.contains("{token}") && bookCuts.count == 2,
              "tpl=\(bookTpl) cuts=\(bookCuts)")
        if bookCuts.count == 2 {
            let rev = "0123456789abcdefXXXXXXXX"
            let token = "fedcba9876543210YYYYYYYY"
            let expected = fill(bookTpl, ["bid": "abc",
                                          "rev": String(rev.prefix(bookCuts[0])),
                                          "token": String(token.prefix(bookCuts[1]))])
            check("зеркало: в ожидании не осталось неподставленных мест",
                  !expected.contains("{") && !expected.contains("}"), expected)
            check("книжный ключ приложения = agent/scan.py::_book_edge_key (побайтово)",
                  NudgeEdge.bookKey(bookID: "abc", sourceRev: rev, confirmToken: token)
                      == expected,
                  "swift=\(NudgeEdge.bookKey(bookID: "abc", sourceRev: rev, confirmToken: token)) "
                      + "python=\(expected)")
            check("зеркало: отсечение действительно РЕЖЕТ (фикстура длиннее лимита)",
                  rev.count > bookCuts[0] && token.count > bookCuts[1])
        }

        let groupTpl = pyReturnTemplate(in: groupBody)
        let groupCuts = pySliceLengths(in: groupBody)
        check("зеркало: групповой шаблон разобран",
              groupTpl.contains("{gid}") && groupTpl.contains("{rev}")
                  && groupTpl.contains("{token}") && groupCuts.count == 2,
              "tpl=\(groupTpl) cuts=\(groupCuts)")
        if groupCuts.count == 2 {
            let rev = "rev0123456789abcdefZZ"
            let token = "tok0123456789abcdefZZ"
            let expected = fill(groupTpl, ["gid": "g1",
                                           "rev": String(rev.prefix(groupCuts[0])),
                                           "token": String(token.prefix(groupCuts[1]))])
            check("групповой ключ приложения = agent/scan.py::_group_edge_key (побайтово)",
                  NudgeEdge.groupKey(groupID: "g1", rev: rev, token: token) == expected,
                  "swift=\(NudgeEdge.groupKey(groupID: "g1", rev: rev, token: token)) "
                      + "python=\(expected)")
        }
    }

    /// ЧЕРНОВИК ПИШЕТСЯ ТОЛЬКО ЧЕРЕЗ `edit(...)` И ОДНО `.onChange`.
    ///
    /// Без этого сторожа любое `draft.title = …`, дописанное завтра мимо правила,
    /// пройдёт зелёным: все проверки `ConfirmMerge` выше останутся безупречными —
    /// чистая функция-то не изменится, её просто перестанут спрашивать. Ровно этот
    /// разрыв («правило идеально, а хост его обходит») и есть то, ради чего в этом
    /// файле вообще есть сторожа по исходнику.
    static func checkConfirmDraftWiring() {
        let appDir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        let raw = (try? String(contentsOf: appDir.appendingPathComponent("main.swift"),
                               encoding: .utf8)) ?? ""
        check("черновик: исходник хоста прочитан", !raw.isEmpty)
        let view = bodyOfFunction("private struct ConfirmView: View", in: strippedCode(raw))
        check("черновик: тело ConfirmView найдено (промах якоря = красное)", !view.isEmpty)

        check("черновик: посев идёт той же чистой функцией (ConfirmMerge.seed в init)",
              occurrences(of: "_draft = State(initialValue: ConfirmMerge.seed(", in: view) == 1)
        check("черновик: ЕДИНСТВЕННОЕ присваивание draft — результат ConfirmMerge.merge",
              occurrences(of: "draft = ConfirmMerge.merge(", in: view) == 1
                  && occurrences(of: "draft = ", in: view)
                      == occurrences(of: "draft = ConfirmMerge.merge(", in: view)
                          + occurrences(of: "_draft = State(", in: view),
              "всего присваиваний \(occurrences(of: "draft = ", in: view))")
        let onChange = slice(from: ".onChange(of: manifest)", upTo: "\n        .", in: view)
        check("черновик: слияние живёт РОВНО в одном .onChange(of: manifest)",
              occurrences(of: ".onChange(of: manifest)", in: view) == 1
                  && onChange.contains("draft = ConfirmMerge.merge(draft, with: newManifest"))
        check("черновик: правки полей идут через ОДНУ функцию edit(...) с флагом",
              occurrences(of: "private func edit<T: Equatable>(", in: view) == 1)
        let editBody = bodyOfFunction("private func edit<T: Equatable>(", in: strippedCode(raw))
        check("черновик: только edit(...) пишет по keyPath и ставит флаг touched",
              occurrences(of: "draft[keyPath: path] = new", in: editBody) == 1
                  && occurrences(of: "draft.touched.insert(", in: editBody) == 1
                  && occurrences(of: "draft[keyPath:", in: view)
                      == occurrences(of: "draft[keyPath:", in: editBody)
                  && occurrences(of: "draft.touched.insert(", in: view) == 1)
        check("черновик: edit(...) ставит флаг только на НАСТОЯЩЕЕ изменение",
              editBody.contains("guard draft[keyPath: path] != new else { return }"))

        // Ни одного присваивания ПОЛЮ черновика в обход правила — ни верхнему
        // (`draft.title = …`), ни вложенному (`draft.params.bitrate = …`). Это тот
        // самый способ, которым дрейф возвращается: чистая функция остаётся
        // безупречной, её просто перестают спрашивать.
        let stray = assignedPaths(of: "draft", in: view)
        check("черновик: ни одного присваивания полю в обход правила",
              stray.isEmpty, stray.isEmpty ? "" : "найдено: \(stray.joined(separator: ", "))")
        // Мета: детектор действительно ловит запись и не считает чтение записью —
        // иначе «ничего не найдено» означало бы сломанный парсер, а не чистый код.
        let fixture = [
            "draft.title = x",
            "let y = draft.params.bitrate",
            "if draft.author == z { }",
            "draft.touched.insert(.cover)",
            "draft.params.split = true",
        ].joined(separator: "\n")
        let detected = assignedPaths(of: "draft", in: fixture)
        check("черновик: детектор присваиваний проверен на фикстуре "
              + "(ловит запись, не считает записью чтение и вызов)",
              detected == ["title", "params.split"], "\(detected)")
    }

    /// ГРАНИЦА M-B: `cover_web` не смеет попасть в решение о сборке.
    ///
    /// Замерено: живая сеть против мёртвой = 0.022 с на активной кнопке. Веб-нога
    /// ушла с критического пути на стороне агента; если ярлык «ищем обложку»
    /// протянут в `buildDisabled` или в `ConfirmPreparation`, вся эта работа
    /// обнуляется — кнопка снова ждёт сеть, и ни одна проверка ЗНАЧЕНИЕМ этого не
    /// увидит: манифест-фикстура с `cover_web=pending` просто станет несобираемой
    /// «по делу». Поэтому — по исходнику, по ТЕЛАМ, а не по файлу.
    static func checkCoverWebNotAGate() {
        let appDir = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
        let host = strippedCode((try? String(contentsOf: appDir.appendingPathComponent("main.swift"),
                                             encoding: .utf8)) ?? "")
        let model = strippedCode((try? String(
            contentsOf: appDir.appendingPathComponent("StateModel.swift"),
            encoding: .utf8)) ?? "")
        check("граница: оба исходника прочитаны", !host.isEmpty && !model.isEmpty)

        let gate = memberBody("private var buildDisabled: Bool", in: host)
        let hint = memberBody("private var buildDisabledHint: String", in: host)
        let ready = memberBody("var isBuildReady: Bool", in: model)
        let prep = memberBody("static func forManifest(", in: model)
        let note = memberBody("private var coverWebNote: some View", in: host)
        check("граница: все четыре тела найдены (промах якоря = красное)",
              !gate.isEmpty && !hint.isEmpty && !ready.isEmpty && !prep.isEmpty)

        // Положительные якоря: тела действительно те, иначе отрицания ниже пусты.
        check("граница: гейт «Собрать» опирается на isBuildReady",
              gate.contains("manifest.isBuildReady"))
        check("граница: isBuildReady читает НАЛИЧИЕ build_token",
              ready.contains("buildToken.isEmpty"))
        check("граница: подготовка решает по build_token и фазе",
              prep.contains("buildToken") || prep.contains("isBuildReady")
                  || prep.contains("phaseValue"))

        for (label, body) in [("гейт «Собрать»", gate), ("подсказка гейта", hint),
                              ("isBuildReady", ready), ("ConfirmPreparation", prep)] {
            check("граница: \(label) слеп к cover_web",
                  !body.contains("coverWeb") && !body.contains("isCoverWebPending")
                      && !body.contains("cover_web"))
        }
        // …и ярлык при этом ЖИВ — иначе отрицания выше зеленели бы от того, что
        // поля просто нет в приложении.
        check("граница: ярлык «ищем обложку» существует и висит на isCoverWebPending",
              !note.isEmpty && note.contains("manifest.isCoverWebPending"))
        check("граница: isCoverWebPending спрашивают ровно в одном месте — в ярлыке",
              occurrences(of: "isCoverWebPending", in: host)
                  == occurrences(of: "isCoverWebPending", in: note),
              "в хосте \(occurrences(of: "isCoverWebPending", in: host)), "
                  + "в ярлыке \(occurrences(of: "isCoverWebPending", in: note))")
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
        // «Окно создано» ≠ «человек его видит»: the escalation ladder (.patches/006).
        checkWindowPresentation()

        // D17 «ранний нудж» (M-D, сторона приложения): фазы и гейт сборки, ярлык
        // cover_web, чистое слияние черновика, рёбра подъёма окна — и три сторожа
        // по исходнику на то, что чистые правила действительно СПРАШИВАЮТ.
        checkManifestPhaseGate()
        checkCoverWebIsALabel()
        checkConfirmMerge()
        checkNudgeEdgeKeys()
        checkNudgeEdgeMirror()
        checkConfirmDraftWiring()
        checkCoverWebNotAGate()

        // M5 — the install-truth gate and everything hanging off it.
        checkAccessGate()
        checkFolderAccessDecode()
        // M6 — the card that finally hangs off that gate.
        checkFolderAccessCard()
        checkManualFallback()
        checkFolderRecheck()
        checkLocalWatchFolder()
        checkWatchDirTruth()
        checkStartupPlan()
        checkVersionRule()
        checkDiskTruth()
        checkSingleFlight()

        let passed = results.filter { $0.ok }.count
        let total = results.count
        print("\n§app-routing self-check: \(passed)/\(total) checks passed")
        let failed = results.filter { !$0.ok }.map { $0.name }
        if !failed.isEmpty { print("  FAILED checks: " + failed.joined(separator: "; ")) }
        exit(passed == total ? 0 : 1)
    }
}
