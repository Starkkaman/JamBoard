"""Webhook transport (docs/WEBHOOK.md): signing, config, WebhookLink against a fake hub, crew-hubd end to end."""

import json
import os
import signal
import socket
import stat
import subprocess
import tempfile
import time
import unittest

import _util
from crewhub import VERSION, config, webhook
from crewhub.commands import CommandHandler
from crewhub.config import Commands, Config

WID = "a" * 24 + "0123456789abcdef0123456789abcdef0123456789ab"  # 64 chars like token_hex(32)
SECRET = "S3cret-webhook-key-XYZ_0123456789abcd"
ALLOW = {config.TEST_HTTP_ENV: "1"}


def wait(cond, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if cond():
                return True
        except Exception:
            pass
        time.sleep(0.02)
    return False


class EnvFlag(object):
    """Sets the test-only http flag for in-process tests."""

    def setUp(self):
        self._old_flag = os.environ.get(config.TEST_HTTP_ENV)
        os.environ[config.TEST_HTTP_ENV] = "1"

    def tearDown(self):
        if self._old_flag is None:
            os.environ.pop(config.TEST_HTTP_ENV, None)
        else:
            os.environ[config.TEST_HTTP_ENV] = self._old_flag


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class SigningTest(unittest.TestCase):
    def test_canonical_matches_the_contract(self):
        env = {"v": 1, "m": "mac", "ts": 1700000000123, "nonce": "0b0e6f0e-1111-4222-8333-944445555666",
               "type": "snapshot", "data": {"z": 1, "a": {"é": "ü", "b": [1, 2]}}}
        want = ('{"data":{"a":{"b":[1,2],"é":"ü"},"z":1},"m":"mac","nonce":"0b0e6f0e-1111-4222-8333-944445555666",'
                '"ts":1700000000123,"type":"snapshot","v":1}').encode("utf-8")
        self.assertEqual(webhook.canonical(env), want)
        self.assertEqual(webhook.canonical(dict(env, sig="ignored", extra=1)), want)
        self.assertEqual(webhook.sign(SECRET, env), _util.webhook_sig(SECRET, env))

    def test_envelope(self):
        env = webhook.envelope(SECRET, "mac", "hello", {"version": VERSION})
        self.assertEqual(set(env), {"v", "m", "ts", "nonce", "type", "data", "sig"})
        self.assertEqual((env["v"], env["m"], env["type"], env["data"]), (1, "mac", "hello", {"version": VERSION}))
        self.assertLess(abs(env["ts"] - time.time() * 1000), 5000)
        self.assertRegex(env["nonce"], r"^[0-9a-f-]{36}$")
        self.assertEqual(env["sig"], _util.webhook_sig(SECRET, env))
        self.assertNotEqual(env["nonce"], webhook.envelope(SECRET, "mac", "hello", {})["nonce"])
        # the wire body round-trips to the same signature (non-ASCII kept as UTF-8)
        body = webhook.encode(webhook.envelope(SECRET, "mac", "snapshot", {"name": "Büro"}))
        self.assertIn("Büro".encode("utf-8"), body)
        back = json.loads(body)
        self.assertEqual(back["sig"], _util.webhook_sig(SECRET, back))


class ConfigTest(EnvFlag, unittest.TestCase):
    def cfg(self, url="https://hass.example.com", wid=WID, secret=SECRET, **extra):
        data = dict({"machineId": "mac", "transport": "webhook", "webhook": {"url": url}}, **extra)
        secrets = {}
        if wid:
            secrets["webhookId"] = wid
        if secret:
            secrets["commandSecret"] = secret
        return Config(data, secrets)

    def test_https_target(self):
        c = self.cfg("https://hass.example.com:8443/ha/", mqtt={"host": "10.0.0.1"})
        self.assertIsNone(c.mqtt)  # transport picks: the mqtt block is ignored
        self.assertEqual(c.transport, "webhook")
        self.assertTrue(c.linked)
        self.assertEqual(c.webhook, {"scheme": "https", "host": "hass.example.com", "port": 8443, "base": "/ha",
                                     "id": WID, "secret": SECRET})
        self.assertEqual(self.cfg().webhook["port"], 443)

    def test_refusals_never_repeat_the_url_or_secrets(self):
        os.environ.pop(config.TEST_HTTP_ENV)
        cases = [
            self.cfg("http://hass.example.com"),
            self.cfg("http://127.0.0.1:8123"),  # loopback http needs the test flag
            self.cfg("https://user:pw@hass.example.com"),
            self.cfg("https://hass.example.com/?x=" + WID),
            self.cfg("ftp://hass.example.com"),
            self.cfg("https://"),
            self.cfg(wid=None),
            self.cfg(wid="short"),
            self.cfg(wid="a/b/../" + WID),
            self.cfg(secret=None),
            Config({"transport": "webhook"}, {"webhookId": WID, "commandSecret": SECRET}),
        ]
        for c in cases:
            self.assertIsNone(c.webhook)
            self.assertFalse(c.linked)
            self.assertTrue(c.webhook_error)
            for bad in (WID, SECRET, "hass.example.com", "pw"):
                self.assertNotIn(bad, c.webhook_error)

    def test_https_connection_verifies_certificates(self):
        import ssl

        h = webhook.Http(self.cfg("https://hass.example.com/ha").webhook, 5)
        self.assertEqual(h.path, "/ha/api/webhook/" + WID)
        conn = h._connect()
        try:
            ctx = conn._context
            self.assertEqual((ctx.verify_mode, ctx.check_hostname), (ssl.CERT_REQUIRED, True))
            self.assertEqual((conn.host, conn.port), ("hass.example.com", 443))
        finally:
            conn.close()

    def test_http_only_for_loopback_with_the_flag(self):
        self.assertEqual(self.cfg("http://127.0.0.1:8123").webhook["scheme"], "http")
        self.assertIsNone(self.cfg("http://192.168.1.5:8123").webhook)

    def test_mqtt_stays_the_default(self):
        for t in (None, "mqtt", "carrier-pigeon"):
            data = {"machineId": "nas", "mqtt": {"host": "h"}, "webhook": {"url": "https://x.example"}}
            if t:
                data["transport"] = t
            c = Config(data, {"webhookId": WID, "commandSecret": SECRET})
            self.assertEqual((c.transport, c.mqtt["host"], c.webhook, c.webhook_error), ("mqtt", "h", None, None))


class FakeExecutor(object):
    def __init__(self):
        self.calls = []

    def execute(self, action, a):
        self.calls.append((action, a))
        return {}


class LinkTest(EnvFlag, unittest.TestCase):
    def setUp(self):
        EnvFlag.setUp(self)
        self.hub = _util.FakeHub(WID, SECRET, "mac", hold=0.5)
        self.logs = []
        self.link = None

    def tearDown(self):
        if self.link is not None:
            self.link.offline(wait=1.0)
        self.hub.close()
        EnvFlag.tearDown(self)

    def make(self, url=None, commands=None, wid=WID):
        cfg = Config({"machineId": "mac", "transport": "webhook", "webhook": {"url": url or self.hub.url}},
                     {"webhookId": wid, "commandSecret": SECRET})
        self.link = webhook.WebhookLink(cfg, self.logs.append, commands)
        return self.link

    def assert_logs_clean(self):
        text = "\n".join(self.logs)
        for bad in (WID, SECRET, "/api/webhook"):
            self.assertNotIn(bad, text)

    def test_hello_then_snapshot_then_poll(self):
        link = self.make()
        link.publish(b'{"machine":{"id":"mac"},"sessions":[]}')
        link.start()
        self.assertTrue(wait(lambda: self.hub.of_type("snapshot") and self.hub.of_type("poll")))
        sent = [e["type"] for e in self.hub.accepted if e["type"] != "poll"]
        self.assertEqual(sent[:2], ["hello", "snapshot"])
        self.assertEqual(self.hub.of_type("hello")[0]["data"], {"version": VERSION})
        self.assertEqual(self.hub.of_type("snapshot")[0]["data"], {"machine": {"id": "mac"}, "sessions": []})
        self.assertEqual(self.hub.rejected, [])
        self.assertTrue(any("webhook connected to 127.0.0.1 as machine mac" in l for l in self.logs))
        # polls are back to back: a second one follows the first held one
        self.assertTrue(wait(lambda: len(self.hub.of_type("poll")) >= 2, 3))
        self.assert_logs_clean()

    def test_offline_keeps_only_the_latest_snapshot_and_backs_off(self):
        self.hub.fail_status = 500
        link = self.make()
        link.start()
        for i in range(3):
            link.publish(json.dumps({"n": i}).encode())
        time.sleep(3.4)
        with self.hub.cond:
            sends = [t for t, typ in self.hub.requests if typ != "poll"]
            polls = [t for t, typ in self.hub.requests if typ == "poll"]
        # backoff 1, 2, 4 s: attempts at ~0, 1, 3 s (not a tight loop)
        self.assertTrue(2 <= len(sends) <= 4, sends)
        self.assertTrue(2 <= len(polls) <= 4, polls)
        gaps = [b - a for a, b in zip(sends, sends[1:])]
        self.assertGreater(gaps[0], 0.8)
        if len(gaps) > 1:
            self.assertGreater(gaps[1], 1.7)
        self.assertTrue(any("failed (HTTP 500)" in l for l in self.logs))
        link.publish(json.dumps({"n": 3}).encode())
        self.hub.fail_status = None
        self.assertTrue(wait(lambda: self.hub.of_type("snapshot"), 6))
        time.sleep(0.3)
        self.assertEqual([e["data"] for e in self.hub.of_type("snapshot")], [{"n": 3}])
        self.assertEqual(len(self.hub.of_type("hello")), 1)
        self.assertTrue(wait(lambda: any("ok again" in l for l in self.logs), 6))
        self.assert_logs_clean()

    def test_connection_refused_backs_off_and_logs_host_only(self):
        link = self.make(url="http://127.0.0.1:%d" % free_port())
        link.start()
        link.publish(b'{"n":1}')
        self.assertTrue(wait(lambda: any("ConnectionRefusedError" in l for l in self.logs), 3))
        time.sleep(1.5)
        fails = [l for l in self.logs if "failed" in l]
        self.assertLessEqual(len(fails), 2)  # one per thread; repeats of the same error aren't logged again
        self.assertTrue(all("to 127.0.0.1 failed" in l for l in fails), fails)
        self.assertIsNotNone(link.pending)  # kept for later
        self.assert_logs_clean()

    def test_unknown_webhook_id_is_an_error(self):
        link = self.make(wid="b" * 64)  # the hub answers 200 with an empty body, like HA
        link.start()
        self.assertTrue(wait(lambda: any("unknown webhook" in l for l in self.logs), 3))
        self.assertEqual(self.hub.accepted, [])
        self.assertNotIn("b" * 64, "\n".join(self.logs))

    def test_poll_command_executor_ack(self):
        cmds = Commands({"enabled": True}, SECRET)
        execu = FakeExecutor()
        handler = CommandHandler(cmds, self.logs.append, executor=execu)
        link = self.make(commands=handler)
        link.start()
        good = _util.command_envelope(SECRET, "stop", {"session": "s1"})
        forged = _util.command_envelope("not-the-secret-000000", "stop", {"session": "s2"})
        stale = _util.command_envelope(SECRET, "stop", {"session": "s3"}, ts=int(time.time() * 1000) - 120000)
        self.assertTrue(wait(lambda: self.hub.of_type("poll")))
        self.hub.push(forged, good, stale)
        end = time.time() + 6
        while time.time() < end and len(self.hub.of_type("ack")) < 2:
            link.step(0.1)  # what the daemon's main loop does
            link.publish_acks()
        handler.close()
        acks = [e["data"] for e in self.hub.of_type("ack")]
        self.assertEqual(execu.calls, [("stop", {"session": "s1"})])
        by_id = {a["id"]: a for a in acks}
        self.assertEqual(by_id[good["id"]], {"id": good["id"], "ok": True})
        self.assertEqual(by_id[stale["id"]], {"id": stale["id"], "ok": False, "error": "stale"})
        self.assertNotIn(forged["id"], by_id)  # bad signature: dropped without an ack
        self.assertTrue(any("cmd dropped" in l for l in self.logs))
        # the same envelope again (HA delivers at most once, but a replay must still be refused)
        self.hub.push(good)
        end = time.time() + 4
        while time.time() < end and len(self.hub.of_type("ack")) < 3:
            link.step(0.1)
            link.publish_acks()
        self.assertEqual(self.hub.of_type("ack")[-1]["data"], {"id": good["id"], "ok": False, "error": "replay"})
        self.assertEqual(len(execu.calls), 1)
        self.assert_logs_clean()

    def test_offline_unblocks_a_held_poll(self):
        self.hub.hold = 30
        link = self.make()
        link.start()
        self.assertTrue(wait(lambda: self.hub.of_type("poll")))
        t0 = time.time()
        link.offline(wait=1.0)
        self.assertLess(time.time() - t0, 3.5)
        self.assertFalse(link.poller.is_alive())
        self.assertFalse(link.sender.is_alive())
        self.link = None


class DaemonWebhookTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.hub_dir = os.path.join(self.home, ".claude", "crew-hub")
        os.makedirs(self.hub_dir, 0o700)
        self.hub = _util.FakeHub(WID, SECRET, "mac", hold=1.0)
        self.proc = None

    def tearDown(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait()
            self.proc.stdout.close()
            self.proc.stderr.close()
        self.hub.close()
        self.tmp.cleanup()

    def test_end_to_end(self):
        with open(os.path.join(self.hub_dir, "config.json"), "w") as f:
            json.dump({"machineId": "mac", "machineName": "MacBook", "transport": "webhook",
                       "webhook": {"url": self.hub.url}, "commands": {"enabled": True}}, f)
        with open(os.path.join(self.hub_dir, "secrets.json"), "w") as f:
            json.dump({"webhookId": WID, "commandSecret": SECRET}, f)
        os.chmod(os.path.join(self.hub_dir, "secrets.json"), 0o600)
        payloads = _util.sample_payloads()
        from test_daemon import write_spool

        write_spool(self.hub_dir, payloads[:5])
        env = dict(os.environ, HOME=self.home, **ALLOW)
        self.proc = subprocess.Popen([_util.DAEMON], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertTrue(wait(lambda: self.hub.of_type("snapshot") and self.hub.of_type("poll")))
        self.assertEqual(self.hub.accepted[0]["type"] in ("hello", "poll"), True)
        self.assertEqual(self.hub.of_type("hello")[0]["data"], {"version": VERSION})
        snap = self.hub.of_type("snapshot")[0]["data"]
        self.assertEqual((snap["machine"]["id"], snap["machine"]["name"]), ("mac", "MacBook"))
        self.assertEqual(snap["machine"]["commands"]["enabled"], True)
        self.assertEqual(snap["sessions"][0]["id"], "7c1addf5-73c5-41d7-9225-77ba4bbbb2d5")
        # a command through poll -> verifier -> executor -> ack POST (the session isn't live here: not_live)
        cmd = _util.command_envelope(SECRET, "send", {"session": "nobody-here", "text": "hi", "urgent": False})
        self.hub.push(cmd)
        self.assertTrue(wait(lambda: self.hub.of_type("ack")))
        self.assertEqual(self.hub.of_type("ack")[0]["data"], {"id": cmd["id"], "ok": False, "error": "not_live"})
        self.assertEqual(self.hub.rejected, [])
        # a change is posted again
        from test_daemon import write_spool as ws

        ws(self.hub_dir, payloads[5:10])
        n = len(self.hub.of_type("snapshot"))
        self.assertTrue(wait(lambda: len(self.hub.of_type("snapshot")) > n))
        # SIGTERM with a poll held: stops promptly
        t0 = time.time()
        self.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.proc.wait(timeout=15), 0)
        self.assertLess(time.time() - t0, 8)
        self.assertEqual(self.proc.stdout.read() + self.proc.stderr.read(), b"")
        with open(os.path.join(self.hub_dir, "crew-hubd.log")) as f:
            log = f.read()
        self.assertIn("webhook 127.0.0.1", log)
        self.assertIn("webhook connected to 127.0.0.1 as machine mac", log)
        self.assertIn("cmd send id=%s error not_live" % cmd["id"], log)
        for name in ("crew-hubd.log", "snapshot.json", "state.json"):
            with open(os.path.join(self.hub_dir, name), "rb") as f:
                text = f.read().decode("utf-8")
            for bad in (WID, SECRET, "/api/webhook", cmd["sig"]):
                self.assertNotIn(bad, text, name)
        for env_ in self.hub.accepted:
            self.assertNotIn(SECRET, json.dumps(env_))

    def test_unusable_webhook_config_is_file_only(self):
        with open(os.path.join(self.hub_dir, "config.json"), "w") as f:
            json.dump({"machineId": "mac", "transport": "webhook", "webhook": {"url": self.hub.url}}, f)
        env = dict(os.environ, HOME=self.home)  # no test flag: http is refused
        env.pop(config.TEST_HTTP_ENV, None)
        self.proc = subprocess.Popen([_util.DAEMON, "--once"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(self.proc.wait(timeout=15), 0)
        with open(os.path.join(self.hub_dir, "crew-hubd.log")) as f:
            log = f.read()
        self.assertIn("transport webhook not usable: config.json webhook.url unusable (must start with https://)", log)
        self.assertIn("file-only", log)
        self.assertEqual(self.hub.requests, [])
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.hub_dir, "snapshot.json")).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
