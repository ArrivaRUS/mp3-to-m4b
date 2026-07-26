# PROVENANCE — packaging/mp3-to-m4b-agent (FROZEN artifact)

`packaging/mp3-to-m4b-agent` is the pre-built, ad-hoc-signed universal Mach-O
helper that the LaunchAgent's `ProgramArguments[0]` points at. It is the file
users grant **Full Disk Access** to. The grant is pinned to two things at once:

1. the **path** it is installed at
   (`~/Library/Application Support/mp3-to-m4b/bin/mp3-to-m4b-agent`), and
2. the ad-hoc designated requirement — i.e. the **cdhash of these exact bytes**.

Change either and every existing user's grant dies silently.

## ⚠️ DO NOT REBUILD

Rebuilding (even from identical source, even with the same flags) may produce a
different cdhash → **every existing user's FDA grant dies silently** and they
each get an unexplained trip to System Settings. That is exactly the failure
mode this helper exists to eliminate
(diagnosis: `../2026.06 fb2-to-epub/.patches/020-tahoe-fda-script-grant-dead-real-not-panel.md`).

- `build/build-app.sh` only **copies** this file into the .app and **verifies
  its SHA-256** against `EXPECTED_HELPER_SHA256` below — on four borders:
  repo → signed staging `.app` → `build/dist/*.app` after `ditto` → `.app`
  extracted from the mounted final DMG (plan v2, M3f). Any mismatch is
  release-blocking.
- `packaging/installer.sh` checks the same golden SHA on the **source** before
  it writes anything and on the **destination** after installing (B5), and
  installs with a byte-compare preserve: an identical installed copy is never
  rewritten (so a re-install never churns the grant).
- A rebuild is a rare, deliberate event (helper bug / security fix): run
  `MP3TOM4B_AGENT_REBUILD_I_UNDERSTAND=1 packaging/agent-src/build-once.sh`,
  update this file, and set `requires_fda_regrant=true` in the release notes.

### When the freeze becomes binding

