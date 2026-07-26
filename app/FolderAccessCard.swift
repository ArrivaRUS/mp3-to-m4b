// FolderAccessCard — M6: the ONE surface that explains why the agent cannot read
// the watched folder, and gets the user out of it.
//
// WHAT T0 CHANGED (arch/plan-binrunner-mp3-v2-addendum.md §5, measured 2026-07-25)
// The onboarding this card was originally designed for — "copy the path, open
// System Settings, find Full Disk Access, press +, paste, flip the switch" — is
// NOT the main path any more. Both TCC rows for our helper were created with
// `auth_value=2, auth_reason=2`: plain USER CONSENT in a system dialog. macOS
// simply asks:
//
//     Приложение «mp3-to-m4b-agent» запрашивает доступ к файлам
//     в Вашей папке «Рабочий стол».          [Не разрешать]  [Разрешить]
//
// (docs/consent-dialog.png — the real dialog off the human's machine.) Full Disk
// Access was never granted and its preflight fails on every single run, including
// the successful ones. So the FDA instructions are a FALLBACK now, folded away in
// a disclosure, and the leading advice is «нажмите «Разрешить»» / «работайте в
// папке, где разрешение не нужно».
//
// WHY `denied` AND `blocked` MAY NEVER SHARE COPY (addendum §4.1)
//   · `blocked` = NO DECISION EXISTS. macOS is holding the syscall open because it
//     wants to ask a human, and a background LaunchAgent cannot show that dialog.
//     The remedy is to LOOK AT THE SCREEN and press «Разрешить».
//   · `denied`  = there IS a decision, and it is "no" (the user pressed «Не
//     разрешать», or plain chmod/ACL). The dialog will never appear again. The
//     remedy is a folder outside the protected zone — or, the long way round,
//     System Settings.
// Each remedy is not merely useless for the other state, it is actively
// misleading: telling a `denied` user to wait for a dialog sends them to wait
// forever, and telling a `blocked` user to go dig in System Settings makes them
// miss the dialog that is on screen right now.
//
// FOUR подачи, mirroring the neighbour's proven shape
// (../2026.06 fb2-to-epub/app/FolderAccessCard.swift):
//   • .blocker     — owns the Status content when there is no build history yet.
//   • .banner      — a warn card above the Status content when there IS history
//                    (the books are still worth seeing; conversion is stuck either way).
//   • .setupStep   — display-only note on the first-run Setup wizard: after the
//                    install macOS will ask once, press «Разрешить».
//   • .settingsRow — a compact warn row in Настройки that hands off to Status,
//                    where the live automat lives.
//
// The texts live in `FolderAccessCopy` as pure values, so the self-check
// (app/selfcheck_routing.swift) asserts the EXACT strings this file renders —
// not a parallel copy that can drift away from what ships.

import Foundation
import SwiftUI

// MARK: - The automat

/// What the access card is showing right now.
///
/// Shape note: every case carries the underlying `FolderAccess` problem, instead
/// of the flat `denied / checking / stillDenied / timeout` the neighbour uses. The
/// flat version was written when there was only ONE problem to be in; with four
/// (`denied` / `blocked` / `missing` / unknown) a phase that forgets its problem
/// starts lying — a «Проверим после сборки» card with no problem attached cannot
/// tell the user whether they are waiting on a dialog or on a settings trip.
enum FolderAccessCardState: Equatable {
    /// The resting state: whatever the agent last published, no recheck in flight.
    case problem(FolderAccess)
    /// «Проверить снова» is in flight — the command is dropped, we are waiting for
    /// `folder_access_ts` to move.
    case checking(FolderAccess)
    /// The recheck came back and the SAME class of problem is still there.
    case stillDenied(FolderAccess)
    /// The recheck could not run yet: the agent is mid-build, so the command is
    /// queued behind it (plan v2 M5f — an honest "later", never a timeout).
    case busy(FolderAccess)
    /// `folder_access_ts` never moved and nothing explains it — the agent is not
    /// answering at all.
    case timeout(FolderAccess)

    /// The two resting problems by name, for readability at call sites and in tests.
    static let denied = FolderAccessCardState.problem(.denied)
    static let awaitingConsent = FolderAccessCardState.problem(.blocked)

    /// The verdict this card is about, whatever phase it is in.
    var problem: FolderAccess {
        switch self {
        case .problem(let p), .checking(let p), .stillDenied(let p),
             .busy(let p), .timeout(let p):
            return p
        }
    }

    var isChecking: Bool { if case .checking = self { return true }; return false }

    /// THE card invariant, as a pure function: an access card exists ONLY for the
    /// two surfaces the fail-closed router hands out for access
    /// (`.folderAccess` / `.accessUnknown`). Every other surface — a repair in
    /// flight, an unprovable job, a healthy normal landing — yields nil, so the app
    /// cannot offer folder help for a job it cannot prove is running (plan v2 §6.3:
    /// the user would grant access to the new binary while the old one keeps doing
    /// the work, and nothing on screen would say so).
    ///
    /// `pinned` is the host's memory of a settled recheck. It survives only while it
    /// is about the SAME problem the agent is reporting now.
    static func forSurface(_ surface: StatusSurface,
                           pinned: FolderAccessCardState? = nil) -> FolderAccessCardState? {
        let problem: FolderAccess
        switch surface {
        case .folderAccess(let access): problem = access
        case .accessUnknown(let raw):   problem = .unknown(raw)
        case .agentRepair, .agentNotRunning, .normal: return nil
        }
        if let pinned = pinned, pinned.problem == problem { return pinned }
        return .problem(problem)
    }

    /// States the host PINS after a recheck settles. They are not derivable from
    /// the live state file, so the refresh cycle has to drop them explicitly once
    /// the live verdict recovers (`FolderRecheck.terminalRecheckDissolves`).
    var isPinned: Bool {
        switch self {
        case .stillDenied, .busy, .timeout: return true
        case .problem, .checking: return false
        }
    }

