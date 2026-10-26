import json
import os
import tempfile
import time
import unittest

import _util  # noqa: F401
from crewhub.redact import redact
from crewhub.state import Model, cap_snapshot, read_transcript_usage, validate_plan

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


if __name__ == "__main__":
    unittest.main()
