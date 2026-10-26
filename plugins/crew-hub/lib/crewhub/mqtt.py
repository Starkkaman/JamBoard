"""Minimal MQTT 3.1.1 client (stdlib only): CONNECT with will + login, PUBLISH QoS 0/1, SUBSCRIBE, PINGREQ, DISCONNECT.

Single-threaded and blocking with timeouts; the daemon calls loop() from its main loop. Any socket or protocol error
raises MQTTError (or OSError) and leaves the client disconnected; the caller reconnects with backoff.
"""

import select
import socket
import struct
import time

CONNECT, CONNACK, PUBLISH, PUBACK, SUBSCRIBE, SUBACK, PINGREQ, PINGRESP, DISCONNECT = 1, 2, 3, 4, 8, 9, 12, 13, 14


class MQTTError(Exception):
    pass


# ---- encoding ----------------------------------------------------------------------------------------------------
def encode_remaining_length(n):
    if n < 0 or n > 268435455:
        raise ValueError("remaining length out of range")
    out = bytearray()
    while True:
        byte = n % 128
        n //= 128
        if n:
            byte |= 0x80
        out.append(byte)
        if not n:
            return bytes(out)


def decode_remaining_length(buf, start=1):
    """(value, bytes used) or None if `buf` doesn't hold the whole field yet."""
    mult, value = 1, 0
    for i in range(4):
        if start + i >= len(buf):
            return None
        b = buf[start + i]
        value += (b & 0x7F) * mult
        if not b & 0x80:
            return value, i + 1
        mult *= 128
    raise MQTTError("malformed remaining length")


def _utf8(s):
    b = s.encode("utf-8") if isinstance(s, str) else bytes(s)
    if len(b) > 65535:
        raise ValueError("string too long")
    return struct.pack("!H", len(b)) + b


def _packet(ptype, flags, body):
    return bytes([(ptype << 4) | flags]) + encode_remaining_length(len(body)) + body


def build_connect(client_id, keepalive=60, username=None, password=None, will=None, clean=True):
    """will = (topic, payload bytes, qos, retain) or None."""
    flags = 0x02 if clean else 0
    payload = _utf8(client_id)
    if will:
        topic, msg, qos, retain = will
        flags |= 0x04 | ((qos & 3) << 3) | (0x20 if retain else 0)
        payload += _utf8(topic) + _utf8(msg if isinstance(msg, bytes) else str(msg).encode("utf-8"))
    if username is not None:
        flags |= 0x80
        payload += _utf8(username)
        if password is not None:
            flags |= 0x40
            payload += _utf8(password)
    body = _utf8("MQTT") + bytes([4, flags]) + struct.pack("!H", keepalive) + payload
    return _packet(CONNECT, 0, body)


def build_publish(topic, payload, qos=0, retain=False, packet_id=None, dup=False):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    flags = (0x08 if dup else 0) | ((qos & 3) << 1) | (1 if retain else 0)
    body = _utf8(topic)
    if qos:
        body += struct.pack("!H", packet_id)
    return _packet(PUBLISH, flags, body + payload)


def build_subscribe(packet_id, topic, qos=1):
    return _packet(SUBSCRIBE, 0x02, struct.pack("!H", packet_id) + _utf8(topic) + bytes([qos]))


def build_puback(packet_id):
    return _packet(PUBACK, 0, struct.pack("!H", packet_id))


PINGREQ_BYTES = bytes([PINGREQ << 4, 0])
DISCONNECT_BYTES = bytes([DISCONNECT << 4, 0])


def parse_packet(buf):
    """(ptype, flags, body, total length) for the first complete packet in `buf`, or None."""
    if len(buf) < 2:
        return None
    rl = decode_remaining_length(buf, 1)
    if rl is None:
        return None
    length, used = rl
    total = 1 + used + length
    if len(buf) < total:
        return None
    return buf[0] >> 4, buf[0] & 0x0F, bytes(buf[1 + used : total]), total


def parse_publish(flags, body):
    """(topic, payload, qos, retain, dup, packet_id)."""
    tlen = struct.unpack("!H", body[:2])[0]
    topic = body[2 : 2 + tlen].decode("utf-8", "replace")
    pos = 2 + tlen
    qos = (flags >> 1) & 3
    pid = None
    if qos:
        pid = struct.unpack("!H", body[pos : pos + 2])[0]
        pos += 2
    return topic, body[pos:], qos, bool(flags & 1), bool(flags & 8), pid


