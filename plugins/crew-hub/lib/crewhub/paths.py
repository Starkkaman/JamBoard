"""Locations under ~/.claude/crew-hub. Everything resolves HOME at call time so tests can use a temp HOME."""

import os


def claude_dir():
    return os.path.join(os.path.expanduser("~"), ".claude")


def hub_dir():
    return os.path.join(claude_dir(), "crew-hub")


def spool_path():
    return os.path.join(hub_dir(), "spool.jsonl")


def plans_dir():
    return os.path.join(hub_dir(), "plans")


def pid_path():
    return os.path.join(hub_dir(), "crew-hubd.pid")


def log_path():
    return os.path.join(hub_dir(), "crew-hubd.log")


def state_path():
    return os.path.join(hub_dir(), "state.json")


def snapshot_path():
    return os.path.join(hub_dir(), "snapshot.json")


def config_path():
    return os.path.join(hub_dir(), "config.json")


def secrets_path():
    return os.path.join(hub_dir(), "secrets.json")


def sessions_dir():
    return os.path.join(claude_dir(), "sessions")


def answers_dir():
    return os.path.join(hub_dir(), "answers")


def answer_path(request_id):
    """answers/<requestId>.json; the caller has already checked request_id is a uuid (no path characters)."""
    return os.path.join(answers_dir(), "%s.json" % request_id)


def claude_json_path():
    """~/.claude.json (read-only for us: project trust flags)."""
    return os.path.join(os.path.expanduser("~"), ".claude.json")


def ensure_hub_dir():
    """Create ~/.claude/crew-hub with mode 0700 (umask can only remove bits)."""
    d = hub_dir()
    try:
        os.makedirs(d, 0o700)
    except FileExistsError:
        pass
    return d


def ensure_answers_dir():
    """Create ~/.claude/crew-hub/answers with mode 0700 and tighten it if someone loosened it."""
    ensure_hub_dir()
    d = answers_dir()
    try:
        os.mkdir(d, 0o700)
    except FileExistsError:
        pass
    try:
        if os.stat(d).st_mode & 0o077:
            os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def user_claude_md():
    """~/.claude/CLAUDE.md (user-level memory; crew-hub-pair keeps one marked block in it)."""
    return os.path.join(claude_dir(), "CLAUDE.md")


def plugin_root():
    """<plugin>/ (this file is <plugin>/lib/crewhub/paths.py)."""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def plan_rule_template():
    return os.path.join(plugin_root(), "templates", "plan-file-rule.md")