The freeze is a **product** constraint, not a build one: it becomes binding at
the moment the **first** user (including the developer's own machine) grants FDA
to these bytes — that is, at T0.3 / R1b. Until then a rebuild costs nothing.
After it, a rebuild costs every user a trip to System Settings.

## Artifact identity (built 2026-07-25, frozen)

| Field | Value |
|---|---|
| File | `packaging/mp3-to-m4b-agent` |
| SHA-256 (`EXPECTED_HELPER_SHA256`) | `791d020d42477755fe3c46070699421280c2dd7e5f248da59f3f826a5bdbc079` |
| Size | 118 256 bytes |
| Format | Mach-O universal (x86_64 + arm64) |
| Signing identifier | `mp3-to-m4b-agent-55554944838d484ade4a3d3ea5a2ecba390a35c0` |
| CDHash (arm64 slice) | `f48e6941ba84d8f2e0cecd3adee53474c491d8c4` |
| CDHash (x86_64 slice) | `9b53eabafa75d0df3e782a8346dd524db3d843a1` |
| Designated requirement | `cdhash H"f48e6941ba84d8f2e0cecd3adee53474c491d8c4" or cdhash H"9b53eabafa75d0df3e782a8346dd524db3d843a1"` |
| Signature | ad-hoc (`codesign -s -`) |
| Min macOS | 11.0 |
| Install path (part of the grant identity) | `~/Library/Application Support/mp3-to-m4b/bin/mp3-to-m4b-agent` |
| Sibling contract (baked into the bytes) | spawns `/bin/bash <dirname(self)>/runner.sh` |

> **One superseded build exists.** The first freeze candidate
> (`8de3c366…`, 117 872 b, cdhash arm64 `9ea66842…`) was rebuilt the same day to
> close the pre-spawn signal window below, before any production grant existed.
> The T0 evidence in this file was collected on those bytes. That does not
> weaken it: T0 proved the **construction** — that `ProgramArguments[0]` decides
> the TCC subject and that the grant is bound to path + cdhash — not one
> particular hash. The final bytes get their own confirmation at R1b (M7),
> together with the human. Any TCC record still naming `8de3c366…` is dead and
> must be removed.

`codesign --verify --strict` passes; `--arch arm64` / `--arch x86_64` both report
an ad-hoc signature with the cdhashes above.

## Build environment (for audit — reproducing is NOT the release path)

| Field | Value |
|---|---|
| Source | `packaging/agent-src/mp3-to-m4b-agent.c` |
| Build script | `packaging/agent-src/build-once.sh` (one-shot, freeze-guarded) |
| Command | `clang -Os -Wall -Wextra -arch arm64 -arch x86_64 -mmacosx-version-min=11.0 -o mp3-to-m4b-agent mp3-to-m4b-agent.c` then `strip`, `codesign --force -s -` |
| Apple clang | 21.0.0 (clang-2100.1.1.101), target arm64-apple-darwin25.5.0 |
| Xcode | 26.6 (Build 17F113) |
| macOS | 26.5.2 (25F84) |
| Date | 2026-07-25 |

A from-source rebuild with a different Xcode/SDK is useful only to audit that
the source matches the behavior; the release file is always the committed
artifact above, byte-for-byte.

## Deltas from the fb2-to-epub donor helper

The design and the process chain are the donor's, proven in production
(`../2026.06 fb2-to-epub/packaging/agent-src/fb2-to-epub-agent.c`). The bytes are
necessarily new (different strings), so the two known micro-windows the donor
consciously froze were re-decided here:

1. **pid-reuse window after `waitpid` — FIXED.** The donor keeps the reaped
   child's pid in `g_child` until process exit, so a signal arriving in that gap
   is `kill()`ed at a possibly recycled pid. Here the loop exits via
   `if (w == pid) { g_child = 0; break; }` (and the error path clears it too).
   Donor's `PROVENANCE.md` explicitly asks for this on any new build.
2. **Pre-spawn signal collapse — FIXED.** The donor's `g_pending_sig` is a
   single slot: two *different* signals arriving between `sigaction()` and
   `posix_spawn()` returning collapse to the last one. Rather than widen the
   slot, the window is removed: TERM/INT/HUP are **blocked before the handlers
   are installed** and unblocked only after `g_child` is published, so no
   handler can ever run without a pid, and the kernel's pending set preserves
   every distinct signal. `g_pending_sig` is gone entirely — this is less code
   than the donor's, not more.

   The one hazard the mask introduces is that the child would inherit it, and a
   bash that starts deaf to TERM/INT/HUP breaks the whole shutdown chain. That
   is why the spawn carries `posix_spawnattr_setsigmask(&attr, &oldmask)` +
   `POSIX_SPAWN_SETSIGMASK`, restoring exactly the mask we were started with.
   Handler dispositions need no such care — `exec` resets them by itself.

   **Mutation-checked** (donor patch 021, rule 5): building the same source with
   `POSIX_SPAWN_SETSIGMASK` dropped makes bash inherit
   `['SIGHUP','SIGINT','SIGTERM']` blocked, while the shipped artifact gives it
   an empty mask. Note that an earlier, weaker mutant (removing only
   `setsigmask`) was NOT detected, because `posix_spawnattr_init` defaults the
   mask to empty and bash unblocks whatever it traps — measure the mask in a
   child spawned *before* any trap is installed, or the test passes vacuously.

Both were closed while the freeze was still free: the artifact is permanent, so
a known defect in it is a bad trade even when it is rare (Yurka's call,
2026-07-25). No further defects are documented in the donor's `PROVENANCE.md` or
its patches 020/021 — 021 is about process, not helper code.

## T0 gate (this machine, 2026-07-25)

The T0 harness lives in `packaging/agent-src/t0/` and is re-runnable after major
OS updates (it is the only thing that can tell us tccd changed its attribution
model).

| Field | Value |
|---|---|
| macOS at T0 | 26.5.2 (25F84) |
| Harness | `packaging/agent-src/t0/t0.sh` |
| T0 helper copy | `~/Library/Application Support/mp3-to-m4b-t0/bin/mp3-to-m4b-agent-t0` (same bytes, different path+name) |
| Gate zone | `~/Downloads/mp3tom4b-t0-probe` — TCC-protected, **local** |
| Diagnostic zone | `~/Desktop/mp3tom4b-t0-probe` — TCC-protected **and iCloud FileProvider** |

**Verdict: the mechanism is proven.** `ProgramArguments[0]` decides the TCC
subject, and the grant is pinned to this file's path *and* its cdhash.

| Cell | Result |
|---|---|
| T0.1 — PA0 = helper, no grant | not readable. tccd names **the helper** as `responsible_path` and `AUTHREQ_SUBJECT`, with `identifier=mp3-to-m4b-agent-555549444d9d51006c4b3182b39cb757e523de5e` |
| T0.2 — PA0 = `runner.sh` (shebang) | not readable, `AUTHREQ_SUBJECT: subject=/bin/bash`, `responsible_path=/bin/bash`, denied in ~200 ms. Same folder, same session as T0.1 |
| T0.3 — PA0 = helper, grant present | **readable in 5 ms**, `marker.txt` content actually read |
| T0.2′ — PA0 = `runner.sh`, grant present | still denied → T0.3's green is the grant, not ambient access |
| T0.4a — different valid binary at the same path | not readable → the grant does not follow the path alone |
| T0.4b — frozen bytes restored | **not confirmed in-session**, see the caveat below |
| T0.5 — `exec` form | not run (informational only; `t0.sh exec-form` is ready) |

Direct database confirmation of the byte pinning — the `csreq` blob stored in
the user's TCC record decodes to exactly the two cdhashes in the identity table
above:

    cdhash H"9ea66842fa67c4122c5d144bf690be18c23a3b10"
     or cdhash H"b5dfb5ad8803c1260a7ac973e06c7afd4d29c5dc"

### Three things T0 taught us that the plan did not predict

1. **An ungranted helper HANGS, it does not get denied.** With PA0 = a shebang
   script, `open()` on a protected folder returns EPERM in ~200 ms and tccd logs
   a full AUTHREQ. With PA0 = the helper and no grant, the same call **blocks
   indefinitely** (>60 s, sampled inside `__open_nocancel`) and tccd logs
   nothing — macOS wants to ask the user, and a bare LaunchAgent has no way to
   ask. The shipping probe therefore needs a watchdog: without one the agent
   wedges forever, launchd will not start a second instance, `folder_access`
   is never published, and the whole access-onboarding surface never appears.
2. **macOS will prompt for an attributable helper.** The T0 grant here was not
   created in System Settings: consent dialogs appeared and were approved by the
   user, producing `kTCCServiceSystemPolicyDesktopFolder` and
   `…DownloadsFolder` records with `auth_value=2, auth_reason=2` (user consent).
   Per-folder consent is enough — Full Disk Access was never granted and the
   `kTCCServiceSystemPolicyAllFiles` preflight is denied on every run.
3. **iCloud-backed folders behave differently.** With one and the same granted
   helper, a local protected folder answers in 5 ms while `~/Desktop` (an iCloud
   FileProvider domain on this machine) blocks. The product's default watch
   folder is `~/Desktop/mp3-to-m4b` — i.e. exactly the bad zone. This is a
   product-level blocker, independent of the helper design.

### T0.4b is deliberately OUTSIDE the gate

The gate is **B + C + D** (T0.3 + T0.2′ + T0.4a). Decision by Yurka, 2026-07-25.

Cell T0.4a — the only way to test byte pinning — poisons the path: after a
foreign binary has run at a granted path, access does not come back when the
frozen bytes are restored (verified for 2.5 min, both with an in-place rewrite
and with a fresh inode). That is a property of the *test*, not of the product:
the installer refuses to place anything but the golden SHA (B5), so a foreign
binary never reaches this path in the field. Re-check T0.4b from a clean slate
**after a reboot** if anyone wants the closure; nothing depends on it. "The
grant is bound to this helper, by path and by bytes" is already established by
T0.3 (it reads), T0.2′ (a shebang PA0 with the same grant does not) and T0.4a
(different bytes at the same path do not).

### Test traces that must be removed before release (donor patch 021, rule 2)

The T0 run left state on the developer's machine. Leaving it is how the sibling
project shipped a production bug — its own test artefact made a user's bytes
match and suppressed the migration. Clean up:

- `packaging/agent-src/t0/t0.sh clean` — removes the T0 tree, the test plist and
  both probe folders;
- the human removes the `mp3-to-m4b-agent-t0` row from Full Disk Access;
- the two TCC records created by the consent dialogs
  (`…DesktopFolder` / `…DownloadsFolder`) are pinned to the **superseded**
  cdhash `9ea66842…` and are dead weight — remove them with the row above.

Practical consequence for the release: a helper rebuild does not merely cost
users a re-grant, it leaves their agent **hung**. The freeze rule is stricter
than the donor assumed.
