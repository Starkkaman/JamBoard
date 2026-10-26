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


def open_bytes(path):
    with open(path, "rb") as f:
        return f.read()


def _no_internal_keys(testcase, obj, where):
    if isinstance(obj, dict):
        for k, v in obj.items():
            testcase.assertFalse(str(k).startswith("_"), "%s has internal key %s" % (where, k))
            _no_internal_keys(testcase, v, "%s.%s" % (where, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _no_internal_keys(testcase, v, "%s[%d]" % (where, i))


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

    def test_published_snapshot_has_no_internal_fields(self):
        """snapshot.json and the MQTT snapshot: no "_" key at any depth, no transcript path, after the sample replay."""
        broker = _util.FakeBroker()
        try:
            os.makedirs(self.hub, 0o700)
            tdir = os.path.join(self.home, "transcripts-PRIVATE")
            os.makedirs(tdir)
            tp = os.path.join(tdir, "7c1addf5-transcript.jsonl")
            with open(tp, "w") as f:
                f.write(json.dumps({"type": "assistant", "message": {"model": "claude-haiku-5-5",
                                                                     "usage": {"input_tokens": 5}}}) + "\n")
            payloads = []
            for p in _util.sample_payloads():
                p = dict(p)
                if "transcript_path" in p:
                    p["transcript_path"] = tp
                payloads.append(p)
            self.assertTrue(any("transcript_path" in p for p in payloads))
            cfg = os.path.join(self.hub, "config.json")
            with open(cfg, "w") as f:
                json.dump({"machineId": "nas", "mqtt": {"host": "127.0.0.1", "port": broker.port}}, f)
            stop_i = [p["hook_event_name"] for p in payloads].index("Stop")
            write_spool(self.hub, payloads[: stop_i + 1])
            self.start()
            # wait until enrichment has read the transcript, so internal signatures exist in the model
            self.assertTrue(wait(lambda: self.snapshot()["sessions"][0]["ctx"]["used"] == 5))
            self.assertTrue(wait(lambda: any(t == "crew/nas/snapshot" and b'"used":5' in pl
                                             for t, pl, *_ in broker.publishes)))
            self.proc.send_signal(signal.SIGTERM)
            self.assertEqual(self.proc.wait(timeout=10), 0)
            with open(os.path.join(self.hub, "state.json")) as f:
                self.assertIn("_tp", f.read())  # the model really had internal fields
            bodies = [("snapshot.json", open_bytes(os.path.join(self.hub, "snapshot.json")))]
            bodies += [("mqtt", pl) for t, pl, *_ in broker.publishes if t == "crew/nas/snapshot"]
            self.assertGreater(len(bodies), 1)
            for where, body in bodies:
                text = body.decode("utf-8")
                self.assertNotIn(tp, text)
                self.assertNotIn("transcripts-PRIVATE", text)
                self.assertNotIn(".jsonl", text)
                _no_internal_keys(self, json.loads(text), where)
        finally:
            broker.close()

    def test_plan_file_without_hooks_is_picked_up(self):
        os.makedirs(os.path.join(self.hub, "plans"), 0o700)
        plan = os.path.join(self.hub, "plans", "nohooks-sess.json")
        with open(plan, "w") as f:
            json.dump({"title": "Side quest", "phase": "probe", "done": 0, "total": 1, "tasks": []}, f)
        self.start()
        self.assertTrue(wait(lambda: self.snapshot()["sessions"][0]["name"] == "Side quest"))
        s = self.snapshot()["sessions"][0]
        self.assertEqual((s["id"], s["status"], s["plan"]["phase"]), ("nohooks-sess", "idle", "probe"))
        os.unlink(plan)
        self.assertTrue(wait(lambda: self.snapshot()["sessions"] == []))
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=10), 0)


    def test_password_migrates_and_never_leaks(self):
        pw = "pw-LEAKCHECK-91x"
        broker = _util.FakeBroker()
        try:
            os.makedirs(self.hub, 0o700)
            cfg = os.path.join(self.hub, "config.json")
            secrets = os.path.join(self.hub, "secrets.json")
            with open(cfg, "w") as f:
                json.dump({"machineId": "nas", "summaries": True,
                           "mqtt": {"host": "127.0.0.1", "port": broker.port, "username": "crew-nas", "password": pw}}, f)
            write_spool(self.hub, _util.sample_payloads())
            self.start()
            self.assertTrue(wait(lambda: any(t == "crew/nas/snapshot" for t, *_ in broker.publishes)))
            self.assertIn(pw.encode(), broker.connects[0])  # the login really used it
            self.proc.send_signal(signal.SIGTERM)
            self.assertEqual(self.proc.wait(timeout=10), 0)
            # migrated: secrets.json 0600 has it, config.json (0600) keeps everything else and no password
            self.assertEqual(stat.S_IMODE(os.stat(secrets).st_mode), 0o600)
            with open(secrets) as f:
                self.assertEqual(json.load(f), {"mqttPassword": pw})
            self.assertEqual(stat.S_IMODE(os.stat(cfg).st_mode), 0o600)
            with open(cfg) as f:
                c = json.load(f)
            self.assertEqual(c, {"machineId": "nas", "summaries": True,
                                 "mqtt": {"host": "127.0.0.1", "port": broker.port, "username": "crew-nas"}})
            # second start reads secrets.json, logs in again, no second migration
            self.start()
            self.assertTrue(wait(lambda: len(broker.connects) >= 2))
            self.assertIn(pw.encode(), broker.connects[1])
            self.proc.send_signal(signal.SIGTERM)
            self.assertEqual(self.proc.wait(timeout=10), 0)
            with open(os.path.join(self.hub, "crew-hubd.log")) as f:
                log = f.read()
            self.assertEqual(log.count("migrated MQTT password to secrets.json"), 1)
            for name in ("crew-hubd.log", "snapshot.json", "state.json", "spool.jsonl", "config.json"):
                self.assertNotIn(pw.encode(), open_bytes(os.path.join(self.hub, name)), name)
            for t, payload, *_ in broker.publishes:
                self.assertNotIn(pw.encode(), payload, t)
        finally:
            broker.close()


class ConfigSecretsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_home = os.environ.get("HOME")
        os.environ["HOME"] = self.tmp.name
        self.hub = os.path.join(self.tmp.name, ".claude", "crew-hub")
        os.makedirs(self.hub, 0o700)
        self.cfg = os.path.join(self.hub, "config.json")
        self.secrets = os.path.join(self.hub, "secrets.json")
        self.logs = []

    def tearDown(self):
        if self.old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.old_home
        self.tmp.cleanup()

    def write(self, path, data, mode=0o600):
        with open(path, "w") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        os.chmod(path, mode)

    def load(self, migrate=True):
        from crewhub import config
        return config.load(self.logs.append, migrate=migrate)

    def test_secrets_only(self):
        self.write(self.cfg, {"mqtt": {"host": "h", "username": "u"}})
        self.write(self.secrets, {"mqttPassword": "s3cret", "commandSecret": "cmd-key"}, 0o644)
        c = self.load()
        self.assertEqual((c.mqtt["password"], c.command_secret), ("s3cret", "cmd-key"))
        self.assertEqual(stat.S_IMODE(os.stat(self.secrets).st_mode), 0o600)  # tightened
        self.assertNotIn("s3cret", "".join(self.logs))

    def test_existing_secrets_win_and_no_migration(self):
        self.write(self.cfg, {"mqtt": {"host": "h", "password": "legacy-pw"}})
        self.write(self.secrets, {"mqttPassword": "new-pw"})
        c = self.load()
        self.assertEqual(c.mqtt["password"], "new-pw")
        with open(self.cfg) as f:
            self.assertEqual(json.load(f)["mqtt"]["password"], "legacy-pw")  # untouched
        with open(self.secrets) as f:
            self.assertEqual(json.load(f), {"mqttPassword": "new-pw"})
        self.assertTrue(any("still has mqtt.password" in line for line in self.logs))
        self.assertNotIn("pw", "".join(l.replace("password", "") for l in self.logs))

    def test_legacy_without_migrate_and_bad_secrets(self):
        self.write(self.cfg, {"mqtt": {"host": "h", "password": "legacy-pw"}})
        c = self.load(migrate=False)
        self.assertEqual(c.mqtt["password"], "legacy-pw")
        self.assertFalse(os.path.exists(self.secrets))
        self.write(self.secrets, "{not json legacy-pw")
        c = self.load()  # unreadable secrets.json: legacy password still works, nothing migrated over it
        self.assertEqual(c.mqtt["password"], "legacy-pw")
        with open(self.secrets) as f:
            self.assertEqual(f.read(), "{not json legacy-pw")
        self.assertNotIn("legacy-pw", "".join(self.logs))

    def test_migration_in_temp_home(self):
        self.write(self.cfg, {"machineId": "m", "mqtt": {"host": "h", "port": 1, "password": "legacy-pw"}}, 0o644)
        c = self.load()
        self.assertEqual(c.mqtt["password"], "legacy-pw")
        with open(self.secrets) as f:
            self.assertEqual(json.load(f), {"mqttPassword": "legacy-pw"})
        with open(self.cfg) as f:
            self.assertEqual(json.load(f), {"machineId": "m", "mqtt": {"host": "h", "port": 1}})
        for p in (self.cfg, self.secrets):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
        self.assertEqual([l for l in self.logs if "migrated" in l], ["migrated MQTT password to secrets.json"])
        self.assertNotIn("legacy-pw", "".join(self.logs))
        self.assertEqual(sorted(os.listdir(self.hub)), ["config.json", "secrets.json"])  # no temp files left

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
