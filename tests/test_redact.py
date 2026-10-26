import json
import os
import tempfile
import unittest

import _util  # noqa: F401  (sets sys.path)
from crewhub.redact import redact

FORBIDDEN = [
    "echo hi",  # Bash command
    "find /tmp",  # Bash command
    "wc -l",
    "Do exactly this",  # user prompt
    "task-notification",  # synthetic user prompt
    "Run the 'echo hi' subagent test",  # task_description (not allowlisted)
    "select:TaskCreate",  # ToolSearch query
    "total_deferred_tools",  # tool_response body
    "async_launched",
    "outputFile",
    "noOutputExpected",
    "Count files in the working directory and",  # never more than the description itself
]
FORBIDDEN_KEYS = ["prompt", "tool_input", "tool_response", "command", "task_description", "stdout", "background_tasks"]


def red(p, **kw):
    return redact(p, 1000.0, kw.get("pid"), kw.get("plans_dir"))


class RedactSampleTest(unittest.TestCase):
    def setUp(self):
        self.payloads = _util.sample_payloads()
        self.recs = [red(p) for p in self.payloads]

    def test_no_leaks(self):
        for rec in self.recs:
            text = json.dumps(rec, ensure_ascii=False)
            for bad in FORBIDDEN:
                self.assertNotIn(bad, text, rec)
            for key in FORBIDDEN_KEYS:
                self.assertNotIn(key, rec)

    def test_common_fields(self):
        for p, rec in zip(self.payloads, self.recs):
            self.assertEqual(rec["v"], 1)
            self.assertEqual(rec["ev"], p["hook_event_name"])
            self.assertEqual(rec["sid"], p["session_id"])
            self.assertEqual(rec["ts"], 1000.0)
            self.assertEqual(rec["cwd"], "<path>")
            self.assertEqual(rec["tp"], "<path>")
            if "tool_use_id" in p:
                self.assertEqual(rec["tuid"], p["tool_use_id"])
            if "tool_name" in p:
                self.assertEqual(rec["tool"], p["tool_name"])

    def by(self, ev, tool=None, agent=None):
        return [r for r in self.recs if r["ev"] == ev and (tool is None or r.get("tool") == tool)
                and (agent is None or r.get("agent") == agent)]

    def test_event_fields(self):
        self.assertEqual(self.by("SessionStart")[0]["source"], "startup")
        self.assertEqual(self.by("SessionEnd")[0]["reason"], "other")
        self.assertNotIn("msg", self.by("UserPromptSubmit")[0])
        self.assertEqual(self.by("Stop")[0]["msg"], "done")
        self.assertEqual(self.by("SubagentStop")[0]["msg"], "hi")
        created = self.by("TaskCreated")
        self.assertEqual([(r["task_id"], r["subject"]) for r in created], [("1", "say hi"), ("2", "count files")])
        self.assertEqual([r["task_id"] for r in self.by("TaskCompleted")], ["1", "2"])

    def test_bash_and_agent(self):
        pre = self.by("PreToolUse", "Bash", agent="af0d389234162b24e")[0]
        self.assertEqual(pre["desc"], "Print hi via echo")
        self.assertEqual(pre["atype"], "general-purpose")
        self.assertEqual(pre["eff"], "medium")
        main_bash = [r for r in self.by("PreToolUse", "Bash") if "agent" not in r][0]
        self.assertEqual(main_bash["desc"], "Count files in the working directory")
        a_pre = self.by("PreToolUse", "Agent")[0]
        self.assertEqual(a_pre["desc"], "Say hi test")
        self.assertEqual(a_pre["sat"], "general-purpose")
        a_post = self.by("PostToolUse", "Agent")[0]
        self.assertEqual(a_post["aid"], "af0d389234162b24e")
        self.assertEqual(a_post["title"], "Say hi test")
        self.assertEqual(a_post["model"], "claude-haiku-5-5")
        self.assertIs(a_post["async"], True)
        sub = self.by("SubagentStart")[0]
        self.assertEqual((sub["agent"], sub["atype"]), ("af0d389234162b24e", "general-purpose"))

    def test_task_update(self):
        ups = self.by("PostToolUse", "TaskUpdate")
        self.assertEqual([(r["taskId"], r["statusChange"]) for r in ups],
                         [("1", {"from": "pending", "to": "completed"}), ("2", {"from": "pending", "to": "completed"})])
        # ToolSearch: name only
        ts = self.by("PreToolUse", "ToolSearch")[0]
        self.assertEqual(set(ts) - {"v", "ts", "ev", "sid", "cwd", "tp", "tuid", "eff", "tool"}, set())


