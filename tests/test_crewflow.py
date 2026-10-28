"""0.5.0: plans derived from the built-in task list (Crew Flow) and token insight (state.py module doc)."""

import json
import os
import tempfile
import unittest

import _util  # noqa: F401
from crewhub.redact import redact
from crewhub.state import (AGENT_TOKENS_EVERY_MS, Model, agent_transcript, derive_plan, join_agents, parse_subject,
                           read_usage)

MACHINE = {"id": "test", "name": "Test", "version": "0", "updatedAt": 0}
T0 = 1792000000.0


class Feed(object):
    """Applies redacted hook payloads for one session with increasing timestamps."""

    def __init__(self, model, sid="s1", tp="/nonexistent/s1.jsonl"):
        self.m, self.sid, self.tp, self.t = model, sid, tp, T0

    def __call__(self, ev, **kw):
        self.t += 1
        p = {"session_id": self.sid, "cwd": "/home/x/project/crew-hub", "transcript_path": self.tp,
             "hook_event_name": ev}
        p.update(kw)
        self.m.apply(redact(p, self.t))
        return self.m.sessions[self.sid]

    def task(self, tid, subject):
        return self("TaskCreated", task_id=tid, task_subject=subject, task_description="secret brief")

    def update(self, tid, to=None, frm="pending", subject=None, deleted_input=False):
        ti = {"taskId": tid}
        resp = {"success": True, "taskId": tid}
        if to and not deleted_input:
            ti["status"] = to
            resp["statusChange"] = {"from": frm, "to": to}
        if deleted_input:
            ti["status"] = "deleted"
        if subject:
            ti["subject"] = subject
        return self("PostToolUse", tool_name="TaskUpdate", tool_input=ti, tool_response=resp, tool_use_id="tu")

    def launch(self, aid, description):
        self("SubagentStart", agent_id=aid, agent_type="general-purpose")
        return self("PostToolUse", tool_name="Agent", tool_use_id="tu-" + aid,
                    tool_input={"description": description, "prompt": "do the secret thing",
                                "subagent_type": "savvy-flow:savvy-medium"},
                    tool_response={"agentId": aid, "description": description, "resolvedModel": "claude-opus-5-5",
                                   "isAsync": True})

    def stop(self, aid, atp=None):
        kw = {"agent_id": aid, "agent_type": "general-purpose", "last_assistant_message": "Done."}
        if atp:
            kw["agent_transcript_path"] = atp
        return self("SubagentStop", **kw)


class ParseTest(unittest.TestCase):
    def test_tier_and_project(self):
        self.assertEqual(parse_subject("[heavy] Crew Hub: Build the parser"),
                         ("heavy", "Crew Hub", "Crew Hub: Build the parser", "Build the parser"))
        self.assertEqual(parse_subject("Crew Hub: [Light] Bump versions"),
                         ("light", "Crew Hub", "Crew Hub: Bump versions", "Bump versions"))
        self.assertEqual(parse_subject("[MEDIUM]  Card   chips"), ("medium", None, "Card chips", "Card chips"))
        self.assertEqual(parse_subject("Plain task"), (None, None, "Plain task", "Plain task"))
        # unknown tiers are part of the title; no project when the prefix is too long or bracketed
        self.assertEqual(parse_subject("[fable] Design")[0], None)
        self.assertEqual(parse_subject("[fable] Design")[2], "[fable] Design")
        self.assertIsNone(parse_subject("x" * 50 + ": rest")[1])
        self.assertEqual(parse_subject(None), (None, None, "", ""))