    /// True while every action on the card must be inert: a probe in flight, or a
    /// build in progress (moving the folder mid-build is forbidden — addendum §3.2
    /// rule 5 — and a second recheck would just queue behind the same build).
    var actionsAreInert: Bool {
        switch self {
        case .checking, .busy: return true
        case .problem, .stillDenied, .timeout: return false
        }
    }
}

// MARK: - What the buttons do

/// The actions the card can offer. Kept as values (not closures) so the copy, the
/// enablement rule and the self-check all talk about the same thing.
enum FolderAccessAction: String, Equatable {
    /// Drop `recheck-access` → a fresh probe → macOS raises the dialog again.
    /// Honest ONLY where TCC has no record yet, i.e. `blocked`.
    case showRequestAgain
    /// Drop `recheck-access` → a fresh probe. No dialog is promised.
    case recheck
    /// Re-point the agent at `~/mp3-to-m4b`, which is not a TCC zone at all.
    case moveOutOfProtectedZone
    /// NSOpenPanel → re-point the agent wherever the user says.
    case chooseFolder
    /// Go to the app's own Настройки screen.
    case openAppSettings
    /// Copy `ProgramArguments[0]` — fail-closed, inside the manual fallback only.
    case copyHelperPath
    /// Open «Файлы и папки» — where an EXISTING folder grant lives as a switch.
    case openFilesAndFolders
    /// Open «Полный доступ к диску» — the only panel with a «+», i.e. the only way
    /// to create access for a bundle-less path client from nothing.
    case openPrivacyPane

    var title: String {
        switch self {
        case .showRequestAgain:       return "Показать запрос ещё раз"
        case .recheck:                return "Проверить снова"
        case .moveOutOfProtectedZone: return "Выбрать папку вне защищённой зоны"
        case .chooseFolder:           return "Выбрать другую папку…"
        case .openAppSettings:        return "Открыть Настройки"
        case .copyHelperPath:         return "Скопировать путь агента"
        case .openFilesAndFolders:    return "Открыть «Файлы и папки»"
        case .openPrivacyPane:        return "Открыть «Полный доступ к диску»"
        }
    }

    var icon: String {
        switch self {
        case .showRequestAgain:       return "bell.badge"
        case .recheck:                return "arrow.clockwise"
        case .moveOutOfProtectedZone: return "house"
        case .chooseFolder:           return "folder"
        case .openAppSettings:        return "gearshape"
        case .copyHelperPath:         return "doc.on.doc"
        case .openFilesAndFolders:    return "folder.badge.gearshape"
        case .openPrivacyPane:        return "externaldrive.badge.person.crop"
        }
    }

    /// The System Settings anchor this action reveals, if any. Verified against the
    /// OS itself rather than documentation: these identifiers are the
    /// `revealElementKeyName` values inside
    /// `SecurityPrivacyExtension.appex/Contents/Resources/TCCServiceList.plist` and
    /// the extension binary's own string table on this machine (macOS 26).
    var settingsAnchor: String? {
        switch self {
        case .openFilesAndFolders: return "Privacy_FilesAndFolders"
        case .openPrivacyPane:     return "Privacy_AllFiles"
        default:                   return nil
        }
    }
}

// MARK: - Where a folder without a TCC problem lives

/// `~/mp3-to-m4b` — the repair target, and the pure "is this path protected" test.
///
/// The whole point (addendum §2): TCC guards a FIXED set of zones — Desktop,
/// Documents, Downloads (plus removable/network volumes). The HOME ROOT is not one
/// of them. So a folder at `~/mp3-to-m4b` does not have a cheaper access story, it
/// has NO access story: no dialog, no Full Disk Access, no wedged probe, no card.
enum LocalWatchFolder {
    /// The protected zones, exactly as the installer's `needs_fda` check sees them.
    static let protectedZones = ["Desktop", "Documents", "Downloads"]

    static let folderName = "mp3-to-m4b"

    /// The repair target for the CURRENT user.
    static var path: String { path(home: NSHomeDirectory()) }

    static func path(home: String) -> String {
        (home as NSString).appendingPathComponent(folderName)
    }

    /// True iff `path` sits inside a TCC-protected zone of `home`. Pure — the
    /// self-check drives it with a synthetic home, no real filesystem involved.
    static func isProtected(_ path: String, home: String) -> Bool {
        guard !home.isEmpty, !path.isEmpty else { return false }
        let dir = normalized(path)
        for zone in protectedZones {
            let prefix = normalized((home as NSString).appendingPathComponent(zone))
            if dir == prefix || dir.hasPrefix(prefix + "/") { return true }
        }
        return false
    }

    /// What macOS calls the protected zone `path` lives in, in the System Settings
    /// UI («Рабочий стол» / «Документы» / «Загрузки»), or nil outside them.
    ///
    /// Worth the extra function: in «Файлы и папки» each app is a row with ONE
    /// switch per folder, so naming the switch turns «найдите нужный переключатель»
    /// into «включите вот этот». The names are the OS's own — the same three the
    /// privacy extension lists under `Privacy_FilesAndFolders`.
    static func protectedZoneName(for path: String, home: String) -> String? {
        guard !home.isEmpty, !path.isEmpty else { return nil }
        let dir = normalized(path)
        let names = ["Desktop": "Рабочий стол",
                     "Documents": "Документы",
                     "Downloads": "Загрузки"]
        for zone in protectedZones {
            let prefix = normalized((home as NSString).appendingPathComponent(zone))
            if dir == prefix || dir.hasPrefix(prefix + "/") { return names[zone] }
        }
        return nil
    }

    /// Collapse `~` and drop a trailing slash — display and comparison share it so
    /// «Уже отслеживается» can never disagree with what the card shows.
    static func normalized(_ path: String) -> String {
        var p = (path as NSString).expandingTildeInPath
        while p.count > 1 && p.hasSuffix("/") { p.removeLast() }
        return p
    }