class RedactSyntheticTest(unittest.TestCase):
    def base(self, **kw):
        p = {"session_id": "s1", "cwd": "/home/x/project/jamfin", "transcript_path": "/t.jsonl",
             "hook_event_name": "PreToolUse", "tool_use_id": "tu1"}
        p.update(kw)
        return p

    def test_agent_prompt_never_copied(self):
        p = self.base(tool_name="Agent", tool_input={"description": "Build it", "subagent_type": "savvy-flow:savvy-medium",
                                                     "prompt": "Run the Bash command rm -rf / and report"})
        rec = red(p)
        self.assertNotIn("Run the Bash command", json.dumps(rec))
        self.assertEqual((rec["desc"], rec["sat"], rec["cwd"]), ("Build it", "savvy-flow:savvy-medium", "jamfin"))

    def test_bash_without_description(self):
        rec = red(self.base(tool_name="Bash", tool_input={"command": "curl -H 'Authorization: x' http://h"}))
        self.assertNotIn("desc", rec)
        self.assertNotIn("curl", json.dumps(rec))

    def test_long_multiline_description(self):
        rec = red(self.base(tool_name="Bash", tool_input={"command": "x", "description": "line one\nsecret two " + "y" * 500}))
        self.assertEqual(rec["desc"], "line one")
        rec = red(self.base(tool_name="Bash", tool_input={"command": "x", "description": "z" * 500}))
        self.assertEqual(len(rec["desc"]), 200)

    def test_files_hosts_mcp_search(self):
        r = red(self.base(tool_name="Read", tool_input={"file_path": "/home/node/secret/dir/notes.txt"}))
        self.assertEqual(r["file"], "notes.txt")
        self.assertNotIn("secret", json.dumps(r))
        r = red(self.base(tool_name="Edit", tool_input={"file_path": "/a/b.py", "old_string": "PASSWORD", "new_string": "x"}))
        self.assertEqual(r["file"], "b.py")
        self.assertNotIn("PASSWORD", json.dumps(r))
        r = red(self.base(tool_name="WebFetch", tool_input={"url": "https://user:pw@example.com:8443/p?token=abc",
                                                            "prompt": "summarise"}))
        self.assertEqual(r["host"], "example.com")
        self.assertNotIn("token", json.dumps(r))
        self.assertNotIn("pw", json.dumps(r))
        r = red(self.base(tool_name="mcp__home-assistant__ha_call_service", tool_input={"service": "unlock"}))
        self.assertEqual(r["mcp"], {"server": "home-assistant", "tool": "ha_call_service"})
        self.assertNotIn("unlock", json.dumps(r))
        for tool, ti in (("Grep", {"pattern": "hunter2"}), ("Glob", {"pattern": "**/*.key"}),
                         ("WebSearch", {"query": "my secret query"}), ("Skill", {"skill": "x", "args": "secret"})):
            r = red(self.base(tool_name=tool, tool_input=ti))
            self.assertEqual(r["tool"], tool)
            for v in ti.values():
                self.assertNotIn(v, json.dumps(r))

    def test_todowrite(self):
        r = red(self.base(tool_name="TodoWrite", tool_input={"todos": [
            {"content": "Write tests", "status": "in_progress", "activeForm": "Writing tests"},
            {"content": "x" * 300, "status": "pending"}]}))
        self.assertEqual(r["todos"][0], {"content": "Write tests", "status": "in_progress"})
        self.assertEqual(len(r["todos"][1]["content"]), 120)

    def test_permission_request_and_notification(self):
        r = red(self.base(hook_event_name="PermissionRequest", tool_name="Bash",
                          tool_input={"command": "sudo reboot", "description": "Reboot the box"}))
        self.assertEqual((r["tool"], r["desc"]), ("Bash", "Reboot the box"))
        self.assertNotIn("sudo", json.dumps(r))
        r = red({"session_id": "s1", "hook_event_name": "Notification", "notification_type": "permission_prompt",
                 "message": "Claude needs your permission to use Bash"})
        self.assertEqual((r["ntype"], r["msg"]), ("permission_prompt", "Claude needs your permission to use Bash"))

    def test_failure_has_no_error_text(self):
        r = red(self.base(hook_event_name="PostToolUseFailure", tool_name="Bash",
                          tool_input={"command": "cat /etc/shadow", "description": "Read file"},
                          error="cat: /etc/shadow: Permission denied"))
        self.assertNotIn("shadow", json.dumps(r))

    def test_plan_path(self):
        with tempfile.TemporaryDirectory() as d:
            plans = os.path.join(d, "plans")
            os.makedirs(plans)
            target = os.path.join(plans, "sess-1.json")
            p = self.base(hook_event_name="PostToolUse", tool_name="Write",
                          tool_input={"file_path": target, "content": "{\"secret\": 1}"}, tool_response={"x": 1})
            r = red(p, plans_dir=plans)
            self.assertEqual(r["plan"], os.path.realpath(target))
            self.assertNotIn("secret", json.dumps(r))
            # outside the plans dir, wrong extension, traversal: no plan
            for fp in (os.path.join(d, "x.json"), os.path.join(plans, "a.txt"), os.path.join(plans, "..", "x.json")):
                p["tool_input"] = {"file_path": fp}
                self.assertNotIn("plan", red(p, plans_dir=plans))
            # PreToolUse never sets plan
            p["hook_event_name"] = "PreToolUse"
            p["tool_input"] = {"file_path": target}
            self.assertNotIn("plan", red(p, plans_dir=plans))

    def test_garbage_input(self):
        for p in (None, [], "x", {"hook_event_name": 5}, {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "str"}):
            rec = red(p)
            self.assertEqual(rec["v"], 1)


