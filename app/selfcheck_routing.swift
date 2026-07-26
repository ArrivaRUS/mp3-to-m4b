// §app-routing self-check — app-level rules that must never regress:
//   1. WHICH BOOK the confirm window presents (ShowcaseState.presentedBook);
//   2. that the window can never end up OFF SCREEN (WindowGeometry);
//   3. (M5) WHICH SURFACE owns the window — the fail-closed install-truth gate,
//      the single-flight over the installer, and the tolerant decode of
//      `folder_access`. See `checkInstallTruth` and friends at the bottom.
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
        check("FDA-шаги называют именно файл-агент, а не приложение",
              FolderAccessCopy.fdaSteps.contains { $0.contains(StateStore.helperName) })
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

        // M5 — the install-truth gate and everything hanging off it.
        checkAccessGate()
        checkFolderAccessDecode()
        // M6 — the card that finally hangs off that gate.
        checkFolderAccessCard()
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
