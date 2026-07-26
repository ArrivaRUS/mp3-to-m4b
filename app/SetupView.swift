// SetupView — the first-launch Setup screen (spec §01 / §6, mockup 01-setup.html).
//
// This is the ONLY GUI path that installs the background agent (the user does not
// like Terminal). It is shown when the agent is NOT yet installed (see
// AppDelegate.isAgentInstalled). The screen is pixel-mapped to
// design/mockups/01-setup.html (CSS = pixel truth): a 400-wide window —
//   header (40 app-icon r11 · title + sub · gear) → welcome (h2 + sub) →
//   wizard card { step 1 ДВИЖОК (ffmpeg) · hairline · step 2 ОТСЛЕЖИВАЕМАЯ ПАПКА }
//   → footnote (FDA hint) → footer (live dot + status + "Открыть папку") → credit.
//
// Two states the mockup defines, driven by the live ffmpeg probe:
//   · STATE A — ffmpeg NOT found: step 1 is `bad` (brew install ffmpeg + recheck),
//     step 2 is disabled, the install button is disabled ("Ожидание движка").
//   · STATE B — ffmpeg found: step 1 is `ok` ("ffmpeg <ver> найден"), step 2 is
//     active (folder field + "Сменить…"), the install button is enabled ("Установить").
//
// "Установить" runs the BUNDLED installer (Contents/Resources/installer.sh) via
// Process, passing the chosen watch folder as argv[1]. The installer owns all
// install logic (venv+Pillow, launchd, FDA guidance) and already accepts the watch
// dir (arg or WATCH_DIR env, default ~/Desktop/mp3-to-m4b). On success the screen
// shows a done state and the AppDelegate flips to Status (the agent is now live).
//
// Unsandboxed, no external deps: SwiftUI + AppKit + Foundation. macOS 11 target.

import AppKit
import CryptoKit
import SwiftUI

// MARK: - ffmpeg probe (Swift mirror of installer.sh detect_tool)

/// The result of probing for the ffmpeg/ffprobe engine. Mirrors installer.sh
/// `detect_tool` resolution order so the screen's verdict matches what the
/// installer will find: env override ($FFMPEG/$FFPROBE) → Homebrew dirs → PATH.
struct FFmpegProbe {
    let ffmpegPath: String?
    let ffprobePath: String?
    /// Short version string ("7.1") parsed from `ffmpeg -version`, when found.
    let version: String?

    /// The engine is usable only when BOTH ffmpeg and ffprobe resolve (the agent
    /// needs ffprobe to scan and ffmpeg to build — installer.sh requires both).
    var isFound: Bool { ffmpegPath != nil && ffprobePath != nil }

    /// Probe synchronously off the main thread (callers hop to a background queue).
    /// Pure read: it never installs or mutates anything.
    static func detect() -> FFmpegProbe {
        let ffmpeg = locate("ffmpeg", envVar: "FFMPEG")
        let ffprobe = locate("ffprobe", envVar: "FFPROBE")
        let ver = ffmpeg.flatMap { parseVersion(ffmpegPath: $0) }
        return FFmpegProbe(ffmpegPath: ffmpeg, ffprobePath: ffprobe, version: ver)
    }

    /// Resolve a tool path the same way installer.sh does:
    ///   1) an absolute, executable path in $FFMPEG / $FFPROBE;
    ///   2) the two Homebrew bin dirs (Apple Silicon, then Intel);
    ///   3) a PATH lookup via `/usr/bin/env`.
    private static func locate(_ name: String, envVar: String) -> String? {
        let fm = FileManager.default
        if let cand = ProcessInfo.processInfo.environment[envVar],
           !cand.isEmpty, fm.isExecutableFile(atPath: cand) {
            return cand
        }
        for cand in ["/opt/homebrew/bin/\(name)", "/usr/local/bin/\(name)"]
        where fm.isExecutableFile(atPath: cand) {
            return cand
        }
        // PATH lookup (`env <name>` prints nothing if not found). Bounded + safe.
        if let resolved = which(name), fm.isExecutableFile(atPath: resolved) {
            return resolved
        }
        return nil
    }

    /// `/usr/bin/which <name>` → first line, or nil. Uses an absolute tool path so
    /// it works under launchd's minimal PATH too.
    private static func which(_ name: String) -> String? {
        guard FileManager.default.isExecutableFile(atPath: "/usr/bin/which") else { return nil }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/which")
        p.arguments = [name]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = Pipe()
        do {
            try p.run()
            p.waitUntilExit()
        } catch {
            return nil
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        let out = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return out.isEmpty ? nil : out.components(separatedBy: "\n").first
    }

    /// Parse "ffmpeg version 7.1 Copyright …" → "7.1". Best-effort; nil on failure
    /// (the step still reads "ffmpeg найден" without a version).
    private static func parseVersion(ffmpegPath: String) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: ffmpegPath)
        p.arguments = ["-version"]
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = Pipe()
        do {
            try p.run()
            p.waitUntilExit()
        } catch {
            return nil
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let line = String(data: data, encoding: .utf8)?
            .components(separatedBy: "\n").first else { return nil }
        // "ffmpeg version 7.1 …" or "ffmpeg version n7.1 …" / "…-tessus" etc.
        let tokens = line.split(separator: " ")
        guard tokens.count >= 3, tokens[0] == "ffmpeg", tokens[1] == "version" else { return nil }
        // Trim a leading 'n' and keep the leading numeric.dotted portion.
        var raw = String(tokens[2])
        if raw.hasPrefix("n") { raw.removeFirst() }
        let allowed = CharacterSet(charactersIn: "0123456789.")
        let trimmed = String(raw.unicodeScalars.prefix { allowed.contains($0) })
        return trimmed.isEmpty ? nil : trimmed
    }
}

// MARK: - Install runner (launches the BUNDLED installer.sh)

/// Drives the bundled installer (Contents/Resources/installer.sh). The installer
/// owns ALL install logic; the app only invokes it with the chosen watch folder.
/// Phase of the install, surfaced by the Setup footer/button.
enum InstallPhase: Equatable {
    case idle
    case running
    case done
    case failed(String)   // human message (stderr tail)
}

