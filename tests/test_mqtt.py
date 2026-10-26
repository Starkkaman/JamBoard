import json
import struct
import time
import unittest

import _util
from crewhub import mqtt
from crewhub.config import Config
from crewhub.daemon import MqttLink


class EncodingTest(unittest.TestCase):
    def test_remaining_length(self):
        cases = {0: b"\x00", 127: b"\x7f", 128: b"\x80\x01", 16383: b"\xff\x7f", 16384: b"\x80\x80\x01",
                 2097151: b"\xff\xff\x7f", 2097152: b"\x80\x80\x80\x01", 268435455: b"\xff\xff\xff\x7f"}
        for n, enc in cases.items():
            self.assertEqual(mqtt.encode_remaining_length(n), enc, n)
            self.assertEqual(mqtt.decode_remaining_length(b"\x30" + enc), (n, len(enc)))
        self.assertIsNone(mqtt.decode_remaining_length(b"\x30\x80"))
        with self.assertRaises(ValueError):
            mqtt.encode_remaining_length(268435456)
        with self.assertRaises(mqtt.MQTTError):
            mqtt.decode_remaining_length(b"\x30\xff\xff\xff\xff\x01")

    def test_connect_bytes(self):
        pkt = mqtt.build_connect("c1", 60)
        self.assertEqual(pkt, b"\x10\x0e\x00\x04MQTT\x04\x02\x00\x3c\x00\x02c1")
        pkt = mqtt.build_connect("c1", 60, "u", "p", will=("crew/m/status", b"offline", 1, True))
        body = b"\x00\x04MQTT\x04" + bytes([0x80 | 0x40 | 0x20 | 0x08 | 0x04 | 0x02]) + b"\x00\x3c" + \
            b"\x00\x02c1" + b"\x00\x0dcrew/m/status" + b"\x00\x07offline" + b"\x00\x01u" + b"\x00\x01p"
        self.assertEqual(pkt, b"\x10" + bytes([len(body)]) + body)

    def test_publish_bytes(self):
        self.assertEqual(mqtt.build_publish("a/b", b"hi"), b"\x30\x07\x00\x03a/bhi")
        self.assertEqual(mqtt.build_publish("a/b", b"hi", qos=1, retain=True, packet_id=10),
                         b"\x33\x09\x00\x03a/b\x00\x0ahi")
        big = mqtt.build_publish("t", b"x" * 200)
        self.assertEqual(big[:3], b"\x30\xcb\x01")  # 3 + 200 = 203 -> 0xcb 0x01
        topic, payload, qos, retain, dup, pid = mqtt.parse_publish(0x03, b"\x00\x03a/b\x00\x0ahi")
        self.assertEqual((topic, payload, qos, retain, dup, pid), ("a/b", b"hi", 1, True, False, 10))

    def test_subscribe_puback_ping(self):
        self.assertEqual(mqtt.build_subscribe(5, "crew/m/cmd", 1), b"\x82\x0f\x00\x05\x00\x0acrew/m/cmd\x01")
        self.assertEqual(mqtt.build_puback(258), b"\x40\x02\x01\x02")
        self.assertEqual(mqtt.PINGREQ_BYTES, b"\xc0\x00")
        self.assertEqual(mqtt.DISCONNECT_BYTES, b"\xe0\x00")

    def test_parse_packet_partial(self):
        pkt = mqtt.build_publish("a", b"z" * 300)
        self.assertIsNone(mqtt.parse_packet(pkt[:2]))
        self.assertIsNone(mqtt.parse_packet(pkt[:-1]))
        ptype, flags, body, total = mqtt.parse_packet(pkt + b"\xd0\x00")
        self.assertEqual((ptype, total), (mqtt.PUBLISH, len(pkt)))


