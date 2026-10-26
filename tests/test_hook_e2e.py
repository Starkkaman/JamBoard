import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest

import _util


def run_hook(home, payload, extra_env=None):
    env = dict(os.environ, HOME=home)
    env.pop("CREW_HUB_NO_DAEMON", None)
    if extra_env:
        env.update(extra_env)
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    t0 = time.time()
    p = subprocess.run([_util.HOOK], input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, timeout=10)
    return p, time.time() - t0


def wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


class HookEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.hub = os.path.join(self.home, ".claude", "crew-hub")
        self.payloads = _util.sample_payloads()

    def tearDown(self):
        pidfile = os.path.join(self.hub, "crew-hubd.pid")
        try:
            with open(pidfile) as f:
                os.kill(int(f.read().strip()), signal.SIGTERM)
        except Exception:
            pass
        self.tmp.cleanup()

    def test_tool_event_appends_one_private_line_silently(self):
        bash = [p for p in self.payloads if p["hook_event_name"] == "PreToolUse" and p.get("tool_name") == "Bash"][0]
        p, elapsed = run_hook(self.home, bash)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stdout, b"")
        self.assertEqual(p.stderr, b"")
        spool = os.path.join(self.hub, "spool.jsonl")
        with open(spool) as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])
        self.assertEqual((rec["ev"], rec["desc"], rec["tool"]), ("PreToolUse", "Print hi via echo", "Bash"))
        self.assertNotIn("echo hi", lines[0])
        self.assertIsInstance(rec["pid"], int)
        self.assertEqual(stat.S_IMODE(os.stat(spool).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.hub).st_mode), 0o700)
        # no daemon for non-SessionStart events
        time.sleep(0.3)
        self.assertFalse(os.path.exists(os.path.join(self.hub, "crew-hubd.pid")))
        self.assertFalse(os.path.exists(os.path.join(self.hub, "crew-hubd.log")))
        sys.stderr.write("\n  crew-hook wall time incl. interpreter start: %.0f ms\n" % (elapsed * 1000))

    def test_every_sample_line_and_garbage_stay_silent(self):
        for payload in self.payloads:
            if payload["hook_event_name"] == "SessionStart":
                continue
            p, _ = run_hook(self.home, payload)
            self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        for junk in (b"", b"not json", b"\xff\xfe", b"[1,2]"):
            p, _ = run_hook(self.home, junk)
            self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        with open(os.path.join(self.hub, "spool.jsonl")) as f:
            self.assertEqual(len(f.read().splitlines()), len(self.payloads) - 1 + 4)

    def test_unwritable_home_still_exits_zero_silently(self):
        os.chmod(self.home, 0o500)
        try:
            p, _ = run_hook(self.home, self.payloads[1])
            self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        finally:
            os.chmod(self.home, 0o700)

    def test_session_start_spawns_one_detached_daemon(self):
        start = self.payloads[0]
        self.assertEqual(start["hook_event_name"], "SessionStart")
        p, elapsed = run_hook(self.home, start)
        self.assertEqual((p.returncode, p.stdout, p.stderr), (0, b"", b""))
        self.assertLess(elapsed, 2.0)  # did not wait for the daemon
        pidfile = os.path.join(self.hub, "crew-hubd.pid")
        def pid_written():
            with open(pidfile) as f:
                return bool(f.read().strip())

        self.assertTrue(wait(lambda: os.path.exists(pidfile) and pid_written()))
        with open(pidfile) as f:
            pid = int(f.read().strip())
        os.kill(pid, 0)
        # double fork + setsid: a new session (led by the exited middle child), not ours or the hook's
        self.assertNotEqual(os.getsid(pid), os.getsid(0))
        self.assertNotEqual(os.getpgid(pid), os.getpgid(0))
        # a second SessionStart must not start another daemon
        run_hook(self.home, start)
        time.sleep(0.5)
        with open(pidfile) as f:
            self.assertEqual(int(f.read().strip()), pid)
        snap = os.path.join(self.hub, "snapshot.json")
        self.assertTrue(wait(lambda: os.path.exists(snap)))
        os.kill(pid, signal.SIGTERM)
        self.assertTrue(wait(lambda: not os.path.exists(pidfile)))


if __name__ == "__main__":
    unittest.main()