class DerivedPlanTest(unittest.TestCase):
    def setUp(self):
        self.m = Model()
        self.f = Feed(self.m)

    def plan(self):
        return self.m.sessions["s1"]["plan"]

    def test_threshold(self):
        self.f("UserPromptSubmit")
        self.f.task("1", "Look something up")
        self.assertIsNone(self.plan())  # one plain task: a tiny todo list, no plan
        self.f.task("2", "Write it down")
        self.assertEqual((self.plan()["source"], self.plan()["total"]), ("tasks", 2))
        m = Model()
        f = Feed(m, sid="s2")
        f.task("1", "[light] Only one")
        p = m.sessions["s2"]["plan"]
        self.assertEqual((p["total"], p["tasks"][0]["tier"], p["tasks"][0]["title"]), (1, "light", "Only one"))
        # a TodoWrite list never becomes a plan
        f("PreToolUse", tool_name="TodoWrite", tool_use_id="x",
          tool_input={"todos": [{"content": "[heavy] a", "status": "pending"}, {"content": "b", "status": "pending"}]})
        self.assertIsNone(m.sessions["s2"]["plan"])

    def test_phases_and_join(self):
        f = self.f
        f("UserPromptSubmit")
        f.task("1", "[heavy] Crew Hub: Build the parser")
        f.task("2", "[medium] Crew Hub: Card chips")
        f.task("3", "[light] Crew Hub: Bump versions")
        p = self.plan()
        self.assertEqual((p["title"], p["phase"], p["done"], p["total"], p["finished"]),
                         ("Crew Hub: 3 tasks", "plan", 0, 3, False))
        self.assertEqual([(t["id"], t["title"], t["tier"], t["status"], t["order"]) for t in p["tasks"]],
                         [("1", "Build the parser", "heavy", "queued", 1), ("2", "Card chips", "medium", "queued", 2),
                          ("3", "Bump versions", "light", "queued", 3)])
        self.assertEqual(self.m.sessions["s1"]["name"], "Crew Hub")
        # exact title (case-insensitive, without the project prefix) and an explicit #id
        f.launch("a1", "build THE parser")
        f.launch("a2", "Something else entirely #2")
        f.launch("a3", "Unrelated helper")
        p = self.plan()
        self.assertEqual(p["phase"], "delegate")
        self.assertEqual([(t["status"], t["agentId"]) for t in p["tasks"]],
                         [("running", "a1"), ("running", "a2"), ("queued", None)])
        f.stop("a1")
        f.stop("a2")
        self.assertEqual(self.plan()["phase"], "review")
        f.update("1", "completed")
        f("TaskCompleted", task_id="1", task_subject="[heavy] Crew Hub: Build the parser")
        p = self.plan()
        self.assertEqual((p["done"], p["phase"]), (1, "review"))
        f.update("2", "completed")
        f.update("3", "in_progress")
        self.assertEqual([t["status"] for t in self.plan()["tasks"]], ["done", "done", "running"])
        self.assertEqual(self.plan()["phase"], "review")
        f.update("3", "completed", frm="in_progress")
        p = self.plan()
        self.assertEqual((p["phase"], p["finished"], p["done"]), ("finished", True, 3))
        self.assertIsInstance(p["updatedAt"], int)

    def test_failed_only_without_completion(self):
        f = self.f
        f.task("1", "[careful] Fix the bug")
        f.task("2", "[light] Tidy")
        f.launch("a1", "[careful] Fix the bug")  # tier prefix on the agent title is ignored for the join
        self.assertEqual(self.plan()["tasks"][0]["agentId"], "a1")
        f("PostToolUse", tool_name="TaskStop", tool_input={"task_id": "a1"}, tool_response={}, tool_use_id="ts")
        self.assertEqual(self.m.sessions["s1"]["agents"][0]["status"], "stopped")
        self.assertEqual([t["status"] for t in self.plan()["tasks"]], ["failed", "queued"])
        # a retry: the newest joined agent decides
        f.launch("a2", "Fix the bug")
        self.assertEqual((self.plan()["tasks"][0]["status"], self.plan()["tasks"][0]["agentId"]), ("running", "a2"))
        f.stop("a2")
        self.assertEqual(self.plan()["tasks"][0]["status"], "queued")
        f("PostToolUse", tool_name="TaskStop", tool_input={"task_id": "a1"}, tool_response={}, tool_use_id="ts2")
        f.update("1", "completed")
        self.assertEqual(self.plan()["tasks"][0]["status"], "done")
        # a stopped agent on a task the orchestrator completed stays done
        f.launch("a3", "Tidy")
        f("PostToolUse", tool_name="TaskStop", tool_input={"task_id": "a3"}, tool_response={}, tool_use_id="ts3")
        self.assertEqual(self.plan()["tasks"][1]["status"], "failed")
        f.update("2", "completed")
        self.assertEqual([t["status"] for t in self.plan()["tasks"]], ["done", "done"])

    def test_same_titles_and_unknown_ref(self):
        tasks = [{"id": "1", "subject": "Review", "status": "completed"}, {"id": "2", "subject": "Review",
                                                                            "status": "pending"},
                 {"id": "3", "subject": "Ship", "status": "pending"}]
        agents = [{"id": "a", "title": "review", "status": "running", "startedAt": 1},
                  {"id": "b", "title": "Ship #9", "status": "running", "startedAt": 2},  # unknown ref: title join
                  {"id": "c", "title": "ship it", "status": "running", "startedAt": 3}]
        j = join_agents(tasks, agents)
        self.assertEqual({k: [a["id"] for a in v] for k, v in j.items()}, {"2": ["a"], "3": ["b"]})

    def test_rename_delete_and_name_title(self):
        f = self.f
        f.task("1", "First")
        f.task("2", "Second")
        f.task("3", "Third")
        f.update("2", subject="[medium] Second, renamed")
        p = self.plan()
        self.assertEqual([(t["title"], t["tier"]) for t in p["tasks"]],
                         [("First", None), ("Second, renamed", "medium"), ("Third", None)])
        # no common project prefix: titled after the session (cwd basename here), not used as the session name
        self.assertEqual(p["title"], "crew-hub")
        self.assertEqual(self.m.sessions["s1"]["name"], "crew-hub")
        f.update("3", deleted_input=True)  # no statusChange in the response: the input's status is used
        f.update("1", "deleted")
        p = self.plan()
        self.assertEqual([t["id"] for t in p["tasks"]], ["2"])  # one task left, but it has a tier
        f.update("2", "deleted")
        self.assertIsNone(self.plan())

    def test_plan_file_wins(self):
        with tempfile.TemporaryDirectory() as d:
            plans = os.path.join(d, "plans")
            os.makedirs(plans)
            f = self.f
            f.task("1", "[heavy] A")
            f.task("2", "[light] B")
            self.assertEqual(self.plan()["source"], "tasks")
            path = os.path.join(plans, "s1.json")
            with open(path, "w") as fh:
                json.dump({"title": "Jamfin: disc engine", "phase": "build", "done": 0, "total": 1,
                           "tasks": [{"id": "t1", "title": "Probe", "status": "running"}]}, fh)
            self.m.enrich(plans_dir=plans)
            p = self.plan()
            self.assertEqual((p["source"], p["title"]), ("file", "Jamfin: disc engine"))
            f.update("1", "completed")
            self.assertEqual(self.plan()["source"], "file")
            os.unlink(path)
            self.m.enrich(plans_dir=plans)
            self.assertEqual((self.plan()["source"], self.plan()["done"]), ("tasks", 1))
        # state.json from 0.4.x: a file plan without "source"
        m = Model()
        m.load_dict({"sessions": {"x": {"id": "x", "status": "idle", "tasks": [], "agents": [], "_planSig": [1, 2],
                                        "plan": {"title": "Old", "tasks": []}, "updatedAt": 1}}})
        m._sync_plan(m.sessions["x"])
        self.assertEqual(m.sessions["x"]["plan"]["source"], "file")

    def test_derive_plan_pure(self):
        s = {"tasks": [{"id": "1", "subject": "a", "status": "pending"}], "agents": []}
        self.assertIsNone(derive_plan(s, "n"))
        s["tasks"].append({"id": "2", "subject": "b", "status": "completed"})
        p = derive_plan(s, "n")
        self.assertEqual((p["title"], p["phase"], p["done"]), ("n", "plan", 1))
        self.assertEqual(derive_plan(s, None)["title"], "Tasks")