/// Bridge between the UI phase and the coordinator's terminal-only `InstallOutcome`
/// (EngineClient+Status.swift). The coordinator stays Foundation-only — it must
/// compile in the Swift self-check without the view layer — so the mapping lives
/// here, next to the phase it maps.
extension InstallOutcome {
    init(_ phase: InstallPhase) {
        switch phase {
        case .done: self = .done
        case .failed(let msg): self = .failed(msg)
        // A finished run can only be done/failed; anything else means the runner
        // returned without a verdict, which is a failure, not a silent success.
        case .idle, .running: self = .failed("Установка не завершилась.")
        }
    }

    var phase: InstallPhase {
        switch self {
        case .done: return .done
        case .failed(let msg): return .failed(msg)
        }
    }
}

/// Locates + runs the bundled installer. Kept tiny: build argv, run, capture
/// stderr for a failure message. The agent (installed by the script) takes over
/// from there; this never writes the support tree itself.
enum InstallRunner {
    /// Absolute path to the bundled installer.sh, or nil if it isn't in the bundle
    /// (e.g. running the bare binary from a checkout without rebuilding the .app).
    static func bundledInstallerPath() -> String? {
        if let p = Bundle.main.path(forResource: "installer", ofType: "sh") { return p }
        // Fallback: Resources/installer.sh next to the executable's bundle.
        let res = Bundle.main.resourceURL?.appendingPathComponent("installer.sh")
        if let res = res, FileManager.default.isExecutableFile(atPath: res.path) {
            return res.path
        }
        return nil
    }

    /// Run installer.sh with the watch folder as argv[1]. Returns the phase result.
    /// Runs synchronously (callers dispatch to a background queue and hop back to
    /// main with the result). `extraEnv` lets tests inject the NO_LAUNCHCTL/NO_VENV
    /// + scratch-HOME escape hatches so a real run never touches the live system.
    static func run(installerPath: String, watchDir: String,
                    extraEnv: [String: String] = [:]) -> InstallPhase {
        runCapturing(installerPath: installerPath, watchDir: watchDir,
                     extraEnv: extraEnv).phase
    }

    /// The OFFLINE repair mode (`--repair-launchd-only`, plan v2 B2): verify the
    /// already-installed files, re-bake the plist, reload, verify the loaded PA0,
    /// write the receipt. No engine detection, no venv, no pip — nothing that can
    /// reach the network. That is what makes it safe to call SYNCHRONOUSLY before
    /// the first frame: the full installer's `pip install --upgrade pip` (no
    /// timeout) is exactly why this mode exists.
    ///
    /// `watchDir` may be empty — the installer then carries the folder over from
    /// the receipt/plist itself. We still pass ours when we know it, because the
    /// resolution order on our side (receipt → plist → same-generation state) is
    /// the stricter one.
    static func runRepair(installerPath: String, watchDir: String,
                          extraEnv: [String: String] = [:])
        -> (phase: InstallPhase, stderrTail: String) {
        runCapturing(installerPath: installerPath, watchDir: watchDir,
                     extraEnv: extraEnv, repairOnly: true)
    }

