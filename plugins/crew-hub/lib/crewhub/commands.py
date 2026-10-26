"""Signed commands from Home Assistant (docs/COMMANDS.md is the contract; DESIGN.md §4-§5).

Flow: MqttLink.on_message (main thread) -> CommandHandler.receive: parse, HMAC check, retained/stale/replay/disabled
checks, argument validation. `answer` runs inline (one small file write that needs the model's waiting state);
every other action goes to one worker thread so the MQTT loop never stalls. Acks are queued and published by the main
thread (the MQTT client is single-threaded).

Never logged: the secret, message text, prompts, answers, peer tokens. Logged: action, id, ok/error code.
Subprocesses: argument lists only, never a shell.
"""

import hashlib
import hmac
import json
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import threading
import time

from . import inbox, paths

ACTIONS = ("send", "answer", "interrupt", "stop", "start")
WINDOW_MS = 60 * 1000
REPLAY_MS = 10 * 60 * 1000
REPLAY_MAX = 5000
TEXT_MAX = 4000
MESSAGE_MAX = 500
NAME_MAX = 60
PREFIX = "[From Isaac via Crew Hub] "
DENY_PREFIX = "Isaac denied this from Crew Hub"
ANSWERS_MAX = 4
ANSWER_VALUE_MAX = 1000
INTERRUPT_TIMEOUT = 10.0
STOP_TIMEOUT = 20.0
START_TIMEOUT = 60.0
ANSWERED_TTL = 15 * 60.0
QUEUE_MAX = 32

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
SIG_RE = re.compile(r"^[0-9a-fA-F]{64}$")
BGID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
BACKGROUNDED_RE = re.compile(r"backgrounded\s*\W\s*([0-9a-f]{6,64})\b")


class CommandError(Exception):
    """args[0] is the ack error code (short, safe to publish)."""

    @property
    def code(self):
        return self.args[0] if self.args else "error"


# ---- envelope ----------------------------------------------------------------------------------------------------
def canonical(env):
    body = {"v": env.get("v"), "id": env.get("id"), "ts": env.get("ts"), "action": env.get("action"), "args": env.get("args")}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(secret, env):
    return hmac.new(secret.encode("utf-8"), canonical(env), hashlib.sha256).hexdigest()


def _now_ms():
    return int(time.time() * 1000)


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


class Verifier(object):
    """Envelope checks. check() returns (env, None) when accepted, (env-or-None, code) when rejected; code None with
    env None means "drop silently" (unsigned / bad signature / unusable envelope)."""

    def __init__(self, secret, enabled, now_ms=_now_ms):
        self.secret = secret
        self.enabled = enabled
        self.now_ms = now_ms
        self.seen = {}  # id -> receive time (ms)

    def _prune(self, now):
        for k, t in list(self.seen.items()):
            if now - t > REPLAY_MS:
                del self.seen[k]
        while len(self.seen) > REPLAY_MAX:
            del self.seen[next(iter(self.seen))]

    def check(self, payload, retain):
        try:
            env = json.loads(payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else payload)
        except Exception:
            return None, None
        if not isinstance(env, dict):
            return None, None
        sig = env.get("sig")
        if not isinstance(sig, str) or not SIG_RE.match(sig):
            return None, None  # unsigned
        if not self.secret:
            return None, None  # can't verify anything without a secret
        try:
            want = sign(self.secret, env)
        except Exception:
            return None, None
        if not hmac.compare_digest(want, sig.lower()):
            return None, None  # bad signature
        cid = env.get("id")
        if not (isinstance(cid, str) and UUID_RE.match(cid)):
            return None, None  # signed but unusable id: nothing to ack against
        if retain:
            return env, "retained"
        if env.get("v") != 1:
            return env, "bad_version"
        if not self.enabled:
            return env, "disabled"
        now = self.now_ms()
        ts = env.get("ts")
        if not _int(ts) or abs(now - ts) > WINDOW_MS:
            return env, "stale"
        self._prune(now)
        if cid in self.seen:
            return env, "replay"
        self.seen[cid] = now
        if env.get("action") not in ACTIONS:
            return env, "unknown_action"
        if not isinstance(env.get("args"), dict):
            return env, "bad_args"
        return env, None


# ---- argument validation -----------------------------------------------------------------------------------------
def _text(args, key, limit, required=True, min_len=1):
    v = args.get(key)
    if v is None and not required:
        return None
    if not isinstance(v, str) or "\x00" in v or len(v) > limit or len(v.strip()) < min_len:
        raise CommandError("bad_args:%s" % key)
    return v


