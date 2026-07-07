# mp3-to-m4b

[Русский](README.md) · **English**

<img src="branding/icon.png" alt="mp3-to-m4b" width="96" align="right">

Automatic assembly of a folder of mp3s into a single **`.m4b` audiobook** (chapters + embedded cover) on macOS, for a folder of your choice. Install the app, point it at a folder, and from then on you just drop a folder of `.mp3` files into it; the app **comes to the front** with a confirmation window (author/narrator, title, cover, quality, build mode, splitting), you hit **Build**, and a finished `.m4b` appears right next to the source. The engine is `ffmpeg`/`ffprobe` only. Installation and status live in a tidy native window; the build itself runs in the background.

## Interface

<p align="center">
  <img src="docs/screenshots/confirm.png" height="380" alt="Confirmation window — author/narrator, title, cover, quality, build mode, splitting">
  &nbsp;&nbsp;
  <img src="docs/screenshots/status.png" height="380" alt="Status screen — watching, stats, recent builds">
  &nbsp;&nbsp;
  <img src="docs/screenshots/queue.png" height="380" alt="Queue — sections by status, Build again, cancel">
  &nbsp;&nbsp;
  <img src="docs/screenshots/settings.png" height="380" alt="Settings — change folder, engine status, reset stats, Full Disk Access">
</p>

<p align="center"><sub><b>Confirm</b> · <b>Status</b> · <b>Queue</b> · <b>Settings</b></sub></p>

The app is native (SwiftUI / AppKit), with a fixed-width 400pt window and a dark theme. The main screens: **Setup** (first run), **Confirm book** (the main flow), **Status**, **Queue**, and **Settings**.

## Features