    /// The single implementation. Also returns the last few stderr lines so the
    /// `.failed` screen can show real diagnostics instead of one cryptic line
    /// (M12f: that screen used to be a dead end with nothing to act on).
    static func runCapturing(installerPath: String, watchDir: String,
                             extraEnv: [String: String] = [:],
                             repairOnly: Bool = false)
        -> (phase: InstallPhase, stderrTail: String) {
        guard FileManager.default.fileExists(atPath: installerPath) else {
            return (.failed("Установщик не найден в приложении."), "")
        }
        let p = Process()
        // Invoke via /bin/bash so an un-executable-bit copy still runs; the script
        // is also chmod 0755 in the bundle, but bash is the robust path.
        p.executableURL = URL(fileURLWithPath: "/bin/bash")
        // The installer ignores an empty positional argument on purpose, so an
        // unknown folder is passed as "" rather than omitted (argv shape stays
        // stable) — in repair mode it then carries the folder over itself.
        p.arguments = repairOnly
            ? [installerPath, "--repair-launchd-only", watchDir]
            : [installerPath, watchDir]
        var env = ProcessInfo.processInfo.environment
        for (k, v) in extraEnv { env[k] = v }
        p.environment = env

        let errPipe = Pipe()
        let outPipe = Pipe()
        p.standardError = errPipe
        p.standardOutput = outPipe

        do {
            try p.run()
        } catch {
            return (.failed("Не удалось запустить установщик: \(error.localizedDescription)"), "")
        }
        // Drain pipes before waiting so a chatty installer can't deadlock on a full
        // pipe buffer.
        let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
        _ = outPipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()

        let stderr = String(data: errData, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let tail = stderr.components(separatedBy: "\n")
            .filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
            .suffix(8).joined(separator: "\n")

        if p.terminationStatus == 0 { return (.done, tail) }
        // Surface the last meaningful stderr line (the installer prints a clear
        // reason on each failure path); fall back to a generic message.
        let lastLine = stderr.components(separatedBy: "\n")
            .reversed().first { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
        let msg = (lastLine?.isEmpty == false ? lastLine! : "Установка не удалась (код \(p.terminationStatus)).")
        return (.failed(msg), tail)
    }
}

// MARK: - Installer test env (the two-stage latch, mirrored on the Swift side)

/// Builds a COMPLETE `extraEnv` for an isolated installer run.
///
/// `installer.sh` used to honour `MP3TOM4B_NO_LAUNCHCTL` / `MP3TOM4B_NO_VENV` /
/// `MP3TOM4B_SUPPORT_DIR` on their own. It no longer does: after the neighbour's
/// lesson (a verify-override that rewrote the human's real LaunchAgent), every
/// hatch sits behind a two-stage latch — `MP3TOM4B_TEST_MODE=1` PLUS
/// `MP3TOM4B_TEST_ROOT=<existing dir>` that CONTAINS every redirected path. Half an
/// arming is worse than none: the redirecting variables are then REFUSED (the run
/// fails) and the work-skipping ones are ignored (the run touches the real system).
///
/// So the app never assembles that dictionary by hand. Anything setting one of the
/// three seams (`SetupView.installerExtraEnv`, `SettingsView.installerExtraEnv`,
/// `AppDelegate.agentInstallerExtraEnv`) goes through here, and the latch is armed
/// by construction.
enum InstallerTestEnv {
    /// `root` must EXIST and contain `supportDir` (and the LaunchAgents dir, when
    /// redirected). `label` keeps a throwaway job off the production label.
    static func latched(root: String, supportDir: String, label: String,
                        launchAgentsDir: String? = nil,
                        skipLaunchctl: Bool = true,
                        skipVenv: Bool = true) -> [String: String] {
        var env: [String: String] = [
            "MP3TOM4B_TEST_MODE": "1",
            "MP3TOM4B_TEST_ROOT": root,
            "MP3TOM4B_SUPPORT_DIR": supportDir,
            "MP3TOM4B_LABEL": label,
        ]
        if let dir = launchAgentsDir { env["MP3TOM4B_LAUNCHAGENTS_DIR"] = dir }
        if skipLaunchctl { env["MP3TOM4B_NO_LAUNCHCTL"] = "1" }
        if skipVenv { env["MP3TOM4B_NO_VENV"] = "1" }
        return env
    }
}

// MARK: - Agent freshness (is the STAGED agent behind the BUNDLED one?)

/// Whether the background agent staged under App Support is behind the agent the
/// .app SHIPS in its bundle. Swift mirror of `agent/agent_version.py` (the single
/// source of truth `agent.selfcheck_agent_update` proves) — the two MUST agree.
///
/// Why this exists: updating the .app does NOT re-stage the agent. A new UI over an
/// old engine (staged `bin/agent/*.py` stuck on old code) silently breaks fast mode /
/// progress / reconvert. The bundle carries the CURRENT agent at
/// `<App>.app/Contents/Resources/agent/` (the exact source `installer.sh` copies), so
/// "staged behind bundled" is decidable by comparing the two trees' `*.py` contents.
enum AgentFreshness: Equatable {
    case upToDate      // staged fingerprint == bundled fingerprint
    case outdated      // bundled readable AND (staged absent OR fingerprints differ)
    case undecidable   // bundled tree unreadable (dev run) → "don't touch"
}

/// Compares the bundled agent tree with the staged one. Mirrors agent_version.compare.
enum AgentUpdate {
    /// The agent the .app ships: `<App>.app/Contents/Resources/agent/`, the SAME dir
    /// the bundled installer resolves as its `find_agent_dir` source ($SELF_DIR/agent).
    /// nil if the bundle has no such dir (a bare-binary dev run without a rebuilt .app).
    static func bundledAgentDir() -> URL? {
        guard let res = Bundle.main.resourceURL else { return nil }
        let dir = res.appendingPathComponent("agent", isDirectory: true)
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: dir.path, isDirectory: &isDir),
              isDir.boolValue else { return nil }
        return dir
    }

    /// The staged (installed) agent: `<supportRoot>/bin/agent/`, exactly where
    /// installer.sh copies it (BIN_DIR/agent). Honors MP3TOM4B_SUPPORT_DIR via the
    /// store's `supportRoot`, so a scratch-tree test never sees the real agent.
    static func stagedAgentDir(store: StateStore) -> URL {
        store.supportRoot.appendingPathComponent("bin/agent", isDirectory: true)
    }

    /// Content fingerprint of an agent dir: `basename -> sha256(bytes)` over its
    /// direct `*.py` files (NOT recursive — the package is flat), or nil if the dir
    /// is absent. Matches agent_version.fingerprint (both sides ship `agent/*.py`
    /// verbatim), so identical trees produce identical maps.
    static func fingerprint(dir: URL) -> [String: String]? {
        let fm = FileManager.default
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: dir.path, isDirectory: &isDir), isDir.boolValue else {
            return nil
        }
        guard let entries = try? fm.contentsOfDirectory(
            at: dir, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles]
        ) else { return nil }
        var out: [String: String] = [:]
        for url in entries where url.pathExtension == "py" {
            guard let data = try? Data(contentsOf: url) else {
                // A file we can't read means we can't fingerprint faithfully → treat
                // the whole tree as unknown rather than silently under-counting.
                return nil
            }
            out[url.lastPathComponent] = sha256Hex(data)
        }
        return out
    }

    /// The freshness verdict (see AgentFreshness). Mirrors agent_version.compare:
    ///   · undecidable — bundled tree unreadable (can't judge → don't touch);
    ///   · outdated    — bundled readable AND (staged absent OR fingerprint differs);
    ///   · upToDate    — both readable and identical.
    static func freshness(store: StateStore) -> AgentFreshness {
        guard let bundled = bundledAgentDir(),
              let bundledFP = fingerprint(dir: bundled) else {
            return .undecidable
        }
        let staged = stagedAgentDir(store: store)
        guard let stagedFP = fingerprint(dir: staged) else {
            // Bundled reference exists but nothing (readable) is staged → outdated.
            return .outdated
        }
        return stagedFP == bundledFP ? .upToDate : .outdated
    }

    /// Streaming SHA-256 of raw bytes as a lowercase hex string (matches Python's
    /// hashlib.sha256(...).hexdigest()). Uses CryptoKit (macOS 10.15+; our target is 11).
    private static func sha256Hex(_ data: Data) -> String {
        let digest = SHA256.hash(data: data)
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    // MARK: Non-python artifacts (frozen helper + runner.sh) — plan v2 M5

    /// The frozen helper the .app ships: `Contents/Resources/mp3-to-m4b-agent`
    /// (build/helper-guard.sh `HELPER_BUNDLE_RELPATH`). nil on a bare-binary dev run.
    static func bundledHelperPath() -> URL? {
        bundledResource(StateStore.helperName)
    }

    /// `Contents/Resources/runner.sh` — the helper's sibling, freely mutable
    /// content under a frozen NAME. nil on a bare-binary dev run.
    static func bundledRunnerPath() -> URL? {
        bundledResource("runner.sh")
    }

    private static func bundledResource(_ name: String) -> URL? {
        guard let res = Bundle.main.resourceURL else { return nil }
        let url = res.appendingPathComponent(name)
        return FileManager.default.fileExists(atPath: url.path) ? url : nil
    }

    /// Freshness over the two NON-python artifacts the installer also stages.
    ///
    /// `AgentFreshness` above only compares `agent/*.py`, because that is the tree
    /// `agent/agent_version.py` mirrors. But an update also carries `runner.sh`
    /// (signal handling, the donor process shape) — a UI that thinks it is current
    /// while the staged runner is a release behind is the same class of bug the py
    /// comparison exists to kill. The helper is frozen and must therefore be
    /// byte-identical; a difference here is not "an update is available", it is
    /// "something is wrong", and either way the correct action is the same: run the
    /// installer, which refuses on a golden-SHA mismatch.
    ///
    /// `.undecidable` when the bundle carries neither artifact (dev run) → don't touch.
    static func artifactFreshness(store: StateStore) -> AgentFreshness {
        let pairs: [(URL?, String)] = [
            (bundledHelperPath(), store.installedHelperPath),
            (bundledRunnerPath(), store.installedRunnerPath),
        ]
        var decided = false
        for (bundled, installedPath) in pairs {
            guard let bundled = bundled,
                  let bundledData = try? Data(contentsOf: bundled) else { continue }
            decided = true
            guard let installedData = try? Data(
                contentsOf: URL(fileURLWithPath: installedPath)) else { return .outdated }
            if sha256Hex(bundledData) != sha256Hex(installedData) { return .outdated }
        }
        return decided ? .upToDate : .undecidable
    }

    /// The verdict the launch path acts on: the python tree AND the artifacts.
    /// `.outdated` wins over everything (something concrete is behind); otherwise
    /// `.upToDate` only when at least one half could actually be decided.
    static func combinedFreshness(store: StateStore) -> AgentFreshness {
        let py = freshness(store: store)
        let art = artifactFreshness(store: store)
        if py == .outdated || art == .outdated { return .outdated }
        if py == .upToDate || art == .upToDate { return .upToDate }
        return .undecidable
    }

    // MARK: bundled >= installed (M11f)

    /// This .app's version (`CFBundleShortVersionString`), or nil outside a bundle.
    static func bundledVersion() -> String? {
        let v = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
        return (v?.isEmpty == false) ? v : nil
    }

    /// TRUE when this .app is OLDER than the install already on disk (receipt's
    /// `engine_version`). An outdated-looking staged tree then means "you opened an
    /// old app", not "the agent needs updating" — and re-running this bundle's
    /// installer would DOWNGRADE the engine, which on Tahoe re-points PA0 back at
    /// runner.sh and kills folder access. Unknown on either side → false (no
    /// opinion), matching the installer's own guard.
    static func isDowngrade(store: StateStore) -> Bool {
        guard let bundled = bundledVersion(),
              let installed = store.loadReceipt()?.engineVersion,
              !installed.isEmpty else { return false }
        return !EngineVersion.atLeast(bundled, installed)
    }
}

