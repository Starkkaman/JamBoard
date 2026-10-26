"""crew-hook body: redact one hook payload, append it to the spool, and on SessionStart make sure crew-hubd runs.

Rules: never print, never raise, never wait for the daemon. Imports stay minimal (json, os, sys, time).
"""

import json
import os
import sys
import time

from . import paths
from .redact import redact

SHELLS = ("sh", "bash", "dash", "zsh", "ksh", "fish")


def _proc_stat(pid):
    """(comm, ppid) from /proc/<pid>/stat, or None where /proc is missing (macOS)."""
    try:
        with open("/proc/%d/stat" % pid, "rb") as f:
            data = f.read().decode("utf-8", "replace")
    except Exception:
        return None
    lp, rp = data.find("("), data.rfind(")")
    if lp < 0 or rp < 0:
        return None
    comm = data[lp + 1 : rp]
    rest = data[rp + 2 :].split()
    try:
        return comm, int(rest[1])
    except Exception:
        return None


def claude_pid():
    """The claude process that ran this hook: our parent, skipping a `sh -c` wrapper if the CLI used one."""
    pid = os.getppid()
    for _ in range(3):
        st = _proc_stat(pid)
        if not st or st[0] not in SHELLS or st[1] <= 1:
            break
        pid = st[1]
    return pid


def append_spool(rec):
    """One os.write on an O_APPEND fd: concurrent hooks can't interleave inside a line."""
    paths.ensure_hub_dir()
    line = (json.dumps(rec, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(paths.spool_path(), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)


def daemon_alive():
    try:
        with open(paths.pid_path(), "r") as f:
            pid = int(f.read().strip() or "0")
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    # Guard against pid reuse where /proc exists.
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as f:
            return b"crew-hubd" in f.read()
    except FileNotFoundError:
        return not os.path.isdir("/proc/self")
    except Exception:
        return True


def spawn_daemon():
    """Double fork + setsid, stdio to /dev/null; the parent only reaps the short-lived first child."""
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    daemon = os.path.join(here, "bin", "crew-hubd")
    if not os.path.isfile(daemon):
        return
    pid = os.fork()
    if pid:
        try:
            os.waitpid(pid, 0)
        except Exception:
            pass
        return
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        if devnull > 2:
            os.close(devnull)
        os.chdir("/")
        os.execv(sys.executable, [sys.executable, daemon])
    except Exception:
        pass
    os._exit(0)


SUMMARY_EVENTS = ("Stop", "SubagentStop")
OFF = ("false", "off", "no", "0")


def summaries_enabled():
    """config.json "summaries" (default true). Missing, unreadable or odd config means enabled."""
    try:
        with open(paths.config_path(), "rb") as f:
            v = json.loads(f.read(1024 * 1024)).get("summaries", True)
    except Exception:
        return True
    if v is False or (isinstance(v, int) and not isinstance(v, bool) and v == 0):
        return False
    return not (isinstance(v, str) and v.strip().lower() in OFF)


def run(raw):
    try:
        payload = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    except Exception:
        payload = {}
    # Only Stop/SubagentStop carry a summary, so only they pay for reading config.json.
    ev = payload.get("hook_event_name") if isinstance(payload, dict) else None
    summaries = summaries_enabled() if ev in SUMMARY_EVENTS else True
    rec = redact(payload, round(time.time(), 3), claude_pid(), paths.plans_dir(), summaries=summaries)
    append_spool(rec)
    if rec.get("ev") == "SessionStart" and os.environ.get("CREW_HUB_NO_DAEMON") != "1" and not daemon_alive():
        spawn_daemon()


def main():
    try:
        raw = sys.stdin.buffer.read()
        run(raw)
    except BaseException:
        pass
    try:
        # Nothing may reach the model: discard anything that might have been buffered by accident.
        sys.stdout = sys.stderr = open(os.devnull, "w")
    except Exception:
        pass
    os._exit(0)