    /// `~/…` for display.
    static func tildeAbbreviated(_ path: String, home: String = NSHomeDirectory()) -> String {
        if !home.isEmpty, path == home { return "~" }
        if !home.isEmpty, path.hasPrefix(home + "/") {
            return "~/" + String(path.dropFirst(home.count + 1))
        }
        return path
    }
}

// MARK: - The recheck contract (pure)

/// «Проверить снова», as a pure decision over what the agent did or did not do.
///
/// The app waits for the TOKEN, never for a verdict: `folder_access_ts` moves on
/// EVERY probe, even when the verdict is unchanged (agent/scan.py
/// `publish_folder_access`). Waiting for the verdict to change would hang forever
/// on the most common outcome — "checked again, still denied".
enum FolderRecheck {

    /// THREE honest outcomes, because the two-outcome version (ok / failed) lies in
    /// the middle case: an agent that is busy building a book has not failed, it has
    /// not got round to us yet, and telling the user «агент не ответил» there sends
    /// them chasing a broken agent that is working perfectly (plan v2 M5f).
    enum Outcome: Equatable {
        /// The probe ran and the folder is readable → the card dissolves.
        case ok
        /// The probe ran and reported a problem (possibly a different one).
        case stillProblem(FolderAccess)
        /// The token did not move, and the agent is building — the command is
        /// queued behind that build and will run right after it.
        case busy
        /// The token did not move and nothing explains it.
        case probeFailed
    }

    /// How long we wait for the token before calling it a failure (plan v2 M5f).
    static let timeout: TimeInterval = 10
    /// How often the host re-reads state while waiting.
    static let pollInterval: TimeInterval = 0.25

    /// - Parameters:
    ///   - tokenBefore: `agent.folder_access_ts` read just before the command was dropped.
    ///   - tokenAfter: the token now.
    ///   - verdict: `agent.folder_access` now.
    ///   - agentIsBuilding: a book is `converting` right now.
    static func evaluate(tokenBefore: String?, tokenAfter: String?,
                         verdict: FolderAccess?, agentIsBuilding: Bool) -> Outcome {
        let moved = tokenAfter != nil && tokenAfter != tokenBefore
        guard moved else {
            // Not a failure yet: a building agent owes us nothing until it is done.
            return agentIsBuilding ? .busy : .probeFailed
        }
        guard let verdict = verdict else {
            // A token without a verdict means we learned nothing — do not dress
            // that up as either success or a specific problem.
            return .probeFailed
        }
        return verdict == .ok ? .ok : .stillProblem(verdict)
    }

    /// A PINNED card (stillDenied / busy / timeout) is not derivable from the live
    /// state, so it has to be dropped explicitly — otherwise a card that says
    /// «доступа пока нет» stays up after the user granted access and the agent is
    /// happily building. Also drops a pin whose problem no longer matches the live
    /// verdict: a `blocked` pin over a live `denied` would give the wrong remedy.
    ///
    /// `agentIsBuilding` is the third dissolver, and it is the one that keeps the
    /// card from becoming a dead end. `.busy` freezes every button on purpose — a
    /// second recheck would queue behind the same build, and re-pointing the folder
    /// mid-build would orphan a half-written `.m4b`. But its whole justification is
    /// «сейчас идёт сборка»: when the build ends and the verdict has not changed,
    /// nothing else would ever clear the pin, and the user is left staring at a card
    /// whose every control is dead (lesson 005 — a visible control that does nothing
    /// is worse than no control).
    static func terminalRecheckDissolves(pinned: FolderAccessCardState?,
                                         live: FolderAccess?,
                                         agentIsBuilding: Bool = false) -> Bool {
        guard let pinned = pinned, pinned.isPinned else { return false }
        if case .busy = pinned, !agentIsBuilding { return true }
        guard let live = live else { return false }
        return live == .ok || live != pinned.problem
    }
}

// MARK: - Copy (the exact strings that ship)

/// Every string the card renders, as pure values. The self-check asserts these,
/// so a wording change that breaks the `denied` / `blocked` distinction turns the
/// suite red instead of shipping.
enum FolderAccessCopy {

    /// The name macOS puts in the consent dialog. It is the helper's FILE NAME (a
    /// path-client has no bundle: `BUNDLE_ATTRIBUTION … attributed bundle: (null)`),
    /// which is why the card can quote the dialog verbatim — and why the helper
    /// must never be renamed. Sourced from the installer contract, not retyped.
    static let helperName = StateStore.helperName

    // MARK: titles

    static func title(_ state: FolderAccessCardState) -> String {
        switch state {
        case .problem(let p):     return restingTitle(p)
        case .checking:           return "Проверяю доступ…"
        case .stillDenied(let p): return repeatedTitle(p)
        case .busy:               return "Проверим сразу после сборки"
        case .timeout:            return "Агент не ответил"
        }
    }

    private static func restingTitle(_ problem: FolderAccess) -> String {
        switch problem {
        case .denied:  return "Доступ к папке запрещён"
        case .blocked: return "macOS ждёт вашего ответа"
        case .missing: return "Папка не найдена"
        case .unknown: return "Непонятный ответ агента"
        case .ok:      return "Доступ к папке есть"
        }
    }

    private static func repeatedTitle(_ problem: FolderAccess) -> String {
        switch problem {
        case .denied:  return "Доступа по-прежнему нет"
        case .blocked: return "Ответа всё ещё нет"
        case .missing: return "Папки всё ещё нет"
        case .unknown: return "Ответ агента всё так же непонятен"
        case .ok:      return "Доступ к папке есть"
        }
    }

    // MARK: bodies

    static func body(_ state: FolderAccessCardState) -> String {
        switch state {
        case .problem(let p):
            return restingBody(p)
        case .checking:
            return "Попросил фонового агента заглянуть в папку ещё раз. Обычно это пара секунд."
        case .stillDenied(let p):
            return repeatedBody(p)
        case .busy:
            return "Агент сейчас собирает книгу и не может отвлечься. Проверка доступа встала в очередь и выполнится сразу после текущей книги. Переносить папку во время сборки тоже нельзя — файлы книги в работе."
        case .timeout:
            return "За 10 секунд фоновый агент не отчитался о проверке — похоже, он не запущен. Попробуйте ещё раз, а если не поможет — откройте Настройки и обновите агента."
        }
    }