def wait(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class LoopbackTest(unittest.TestCase):
    def setUp(self):
        self.broker = _util.FakeBroker()

    def tearDown(self):
        self.broker.close()

    def test_client_roundtrip(self):
        got = []
        c = mqtt.Client("127.0.0.1", self.broker.port, "test-client", "user", "pw", keepalive=60,
                        will=("crew/m/status", b"offline", 1, True), on_message=lambda t, p, r: got.append((t, p, r)),
                        timeout=3)
        self.broker.to_client = [mqtt.build_publish("crew/m/cmd", b'{"id":"1"}', qos=1, retain=True, packet_id=7),
                                 mqtt.build_publish("crew/m/cmd", b'{"id":"2"}', qos=0, retain=False)]
        c.connect()
        c.publish("crew/m/snapshot", b'{"a":1}', qos=1, retain=True)
        c.publish("crew/m/x", b"q0", qos=0)
        c.subscribe("crew/m/cmd", 1)
        self.assertTrue(wait(lambda: (c.loop(0.05) or True) and len(got) == 2))
        self.assertEqual(got, [("crew/m/cmd", b'{"id":"1"}', True), ("crew/m/cmd", b'{"id":"2"}', False)])
        c.disconnect()
        self.assertTrue(wait(lambda: self.broker.disconnects == 1))
        self.assertEqual(self.broker.publishes[0], ("crew/m/snapshot", b'{"a":1}', 1, True))
        self.assertEqual(self.broker.publishes[1], ("crew/m/x", b"q0", 0, False))
        body = self.broker.connects[0]
        self.assertEqual(body[:7], b"\x00\x04MQTT\x04")
        self.assertTrue(body[7] & 0x20)  # will retain

    def test_refused(self):
        self.broker.refuse_code = 5
        c = mqtt.Client("127.0.0.1", self.broker.port, "x", timeout=3)
        with self.assertRaises(mqtt.MQTTError):
            c.connect()
        self.assertFalse(c.connected)

    def test_no_broker_raises(self):
        self.broker.close()
        c = mqtt.Client("127.0.0.1", self.broker.port, "x", timeout=1)
        with self.assertRaises(OSError):
            c.connect()


class MqttLinkTest(unittest.TestCase):
    def test_link_online_snapshot_and_cmd_logging(self):
        broker = _util.FakeBroker()
        logs = []
        try:
            cfg = Config({"machineId": "NAS box", "mqtt": {"host": "127.0.0.1", "port": broker.port,
                                                           "username": "crew-nas", "password": "s3cret"}})
            self.assertEqual(cfg.topic("snapshot"), "crew/nas-box/snapshot")
            link = MqttLink(cfg, logs.append)
            broker.to_client = [mqtt.build_publish("crew/nas-box/cmd", b'{"id":"r","action":"stop"}', qos=1,
                                                   retain=True, packet_id=3),
                                mqtt.build_publish("crew/nas-box/cmd", b'{"id":"9","action":"send_message","args":{"text":"T0P"}}',
                                                   qos=1, packet_id=4)]
            self.assertFalse(link.publish(b'{"s":1}'))  # not connected yet: kept as pending
            self.assertTrue(wait(lambda: link.step(0.05) and len(broker.publishes) >= 2 and
                                 any("id=9" in m for m in logs)))
            self.assertEqual(broker.publishes[0], ("crew/nas-box/status", b"online", 1, True))
            self.assertEqual(broker.publishes[1], ("crew/nas-box/snapshot", b'{"s":1}', 1, True))
            self.assertTrue(any("retained" in m for m in logs))
            self.assertFalse(any("T0P" in m for m in logs))  # command args are never logged
            self.assertFalse(any("s3cret" in m for m in logs))
            link.offline()
            self.assertTrue(wait(lambda: broker.disconnects == 1))
            self.assertEqual(broker.publishes[-1], ("crew/nas-box/status", b"offline", 1, True))
        finally:
            broker.close()

    def test_backoff_never_raises(self):
        broker = _util.FakeBroker()
        port = broker.port
        broker.close()
        logs = []
        link = MqttLink(Config({"machineId": "m", "mqtt": {"host": "127.0.0.1", "port": port}}), logs.append)
        self.assertFalse(link.step(0.01))
        self.assertEqual(link.backoff, 2.0)
        link.next_try = 0
        link.step(0.01)
        self.assertEqual(link.backoff, 4.0)
        for _ in range(10):
            link.next_try = 0
            link.step(0.01)
        self.assertEqual(link.backoff, 60.0)
        self.assertFalse(any("password" in m for m in logs))


if __name__ == "__main__":
    unittest.main()
