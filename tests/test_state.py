import json
import os
import tempfile
import time
import unittest

import _util  # noqa: F401
from crewhub.redact import redact
from crewhub.state import (AGENT_IDLE_MS, FINISHED_TTL_MS, MAX_FINISHED, Model, cap_snapshot, display_name,
                           is_derived_name, read_transcript_usage, validate_plan)

SID = "7c1addf5-73c5-41d7-9225-77ba4bbbb2d5"
MACHINE = {"id": "test", "name": "Test", "version": "0", "updatedAt": 0}


def replay(model, payloads, t0=1792000000.0):
    for i, p in enumerate(payloads):
        model.apply(redact(p, t0 + i, None, None))


class FoldSampleTest(unittest.TestCase):
    def setUp(self):
        self.payloads = _util.sample_payloads()
        self.stop_i = [p["hook_event_name"] for p in self.payloads].index("Stop")

    def test_snapshot_after_stop(self):
        m = Model()
        replay(m, self.payloads[: self.stop_i + 1])
        snap = m.snapshot(MACHINE)
        self.assertEqual(len(snap["sessions"]), 1)
        s = snap["sessions"][0]
        self.assertEqual(s["id"], SID)
        self.assertEqual(s["status"], "idle")
        self.assertIsNone(s["waiting"])
        self.assertEqual(s["summary"], "done")
        self.assertEqual(s["effort"], "medium")
        self.assertEqual(s["project"], "<path>")
        self.assertEqual([(t["id"], t["subject"], t["status"]) for t in s["tasks"]],
                         [("1", "say hi", "completed"), ("2", "count files", "completed")])
        self.assertEqual(len(s["agents"]), 1)
        a = s["agents"][0]
        self.assertEqual(a["id"], "af0d389234162b24e")
        self.assertEqual(a["title"], "Say hi test")
        self.assertEqual(a["model"], "claude-haiku-5-5")
        self.assertEqual(a["type"], "general-purpose")
        self.assertEqual(a["status"], "done")
        self.assertEqual(a["steps"], 1)
        self.assertEqual(a["current"]["text"], "Print hi via echo")
        self.assertEqual(a["trail"], [])
        self.assertIsNotNone(a["endedAt"])
        # nothing internal or sensitive in the published snapshot
        text = json.dumps(snap)
        self.assertNotIn('"_', text)
        self.assertNotIn("echo hi", text)

    def test_ended_after_session_end(self):
        m = Model()
        replay(m, self.payloads)
        s = m.snapshot(MACHINE)["sessions"][0]
        self.assertEqual(s["status"], "ended")

    def test_working_while_running(self):
        m = Model()
        sub_i = [p["hook_event_name"] for p in self.payloads].index("SubagentStart")
        replay(m, self.payloads[: sub_i + 1])
        s = m.snapshot(MACHINE)["sessions"][0]
        self.assertEqual(s["status"], "working")
        self.assertEqual(s["agents"][0]["status"], "running")
        self.assertEqual([t["status"] for t in s["tasks"]], ["pending", "pending"])

    def test_persistence_roundtrip(self):
        m = Model()
        replay(m, self.payloads[: self.stop_i + 1])
        m2 = Model()
        m2.load_dict(json.loads(json.dumps(m.to_dict())))
        self.assertEqual(m.snapshot(MACHINE)["sessions"], m2.snapshot(MACHINE)["sessions"])