    private static func restingBody(_ problem: FolderAccess) -> String {
        switch problem {
        case .denied:
            return "Вы уже ответили «Не разрешать», и macOS это запомнила — спрашивать заново она больше не будет. Самый быстрый выход: работать в папке, для которой разрешение вообще не нужно."
        case .blocked:
            return "Прямо сейчас на экране должно быть окно: «Приложение «\(helperName)» запрашивает доступ к файлам…». Нажмите в нём «Разрешить» — и сборка продолжится сама."
        case .missing:
            return "Папки, которую слушает фоновый агент, больше нет: её переименовали, перенесли или удалили. Верните её на место или выберите другую."
        case .unknown(let raw):
            return "Фоновый агент сообщил про доступ значение «\(raw)», которого это приложение не знает. Скорее всего, агент новее приложения — обновите приложение. До тех пор статусу доступа доверять нельзя."
        case .ok:
            return "Папка читается, ничего делать не нужно."
        }
    }

    private static func repeatedBody(_ problem: FolderAccess) -> String {
        switch problem {
        case .denied:
            return "Агент проверил заново — папка закрыта. Разрешение либо не включили, либо включили не для того файла: система спрашивает про «\(helperName)», а не про приложение."
        case .blocked:
            return "Агент спросил заново — и система снова ждёт вашего ответа. Окно с вопросом должно быть на экране прямо сейчас: нажмите в нём «Разрешить»."
        case .missing:
            return "Агент проверил заново — папки по-прежнему нет на месте."
        case .unknown(let raw):
            return "Агент проверил заново и повторил значение «\(raw)». Это приложение его не знает."
        case .ok:
            return "Папка читается, ничего делать не нужно."
        }
    }

    // MARK: the extra line under the body

    /// The quiet third line: what to do when the dialog is nowhere to be seen, or —
    /// in the phases that talk about themselves rather than the problem — a recap
    /// of WHICH problem we are still stuck on.
    static func hint(_ state: FolderAccessCardState) -> String? {
        switch state {
        case .problem(.blocked), .stillDenied(.blocked):
            return "Не видите окно? Оно могло уйти за другие окна или на соседний рабочий стол — сверните окна и посмотрите ещё раз. Спрашивает не приложение, а фоновый агент «\(helperName)»: читает папку именно он."
        case .busy(let p), .timeout(let p):
            return "Пока ничего не изменилось: \(shortProblem(p))."
        default:
            return nil
        }
    }

    /// One lower-case clause naming the problem — for the recap line, the banner
    /// sub-line and the Настройки row.
    static func shortProblem(_ problem: FolderAccess) -> String {
        switch problem {
        case .denied:  return "доступ к папке запрещён"
        case .blocked: return "система ждёт вашего ответа в окне запроса"
        case .missing: return "папка не найдена"
        case .unknown: return "агент ответил значением, которого приложение не знает"
        case .ok:      return "доступ есть"
        }
    }

    // MARK: actions

    /// The FIRST button. In `denied` this is deliberately the folder move, not the
    /// System Settings trip: the move fixes the case in one click and without any
    /// system panel, while the FDA route is long, scary and — since T0 — rare
    /// (addendum §5.3, "рекомендую сделать его ПЕРВОЙ кнопкой в карточке denied").
    static func primary(_ state: FolderAccessCardState) -> FolderAccessAction {
        switch state.problem {
        case .blocked:
            return .showRequestAgain
        case .denied:
            return .moveOutOfProtectedZone
        case .missing:
            return .chooseFolder
        case .unknown, .ok:
            return .recheck
        }
    }

    static func secondary(_ state: FolderAccessCardState) -> FolderAccessAction? {
        switch state.problem {
        case .blocked:          return .chooseFolder
        case .denied:           return .recheck
        case .missing:          return .recheck
        case .unknown, .ok:     return .openAppSettings
        }
    }

    /// A timeout is about the AGENT, not about the folder — so it overrides the
    /// problem-shaped actions with the two that can actually help.
    static func actions(_ state: FolderAccessCardState)
        -> (primary: FolderAccessAction, secondary: FolderAccessAction?) {
        if case .timeout = state { return (.recheck, .openAppSettings) }
        return (primary(state), secondary(state))
    }

    // MARK: the folded-away Full Disk Access fallback

    /// The disclosure header, or nil when the fallback makes no sense (a missing
    /// folder and an unknown verdict are not access problems).
    static func disclosureTitle(_ state: FolderAccessCardState) -> String? {
        switch state.problem {
        case .blocked: return "Если запрос так и не появился"
        case .denied:  return "Включить доступ вручную в Системных настройках"
        case .missing, .unknown, .ok: return nil
        }
    }

    // TWO routes, not one — and the order matters more than the wording.
    //
    // The shipped version had a single route: «Полный доступ к диску» → «+» → paste
    // the path. It sent the human to the wrong panel, and the reason is worth writing
    // down because it was invisible from the code: our grant is NOT a Full Disk
    // Access grant. Read off the live TCC database, the row is
    // `kTCCServiceSystemPolicyDesktopFolder`, and `TCCServiceList.plist` inside
    // SecurityPrivacyExtension.appex maps only `kTCCServiceSystemPolicyAllFiles` to
    // the `Privacy_AllFiles` anchor we were opening. There is no Full Disk Access row
    // for our helper — there never was one, and there cannot be one until somebody
    // presses «+». So the user followed our instructions, looked for
    // `mp3-to-m4b-agent` in a list where it cannot appear, and concluded the app
    // lies. Exactly the failure this whole card exists to prevent.
    //
    // Both panels are real and they answer DIFFERENT questions:
    //   · «Файлы и папки» (`Privacy_FilesAndFolders`) — where an EXISTING folder
    //     grant lives, as a switch. This is the one-toggle repair, and it is the
    //     likely case: the grant was given once and later switched off (which is
    //     precisely how the human reproduced the denied card).
    //   · «Полный доступ к диску» (`Privacy_AllFiles`) — the only panel with a «+»,
    //     so the only way to create access from nothing for a path-client with no
    //     bundle. Needed when the row does not exist at all (после «Не разрешать»,
    //     or when the panel refuses to draw a bundle-less client — donor lesson 020B).
    // Cheap repair first, heavy one second.

