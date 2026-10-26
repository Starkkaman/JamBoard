"""crew-hook body: redact one hook payload, append it to the spool, and on SessionStart make sure crew-hubd runs.

Rules: never raise, never wait for the daemon, print only a real hook decision. Imports stay minimal (json, os, sys,
time); the PermissionRequest path imports a little more.

PermissionRequest (docs/COMMANDS.md, bridge-tests §d/§e): the record gets a fresh requestId (and the questions of an
AskUserQuestion). When config.json commands.enabled is true and secrets.json has a commandSecret, the hook then waits
up to commands.holdSeconds for ~/.claude/crew-hub/answers/<requestId>.json (written by crew-hubd for a signed `answer`
command). The normal terminal/claude.ai prompt is shown at the same time; whichever answer comes first wins. On an
answer it prints the decision JSON, deletes the file and records "answered"; on timeout (or when Claude Code kills it
because the prompt was answered elsewhere) it records "unanswered" and prints nothing.
"""

import json
import os
import sys
import time

from . import paths
from .redact import redact

POLL_ANSWER = 0.25
ANSWER_MAX_BYTES = 64 * 1024
DENY_PREFIX = "Isaac denied this from Crew Hub"
QUESTION_TOOL = "AskUserQuestion"

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


# ---- PermissionRequest: answers from the hub ------------------------------------------------------------------------
def _uuid4():
    b = bytearray(os.urandom(16))
    b[6] = (b[6] & 0x0F) | 0x40
    b[8] = (b[8] & 0x3F) | 0x80
    h = b.hex()
    return "%s-%s-%s-%s-%s" % (h[:8], h[8:12], h[12:16], h[16:20], h[20:])


def _read_json(path, limit=1024 * 1024):
    try:
        with open(path, "rb") as f:
            d = json.loads(f.read(limit))
    except Exception:
        return None
    return d if isinstance(d, dict) else None


def commands_hold():
    """Seconds to hold for a hub answer: 0 unless commands.enabled is true and secrets.json has a commandSecret."""
    cfg = _read_json(paths.config_path())
    cmds = cfg.get("commands") if cfg else None
    if not isinstance(cmds, dict) or cmds.get("enabled") is not True:
        return 0.0
    sec = _read_json(paths.secrets_path(), 64 * 1024)
    cs = sec.get("commandSecret") if sec else None
    if not (isinstance(cs, str) and cs):
        return 0.0
    from .config import HOLD_DEFAULT, hold_seconds

    return hold_seconds(cmds.get("holdSeconds", HOLD_DEFAULT))


def read_answer(path, request_id):
    """The answer file for `request_id` as a dict, or None (missing, still being written, or invalid: then deleted)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        data = os.read(fd, ANSWER_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    ok = False
    try:
        d = json.loads(data.decode("utf-8"))
        ok = (
            len(data) <= ANSWER_MAX_BYTES
            and (not hasattr(os, "getuid") or st.st_uid == os.getuid())
            and isinstance(d, dict)
            and d.get("requestId") == request_id
            and d.get("decision") in ("allow", "deny")
        )
    except Exception:
        d = None
    if not ok:
        _unlink(path)
        return None
    return d


def _unlink(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def map_answers(tool_input, answers):
    """{question text: label} from the hub -> the same keyed by the full question text, labels mapped back from
    their clipped form. Unknown questions are dropped; free-text answers pass through."""
    from .redact import QUESTION_MAX, clip_text

    qs = tool_input.get("questions") if isinstance(tool_input, dict) else None
    if not isinstance(qs, list) or not isinstance(answers, dict):
        return {}
    out = {}
    for key, value in answers.items():
        if not isinstance(key, str) or not isinstance(value, str) or not value.strip():
            continue
        q = None
        for cand in qs:
            if isinstance(cand, dict) and isinstance(cand.get("question"), str):
                if key == cand["question"] or key == clip_text(cand["question"], QUESTION_MAX):
                    q = cand
                    break
        if q is None:
            continue
        labels = [o.get("label") for o in (q.get("options") or []) if isinstance(o, dict) and isinstance(o.get("label"), str)]

        def full(text):
            text = text.strip()
            for lab in labels:
                if text == lab or text == clip_text(lab, QUESTION_MAX):
                    return lab
            return text

        if q.get("multiSelect") is True and value.strip() not in labels:
            parts = [full(p) for p in value.split(",") if p.strip()]
            mapped = ", ".join(parts)
        else:
            mapped = full(value)
        if mapped:
            out[q["question"]] = mapped
    return out


def decision_output(payload, ans):
    """The verified PermissionRequest decision JSON for an answer, or None when the answer can't be used."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    if ans.get("decision") == "deny":
        msg = ans.get("message")
        msg = " ".join(msg.split())[:500] if isinstance(msg, str) else ""
        d = {"behavior": "deny", "message": "%s: %s" % (DENY_PREFIX, msg) if msg else DENY_PREFIX + "."}
    elif tool == QUESTION_TOOL:
        mapped = map_answers(tool_input, ans.get("answers"))
        if not mapped:
            return None  # allow without answers is ignored by Claude Code for questions
        updated = dict(tool_input)
        updated["answers"] = mapped
        d = {"behavior": "allow", "updatedInput": updated}
    else:
        d = {"behavior": "allow"}
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": d}}


