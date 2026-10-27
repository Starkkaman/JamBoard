"""Plan-file rule block in the user-level ~/.claude/CLAUDE.md (always a temp HOME; never the real one)."""

import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

import _util
from crewhub import planrule

PAIR = os.path.join(_util.PLUGIN, "bin", "crew-hub-pair")


class PlanRuleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = self.tmp.name
        self.md = os.path.join(self.home, ".claude", "CLAUDE.md")
        self.env = mock.patch.dict(os.environ, {"HOME": self.home})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def text(self):
        with open(self.md, encoding="utf-8") as f:
            return f.read()

    def test_template_content(self):
        b = planrule.block()
        self.assertTrue(b.startswith(planrule.START + "\n") and b.endswith("\n" + planrule.END))
        for needle in ("~/.claude/crew-hub/plans/<local session_id>.json", "<Project>: <goal>", "agentId",
                       "heavy|careful|medium|light", "queued|running|done|failed", '"finished": true',
                       "$CLAUDE_CODE_SESSION_ID", "ccrSessionId", "Write"):
            self.assertIn(needle, b)

    def test_creates_missing_file_and_dir(self):
        self.assertEqual(planrule.install(), "added")
        self.assertEqual(self.text(), planrule.block() + "\n")
        self.assertEqual(planrule.install(), "unchanged")
        self.assertEqual(self.text().count(planrule.START), 1)

    def test_existing_content_kept_and_idempotent(self):
        os.makedirs(os.path.dirname(self.md))
        before = "# My rules\n\n- be nice\n- no emojis"  # no trailing newline
        with open(self.md, "w") as f:
            f.write(before)
        os.chmod(self.md, 0o600)
        self.assertEqual(planrule.install(), "added")
        first = self.text()
        self.assertTrue(first.startswith(before + "\n\n" + planrule.START))
        self.assertEqual(planrule.install(), "unchanged")
        self.assertEqual(self.text(), first)
        self.assertEqual(first.count(planrule.START), 1)
        self.assertEqual(stat.S_IMODE(os.stat(self.md).st_mode), 0o600)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.md))), ["CLAUDE.md"])  # no temp files

    def test_old_block_replaced_and_duplicates_dropped(self):
        os.makedirs(os.path.dirname(self.md))
        stale = "%s\nold rule text\n%s" % (planrule.START, planrule.END)
        with open(self.md, "w") as f:
            f.write("top\n\n%s\n\nmiddle\n%s\nbottom\n" % (stale, stale))
        self.assertEqual(planrule.install(), "updated")
        t = self.text()
        self.assertNotIn("old rule text", t)
        self.assertEqual(t.count(planrule.START), 1)
        self.assertEqual(t.count(planrule.END), 1)
        self.assertTrue(t.startswith("top\n\n" + planrule.block() + "\n\nmiddle\n"))
        self.assertTrue(t.endswith("bottom\n"))

    def test_unterminated_block_refused(self):
        os.makedirs(os.path.dirname(self.md))
        with open(self.md, "w") as f:
            f.write("keep\n%s\nhalf a block\n" % planrule.START)
        with self.assertRaises(planrule.RuleError):
            planrule.install()
        self.assertEqual(self.text(), "keep\n%s\nhalf a block\n" % planrule.START)

    def test_symlinked_file_is_followed(self):
        os.makedirs(os.path.dirname(self.md))
        real = os.path.join(self.home, "dotfiles-CLAUDE.md")
        with open(real, "w") as f:
            f.write("dotfiles\n")
        os.symlink(real, self.md)
        self.assertEqual(planrule.install(), "added")
        self.assertTrue(os.path.islink(self.md))
        with open(real) as f:
            self.assertIn(planrule.START, f.read())

    def test_cli_flag_twice(self):
        os.makedirs(os.path.dirname(self.md))
        with open(self.md, "w") as f:
            f.write("# mine\n")
        env = dict(os.environ, HOME=self.home)
        outs = []
        for _ in range(2):
            r = subprocess.run([PAIR, "--install-plan-rule"], env=env, stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
            self.assertEqual(r.returncode, 0, r.stderr)
            outs.append(r.stdout.decode())
        self.assertIn("plan-file rule added to", outs[0])
        self.assertIn("is up to date", outs[1])
        t = self.text()
        self.assertTrue(t.startswith("# mine\n\n" + planrule.START))
        self.assertEqual(t.count(planrule.START), 1)
        # it never touched the hub dir (no pairing, no daemon)
        self.assertFalse(os.path.exists(os.path.join(self.home, ".claude", "crew-hub")))


if __name__ == "__main__":
    unittest.main()