    static let routeToggleTitle = "Если доступ когда-то выдавали — быстрее так"
    static let routeAddTitle = "Если строки «\(helperName)» там нет"

    /// `zone` is the human name of the protected folder the agent watches («Рабочий
    /// стол» / «Документы» / «Загрузки») — the switch is labelled with it, so naming
    /// it turns "find the right toggle" into "flip this toggle".
    static func routeToggleSteps(zone: String?) -> [String] {
        [
            "Откройте «Файлы и папки» кнопкой ниже.",
            zone.map { "Найдите «\(helperName)» и включите у него переключатель «\($0)»." }
                ?? "Найдите «\(helperName)» и включите у него переключатель нужной папки.",
        ]
    }

    static let routeAddSteps: [String] = [
        "Нажмите «Скопировать путь агента» — путь ляжет в буфер обмена.",
        "Откройте «Полный доступ к диску» кнопкой ниже.",
        "Нажмите «+» под списком, затем Cmd-Shift-G и вставьте путь.",
        "Включите переключатель у «\(helperName)» и введите пароль, если система попросит.",
    ]

    /// The neighbour's patch 020 in one sentence: the panel does not redraw fresh
    /// entries, so a user who trusts the list gives up on a grant that worked.
    static let fdaCaveat =
        "Список в Системных настройках иногда не показывает свежие записи — это известная особенность macOS. Верить нужно кнопке «Проверить снова», а не виду списка."

    /// Shown when the fail-closed copy refused: PA0 is not the helper right now, so
    /// the path in the clipboard would have granted access to the wrong binary.
    static let copyRefused =
        "Путь не скопирован: LaunchAgent сейчас указывает не на этот файл. Сначала почините агента — иначе доступ был бы выдан не тому."

    static let copyDone = "Путь скопирован ✓"

    /// `NSWorkspace.open` returned false for every candidate URL — the panel did not
    /// open. Silence here would be the same defect as the silent copy button, one
    /// level down: the user presses «Открыть…», nothing happens, and the button is
    /// indistinguishable from a dead one.
    static let paneOpenFailed =
        "Не удалось открыть Системные настройки. Откройте их вручную: Конфиденциальность и безопасность → Файлы и папки."
}

/// How a press ANSWERS — kept as pure values so the "a control must show that it
/// fired" rule is a testable contract and not a detail buried in a view body.
///
/// Reason it exists: the copy button worked, relabelled itself, and was still
/// reported as broken. The relabel was the whole acknowledgement — same grey pill,
/// same icon, same size — and it never reverted, so a second press changed nothing
/// whatsoever. Colour, icon and expiry are therefore part of the contract now.
enum FolderAccessAck {

    /// How long a SUCCESS receipt stays up. It must expire: a permanently green
    /// button is exactly as uninformative as a permanently grey one, because the
    /// next press produces no visible change.
    static let successLingers: TimeInterval = 2.5

    static func copyTitle(copied: Bool) -> String {
        copied ? FolderAccessCopy.copyDone : FolderAccessAction.copyHelperPath.title
    }

    static func copyIcon(copied: Bool) -> String {
        copied ? "checkmark.circle.fill" : FolderAccessAction.copyHelperPath.icon
    }

    /// A refusal outranks a receipt — they can never be shown at once, and «не
    /// скопировано» must never be dressed in the colour of success.
    static func copyTone(copied: Bool, refused: Bool) -> GhostPillButton.Tone {
        if refused { return .danger }
        return copied ? .success : .neutral
    }
}

extension FolderAccessCopy {

    // MARK: compact подачи

    /// The banner's one-line sub-title (the banner has no room for the full body).
    static func bannerSub(_ state: FolderAccessCardState) -> String {
        switch state {
        case .problem(.blocked), .stillDenied(.blocked):
            return "Нажмите «Разрешить» в системном окне — без этого агент не видит ваши mp3."
        case .problem(.denied), .stillDenied(.denied):
            return "Агенту закрыт доступ к папке — новые книги не появятся и сборка не пойдёт."
        case .problem(.missing), .stillDenied(.missing):
            return "Отслеживаемой папки нет на месте — верните её или выберите другую."
        case .checking:
            return "Проверяю доступ…"
        case .busy:
            return "Агент занят сборкой — проверим доступ сразу после неё."
        case .timeout:
            return "Фоновый агент не ответил на проверку доступа."
        default:
            return "Агент сообщил про доступ к папке значение, которого приложение не знает."
        }
    }

    /// The Настройки row: title + sub. It hands off to Status rather than repeating
    /// the automat, so its action label says where it goes.
    static func settingsRow(_ state: FolderAccessCardState) -> (title: String, sub: String) {
        (restingTitle(state.problem), shortProblem(state.problem))
    }

    static let settingsRowAction = "Исправить"

    // MARK: Setup wizard (display-only)

    static let setupStepCap = "ДОСТУП К ПАПКЕ"
    static let setupStepTitle = "macOS спросит один раз"
    /// Setup runs BEFORE the agent exists, so this step is a forecast, not a
    /// verdict — it is the single most valuable sentence in the whole flow
    /// (addendum §5.2: the main risk is «нажал Не разрешать, потому что не понял,
    /// кто спрашивает»).
    static let setupStepSub =
        "После установки появится окно «Приложение «\(helperName)» запрашивает доступ к файлам…» — нажмите «Разрешить»."
}

// MARK: - The card

struct FolderAccessCard: View {

    /// Which of the four surfaces to draw.
    enum Presentation { case blocker, banner, setupStep, settingsRow }