def _followup(rec, ev, **extra):
    out = {"v": 1, "ts": round(time.time(), 3), "ev": ev, "rid": rec.get("rid")}
    for k in ("sid", "pid", "agent", "tool"):
        if rec.get(k):
            out[k] = rec[k]
    out.update(extra)
    try:
        append_spool(out)
    except Exception:
        pass


def permission_request(payload, rec):
    """Record the request; hold for a hub answer when enabled. Returns the decision text to print, or None."""
    rec["rid"] = _uuid4()
    if payload.get("tool_name") == QUESTION_TOOL:
        from .redact import questions

        qs = questions(payload.get("tool_input"))
        if qs:
            rec["questions"] = qs
    try:
        hold = commands_hold() if rec.get("sid") else 0.0
    except Exception:
        hold = 0.0
    rec["hold"] = hold > 0
    if hold > 0:
        rec["holdS"] = hold
    append_spool(rec)
    if hold <= 0:
        return None

    import signal

    state = {"done": False}

    def on_signal(signum, frame):
        if not state["done"]:
            state["done"] = True
            _followup(rec, "unanswered", why="ended")
        os._exit(0)

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), on_signal)
            except Exception:
                pass
    path = paths.answer_path(rec["rid"])
    end = time.monotonic() + hold
    while time.monotonic() < end:
        ans = read_answer(path, rec["rid"])
        if ans is not None:
            out = decision_output(payload, ans)
            _unlink(path)
            if out is not None:
                state["done"] = True
                _followup(rec, "answered", decision=ans.get("decision"))
                return json.dumps(out, ensure_ascii=False)
        time.sleep(POLL_ANSWER)
    state["done"] = True
    _followup(rec, "unanswered", why="timeout")
    return None


def run(raw):
    """Handle one hook payload. Returns text for stdout (a hook decision) or None."""
    try:
        payload = json.loads(raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    # Only Stop/SubagentStop carry a summary, so only they pay for reading config.json.
    ev = payload.get("hook_event_name")
    summaries = summaries_enabled() if ev in SUMMARY_EVENTS else True
    rec = redact(payload, round(time.time(), 3), claude_pid(), paths.plans_dir(), summaries=summaries)
    if rec.get("ev") == "PermissionRequest":
        return permission_request(payload, rec)
    append_spool(rec)
    if rec.get("ev") == "SessionStart" and os.environ.get("CREW_HUB_NO_DAEMON") != "1" and not daemon_alive():
        spawn_daemon()
    return None


def main():
    out = None
    try:
        raw = sys.stdin.buffer.read()
        out = run(raw)
    except BaseException:
        out = None
    try:
        # Nothing else may reach the model: discard anything that might have been buffered by accident.
        sys.stdout = sys.stderr = open(os.devnull, "w")
    except Exception:
        pass
    if isinstance(out, str) and out:
        try:
            data = (out + "\n").encode("utf-8")
            while data:
                n = os.write(1, data)
                data = data[n:]
        except Exception:
            pass
    os._exit(0)
