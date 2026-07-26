#!/usr/bin/env python3
"""T0 probe payload — the tiny stand-in for `python3 -m agent` inside the gate.

It answers exactly one question, honestly: *can the process at the end of the
LaunchAgent chain actually read a folder inside a TCC-protected zone?* — and it
records the pids needed to correlate that answer with tccd's own log.

Written for the system interpreter (macOS ships 3.9), stdlib only.

Contract (arch/plan-binrunner-mp3-v2.md §4.2):
  * BEFORE touching the watch folder it writes ``python.json`` with
    ``os.getpid()`` / ``os.getppid()`` / ``sys.executable`` — so the pid is on
    disk even if the read hangs or the process is killed;
  * then it listdir()s ``$T0_WATCH`` and, on success, actually READS
    ``marker.txt``. A listing that succeeds but a read that fails is not "ok" —
    we verify the functional outcome, never the System Settings panel
    (donor lesson 020B);
  * the states are the agent's tri-state PLUS one this machine forced us to add:
    ``ok`` | ``denied`` (EPERM/EACCES — TCC and chmod merged ON PURPOSE) |
    ``missing`` (ENOENT) | ``blocked``;
  * everything lands in ``$T0_STATE_ROOT/t0_result.json``, which is the file the
    harness polls for.

WHY ``blocked`` EXISTS (measured 2026-07-25, macOS 26.5.2)
    With PA0 = the frozen Mach-O helper and NO grant yet, ``open()`` on a
    TCC-protected folder does not return EPERM — it **hangs indefinitely**
    (>60 s, sampled inside ``__open_nocancel``), and tccd logs nothing at all.
    With PA0 = a shebang script the very same call is denied in ~200 ms with a
    full tccd AUTHREQ trace naming ``/bin/bash``. The difference is the subject:
    a platform binary is silently denied, an attributable one is treated as a
    promptable client — and a bare LaunchAgent has no way to show that prompt.

    So the probe runs the syscall on a DAEMON thread behind a watchdog and, on
    timeout, reports ``blocked`` and leaves via ``os._exit`` — the stuck thread
    can never be joined, and a probe that wedges its own process would take the
    whole agent down with it.
"""

from __future__ import annotations

import errno
import json
import os
import sys
import threading
import time

MARKER = "marker.txt"
# Generous next to the ~200 ms an answered request takes, short enough that the
# harness stays interactive.
PROBE_TIMEOUT_S = float(os.environ.get("T0_PROBE_TIMEOUT", "8"))


def _write_json(path: str, payload: dict) -> None:
    """Atomic-ish write: tmp + rename, so the harness never reads a half file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def main() -> int:
    state_root = os.environ.get("T0_STATE_ROOT") or os.environ.get("MP3TOM4B_SUPPORT_DIR")
    watch = os.environ.get("T0_WATCH")
    if not state_root or not watch:
        sys.stderr.write("t0_probe: T0_STATE_ROOT and T0_WATCH must be set\n")
        return 2
    os.makedirs(state_root, exist_ok=True)

    # 1. pids FIRST — before any protected-path syscall.
    ident = {
        "py_pid": os.getpid(),
        "py_ppid": os.getppid(),
        "python_exe": os.path.realpath(sys.executable),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
    }
    _write_json(os.path.join(state_root, "python.json"), ident)

    # 2. the probe itself — on a daemon thread, behind a watchdog.
    box = {"access": "ok", "entries": [], "marker_text": None, "err": None}

    def probe() -> None:
        try:
            box["entries"] = sorted(os.listdir(watch))
        except PermissionError as exc:
            box["access"] = "denied"
            box["err"] = "%s (errno=%s)" % (exc.strerror, exc.errno)
        except FileNotFoundError as exc:
            box["access"] = "missing"
            box["err"] = "%s (errno=%s)" % (exc.strerror, exc.errno)
        except OSError as exc:
            if exc.errno in (errno.EPERM, errno.EACCES):
                box["access"] = "denied"
            elif exc.errno == errno.ENOENT:
                box["access"] = "missing"
            else:
                box["access"] = "error"
            box["err"] = "%s (errno=%s)" % (exc.strerror, exc.errno)

        # A listing is not proof: read the marker's CONTENT too. TCC can let the
        # directory enumeration through and still block the file (and a stale
        # directory cache can fake a listing outright).
        if box["access"] == "ok":
            try:
                with open(os.path.join(watch, MARKER), "r", encoding="utf-8") as fh:
                    box["marker_text"] = fh.read().strip()
            except PermissionError as exc:
                box["access"], box["err"] = "denied", "listdir ok, marker read denied: %s" % exc
            except FileNotFoundError as exc:
                box["access"], box["err"] = "error", "listdir ok, marker absent: %s" % exc
            except OSError as exc:
                box["access"], box["err"] = "error", "listdir ok, marker unreadable: %s" % exc
        done.set()

    done = threading.Event()
    t0 = time.time()
    worker = threading.Thread(target=probe, name="t0-probe", daemon=True)
    worker.start()
    finished = done.wait(PROBE_TIMEOUT_S)
    elapsed = round(time.time() - t0, 3)

    if finished:
        access = box["access"]
        entries = box["entries"]
        marker_text = box["marker_text"]
        err = box["err"]
    else:
        access = "blocked"
        entries = []
        marker_text = None
        err = ("open() did not return within %ss — the request is neither "
               "allowed nor denied (see the module docstring)" % PROBE_TIMEOUT_S)

    # 3. stitch in what the shell layer recorded about itself.
    shell = _read_json(os.path.join(state_root, "bash.json"))

    result = dict(ident)
    result.update(
        {
            "folder_access": access,
            "error": err,
            "elapsed_s": elapsed,
            "watch": watch,
            "entries": entries,
            "marker_seen": MARKER in entries,
            "marker_text": marker_text,
            "bash_pid": shell.get("bash_pid"),
            "bash_ppid": shell.get("bash_ppid"),
            "pa0_hint": shell.get("pa0_hint"),
            "form": "donor(background+wait)",
        }
    )
    _write_json(os.path.join(state_root, "t0_result.json"), result)

    sys.stdout.write("t0_probe: folder_access=%s (%ss) py_pid=%s bash_pid=%s\n"
                     % (access, elapsed, result["py_pid"], result["bash_pid"]))
    sys.stdout.flush()

    # os._exit, not sys.exit: when the state is `blocked` the worker is wedged in
    # a syscall forever. A normal shutdown would wait for it and the probe would
    # hang exactly where it is supposed to report.
    os._exit(0)


if __name__ == "__main__":
    main()