    let state: FolderAccessCardState
    /// `var`, not `let`: the host builds ONE configured card (state + actions) and
    /// each screen re-stamps the подача it needs. Building four cards with four
    /// copies of the same wiring is how the two drift apart.
    var presentation: Presentation = .blocker

    /// The same card in a different подача.
    func presented(as presentation: Presentation) -> FolderAccessCard {
        var copy = self
        copy.presentation = presentation
        return copy
    }

    /// The folder the agent is on (shown so the user can tell WHICH folder is meant).
    var watchDir: String? = nil

    /// Host-driven ack after a VERIFIED clipboard write (fail-closed copy).
    var pathCopied: Bool = false
    /// Host-driven honest hint after the fail-closed copy REFUSED.
    var copyRefused: Bool = false
    /// Host-driven: a System Settings panel would not open. Without this the
    /// «Открыть…» buttons are silent on failure — the same defect as the copy
    /// button, one level down.
    var paneOpenFailed: Bool = false

    /// The one entry point for every button. The host maps the action to work.
    var perform: (FolderAccessAction) -> Void = { _ in }
    /// «Исправить» in Настройки / the Setup step: hand off to the live card.
    var onHandOff: () -> Void = {}

    var body: some View {
        switch presentation {
        case .blocker:     blocker
        case .banner:      banner
        case .setupStep:   setupStep
        case .settingsRow: settingsRow
        }
    }

    // MARK: derived bits

    private var actions: (primary: FolderAccessAction, secondary: FolderAccessAction?) {
        FolderAccessCopy.actions(state)
    }
    private var enabled: Bool { !state.actionsAreInert }

    /// `blocked` is an INVITATION (the system is waiting for you), the rest are
    /// warnings — so the two do not share an accent either. The copy carries the
    /// distinction; the colour just stops the two from looking interchangeable.
    private var isInvitation: Bool {
        if case .blocked = state.problem { return true }
        return false
    }
    private var tint: Color { isInvitation ? Tokens.C.brandTeal : Tokens.C.warnBase }
    private var tintBg: Color { isInvitation ? Tokens.C.rowIcBrandTealBg : Tokens.C.warnTint10 }
    private var tintBorder: Color { isInvitation ? Tokens.C.stepCurBorder : Tokens.C.warnBorder30 }

    private var glyph: String {
        switch state {
        case .checking: return "arrow.clockwise"
        case .busy:     return "hourglass"
        case .timeout:  return "exclamationmark.triangle.fill"
        case .problem(let p), .stillDenied(let p):
            switch p {
            case .blocked: return "hand.raised.fill"
            case .denied:  return "lock.shield"
            case .missing: return "questionmark.folder"
            case .unknown: return "questionmark.circle"
            case .ok:      return "checkmark.circle"
            }
        }
    }

    // MARK: - Presentation: BLOCKER (owns the Status content)

    private var blocker: some View {
        VStack(spacing: 12) {
            ZStack {
                RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                    .fill(tintBg)
                Image(systemName: glyph)
                    .font(.system(size: 22, weight: .semibold))
                    .foregroundColor(tint)
            }
            .frame(width: 56, height: 56)

            Text(FolderAccessCopy.title(state))
                .font(.system(size: Tokens.F.h1Confirm, weight: .bold))
                .tracking(-0.2)
                .foregroundColor(Tokens.C.textHigh)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)

            Text(FolderAccessCopy.body(state))
                .font(.system(size: Tokens.F.emptyBody))
                .foregroundColor(Tokens.C.textSecondary)
                .multilineTextAlignment(.center)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)

            if let dir = watchDir, !dir.isEmpty {
                Text(LocalWatchFolder.tildeAbbreviated(dir))
                    .font(.system(size: Tokens.F.small, design: .monospaced))
                    .foregroundColor(Tokens.C.textTertiary)
                    .lineLimit(2)
                    .truncationMode(.middle)
                    .multilineTextAlignment(.center)
            }

            if state.isChecking { FolderAccessProgressBar() }