def _session(args):
    v = args.get("session")
    if not isinstance(v, str) or not SESSION_RE.match(v):
        raise CommandError("bad_args:session")
    return v


def validate(action, args, cmds):
    """Clean args for `action` or CommandError. `cmds` is config.Commands (allowlists)."""
    if not isinstance(args, dict):
        raise CommandError("bad_args")
    if action == "send":
        urgent = args.get("urgent", False)
        if not isinstance(urgent, bool):
            raise CommandError("bad_args:urgent")
        return {"session": _session(args), "text": _text(args, "text", TEXT_MAX), "urgent": urgent}
    if action == "answer":
        rid = args.get("requestId")
        if not isinstance(rid, str) or not UUID_RE.match(rid):
            raise CommandError("bad_args:requestId")
        decision = args.get("decision")
        if decision not in ("allow", "deny"):
            raise CommandError("bad_args:decision")
        message = _text(args, "message", MESSAGE_MAX, required=False, min_len=0)
        answers = args.get("answers")
        if answers is not None:
            if not isinstance(answers, dict) or len(answers) > ANSWERS_MAX:
                raise CommandError("bad_args:answers")
            for k, v in answers.items():
                if not (isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()):
                    raise CommandError("bad_args:answers")
                if len(k) > ANSWER_VALUE_MAX or len(v) > ANSWER_VALUE_MAX or "\x00" in k or "\x00" in v:
                    raise CommandError("bad_args:answers")
        out = {"session": _session(args), "requestId": rid.lower(), "decision": decision}
        if message:
            out["message"] = message.strip()
        if answers:
            out["answers"] = dict(answers)
        return out
    if action in ("interrupt", "stop"):
        return {"session": _session(args)}
    if action == "start":
        folder = args.get("folder")
        if not isinstance(folder, str) or folder not in cmds.folders:
            raise CommandError("unknown_folder")
        model = args.get("model")
        if not isinstance(model, str) or model not in cmds.models:
            raise CommandError("model_not_allowed")
        mode = args.get("permissionMode")
        if not isinstance(mode, str) or mode not in cmds.permission_modes:
            raise CommandError("mode_not_allowed")
        name = _text(args, "name", NAME_MAX)
        name = name.strip()
        if name.startswith("-") or any(ord(c) < 32 or ord(c) == 127 for c in name):
            raise CommandError("bad_args:name")
        prompt = _text(args, "prompt", TEXT_MAX)
        return {"folder": folder, "model": model, "permissionMode": mode, "name": name, "prompt": prompt}
    raise CommandError("unknown_action")


# ---- helpers -----------------------------------------------------------------------------------------------------
def find_claude():
    found = shutil.which("claude")
    if found:
        return found
    cand = os.path.join(os.path.expanduser("~"), ".local", "bin", "claude")
    return cand if os.path.isfile(cand) else "claude"


