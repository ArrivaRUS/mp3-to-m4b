-- mp3-to-m4b installer applet (UI conductor).
--
-- Thin AppleScript front-end: it shows dialogs and delegates ALL real work to the
-- bundled packaging/installer.sh (copied into Contents/Resources by build-app.sh).
-- Flow:
--   1. Verify ffmpeg is present (offer to open the Homebrew page if not).
--   2. choose folder, defaulting to ~/Desktop/mp3-to-m4b (created if missing).
--   3. Run the bundled installer.sh with the chosen folder.
--   4. Show a success screen with where-to-drop-files guidance.
--   5. Surface any failure with the installer's own message.
--
-- NOTE: the SwiftUI app is the day-to-day UI (it reads the agent's state and has a
-- Setup screen). This applet is the optional one-shot "install the background
-- agent" front-end, mirroring the fb2-to-epub neighbor. ffmpeg is detected via the
-- same Homebrew locations the installer uses (/opt/homebrew, /usr/local), so the
-- check matches Apple Silicon and Intel.

on ffmpeg_present()
	return (do shell script "command -v ffmpeg >/dev/null 2>&1 || test -x /opt/homebrew/bin/ffmpeg || test -x /usr/local/bin/ffmpeg") is ""
end ffmpeg_present

on resource_path(theName)
	-- Resources sit next to this compiled script inside the .app bundle.
	set rsrc to (path to resource theName) as text
	return POSIX path of rsrc
end resource_path

on run
	-- 1. ffmpeg check ---------------------------------------------------------
	if not ffmpeg_present() then
		set theChoice to button returned of (display dialog ¬
			"mp3-to-m4b needs ffmpeg to build audiobooks, but it isn't installed." & return & return & ¬
			"Install Homebrew (free), then run:  brew install ffmpeg" & return & ¬
			"After that, run this app again." ¬
			buttons {"Quit", "Get Homebrew"} default button "Get Homebrew" with title "mp3-to-m4b" with icon caution)
		if theChoice is "Get Homebrew" then
			open location "https://brew.sh"
		end if
		return
	end if

	-- 2. Default folder + folder picker ---------------------------------------
	set defaultDir to (POSIX path of (path to home folder)) & "Desktop/mp3-to-m4b"
	do shell script "mkdir -p " & quoted form of defaultDir

	display dialog ¬
		"mp3-to-m4b watches a folder. Drop a folder of .mp3 files into it and the app turns them into one .m4b audiobook automatically." & return & return & ¬
		"Pick the folder to watch. The default is an 'mp3-to-m4b' folder on your Desktop." ¬
		buttons {"Cancel", "Choose Folder…"} default button "Choose Folder…" with title "mp3-to-m4b"

	set watchFolder to (choose folder with prompt "Choose the folder mp3-to-m4b should watch:" ¬
		default location (defaultDir as POSIX file))
	set watchPath to POSIX path of watchFolder

	-- 3. Run the bundled installer --------------------------------------------
	try
		set installerPath to my resource_path("installer.sh")
	on error
		display dialog "mp3-to-m4b: the installer is missing from the app bundle. Re-download the app." ¬
			buttons {"OK"} default button "OK" with title "mp3-to-m4b" with icon stop
		return
	end try

	try
		set installOutput to do shell script ¬
			"/bin/bash " & quoted form of installerPath & " " & quoted form of watchPath
	on error errMsg number errNum
		display dialog ¬
			"mp3-to-m4b couldn't finish installing." & return & return & errMsg ¬
			buttons {"OK"} default button "OK" with title "mp3-to-m4b" with icon stop
		return
	end try

	-- 4. Success ---------------------------------------------------------------
	set fdaNote to ""
	set homePosix to POSIX path of (path to home folder)
	if watchPath starts with (homePosix & "Desktop/") ¬
		or watchPath starts with (homePosix & "Documents/") ¬
		or watchPath starts with (homePosix & "Downloads/") then
		set fdaNote to return & return & ¬
			"Note: your folder is in a protected location. If books aren't picked up, " & ¬
			"open System Settings → Privacy & Security → Full Disk Access, click +, then " & ¬
			"add this file (press ⇧⌘G and paste the path):" & return & ¬
			"~/Library/Application Support/mp3-to-m4b/bin/runner.sh"
	end if

	display dialog ¬
		"mp3-to-m4b is set up." & return & return & ¬
		"Watching:" & return & watchPath & return & return & ¬
		"Drop a folder of .mp3 files into that folder — the app will offer to build it " & ¬
		"into a single .m4b audiobook." & fdaNote ¬
		buttons {"Open Folder", "Done"} default button "Done" with title "mp3-to-m4b"

	if button returned of result is "Open Folder" then
		do shell script "open " & quoted form of watchPath
	end if
end run
