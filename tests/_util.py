import json
import os
import socket
import struct
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN = os.path.join(ROOT, "plugins", "crew-hub")
LIB = os.path.join(PLUGIN, "lib")
HOOK = os.path.join(PLUGIN, "bin", "crew-hook")
DAEMON = os.path.join(PLUGIN, "bin", "crew-hubd")
SAMPLE = os.path.join(ROOT, "docs", "evidence", "hooks-2.1.295-sample.jsonl")

if LIB not in sys.path:
    sys.path.insert(0, LIB)


def sample_payloads():
    with open(SAMPLE, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


class FakeBroker(object):
    """Tiny MQTT 3.1.1 broker for one client at a time: CONNACK, PUBACK, SUBACK, PINGRESP; records everything.

    `to_client` packets (raw bytes) are sent right after the SUBACK, e.g. a retained or live cmd message.
    """

    def __init__(self, refuse_code=0):
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.refuse_code = refuse_code
        self.connects = []  # raw CONNECT bodies
        self.publishes = []  # (topic, payload, qos, retain)
        self.subscribes = []
        self.disconnects = 0
        self.to_client = []
        self.event = threading.Event()
        self.closed = False
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def close(self):
        self.closed = True
        try:
            self.srv.shutdown(socket.SHUT_RDWR)  # wakes the blocked accept() on Linux
        except OSError:
            pass
        try:
            self.srv.close()
        except OSError:
            pass

    def _recv_packet(self, conn, buf):
        from crewhub.mqtt import parse_packet

        while True:
            pkt = parse_packet(buf)
            if pkt is not None:
                del buf[: pkt[3]]
                return pkt[0], pkt[1], pkt[2]
            data = conn.recv(65536)
            if not data:
                return None
            buf += data

    def _serve(self):
        from crewhub.mqtt import parse_publish

        while not self.closed:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            buf = bytearray()
            try:
                while True:
                    pkt = self._recv_packet(conn, buf)
                    if pkt is None:
                        break
                    ptype, flags, body = pkt
                    if ptype == 1:
                        self.connects.append(body)
                        conn.sendall(bytes([0x20, 2, 0, self.refuse_code]))
                        if self.refuse_code:
                            break
                    elif ptype == 3:
                        topic, payload, qos, retain, dup, pid = parse_publish(flags, body)
                        self.publishes.append((topic, payload, qos, retain))
                        if qos == 1:
                            conn.sendall(bytes([0x40, 2]) + struct.pack("!H", pid))
                    elif ptype == 8:
                        pid = body[:2]
                        self.subscribes.append(body)
                        conn.sendall(bytes([0x90, 3]) + pid + bytes([1]))
                        for raw in self.to_client:
                            conn.sendall(raw)
                    elif ptype == 4:
                        pass  # PUBACK from client
                    elif ptype == 12:
                        conn.sendall(bytes([0xD0, 0]))
                    elif ptype == 14:
                        self.disconnects += 1
                        break
                    self.event.set()
            except OSError:
                pass
            finally:
                conn.close()
                self.event.set()


# ---- fake Home Assistant webhook endpoint (docs/WEBHOOK.md), plain http on localhost --------------------------------
def webhook_canonical(env):
    """Written from the WEBHOOK.md text, independent of crewhub.webhook."""
    keys = ("v", "m", "ts", "nonce", "type", "data")
    return json.dumps({k: env.get(k) for k in keys}, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def webhook_sig(secret, env):
    import hashlib
    import hmac

    return hmac.new(secret.encode("utf-8"), webhook_canonical(env), hashlib.sha256).hexdigest()


def command_envelope(secret, action, args, ts=None, cid=None):
    """A COMMANDS.md envelope signed independently of crewhub.commands."""
    import hashlib
    import hmac
    import time
    import uuid

    env = {"v": 1, "id": cid or str(uuid.uuid4()), "ts": int(time.time() * 1000) if ts is None else ts,
           "action": action, "args": args}
    canon = json.dumps({k: env[k] for k in ("v", "id", "ts", "action", "args")}, sort_keys=True,
                       separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    env["sig"] = hmac.new(secret.encode("utf-8"), canon, hashlib.sha256).hexdigest()
    return env


class FakeHub(object):
    """Plays Home Assistant's side of the webhook: verifies every request like WEBHOOK.md says (401 + empty body on
    any failure, 200 + empty body for an unknown webhook id, like HA), holds polls up to `hold` seconds and hands out
    queued commands. Records accepted envelopes per type and every request's arrival time."""

    def __init__(self, webhook_id, secret, machine, hold=1.0):
        import http.server
        import time

        self.webhook_id = webhook_id
        self.secret = secret
        self.machine = machine
        self.hold = hold
        self.fail_status = None  # e.g. 500: answer every request with it
        self.cond = threading.Condition()
        self.queue = []  # commands for the next poll
        self.accepted = []  # envelopes, in order
        self.rejected = []  # reasons
        self.requests = []  # (time, type or None)
        self.paths = []
        self.nonces = set()
        self.closing = False
        hub = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, obj=None):
                body = b"" if obj is None else json.dumps(obj).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    self.close_connection = True  # the client went away (e.g. it aborted a held poll)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                with hub.cond:
                    hub.paths.append(self.path)
                status, obj = hub.handle(self.path, raw)
                self._send(status, obj)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self._time = time.time

    def close(self):
        with self.cond:
            self.closing = True
            self.cond.notify_all()
        self.server.shutdown()
        self.server.server_close()

    def push(self, *commands):
        with self.cond:
            self.queue.extend(commands)
            self.cond.notify_all()

    def of_type(self, typ):
        with self.cond:
            return [e for e in self.accepted if e["type"] == typ]

    def handle(self, path, raw):
        import hmac

        now = self._time()
        try:
            env = json.loads(raw.decode("utf-8"))
        except Exception:
            env = None
        typ = env.get("type") if isinstance(env, dict) else None
        with self.cond:
            self.requests.append((now, typ))
            if self.fail_status:
                return self.fail_status, {"error": "fake failure"}
        if path != "/api/webhook/" + self.webhook_id:
            return 200, None  # HA: unknown webhook id -> 200, empty body
        reason = None
        if len(raw) > 256 * 1024:
            reason = "too large"
        elif not isinstance(env, dict) or env.get("v") != 1 or not isinstance(env.get("sig"), str) \
                or typ not in ("hello", "snapshot", "poll", "ack") or not isinstance(env.get("data"), dict) \
                or not isinstance(env.get("nonce"), str) or not isinstance(env.get("ts"), int):
            reason = "shape"
        elif env.get("m") != self.machine:
            reason = "machine"
        elif abs(now * 1000 - env["ts"]) > 120 * 1000:
            reason = "ts"
        elif not hmac.compare_digest(webhook_sig(self.secret, env), env["sig"]):
            reason = "sig"
        elif env["nonce"] in self.nonces:
            reason = "nonce"
        if reason:
            with self.cond:
                self.rejected.append(reason)
            return 401, None
        with self.cond:
            self.nonces.add(env["nonce"])
            self.accepted.append(env)
            self.cond.notify_all()
            if typ != "poll":
                return 200, {"ok": True}
            end = now + self.hold
            while not self.queue and not self.closing and self._time() < end:
                self.cond.wait(max(0.01, end - self._time()))
            out, self.queue = self.queue, []
        return 200, {"commands": out}
