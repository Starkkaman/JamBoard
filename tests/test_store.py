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
        self.assertEqual(self.store.working_count(), 3)  # fixture: Jamfin, NerveOS, Crew Hub
        self.assertTrue(self.store.any_waiting())
        self.assertEqual(self.store.waiting_names(), ["Home Assistant Mr Manager", "Music Assistant tidy-up"])

    def test_offline_machine_sessions_do_not_count(self):
        self.store.handle_message("crew/nas/snapshot", json.dumps(nas_snapshot()))
        self.assertEqual(self.store.working_count(), 3)  # fixture: Jamfin, NerveOS, Crew Hub
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


JAMFIN = "8c1f2a4e-3b7d-4e19-9a61-0f2d5c7b9e10"
ERRDEMO = "e5c7a913-0b2d-4f6e-8a14-c3d5e7f9a1b2"
DAY = 24 * 3600 * 1000


def _sess(view, sid):
    return next((s for s in view["snapshot"]["sessions"] if s["id"] == sid), None)


class DismissTest(unittest.TestCase):
    """0.4.0 crew_hub/dismiss: filtering of outgoing data, reappearing sessions, pruning, persistence round trip."""

    def setUp(self):
        self.t = 1000
        self.store = CrewStore("crew", {"nas": "NAS"}, clock=lambda: self.t)
        self.events = []
        self.saves = 0
        self.store.add_listener(self.events.append)
        self.store.on_dismissed_change = self._saved
        self.snap = nas_snapshot()
        self.store.handle_message("crew/nas/snapshot", json.dumps(self.snap))

    def _saved(self):
        self.saves += 1

    def _push(self):
        self.store.handle_message("crew/nas/snapshot", json.dumps(self.snap))

    def test_agents_filtered_out_of_events_and_as_dict_only(self):
        done = ["a19b8e7c2d3f4a55", "a5c1e0d2b7f94e31"]
        self.assertIsNone(self.store.dismiss("nas", JAMFIN, done))
        self.assertEqual(self.saves, 1)
        ev = self.events[-1]
        self.assertEqual(ev["type"], "machine")
        ids = [a["id"] for a in _sess(ev["data"], JAMFIN)["agents"]]
        self.assertFalse(set(done) & set(ids))
        self.assertIn("a7f3c2e1d9b84a60", ids)
        self.assertEqual(ids, [a["id"] for a in _sess(self.store.as_dict()["nas"], JAMFIN)["agents"]])
        # commands/diagnostics read the real snapshot, and the stored snapshot is never modified
        self.assertEqual(self.store.machine("nas")["snapshot"], self.snap)
        self._push()
        self.assertFalse(set(done) & {a["id"] for a in _sess(self.events[-1]["data"], JAMFIN)["agents"]})

    def test_no_dismissals_returns_the_same_snapshot(self):
        self.assertIs(self.store.as_dict()["nas"]["snapshot"], self.store.machine("nas")["snapshot"])

    def test_session_hidden_while_idle_and_back_when_working(self):
        self.assertIsNone(self.store.dismiss("nas", ERRDEMO, session_dismissed=True))
        self.assertIsNone(_sess(self.events[-1]["data"], ERRDEMO))
        self.assertIn(ERRDEMO, self.store.dismissed_data()["nas"])
        for s in self.snap["sessions"]:
            if s["id"] == ERRDEMO:
                s["status"] = "working"
        self._push()
        self.assertIsNotNone(_sess(self.events[-1]["data"], ERRDEMO))
        self.assertNotIn("nas", self.store.dismissed_data())  # flag cleared, nothing left
        self.assertEqual(self.saves, 2)
        for s in self.snap["sessions"]:
            if s["id"] == ERRDEMO:
                s["status"] = "idle"
        self._push()
        self.assertIsNotNone(_sess(self.events[-1]["data"], ERRDEMO))  # stays visible until dismissed again

    def test_working_session_is_not_hidden(self):
        self.assertIsNone(self.store.dismiss("nas", JAMFIN, session_dismissed=True))
        self.assertIsNotNone(_sess(self.events[-1]["data"], JAMFIN))
        self.assertEqual(self.store.dismissed_data(), {})

    def test_refusals(self):
        self.assertEqual(self.store.dismiss("mac", ERRDEMO, session_dismissed=True), "unknown_machine")
        self.assertEqual(self.store.dismiss("bad id", ERRDEMO, session_dismissed=True), "unknown_machine")
        self.assertEqual(self.store.dismiss("nas", "", session_dismissed=True), "invalid_args")
        self.assertEqual(self.store.dismiss("nas", ERRDEMO), "invalid_args")
        self.assertEqual(self.store.dismiss("nas", ERRDEMO, ["x" * 200]), "invalid_args")
        self.assertEqual(self.store.dismiss("nas", ERRDEMO, "abc"), "invalid_args")
        self.assertEqual(self.saves, 0)

    def test_pruned_after_24h_unseen(self):
        self.store.dismiss("nas", JAMFIN, ["a19b8e7c2d3f4a55"])
        self.store.dismiss("nas", ERRDEMO, session_dismissed=True)
        self.store.dismiss("nas", "gone-session", ["gone-agent"])
        saves = self.saves
        # the agent and the session leave the snapshot
        jam = next(s for s in self.snap["sessions"] if s["id"] == JAMFIN)
        jam["agents"] = [a for a in jam["agents"] if a["id"] != "a19b8e7c2d3f4a55"]
        self.t += DAY - 1
        self._push()
        self.assertEqual(set(self.store.dismissed_data()["nas"]), {JAMFIN, ERRDEMO, "gone-session"})
        self.assertEqual(self.saves, saves)  # only seen times changed: no save
        self.t += 2
        self._push()
        self.assertEqual(self.store.dismissed_data()["nas"].keys(), {ERRDEMO})
        self.assertGreater(self.saves, saves)

    def test_load_round_trip_and_bad_data(self):
        self.store.dismiss("nas", JAMFIN, ["a19b8e7c2d3f4a55"])
        self.store.dismiss("nas", ERRDEMO, session_dismissed=True)
        data = json.loads(json.dumps(self.store.dismissed_data()))
        other = CrewStore("crew", {"nas": "NAS"}, clock=lambda: self.t)
        other.load_dismissed(data)
        other.handle_message("crew/nas/snapshot", json.dumps(self.snap))
        self.assertIsNone(_sess(other.as_dict()["nas"], ERRDEMO))
        self.assertEqual(other.dismissed_data(), self.store.dismissed_data())
        other.load_dismissed({"bad id": {}, "nas": {"s": {"agents": [1, "ok"]}, "t": "x", "u": {"session": "yes"}}, "m": []})
        self.assertEqual(other.dismissed_data(), {"nas": {"s": {"agents": ["ok"], "session": False, "seen": self.t,
                                                                  "agentSeen": {"ok": self.t}}}})
        other.load_dismissed(None)
        self.assertEqual(other.dismissed_data(), {})

    def test_reset_forgets_dismissals(self):
        self.store.dismiss("nas", ERRDEMO, session_dismissed=True)
        self.store.reset()
        self.assertEqual(self.store.dismissed_data(), {})


if __name__ == "__main__":
    unittest.main()