# ---- client ------------------------------------------------------------------------------------------------------
class Client(object):
    def __init__(self, host, port=1883, client_id="crew-hubd", username=None, password=None, keepalive=60,
                 will=None, on_message=None, timeout=10.0):
        self.host, self.port = host, port
        self.client_id = client_id
        self.username, self.password = username, password
        self.keepalive = keepalive
        self.will = will
        self.on_message = on_message
        self.timeout = timeout
        self.sock = None
        self.buf = bytearray()
        self.next_id = 0
        self.last_send = 0.0
        self.last_recv = 0.0
        self.ping_outstanding = 0.0

    @property
    def connected(self):
        return self.sock is not None

    def _pid(self):
        self.next_id = self.next_id % 65535 + 1
        return self.next_id

    def _send(self, data):
        if self.sock is None:
            raise MQTTError("not connected")
        try:
            self.sock.sendall(data)
        except OSError:
            self.close()
            raise
        self.last_send = time.time()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buf = bytearray()
        self.ping_outstanding = 0.0

    def connect(self):
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = sock
        self.last_recv = time.time()
        self._send(build_connect(self.client_id, self.keepalive, self.username, self.password, self.will))
        ptype, _, body = self._wait_for(lambda t, f, b: t == CONNACK, self.timeout)
        if len(body) < 2 or body[1] != 0:
            code = body[1] if len(body) >= 2 else -1
            self.close()
            raise MQTTError("connection refused, code %s" % code)

    def disconnect(self):
        try:
            if self.sock is not None:
                self._send(DISCONNECT_BYTES)
        except Exception:
            pass
        self.close()

    def publish(self, topic, payload, qos=0, retain=False):
        """QoS 1 blocks until PUBACK (or raises after `timeout`)."""
        if qos:
            pid = self._pid()
            self._send(build_publish(topic, payload, qos, retain, pid))
            self._wait_for(lambda t, f, b: t == PUBACK and b[:2] == struct.pack("!H", pid), self.timeout)
        else:
            self._send(build_publish(topic, payload, 0, retain))

    def subscribe(self, topic, qos=1):
        pid = self._pid()
        self._send(build_subscribe(pid, topic, qos))
        _, _, body = self._wait_for(lambda t, f, b: t == SUBACK and b[:2] == struct.pack("!H", pid), self.timeout)
        if len(body) >= 3 and body[2] == 0x80:
            raise MQTTError("subscription to %s refused" % topic)

    def loop(self, timeout=0.5):
        """Handle incoming packets for up to `timeout` seconds and keep the connection alive."""
        if self.sock is None:
            raise MQTTError("not connected")
        now = time.time()
        if self.ping_outstanding and now - self.ping_outstanding > self.keepalive:
            self.close()
            raise MQTTError("no PINGRESP")
        if now - self.last_send >= self.keepalive / 2.0 and not self.ping_outstanding:
            self._send(PINGREQ_BYTES)
            self.ping_outstanding = now
        self._read(timeout)
        while True:
            pkt = self._next_packet()
            if pkt is None:
                break
            self._dispatch(*pkt)

    # ---- internals --------------------------------------------------------------------------------------------
    def _read(self, timeout):
        try:
            r, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
        except (OSError, ValueError):
            self.close()
            raise MQTTError("select failed")
        if not r:
            return False
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return False
        except OSError:
            self.close()
            raise
        if not data:
            self.close()
            raise MQTTError("connection closed by broker")
        self.buf += data
        self.last_recv = time.time()
        return True

    def _next_packet(self):
        pkt = parse_packet(self.buf)
        if pkt is None:
            return None
        ptype, flags, body, total = pkt
        del self.buf[:total]
        return ptype, flags, body

    def _dispatch(self, ptype, flags, body):
        if ptype == PUBLISH:
            topic, payload, qos, retain, dup, pid = parse_publish(flags, body)
            if qos == 1 and pid is not None:
                self._send(build_puback(pid))
            if self.on_message:
                try:
                    self.on_message(topic, payload, retain)
                except Exception:
                    pass
        elif ptype == PINGRESP:
            self.ping_outstanding = 0.0
        # stray PUBACK/SUBACK: ignore

    def _wait_for(self, match, timeout):
        deadline = time.time() + timeout
        while True:
            while True:
                pkt = self._next_packet()
                if pkt is None:
                    break
                if match(*pkt):
                    return pkt
                self._dispatch(*pkt)
            left = deadline - time.time()
            if left <= 0:
                self.close()
                raise MQTTError("timed out waiting for broker")
            if self.sock is None:
                raise MQTTError("not connected")
            self._read(left)