class FoldRulesTest(unittest.TestCase):
    def ev(self, m, ev, t, **kw):
        rec = {"v": 1, "ts": t, "ev": ev, "sid": "s"}
        rec.update(kw)
        m.apply(rec)
        return m.sessions["s"]

    def test_waiting_permission_then_resume(self):
        m = Model()
        self.ev(m, "UserPromptSubmit", 1)
        s = self.ev(m, "PermissionRequest", 2, tool="Bash", desc="Reboot the box")
        self.assertEqual(s["status"], "waiting")
        self.assertEqual(s["waiting"], {"kind": "permission", "tool": "Bash", "text": "Reboot the box"})
        s = self.ev(m, "Notification", 3, ntype="permission_prompt", msg="Claude needs your permission to use Bash")
        self.assertEqual(s["waiting"]["tool"], "Bash")
        s = self.ev(m, "PostToolUse", 4, tool="Bash")
        self.assertEqual((s["status"], s["waiting"]), ("working", None))
        s = self.ev(m, "Stop", 5, msg="ok")
        self.assertEqual(s["status"], "idle")
        s = self.ev(m, "Notification", 6, ntype="idle_prompt", msg="Claude is waiting for your input")
        self.assertEqual(s["waiting"]["kind"], "input")
        s = self.ev(m, "Notification", 7, ntype="auth_success", msg="ok")
        self.assertEqual(s["status"], "waiting")  # unrelated notifications don't change status

    def test_agent_cap_and_trail(self):
        m = Model()
        for i in range(15):
            self.ev(m, "SubagentStart", i, agent="a%d" % i, atype="x")
            if i < 10:
                self.ev(m, "SubagentStop", i + 0.5, agent="a%d" % i)
        s = m.sessions["s"]
        self.assertEqual(len(s["agents"]), 12)
        self.assertNotIn("a0", [a["id"] for a in s["agents"]])
        self.assertIn("a14", [a["id"] for a in s["agents"]])
        for i in range(5):
            self.ev(m, "PreToolUse", 100 + i, agent="a14", tool="Read", file="f%d.py" % i)
        a = [a for a in s["agents"] if a["id"] == "a14"][0]
        self.assertEqual(a["steps"], 5)
        self.assertEqual(a["current"]["text"], "Read f4.py")
        self.assertEqual([t["text"] for t in a["trail"]], ["Read f1.py", "Read f2.py", "Read f3.py"])

    def test_task_update_in_progress_and_todowrite(self):
        m = Model()
        self.ev(m, "TaskCreated", 1, task_id="1", subject="one")
        s = self.ev(m, "PostToolUse", 2, tool="TaskUpdate", taskId="1", statusChange={"from": "pending", "to": "in_progress"})
        self.assertEqual(s["tasks"][0]["status"], "in_progress")
        s = self.ev(m, "PostToolUse", 3, tool="TaskUpdate", taskId="1", statusChange={"from": "in_progress", "to": "deleted"})
        self.assertEqual(s["tasks"], [])
        s = self.ev(m, "PreToolUse", 4, tool="TodoWrite", todos=[{"content": "a", "status": "completed"},
                                                               {"content": "b", "status": "in_progress"}])
        self.assertEqual([(t["subject"], t["status"]) for t in s["tasks"]], [("a", "completed"), ("b", "in_progress")])

    def test_plan_file(self):
        with tempfile.TemporaryDirectory() as d:
            plans = os.path.join(d, "plans")
            os.makedirs(plans)
            path = os.path.join(plans, "s.json")
            with open(path, "w") as f:
                json.dump({"title": "Jamfin disc engine", "phase": "build", "done": 1, "total": 3, "finished": False,
                           "updatedAt": 5, "ccrSessionId": "session_x", "secret": "nope",
                           "tasks": [{"id": "t1", "title": "Probe", "tier": "medium", "status": "running", "order": 1,
                                      "agentId": "a1", "summary": None, "prompt": "brief text"}]}, f)
            m = Model()
            self.ev(m, "UserPromptSubmit", 1)
            m.apply({"v": 1, "ts": 2, "ev": "PostToolUse", "sid": "s", "tool": "Write", "plan": path}, plans_dir=plans)
            plan = m.sessions["s"]["plan"]
            self.assertEqual((plan["title"], plan["done"], plan["total"], plan["ccrSessionId"]),
                             ("Jamfin disc engine", 1, 3, "session_x"))
            self.assertNotIn("secret", plan)
            self.assertNotIn("prompt", plan["tasks"][0])
            os.unlink(path)
            m.enrich(plans_dir=plans)
            self.assertIsNone(m.sessions["s"]["plan"])
            # a plan path outside the plans dir is ignored
            m.apply({"v": 1, "ts": 3, "ev": "PostToolUse", "sid": "s", "tool": "Write",
                     "plan": os.path.join(d, "s.json")}, plans_dir=plans)
            self.assertIsNone(m.sessions["s"]["plan"])

    def test_validate_plan_garbage(self):
        self.assertIsNone(validate_plan([1, 2]))
        p = validate_plan({"tasks": [1, {"status": "weird"}], "done": True})
        self.assertEqual(p["tasks"][0]["status"], "queued")
        self.assertIsNone(p["done"])

    def test_prune_and_dead_pid(self):
        m = Model()
        s = self.ev(m, "UserPromptSubmit", time.time())
        s["_pid"] = 2 ** 22 + 12345  # beyond pid_max: never alive
        m.enrich()
        self.assertEqual(s["status"], "ended")
        m.prune(int(time.time() * 1000) + 3601 * 1000)
        self.assertEqual(m.sessions, {})

    def test_hook_pid_of_shell_not_trusted(self):
        m = Model()
        s = self.ev(m, "UserPromptSubmit", time.time(), pid=os.getpid())  # python, not claude
        m.enrich()
        self.assertEqual(s["status"], "working")
        self.assertNotIn("_pid", s)

    def test_sessions_dir_enrichment(self):
        with tempfile.TemporaryDirectory() as d:
            m = Model()
            s = self.ev(m, "UserPromptSubmit", 1)
            for name, pid, upd, label in (("1.json", os.getpid(), 10, "live one"), ("2.json", 2 ** 22 + 1, 99, "dead one")):
                with open(os.path.join(d, name), "w") as f:
                    json.dump({"pid": pid, "sessionId": "s", "name": label, "bridgeSessionId": "session_abc",
                               "status": "idle", "updatedAt": upd, "statusUpdatedAt": 50000}, f)
            with open(os.path.join(d, "3.key"), "w") as f:
                f.write("x")
            m.enrich(sessions_dir=d)
            self.assertEqual((s["name"], s["ccr"], s["_pid"]), ("live one", "session_abc", os.getpid()))
            self.assertEqual(s["status"], "idle")  # file says idle, newer than our last status change

    def test_transcript_usage(self):
        with tempfile.TemporaryDirectory() as d:
            tp = os.path.join(d, "t.jsonl")
            lines = [
                {"type": "user", "message": {"content": "x"}},
                {"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {
                    "input_tokens": 2, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000, "output_tokens": 9}}},
                {"type": "assistant", "isSidechain": True, "message": {"model": "claude-haiku-5-5", "usage": {"input_tokens": 1}}},
                {"type": "assistant", "message": {"model": "<synthetic>", "usage": {"input_tokens": 0}}},
            ]
            with open(tp, "w") as f:
                f.write("x" * 200000 + "\n")  # pushes the start beyond the 128 KB tail
                for line in lines:
                    f.write(json.dumps(line) + "\n")
            self.assertEqual(read_transcript_usage(tp), ("claude-opus-5-5", 1102))
            m = Model()
            s = self.ev(m, "UserPromptSubmit", time.time(), tp=tp)
            m.enrich()
            self.assertEqual((s["model"], s["ctx"]), ("claude-opus-5-5", {"used": 1102, "max": 1000000}))

    def test_cap_snapshot(self):
        m = Model()
        for i in range(40):
            sid = "s%02d" % i
            m.apply({"v": 1, "ts": i, "ev": "UserPromptSubmit", "sid": sid})
            for j in range(12):
                m.apply({"v": 1, "ts": i, "ev": "SubagentStart", "sid": sid, "agent": "a%d" % j, "atype": "t" * 50})
                for k in range(4):
                    m.apply({"v": 1, "ts": i, "ev": "PreToolUse", "sid": sid, "agent": "a%d" % j, "tool": "Bash",
                             "desc": "step %d " % k + "d" * 150})
        snap = m.snapshot(MACHINE)
        self.assertLessEqual(len(json.dumps(snap, separators=(",", ":")).encode()), 40000)
        self.assertGreaterEqual(len(snap["sessions"]), 1)
        self.assertEqual(cap_snapshot({"machine": {}, "sessions": []}), {"machine": {}, "sessions": []})


