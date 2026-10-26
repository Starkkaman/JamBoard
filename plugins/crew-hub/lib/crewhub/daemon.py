"""crew-hubd: one per machine. Spool -> state model -> ~/.claude/crew-hub/snapshot.json, plus MQTT when configured.

Loop (every POLL seconds): read new spool lines and fold them; every ENRICH seconds read ~/.claude/sessions/*.json,
transcript tails and plan files; every PLAN_POLL seconds stat the plans dir (plans from sessions without hooks); publish the snapshot when it changed (at most once a second) and at least every 60 s.
Signed commands on crew/<m>/cmd are verified and executed by commands.CommandHandler (docs/COMMANDS.md); acks go to
crew/<m>/ack. Never logs secrets, prompts or command arguments.
"""

import fcntl
import json
import os
import signal
import sys
import time

from . import VERSION, config, paths
from .commands import CommandHandler, Executor
from .mqtt import Client
from .spool import Spool
from .state import Model, now_ms

POLL = 0.25
ENRICH = 3.0
PLAN_POLL = 2.0
MIN_PUBLISH = 1.0
HEARTBEAT = 60.0
SAVE_EVERY = 2.0
LOG_MAX = 1024 * 1024
ANSWER_SWEEP = 60.0
ANSWER_MAX_AGE = 10 * 60.0


class Log(object):
    def __init__(self, path):
        self.path = path

    def __call__(self, msg):
        try:
            try:
                if os.path.getsize(self.path) > LOG_MAX:
                    os.replace(self.path, self.path + ".1")
            except OSError:
                pass
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                line = "%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
                os.write(fd, line.encode("utf-8", "replace"))
            finally:
                os.close(fd)
        except Exception:
            pass


def write_atomic(path, data):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


class MqttLink(object):
    """Owns the broker connection: connect with LWT, online status, cmd subscription, backoff up to 60 s."""

    def __init__(self, cfg, log, commands=None):
        self.cfg = cfg
        self.log = log
        self.commands = commands
        self.client = None
        self.next_try = 0.0
        self.backoff = 1.0
        self.pending = None  # latest snapshot body not yet acknowledged by the broker
        self.status_topic = cfg.topic("status")
        self.snapshot_topic = cfg.topic("snapshot")
        self.cmd_topic = cfg.topic("cmd")
        self.ack_topic = cfg.topic("ack")

    @property
    def connected(self):
        return self.client is not None and self.client.connected

    def _fail(self, what, err):
        if self.client is not None:
            self.client.close()
        self.client = None
        self.log("mqtt %s failed (%s: %s); retry in %ds" % (what, type(err).__name__, err, int(self.backoff)))
        self.next_try = time.time() + self.backoff
        self.backoff = min(self.backoff * 2, 60.0)

    def step(self, timeout):
        """Connect when due, then serve the socket for up to `timeout` seconds. Returns False if it didn't wait."""
        now = time.time()
        if not self.connected and now >= self.next_try:
            m = self.cfg.mqtt
            client = Client(
                m["host"], m["port"], client_id=("crew-hubd-" + self.cfg.machine_id)[:64],
                username=m.get("username"), password=m.get("password"), keepalive=60,
                will=(self.status_topic, b"offline", 1, True), on_message=self.on_message,
            )
            try:
                client.connect()
                self.client = client
                client.publish(self.status_topic, b"online", qos=1, retain=True)
                client.subscribe(self.cmd_topic, qos=1)
                self.log("mqtt connected to %s:%d as machine %s" % (m["host"], m["port"], self.cfg.machine_id))
                self.backoff = 1.0
                if self.pending is not None:
                    self.publish(self.pending)
            except Exception as e:
                self.client = client
                self._fail("connect", e)
        if self.connected:
            try:
                self.client.loop(timeout)
                return True
            except Exception as e:
                self._fail("loop", e)
        return False

    def publish(self, body):
        self.pending = body
        if not self.connected:
            return False
        try:
            self.client.publish(self.snapshot_topic, body, qos=1, retain=True)
            self.pending = None
            return True
        except Exception as e:
            self._fail("publish", e)
            return False

    def on_message(self, topic, payload, retain):
        if topic != self.cmd_topic:
            return
        if self.commands is None:
            self.log("cmd: dropped a message (%d bytes); commands not set up" % len(payload))
            return
        try:
            self.commands.receive(payload, retain)
        except Exception as e:
            self.log("cmd: handler error %s" % type(e).__name__)

    def publish_acks(self):
        """Acks queued by the command handler (from this thread or its worker), QoS 1, never retained."""
        if self.commands is None:
            return
        for body in self.commands.acks():
            if not self.connected:
                self.log("cmd ack id=%s dropped: not connected" % body.get("id"))
                continue
            try:
                self.client.publish(self.ack_topic, json.dumps(body, separators=(",", ":")).encode("utf-8"), qos=1,
                                    retain=False)
            except Exception as e:
                self._fail("ack publish", e)

    def offline(self):
        if not self.connected:
            return
        try:
            self.client.publish(self.status_topic, b"offline", qos=1, retain=True)
        except Exception:
            pass
        try:
            self.client.disconnect()
        except Exception:
            pass
        self.client = None


