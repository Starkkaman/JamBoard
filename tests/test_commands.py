"""Step 5a: signed commands (docs/COMMANDS.md), the executor, and the PermissionRequest answer round trip."""

import hashlib
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid

import _util
from crewhub import commands, config, inbox, paths
from crewhub.commands import CommandError, CommandHandler, Executor, Verifier, sign, validate
from crewhub.state import Model

SECRET = "x" * 32
SID = "7c1addf5-73c5-41d7-9225-77ba4bbbb2d5"


def envelope(action, args, secret=SECRET, ts=None, cid=None):
    env = {"v": 1, "id": cid or str(uuid.uuid4()), "ts": ts if ts is not None else int(time.time() * 1000),
           "action": action, "args": args}
    env["sig"] = sign(secret, env)
    return json.dumps(env).encode("utf-8")


def cmds_config(extra=None, secret=SECRET):
    data = {"enabled": True, "holdSeconds": 5}
    data.update(extra or {})
    return config.Commands(data, secret)


def wait(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.05)
    return False


class TempHome(unittest.TestCase):
    """os.environ HOME -> a temp dir for the test (paths resolve HOME at call time)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        self.hub = paths.ensure_hub_dir()
        self.sessions = paths.sessions_dir()
        os.makedirs(self.sessions, exist_ok=True)

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self._old_home
        self.tmp.cleanup()

    def write_json(self, path, data, mode=0o600):
        with open(path, "w") as f:
            json.dump(data, f)
        os.chmod(path, mode)

    def registry(self, pid=None, kind="interactive", job=None, sock=None, key=True, proc_start=None, sid=SID):
        pid = pid or os.getpid()
        ps = proc_start if proc_start is not None else inbox.proc_start(pid)
        entry = {"pid": pid, "sessionId": sid, "procStart": ps, "kind": kind,
                 "messagingSocketPath": sock or os.path.join(self.home, "s.sock")}
        if job:
            entry["jobId"] = job
        self.write_json(os.path.join(self.sessions, "%d.json" % pid), entry)
        if key:
            digest = hashlib.sha256(entry["messagingSocketPath"].encode()).hexdigest()
            self.write_json(os.path.join(self.sessions, "%d.%s.key" % (pid, digest)),
                            {"peerToken": "tok-" + "a" * 28, "procStart": ps})
        return entry


class VerifierTest(unittest.TestCase):
    def setUp(self):
        self.v = Verifier(SECRET, True)

    def test_good_signature_accepted_once(self):
        raw = envelope("stop", {"session": SID})
        env, code = self.v.check(raw, False)
        self.assertIsNone(code)
        self.assertEqual(env["action"], "stop")
        env2, code2 = self.v.check(raw, False)
        self.assertEqual(code2, "replay")
        self.assertEqual(env2["id"], env["id"])

    def test_bad_signature_and_unsigned_are_dropped_without_ack(self):
        self.assertEqual(self.v.check(envelope("stop", {"session": SID}, secret="y" * 32), False), (None, None))
        d = json.loads(envelope("stop", {"session": SID}))
        d["args"]["session"] = "other"  # tampered after signing
        self.assertEqual(self.v.check(json.dumps(d).encode(), False), (None, None))
        del d["sig"]
        self.assertEqual(self.v.check(json.dumps(d).encode(), False), (None, None))
        self.assertEqual(self.v.check(b"not json", False), (None, None))
        self.assertEqual(Verifier(None, True).check(envelope("stop", {"session": SID}), False), (None, None))

    def test_stale_both_directions(self):
        now = int(time.time() * 1000)
        self.assertEqual(self.v.check(envelope("stop", {"session": SID}, ts=now - 61000), False)[1], "stale")
        self.assertEqual(self.v.check(envelope("stop", {"session": SID}, ts=now + 61000), False)[1], "stale")
        self.assertIsNone(self.v.check(envelope("stop", {"session": SID}, ts=now - 50000), False)[1])

    def test_retained_disabled_unknown(self):
        self.assertEqual(self.v.check(envelope("stop", {"session": SID}), True)[1], "retained")
        self.assertEqual(Verifier(SECRET, False).check(envelope("stop", {"session": SID}), False)[1], "disabled")
        self.assertEqual(self.v.check(envelope("shell", {"cmd": "x"}), False)[1], "unknown_action")

    def test_canonical_form(self):
        env = {"v": 1, "id": "i", "ts": 5, "action": "send", "args": {"text": "é", "b": 1}, "sig": "zz"}
        self.assertEqual(commands.canonical(env),
                         '{"action":"send","args":{"b":1,"text":"é"},"id":"i","ts":5,"v":1}'.encode("utf-8"))


class ValidateTest(unittest.TestCase):
    def setUp(self):
        self.c = cmds_config({"folders": {"proj": {"path": "/tmp", "label": "Projects"}}})

    def bad(self, action, args, code):
        with self.assertRaises(CommandError) as cm:
            validate(action, args, self.c)
        self.assertEqual(cm.exception.code, code)

    def test_send(self):
        self.assertEqual(validate("send", {"session": SID, "text": "hi"}, self.c),
                         {"session": SID, "text": "hi", "urgent": False})
        self.bad("send", {"session": SID, "text": "x" * 4001}, "bad_args:text")
        self.bad("send", {"session": SID, "text": "  "}, "bad_args:text")
        self.bad("send", {"session": SID, "text": "a", "urgent": "yes"}, "bad_args:urgent")
        self.bad("send", {"session": "../x", "text": "a"}, "bad_args:session")

    def test_answer(self):
        rid = str(uuid.uuid4())
        ok = validate("answer", {"session": SID, "requestId": rid, "decision": "deny", "message": "no"}, self.c)
        self.assertEqual(ok["message"], "no")
        self.bad("answer", {"session": SID, "requestId": "../../x", "decision": "allow"}, "bad_args:requestId")
        self.bad("answer", {"session": SID, "requestId": rid, "decision": "maybe"}, "bad_args:decision")
        self.bad("answer", {"session": SID, "requestId": rid, "decision": "deny", "message": "m" * 501},
                 "bad_args:message")
        self.bad("answer", {"session": SID, "requestId": rid, "decision": "allow", "answers": {"q": 1}},
                 "bad_args:answers")
        self.bad("answer", {"session": SID, "requestId": rid, "decision": "allow",
                            "answers": {str(i): "a" for i in range(5)}}, "bad_args:answers")

    def test_start(self):
        good = {"folder": "proj", "model": "claude-haiku-5-5", "permissionMode": "default", "name": "t", "prompt": "p"}
        self.assertEqual(validate("start", good, self.c)["folder"], "proj")
        self.bad("start", dict(good, folder="/etc"), "unknown_folder")
        self.bad("start", dict(good, model="gpt"), "model_not_allowed")
        self.bad("start", dict(good, permissionMode="bypassPermissions"), "mode_not_allowed")
        self.bad("start", dict(good, name="--dangerously-skip-permissions"), "bad_args:name")
        self.bad("start", dict(good, name="x" * 61), "bad_args:name")
        self.bad("start", dict(good, prompt="p" * 4001), "bad_args:prompt")
        self.bad("start", dict(good, prompt="a\x00b"), "bad_args:prompt")

    def test_interrupt_stop(self):
        self.assertEqual(validate("stop", {"session": SID}, self.c), {"session": SID})
        self.bad("interrupt", {}, "bad_args:session")


class FakeInbox(object):
    """Unix socket server: reads one connection to EOF, records it, closes without a reply."""

    def __init__(self, path, reply=b""):
        self.path = path
        self.reply = reply
        self.data = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(path)
        self.srv.listen(2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        with conn:
            buf = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            self.data.append(buf)
            if self.reply:
                conn.sendall(self.reply)

    def frames(self):
        return [json.loads(line) for line in self.data[0].decode().splitlines()]

    def close(self):
        self.srv.close()


class SendTest(TempHome):
    def test_send_delivers_auth_and_user_frame(self):
        sock = os.path.join(self.home, "s.sock")
        srv = FakeInbox(sock)
        try:
            self.registry(sock=sock)
            ex = Executor(cmds_config(), sessions_dir=self.sessions)
            self.assertEqual(ex.do_send({"session": SID, "text": "status?", "urgent": True}), {})
            self.assertTrue(wait(lambda: srv.data))
            auth, frame = srv.frames()
            self.assertEqual(auth, {"type": "auth", "token": "tok-" + "a" * 28})
            self.assertEqual(frame, {"type": "user", "priority": "now", "from": "crew-hub",
                                     "message": {"role": "user", "content": "[From Isaac via Crew Hub] status?"}})
        finally:
            srv.close()

    def test_send_next_priority_and_refusal(self):
        sock = os.path.join(self.home, "s.sock")
        srv = FakeInbox(sock, reply=b'{"error":"bad token"}')
        try:
            self.registry(sock=sock)
            ex = Executor(cmds_config(), sessions_dir=self.sessions)
            with self.assertRaises(CommandError) as cm:
                ex.do_send({"session": SID, "text": "x", "urgent": False})
            self.assertEqual(cm.exception.code, "refused")
            self.assertEqual(srv.frames()[1]["priority"], "next")
        finally:
            srv.close()

    def test_lookup_rules(self):
        ex = Executor(cmds_config(), sessions_dir=self.sessions)
        # reused pid: registry procStart differs from /proc
        self.registry(proc_start="1")
        with self.assertRaises(CommandError) as cm:
            ex.do_send({"session": SID, "text": "x", "urgent": False})
        self.assertEqual(cm.exception.code, "not_live")
        # live entry, but the key file belongs to an older process with this pid
        entry = self.registry()
        digest = hashlib.sha256(entry["messagingSocketPath"].encode()).hexdigest()
        self.write_json(os.path.join(self.sessions, "%d.%s.key" % (os.getpid(), digest)),
                        {"peerToken": "t", "procStart": "1"})
        with self.assertRaises(CommandError) as cm:
            ex.do_send({"session": SID, "text": "x", "urgent": False})
        self.assertEqual(cm.exception.code, "no_inbox")
        # unknown session
        with self.assertRaises(CommandError) as cm:
            ex.do_send({"session": "nope", "text": "x", "urgent": False})
        self.assertEqual(cm.exception.code, "not_live")


class BgActionsTest(TempHome):
    def test_stop_and_interrupt_argv(self):
        self.registry(kind="bg", job="ab12cd34")
        calls = []

        def fake_run(argv, **kw):
            calls.append((argv, kw))
            return subprocess.CompletedProcess(argv, 0, b"stopped ab12cd34\n", b"")

        escapes = []
        ex = Executor(cmds_config(), sessions_dir=self.sessions, claude="/opt/claude", run=fake_run,
                      pty_escape=lambda argv: escapes.append(argv) or True)
        self.assertEqual(ex.do_stop({"session": SID}), {})
        self.assertEqual(calls[0][0], ["/opt/claude", "stop", "ab12cd34"])
        self.assertNotIn("shell", calls[0][1])
        self.assertEqual(ex.do_interrupt({"session": SID}), {})
        self.assertEqual(escapes, [["/opt/claude", "attach", "ab12cd34"]])

    def test_interactive_sessions_refused(self):
        self.registry(kind="interactive")
        ex = Executor(cmds_config(), sessions_dir=self.sessions, run=None, pty_escape=None)
        for fn in (ex.do_stop, ex.do_interrupt):
            with self.assertRaises(CommandError) as cm:
                fn({"session": SID})
            self.assertEqual(cm.exception.code, "not_bg")

    def test_stop_failure(self):
        self.registry(kind="bg", job="ab12cd34")
        ex = Executor(cmds_config(), sessions_dir=self.sessions, claude="/opt/claude",
                      run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, b"", b"no such"))
        with self.assertRaises(CommandError) as cm:
            ex.do_stop({"session": SID})
        self.assertEqual(cm.exception.code, "stop_failed")

    def test_pty_escape_sends_exactly_one_escape(self):
        out = os.path.join(self.home, "keys.bin")
        script = os.path.join(self.home, "fake-claude")
        with open(script, "w") as f:
            f.write("#!%s\n" % sys.executable + "\n".join([
                "import os, sys, select, time, tty",
                "tty.setraw(0)",
                "os.write(1, b'\\x1b[2Jfake attach screen\\r\\n')",
                "got = b''",
                "end = time.time() + 3",
                "while time.time() < end:",
                "    r, _, _ = select.select([0], [], [], 0.1)",
                "    if r:",
                "        got += os.read(0, 64)",
                "    open(%r, 'wb').write(got)" % out,
            ]) + "\n")
        os.chmod(script, 0o700)
        t0 = time.time()
        self.assertTrue(commands.pty_escape([script, "attach", "ab12cd34"], timeout=10))
        self.assertLess(time.time() - t0, 10)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"\x1b")

    def test_pty_escape_without_output_sends_nothing(self):
        script = os.path.join(self.home, "silent")
        with open(script, "w") as f:
            f.write("#!/bin/sh\nexec sleep 30\n")
        os.chmod(script, 0o700)
        t0 = time.time()
        self.assertFalse(commands.pty_escape([script], timeout=2, first_output=1.0))
        self.assertLess(time.time() - t0, 5)


class StartTest(TempHome):
    def setUp(self):
        super().setUp()
        self.proj = os.path.join(self.home, "proj")
        os.mkdir(self.proj)
        self.cj = os.path.join(self.home, "claude.json")
        self.write_json(self.cj, {"projects": {self.proj: {"hasTrustDialogAccepted": True}}})
        self.c = cmds_config({"folders": {"proj": {"path": self.proj, "label": "Proj"},
                                          "gone": {"path": os.path.join(self.home, "gone"), "label": "Gone"},
                                          "home": {"path": self.home, "label": "Home"}}})
        self.calls = []

    def fake_run(self, argv, **kw):
        self.calls.append((argv, kw))
        # the CLI's real output, with colour codes
        return subprocess.CompletedProcess(argv, 0, "\x1b[32mbackgrounded\x1b[0m · 9f8e7d6c · t1\n".encode(), b"")

    def args(self, **kw):
        a = {"folder": "proj", "model": "claude-haiku-5-5", "permissionMode": "default", "name": "t1",
             "prompt": "--help me"}
        a.update(kw)
        return validate("start", a, self.c)

    def test_exact_argv_and_cwd(self):
        self.write_json(os.path.join(self.sessions, "1.json"), {"pid": 1, "sessionId": "s-1", "jobId": "9f8e7d6c"})
        ex = Executor(self.c, sessions_dir=self.sessions, claude="/opt/claude", run=self.fake_run, claude_json=self.cj)
        self.assertEqual(ex.do_start(self.args()), {"bgId": "9f8e7d6c", "sessionId": "s-1"})
        argv, kw = self.calls[0]
        self.assertEqual(argv, ["/opt/claude", "--bg", "--remote-control", "t1", "-n", "t1", "--permission-mode",
                                "default", "--model", "claude-haiku-5-5", "--", "--help me"])
        self.assertEqual(kw["cwd"], self.proj)
        self.assertTrue(kw["start_new_session"])
        self.assertNotIn("shell", kw)

    def test_folder_checks(self):
        ex = Executor(self.c, sessions_dir=self.sessions, claude="/opt/claude", run=self.fake_run, claude_json=self.cj)
        for folder, code in (("gone", "folder_missing"), ("home", "folder_untrusted")):
            with self.assertRaises(CommandError) as cm:
                ex.do_start(self.args(folder=folder))
            self.assertEqual(cm.exception.code, code)
        self.assertEqual(self.calls, [])

    def test_unparseable_output_is_an_error(self):
        ex = Executor(self.c, sessions_dir=self.sessions, claude="/opt/claude", claude_json=self.cj,
                      run=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, b"something else", b""))
        with self.assertRaises(CommandError) as cm:
            ex.do_start(self.args())
        self.assertEqual(cm.exception.code, "start_failed")


def read_text(path):
    with open(path) as f:
        return f.read()


def collect(p, timeout):
    """Wait for a Popen whose stdin was already closed; (out, err), pipes closed."""
    p.wait(timeout)
    out, err = p.stdout.read(), p.stderr.read()
    p.stdout.close()
    p.stderr.close()
    return out, err


def hook_env(home):
    env = dict(os.environ, HOME=home, CREW_HUB_NO_DAEMON="1")
    return env


def spool_records(home):
    try:
        with open(os.path.join(home, ".claude", "crew-hub", "spool.jsonl")) as f:
            return [json.loads(line) for line in f if line.strip()]
    except FileNotFoundError:
        return []


PERM = {"session_id": SID, "transcript_path": "/x/t.jsonl", "cwd": "/home/u/project", "prompt_id": "p",
        "permission_mode": "default", "hook_event_name": "PermissionRequest", "tool_name": "Write",
        "tool_input": {"file_path": "/home/u/secret/out.txt", "content": "TOPSECRET"},
        "permission_suggestions": []}
LONG_Q = "Which drink would you like with lunch today? " * 6  # > 200 chars: the hub sees it clipped
ASK = {"session_id": SID, "transcript_path": "/x/t.jsonl", "cwd": "/home/u/project", "prompt_id": "p",
       "permission_mode": "default", "hook_event_name": "PermissionRequest", "tool_name": "AskUserQuestion",
       "tool_input": {"questions": [{"question": LONG_Q, "header": "Drink", "multiSelect": False,
                                     "options": [{"label": "Tea", "description": "hot"},
                                                 {"label": "Coffee", "description": "TOPSECRET desc"}]}]}}


class AnswerRoundTripTest(TempHome):
    def setUp(self):
        super().setUp()
        self.write_json(paths.config_path(), {"commands": {"enabled": True, "holdSeconds": 8}})
        self.write_json(paths.secrets_path(), {"commandSecret": SECRET})
        self.model = Model()
        self.logs = []
        self.handler = CommandHandler(config.load().commands, self.logs.append,
                                      executor=Executor(config.load().commands, sessions_dir=self.sessions,
                                                        waiting=self.model.waiting_state))
        self.proc = None

    def tearDown(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if self.proc and not self.proc.stdout.closed:
            self.proc.stdout.close()
            self.proc.stderr.close()
        super().tearDown()

    def start_hook(self, payload):
        self.proc = subprocess.Popen([_util.HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=hook_env(self.home))
        self.proc.stdin.write(json.dumps(payload).encode())
        self.proc.stdin.close()
        self.assertTrue(wait(lambda: any(r.get("ev") == "PermissionRequest" for r in spool_records(self.home))))
        rec = [r for r in spool_records(self.home) if r.get("ev") == "PermissionRequest"][-1]
        self.model.apply(rec)
        return rec

    def answer(self, args):
        self.handler.receive(envelope("answer", args), False)
        acks = self.handler.acks()
        self.assertEqual(len(acks), 1)
        return acks[0]

    def finish(self):
        out, err = collect(self.proc, 10)
        self.assertEqual(self.proc.returncode, 0)
        self.assertEqual(err, b"")
        return out

    def test_permission_deny_round_trip(self):
        rec = self.start_hook(PERM)
        self.assertTrue(rec["hold"])
        self.assertEqual(rec["file"], "out.txt")
        rid = rec["rid"]
        snap = self.model.snapshot({"id": "m"}, commands_active=True)["sessions"][0]
        self.assertEqual(snap["status"], "waiting")
        self.assertEqual(snap["waiting"]["requestId"], rid)
        self.assertEqual(snap["waiting"]["kind"], "permission")
        self.assertTrue(snap["waiting"]["hubCanAnswer"])
        self.assertIn("answer", snap["controls"])
        ack = self.answer({"session": SID, "requestId": rid, "decision": "deny", "message": "not now"})
        self.assertTrue(ack["ok"], ack)
        afile = paths.answer_path(rid)
        self.assertEqual(stat.S_IMODE(os.stat(paths.answers_dir()).st_mode), 0o700)
        out = self.finish()
        self.assertEqual(json.loads(out), {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": {
            "behavior": "deny", "message": "Isaac denied this from Crew Hub: not now"}}})
        self.assertFalse(os.path.exists(afile))
        recs = spool_records(self.home)
        self.assertEqual(recs[-1]["ev"], "answered")
        self.assertEqual(recs[-1]["rid"], rid)
        # second answer: already answered; the fold of "answered" clears the waiting state
        again = self.answer({"session": SID, "requestId": rid, "decision": "allow"})
        self.assertEqual(again["error"], "already_answered")
        self.model.apply(recs[-1])
        s = self.model.snapshot({"id": "m"}, commands_active=True)["sessions"][0]
        self.assertIsNone(s["waiting"])
        self.assertEqual(s["status"], "working")
        raw = read_text(os.path.join(self.hub, "spool.jsonl"))
        self.assertNotIn("TOPSECRET", raw)
        self.assertNotIn("/home/u/secret", raw)

    def test_permission_allow(self):
        rid = self.start_hook(PERM)["rid"]
        self.assertTrue(self.answer({"session": SID, "requestId": rid, "decision": "allow"})["ok"])
        self.assertEqual(json.loads(self.finish())["hookSpecificOutput"]["decision"], {"behavior": "allow"})

    def test_question_answer_maps_clipped_text_back(self):
        rec = self.start_hook(ASK)
        q = rec["questions"][0]
        self.assertTrue(q["question"].endswith("…"))
        self.assertLessEqual(len(q["question"]), 200)
        self.assertEqual(q["options"], ["Tea", "Coffee"])
        self.assertEqual(q["header"], "Drink")
        self.assertNotIn("TOPSECRET", json.dumps(rec))
        snap = self.model.snapshot({"id": "m"}, commands_active=True)["sessions"][0]
        self.assertEqual(snap["waiting"]["kind"], "question")
        self.assertEqual(snap["waiting"]["questions"], rec["questions"])
        # allow without answers is useless for a question
        bad = self.answer({"session": SID, "requestId": rec["rid"], "decision": "allow"})
        self.assertEqual(bad["error"], "bad_args:answers")
        ok = self.answer({"session": SID, "requestId": rec["rid"], "decision": "allow",
                          "answers": {q["question"]: "Coffee"}})
        self.assertTrue(ok["ok"], ok)
        out = json.loads(self.finish())
        d = out["hookSpecificOutput"]["decision"]
        self.assertEqual(d["behavior"], "allow")
        self.assertEqual(d["updatedInput"]["answers"], {LONG_Q: "Coffee"})
        self.assertEqual(d["updatedInput"]["questions"], ASK["tool_input"]["questions"])

    def test_not_waiting(self):
        self.start_hook(PERM)
        ack = self.answer({"session": SID, "requestId": str(uuid.uuid4()), "decision": "allow"})
        self.assertEqual(ack["error"], "not_waiting")
        self.assertFalse(os.path.exists(paths.answers_dir()) and os.listdir(paths.answers_dir()))
        self.proc.kill()

    def test_logs_never_hold_text_or_secret(self):
        rid = self.start_hook(PERM)["rid"]
        self.answer({"session": SID, "requestId": rid, "decision": "deny", "message": "M3SSAGE"})
        self.finish()
        joined = "\n".join(self.logs)
        self.assertIn("cmd answer id=", joined)
        self.assertNotIn("M3SSAGE", joined)
        self.assertNotIn(SECRET, joined)


class HookHoldTest(TempHome):
    def run_hook(self, payload, timeout=15):
        t0 = time.time()
        p = subprocess.run([_util.HOOK], input=json.dumps(payload).encode(), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, env=hook_env(self.home), timeout=timeout)
        return p, time.time() - t0

    def test_timeout_is_silent(self):
        self.write_json(paths.config_path(), {"commands": {"enabled": True, "holdSeconds": 1}})
        self.write_json(paths.secrets_path(), {"commandSecret": SECRET})
        p, elapsed = self.run_hook(PERM)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        self.assertGreaterEqual(elapsed, 0.9)
        recs = spool_records(self.home)
        self.assertEqual([r["ev"] for r in recs], ["PermissionRequest", "unanswered"])
        self.assertEqual(recs[0]["rid"], recs[1]["rid"])
        # the folded state keeps the prompt (still open elsewhere) but HA can no longer answer it
        m = Model()
        for r in recs:
            m.apply(r)
        s = m.snapshot({"id": "m"}, commands_active=True)["sessions"][0]
        self.assertEqual(s["status"], "waiting")
        self.assertFalse(s["waiting"]["hubCanAnswer"])
        self.assertNotIn("answer", s["controls"])

    def test_no_hold_without_secret_or_when_disabled(self):
        for cfg, secrets in (({"commands": {"enabled": True}}, {}),
                             ({"commands": {"enabled": False}}, {"commandSecret": SECRET}),
                             ({}, {"commandSecret": SECRET})):
            self.write_json(paths.config_path(), cfg)
            self.write_json(paths.secrets_path(), secrets)
            p, elapsed = self.run_hook(PERM)
            self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
            self.assertLess(elapsed, 2)
            self.assertFalse(spool_records(self.home)[-1]["hold"])

    def test_killed_hook_records_unanswered(self):
        self.write_json(paths.config_path(), {"commands": {"enabled": True, "holdSeconds": 30}})
        self.write_json(paths.secrets_path(), {"commandSecret": SECRET})
        p = subprocess.Popen([_util.HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=hook_env(self.home))
        p.stdin.write(json.dumps(PERM).encode())
        p.stdin.close()
        self.assertTrue(wait(lambda: spool_records(self.home)))
        p.terminate()
        out, err = collect(p, 5)
        self.assertEqual((p.returncode, out, err), (0, b"", b""))
        self.assertEqual(spool_records(self.home)[-1]["ev"], "unanswered")

    def test_invalid_answer_file_is_ignored(self):
        self.write_json(paths.config_path(), {"commands": {"enabled": True, "holdSeconds": 2}})
        self.write_json(paths.secrets_path(), {"commandSecret": SECRET})
        p = subprocess.Popen([_util.HOOK], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             env=hook_env(self.home))
        p.stdin.write(json.dumps(PERM).encode())
        p.stdin.close()
        self.assertTrue(wait(lambda: spool_records(self.home)))
        rid = spool_records(self.home)[0]["rid"]
        paths.ensure_answers_dir()
        self.write_json(paths.answer_path(rid), {"requestId": "other", "decision": "allow"})
        out, err = collect(p, 10)
        self.assertEqual((p.returncode, out, err), (0, b"", b""))
        self.assertFalse(os.path.exists(paths.answer_path(rid)))


class SnapshotTest(TempHome):
    def test_machine_commands_and_controls(self):
        c = config.Config({"mqtt": {"host": "h"}, "commands": {
            "enabled": True, "folders": {"proj": {"path": "/srv/very/private/path", "label": "Projects"}}}},
            {"commandSecret": SECRET})
        pub = c.commands.public()
        self.assertEqual(pub["folders"], [{"key": "proj", "label": "Projects"}])
        self.assertNotIn("/srv/very", json.dumps(pub))
        self.assertIsNone(config.Config({"commands": {"enabled": True}}, {}).commands.public())
        sock = os.path.join(self.home, "s.sock")
        srv = socket.socket(socket.AF_UNIX)
        srv.bind(sock)
        try:
            self.registry(kind="bg", job="ab12cd34", sock=sock)
            m = Model()
            m.apply({"v": 1, "ts": time.time(), "ev": "SessionStart", "sid": SID, "pid": os.getpid()})
            m.enrich(self.sessions)
            s = m.snapshot({"id": "m"}, commands_active=True)["sessions"][0]
            self.assertEqual(s["kind"], "bg")
            self.assertEqual(s["controls"], ["send", "interrupt", "stop"])
            self.assertEqual(m.snapshot({"id": "m"}, commands_active=False)["sessions"][0]["controls"], [])
        finally:
            srv.close()

    def test_registry_resolution_clears_request(self):
        m = Model()
        t = time.time()
        m.apply({"v": 1, "ts": t, "ev": "PermissionRequest", "sid": SID, "tool": "Bash", "desc": "List files",
                 "rid": str(uuid.uuid4()), "hold": True, "holdS": 55})
        self.assertEqual(m.sessions[SID]["status"], "waiting")
        # a PreToolUse from a parallel subagent doesn't hide the open prompt
        m.apply({"v": 1, "ts": t + 0.1, "ev": "PreToolUse", "sid": SID, "agent": "a1", "tool": "Read"})
        self.assertEqual(m.sessions[SID]["status"], "waiting")
        self.assertEqual(m.sessions[SID]["waiting"]["text"], "List files")
        # answered in claude.ai: the registry goes busy later than the request
        entry = self.registry()
        entry.update(status="busy", statusUpdatedAt=int((t + 5) * 1000))
        self.write_json(os.path.join(self.sessions, "%d.json" % os.getpid()), entry)
        m.enrich(self.sessions)
        self.assertEqual(m.sessions[SID]["status"], "working")
        self.assertIsNone(m.sessions[SID]["waiting"])

    def test_post_tool_use_and_stop_clear(self):
        m = Model()
        t = time.time()
        r1, r2 = str(uuid.uuid4()), str(uuid.uuid4())
        m.apply({"v": 1, "ts": t, "ev": "PermissionRequest", "sid": SID, "tool": "Bash", "rid": r1, "hold": True})
        m.apply({"v": 1, "ts": t + 1, "ev": "PermissionRequest", "sid": SID, "tool": "Write", "rid": r2, "hold": True})
        self.assertEqual(m.waiting_state(SID, r1), ("ok", "permission"))
        m.apply({"v": 1, "ts": t + 2, "ev": "PostToolUse", "sid": SID, "tool": "Write"})
        self.assertEqual(m.sessions[SID]["waiting"]["requestId"], r1)
        self.assertEqual(m.waiting_state(SID, r2), ("not_waiting", None))
        m.apply({"v": 1, "ts": t + 3, "ev": "Stop", "sid": SID})
        self.assertEqual(m.sessions[SID]["status"], "idle")
        self.assertEqual(m.waiting_state(SID, r1), ("not_waiting", None))


class DaemonCommandTest(unittest.TestCase):
    """The real crew-hubd: a signed command over the fake broker gets an ack on crew/<m>/ack."""

    def test_signed_send_to_unknown_session_acks_error(self):
        from crewhub import mqtt

        tmp = tempfile.TemporaryDirectory()
        broker = _util.FakeBroker()
        proc = None
        try:
            hub = os.path.join(tmp.name, ".claude", "crew-hub")
            os.makedirs(hub, 0o700)
            with open(os.path.join(hub, "config.json"), "w") as f:
                json.dump({"machineId": "m1", "mqtt": {"host": "127.0.0.1", "port": broker.port},
                           "commands": {"enabled": True, "folders": {"p": {"path": "/srv/x", "label": "P"}}}}, f)
            with open(os.path.join(hub, "secrets.json"), "w") as f:
                json.dump({"commandSecret": SECRET}, f)
            good = envelope("send", {"session": SID, "text": "S3NDTEXT"})
            cid = json.loads(good)["id"]
            broker.to_client = [mqtt.build_publish("crew/m1/cmd", good, qos=1, packet_id=7),
                                mqtt.build_publish("crew/m1/cmd", envelope("stop", {"session": SID}, secret="z" * 32),
                                                   qos=1, packet_id=8)]
            proc = subprocess.Popen([_util.DAEMON], env=dict(os.environ, HOME=tmp.name),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            acks = lambda: [json.loads(p[1]) for p in broker.publishes if p[0] == "crew/m1/ack"]
            self.assertTrue(wait(lambda: acks(), timeout=10))
            time.sleep(0.5)
            self.assertEqual(acks(), [{"id": cid, "ok": False, "error": "not_live"}])
            ack_pub = [p for p in broker.publishes if p[0] == "crew/m1/ack"][0]
            self.assertEqual((ack_pub[2], ack_pub[3]), (1, False))
            self.assertTrue(wait(lambda: any(p[0] == "crew/m1/snapshot" for p in broker.publishes)))
            snap = json.loads([p for p in broker.publishes if p[0] == "crew/m1/snapshot"][-1][1])
            self.assertEqual(snap["machine"]["commands"]["folders"], [{"key": "p", "label": "P"}])
            log = read_text(os.path.join(hub, "crew-hubd.log"))
            self.assertIn("cmd send id=%s error not_live" % cid, log)
            self.assertIn("cmd dropped", log)
            self.assertNotIn("S3NDTEXT", log)
            self.assertNotIn(SECRET, log)
            self.assertNotIn("/srv/x", json.dumps(snap))
        finally:
            if proc:
                proc.terminate()
                proc.wait(10)
            broker.close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
