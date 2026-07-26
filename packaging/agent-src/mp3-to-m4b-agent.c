// mp3-to-m4b-agent.c — the stable Full-Disk-Access "responsible target" of the
// mp3-to-m4b LaunchAgent (release 1.0, arch/plan-binrunner-mp3-v2.md §M0).
//
// WHY A MACH-O BINARY (and not the old bin/runner.sh):
//   macOS Tahoe (26.x) attributes a launchd agent's TCC request to the Mach-O
//   IMAGE of the responsible process. For a shebang script the image is
//   /bin/bash — the script's own path never reaches tccd ("AUTHREQ_SUBJECT
//   subject=/bin/bash", and platform binaries are silently denied), so a grant
//   given to runner.sh is dead as a class. Making ProgramArguments[0] point at
//   THIS binary makes the subject our own file: the user grants Full Disk Access
//   to it once, and the grant lives as long as the path AND the bytes stay
//   stable (ad-hoc designated requirement = cdhash of these bytes).
//   Diagnosis: ../2026.06 fb2-to-epub/.patches/020-tahoe-fda-script-grant-dead-real-not-panel.md
//
// WHY spawn+wait AND NOT exec (load-bearing, do not "simplify"):
//   exec would REPLACE this process image with /bin/bash — the TCC subject
//   would become /bin/bash again and the bug would be back. Instead we spawn
//   `/bin/bash <runner>` as a CHILD and wait for it: this helper stays alive as
//   the responsible parent, and the children (bash → python3 → ffmpeg) are
//   attributed to it. Proven in production by the fb2-to-epub sibling and
//   re-proven for THIS project by the T0 gate (packaging/agent-src/t0/) before
//   any of M1–M7 is allowed to start.
//
// BYTE STABILITY (the second axis of the grant):
//   This source is compiled ONCE by packaging/agent-src/build-once.sh; the
//   resulting universal binary packaging/mp3-to-m4b-agent is committed to git
//   and FROZEN. Rebuilding produces a new cdhash and silently kills every
//   user's grant — read packaging/agent-src/PROVENANCE.md before touching it.
//
// Behavior (parity with the old runner.sh unless stated):
//   • finds runner.sh NEXT TO ITSELF (own path via _NSGetExecutablePath +
//     realpath — not argv[0], which launchd/relative invocations make
//     unreliable). This "sibling named runner.sh" contract is baked into the
//     frozen bytes: renaming runner.sh breaks startup permanently;
//   • missing runner → message to stderr + exit 1 (same as before);
//   • environment is inherited as-is (PYTHON3 / PATH / MP3TOM4B_* / FFMPEG /
//     FFPROBE come from the LaunchAgent's EnvironmentVariables);
//   • SIGTERM/SIGINT/SIGHUP are forwarded to the child so runner.sh's traps run
//     (and runner.sh in turn forwards to python, which stops its ffmpeg);
//   • the child's exit code is mirrored (128+signal when it died of a signal);
//   • no stdout noise — stdout/stderr pass through to the agent's log file.
//
// No dependencies beyond libSystem. Keep this file as small as possible: any
// future change means a new cdhash and a re-grant for every user.

#include <errno.h>
#include <libgen.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <signal.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

// Child pid for the signal forwarder. sig_atomic_t is int on macOS and pid_t
// fits; 0 = "not spawned yet" / "already reaped".
//
// There is no "pending signal" slot here, on purpose. The window between
// installing the handlers and knowing the child's pid is closed by BLOCKING
// TERM/INT/HUP across the spawn instead: while they are blocked no handler can
// run with g_child == 0, and the kernel remembers every distinct signal that
// arrived, so unblocking delivers them all. A single-slot "remember the last
// one" (the donor's approach) silently drops the first of two different
// signals; the mask has no such failure mode and is less code.
static volatile sig_atomic_t g_child = 0;

static void forward_signal(int sig) {
    pid_t child = (pid_t)g_child;
    if (child > 0) {
        kill(child, sig);            // async-signal-safe
    }
}

// Resolve our own absolute path. _NSGetExecutablePath is authoritative for the
// running image (works for absolute, relative and PATH-based invocations);
// realpath collapses symlinks so the runner is looked up next to the REAL file.
// argv[0] is only a last-resort fallback.
static int resolve_self_path(const char *argv0, char *out, size_t outsz) {
    char buf[PATH_MAX];
    uint32_t sz = (uint32_t)sizeof(buf);
    if (_NSGetExecutablePath(buf, &sz) != 0) {
        if (argv0 == NULL || argv0[0] == '\0') return -1;
        strlcpy(buf, argv0, sizeof(buf));
    }
    if (realpath(buf, out) == NULL) {
        // realpath may fail on exotic mounts; fall back to the raw path.
        strlcpy(out, buf, outsz);
    }
    return 0;
}