class Daemon(object):
    def __init__(self, log):
        self.log = log
        self.stop = False
        self.cfg = config.load(log, migrate=True)  # under the pidfile lock: one migration at most
        self.model = Model(self.cfg.ctx_max)
        pos = None
        try:
            with open(paths.state_path(), "r") as f:
                saved = json.load(f)
            self.model.load_dict(saved.get("model"))
            pos = saved.get("spool")
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log("state.json unreadable (%s); starting fresh" % type(e).__name__)
        self.spool = Spool(paths.spool_path(), pos)
        self.commands = None
        if self.cfg.mqtt:
            cmds = self.cfg.commands
            self.commands = CommandHandler(cmds, log, executor=Executor(cmds, waiting=self.model.waiting_state, log=log))
            if cmds.enabled and not cmds.secret:
                log("commands enabled in config.json but secrets.json has no commandSecret: commands stay off")
            elif cmds.active:
                log("commands on: %d folder preset(s), hold %ds" % (len(cmds.folders), int(cmds.hold_seconds)))
        self.mqtt = MqttLink(self.cfg, log, self.commands) if self.cfg.mqtt else None
        self.last_sweep = 0.0
        self.last_core = None
        self.last_publish = 0.0
        self.last_enrich = 0.0
        self.last_plan_poll = 0.0
        self.last_save = 0.0
        self.dirty_state = True
        self.started = now_ms()

    def machine(self):
        m = {"id": self.cfg.machine_id, "name": self.cfg.machine_name, "version": VERSION, "updatedAt": now_ms()}
        pub = self.cfg.commands.public() if self.cfg.mqtt else None
        if pub:
            m["commands"] = pub
        return m

    def commands_active(self):
        return bool(self.cfg.mqtt and self.cfg.commands.active)

    def sweep_answers(self, now):
        """Delete answer files nobody picked up (the hook was gone or the prompt was answered elsewhere)."""
        if now - self.last_sweep < ANSWER_SWEEP:
            return
        self.last_sweep = now
        d = paths.answers_dir()
        try:
            names = os.listdir(d)
        except OSError:
            return
        for n in names:
            p = os.path.join(d, n)
            try:
                if now - os.lstat(p).st_mtime > ANSWER_MAX_AGE:
                    os.unlink(p)
            except OSError:
                pass

    def ingest(self):
        lines = self.spool.poll()
        plans = paths.plans_dir()
        for raw in lines:
            try:
                rec = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            try:
                self.model.apply(rec, plans_dir=plans)
            except Exception as e:
                self.log("fold error on %s: %s" % (rec.get("ev") if isinstance(rec, dict) else "?", type(e).__name__))
        if lines:
            self.dirty_state = True

    def maybe_enrich(self, now):
        if now - self.last_enrich < ENRICH:
            return
        self.last_enrich = now
        try:
            self.model.enrich(paths.sessions_dir(), paths.plans_dir())
        except Exception as e:
            self.log("enrich error: %s: %s" % (type(e).__name__, e))
        if self.model.changed:
            self.dirty_state = True

    def maybe_poll_plans(self, now):
        if now - self.last_plan_poll < PLAN_POLL:
            return
        self.last_plan_poll = now
        try:
            self.model.poll_plans(paths.plans_dir(), paths.sessions_dir())
        except Exception as e:
            self.log("plan poll error: %s: %s" % (type(e).__name__, e))
        if self.model.changed:
            self.dirty_state = True

    def maybe_publish(self, now, force=False):
        heartbeat = now - self.last_publish >= HEARTBEAT
        if not (force or heartbeat or (self.model.changed and now - self.last_publish >= MIN_PUBLISH)):
            return
        snap = self.model.snapshot(self.machine(), self.commands_active())
        self.model.changed = False
        core = json.dumps(snap["sessions"], sort_keys=True, separators=(",", ":"))
        if not (force or heartbeat) and core == self.last_core:
            return
        self.last_core = core
        self.last_publish = now
        body = json.dumps(snap, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            write_atomic(paths.snapshot_path(), body)
        except Exception as e:
            self.log("snapshot write failed: %s" % e)
        if self.mqtt:
            self.mqtt.publish(body)

    def save_state(self, now, force=False):
        if not (force or (self.dirty_state and now - self.last_save >= SAVE_EVERY)):
            return
        self.last_save = now
        self.dirty_state = False
        try:
            data = {"v": 1, "spool": self.spool.position(), "model": self.model.to_dict()}
            write_atomic(paths.state_path(), json.dumps(data, separators=(",", ":")).encode("utf-8"))
        except Exception as e:
            self.log("state save failed: %s" % e)

    def run(self, once=False):
        mode = "mqtt %s:%d" % (self.cfg.mqtt["host"], self.cfg.mqtt["port"]) if self.cfg.mqtt else "file-only"
        self.log("crew-hubd %s started, pid %d, machine %s, %s" % (VERSION, os.getpid(), self.cfg.machine_id, mode))
        first = True
        while not self.stop:
            now = time.time()
            try:
                self.ingest()
                self.maybe_enrich(now)
                self.maybe_poll_plans(now)
                self.maybe_publish(now, force=first)
                self.save_state(now)
                self.sweep_answers(now)
            except Exception as e:
                self.log("loop error: %s: %s" % (type(e).__name__, e))
            first = False
            if once:
                break
            waited = False
            if self.mqtt:
                try:
                    waited = self.mqtt.step(POLL)
                    self.mqtt.publish_acks()
                except Exception as e:
                    self.log("mqtt error: %s" % e)
            if not waited:
                time.sleep(POLL)
        # shutdown
        try:
            self.ingest()
            self.maybe_publish(time.time(), force=True)
        except Exception:
            pass
        if self.commands:
            self.commands.close()
        if self.mqtt:
            try:
                self.mqtt.publish_acks()
            except Exception:
                pass
            self.mqtt.offline()
        self.save_state(time.time(), force=True)
        self.log("crew-hubd stopped")


def _lock_pidfile():
    """Hold an exclusive flock on the pidfile for the daemon's lifetime. Returns the fd, or None if another runs."""
    fd = os.open(paths.pid_path(), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, ("%d\n" % os.getpid()).encode())
    return fd


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    once = "--once" in argv
    paths.ensure_hub_dir()
    log = Log(paths.log_path())
    fd = _lock_pidfile()
    if fd is None:
        return 0  # another crew-hubd holds the lock
    daemon = Daemon(log)

    def _stop(signum, frame):
        daemon.stop = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        daemon.run(once=once)
    except Exception as e:
        log("fatal: %s: %s" % (type(e).__name__, e))
    finally:
        try:
            os.unlink(paths.pid_path())
        except OSError:
            pass
        os.close(fd)
    return 0