def is_trusted(path, claude_json=None):
    """projects[path].hasTrustDialogAccepted in ~/.claude.json (read-only, exact path)."""
    try:
        with open(claude_json or paths.claude_json_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    projects = data.get("projects") if isinstance(data, dict) else None
    entry = projects.get(path) if isinstance(projects, dict) else None
    return isinstance(entry, dict) and entry.get("hasTrustDialogAccepted") is True


def write_private_atomic(path, data):
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_start_argv(claude, a):
    """`claude --bg --remote-control <name> -n <name> --permission-mode <m> --model <model> -- <prompt>`.

    `--` ends option parsing so a prompt starting with "-" stays a prompt; the name never starts with "-"."""
    return [
        claude, "--bg", "--remote-control", a["name"], "-n", a["name"],
        "--permission-mode", a["permissionMode"], "--model", a["model"], "--", a["prompt"],
    ]


def parse_bg_id(output):
    m = BACKGROUNDED_RE.search(ANSI_RE.sub("", output or ""))
    return m.group(1) if m else None


def pty_escape(argv, timeout=INTERRUPT_TIMEOUT, settle=0.4, first_output=6.0, after=1.0):
    """Run argv (`claude attach <id>`) in a pty, wait for its first screen output to settle, send ONE ESC, read for
    `after` seconds, then close the client. Returns True when the ESC was sent. Never sends a second ESC."""
    import fcntl
    import pty
    import struct
    import termios

    deadline = time.time() + timeout
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.environ["TERM"] = "xterm-256color"
            os.environ["COLUMNS"] = "120"
            os.environ["LINES"] = "40"
            os.execvp(argv[0], argv)
        except BaseException:
            pass
        os._exit(127)
    sent = False
    try:
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        except OSError:
            pass

        def pump(until, stop_when_quiet=None):
            got = False
            last = None
            while time.time() < min(until, deadline):
                wait = 0.1
                r, _, _ = select.select([fd], [], [], wait)
                if r:
                    try:
                        data = os.read(fd, 65536)
                    except OSError:
                        return got, True
                    if not data:
                        return got, True
                    got = True
                    last = time.time()
                elif stop_when_quiet and got and last and time.time() - last >= stop_when_quiet:
                    break
            return got, False

        got, eof = pump(time.time() + first_output, stop_when_quiet=settle)
        if got and not eof and time.time() < deadline:
            os.write(fd, b"\x1b")
            sent = True
            pump(time.time() + after)
    finally:
        for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                break
            end = time.time() + 0.5
            done = False
            while time.time() < end:
                try:
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        done = True
                        break
                except ChildProcessError:
                    done = True
                    break
                time.sleep(0.05)
            if done:
                break
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass
    return sent


# ---- executor ----------------------------------------------------------------------------------------------------
class Executor(object):
    """Runs validated actions. Every method returns a result dict or raises CommandError.

    `waiting(session, requestId)` (from the daemon's model) answers "ok" | "not_waiting" | "no_hold" | ("ok", kind)."""

    def __init__(self, cmds, sessions_dir=None, claude=None, waiting=None, run=subprocess.run, pty_escape=pty_escape,
                 claude_json=None, log=None):
        self.cmds = cmds
        self.sessions_dir = sessions_dir or paths.sessions_dir()
        self.claude = claude or find_claude()
        self.waiting = waiting or (lambda session, rid: ("not_waiting", None))
        self.run_proc = run
        self.pty_escape = pty_escape
        self.claude_json = claude_json
        self.log = log or (lambda msg: None)
        self.answered = {}  # requestId -> time
        self.lock = threading.Lock()

    def execute(self, action, a):
        return getattr(self, "do_" + action)(a)

    # send ------------------------------------------------------------------------------------------------------
    def do_send(self, a):
        entry = inbox.find_live(self.sessions_dir, a["session"])
        if entry is None:
            raise CommandError("not_live")
        try:
            inbox.send_frame(self.sessions_dir, entry, inbox.user_frame(PREFIX + a["text"], a["urgent"]))
        except inbox.InboxError as e:
            raise CommandError(e.args[0] if e.args else "send_failed")
        return {}

    # answer ----------------------------------------------------------------------------------------------------
    def do_answer(self, a):
        rid = a["requestId"]
        now = time.time()
        with self.lock:
            for k, t in list(self.answered.items()):
                if now - t > ANSWERED_TTL:
                    del self.answered[k]
            if rid in self.answered:
                raise CommandError("already_answered")
            state = self.waiting(a["session"], rid)
            status, kind = state if isinstance(state, tuple) else (state, None)
            if status != "ok":
                raise CommandError(status)
            if kind == "question" and a["decision"] == "allow" and not a.get("answers"):
                raise CommandError("bad_args:answers")
            path = paths.answer_path(rid)
            if os.path.lexists(path):
                raise CommandError("already_answered")
            body = {"v": 1, "requestId": rid, "session": a["session"], "decision": a["decision"], "ts": int(now * 1000)}
            if a.get("message"):
                body["message"] = a["message"]
            if a.get("answers") and kind != "permission":
                body["answers"] = a["answers"]
            paths.ensure_answers_dir()
            write_private_atomic(path, json.dumps(body, ensure_ascii=False).encode("utf-8"))
            self.answered[rid] = now
        return {}

    # interrupt / stop ------------------------------------------------------------------------------------------
    def _bg_entry(self, session):
        entry = inbox.find_live(self.sessions_dir, session)
        if entry is None:
            raise CommandError("not_live")
        if inbox.entry_kind(entry) != "bg":
            raise CommandError("not_bg")
        bg = entry.get("jobId")
        if not isinstance(bg, str) or not BGID_RE.match(bg):
            raise CommandError("no_bg_id")
        return entry, bg

    def do_interrupt(self, a):
        _, bg = self._bg_entry(a["session"])
        if not self.pty_escape([self.claude, "attach", bg]):
            raise CommandError("attach_failed")
        return {}

    def do_stop(self, a):
        _, bg = self._bg_entry(a["session"])
        try:
            r = self.run_proc([self.claude, "stop", bg], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=STOP_TIMEOUT, start_new_session=True)
        except subprocess.TimeoutExpired:
            raise CommandError("timeout")
        except OSError:
            raise CommandError("stop_failed")
        if r.returncode != 0:
            raise CommandError("stop_failed")
        return {}

    # start -----------------------------------------------------------------------------------------------------
    def do_start(self, a):
        preset = self.cmds.folders.get(a["folder"])
        if preset is None:
            raise CommandError("unknown_folder")
        path = preset["path"]
        if not os.path.isdir(path):
            raise CommandError("folder_missing")
        if not is_trusted(path, self.claude_json):
            raise CommandError("folder_untrusted")
        argv = build_start_argv(self.claude, a)
        try:
            r = self.run_proc(argv, cwd=path, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              timeout=START_TIMEOUT, start_new_session=True)
        except subprocess.TimeoutExpired:
            raise CommandError("timeout")
        except OSError:
            raise CommandError("start_failed")
        out = r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else (r.stdout or "")
        err = r.stderr.decode("utf-8", "replace") if isinstance(r.stderr, bytes) else (r.stderr or "")
        bg = parse_bg_id(out) or parse_bg_id(err)
        if r.returncode != 0 or not bg:
            raise CommandError("start_failed")
        result = {"bgId": bg}
        sid = self._session_for_job(bg)
        if sid:
            result["sessionId"] = sid
        return result

    def _session_for_job(self, bg, wait=3.0):
        end = time.time() + wait
        while True:
            for d in inbox.read_entries(self.sessions_dir):
                if d.get("jobId") == bg and isinstance(d.get("sessionId"), str):
                    return d["sessionId"][:100]
            if time.time() >= end:
                return None
            time.sleep(0.25)


# ---- handler -----------------------------------------------------------------------------------------------------
class CommandHandler(object):
    """Glue for the daemon: receive() from the MQTT callback, acks() drained by the main loop."""

    def __init__(self, cmds, log, executor=None, now_ms=_now_ms):
        self.cmds = cmds
        self.log = log
        self.verifier = Verifier(cmds.secret, cmds.active, now_ms)
        self.executor = executor or Executor(cmds, log=log)
        self.ack_queue = queue.Queue()
        self.jobs = queue.Queue(QUEUE_MAX)
        self.worker = None

    def _ack(self, cid, action, ok, error=None, result=None):
        body = {"id": cid, "ok": bool(ok)}
        if error:
            body["error"] = str(error)[:80]
        if result:
            body["result"] = result
        self.ack_queue.put(body)
        self.log("cmd %s id=%s %s" % (action, cid, "ok" if ok else "error " + str(error)[:80]))

    def receive(self, payload, retain):
        env, code = self.verifier.check(payload, retain)
        if env is None:
            self.log("cmd dropped (%d bytes): unsigned, bad signature or unusable envelope" % len(payload or b""))
            return
        cid = env["id"]
        action = env.get("action") if isinstance(env.get("action"), str) else "?"
        action = action[:20] if action in ACTIONS else "?"
        if code:
            self._ack(cid, action, False, code)
            return
        try:
            a = validate(action, env["args"], self.cmds)
        except CommandError as e:
            self._ack(cid, action, False, e.code)
            return
        if action == "answer":
            self._run(cid, action, a)  # inline: tiny, and needs the model's state from this thread
            return
        self._ensure_worker()
        try:
            self.jobs.put_nowait((cid, action, a))
        except queue.Full:
            self._ack(cid, action, False, "busy")

    def _run(self, cid, action, a):
        try:
            result = self.executor.execute(action, a)
        except CommandError as e:
            self._ack(cid, action, False, e.code)
        except Exception as e:
            self._ack(cid, action, False, "internal:%s" % type(e).__name__)
        else:
            self._ack(cid, action, True, result=result or None)

    def _ensure_worker(self):
        if self.worker is not None and self.worker.is_alive():
            return
        self.worker = threading.Thread(target=self._work, name="crew-cmd", daemon=True)
        self.worker.start()

    def _work(self):
        while True:
            item = self.jobs.get()
            if item is None:
                return
            self._run(*item)

    def acks(self):
        out = []
        while True:
            try:
                out.append(self.ack_queue.get_nowait())
            except queue.Empty:
                return out

    def close(self, wait=2.0):
        if self.worker is not None and self.worker.is_alive():
            try:
                self.jobs.put_nowait(None)
            except queue.Full:
                pass
            self.worker.join(wait)
