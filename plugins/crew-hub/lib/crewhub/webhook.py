"""Webhook transport for machines off the LAN (docs/WEBHOOK.md is the contract).

Every request is a signed envelope POSTed to <url>/api/webhook/<webhookId> over HTTPS with normal certificate
verification (ssl.create_default_context(); plain http only for loopback tests, see config.parse_webhook_url).

WebhookLink runs two threads:
- the sender posts `hello` once at start, then acks (first) and the latest snapshot; offline it keeps only the latest
  snapshot (that's the offline buffer) and backs off exponentially up to 60 s;
- the poller runs one long `poll` at a time, back to back (35 s socket timeout, HA holds it up to 25 s), and hands each
  received command envelope to the main thread, which feeds it to the same CommandHandler as MQTT (same verification).

Never logged: the secret, the webhook id, the URL path, a signature or a request body. Logged: the host, the HTTP
status, error types.
"""

import hashlib
import hmac
import http.client
import json
import queue
import socket
import ssl
import threading
import time
import uuid

from . import VERSION

PROTOCOL = 1
POLL_TIMEOUT = 35.0
SEND_TIMEOUT = 10.0
BACKOFF_MAX = 60.0
BODY_MAX = 256 * 1024
RESPONSE_MAX = 1024 * 1024
ACKS_MAX = 64
ACK_TTL = 60.0
COMMANDS_MAX = 20
INBOUND_MAX = 64
QUICK_POLL = 0.5  # an empty poll answered faster than this means something is off: pause instead of spinning
QUICK_POLL_PAUSE = 1.0
LOG_REPEAT = 600.0
STOP_WAIT = 5.0
TYPES = ("hello", "snapshot", "poll", "ack")


# ---- envelope ----------------------------------------------------------------------------------------------------
def canonical(env):
    body = {"v": env.get("v"), "m": env.get("m"), "ts": env.get("ts"), "nonce": env.get("nonce"),
            "type": env.get("type"), "data": env.get("data")}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign(secret, env):
    return hmac.new(secret.encode("utf-8"), canonical(env), hashlib.sha256).hexdigest()


def envelope(secret, machine_id, typ, data, ts=None, nonce=None):
    env = {"v": PROTOCOL, "m": machine_id, "ts": int(time.time() * 1000) if ts is None else ts,
           "nonce": nonce or str(uuid.uuid4()), "type": typ, "data": data}
    env["sig"] = sign(secret, env)
    return env


def encode(env):
    return json.dumps(env, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


# ---- HTTP --------------------------------------------------------------------------------------------------------
class HttpStatus(Exception):
    def __init__(self, status):
        Exception.__init__(self, status)
        self.status = status


class BadResponse(Exception):
    pass


def describe(err):
    """A log-safe description: never str() of an arbitrary exception (it could carry a URL)."""
    if isinstance(err, HttpStatus):
        hint = ""
        if err.status == 401:
            hint = " (rejected: re-pair if this persists, or check the clock)"
        return "HTTP %d%s" % (err.status, hint)
    if isinstance(err, BadResponse):
        return ("unexpected response (%s)" % err.args[0]) if err.args else "unexpected response"
    if isinstance(err, ssl.SSLCertVerificationError):
        return "TLS certificate not trusted"
    if isinstance(err, ssl.SSLError):
        return "TLS error (%s)" % (err.reason or type(err).__name__)
    if isinstance(err, socket.timeout):
        return "timed out"
    if isinstance(err, OSError):
        return "%s%s" % (type(err).__name__, " (errno %d)" % err.errno if isinstance(err.errno, int) else "")
    return type(err).__name__


class Http(object):
    """One keep-alive connection to the hub. post(body) returns the parsed JSON object or raises."""

    def __init__(self, target, timeout):
        self.target = target
        self.timeout = timeout
        self.conn = None
        self.aborted = False
        self.path = target["base"] + "/api/webhook/" + target["id"]

    def _connect(self):
        t = self.target
        if t["scheme"] == "https":
            return http.client.HTTPSConnection(t["host"], t["port"], timeout=self.timeout,
                                               context=ssl.create_default_context())
        return http.client.HTTPConnection(t["host"], t["port"], timeout=self.timeout)

    def close(self):
        conn, self.conn = self.conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def abort(self):
        """From another thread: unblock a request in flight and refuse new ones (used on shutdown)."""
        self.aborted = True
        conn = self.conn
        sock = getattr(conn, "sock", None) if conn is not None else None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def post(self, body):
        for attempt in (0, 1):
            if self.aborted:
                raise BadResponse("aborted")
            fresh = self.conn is None
            if fresh:
                self.conn = self._connect()
            conn = self.conn
            try:
                conn.request("POST", self.path, body=body, headers={
                    "Content-Type": "application/json", "User-Agent": "crew-hubd/%s" % VERSION})
                resp = conn.getresponse()
                data = resp.read(RESPONSE_MAX + 1)
                status = resp.status
                keep = not resp.will_close and len(data) <= RESPONSE_MAX
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError):
                self.close()
                if fresh or attempt:
                    raise
                continue  # the server dropped our idle keep-alive connection: one retry on a fresh one
            except Exception:
                self.close()
                raise
            if not keep:
                self.close()
            if status != 200:
                raise HttpStatus(status)
            if len(data) > RESPONSE_MAX:
                raise BadResponse("too large")
            try:
                obj = json.loads(data.decode("utf-8"))
            except Exception:
                # HA answers 200 with an empty body for an unknown webhook id: the pairing was replaced or removed.
                raise BadResponse("not JSON; unknown webhook? re-pair")
            if not isinstance(obj, dict):
                raise BadResponse("not an object")
            return obj
        raise BadResponse("no response")  # not reached


