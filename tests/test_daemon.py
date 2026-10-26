import json
import os
import signal
import stat
import subprocess
import tempfile
import time
import unittest

import _util
from crewhub.redact import redact
from crewhub.spool import Spool

SID = "7c1addf5-73c5-41d7-9225-77ba4bbbb2d5"


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


def write_spool(hub, payloads, t0=None):
    os.makedirs(hub, 0o700, exist_ok=True)
    t0 = t0 or time.time()
    with open(os.path.join(hub, "spool.jsonl"), "a") as f:
        for i, p in enumerate(payloads):
            f.write(json.dumps(redact(p, t0 + i * 0.001, None, None)) + "\n")


class DaemonSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.hub = os.path.join(self.home, ".claude", "crew-hub")
        self.proc = None

    def tearDown(self):
        self._close()
        self.tmp.cleanup()

    def _close(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait()
            self.proc.stdout.close()
            self.proc.stderr.close()

    def start(self):
        self._close()
        env = dict(os.environ, HOME=self.home)
        self.proc = subprocess.Popen([_util.DAEMON], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def snapshot(self):
        with open(os.path.join(self.hub, "snapshot.json")) as f:
            return json.load(f)

    def test_file_only_mode(self):
        payloads = _util.sample_payloads()
        stop_i = [p["hook_event_name"] for p in payloads].index("Stop")
        write_spool(self.hub, payloads[: stop_i + 1])
        self.start()
        self.assertTrue(wait(lambda: self.snapshot()["sessions"][0]["status"] == "idle"))
        snap = self.snapshot()
        self.assertEqual(snap["machine"]["version"], "0.1.0")
        s = snap["sessions"][0]
        self.assertEqual(s["id"], SID)
        self.assertEqual(s["agents"][0]["title"], "Say hi test")
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.hub, "snapshot.json")).st_mode), 0o600)
        # a second daemon exits at once (single instance)
        env = dict(os.environ, HOME=self.home)
        second = subprocess.run([_util.DAEMON], env=env, timeout=10, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(second.returncode, 0)
        self.assertTrue(self.proc.poll() is None)
        # live append is picked up
        write_spool(self.hub, payloads[stop_i + 1:])
        self.assertTrue(wait(lambda: self.snapshot()["sessions"][0]["status"] == "ended"))
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=10), 0)
        self.assertEqual(self.proc.stdout.read() + self.proc.stderr.read(), b"")
        self.assertFalse(os.path.exists(os.path.join(self.hub, "crew-hubd.pid")))
        with open(os.path.join(self.hub, "state.json")) as f:
            state = json.load(f)
        self.assertEqual(state["spool"]["offset"], os.path.getsize(os.path.join(self.hub, "spool.jsonl")))
        with open(os.path.join(self.hub, "crew-hubd.log")) as f:
            log = f.read()
        self.assertIn("file-only", log)
        self.assertIn("stopped", log)

    def test_restart_resumes_from_offset(self):
        payloads = _util.sample_payloads()
        write_spool(self.hub, payloads[:16])  # through the subagent's Bash PreToolUse
        self.start()
        self.assertTrue(wait(lambda: self.snapshot()["sessions"][0]["agents"][0]["steps"] == 1))
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(timeout=10)
        self.start()
        time.sleep(1.0)
        self.assertEqual(self.snapshot()["sessions"][0]["agents"][0]["steps"], 1)  # not double-counted
        self.proc.send_signal(signal.SIGTERM)
        self.proc.wait(timeout=10)

    def test_mqtt_mode_against_fake_broker(self):
        broker = _util.FakeBroker()
        try:
            os.makedirs(self.hub, 0o700)
            cfg = os.path.join(self.hub, "config.json")
            with open(cfg, "w") as f:
                json.dump({"machineId": "nas", "machineName": "NAS", "mqtt": {"host": "127.0.0.1", "port": broker.port,
                                                                              "username": "crew-nas", "password": "pw-xyz"}}, f)
            os.chmod(cfg, 0o644)
            write_spool(self.hub, _util.sample_payloads()[:5])
            self.start()
            self.assertTrue(wait(lambda: any(t == "crew/nas/snapshot" for t, *_ in broker.publishes)))
            self.assertEqual(broker.publishes[0], ("crew/nas/status", b"online", 1, True))
            topic, payload, qos, retain = [p for p in broker.publishes if p[0] == "crew/nas/snapshot"][0]
            self.assertEqual((qos, retain), (1, True))
            snap = json.loads(payload)
            self.assertEqual(snap["machine"]["id"], "nas")
            self.assertEqual(snap["sessions"][0]["id"], SID)
            self.assertNotIn("pw-xyz", payload.decode())
            self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o600)  # tightened
            self.proc.send_signal(signal.SIGTERM)
            self.assertEqual(self.proc.wait(timeout=10), 0)
            self.assertEqual(broker.publishes[-1], ("crew/nas/status", b"offline", 1, True))
            with open(os.path.join(self.hub, "crew-hubd.log")) as f:
                self.assertNotIn("pw-xyz", f.read())
        finally:
            broker.close()


class SpoolRotationTest(unittest.TestCase):
    def test_rotate_when_big_and_consumed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "spool.jsonl")
            with open(path, "w") as f:
                for i in range(100):
                    f.write(json.dumps({"i": i}) + "\n")
            sp = Spool(path, rotate_bytes=500, grace=0.3)
            self.assertEqual(len(sp.poll()), 100)
            self.assertFalse(os.path.exists(path))  # renamed to .old
            self.assertTrue(os.path.exists(path + ".old"))
            # a late writer that held the old fd, and a new writer that creates the new file
            with open(path + ".old", "a") as f:
                f.write('{"late":1}\n')
            with open(path, "a") as f:
                f.write('{"new":1}\n')
            lines = sp.poll()
            self.assertEqual(sorted(lines), sorted([b'{"late":1}', b'{"new":1}']))
            pos = sp.position()
            self.assertIn("old", pos)
            # restart mid-rotation resumes both offsets without duplicates
            sp2 = Spool(path, pos, rotate_bytes=500, grace=0.3)
            self.assertEqual(sp2.poll(), [])
            time.sleep(0.35)
            self.assertEqual(sp2.poll(), [])
            self.assertFalse(os.path.exists(path + ".old"))
            self.assertNotIn("old", sp2.position())

    def test_partial_line_waits(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "spool.jsonl")
            with open(path, "w") as f:
                f.write('{"a":1}\n{"b":')
            sp = Spool(path)
            self.assertEqual(sp.poll(), [b'{"a":1}'])
            with open(path, "a") as f:
                f.write('2}\n')
            self.assertEqual(sp.poll(), [b'{"b":2}'])


if __name__ == "__main__":
    unittest.main()