class SummarySanitizeTest(unittest.TestCase):
    def stop(self, msg, ev="Stop", **kw):
        return redact({"session_id": "s1", "hook_event_name": ev, "last_assistant_message": msg}, 1000.0, None, None, **kw)

    def test_path_leak(self):
        r = self.stop("Done: edited /home/node/project/secret-proj/lib/notes.py and ~/.claude/crew-hub/config.json.")
        self.assertEqual(r["msg"], "Done: edited notes.py and config.json.")
        for bad in ("/home", "node", "secret-proj", ".claude", "crew-hub/"):
            self.assertNotIn(bad, r["msg"])
        # one-segment tokens and ratios are not paths
        self.assertEqual(self.stop("and/or ~/x 1/2 /tmp")["msg"], "and/or ~/x 1/2 /tmp")

    def test_url_with_query_leak(self):
        r = self.stop("Fetched https://user:pw@api.example.com:8443/v1/items?token=hunter2&id=9 fine")
        self.assertEqual(r["msg"], "Fetched api.example.com fine")
        for bad in ("hunter2", "token", "user", "pw@", "/v1", "8443"):
            self.assertNotIn(bad, r["msg"])

    def test_token_leak(self):
        fake = "sk-ant-api03-Zx9Qw8Er7Ty6Ui5Op4As3Df2Gh1"
        r = self.stop("Configured key %s and id 7c1addf5-73c5-41d7-9225-77ba4bbbb2d5 for well-understood-multipart-code" % fake,
                      ev="SubagentStop")
        self.assertNotIn("Zx9Qw8", r["msg"])
        self.assertNotIn("7c1addf5", r["msg"])
        self.assertIn("well-understood-multipart-code", r["msg"])  # letters only: not token-like
        self.assertEqual(r["msg"].count("\u2026"), 2)

    def test_email_leak(self):
        r = self.stop("Sent it to someone.else+tag@mail.example.org today")
        self.assertEqual(r["msg"], "Sent it to \u2026 today")

    def test_first_line_and_length(self):
        r = self.stop("Short headline\n/home/node/secret second line https://x.example.com/?k=v")
        self.assertEqual(r["msg"], "Short headline")
        r = self.stop("word " * 100)
        self.assertLessEqual(len(r["msg"]), 120)
        self.assertTrue(r["msg"].endswith("\u2026"))
        self.assertNotIn("msg", self.stop("   \n  "))
        self.assertNotIn("msg", self.stop(42))

    def test_summaries_off(self):
        for ev in ("Stop", "SubagentStop"):
            self.assertNotIn("msg", self.stop("all good", ev=ev, summaries=False))
        # Notification messages are not summaries
        n = redact({"session_id": "s1", "hook_event_name": "Notification", "message": "Claude is waiting for your input"},
                   1.0, summaries=False)
        self.assertEqual(n["msg"], "Claude is waiting for your input")


if __name__ == "__main__":
    unittest.main()