// MARK: - SetupView (spec §01 / §6, mockup 01)

/// The first-launch Setup screen. Owns the live ffmpeg probe + the chosen watch
/// folder + the install phase. Calls `onInstalled` after a successful install so
/// the AppDelegate can flip the window to Status.
struct SetupView: View {
    /// Called once the install succeeds (the agent is now live) — the host flips
    /// to Status and starts the state watcher.
    let onInstalled: () -> Void
    /// Test seam: extra env for the installer process. Empty in production — a real
    /// user install touches the system. Build it with `InstallerTestEnv.latched`:
    /// the installer refuses a half-armed latch (see that type).
    var installerExtraEnv: [String: String] = [:]
    /// Show the "macOS is about to ask for folder access" notice and call the
    /// continuation when the user acknowledges it (or never, if they back out).
    /// The host owns the notice so the SAME screen appears for every path that can
    /// trigger the system dialog (first install, launch-time update, repair).
    /// Default = run immediately, which keeps a bare SetupView (previews, unit
    /// compiles) behaving exactly as before.
    var requestConsentNotice: (String, @escaping () -> Void) -> Void = { _, go in go() }

    @State private var probe: FFmpegProbe = FFmpegProbe(ffmpegPath: nil, ffprobePath: nil, version: nil)
    @State private var probing = true
    /// The watch folder (default ~/Desktop/mp3-to-m4b, spec §6 / installer default).
    @State private var watchDir: String = SetupView.defaultWatchDir
    /// Whether `watchDir` currently exists as a directory on disk. The step-2 status
    /// circle reflects THIS (real state), not an assumption — the default folder may
    /// not exist yet. Re-checked on path change and right after "Создать".
    @State private var watchDirExists: Bool = SetupView.directoryExists(at: SetupView.defaultWatchDir)
    @State private var phase: InstallPhase = .idle

    static var defaultWatchDir: String {
        (NSHomeDirectory() as NSString).appendingPathComponent("Desktop/mp3-to-m4b")
    }

    /// True iff `path` exists AND is a directory (a plain file at the path → false).
    /// Pure read: never creates or mutates anything. The single source of truth for
    /// the step-2 status circle and the "Создать" button's visibility.
    static func directoryExists(at path: String) -> Bool {
        var isDir: ObjCBool = false
        let exists = FileManager.default.fileExists(atPath: path, isDirectory: &isDir)
        return exists && isDir.boolValue
    }

    private var ffmpegFound: Bool { probe.isFound }

    var body: some View {
        VStack(spacing: 0) {
            header
            welcome
            wizardCard
            footnote
            footer
            credit
        }
        .frame(width: Tokens.M.windowStandard)   // 400 (spec §2)
        .onAppear {
            runProbe()
            recheckFolder()   // re-read disk on appear (folder may have changed externally)
        }
    }