            if let hint = FolderAccessCopy.hint(state) {
                Text(hint)
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textTertiary)
                    .multilineTextAlignment(.center)
                    .lineSpacing(2)
                    .fixedSize(horizontal: false, vertical: true)
            }

            actionButtons
            disclosure
        }
        .padding(.init(top: 22, leading: 18, bottom: 20, trailing: 18))
        .frame(maxWidth: .infinity)
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCard)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(tintBorder, lineWidth: 1)
        )
        .padding(.horizontal, 14)
        .padding(.top, 4)
    }

    @ViewBuilder
    private var actionButtons: some View {
        VStack(spacing: 8) {
            PrimaryPillButton(title: primaryTitle, icon: primaryIcon,
                              enabled: enabled) { perform(actions.primary) }
            if let secondary = actions.secondary {
                GhostPillButton(title: secondary.title, icon: secondary.icon,
                                enabled: enabled) { perform(secondary) }
            }
        }
    }

    // The copy/pane actions never reach the primary slot (asserted in the
    // self-check), so the primary button has no acknowledgement branch to carry.
    private var primaryTitle: String { actions.primary.title }
    private var primaryIcon: String { actions.primary.icon }

    // MARK: - The folded-away FDA fallback (shared by blocker and banner)

    @ViewBuilder
    private var disclosure: some View {
        if let title = FolderAccessCopy.disclosureTitle(state) {
            FolderAccessDisclosure(
                title: title,
                toggleTitle: FolderAccessCopy.routeToggleTitle,
                toggleSteps: FolderAccessCopy.routeToggleSteps(zone: watchZoneName),
                addTitle: FolderAccessCopy.routeAddTitle,
                addSteps: FolderAccessCopy.routeAddSteps,
                caveat: FolderAccessCopy.fdaCaveat,
                copyTitle: FolderAccessAck.copyTitle(copied: pathCopied),
                copyDone: pathCopied,
                copyRefusedText: copyRefused ? FolderAccessCopy.copyRefused : nil,
                paneFailedText: paneOpenFailed ? FolderAccessCopy.paneOpenFailed : nil,
                onCopy: { perform(.copyHelperPath) },
                onOpenFilesAndFolders: { perform(.openFilesAndFolders) },
                onOpenPane: { perform(.openPrivacyPane) })
        }
    }

    /// The System-Settings switch name for the watched folder's zone, when it is in
    /// one. nil outside the protected zones — and then the step text falls back to a
    /// generic wording instead of naming a switch that will not be there.
    private var watchZoneName: String? {
        guard let dir = watchDir, !dir.isEmpty else { return nil }
        return LocalWatchFolder.protectedZoneName(for: dir, home: NSHomeDirectory())
    }

    // MARK: - Presentation: BANNER (Status keeps its content)

    private var banner: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(alignment: .top, spacing: 11) {
                ZStack {
                    RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                        .fill(tintBg)
                    Image(systemName: glyph)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(tint)
                }
                .frame(width: 28, height: 28)

                VStack(alignment: .leading, spacing: 2) {
                    Text(FolderAccessCopy.title(state))
                        .font(.system(size: Tokens.F.body, weight: .semibold))
                        .foregroundColor(Tokens.C.textHigh)
                        .fixedSize(horizontal: false, vertical: true)
                    Text(FolderAccessCopy.bannerSub(state))
                        .font(.system(size: Tokens.F.small))
                        .foregroundColor(Tokens.C.textSecondary)
                        .lineSpacing(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 0)
            }

            if state.isChecking {
                FolderAccessProgressBar().padding(.top, 12)
            } else {
                VStack(spacing: 8) {
                    actionButtons
                }
                .padding(.top, 12)
                disclosure.padding(.top, 8)
            }
        }
        .padding(.init(top: 13, leading: 14, bottom: 13, trailing: 14))
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .fill(Tokens.C.bgCard)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.card, style: .continuous)
                .stroke(tintBorder, lineWidth: 1)
        )
        .padding(.horizontal, 14)
        .padding(.top, 4)
    }

    // MARK: - Presentation: SETUP STEP (display-only forecast)

    /// The BODY of the wizard's access step. Setup runs before the agent exists, so
    /// there is no live verdict here and no live action — only the sentence that
    /// stops the user from pressing «Не разрешать» out of confusion.
    private var setupStep: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(FolderAccessCopy.setupStepCap)
                .font(.system(size: Tokens.F.cap, weight: .bold))
                .tracking(1.2)
                .foregroundColor(Tokens.C.textTertiary)
            Text(FolderAccessCopy.setupStepTitle)
                .font(.system(size: Tokens.F.body, weight: .semibold))
                .foregroundColor(Tokens.C.textHigh)
                .padding(.top, 4)
            Text(FolderAccessCopy.setupStepSub)
                .font(.system(size: Tokens.F.small))
                .foregroundColor(Tokens.C.textSecondary)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 3)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    // MARK: - Presentation: SETTINGS ROW (compact, hands off)

    private var settingsRow: some View {
        HStack(spacing: 11) {
            ZStack {
                RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                    .fill(tintBg)
                Image(systemName: glyph)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundColor(tint)
            }
            .frame(width: 28, height: 28)
            VStack(alignment: .leading, spacing: 1) {
                Text(FolderAccessCopy.settingsRow(state).title)
                    .font(.system(size: Tokens.F.body))
                    .foregroundColor(tint)
                Text(FolderAccessCopy.settingsRow(state).sub)
                    .font(.system(size: Tokens.F.small))
                    .foregroundColor(Tokens.C.textTertiary)
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            Button(action: onHandOff) {
                Text(FolderAccessCopy.settingsRowAction)
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
                    .foregroundColor(Tokens.C.textOnAccent)
                    .padding(.horizontal, 13)
                    .padding(.vertical, 7)
                    .background(
                        RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                            .fill(Tokens.Grad.brandButton)
                    )
            }
            .buttonStyle(.plain)
            .contentShape(Rectangle())
        }
    }
}

// MARK: - Shared controls (used by the card AND by the install/diagnostics screens)

/// Full-width primary action (brand gradient pill). Lives here rather than in
/// main.swift so the self-check can compile the card without the AppKit host.
struct PrimaryPillButton: View {
    let title: String
    let icon: String
    var enabled: Bool = true
    let action: () -> Void

    var body: some View {
        Button(action: { if enabled { action() } }) {
            HStack(spacing: 6) {
                Image(systemName: icon).font(.system(size: 12, weight: .bold))
                Text(title).font(.system(size: Tokens.F.caption, weight: .semibold))
                    .multilineTextAlignment(.center)
            }
            .foregroundColor(Tokens.C.textOnAccent)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 9)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.appIconConfirm, style: .continuous)
                    .fill(Tokens.Grad.brandButton)
            )
            .opacity(enabled ? 1 : 0.45)
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .disabled(!enabled)
    }
}

/// Full-width secondary action (quiet surface + contour).
///
/// `tone` exists because of a measured user report: the copy button DID work and
/// DID relabel itself, and the human still said «не копирует». A text swap on an
/// identical grey pill, inside a dense list of identical grey pills, is not
/// feedback — nothing flashes, nothing changes colour, and the eye is on the
/// clipboard, not on the button just left behind. A silent control is
/// indistinguishable from a dead one, which is the third time this project has paid
/// for that lesson. So an acknowledgement changes the button's COLOUR, not just its
/// words.
struct GhostPillButton: View {
    /// Neutral = the resting control. Success/danger are host-driven answers to the
    /// press that just happened.
    enum Tone { case neutral, success, danger }

    let title: String
    let icon: String
    var enabled: Bool = true
    var tone: Tone = .neutral
    let action: () -> Void