def _no_internal_keys(testcase, obj, where="snapshot"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            testcase.assertFalse(str(k).startswith("_"), "%s has internal key %s" % (where, k))
            _no_internal_keys(testcase, v, "%s.%s" % (where, k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _no_internal_keys(testcase, v, "%s[%d]" % (where, i))


class PlanPollTest(unittest.TestCase):
    """Plans from sessions without hooks: picked up from the plans dir alone."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.plans = os.path.join(self.tmp.name, "plans")
        self.sessions = os.path.join(self.tmp.name, "sessions")
        os.makedirs(self.plans)
        os.makedirs(self.sessions)

    def tearDown(self):
        self.tmp.cleanup()

    def write_plan(self, sid, mtime=None, **fields):
        path = os.path.join(self.plans, sid + ".json")
        data = {"title": "Plan of " + sid, "phase": "build", "done": 0, "total": 2, "tasks": []}
        data.update(fields)
        with open(path, "w") as f:
            json.dump(data, f)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_new_plan_without_registry_is_idle_session(self):
        m = Model()
        self.write_plan("nohooks-1", ccrSessionId="session_zz")
        m.poll_plans(self.plans, self.sessions)
        s = m.sessions["nohooks-1"]
        self.assertEqual((s["status"], s["name"], s["ccr"]), ("idle", "Plan of nohooks-1", "session_zz"))
        self.assertEqual(s["plan"]["phase"], "build")
        # liveness checks never end it while the file exists, even a day later
        m.enrich(sessions_dir=self.sessions, plans_dir=self.plans, now=int(time.time() * 1000) + 3 * 24 * 3600 * 1000)
        self.assertEqual(m.sessions["nohooks-1"]["status"], "idle")
        snap = m.snapshot(MACHINE)
        self.assertEqual([x["id"] for x in snap["sessions"]], ["nohooks-1"])
        _no_internal_keys(self, snap)

    def test_new_plan_with_live_registry_entry(self):
        with open(os.path.join(self.sessions, "%d.json" % os.getpid()), "w") as f:
            json.dump({"pid": os.getpid(), "sessionId": "live-1", "name": "project-ac", "bridgeSessionId": "session_abc",
                       "status": "busy", "updatedAt": 10, "statusUpdatedAt": 10}, f)
        with open(os.path.join(self.sessions, "dead.json"), "w") as f:
            json.dump({"pid": 2 ** 22 + 7, "sessionId": "dead-1", "name": "gone", "status": "busy", "updatedAt": 99}, f)
        m = Model()
        self.write_plan("live-1")
        self.write_plan("dead-1")
        m.poll_plans(self.plans, self.sessions)
        s = m.sessions["live-1"]
        # a derived local name is not shown: the plan title wins, the local name stays in localName
        self.assertEqual((s["status"], s["name"], s["localName"], s["ccr"], s["_pid"]),
                         ("working", "Plan of live-1", "project-ac", "session_abc", os.getpid()))
        d = m.sessions["dead-1"]
        self.assertEqual((d["status"], d["name"]), ("idle", "Plan of dead-1"))
        self.assertNotIn("_pid", d)
        # registry later says idle: followed by enrichment
        with open(os.path.join(self.sessions, "%d.json" % os.getpid()), "w") as f:
            json.dump({"pid": os.getpid(), "sessionId": "live-1", "name": "project-ac", "status": "idle",
                       "updatedAt": 50000, "statusUpdatedAt": 50000}, f)
        m.enrich(sessions_dir=self.sessions, plans_dir=self.plans)
        self.assertEqual(m.sessions["live-1"]["status"], "idle")
        self.assertEqual(m.sessions["dead-1"]["name"], "Plan of dead-1")

    def test_update_by_mtime(self):
        m = Model()
        self.write_plan("p1", mtime=1000000, done=0)
        m.poll_plans(self.plans, self.sessions)
        self.assertEqual(m.sessions["p1"]["plan"]["done"], 0)
        m.changed = False
        m.poll_plans(self.plans, self.sessions)
        self.assertFalse(m.changed)  # unchanged file: no re-read, no change
        self.write_plan("p1", mtime=1000005, done=1, title="Renamed plan")
        m.poll_plans(self.plans, self.sessions)
        self.assertTrue(m.changed)
        self.assertEqual((m.sessions["p1"]["plan"]["done"], m.sessions["p1"]["name"]), (1, "Renamed plan"))

    def test_deletion(self):
        m = Model()
        p1 = self.write_plan("p1")
        p2 = self.write_plan("hooked")
        m.apply({"v": 1, "ts": time.time(), "ev": "UserPromptSubmit", "sid": "hooked"})
        m.poll_plans(self.plans, self.sessions)
        self.assertIsNotNone(m.sessions["hooked"]["plan"])
        os.unlink(p1)
        os.unlink(p2)
        m.poll_plans(self.plans, self.sessions)
        self.assertNotIn("p1", m.sessions)  # plan-only session goes with its file
        self.assertIsNone(m.sessions["hooked"]["plan"])  # a session with hooks stays, without the plan

    def test_hook_event_turns_plan_session_normal_and_bad_file_ignored(self):
        m = Model()
        self.write_plan("p1")
        with open(os.path.join(self.plans, "bad.json"), "w") as f:
            f.write("{broken")
        m.poll_plans(self.plans, self.sessions)
        self.assertNotIn("bad", m.sessions)
        m.apply({"v": 1, "ts": time.time(), "ev": "UserPromptSubmit", "sid": "p1"})
        self.assertNotIn("_fromPlan", m.sessions["p1"])
        self.assertEqual(m.sessions["p1"]["status"], "working")


DEAD_PID = 2 ** 22 + 4242  # beyond pid_max: never alive


def _reg(d, name, pid, sid="s", status="idle", upd=10, **extra):
    data = {"pid": pid, "sessionId": sid, "name": name, "status": status, "updatedAt": upd, "statusUpdatedAt": upd}
    data.update(extra)
    with open(os.path.join(d, "%s.json" % pid), "w") as f:
        json.dump(data, f)


class AgentLifecycleTest(unittest.TestCase):
    """0.4.0: stopped agents, inactivity, pruning by time and count."""

    T0 = 1792000000.0

    def ev(self, m, ev, t, **kw):
        rec = {"v": 1, "ts": self.T0 + t, "ev": ev, "sid": "s"}
        rec.update(kw)
        m.apply(rec)
        return m.sessions["s"]

    def ms(self, t):
        return int((self.T0 + t) * 1000)

    def started(self, m, aid, t, title="Do things", steps=1):
        self.ev(m, "SubagentStart", t, agent=aid, atype="general-purpose")
        self.ev(m, "PostToolUse", t, tool="Agent", aid=aid, title=title)
        for i in range(steps):
            self.ev(m, "PreToolUse", t + i, agent=aid, tool="Bash", desc="step %d" % i)

    def agent(self, m, aid):
        return [a for a in m.sessions["s"]["agents"] if a["id"] == aid][0]

    def test_session_start_marks_running_agents_stopped(self):
        m = Model()
        self.ev(m, "UserPromptSubmit", 0)
        self.started(m, "a1", 1)
        self.started(m, "a2", 2)
        self.ev(m, "SubagentStop", 5, agent="a2")
        self.ev(m, "SessionStart", 100, source="resume")
        a1, a2 = self.agent(m, "a1"), self.agent(m, "a2")
        self.assertEqual((a1["status"], a1["stopReason"], a1["endedAt"]), ("stopped", "session restarted", self.ms(100)))
        self.assertEqual((a2["status"], a2["stopReason"]), ("done", None))
        # the plan is the orchestrator's truth: untouched
        self.assertIsNone(m.sessions["s"]["plan"])
        # a stopped agent that steps again is running again
        self.ev(m, "PreToolUse", 101, agent="a1", tool="Read", file="x.py")
        a1 = self.agent(m, "a1")
        self.assertEqual((a1["status"], a1["endedAt"], a1["stopReason"]), ("running", None, None))
        snap = m.snapshot(MACHINE)
        self.assertEqual({a["id"]: a["stopReason"] for a in snap["sessions"][0]["agents"]}, {"a1": None, "a2": None})

    def test_compact_session_start_keeps_agents_running(self):
        m = Model()
        self.ev(m, "UserPromptSubmit", 0)
        self.started(m, "a1", 1)
        self.ev(m, "SessionStart", 50, source="compact")
        self.assertEqual(self.agent(m, "a1")["status"], "running")
        for src in ("startup", "clear", None):
            m2 = Model()
            self.ev(m2, "UserPromptSubmit", 0)
            self.started(m2, "a1", 1)
            kw = {"source": src} if src else {}
            self.ev(m2, "SessionStart", 50, **kw)
            self.assertEqual(self.agent(m2, "a1")["stopReason"], "session restarted", src)

    def test_task_stop_marks_agent_stopped_by_session(self):
        m = Model()
        self.ev(m, "UserPromptSubmit", 0)
        self.started(m, "a1", 1)
        self.started(m, "a2", 2)
        self.ev(m, "PostToolUse", 60, tool="TaskStop", stopTask="a1")
        self.ev(m, "PostToolUse", 61, tool="TaskStop", stopTask="nope")  # unknown id: no agent is made up
        a1, a2 = self.agent(m, "a1"), self.agent(m, "a2")
        self.assertEqual((a1["status"], a1["stopReason"], a1["endedAt"]), ("stopped", "stopped by session", self.ms(60)))
        self.assertEqual(a2["status"], "running")
        self.assertEqual([a["id"] for a in m.sessions["s"]["agents"]], ["a1", "a2"])
        # a late step doesn't revive an explicitly stopped agent; a new SubagentStart (resumed agent) does
        self.ev(m, "PreToolUse", 62, agent="a1", tool="Read", file="x.py")
        self.assertEqual(self.agent(m, "a1")["status"], "stopped")
        self.ev(m, "SubagentStart", 70, agent="a1")
        a1 = self.agent(m, "a1")
        self.assertEqual((a1["status"], a1["stopReason"], a1["endedAt"]), ("running", None, None))
        # a finished agent stays done
        self.ev(m, "SubagentStop", 80, agent="a2")
        self.ev(m, "PostToolUse", 81, tool="KillShell", stopTask="a2")
        self.assertEqual(self.agent(m, "a2")["status"], "done")
        # the plan is untouched
        self.assertIsNone(m.sessions["s"]["plan"])

    def test_registry_moves_to_new_process_marks_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            m = Model()
            s = self.ev(m, "UserPromptSubmit", 0)
            self.started(m, "a1", 1)
            s["_regProc"] = [DEAD_PID, "123"]  # the process we saw before the restart
            _reg(d, "project-aa", os.getpid(), status="busy", upd=self.ms(50))
            m.enrich(sessions_dir=d, now=self.ms(60))
            a1 = self.agent(m, "a1")
            self.assertEqual((a1["status"], a1["stopReason"]), ("stopped", "session restarted"))
            self.assertEqual(s["_regProc"], [os.getpid(), None])
            # same process on the next pass: nothing new happens
            self.started(m, "a2", 70)
            m.enrich(sessions_dir=d, now=self.ms(80))
            self.assertEqual(self.agent(m, "a2")["status"], "running")

    def test_idle_session_and_no_step_for_10_min_marks_stopped(self):
        with tempfile.TemporaryDirectory() as d:
            m = Model()
            self.ev(m, "UserPromptSubmit", 0)
            self.started(m, "a1", 1)
            _reg(d, "x", os.getpid(), status="idle", upd=self.ms(5))
            last = self.ms(1)
            m.enrich(sessions_dir=d, now=last + AGENT_IDLE_MS - 1000)
            self.assertEqual(self.agent(m, "a1")["status"], "running")  # not 10 minutes yet
            now = last + AGENT_IDLE_MS + 1000
            m.enrich(sessions_dir=d, now=now)
            a1 = self.agent(m, "a1")
            self.assertEqual((a1["status"], a1["stopReason"], a1["endedAt"]), ("stopped", "inactive", now))

    def test_busy_session_keeps_quiet_agent_running(self):
        with tempfile.TemporaryDirectory() as d:
            m = Model()
            self.ev(m, "UserPromptSubmit", 0)
            self.started(m, "a1", 1)
            _reg(d, "x", os.getpid(), status="busy", upd=self.ms(5))
            m.enrich(sessions_dir=d, now=self.ms(1) + 3 * AGENT_IDLE_MS)
            self.assertEqual(self.agent(m, "a1")["status"], "running")

    def test_ended_session_and_no_step_marks_stopped(self):
        m = Model()
        s = self.ev(m, "UserPromptSubmit", 0)
        self.started(m, "a1", 1)
        s["_pid"] = DEAD_PID  # process gone without SessionEnd
        m.enrich(now=self.ms(2))
        self.assertEqual((s["status"], self.agent(m, "a1")["status"]), ("ended", "running"))
        m.enrich(now=self.ms(1) + AGENT_IDLE_MS + 1000)
        self.assertEqual(self.agent(m, "a1")["stopReason"], "inactive")

    def test_untitled_zero_step_agent_dropped_once_finished(self):
        m = Model()
        self.ev(m, "UserPromptSubmit", 0)
        self.ev(m, "SubagentStart", 1, agent="ghost")
        self.started(m, "a1", 1)
        m.prune(self.ms(2))
        self.assertIn("ghost", [a["id"] for a in m.sessions["s"]["agents"]])  # still running: kept
        self.ev(m, "SubagentStop", 3, agent="ghost")
        self.ev(m, "SubagentStop", 3, agent="a1")
        m.prune(self.ms(4))
        self.assertEqual([a["id"] for a in m.sessions["s"]["agents"]], ["a1"])

    def test_prune_finished_by_time(self):
        m = Model()
        self.ev(m, "UserPromptSubmit", 0)
        self.started(m, "old", 1)
        self.started(m, "new", 2)
        self.started(m, "run", 3)
        self.ev(m, "SubagentStop", 10, agent="old")
        self.ev(m, "SubagentStop", 1000, agent="new")
        m.prune(self.ms(10) + FINISHED_TTL_MS - 1000)
        self.assertEqual([a["id"] for a in m.sessions["s"]["agents"]], ["old", "new", "run"])
        m.prune(self.ms(10) + FINISHED_TTL_MS + 1000)
        self.assertEqual([a["id"] for a in m.sessions["s"]["agents"]], ["new", "run"])
        m.prune(self.ms(10) + 10 * FINISHED_TTL_MS)
        self.assertEqual([a["id"] for a in m.sessions["s"]["agents"]], ["run"])  # running never pruned

    def test_prune_finished_by_count(self):
        m = Model()
        self.ev(m, "UserPromptSubmit", 0)
        for i in range(10):
            self.started(m, "f%d" % i, i)
            self.ev(m, "SubagentStop", 20 + i, agent="f%d" % i)
        for i in range(2):
            self.started(m, "r%d" % i, 40 + i)
        m.prune(self.ms(60))
        ids = [a["id"] for a in m.sessions["s"]["agents"]]
        self.assertEqual([i for i in ids if i.startswith("f")], ["f%d" % i for i in range(10 - MAX_FINISHED, 10)])
        self.assertEqual([i for i in ids if i.startswith("r")], ["r0", "r1"])
        # more running agents than MAX_AGENTS: the cap drops finished ones only, never a running one
        for i in range(2, 14):
            self.started(m, "r%d" % i, 60 + i)
        ids = [a["id"] for a in m.sessions["s"]["agents"]]
        self.assertEqual(ids, ["r%d" % i for i in range(14)])


class SessionNameTest(unittest.TestCase):
    def sess(self, reg=None, plan_title=None, project=None):
        s = {"id": "s", "project": project, "plan": {"title": plan_title} if plan_title else None}
        if reg is not None:
            s["_reg"] = reg
        return s

    def test_derived_name_pattern(self):
        self.assertTrue(is_derived_name("project-aa", "project"))
        self.assertTrue(is_derived_name("project-2c"))
        self.assertFalse(is_derived_name("project-aa", "crew-hub"))
        self.assertFalse(is_derived_name("Crew Hub design proposal"))
        self.assertFalse(is_derived_name("TrueNAS"))

    def test_order(self):
        reg = {"name": "Home Assistant Mr Manager", "source": "user", "former": "Old", "cwd": "project"}
        self.assertEqual(display_name(self.sess(reg, "NerveOS: x")), ("Home Assistant Mr Manager",) * 2)
        reg = {"name": "Jellyfin app", "source": "auto", "former": None, "cwd": "project"}
        self.assertEqual(display_name(self.sess(reg))[0], "Jellyfin app")
        # derived registry name: newest human former name
        reg = {"name": "project-aa", "source": "derived", "former": "Truenas - Android Agent", "cwd": "project"}
        self.assertEqual(display_name(self.sess(reg, "NerveOS: x")), ("Truenas - Android Agent", "project-aa"))
        # then the plan title prefix, or the whole title
        reg = {"name": "project-aa", "source": "derived", "former": None, "cwd": "project"}
        self.assertEqual(display_name(self.sess(reg, "NerveOS: finish the hot menu")), ("NerveOS", "project-aa"))
        self.assertEqual(display_name(self.sess(reg, "Jamfin disc engine"))[0], "Jamfin disc engine")
        # then the cwd basename with the local name, or just the local name when the cwd is "project"
        self.assertEqual(display_name(self.sess(reg)), ("project-aa", "project-aa"))
        reg = {"name": "crew-hub-3f", "source": "derived", "former": None, "cwd": "crew-hub"}
        self.assertEqual(display_name(self.sess(reg)), ("crew-hub (crew-hub-3f)", "crew-hub-3f"))
        # no registry at all
        self.assertEqual(display_name(self.sess(None, project="jamfin")), ("jamfin", None))
        self.assertEqual(display_name(self.sess(None)), (None, None))
        # registry without nameSource (older Claude Code): judged by the pattern
        self.assertEqual(display_name(self.sess({"name": "live one", "cwd": None}))[0], "live one")
        self.assertEqual(display_name(self.sess({"name": "project-ac", "cwd": None}, "P: x"))[0], "P")

    def test_registry_files(self):
        with tempfile.TemporaryDirectory() as d:
            m = Model()
            m.apply({"v": 1, "ts": 1, "ev": "UserPromptSubmit", "sid": "s", "cwd": "project"})
            _reg(d, "project-aa", os.getpid(), nameSource="derived", cwd="/home/node/project",
                 formerNames=[{"name": "project-3c", "until": 9, "sessionId": "x"},
                              {"name": "Older title", "until": 5, "sessionId": "y"},
                              {"name": "Newest title", "until": 8, "sessionId": "z"}])
            _reg(d, "Dead entry title", DEAD_PID, nameSource="auto", cwd="/home/node/project", upd=4)
            m.enrich(sessions_dir=d)
            s = m.sessions["s"]
            self.assertEqual((s["name"], s["localName"]), ("Newest title", "project-aa"))
            snap = m.snapshot(MACHINE)["sessions"][0]
            self.assertEqual((snap["name"], snap["localName"]), ("Newest title", "project-aa"))
            _no_internal_keys(self, snap)
            # user rename wins
            _reg(d, "Kitchen kiosk", os.getpid(), nameSource="user", cwd="/home/node/project")
            m.enrich(sessions_dir=d)
            self.assertEqual((s["name"], s["localName"]), ("Kitchen kiosk", "Kitchen kiosk"))
            # no former names: the dead entry's auto title counts as a former name
            _reg(d, "project-aa", os.getpid(), nameSource="derived", cwd="/home/node/project")
            m.enrich(sessions_dir=d)
            self.assertEqual(s["name"], "Dead entry title")
            os.unlink(os.path.join(d, "%d.json" % DEAD_PID))
            m.enrich(sessions_dir=d)
            self.assertEqual(s["name"], "project-aa")


if __name__ == "__main__":
    unittest.main()