    // MARK: Header — padding 18 18 14, 40 app-icon (r11) + title/sub + gear.

    private var header: some View {
        HStack(spacing: 12) {
            SetupAppIcon()
            VStack(alignment: .leading, spacing: 2) {
                Text("mp3-to-m4b")
                    .font(.system(size: 17, weight: .bold))
                    .tracking(-0.2)
                    .foregroundColor(Tokens.C.textHigh)
                Text("Сборка аудиокниг MP3 → M4B")
                    .font(.system(size: Tokens.F.caption))
                    .foregroundColor(Tokens.C.textSecondary)
            }
            Spacer(minLength: 8)
            // Gear (icon-btn 28 r8) — decorative parity with the mockup; it has no
            // dedicated action on the Setup screen yet, so it is omitted rather than
            // shown dead (spec §1: no dead controls). Intentionally left out.
        }
        .padding(.init(top: 18, leading: 18, bottom: 14, trailing: 18))
    }

    // MARK: Welcome — text-center, padding 14 28 4. Copy tracks the ffmpeg state.

    private var welcome: some View {
        VStack(spacing: 6) {
            Text(ffmpegFound ? "Готово к работе" : "Нужен движок ffmpeg")
                .font(.system(size: 19, weight: .bold))
                .tracking(-0.3)
                .foregroundColor(Tokens.C.textHigh)
            Text(ffmpegFound
                 ? "После «Завершить установку» начну следить за папкой ниже — кидайте в неё папку-сборник с mp3, и рядом появится .m4b с главами."
                 : "Сборку .m4b делает ffmpeg. Установите его одной командой и нажмите «Проверить снова».")
                .font(.system(size: Tokens.F.emptyBody))
                .foregroundColor(Tokens.C.textSecondary)
                .multilineTextAlignment(.center)
                .lineSpacing(3)
                .fixedSize(horizontal: false, vertical: true)
        }
        .frame(maxWidth: .infinity)
        .padding(.init(top: 14, leading: 28, bottom: 4, trailing: 28))
    }

    // MARK: Wizard card — .wizard .card: margin 18 14 12, radius 16, bg #11161d,
    // border .06; two steps split by a hairline (margin 0 16).

