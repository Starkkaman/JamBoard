"""crew-hub-pair (bin/crew-hub-pair --stdin) with a temp HOME against the fake hub: files, modes, hello, no echo."""

import base64
import json
import os
import signal
import stat
import subprocess
import tempfile
import time
import unittest

import _util
from crewhub import VERSION, config

PAIR = os.path.join(_util.PLUGIN, "bin", "crew-hub-pair")
WID = "c" * 32 + "0123456789abcdef0123456789abcdef"
SECRET = "pairing-Secret_value-9876543210-QWERTY"


NEW_SECRET = "rotated-Command_secret-0123456789-abcdefXYZ"
MQTT_PW = "mqtt-PASSWORD-kept-555"


def encode(body, prefix="crewhub1:"):
    raw = json.dumps(body).encode("utf-8")
    return prefix + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def make_code(u, w=WID, s=SECRET, m="mac", prefix="crewhub1:", **extra):
    return encode(dict({"u": u, "w": w, "s": s, "m": m}, **extra), prefix)


def make_mqtt_code(s=NEW_SECRET, m="nas"):
    """What Home Assistant 0.3.1 shows for an MQTT machine (WEBHOOK.md "Pairing")."""
    return encode({"t": "mqtt", "m": m, "s": s})


def read_text(path):
    with open(path) as f:
        return f.read()


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


class PairTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.hub_dir = os.path.join(self.home, ".claude", "crew-hub")
        self.hub = _util.FakeHub(WID, SECRET, "mac", hold=0.5)
        self.daemon_pids = []

    def tearDown(self):
        for pid in self.daemon_pids:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        self.hub.close()
        self.tmp.cleanup()

    def run_pair(self, stdin, *args, allow_http=True):
        env = dict(os.environ, HOME=self.home)
        env.pop(config.TEST_HTTP_ENV, None)
        if allow_http:
            env[config.TEST_HTTP_ENV] = "1"
        r = subprocess.run([PAIR, "--stdin"] + list(args), input=stdin.encode("utf-8"), env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40)
        return r.returncode, r.stdout.decode("utf-8"), r.stderr.decode("utf-8")

    def read(self, name):
        with open(os.path.join(self.hub_dir, name)) as f:
            return json.load(f)

    def mode(self, name=None):
        return stat.S_IMODE(os.stat(os.path.join(self.hub_dir, name) if name else self.hub_dir).st_mode)

    def assert_no_echo(self, text, *secrets):
        for s in secrets:
            self.assertNotIn(s, text)

    def test_webhook_pairing(self):
        os.makedirs(self.hub_dir, 0o755)
        os.chmod(self.hub_dir, 0o755)
        with open(os.path.join(self.hub_dir, "config.json"), "w") as f:
            json.dump({"summaries": False, "contextMax": {"opus": 1000}, "machineName": "Isaac's MacBook",
                       "commands": {"enabled": False, "holdSeconds": 30}}, f)
        with open(os.path.join(self.hub_dir, "secrets.json"), "w") as f:
            json.dump({"mqttPassword": "old-mqtt-pw"}, f)
        code = make_code(self.hub.url)
        rc, out, err = self.run_pair(code + "\n", "--no-daemon")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("paired as mac with 127.0.0.1", out)
        self.assertIn('"folders"', out)  # how to add a folder preset
        self.assertIn("plan-file rule added to", out)
        with open(os.path.join(self.home, ".claude", "CLAUDE.md")) as f:
            self.assertEqual(f.read().count("<!-- crew-hub:plan-file -->"), 1)
        self.assert_no_echo(out + err, code, WID, SECRET, code[9:40])
        self.assertEqual(self.mode(), 0o700)
        self.assertEqual((self.mode("config.json"), self.mode("secrets.json")), (0o600, 0o600))
        self.assertEqual(self.read("config.json"), {
            "summaries": False, "contextMax": {"opus": 1000}, "machineName": "Isaac's MacBook",
            "transport": "webhook", "webhook": {"url": self.hub.url}, "machineId": "mac",
            "commands": {"enabled": True, "holdSeconds": 30, "folders": {}}})
        self.assertEqual(self.read("secrets.json"),
                         {"mqttPassword": "old-mqtt-pw", "webhookId": WID, "commandSecret": SECRET})
        hello = self.hub.of_type("hello")
        self.assertEqual(len(hello), 1)
        self.assertEqual((hello[0]["m"], hello[0]["data"]), ("mac", {"version": VERSION}))
        self.assertEqual(self.hub.rejected, [])
        self.assertEqual(sorted(os.listdir(self.hub_dir)), ["config.json", "secrets.json"])  # no temp files
        # what crew-hubd will load from it
        env_old = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        os.environ[config.TEST_HTTP_ENV] = "1"
        try:
            c = config.load()
        finally:
            os.environ["HOME"] = env_old
            os.environ.pop(config.TEST_HTTP_ENV, None)
        self.assertEqual((c.webhook["id"], c.webhook["port"], c.commands.active), (WID, self.hub.port, True))

    def test_typed_webhook_code(self):
        """0.3.1 webhook codes carry "t": "webhook"; codes without "t" (0.3.0) are webhook codes too (above)."""
        rc, out, err = self.run_pair(make_code(self.hub.url, t="webhook") + "\n", "--no-daemon")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("paired as mac with 127.0.0.1", out)
        self.assertEqual(self.read("secrets.json"), {"webhookId": WID, "commandSecret": SECRET})

    # ---- "t": "mqtt" codes: a new command secret for a machine already on MQTT ----
    def write_mqtt_setup(self, machine="nas", port=1883, commands=True, transport="mqtt"):
        os.makedirs(self.hub_dir, 0o700)
        cfg = {"machineId": machine, "machineName": "NAS", "prefix": "crew", "summaries": True,
               "mqtt": {"host": "127.0.0.1", "port": port, "username": "crew-nas"},
               "commands": {"enabled": commands, "folders": {"project": {"path": "/srv/project", "label": "Projects"}},
                            "holdSeconds": 30}}
        if transport:
            cfg["transport"] = transport
        with open(os.path.join(self.hub_dir, "config.json"), "w") as f:
            json.dump(cfg, f, indent=2)
        os.chmod(os.path.join(self.hub_dir, "config.json"), 0o600)
        with open(os.path.join(self.hub_dir, "secrets.json"), "w") as f:
            json.dump({"mqttPassword": MQTT_PW, "commandSecret": SECRET, "extra": 1}, f)
        os.chmod(os.path.join(self.hub_dir, "secrets.json"), 0o600)

    def raw(self, name):
        with open(os.path.join(self.hub_dir, name), "rb") as f:
            return f.read()

    def test_mqtt_code_replaces_only_the_command_secret(self):
        self.write_mqtt_setup()
        cfg_before = self.raw("config.json")
        code = make_mqtt_code()
        rc, out, err = self.run_pair(code + "\n", "--no-daemon")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("new command secret installed for nas", out)
        self.assert_no_echo(out + err, code, NEW_SECRET, SECRET, MQTT_PW, code[9:40])
        self.assertEqual(self.raw("config.json"), cfg_before)  # MQTT login, machine id, commands: untouched
        self.assertEqual(self.read("secrets.json"), {"mqttPassword": MQTT_PW, "commandSecret": NEW_SECRET, "extra": 1})
        self.assertEqual((self.mode(), self.mode("secrets.json")), (0o700, 0o600))
        self.assertEqual(sorted(os.listdir(self.hub_dir)), ["config.json", "secrets.json"])  # no temp files
        self.assertEqual(self.hub.requests, [])  # no network for an MQTT code
        # what crew-hubd will load: same broker login, the new secret signs commands
        env_old = os.environ.get("HOME")
        os.environ["HOME"] = self.home
        try:
            c = config.load()
        finally:
            os.environ["HOME"] = env_old
        self.assertEqual((c.transport, c.machine_id, c.mqtt["password"], c.mqtt["username"]),
                         ("mqtt", "nas", MQTT_PW, "crew-nas"))
        self.assertEqual(c.command_secret, NEW_SECRET)
        self.assertTrue(c.commands.active)

    def test_mqtt_code_without_transport_key(self):
        """Configs from before 0.3.0 have no "transport" (= mqtt)."""
        self.write_mqtt_setup(transport=None)
        rc, out, err = self.run_pair(make_mqtt_code() + "\n", "--no-daemon")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read("secrets.json")["commandSecret"], NEW_SECRET)

    def test_mqtt_code_with_commands_off_says_so(self):
        self.write_mqtt_setup(commands=False)
        rc, out, err = self.run_pair(make_mqtt_code() + "\n", "--no-daemon")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("commands are off on this machine", out)
        self.assertFalse(self.read("config.json")["commands"]["enabled"])  # not switched on behind the person's back
        self.assertEqual(self.read("secrets.json")["commandSecret"], NEW_SECRET)

    def test_mqtt_code_refused_unless_already_mqtt_for_that_machine(self):
        code = make_mqtt_code()
        # nothing set up yet
        rc, out, err = self.run_pair(code + "\n", "--no-daemon")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("isn't set up for MQTT yet", err)
        self.assertIn("crew-hub-pair --mqtt", err)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "CLAUDE.md")))  # failed: no rule
        self.assertFalse(os.path.exists(os.path.join(self.hub_dir, "secrets.json")))
        self.assert_no_echo(out + err, code, NEW_SECRET)

        cases = [
            (dict(machine="pc"), "this code is for machine nas, but this machine is set up as pc"),
            (dict(transport="webhook"), "uses the webhook transport"),
        ]
        for setup, msg in cases:
            self.tmp.cleanup()
            os.makedirs(self.home, exist_ok=True)
            self.write_mqtt_setup(**setup)
            before = (self.raw("config.json"), self.raw("secrets.json"))
            rc, out, err = self.run_pair(code + "\n", "--no-daemon")
            self.assertEqual(rc, 2, (setup, out, err))
            self.assertIn(msg, err)
            self.assertEqual((self.raw("config.json"), self.raw("secrets.json")), before)
            self.assert_no_echo(out + err, code, NEW_SECRET, SECRET, MQTT_PW)

        # an mqtt config without a broker host
        self.tmp.cleanup()
        os.makedirs(self.hub_dir)
        with open(os.path.join(self.hub_dir, "config.json"), "w") as f:
            json.dump({"machineId": "nas", "transport": "mqtt"}, f)
        rc, out, err = self.run_pair(code + "\n", "--no-daemon")
        self.assertEqual(rc, 2, out + err)
        self.assertIn("no broker in config.json", err)
        self.assertFalse(os.path.exists(os.path.join(self.hub_dir, "secrets.json")))

    def test_mqtt_code_restarts_the_daemon_with_the_new_secret(self):
        from crewhub import mqtt

        broker = _util.FakeBroker()
        try:
            self.write_mqtt_setup(port=broker.port)
            env = dict(os.environ, HOME=self.home)
            old = subprocess.Popen([_util.DAEMON], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.daemon_pids.append(old.pid)
            self.addCleanup(old.wait, 10)  # reaped even when an assertion fails (tearDown kills it first)
            pidfile = os.path.join(self.hub_dir, "crew-hubd.pid")
            self.assertTrue(wait(lambda: read_text(pidfile).strip() == str(old.pid)))
            self.assertTrue(wait(lambda: len(broker.connects) >= 1 and broker.subscribes))
            # from now on every (re)subscribe gets one command signed with the new and one with the old secret
            new_env = json.dumps(_util.command_envelope(NEW_SECRET, "send", {"session": "no-such-session", "text": "x"}))
            old_env = json.dumps(_util.command_envelope(SECRET, "send", {"session": "no-such-session", "text": "y"}))
            broker.to_client = [mqtt.build_publish("crew/nas/cmd", new_env, qos=1, packet_id=7),
                                mqtt.build_publish("crew/nas/cmd", old_env, qos=1, packet_id=8)]
            rc, out, err = self.run_pair(make_mqtt_code() + "\n")
            self.assertEqual(rc, 0, out + err)
            self.assertIn("new command secret installed for nas", out)
            self.assertIn("crew-hubd restarted", out)
            self.assertEqual(old.wait(timeout=10), 0)
            new_pid = int(read_text(pidfile).strip())
            self.daemon_pids.append(new_pid)
            self.assertNotEqual(new_pid, old.pid)
            new_id, old_id = json.loads(new_env)["id"], json.loads(old_env)["id"]
            acks = lambda: [json.loads(p[1]) for p in broker.publishes if p[0] == "crew/nas/ack"]
            self.assertTrue(wait(lambda: any(a["id"] == new_id for a in acks()), 10))
            time.sleep(0.5)
            # the restarted daemon verifies with the new secret; the old secret's command is dropped (no ack)
            self.assertEqual([a["id"] for a in acks()], [new_id])
            self.assertTrue(len(broker.connects) >= 2)
            os.kill(new_pid, signal.SIGTERM)
            self.assertTrue(wait(lambda: not os.path.exists(pidfile), 10))
            self.assert_no_echo(read_text(os.path.join(self.hub_dir, "crew-hubd.log")), NEW_SECRET, SECRET, MQTT_PW)
        finally:
            broker.close()

    def test_default_name_is_the_hostname(self):
        rc, out, err = self.run_pair(make_code(self.hub.url) + "\n", "--no-daemon")
        self.assertEqual(rc, 0, out + err)
        import socket

        self.assertEqual(self.read("config.json")["machineName"], socket.gethostname().split(".")[0])
        rc, out, err = self.run_pair(make_code(self.hub.url) + "\n", "--no-daemon", "--name", "Laptop")
        self.assertEqual(self.read("config.json")["machineName"], "Laptop")

    def test_bad_codes_change_nothing_and_are_not_echoed(self):
        good = make_code(self.hub.url)
        cases = [
            ("not-a-code-" + SECRET, "not a Crew Hub pairing code"),
            (good.replace("crewhub1:", "crewhub2:"), "not a Crew Hub pairing code"),
            ("crewhub1:" + good[9:-6] + "!!", "damaged"),
            ("crewhub1:" + base64.urlsafe_b64encode(b"[1,2]").decode().rstrip("="), "damaged"),
            (make_code(self.hub.url, s="short"), "too short"),
            (make_code(self.hub.url, m="Mac Book"), "machine id"),
            (make_code(self.hub.url, w="../../etc"), "webhook id"),
            (make_code("http://hass.example.com"), "must start with https://"),
            (make_code(self.hub.url, t="carrier-pigeon"), "unknown to this crew-hub-pair"),
            (make_mqtt_code(s="short"), "too short"),
            (make_mqtt_code(m="NAS!"), "machine id"),
            (encode({"t": "mqtt", "s": SECRET}), "incomplete"),
        ]
        for code, msg in cases:
            rc, out, err = self.run_pair(code + "\n", "--no-daemon")
            self.assertEqual(rc, 2, (msg, out, err))
            self.assertIn(msg, err)
            self.assert_no_echo(out + err, code, SECRET, WID, "hass.example.com")
            self.assertFalse(os.path.exists(os.path.join(self.hub_dir, "config.json")))
        # loopback http without the test flag is refused too
        rc, out, err = self.run_pair(good + "\n", "--no-daemon", allow_http=False)
        self.assertEqual(rc, 2)
        self.assertIn("must start with https://", err)
        self.assertEqual(self.hub.requests, [])

    def test_rejected_hello(self):
        code = make_code(self.hub.url, s="another-secret-that-HA-does-not-know")
        rc, out, err = self.run_pair(code + "\n", "--no-daemon")
        self.assertEqual(rc, 1)
        self.assertIn("hello to 127.0.0.1 failed: HTTP 401", out)
        self.assert_no_echo(out + err, code, "another-secret-that-HA-does-not-know", WID)
        self.assertEqual(self.read("config.json")["transport"], "webhook")  # saved; the daemon retries

    def test_restarts_the_daemon(self):
        os.makedirs(self.hub_dir, 0o700)
        env = dict(os.environ, HOME=self.home)
        old = subprocess.Popen([_util.DAEMON], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.daemon_pids.append(old.pid)
        pidfile = os.path.join(self.hub_dir, "crew-hubd.pid")
        self.assertTrue(wait(lambda: read_text(pidfile).strip() == str(old.pid)))
        rc, out, err = self.run_pair(make_code(self.hub.url) + "\n")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(old.wait(timeout=10), 0)  # SIGTERM -> clean exit
        self.assertIn("crew-hubd restarted", out)
        new_pid = int(read_text(pidfile).strip())
        self.daemon_pids.append(new_pid)
        self.assertNotEqual(new_pid, old.pid)
        # the new daemon runs in webhook mode: its own hello and a snapshot reach the hub
        self.assertTrue(wait(lambda: len(self.hub.of_type("hello")) >= 2 and self.hub.of_type("snapshot")))
        os.kill(new_pid, signal.SIGTERM)
        self.assertTrue(wait(lambda: not os.path.exists(pidfile), 10))
        with open(os.path.join(self.hub_dir, "crew-hubd.log")) as f:
            log = f.read()
        self.assertIn("file-only", log)
        self.assertIn("webhook 127.0.0.1", log)
        self.assert_no_echo(log, WID, SECRET)

    def test_mqtt_pairing(self):
        os.makedirs(self.hub_dir, 0o700)
        with open(os.path.join(self.hub_dir, "config.json"), "w") as f:
            json.dump({"transport": "webhook", "webhook": {"url": "https://x.example"}, "summaries": True}, f)
        with open(os.path.join(self.hub_dir, "secrets.json"), "w") as f:
            json.dump({"webhookId": WID, "commandSecret": SECRET}, f)
        pw = "mqtt-PASSWORD-xyz-123"
        cs = "command-SECRET-for-mqtt-abcdef"
        answers = "\n".join(["192.0.2.10", "1883", "crew-mac", pw, "mac", cs]) + "\n"
        rc, out, err = self.run_pair(answers, "--mqtt", "--no-daemon")
        self.assertEqual(rc, 0, out + err)
        self.assert_no_echo(out + err, pw, cs, SECRET, WID)
        self.assertIn("machine mac, broker 192.0.2.10:1883", out)
        cfg = self.read("config.json")
        self.assertEqual(cfg["transport"], "mqtt")
        self.assertEqual(cfg["mqtt"], {"host": "192.0.2.10", "port": 1883, "username": "crew-mac"})
        self.assertEqual((cfg["machineId"], cfg["summaries"], cfg["commands"]["enabled"]), ("mac", True, True))
        self.assertEqual(self.read("secrets.json"), {"mqttPassword": pw, "commandSecret": cs})
        self.assertEqual((self.mode("config.json"), self.mode("secrets.json")), (0o600, 0o600))
        # again with empty password and secret: both kept
        answers = "\n".join(["192.0.2.10", "", "crew-mac", "", "mac", ""]) + "\n"
        rc, out, err = self.run_pair(answers, "--mqtt", "--no-daemon")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read("secrets.json"), {"mqttPassword": pw, "commandSecret": cs})
        self.assertTrue(self.read("config.json")["commands"]["enabled"])
        # bad answers
        rc, out, err = self.run_pair("h\nnot-a-port\n", "--mqtt", "--no-daemon")
        self.assertEqual(rc, 2)
        self.assertIn("port", err)

    def test_mqtt_after_webhook_drops_the_webhook_secret(self):
        os.makedirs(self.hub_dir, 0o700)
        with open(os.path.join(self.hub_dir, "config.json"), "w") as f:
            json.dump({"transport": "webhook", "webhook": {"url": "https://x.example"}}, f)
        with open(os.path.join(self.hub_dir, "secrets.json"), "w") as f:
            json.dump({"webhookId": WID, "commandSecret": SECRET}, f)
        rc, out, err = self.run_pair("h.lan\n1883\nu\npw-123\nmac\n\n", "--mqtt", "--no-daemon")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.read("secrets.json"), {"mqttPassword": "pw-123"})
        self.assertFalse(self.read("config.json")["commands"]["enabled"])


if __name__ == "__main__":
    unittest.main()