int main(int argc, char *argv[]) {
    char self[PATH_MAX];
    if (resolve_self_path(argc > 0 ? argv[0] : NULL, self, sizeof(self)) != 0) {
        fprintf(stderr, "mp3-to-m4b-agent: cannot resolve own path\n");
        return 1;
    }

    // dirname() may modify its argument and returns a pointer into it —
    // keep the copy alive for as long as `dir` is used.
    char selfcopy[PATH_MAX];
    strlcpy(selfcopy, self, sizeof(selfcopy));
    const char *dir = dirname(selfcopy);

    char runner[PATH_MAX];
    int n = snprintf(runner, sizeof(runner), "%s/runner.sh", dir);
    if (n < 0 || (size_t)n >= sizeof(runner)) {
        fprintf(stderr, "mp3-to-m4b-agent: runner path too long\n");
        return 1;
    }
    if (access(runner, R_OK) != 0) {
        fprintf(stderr, "mp3-to-m4b: runner not found at %s\n", runner);
        return 1;
    }

    // 1. Block the three signals FIRST, before the handlers even exist, and
    //    keep the mask we inherited. Nothing can be delivered from here until
    //    step 5, by which time the child's pid is known.
    sigset_t blockset, oldmask;
    sigemptyset(&blockset);
    sigaddset(&blockset, SIGTERM);
    sigaddset(&blockset, SIGINT);
    sigaddset(&blockset, SIGHUP);
    sigprocmask(SIG_BLOCK, &blockset, &oldmask);

    // 2. Install the forwarders. No SA_RESTART: a forwarded signal interrupts
    //    waitpid with EINTR and the loop below retries until the child exits.
    struct sigaction sa;
    memset(&sa, 0, sizeof(sa));
    sa.sa_handler = forward_signal;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);
    sigaction(SIGHUP, &sa, NULL);

    // 3. spawn+wait, NOT exec — see the header comment. Environment inherited.
    //    The child must NOT inherit our temporary block mask (bash would start
    //    deaf to the very signals we forward to it), so the spawn attributes
    //    reset it to the mask WE were started with. Handler dispositions are
    //    reset to default by exec on their own, so SETSIGDEF is not needed.
    char *child_argv[] = { "/bin/bash", runner, NULL };
    posix_spawnattr_t attr;
    posix_spawnattr_t *attrp = NULL;
    if (posix_spawnattr_init(&attr) == 0) {
        if (posix_spawnattr_setsigmask(&attr, &oldmask) == 0 &&
            posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK) == 0) {
            attrp = &attr;
        } else {
            posix_spawnattr_destroy(&attr);
        }
    }
    if (attrp == NULL) {
        // Could not build the attributes: rather than hand bash a blocked mask,
        // restore ours now and spawn with plain inheritance (the donor's exact
        // behaviour, including its narrow pre-spawn window).
        sigprocmask(SIG_SETMASK, &oldmask, NULL);
    }
    pid_t pid = 0;
    int rc = posix_spawn(&pid, "/bin/bash", NULL, attrp, child_argv, environ);
    if (attrp != NULL) posix_spawnattr_destroy(&attr);
    if (rc != 0) {
        sigprocmask(SIG_SETMASK, &oldmask, NULL);
        fprintf(stderr, "mp3-to-m4b-agent: posix_spawn(/bin/bash %s): %s\n",
                runner, strerror(rc));
        return 1;
    }

    // 4./5. Publish the pid, then let everything that queued up arrive. Each
    //       distinct pending signal is delivered here and forwarded in turn.
    g_child = (sig_atomic_t)pid;
    sigprocmask(SIG_SETMASK, &oldmask, NULL);

    int status = 0;
    for (;;) {
        pid_t w = waitpid(pid, &status, 0);
        // Clear g_child BEFORE leaving the loop: once the child is reaped its
        // pid may be recycled by the OS, and a signal arriving in the gap
        // between here and exit() would otherwise be kill()'d at a stranger.
        if (w == pid) { g_child = 0; break; }
        if (w == -1 && errno == EINTR) continue;  // signal forwarded; keep waiting
        g_child = 0;
        fprintf(stderr, "mp3-to-m4b-agent: waitpid: %s\n", strerror(errno));
        return 1;
    }
    if (WIFEXITED(status)) return WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return 128 + WTERMSIG(status);
    return 1;
}