    private var wizardCard: some View {
        VStack(spacing: 0) {
            stepEngine
            SetupHairline(color: Tokens.C.borderHairline)
                .padding(.horizontal, 16)
            stepFolder
        }
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.window, style: .continuous)
                .fill(Tokens.C.bgCard)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.window, style: .continuous)
                .stroke(Tokens.C.borderCard, lineWidth: 1)
        )
        .padding(.init(top: 18, leading: 14, bottom: 12, trailing: 14))
    }

    // MARK: Step 1 — ДВИЖОК (ffmpeg). bad (not found) / ok (found). padding 15 16.

    private var stepEngine: some View {
        HStack(alignment: .top, spacing: 12) {
            if probing {
                stepNum(.current, text: "1")
            } else if ffmpegFound {
                stepNum(.ok, text: nil)
            } else {
                stepNum(.bad, text: nil)
            }
            VStack(alignment: .leading, spacing: 0) {
                cap("ДВИЖОК")
                Text(engineTitle)
                    .font(.system(size: Tokens.F.input, weight: .semibold))
                    .foregroundColor(Tokens.C.textHigh)
                    .padding(.top, 3)
                if probing {
                    Text("Проверяю наличие ffmpeg…")
                        .font(.system(size: Tokens.F.chDur))
                        .foregroundColor(Tokens.C.textSecondary)
                        .padding(.top, 2)
                } else if ffmpegFound {
                    Text("Готов к сборке")
                        .font(.system(size: Tokens.F.chDur))
                        .foregroundColor(Tokens.C.stepOkSub)
                        .padding(.top, 2)
                } else {
                    Text("Без него сборка недоступна")
                        .font(.system(size: Tokens.F.chDur))
                        .foregroundColor(Tokens.C.dangerStepSub)
                        .padding(.top, 2)
                    installBox
                    recheckButton
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.init(top: 15, leading: 16, bottom: 15, trailing: 16))
    }

    private var engineTitle: String {
        if probing { return "Проверка движка…" }
        if ffmpegFound {
            if let v = probe.version { return "ffmpeg \(v) найден" }
            return "ffmpeg найден"
        }
        return "ffmpeg не найден"
    }

    // install-box: brew command (mono) + "Копировать". margin-top 9, padding 10 12,
    // radius 10, bg #0a1018, border .07.
    private var installBox: some View {
        HStack(spacing: 8) {
            Text("brew install ffmpeg")
                .font(.system(size: 12, design: .monospaced))
                .foregroundColor(Tokens.C.textHigh)
                .lineLimit(1)
            Spacer(minLength: 8)
            Button(action: copyBrewCommand) {
                Text("Копировать")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundColor(Tokens.C.installBtnText)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 5)
                    .background(
                        RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous)
                            .fill(Tokens.C.surfaceControl)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: Tokens.R.small, style: .continuous)
                            .stroke(Tokens.C.borderControlStrong, lineWidth: 1)
                    )
            }
            .buttonStyle(.plain)
            .contentShape(Rectangle())
        }
        .padding(.init(top: 10, leading: 12, bottom: 10, trailing: 12))
        .background(
            RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                .fill(Tokens.C.bgInput)
        )
        .overlay(
            RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                .stroke(Tokens.C.borderFieldInput, lineWidth: 1)
        )
        .padding(.top, 9)
    }

    // recheck: margin-top 9, padding 9 13, radius 10, cyan tint .12 / border .40,
    // teal text + refresh glyph. Re-runs the ffmpeg probe.
    private var recheckButton: some View {
        Button(action: runProbe) {
            HStack(spacing: 7) {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 12, weight: .semibold))
                Text("Проверить снова")
                    .font(.system(size: Tokens.F.emptyBody, weight: .semibold))
            }
            .foregroundColor(Tokens.C.stepOkSub)
            .padding(.init(top: 9, leading: 13, bottom: 9, trailing: 13))
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .fill(Tokens.C.recheckBg)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .stroke(Tokens.C.recheckBorder, lineWidth: 1)
            )
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .padding(.top, 9)
    }

    // MARK: Step 2 — ОТСЛЕЖИВАЕМАЯ ПАПКА. disabled until ffmpeg found. padding 15 16.

    private var stepFolder: some View {
        HStack(alignment: .top, spacing: 12) {
            if !ffmpegFound {
                stepNum(.disabled, text: "2")
            } else if watchDirExists {
                stepNum(.ok, text: nil)               // folder is real → honest ✓
            } else {
                stepNum(.current, text: "2")          // folder not yet → neutral, not a fake ✓
            }
            VStack(alignment: .leading, spacing: 0) {
                cap("ОТСЛЕЖИВАЕМАЯ ПАПКА")
                if !ffmpegFound {
                    Text("Доступно после установки движка")
                        .font(.system(size: Tokens.F.input, weight: .semibold))
                        .foregroundColor(Tokens.C.textHigh)
                        .padding(.top, 3)
                    folderField
                        .padding(.top, 9)
                        .disabled(true)
                } else {
                    // ffmpeg found → folder field is live. Status line tracks reality:
                    // folder exists → "Папка готова"; missing → honest "Папка не создана".
                    Text(watchDirExists ? "Папка готова" : "Папка не создана")
                        .font(.system(size: Tokens.F.chDur))
                        .foregroundColor(watchDirExists ? Tokens.C.stepOkSub : Tokens.C.stepCurText)
                        .padding(.top, 3)
                    folderField
                        .padding(.top, 9)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(.init(top: 15, leading: 16, bottom: 15, trailing: 16))
        .opacity(ffmpegFound ? 1 : 0.42)   // .step-disabled
    }

    // field: field-input (folder glyph + path, mono) + "Сменить…" (field-btn).
    private var folderField: some View {
        HStack(spacing: 8) {
            HStack(spacing: 7) {
                Image(systemName: "folder")
                    .font(.system(size: 13, weight: .regular))
                    .foregroundColor(ffmpegFound ? Tokens.C.textSecondary : Tokens.C.textQuaternary)
                Text(displayWatchDir)
                    .font(.system(size: 11.5, design: .monospaced))
                    .foregroundColor(ffmpegFound ? Tokens.C.textHigh : Tokens.C.textSecondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            .padding(.init(top: 9, leading: 11, bottom: 9, trailing: 11))
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .fill(Tokens.C.bgInput)
            )
            .overlay(
                RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                    .stroke(Tokens.C.borderFieldInput, lineWidth: 1)
            )

            // "Создать" — shown only when the folder is missing (and ffmpeg is found).
            // Accent (brand gradient) so it reads as the recommended next action; on
            // tap it creates the folder at the shown path, re-checks, and disappears.
            if ffmpegFound && !watchDirExists {
                Button(action: createWatchFolder) {
                    Text("Создать")
                        .font(.system(size: Tokens.F.caption, weight: .semibold))
                        .foregroundColor(Tokens.C.textOnAccent)
                        .padding(.init(top: 9, leading: 13, bottom: 9, trailing: 13))
                        .background(
                            RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                                .fill(Tokens.Grad.brandButton)
                        )
                }
                .buttonStyle(.plain)
                .contentShape(Rectangle())
            }

            Button(action: chooseFolder) {
                Text("Сменить…")
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
                    .foregroundColor(Tokens.C.textHigh)
                    .padding(.init(top: 9, leading: 13, bottom: 9, trailing: 13))
                    .background(
                        RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                            .fill(Tokens.C.surfaceControl)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: Tokens.R.control, style: .continuous)
                            .stroke(Tokens.C.borderControlStrong, lineWidth: 1)
                    )
            }
            .buttonStyle(.plain)
            .contentShape(Rectangle())
            .disabled(!ffmpegFound)
        }
    }

    /// Tilde-collapsed path for display (mockup shows "~/Desktop/mp3-to-m4b").
    private var displayWatchDir: String {
        let home = NSHomeDirectory()
        if watchDir == home { return "~" }
        if watchDir.hasPrefix(home + "/") {
            return "~/" + String(watchDir.dropFirst(home.count + 1))
        }
        return watchDir
    }

    // MARK: Footnote — the access forecast (shown when ffmpeg found; mockup STATE B).
    //
    // REWRITTEN after T0 (addendum §5.1–5.2). The old line said «может понадобиться
    // разовый Full Disk Access» — measurably wrong, and wrong in the expensive
    // direction: the grant our helper actually receives is a plain consent dialog
    // (`auth_reason=2`), while its Full Disk Access preflight is refused on every
    // run, including the successful ones. The FDA panel is now the fallback for
    // «Не разрешать», not the first step.
    //
    // This is also the single highest-value sentence in the whole newcomer path.
    // The dialog is raised by the AGENT, seconds after «Установить», and it names
    // the helper file, not this app. A user who does not know that is one click
    // away from «Не разрешать» — and that answer is final: macOS records it and
    // never asks again. So the forecast is shown BEFORE the install, in the card's
    // own words (FolderAccessCard.setupStep), and repeats verbatim what the system
    // dialog will say.
    //
    // Shown only for a folder inside a protected zone: in `~/mp3-to-m4b` nothing
    // will ask, and promising a dialog that never comes is its own kind of lying.

    @ViewBuilder
    private var footnote: some View {
        if ffmpegFound && selectedIsInProtectedZone {
            HStack(alignment: .top, spacing: 10) {
                ZStack {
                    RoundedRectangle(cornerRadius: Tokens.R.chip, style: .continuous)
                        .fill(Tokens.C.rowIcBrandTealBg)
                    Image(systemName: "hand.raised.fill")
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundColor(Tokens.C.brandTeal)
                }
                .frame(width: 28, height: 28)
                FolderAccessCard(state: .awaitingConsent, presentation: .setupStep)
                Spacer(minLength: 0)
            }
            .padding(.init(top: 4, leading: 20, bottom: 16, trailing: 20))
        }
    }

    /// The folder chosen on this screen sits inside a TCC-protected zone (Рабочий
    /// стол / Документы / Загрузки), so the consent dialog WILL appear after the
    /// install. Same pure rule the access card and Настройки use.
    private var selectedIsInProtectedZone: Bool {
        LocalWatchFolder.isProtected(watchDir, home: NSHomeDirectory())
    }

    // MARK: Footer — live dot + status text + the install action button.
    // padding 13 16, top border .06, bg rgba(7,11,16,.5).

    private var footer: some View {
        HStack(spacing: 10) {
            Circle()
                .fill(ffmpegFound ? Tokens.C.brandCyan : Tokens.C.textQuaternary)
                .frame(width: 7, height: 7)
                .shadow(color: ffmpegFound ? Tokens.C.brandCyan.opacity(0.7) : .clear, radius: 7)
            Text(footerStatus)
                .font(.system(size: Tokens.F.caption))
                .foregroundColor(Tokens.C.textSecondary)
            Spacer(minLength: 8)
            installButton
        }
        .padding(.init(top: 13, leading: 16, bottom: 13, trailing: 16))
        .background(Tokens.C.surfaceFooter)
    }

    private var footerStatus: String {
        switch phase {
        case .running: return "Устанавливаю…"
        case .done: return "Готово — агент запущен"
        case .failed: return "Установка не удалась"
        case .idle:
            if probing { return "Проверка движка" }
            return ffmpegFound ? "Готово к установке" : "Ожидание движка"
        }
    }

    // The single primary action: disabled (dim) until ffmpeg found; runs the
    // bundled installer; shows a spinner while running; surfaces a failure message
    // inline. After success the host navigates away (onInstalled).
    @ViewBuilder
    private var installButton: some View {
        switch phase {
        case .running:
            HStack(spacing: 7) {
                ProgressView()
                    .controlSize(.small)
                    .progressViewStyle(.circular)
                Text("Установка…")
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
                    .foregroundColor(Tokens.C.textSoft)
            }
            .padding(.init(top: 7, leading: 13, bottom: 7, trailing: 13))
            .background(
                RoundedRectangle(cornerRadius: Tokens.R.appIconConfirm, style: .continuous)
                    .fill(Tokens.C.surfaceControl)
            )
        case .done:
            primaryLabel(icon: "checkmark", title: "Готово", enabled: true,
                         gradient: true, action: onInstalled)
        case .failed(let msg):
            VStack(alignment: .trailing, spacing: 4) {
                primaryLabel(icon: "arrow.clockwise", title: "Повторить",
                             enabled: ffmpegFound, gradient: true, action: startInstall)
            }
            .help(msg)
        case .idle:
            primaryLabel(icon: "folder", title: "Завершить установку",
                         enabled: ffmpegFound, gradient: ffmpegFound, action: startInstall)
        }
    }

    /// One pill-shaped action label (.btn — radius 9, padding 7 13). Enabled →
    /// brand gradient; disabled → dim flat fill (mockup .btn[disabled] opacity .4).
    private func primaryLabel(icon: String, title: String, enabled: Bool,
                              gradient: Bool, action: @escaping () -> Void) -> some View {
        Button(action: { if enabled { action() } }) {
            HStack(spacing: 6) {
                Image(systemName: icon)
                    .font(.system(size: 12, weight: .bold))
                Text(title)
                    .font(.system(size: Tokens.F.caption, weight: .semibold))
            }
            .foregroundColor(enabled && gradient ? Tokens.C.textOnAccent
                             : (enabled ? Tokens.C.textHigh : Tokens.C.textSecondary))
            .padding(.init(top: 7, leading: 13, bottom: 7, trailing: 13))
            .background(installButtonBackground(enabled: enabled, gradient: gradient))
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .disabled(!enabled)
    }

    // Concrete-fill branches (macOS-11-safe: no AnyShapeStyle).
    @ViewBuilder
    private func installButtonBackground(enabled: Bool, gradient: Bool) -> some View {
        if enabled && gradient {
            RoundedRectangle(cornerRadius: Tokens.R.appIconConfirm, style: .continuous)
                .fill(Tokens.Grad.brandButton)
        } else if enabled {
            RoundedRectangle(cornerRadius: Tokens.R.appIconConfirm, style: .continuous)
                .fill(Tokens.C.surfaceControl)
        } else {
            RoundedRectangle(cornerRadius: Tokens.R.appIconConfirm, style: .continuous)
                .fill(Color.white(0.05))
        }
    }

    // MARK: Credit — centered, 11px, GitHub link.

    private var credit: some View {
        HStack(spacing: 0) {
            Text("mp3-to-m4b \(Tokens.appVersion) · by Alex Kovalev · ")
                .font(.system(size: Tokens.F.small))
                .tracking(0.1)
                .foregroundColor(Tokens.C.textQuaternary)
            Text("GitHub")
                .font(.system(size: Tokens.F.small, weight: .semibold))
                .tracking(0.1)
                .foregroundColor(Tokens.C.linkBlue)
        }
        .frame(maxWidth: .infinity)
        .padding(.init(top: 9, leading: 16, bottom: 13, trailing: 16))
    }

    // MARK: step-num circle (26, radius 50%) — ok ✓ / bad ✕ / cur N / disabled N.

    private enum StepState { case ok, bad, current, disabled }

    @ViewBuilder
    private func stepNum(_ state: StepState, text: String?) -> some View {
        ZStack {
            Circle().fill(stepBg(state))
            Circle().stroke(stepBorder(state), lineWidth: 1)
            switch state {
            case .ok:
                Image(systemName: "checkmark")
                    .font(.system(size: 12, weight: .heavy))
                    .foregroundColor(Tokens.C.stepOkSub)
            case .bad:
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .heavy))
                    .foregroundColor(Tokens.C.dangerBase)
            case .current:
                Text(text ?? "")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundColor(Tokens.C.stepCurText)
            case .disabled:
                Text(text ?? "")
                    .font(.system(size: 12, weight: .bold))
                    .foregroundColor(Tokens.C.textSecondary)
            }
        }
        .frame(width: 26, height: 26)
    }

    private func stepBg(_ s: StepState) -> Color {
        switch s {
        case .ok: return Tokens.C.stepOkBg
        case .bad: return Tokens.C.dangerTint13
        case .current: return Tokens.C.stepCurBg
        case .disabled: return Color.white(0.05)
        }
    }
    private func stepBorder(_ s: StepState) -> Color {
        switch s {
        case .ok: return Tokens.C.stepOkBorder
        case .bad: return Tokens.C.dangerBorder40
        case .current: return Tokens.C.stepCurBorder
        case .disabled: return Color.white(0.10)
        }
    }

    private func cap(_ text: String) -> some View {
        Text(text)
            .font(.system(size: Tokens.F.cap, weight: .bold))
            .tracking(1.2)
            .foregroundColor(Tokens.C.textTertiary)
    }

    // MARK: Actions

    /// Probe ffmpeg off the main thread; update the UI on completion. Used at
    /// .onAppear and by "Проверить снова".
    private func runProbe() {
        probing = true
        DispatchQueue.global(qos: .userInitiated).async {
            let result = FFmpegProbe.detect()
            DispatchQueue.main.async {
                self.probe = result
                self.probing = false
            }
        }
    }

    /// NSOpenPanel to choose the watch folder. Defaults the panel to the current
    /// choice's parent so re-picking is quick. Directories only.
    private func chooseFolder() {
        let panel = NSOpenPanel()
        panel.title = "Выберите отслеживаемую папку"
        panel.prompt = "Выбрать"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.directoryURL = URL(fileURLWithPath: watchDir).deletingLastPathComponent()
        guard panel.runModal() == .OK, let url = panel.url else { return }
        watchDir = url.path
        recheckFolder()   // new path → re-read disk so the status circle stays honest
    }

    /// Re-read whether `watchDir` exists as a directory and update the status state.
    /// Called after a path change (Сменить…) and after Создать.
    private func recheckFolder() {
        watchDirExists = SetupView.directoryExists(at: watchDir)
    }

    /// Create the watch folder at the shown path (default ~/Desktop/mp3-to-m4b),
    /// making intermediate dirs. Shown only when the folder is missing. On success
    /// the re-check flips the status circle to ✓ and hides this button — no restart.
    /// Errors surface via NSAlert (rare: permission/FDA); state stays honest either way.
    private func createWatchFolder() {
        let dir = watchDir
        do {
            try FileManager.default.createDirectory(
                atPath: dir, withIntermediateDirectories: true, attributes: nil)
        } catch {
            let alert = NSAlert()
            alert.messageText = "Не удалось создать папку"
            alert.informativeText = error.localizedDescription
            alert.alertStyle = .warning
            alert.runModal()
        }
        recheckFolder()   // reflect reality whether create succeeded or failed
    }

    /// Copy the brew command to the clipboard (parity with the mockup "Копировать").
    private func copyBrewCommand() {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString("brew install ffmpeg", forType: .string)
    }

    /// «Завершить установку» — ask for consent-notice first, then install.
    ///
    /// The install ends with launchd bootstrapping the agent, whose FIRST tick
    /// probes the watch folder — and on macOS 26 that probe is what makes the
    /// system put up its own «Разрешить / Не разрешать» dialog (addendum §5.1: both
    /// grants we measured were `auth_reason=2`, i.e. user consent in that dialog,
    /// not a trip to System Settings). A dialog nobody warned about is the single
    /// cheapest way to lose the grant forever: «Не разрешать» writes `auth_value=0`
    /// and the dialog never comes back. So the host gets a chance to explain what
    /// is about to be asked BEFORE we start the installer.
    private func startInstall() {
        guard ffmpegFound else { return }
        requestConsentNotice(watchDir) { runInstall() }
    }

    /// The install itself, off the main thread. Separated from `startInstall` so
    /// the consent notice sits in front of it without duplicating this logic.
    private func runInstall() {
        guard let installer = InstallRunner.bundledInstallerPath() else {
            phase = .failed("Установщик не найден в приложении (пересоберите .app).")
            return
        }
        let dir = watchDir
        let env = installerExtraEnv
        phase = .running
        // Single-flight (B4): the same coordinator the Settings re-point and the
        // launch-time auto-update go through, so an install can never overlap one
        // of those. A refusal comes back as .failed with an honest reason.
        InstallCoordinator.shared.submit(
            id: "setup-install",
            work: { InstallOutcome(InstallRunner.run(installerPath: installer,
                                                     watchDir: dir, extraEnv: env)) },
            completion: { outcome in
                switch outcome {
                case .done:
                    self.phase = .done
                    // Give the agent a beat to write its first state, then hand off.
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) {
                        self.onInstalled()
                    }
                case .failed(let m):
                    self.phase = .failed(m)
                }
            })
    }
}

// MARK: - Local helpers (file-scoped; the shared ones in main/StatusView are private)

/// The 40px app-icon (radius 11, canvas.appIcon radial + brand glyph + teal glow),
/// matching the Status header icon (mockup 01 .app-icon). Defined locally because
/// StatusView's AppIconBadge is `private` (file-scoped).
private struct SetupAppIcon: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .fill(Tokens.Canvas.appIconGradient)
            Image(systemName: "books.vertical.fill")
                .font(.system(size: 18, weight: .semibold))
                .foregroundColor(Tokens.C.brandCyan)
        }
        .frame(width: 40, height: 40)
        .overlay(
            RoundedRectangle(cornerRadius: 11, style: .continuous)
                .stroke(Color.white(0.18), lineWidth: 0.5)
        )
        .shadow(color: Tokens.C.brandTeal.opacity(0.45), radius: 8, x: 0, y: 6)
    }
}

/// A 1px full-width rule (main.swift's Hairline is `private`, so a local copy).
private struct SetupHairline: View {
    let color: Color
    var body: some View {
        Rectangle().fill(color).frame(height: 1)
    }
}