- **Folder-based auto-build.** Point it at a single folder — the background agent catches new books on its own, with no manual runs. A **subfolder of mp3s** → one `.m4b` audiobook (chapter = file, in natural order). Source files are left untouched; repeated runs are idempotent (anything already built is not silently rebuilt).
- **Confirmation window before building.** No book is built until you confirm the parameters in a pop-up window: **author / narrator** and **title** (editable fields), **cover**, **bitrate**, **stereo / mono**, **sample rate** (“same as source” by default), **build mode** (Fast / Seamless), and **splitting into parts**. At the bottom: **Skip**, **Later** (into the queue), and **Build**.
- **Auto bring-to-front on drop.** When you deliberately drop (or re-drop) a book into the watched folder, the window comes to the front on its own, so you can see the book was picked up. The trigger is **the drop event itself**, not content “novelty”: re-dropping an already-built book raises the window too (detected for both a Finder copy and a move).
- **Live progress + cancel.** On the build screen there’s a **determinate** progress bar with a percentage, “Chapter X of Y: …”, elapsed time, and an estimate of the remaining time (ETA), plus a **Cancel conversion** button. Cancelling kills `ffmpeg` with no orphaned processes and no truncated `.m4b`; the book returns to confirmation so you can rebuild it.
- **Fast and Seamless modes.** **Fast** (the default) encodes chapters in parallel groups and joins them without re-encoding (`stream-copy`) — many times faster on a multi-core Mac, at the cost of ~25 ms of silence exactly at chapter boundaries. **Seamless** is a single continuous encode, bit-exact at the joins. There’s an automatic fallback from Fast to Seamless on any doubt about integrity.
- **Splitting into parts.** A threshold slider (default ~300 MB) splits the book at chapter boundaries into several `.m4b` files (`Part N of M`), or leaves it as a single file. A preview shows “≈ N parts of ~X MB” right away.
- **Grouping loose mp3s.** If loose `.mp3` files sit in the root of the watched folder (not inside a subfolder), the app asks: **merge into one book** or **build them separately**.
- **Queue.** Every book is shown in sections by status (Waiting / In progress / Done / Error). On a finished book there’s **Open** (reveal in Finder) and **Build again** (rebuild, even if the `.m4b` was deleted).
- **Cover.** Taken from the mp3 if one is embedded; otherwise the app generates several square variants to choose from (details in the [Covers](#covers) section).
- **Status screen.** A progress ring, **Built** and **Today** counters, the background agent’s status and the engine version, a compact confirmation-queue row, an **Open Folder** button, and **Clear** for the recent-builds list. Everything updates event-driven.
- **Auto-update of the background agent.** On launch the app compares the bundled engine against the installed one and, if they differ, **reinstalls the agent itself** (with an “Updating the background agent…” indicator). An **Update agent** button is also in Settings. This way, after you install a new DMG the engine is never left on old code.

## Installation (recommended path — DMG)

1. Download `mp3-to-m4b-0.9.dmg` from the **[v0.9](https://github.com/ArrivaRUS/mp3-to-m4b/releases/tag/v0.9)** release page.
2. Open the `.dmg` and drag the **mp3-to-m4b** app into the **Applications** folder — the arrow on the window background shows where it goes.
3. **First launch** (one time only): open the Applications folder, **right-click** `mp3-to-m4b` → **Open** → confirm **Open** in the dialog.
   *Alternative:* launch the app, then go to **System Settings → Privacy & Security → Open Anyway**.
   The app is built without a paid Apple signing certificate (ad-hoc, not notarized), so macOS asks for confirmation — this is a one-time step, after which it launches with a normal double-click.
4. The app checks that **ffmpeg** (and `ffprobe`) is installed — if not, it points you to `brew install ffmpeg` — then prompts you to **choose a folder** to watch. The default is `~/Desktop/mp3-to-m4b` (created automatically). Installing the background agent is launched right from the window (“Finish installation”).
5. Done. A status screen appears showing the chosen folder.

From now on, drop into the chosen folder:

- a **subfolder of `.mp3` files** → `<Book name>.m4b` appears next to it (or several parts if splitting is enabled);
- **loose `.mp3` files in the root** → the app asks whether to merge them into one book or build them separately.

Source files are left untouched. Repeated runs are idempotent — anything already built is not rebuilt (for a rebuild there’s the **Build again** button).

## Full Disk Access (if the folder is in Desktop / Documents / Downloads)

`~/Desktop`, `~/Documents`, and `~/Downloads` are macOS-protected zones (TCC). On a fresh Mac, the background agent may need **one-time** access to them. If books stop building (or never start) — grant Full Disk Access to the **runner**:

1. **System Settings → Privacy & Security → Full Disk Access**.
2. Click **+**, then in the picker press **Cmd-Shift-G** and paste the path:
   ```
   ~/Library/Application Support/mp3-to-m4b/bin/runner.sh
   ```
3. Add it and **turn the toggle on**.

The access is bound to this specific file and persists across app updates. The installer prints the exact runner path at the end of installation. There is also a quick jump to the right pane inside the app: **⚙ Settings → Full Disk Access**.

> Why the runner specifically: macOS binds file-access permissions not to the interpreter script but to the executable named in the agent’s `ProgramArguments`. This gives the agent a stable “responsible” target at a fixed path (`runner.sh`) that you can grant access to once.

## Requirements

- **macOS** (11.0+).
- **[ffmpeg](https://ffmpeg.org)** and **`ffprobe`** — the build engine. Both are required (ffprobe ships alongside ffmpeg).
  Install with `brew install ffmpeg` ([Homebrew](https://brew.sh)).
- **`python3`** — included with the Xcode Command Line Tools (`xcode-select --install`). The background agent runs on it.

> The installer creates a virtual environment under `~/Library/Application Support/mp3-to-m4b/venv` and installs **Pillow** into it — the only third-party dependency (needed to generate covers). Everything else is the Python standard library.

## Covers

The app tries to give every book a cover, and lets you pick it right in the confirmation window — there is no separate selection screen.

- **Embedded cover.** If the source mp3s already carry an image (`attached_pic`), the app picks it up and offers it as the default selection.
- **Generated variants.** The app always draws several **square typographic covers** (author + title, Cyrillic, a play glyph) — shown as a preview strip with a large preview of the selected one. This is a native render via Pillow, **with no generative AI**.
- **Your own image.** The **Replace** button opens a file picker — you can embed any image of your own. The file is copied into the cover store by the **agent** (the app doesn’t write there).
- The selected cover travels with the build command and is **embedded into the `.m4b`** by the engine (`ffmpeg`, `attached_pic`) — always exactly the one selected; with no selection it uses the embedded one, then the first generated variant.

> Online cover lookup (by author + title) is **deferred** in this version — the cover-generation engine exists in the code, but the “Search online” button is not yet enabled in the UI. So a book always gets a cover (embedded or generated), even offline.

## Managing it

| Action | Command |
| --- | --- |
| Live logs | `tail -f ~/Library/Logs/mp3-to-m4b.log` |
| Stop the agent | `launchctl bootout gui/$(id -u)/com.arrivarus.mp3tom4b.agent` |
| Start the agent | `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.arrivarus.mp3tom4b.agent.plist` |
| Restart / run manually | `launchctl kickstart -k gui/$(id -u)/com.arrivarus.mp3tom4b.agent` |
| Change the watched folder | Click **Change** in **⚙ Settings** and pick a new folder — reinstalling is idempotent |
| Rebuild a finished book | **Build again** on a book in the “Done” section (even if the `.m4b` was deleted) |
| Uninstall | Remove the agent: `launchctl bootout gui/$(id -u)/com.arrivarus.mp3tom4b.agent`, then delete `~/Library/Application Support/mp3-to-m4b` and `~/Library/LaunchAgents/com.arrivarus.mp3tom4b.agent.plist` |

## How it works

- macOS `launchd` watches **two** directories via `WatchPaths`: the chosen folder (a new book wakes the agent) and `queue/commands/` (a command from the app wakes it). Agent: `com.arrivarus.mp3tom4b.agent`.
- When it fires, launchd runs the **runner** (`runner.sh`, the FDA target), which does `exec python3 -m agent` — that’s the engine.
- **The app is a reader; the agent is the single writer.** They communicate through state files: the agent writes `state.json` atomically (the showcase: progress, statistics, the queue, covers) plus per-book manifests in `queue/books/`; the app reads them and reacts event-driven, and drops its own actions (Build, cancel, grouping choice, Build again) as commands into `queue/commands/`.
- **The build** runs through `ffmpeg`/`ffprobe`: probing chapters and tags (`ffprobe`), then a `.m4b` with chapters (FFMETADATA), an embedded cover (`attached_pic`), and the container `-f ipod -movflags +faststart`.
- **Codec:** the app prefers Apple **`aac_at`** (AudioToolbox — faster and higher quality), and falls back to the built-in `aac` when it isn’t available. Always CBR at the chosen bitrate, so the file-size estimate stays accurate.
- **Fast mode** encodes chapters in parallel groups of consecutive chapters (up to 8 workers) and joins the result with a `stream-copy` concatenation (a seam only at group boundaries); chapter marks are re-derived from the **actual** fragment durations (`ffprobe`) to avoid drift. **Seamless mode** is a single continuous encode. On a Fast-path validation failure there’s an automatic fallback to Seamless.
- **Sample rate** defaults to “same as source”: the agent reads the chapters’ sample rates and takes the maximum (the minority is upsampled, never downsampled); the window lets you set an explicit 44.1 / 48 kHz.
- The absolute paths to `ffmpeg`/`ffprobe` and the venv `python3` are passed to the agent via `EnvironmentVariables` (the agent starts with a minimal `PATH`).
- `ThrottleInterval=5s` smooths out batch copying of files into the folder.

The scripts and the staged engine live in `~/Library/Application Support/mp3-to-m4b/bin/` (`runner.sh` + the `agent/` package), the data (state, the queue, covers) lives in `~/Library/Application Support/mp3-to-m4b/`, the venv is there too under `venv/`, the agent config is at `~/Library/LaunchAgents/com.arrivarus.mp3tom4b.agent.plist`, and the log is at `~/Library/Logs/mp3-to-m4b.log`.

## Advanced path — install from the CLI

For developers and anyone who prefers the terminal. This bypasses the app and the DMG — it installs the same agent with the same installer (`packaging/installer.sh`).

```sh
# 1. ffmpeg (if you don't have it yet) — ffprobe ships with it
brew install ffmpeg

# 2. Clone and install
git clone https://github.com/ArrivaRUS/mp3-to-m4b.git
cd mp3-to-m4b
./packaging/installer.sh                       # default folder ~/Desktop/mp3-to-m4b
./packaging/installer.sh "/path/to/my folder"  # or your own folder
```

The installer detects `ffmpeg`/`ffprobe`/`python3`, creates a venv with Pillow, copies the `agent/` package and the runner into App Support, generates the `plist` via `plutil` (safe for paths with spaces/unicode), and reloads the agent idempotently. Handy flags for a dry run: `MP3TOM4B_NO_LAUNCHCTL=1` (skip the launchd load), `MP3TOM4B_NO_VENV=1` (skip venv/Pillow), `MP3TOM4B_SUPPORT_DIR=…` (redirect the whole App Support tree to a scratch dir).

## Building from source

The app is **native** — `build-app.sh` compiles the Swift sources (SwiftUI / AppKit / Foundation, no third-party dependencies) into a universal binary. Artifacts (`*.app`, `*.dmg`, `build/dist/`) are in `.gitignore` — binaries are not committed, and the `.dmg` goes into a GitHub Release.

```sh
# 1. app: build/dist/mp3-to-m4b.app
#    (compiles Swift into a universal arm64+x86_64 binary + icon + ad-hoc codesign)
build/build-app.sh [version]

# 2. DMG: build/dist/mp3-to-m4b-<version>.dmg (+ .sha256)
python3 -m venv build/.venv && build/.venv/bin/pip install dmgbuild   # requires dmgbuild
build/make-dmg.sh [version]
```

`build-app.sh`:

- compiles `app/*.swift` (`main.swift`, `Tokens.swift`, `StateModel.swift`, `EngineClient.swift`, `EngineClient+Status.swift`, `QueueView.swift`, `StatusView.swift`, `SetupView.swift`) with `xcrun swiftc` for **arm64 and x86_64** and joins them into a universal binary (`lipo`);
- places the engine — the `agent/` package, the FDA runner `runner.sh`, and `installer.sh` — into `Contents/Resources` (so the app can install the agent right from the bundle);
- builds the icon from `branding/icon-app.svg` → `.icns` (rasterizer: `cairosvg` from `build/.venv`, else `sips`);
- writes a clean `Info.plist` with a fixed `CFBundleIdentifier=com.arrivarus.mp3tom4b` (a stable id matters so that TCC grants don’t get dropped on a rebuild) and `LSMinimumSystemVersion=11.0`;
- runs an ad-hoc `codesign` with a strict verify (build/sign happens in a staging dir outside iCloud, with retries against the FinderInfo race).

`make-dmg.sh` packages the `.app` via `dmgbuild` into a **branded** window (background `branding/dmg-background.png`, the app icon + an `/Applications` symlink, first-launch instructions). The dependency-free `build/build-dmg.sh` (plain `hdiutil`, no background) remains as a fallback.

## License

[MIT](LICENSE)
