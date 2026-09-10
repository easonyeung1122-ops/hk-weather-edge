#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""watch_silent.py - run the intraday watch loop with NO console window at all.

Why this file exists
--------------------
cmd.exe and python.exe are *console-subsystem* programs. Windows is required to
attach them to a console, and when the launching process has none of its own
(the desktopscheduler, Explorer, a GUI host) the system creates a brand new
console window for the child. That black box is what kept appearing on
2026-09-10, up to twice an hour, and it stayed on screen because the loop runs
until 17:00.

pythonw.exe is built as a *GUI-subsystem* binary, so Windows never creates a
console for it in the first place. Nothing to hide, nothing to flash.

Who starts it
-------------
  * the Windows scheduled task ``HKWeatherEdge-Watch`` - daily from 11:00,
    repeating every 30 minutes for 6 hours, run interactively as the user;
  * ``watch_silent.vbs`` - manual double-click from Explorer.

Because pythonw.exe is started *directly* by the scheduler and is not detached,
the task stays in state "Running" for as long as the loop does. That is what
makes the task's MultipleInstances=IgnoreNew meaningful - it degrades into a
watchdog that only restarts the loop if the previous one died. Launching via
wscript with a non-waiting Run() would instead leave a task that completes in
under a second, and every 30-minute trigger would stack another loop on top.

Output still goes to ``watch.log``, exactly as it did via ``watch.bat``.
"""
import datetime as dt
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))          # .../scripts
ROOT = os.path.dirname(HERE)                               # .../hk-weather-edge
sys.path.insert(0, HERE)

LOG = os.path.join(ROOT, "watch.log")
PIDFILE = os.path.join(ROOT, ".watch_loop.pid")

# Arguments are fixed here on purpose: a scheduled task should not depend on
# how someone happens to have quoted a command line. Override with
# ``watch_silent.py --watch --loop --loop-min 5 --until 18:00`` if needed.
ARGS = ["--watch", "--loop", "--loop-min", "10", "--until", "17:00"]


def _pid_alive(pid):
    """True if ``pid`` is a live process. Windows-only, no psutil dependency."""
    if not pid or pid <= 0:
        return False
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint]
        k.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        h = k.OpenProcess(0x1000, False, pid)              # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259              # STILL_ACTIVE
    except Exception:
        return False


def _acquire_lock(log):
    """Stop a second loop from starting while one is already running.

    The 30-minute scheduler trigger is already guarded by the task's
    IgnoreNew setting, but a manual double-click of watch_silent.vbs is not -
    and two loops would double every API call and interleave the log.
    """
    other = None
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            other = int((f.read().strip() or "0"))
    except Exception:
        other = None
    if other and other != os.getpid() and _pid_alive(other):
        log.write("[silent] a loop is already running (pid=%d) - nothing to do\n"
                  % other)
        return False
    with open(PIDFILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def _release_lock():
    try:
        with open(PIDFILE, encoding="utf-8") as f:
            mine = int((f.read().strip() or "0"))
        if mine == os.getpid():
            os.remove(PIDFILE)
    except Exception:
        pass


def _open_log():
    """Open watch.log for append, surviving a transient lock, never silently.

    ``watch.bat`` used shell redirection (``>> watch.log``). cmd.exe opens the
    redirect target *denying write sharing*, so while an old-style loop was
    alive nothing else could append to the log - measured on 2026-09-10 16:35,
    ``PermissionError [Errno 13]``. Python's own ``open()`` is share-friendly,
    so once every launcher goes through this file the contention disappears.
    The retries are for the transition period, and the fallback is a real file
    rather than os.devnull: a launcher that runs and records nothing is worse
    than one that fails loudly.
    """
    last = None
    for attempt in (0, 1, 2):
        try:
            return open(LOG, "a", encoding="utf-8", errors="replace", buffering=1)
        except OSError as e:
            last = e
            time.sleep(1.0 * (attempt + 1))
    alt = os.path.join(ROOT, "watch_err.log")
    try:
        f = open(alt, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        raise last
    f.write("[silent] cannot append to %s (%s); writing here instead\n"
            % (LOG, last))
    return f


def main():
    # Rebind the streams BEFORE hk_edge is imported. Under pythonw.exe both
    # sys.stdout and sys.stderr are None, and print() then silently discards
    # everything - the loop would run and record nothing.
    log = _open_log()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = log
    sys.stderr = log

    log.write("\n" + "=" * 72 + "\n")
    log.write("[silent] start %s  pid=%d  args=%s\n"
              % (dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), os.getpid(),
                 " ".join(sys.argv[1:]) or " ".join(ARGS)))

    if not _acquire_lock(log):
        log.flush()
        return 0

    old_argv = sys.argv
    sys.argv = ["hk_edge.py"] + (sys.argv[1:] or ARGS)
    rc = 0
    try:
        import hk_edge
        hk_edge.main()
    except SystemExit as e:
        rc = e.code if isinstance(e.code, int) else 0
    except BaseException:
        import traceback
        log.write("[silent] CRASH\n")
        traceback.print_exc(file=log)
        rc = 1
    finally:
        log.write("[silent] exit rc=%s %s\n"
                  % (rc, dt.datetime.now().strftime("%H:%M:%S")))
        sys.argv = old_argv
        _release_lock()
        try:
            log.flush()
            log.close()
        except Exception:
            pass
        # Detach the closed stream before the interpreter shuts down, otherwise
        # CPython's exit-time flush hits "ValueError: I/O operation on closed
        # file" and reports "lost sys.stdout / lost sys.stderr".
        sys.stdout, sys.stderr = old_out, old_err
    return rc


if __name__ == "__main__":
    sys.exit(main())
