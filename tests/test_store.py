"""Pure-Python tests of the crew_hub integration's store (no Home Assistant needed).

store.py is loaded by file path so the package __init__ (which imports homeassistant) is not executed.
"""

import importlib.util
import json
import os
import sys
import unittest

from _util import ROOT

_PATH = os.path.join(ROOT, "custom_components", "crew_hub", "store.py")
_spec = importlib.util.spec_from_file_location("crew_hub_store", _PATH)
store_mod = importlib.util.module_from_spec(_spec)
sys.modules["crew_hub_store"] = store_mod
_spec.loader.exec_module(store_mod)
CrewStore = store_mod.CrewStore

FIXTURE = os.path.join(ROOT, "card", "fixtures", "snapshot-sample.json")


def nas_snapshot():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        snap = json.load(f)["nas"]
    snap.pop("status", None)
    return snap


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.t = 1000
        self.store = CrewStore("crew", [{"id": "nas", "name": "NAS", "secret": "never-kept"}], clock=lambda: self.t)
        self.events = []
        self.store.add_listener(self.events.append)

    def test_initial_view_lists_configured_machines(self):
        self.assertEqual(self.store.as_dict(),
                         {"nas": {"status": "unknown", "snapshot": None, "receivedAt": None, "configured": True}})
        self.assertNotIn("never-kept", json.dumps(self.store.as_dict()))
        self.assertEqual(self.store.topics, ["crew/+/snapshot", "crew/+/status"])

    def test_status_and_snapshot(self):
        self.assertTrue(self.store.handle_message("crew/nas/status", b"online"))
        self.assertEqual(self.events[-1], {"type": "machine", "id": "nas",
                                           "data": {"status": "online", "snapshot": None, "receivedAt": 1000, "configured": True}})
        self.assertFalse(self.store.handle_message("crew/nas/status", "online"))  # unchanged: no event
        self.assertEqual(len(self.events), 1)
        snap = nas_snapshot()
        self.t = 2000
        self.assertTrue(self.store.handle_message("crew/nas/snapshot", json.dumps(snap).encode()))
        data = self.events[-1]["data"]
        self.assertEqual(data["snapshot"], snap)
        self.assertEqual(data["status"], "online")
        self.assertEqual(data["receivedAt"], 2000)
        self.assertEqual(self.store.working_count(), 1)
        self.assertTrue(self.store.any_waiting())
        self.assertEqual(self.store.waiting_names(), ["Home Assistant Mr Manager", "Music Assistant tidy-up"])

    def test_offline_machine_sessions_do_not_count(self):
        self.store.handle_message("crew/nas/snapshot", json.dumps(nas_snapshot()))
        self.assertEqual(self.store.working_count(), 1)
        self.store.handle_message("crew/nas/status", "offline")
        self.assertEqual(self.store.working_count(), 0)
        self.assertFalse(self.store.any_waiting())
        self.assertEqual(self.store.machine("nas")["status"], "offline")

    def test_bad_input_is_ignored(self):
        bad = [
            ("crew/nas/snapshot", b"{not json"),
            ("crew/nas/snapshot", b"[1, 2]"),
            ("crew/nas/snapshot", b"\xff\xfe"),
            ("crew/nas/snapshot", json.dumps({"x": "a" * (256 * 1024)}).encode()),
            ("crew/nas/cmd", b"{}"),
            ("crew/nas/extra/snapshot", b"{}"),
            ("other/nas/snapshot", b"{}"),
            ("crew/bad id/snapshot", b"{}"),
            ("crew/nas/snapshot", 12),
        ]
        for topic, payload in bad:
            self.assertFalse(self.store.handle_message(topic, payload), topic)
        self.assertEqual(self.events, [])
        self.assertIsNone(self.store.machine("nas")["snapshot"])

    def test_unknown_status_text_and_cleared_snapshot(self):
        self.store.handle_message("crew/nas/status", "online")
        self.store.handle_message("crew/nas/status", "weird")
        self.assertEqual(self.store.status("nas"), "unknown")
        self.store.handle_message("crew/nas/snapshot", "{}")
        self.assertEqual(self.store.machine("nas")["snapshot"], {})
        self.store.handle_message("crew/nas/snapshot", b"")  # retained message cleared
        self.assertIsNone(self.store.machine("nas")["snapshot"])

    def test_unconfigured_machine_shows_and_cap(self):
        self.store.handle_message("crew/mac/snapshot", "{}")
        self.assertFalse(self.store.machine("mac")["configured"])
        for i in range(store_mod.MAX_MACHINES + 5):
            self.store.handle_message("crew/m%d/status" % i, "online")
        self.assertLessEqual(len(self.store.as_dict()), store_mod.MAX_MACHINES + 1)  # + configured nas
        # configured machines are always accepted, even at the cap
        self.assertTrue(self.store.handle_message("crew/nas/status", "online"))

    def test_reconfigure_prefix_reset_and_listeners(self):
        self.store.handle_message("crew/nas/status", "online")
        self.store.set_configured({"nas": "NAS", "mac": "Mac"})
        self.assertEqual(self.events[-1]["type"], "full")
        self.assertEqual(set(self.events[-1]["machines"]), {"nas", "mac"})
        n = len(self.events)
        self.store.set_configured({"nas": "NAS", "mac": "Mac"})  # unchanged: no event
        self.assertEqual(len(self.events), n)
        self.store.set_prefix("crew2")
        self.assertEqual(self.store.status("nas"), "unknown")
        self.assertFalse(self.store.handle_message("crew/nas/status", "online"))
        self.assertTrue(self.store.handle_message("crew2/nas/status", "online"))
        self.store.reset()
        self.assertEqual(self.events[-1], {"type": "full", "machines": {}})

    def test_listener_errors_are_isolated_and_remove_works(self):
        got = []

        def boom(event):
            raise RuntimeError("x")

        self.store.add_listener(boom)
        remove = self.store.add_listener(got.append)
        with self.assertLogs(store_mod._LOGGER, "ERROR"):
            self.store.handle_message("crew/nas/status", "online")
        self.assertEqual(len(got), 1)
        remove()
        remove()  # idempotent
        self.store.handle_message("crew/nas/status", "offline")
        self.assertEqual(len(got), 1)


if __name__ == "__main__":
    unittest.main()