    var body: some View {
        Button(action: { if enabled { action() } }) {
            HStack(spacing: 6) {
                Image(systemName: icon).font(.system(size: 12, weight: .semibold))
                Text(title).font(.system(size: Tokens.F.caption, weight: .semibold))
                    .multilineTextAlignment(.center)
            }
            .foregroundColor(foreground)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 9)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.appIconConfirm, style: .continuous)
                    .fill(fill)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.appIconConfirm, style: .continuous)
                    .stroke(border, lineWidth: 1)
            )
            .opacity(enabled ? 1 : 0.45)
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .disabled(!enabled)
    }

    private var foreground: Color {
        switch tone {
        case .neutral: return Tokens.C.textHigh
        case .success: return Tokens.C.stepOkSub
        case .danger:  return Tokens.C.dangerText
        }
    }
    private var fill: Color {
        switch tone {
        case .neutral: return Tokens.C.surfaceControl
        case .success: return Tokens.C.stepOkBg
        case .danger:  return Tokens.C.dangerTint10
        }
    }
    private var border: Color {
        switch tone {
        case .neutral: return Tokens.C.borderControl
        case .success: return Tokens.C.stepOkBorder
        case .danger:  return Tokens.C.dangerBorder30
        }
    }
}

/// The 6px indeterminate bar shown while a recheck is in flight.
private struct FolderAccessProgressBar: View {
    @SwiftUI.State private var slide = false

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width
            ZStack(alignment: .leading) {
                RoundedRectangle(cornerRadius: 3, style: .continuous)
                    .fill(Tokens.C.progressTrack)
                RoundedRectangle(cornerRadius: 3, style: .continuous)
                    .fill(Tokens.Grad.barSolid)
                    .frame(width: 0.35 * w)
                    .offset(x: slide ? w * 0.65 : 0)
            }
        }
        .frame(height: 6)
        .onAppear {
            withAnimation(.easeInOut(duration: 1.1).repeatForever(autoreverses: true)) {
                slide = true
            }
        }
    }
}

/// «Если запрос так и не появился» / «Включить доступ вручную…» — the manual route,
/// collapsed by default. It is a FALLBACK (addendum §5.1), so it must not be the
/// first thing the eye lands on; it is still one click away because the cases it
/// covers («Не разрешать» already pressed, or the switch turned off later) have no
/// other in-place remedy.
///
/// TWO routes inside, cheap first — see `FolderAccessCopy` for why one route was
/// not merely incomplete but actively misleading.
private struct FolderAccessDisclosure: View {
    let title: String
    /// «Файлы и папки» — flip an existing switch. `zone` names it.
    let toggleTitle: String
    let toggleSteps: [String]
    /// «Полный доступ к диску» — «+» a bundle-less binary in from nothing.
    let addTitle: String
    let addSteps: [String]
    let caveat: String
    let copyTitle: String
    /// The copy succeeded a moment ago → the button goes green with a checkmark.
    let copyDone: Bool
    let copyRefusedText: String?
    /// A settings panel refused to open — shown under whichever button was pressed.
    let paneFailedText: String?
    let onCopy: () -> Void
    let onOpenFilesAndFolders: () -> Void
    let onOpenPane: () -> Void

    @SwiftUI.State private var expanded = false

    var body: some View {
        disclosure
            // The window height is AppKit's, the content height is SwiftUI's, and
            // nothing connects them on its own — so say it out loud. Without this the
            // block expands INSIDE a window that stays the same size and the last
            // line is cut mid-word while the screen below sits empty.
            .onChange(of: expanded) { _ in
                NotificationCenter.default.post(name: .mp3ContentHeightDidChange, object: nil)
            }
    }

    private var disclosure: some View {
        VStack(alignment: .leading, spacing: 9) {
            Button(action: { expanded.toggle() }) {
                HStack(spacing: 6) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 10, weight: .bold))
                    Text(title)
                        .font(.system(size: Tokens.F.caption, weight: .semibold))
                        .multilineTextAlignment(.leading)
                    Spacer(minLength: 0)
                }
                .foregroundColor(Tokens.C.textTertiary)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            if expanded {
                // ROUTE 1 — the switch already exists somewhere in «Файлы и папки».
                routeHeading(toggleTitle)
                steps(toggleSteps)
                GhostPillButton(title: FolderAccessAction.openFilesAndFolders.title,
                                icon: FolderAccessAction.openFilesAndFolders.icon,
                                action: onOpenFilesAndFolders)

                // ROUTE 2 — nothing to switch: add the binary by hand.
                routeHeading(addTitle).padding(.top, 4)
                steps(addSteps)
                GhostPillButton(title: copyTitle,
                                icon: FolderAccessAck.copyIcon(copied: copyDone),
                                tone: FolderAccessAck.copyTone(copied: copyDone,
                                                               refused: copyRefusedText != nil),
                                action: onCopy)
                if let refused = copyRefusedText { note(refused, Tokens.C.dangerText) }
                GhostPillButton(title: FolderAccessAction.openPrivacyPane.title,
                                icon: FolderAccessAction.openPrivacyPane.icon,
                                action: onOpenPane)

                if let failed = paneFailedText { note(failed, Tokens.C.dangerText) }
                note(caveat, Tokens.C.textTertiary).padding(.top, 2)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func routeHeading(_ text: String) -> some View {
        Text(text)
            .font(.system(size: Tokens.F.small, weight: .bold))
            .foregroundColor(Tokens.C.textSoft)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func steps(_ list: [String]) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            ForEach(Array(list.enumerated()), id: \.offset) { index, step in
                HStack(alignment: .top, spacing: 8) {
                    Text("\(index + 1)")
                        .font(.system(size: Tokens.F.small, weight: .bold))
                        .foregroundColor(Tokens.C.textTertiary)
                        .frame(width: 16, height: 16)
                        .background(Circle().fill(Tokens.C.surfaceControl))
                    Text(step)
                        .font(.system(size: Tokens.F.small))
                        .foregroundColor(Tokens.C.textSecondary)
                        .lineSpacing(2)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func note(_ text: String, _ color: Color) -> some View {
        Text(text)
            .font(.system(size: Tokens.F.small))
            .foregroundColor(color)
            .lineSpacing(2)
            .fixedSize(horizontal: false, vertical: true)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}
