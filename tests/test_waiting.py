"""0.4.1 waiting rules (state.py module doc): waiting means a prompt is open right now.

One test per false-"needs you" cause found in the 2026-10-27 diagnosis, plus registry-driven set/clear, deny in
claude.ai (no PostToolUse), a running hold, registry write lag and the no-registry expiries."""

import json
import os
import shutil
import tempfile
import time
import unittest
import uuid

import _util  # noqa: F401
from crewhub import inbox
from crewhub.state import NOREG_MAX_MS, NOTE_MAX_MS, Model

SID = "s"


class WaitingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.sessions = os.path.join(self.tmp, "sessions")
        os.makedirs(self.sessions)
        self.t = time.time()
        self.m = self.model()

    def model(self):
        m = Model()
        m.apply({"v": 1, "ts": self.t - 60, "ev": "SessionStart", "sid": SID, "source": "startup"})
        return m

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def ev(self, ev, dt, **kw):
        rec = {"v": 1, "ts": self.t + dt, "ev": ev, "sid": SID}
        rec.update(kw)
        self.m.apply(rec)
        return self.m.sessions[SID]

    def ask(self, dt, hold=True, hold_s=55, agent=None, tool="AskUserQuestion"):
        rid = str(uuid.uuid4())
        kw = {"tool": tool, "rid": rid, "hold": hold}
        if hold:
            kw["holdS"] = hold_s
        if agent:
            kw["agent"] = agent
        if tool == "AskUserQuestion":
            kw["questions"] = [{"question": "Which?", "multiSelect": False, "options": ["A", "B"]}]
        self.ev("PermissionRequest", dt, **kw)
        return rid

    def reg(self, status, at_dt=None, waiting_for=None, sid=SID):
        pid = os.getpid()
        at = int((self.t + (at_dt or 0)) * 1000) if at_dt is not None else int((self.t - 600) * 1000)
        entry = {"pid": pid, "sessionId": sid, "procStart": inbox.proc_start(pid), "kind": "interactive",
                 "status": status, "statusUpdatedAt": at, "updatedAt": at}
        if waiting_for:
            entry["waitingFor"] = waiting_for
        with open(os.path.join(self.sessions, "%d.json" % pid), "w") as f:
            json.dump(entry, f)

    def no_reg(self):
        for n in os.listdir(self.sessions):
            os.unlink(os.path.join(self.sessions, n))

    def enrich(self, dt):
        self.m.enrich(self.sessions, now=int((self.t + dt) * 1000))
        return self.m.sessions[SID]

    def state(self, s=None):
        s = s or self.m.sessions[SID]
        return s["status"], (s["waiting"] or {}).get("kind")

    # ---- causes from the diagnosis ----
    def test_cause_idle_prompt_is_idle_not_waiting(self):
        # Interactive sessions get "Claude is waiting for your input" 60 s after a turn: that's idle, not a prompt.
        self.ev("UserPromptSubmit", 0)
        self.ev("PreToolUse", 1, tool="Bash")
        s = self.ev("Notification", 2, ntype="idle_prompt", msg="Claude is waiting for your input")
        self.assertEqual((s["status"], s["waiting"]), ("idle", None))
        self.ev("Stop", 3)
        s = self.ev("Notification", 63, ntype="idle_prompt", msg="Claude is waiting for your input")
        self.assertEqual((s["status"], s["waiting"]), ("idle", None))
        # no registry and a registry saying idle agree
        self.assertEqual(self.state(self.enrich(70)), ("idle", None))
        self.reg("idle", 3)
        self.assertEqual(self.state(self.enrich(73)), ("idle", None))

    def test_cause_notification_without_request_no_registry(self):
        # A permission_prompt Notification with no open request used to stay waiting until the next event (none
        # comes after a deny). Without a registry it now lasts at most NOTE_MAX_MS.
        self.ev("PreToolUse", 0, tool="Bash")
        s = self.ev("Notification", 6, ntype="permission_prompt", msg="Claude needs your permission to use Bash")
        self.assertEqual(self.state(s), ("waiting", "permission"))
        self.assertEqual(s["waiting"]["text"], "Claude needs your permission to use Bash")
        self.assertEqual(self.state(self.enrich(6 + NOTE_MAX_MS / 1000 - 1)), ("waiting", "permission"))
        self.assertEqual(self.state(self.enrich(6 + NOTE_MAX_MS / 1000 + 1)), ("idle", None))

    def test_cause_notification_without_request_follows_registry(self):
        self.reg("busy", 0)
        self.enrich(0.5)
        self.ev("Notification", 6, ntype="permission_prompt", msg="Claude needs your permission")
        # the registry still says busy: kept for the write lag, then dropped
        self.assertEqual(self.state(self.enrich(7)), ("waiting", "permission"))
        self.assertEqual(self.state(self.enrich(10)), ("working", None))
        # the registry says waiting: shown, with the notification text
        self.reg("waiting", 11, "permission prompt")
        self.ev("Notification", 12, ntype="permission_prompt", msg="Claude needs your permission")
        s = self.enrich(13)
        self.assertEqual(self.state(s), ("waiting", "permission"))
        self.reg("idle", 20)
        self.assertEqual(self.state(self.enrich(21)), ("idle", None))

    def test_cause_deny_in_claude_ai_cleared_by_registry(self):
        # Denied in claude.ai/terminal: no PostToolUse, PermissionDenied or Stop. The registry leaving "waiting"
        # after the request ends it, even while crew-hook still holds (it holds until its timeout regardless).
        self.reg("waiting", 0.2, "permission prompt")
        self.enrich(0.1)
        rid = self.ask(0.1, tool="Bash")
        self.assertEqual(self.state(self.enrich(1)), ("waiting", "permission"))
        self.assertEqual(self.m.waiting_state(SID, rid), ("ok", "permission"))
        self.reg("idle", 9)
        s = self.enrich(10)
        self.assertEqual((s["status"], s["waiting"]), ("idle", None))
        self.assertNotIn("_reqs", s)
        self.assertEqual(self.m.waiting_state(SID, rid), ("not_waiting", None))

    def test_cause_deny_in_claude_ai_cleared_by_next_progress_event(self):
        # No registry (other machines, print sessions): the next progress event of the session, agents included.
        for ev, kw in (("PreToolUse", {"agent": "a1", "tool": "Read"}), ("PostToolUseFailure", {"tool": "Read"}),
                       ("UserPromptSubmit", {}), ("SubagentStart", {"agent": "a2"}), ("TaskCreated", {"task_id": "1"}),
                       ("PermissionDenied", {"tool": "Read"}), ("Stop", {})):
            self.m = self.model()
            self.ask(0, hold=False, tool="Bash")
            self.assertEqual(self.state(), ("waiting", "permission"), ev)
            s = self.ev(ev, 5, **kw)
            self.assertNotEqual(s["status"], "waiting", ev)
            self.assertIsNone(s["waiting"], ev)

    def test_cause_unanswered_kept_only_while_registry_waiting(self):
        self.reg("waiting", 0.1, "input needed")
        self.enrich(0.2)
        rid = self.ask(0.1)
        self.ev("unanswered", 55.2, rid=rid, tool="AskUserQuestion", why="timeout")
        s = self.enrich(56)
        self.assertEqual(self.state(s), ("waiting", "question"))
        self.assertFalse(s["waiting"]["hubCanAnswer"])
        self.assertEqual(self.m.waiting_state(SID, rid), ("not_waiting", None))
        self.reg("busy", 300)
        self.assertEqual(self.state(self.enrich(301)), ("working", None))
        # without a registry the unanswered request is dropped at once
        self.no_reg()
        self.m = self.model()
        rid = self.ask(0)
        s = self.ev("unanswered", 55.2, rid=rid, tool="AskUserQuestion", why="timeout")
        self.assertEqual((s["status"], s["waiting"]), ("working", None))

    def test_cause_subagent_prompt(self):
        # A subagent's request no longer outlives the session's progress (no registry) ...
        self.ev("SubagentStart", 0, agent="a1")
        self.ask(1, hold=False, agent="a1", tool="Bash")
        self.assertEqual(self.state(), ("waiting", "permission"))
        s = self.ev("PostToolUse", 30, tool="Read")  # main thread, different tool
        self.assertEqual((s["status"], s["waiting"]), ("working", None))
        # ... and with a registry it follows the registry, not other agents' steps
        self.reg("waiting", 40, "permission prompt")
        self.enrich(40.5)
        self.ask(40, hold=False, agent="a1", tool="Bash")
        self.ev("PreToolUse", 41, agent="a2", tool="Read")
        self.assertEqual(self.state(self.enrich(42)), ("waiting", "permission"))
        self.reg("busy", 50)
        self.assertEqual(self.state(self.enrich(51)), ("working", None))

    def test_cause_stale_requests_do_not_force_waiting(self):
        # Pre-0.4.1 state.json with an old unanswered request (hubCanAnswer False) kept the session waiting through
        # every later event. After loading, the rules drop it.
        stale = {"kind": "question", "requestId": "r-old", "tool": "AskUserQuestion", "text": "AskUserQuestion",
                 "since": int((self.t - 3600) * 1000), "hubCanAnswer": False, "_holdS": 55}
        self.m.load_dict({"sessions": {SID: {
            "id": SID, "ccr": None, "name": None, "localName": None, "project": None, "status": "waiting",
            "waiting": dict(stale), "summary": None, "model": None, "effort": None, "ctx": {"used": None, "max": None},
            "costUsd": None, "startedAt": 0, "updatedAt": 0, "tasks": [], "plan": None, "agents": [],
            "_statusAt": 0, "_reqs": [stale]}}})
        s = self.ev("PreToolUse", 0, tool="Bash")
        self.assertEqual((s["status"], s["waiting"]), ("working", None))
        self.assertNotIn("_reqs", s)

    def test_cause_stale_requests_dropped_by_registry(self):
        self.ask(0, hold=False, tool="Bash")
        self.reg("idle", -100)  # the registry never showed this prompt
        self.assertEqual(self.state(self.enrich(1)), ("waiting", "permission"))  # write lag
        self.assertEqual(self.state(self.enrich(4)), ("idle", None))

    # ---- registry, holds, lag, expiry ----
    def test_registry_sets_and_clears_without_hooks(self):
        self.ev("SessionStart", 0, source="startup")
        self.reg("waiting", 5, "permission prompt")
        s = self.enrich(6)
        self.assertEqual(s["status"], "waiting")
        self.assertEqual(s["waiting"], {"kind": "permission", "tool": None, "text": "permission prompt"})
        self.reg("waiting", 7, "input needed")
        self.assertEqual(self.enrich(8)["waiting"]["kind"], "question")
        self.reg("busy", 9)
        self.assertEqual(self.state(self.enrich(10)), ("working", None))
        self.reg("idle", 12)
        self.assertEqual(self.state(self.enrich(13)), ("idle", None))

    def test_registry_waiting_text_prefers_request(self):
        self.reg("waiting", 1, "input needed")
        self.enrich(0.5)
        self.ask(1)
        s = self.enrich(2)
        self.assertEqual(s["waiting"]["tool"], "AskUserQuestion")
        self.assertEqual(s["waiting"]["questions"][0]["question"], "Which?")
        self.assertTrue(s["waiting"]["hubCanAnswer"])

    def test_running_hold_keeps_waiting(self):
        # The registry hasn't caught up (busy since before the request): a hold crew-hook still runs keeps it.
        self.reg("busy", -30)
        self.enrich(-1)
        rid = self.ask(0, hold_s=55)
        s = self.enrich(30)
        self.assertEqual(self.state(s), ("waiting", "question"))
        self.assertTrue(s["waiting"]["hubCanAnswer"])
        self.assertEqual(self.m.waiting_state(SID, rid)[0], "ok")
        self.assertEqual(self.state(self.enrich(58)), ("working", None))
        # no registry: a running hold is not ended by other progress, only by its own clears
        self.no_reg()
        self.m = self.model()
        self.ask(0)
        self.ev("PreToolUse", 1, agent="a1", tool="Read")
        self.ev("PostToolUse", 2, agent="a1", tool="Read")
        self.assertEqual(self.state(), ("waiting", "question"))
        s = self.ev("PostToolUse", 3, tool="AskUserQuestion")
        self.assertEqual((s["status"], s["waiting"]), ("working", None))

    def test_registry_lag_after_request(self):
        self.reg("busy", -30)
        self.enrich(-1)
        self.ask(0, hold=False, tool="Bash")
        self.assertEqual(self.state(self.enrich(2)), ("waiting", "permission"))
        self.assertEqual(self.state(self.enrich(4)), ("working", None))

    def test_stale_cached_registry_waiting_after_answer(self):
        # Answered: PostToolUse arrives before the next registry read; the cached "waiting" must not re-flag it.
        self.reg("waiting", 0.1, "input needed")
        self.enrich(0.2)
        self.ask(0.1)
        s = self.ev("PostToolUse", 10, tool="AskUserQuestion")
        self.assertEqual((s["status"], s["waiting"]), ("working", None))
        self.reg("busy", 10)
        self.assertEqual(self.state(self.enrich(12)), ("working", None))

    def test_no_registry_expiry(self):
        self.ask(0, hold=False, tool="Bash")
        self.assertEqual(self.state(self.enrich(NOREG_MAX_MS / 1000 - 5)), ("waiting", "permission"))
        self.assertEqual(self.state(self.enrich(NOREG_MAX_MS / 1000 + 5)), ("idle", None))

    def test_no_registry_hold_over_without_unanswered_record(self):
        # crew-hook killed hard (no "unanswered"): the hold running out ends it without a registry.
        rid = self.ask(0, hold_s=10)
        self.assertEqual(self.state(self.enrich(5)), ("waiting", "question"))
        s = self.enrich(13)
        self.assertEqual((s["status"], s["waiting"]), ("idle", None))
        self.assertEqual(self.m.waiting_state(SID, rid), ("not_waiting", None))

    def test_plan_only_session_follows_registry_waiting(self):
        plans = os.path.join(self.tmp, "plans")
        os.makedirs(plans)
        with open(os.path.join(plans, "p1.json"), "w") as f:
            json.dump({"title": "Plan: x", "tasks": []}, f)
        self.reg("waiting", 0, "permission prompt", sid="p1")
        self.m.poll_plans(plans, self.sessions, now=int(self.t * 1000))
        s = self.m.sessions["p1"]
        self.assertEqual(self.state(s), ("waiting", "permission"))
        self.reg("idle", 5, sid="p1")
        self.m.enrich(self.sessions, now=int((self.t + 6) * 1000))
        self.assertEqual(self.state(s), ("idle", None))

    def test_snapshot_has_no_internal_keys(self):
        self.reg("waiting", 0.1, "input needed")
        self.enrich(0.2)
        self.ask(0.1)
        self.ev("Notification", 6, ntype="permission_prompt", msg="Claude needs your permission")
        text = json.dumps(self.m.snapshot({"id": "m"}, commands_active=True))
        self.assertNotIn('"_', text)


if __name__ == "__main__":
    unittest.main()
