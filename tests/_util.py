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