def post_once(target, machine_id, typ, data, timeout=SEND_TIMEOUT):
    """One signed request on a fresh connection (crew-hub-pair's hello). Returns the response object or raises."""
    h = Http(target, timeout)
    try:
        return h.post(encode(envelope(target["secret"], machine_id, typ, data)))
    finally:
        h.close()


class _Limiter(object):
    """Logs a failure when it differs from the last one or every LOG_REPEAT seconds; recovery once."""

    def __init__(self, log, what, host):
        self.log = log
        self.what = what
        self.host = host
        self.last = None
        self.last_at = 0.0
        self.failing = False

    def fail(self, desc, retry):
        now = time.time()
        if desc != self.last or now - self.last_at >= LOG_REPEAT:
            self.log("webhook %s to %s failed (%s); retry in %ds" % (self.what, self.host, desc, int(retry)))
            self.last = desc
            self.last_at = now
        self.failing = True

    def ok(self):
        if self.failing:
            self.log("webhook %s to %s ok again" % (self.what, self.host))
        self.failing = False
        self.last = None


# ---- link --------------------------------------------------------------------------------------------------------
class WebhookLink(object):
    """Same surface the daemon uses for MqttLink: start, step, publish, publish_acks, offline."""

    def __init__(self, cfg, log, commands=None, poll_timeout=POLL_TIMEOUT, send_timeout=SEND_TIMEOUT):
        self.target = cfg.webhook
        self.secret = cfg.webhook["secret"]
        self.machine_id = cfg.machine_id
        self.host = cfg.webhook["host"]
        self.log = log
        self.commands = commands
        self.cond = threading.Condition()
        self.stop_event = threading.Event()
        self.pending = None  # latest snapshot body (bytes) not yet accepted by HA
        self.acks = []  # [(queued at, ack body)]
        self.hello_done = False
        self.stopping = False
        self.stop_deadline = 0.0
        self.inbound = queue.Queue(INBOUND_MAX)
        self.send_http = Http(self.target, send_timeout)
        self.poll_http = Http(self.target, poll_timeout)
        self.send_log = _Limiter(log, "send", self.host)
        self.poll_log = _Limiter(log, "poll", self.host)
        self.connected = False  # last request from the sender succeeded
        self.sender = threading.Thread(target=self._send_loop, name="crew-webhook-send", daemon=True)
        self.poller = threading.Thread(target=self._poll_loop, name="crew-webhook-poll", daemon=True)
        self.started = False

    def start(self):
        if self.started:
            return
        self.started = True
        self.sender.start()
        self.poller.start()

    # main thread -------------------------------------------------------------------------------------------------
    def publish(self, body):
        if len(body) > BODY_MAX - 1024:
            self.log("webhook snapshot of %d bytes too large; not sent" % len(body))
            return False
        with self.cond:
            self.pending = body
            self.cond.notify_all()
        return True

    def step(self, timeout):
        """Feed received command envelopes to the handler (main thread); waits up to `timeout` for the first."""
        try:
            env = self.inbound.get(timeout=timeout)
        except queue.Empty:
            return True
        while True:
            self._deliver(env)
            try:
                env = self.inbound.get_nowait()
            except queue.Empty:
                return True

    def _deliver(self, env):
        payload = json.dumps(env, ensure_ascii=False).encode("utf-8")
        if self.commands is None:
            self.log("cmd: dropped a message (%d bytes); commands not set up" % len(payload))
            return
        try:
            self.commands.receive(payload, False)  # never "retained" over the webhook
        except Exception as e:
            self.log("cmd: handler error %s" % type(e).__name__)

    def publish_acks(self):
        if self.commands is None:
            return
        bodies = self.commands.acks()
        if not bodies:
            return
        with self.cond:
            now = time.time()
            for b in bodies:
                self.acks.append((now, b))
            while len(self.acks) > ACKS_MAX:
                _, old = self.acks.pop(0)
                self.log("cmd ack id=%s dropped: too many queued" % old.get("id"))
            self.cond.notify_all()

    def offline(self, wait=STOP_WAIT):
        """Stop both threads; the sender gets up to `wait` seconds to deliver what is still queued."""
        with self.cond:
            self.stopping = True
            self.stop_deadline = time.time() + wait
            self.cond.notify_all()
        self.stop_event.set()
        self.poll_http.abort()
        if self.started:
            self.sender.join(wait + 1.0)
            self.poller.join(2.0)
        if not self.poller.is_alive():
            self.poll_http.close()
        if not self.sender.is_alive():
            self.send_http.close()

    # sender thread -----------------------------------------------------------------------------------------------
    def _has_work(self):
        return (not self.hello_done) or bool(self.acks) or self.pending is not None

    def _next_item(self):
        """(type, data, token) under the lock. Acks older than ACK_TTL are dropped (HA gave up after 15 s)."""
        now = time.time()
        while self.acks and now - self.acks[0][0] > ACK_TTL:
            _, old = self.acks.pop(0)
            self.log("cmd ack id=%s dropped: not delivered in time" % old.get("id"))
        if not self.hello_done:
            return "hello", {"version": VERSION}, None
        if self.acks:
            return "ack", self.acks[0][1], self.acks[0]
        if self.pending is not None:
            return "snapshot", self.pending, self.pending
        return None

    def _send_loop(self):
        backoff = 1.0
        next_try = 0.0
        while True:
            with self.cond:
                while True:
                    if self.stopping:
                        if not self._has_work() or time.time() >= self.stop_deadline:
                            return
                        break  # one last attempt for each queued item, no backoff
                    if self._has_work():
                        delay = next_try - time.time()
                        if delay <= 0:
                            break
                        self.cond.wait(delay)
                    else:
                        self.cond.wait()
                item = self._next_item()
                stopping = self.stopping
            if item is None:
                continue
            typ, data, token = item
            try:
                if typ == "snapshot":
                    data = json.loads(data.decode("utf-8"))
                body = encode(envelope(self.secret, self.machine_id, typ, data))
            except Exception as e:
                self.log("webhook %s not encodable (%s); dropped" % (typ, type(e).__name__))
                self._done(typ, token)
                continue
            if len(body) > BODY_MAX:
                self.log("webhook %s of %d bytes too large; dropped" % (typ, len(body)))
                self._done(typ, token)
                continue
            try:
                resp = self.send_http.post(body)
                if resp.get("ok") is not True:
                    raise BadResponse("no ok")
            except Exception as e:
                self.connected = False
                if stopping:
                    self.log("webhook send to %s failed on shutdown (%s)" % (self.host, describe(e)))
                    return
                self.send_log.fail(describe(e), backoff)
                next_try = time.time() + backoff
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            if not self.connected:
                if not self.send_log.failing:
                    self.log("webhook connected to %s as machine %s" % (self.host, self.machine_id))
                self.send_log.ok()
            self.connected = True
            backoff = 1.0
            next_try = 0.0
            self._done(typ, token)

    def _done(self, typ, token):
        with self.cond:
            if typ == "hello":
                self.hello_done = True
            elif typ == "ack":
                if self.acks and self.acks[0] is token:
                    self.acks.pop(0)
            elif typ == "snapshot":
                if self.pending is token:
                    self.pending = None  # a newer snapshot that arrived meanwhile stays queued

    # poll thread -------------------------------------------------------------------------------------------------
    def _poll_loop(self):
        backoff = 1.0
        while not self.stop_event.is_set():
            started = time.time()
            try:
                resp = self.poll_http.post(encode(envelope(self.secret, self.machine_id, "poll", {})))
                cmds = resp.get("commands")
                if not isinstance(cmds, list):
                    raise BadResponse("no commands list")
            except Exception as e:
                if self.stop_event.is_set():
                    return
                self.poll_log.fail(describe(e), backoff)
                self.stop_event.wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX)
                continue
            self.poll_log.ok()
            backoff = 1.0
            if len(cmds) > COMMANDS_MAX:
                self.log("webhook poll returned %d commands; kept the first %d" % (len(cmds), COMMANDS_MAX))
            for env in cmds[:COMMANDS_MAX]:
                if not isinstance(env, dict):
                    self.log("cmd dropped: not an object")
                    continue
                try:
                    self.inbound.put_nowait(env)
                except queue.Full:
                    self.log("cmd dropped: inbound queue full")
            if not cmds and time.time() - started < QUICK_POLL:
                self.stop_event.wait(QUICK_POLL_PAUSE)