def _assistant(mid, i, o, cr, cw, side=False, model="claude-opus-5-5", block="text"):
    return json.dumps({"type": "assistant", "isSidechain": side, "uuid": "u-" + mid,
                       "message": {"id": mid, "model": model, "role": "assistant", "content": [{"type": block}],
                                   "usage": {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,
                                             "cache_creation_input_tokens": cw,
                                             "cache_creation": {"ephemeral_5m_input_tokens": 999}}}}) + "\n"


class TokenTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, "-home-x-project")
        os.makedirs(self.dir)
        self.tp = os.path.join(self.dir, "s1.jsonl")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, path, text, mode="a"):
        with open(path, mode) as f:
            f.write(text)

    def test_read_usage_incremental(self):
        self.write(self.tp, json.dumps({"type": "user", "message": {"content": '"usage" "assistant"'}}) + "\n")
        self.write(self.tp, _assistant("m1", 2, 8, 100, 50, block="thinking") + _assistant("m1", 2, 8, 100, 50))
        tok, n = read_usage(self.tp, None, main_only=True)
        self.assertEqual(tok["t"], [2, 8, 100, 50, 1])
        self.assertEqual(n, os.path.getsize(self.tp))
        # the same message again with a grown output count, a new one, a sidechain one, a synthetic one, half a line
        self.write(self.tp, _assistant("m1", 2, 30, 100, 50) + _assistant("m2", 1, 5, 150, 0)
                   + _assistant("m3", 9, 9, 9, 9, side=True) + _assistant("m4", 7, 7, 7, 7, model="<synthetic>"))
        size = os.path.getsize(self.tp)
        half = _assistant("m5", 1, 1, 1, 1)
        self.write(self.tp, half[:20])
        tok, n = read_usage(self.tp, tok, main_only=True)
        self.assertEqual(tok["t"], [3, 35, 250, 50, 2])
        self.assertEqual(tok["off"], size)  # the half line waits
        self.write(self.tp, half[20:])
        tok, _ = read_usage(self.tp, tok, main_only=True)
        self.assertEqual(tok["t"], [4, 36, 251, 51, 3])
        # nothing new: nothing read
        self.assertEqual(read_usage(self.tp, tok, main_only=True)[1], 0)
        # a budget smaller than the file: totals build up over several calls, the same as one read
        many = "".join(_assistant("b%d" % k, 1, 2, 3, 4) for k in range(200))
        p2 = os.path.join(self.dir, "big.jsonl")
        self.write(p2, many)
        tok2, calls = None, 0
        while True:
            tok2, n = read_usage(p2, tok2, budget=1000)
            calls += 1
            if not n:
                break
        self.assertGreater(calls, 20)
        self.assertEqual(tok2["t"], [200, 400, 600, 800, 200])
        # a replaced (smaller) file starts over
        self.write(self.tp, _assistant("z", 5, 5, 5, 5), mode="w")
        tok, _ = read_usage(self.tp, tok, main_only=True)
        self.assertEqual(tok["t"], [5, 5, 5, 5, 1])
        self.assertEqual(read_usage(os.path.join(self.dir, "missing.jsonl"), None), (None, 0))

    def test_session_and_agent_tokens(self):
        m = Model()
        f = Feed(m, tp=self.tp)
        self.write(self.tp, _assistant("m1", 10, 20, 900, 70))
        f("UserPromptSubmit")
        f.task("1", "[medium] Work")
        f.launch("a1", "Work")
        # agent transcript at <session>/subagents/agent-<id>.jsonl (derived while running)
        apath = agent_transcript(self.tp, "a1")
        self.assertEqual(apath, os.path.join(self.dir, "s1", "subagents", "agent-a1.jsonl"))
        self.assertIsNone(agent_transcript(self.tp, "../x"))
        os.makedirs(os.path.dirname(apath))
        self.write(apath, _assistant("x1", 1, 2, 30, 4, side=True))
        now = int(f.t * 1000)
        m.enrich(now=now)
        s = m.sessions["s1"]
        self.assertEqual(s["tokens"], {"in": 10, "out": 20, "cacheRead": 900, "cacheWrite": 70, "cacheHit": 0.918,
                                       "turns": 1})
        self.assertEqual(s["agents"][0]["tokens"], {"in": 1, "out": 2, "cacheRead": 30, "cacheWrite": 4})
        # running agents refresh at most every 15 s
        self.write(apath, _assistant("x2", 1, 1, 1, 1, side=True))
        m.enrich(now=now + 1000)
        self.assertEqual(s["agents"][0]["tokens"]["in"], 1)
        m.enrich(now=now + AGENT_TOKENS_EVERY_MS + 1)
        self.assertEqual(s["agents"][0]["tokens"]["in"], 2)
        # session totals grow incrementally
        self.write(self.tp, _assistant("m2", 5, 5, 1000, 0))
        m.enrich(now=now + AGENT_TOKENS_EVERY_MS + 2)
        self.assertEqual((s["tokens"]["in"], s["tokens"]["turns"], s["tokens"]["cacheHit"]), (15, 2, 0.957))
        # at stop the hook's agent_transcript_path is summed once, right away (no 15 s wait)
        other = os.path.join(self.tmp.name, "agent-a1-copy.jsonl")
        self.write(apath, _assistant("x3", 4, 4, 4, 4, side=True))
        with open(apath) as src:
            self.write(other, src.read(), mode="w")
        f.stop("a1", atp=other)
        m.enrich(now=now + AGENT_TOKENS_EVERY_MS + 3)
        a = s["agents"][0]
        self.assertEqual(a["tokens"], {"in": 6, "out": 7, "cacheRead": 35, "cacheWrite": 9})
        self.assertEqual(a["_tokEnd"], a["endedAt"])
        self.write(other, _assistant("x4", 100, 0, 0, 0, side=True))
        m.enrich(now=now + 10 * AGENT_TOKENS_EVERY_MS)
        self.assertEqual(a["tokens"]["in"], 6)  # summed once at stop
        # nothing private in the snapshot: no paths, no reader state, no "_" keys
        text = json.dumps(m.snapshot(MACHINE))
        for leak in (self.tmp.name, "jsonl", '"_', "secret", "do the secret thing"):
            self.assertNotIn(leak, text)
        snap = m.snapshot(MACHINE)["sessions"][0]
        self.assertEqual(snap["tokens"]["turns"], 2)
        self.assertEqual(snap["agents"][0]["tokens"]["in"], 6)
        self.assertEqual(snap["plan"]["source"], "tasks")
        # the snapshot is a copy: changing it leaves the model alone
        snap["plan"]["tasks"].clear()
        snap["agents"][0]["tokens"]["in"] = -1
        self.assertEqual(len(s["plan"]["tasks"]), 1)
        self.assertEqual(a["tokens"]["in"], 6)

    def test_redact_agent_transcript_path_local_only(self):
        rec = redact({"session_id": "s", "hook_event_name": "SubagentStop", "agent_id": "a1",
                      "agent_transcript_path": "/home/x/.claude/projects/p/s/subagents/agent-a1.jsonl",
                      "last_assistant_message": "ok"}, 1.0)
        self.assertEqual(rec["atp"], "/home/x/.claude/projects/p/s/subagents/agent-a1.jsonl")
        # only SubagentStop carries it
        rec = redact({"session_id": "s", "hook_event_name": "PreToolUse", "tool_name": "Bash",
                      "agent_transcript_path": "/x/y.jsonl", "tool_input": {"command": "ls"}}, 1.0)
        self.assertNotIn("atp", rec)
        m = Model()
        m.apply(redact({"session_id": "s", "hook_event_name": "SubagentStop", "agent_id": "a1",
                        "agent_transcript_path": "/home/x/agent-a1.jsonl"}, 1.0))
        self.assertNotIn("agent-a1", json.dumps(m.snapshot(MACHINE)))

    def test_redact_task_update_subject(self):
        rec = redact({"session_id": "s", "hook_event_name": "PostToolUse", "tool_name": "TaskUpdate",
                      "tool_input": {"taskId": "3", "subject": "[light] New\nname", "description": "secret"},
                      "tool_response": {"success": True, "taskId": "3", "updatedFields": ["subject"]}}, 1.0)
        self.assertEqual((rec["taskId"], rec["subject"]), ("3", "[light] New"))
        self.assertNotIn("secret", json.dumps(rec))
        self.assertNotIn("statusChange", rec)


if __name__ == "__main__":
    unittest.main()
