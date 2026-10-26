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


def sessions_dir():
    return os.path.join(claude_dir(), "sessions")


def ensure_hub_dir():
    """Create ~/.claude/crew-hub with mode 0700 (umask can only remove bits)."""
    d = hub_dir()
    try:
        os.makedirs(d, 0o700)
    except FileExistsError:
        pass
    return d
